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


app = FastAPI(title="TalkShelf", lifespan=lifespan)


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


app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
app.include_router(api.router)
