"""TalkShelf FastAPI 入口。

⚠️ 必须单 worker 运行（--workers 1）：异步补做队列与对话后台整理都在进程内存中，
多 worker 会各持一份、把同一条 pending 笔记/对话重复处理（设计文档 §5 / §32）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from . import api, config, db, queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    q = queue.ReprocessQueue()
    app.state.queue = q
    q.start()
    q.scan_pending()  # 启动扫描 status='pending' 补处理（§14 第 5 条；failed 不自动重试）
    # 对话后台整理（§32 / §36）：sync 端点经 kick() 调度任务到本事件循环（线程池线程跑 LLM/抓取/检索）；
    # 两个 runner 共用同一调度骨架，按 status 区分（draft=记录对话 / search=检索会话）
    app.state.conv_runner = api.ConversationRunner(
        asyncio.get_running_loop(), "draft", api._process_round
    )
    app.state.search_runner = api.ConversationRunner(
        asyncio.get_running_loop(), "search", api._process_search_round
    )
    logger.info("TalkShelf 启动完成，DB: %s", config.DATABASE_PATH)
    try:
        yield
    finally:
        await app.state.conv_runner.stop()
        await app.state.search_runner.stop()
        await q.stop()


# M4 部署加固：公网不暴露 API 形状（/docs Swagger UI 与 /openapi.json 默认开启，全部关闭）。
# 页面上游各页面路由已自带鉴权；开发时要在本地看接口文档，临时把 docs_url 改回 "/docs" 即可。
app = FastAPI(
    title="TalkShelf",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def static_no_cache(request: Request, call_next):
    """/static 一律 Cache-Control: no-cache：浏览器每次重新校验（304 则用缓存）。

    StaticFiles 不发送 Cache-Control，浏览器会按 Last-Modified 启发式缓存
    （旧 app.js/style.css 可被缓存数小时）——改 UI 后用户拿到旧 JS/CSS，
    「更多功能」按钮无反应、样式不生效（§26.4）。
    """
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """安全响应头（M4 部署加固）：全响应统一下发。

    - X-Content-Type-Options: nosniff —— 禁止 MIME 嗅探；
    - X-Frame-Options: DENY + CSP frame-ancestors 'none' —— 禁止被 iframe 套壳（点击劫持）；
    - CSP：default-src 'self' + 内联脚本/样式白名单（模板全内联 script/style，见各 *.html），
      收紧外部资源载入面；img-src 保留 data:（data URI 图像位，未用不碍事）；
    - Referrer-Policy: strict-origin-when-cross-origin —— 减少跨站 Referrer 泄露；
    - HSTS 仅在 HTTPS 场景下发（CF 边缘经隧道透传 X-Forwarded-Proto: https；
      本地 http 访问不下发，避免误伤）。CF 面板侧仍建议开 Always Use HTTPS + HSTS 兜底。
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'",
    )
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if proto == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=15552000")
    return response


app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
app.include_router(api.router)
