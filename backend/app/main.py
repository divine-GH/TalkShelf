"""note-brain FastAPI 入口。

⚠️ 必须单 worker 运行（--workers 1）：异步补做队列在进程内存中，
多 worker 会各持一个队列、把同一条 pending 笔记重复处理（设计文档 §5）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
    logger.info("note-brain 启动完成，DB: %s", config.DATABASE_PATH)
    try:
        yield
    finally:
        await q.stop()


app = FastAPI(title="note-brain", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
app.include_router(api.router)
