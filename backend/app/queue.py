"""异步补做队列（设计文档 §5 / §14 第 5 条）：进程内 asyncio 队列 + 单 worker。

⚠️ 队列在进程内存中：uvicorn 必须固定单 worker（--workers 1），多 worker 会重复处理 pending。

管线（§14 第 6 条顺序：结构化 → embedding → 查重 → FTS；M1 无 embedding）：
  1. pending 直存笔记：LLM 补整理（失败走指数退避重试 1m/5m/15m/1h/6h，最多 5 次 → failed）；
  2. FTS 同步（幂等，不依赖 LLM）；
  3. 查重（M1 FTS 近似版）：命中标 duplicate；失败只记日志不反噬（§21.2 #5）。
启动时扫描 status='pending' 补处理；failed 不自动重试（留待手动 reprocess，§15.5 #4）。
"""
from __future__ import annotations

import asyncio
import logging

from . import config, db, dedup, llm, notes

logger = logging.getLogger(__name__)


class ReprocessQueue:
    def __init__(self) -> None:
        self._q: asyncio.Queue[int] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._retries: dict[int, int] = {}
        self._inflight: set[int] = set()
        self._queued: set[int] = set()

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="reprocess-queue")

    async def stop(self) -> None:
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    def submit(self, note_id: int) -> None:
        if note_id in self._inflight or note_id in self._queued:
            return
        self._queued.add(note_id)
        self._q.put_nowait(note_id)

    def scan_pending(self) -> None:
        """应用启动时补处理（§14 第 5 条）。"""
        conn = db.connect()
        try:
            rows = conn.execute("SELECT id FROM notes WHERE status = 'pending'").fetchall()
        finally:
            conn.close()
        for row in rows:
            self.submit(row["id"])

    # ------------------------------------------------------------------ worker
    async def _run(self) -> None:
        while True:
            note_id = await self._q.get()
            self._queued.discard(note_id)
            self._inflight.add(note_id)
            try:
                await asyncio.to_thread(self._process, note_id)
                self._retries.pop(note_id, None)
            except Exception as e:  # noqa: BLE001 —— 任务失败统一走退避
                self._schedule_retry(note_id, e)
            finally:
                self._inflight.discard(note_id)

    def _schedule_retry(self, note_id: int, exc: Exception) -> None:
        attempt = self._retries.get(note_id, 0) + 1
        self._retries[note_id] = attempt
        if attempt <= len(config.BACKOFF_SCHEDULE):
            delay = config.BACKOFF_SCHEDULE[attempt - 1]
            logger.warning("笔记 #%s 补处理失败（第 %d 次，%ds 后重试）：%s", note_id, attempt, delay, exc)

            async def _retry_later() -> None:
                await asyncio.sleep(delay)
                self.submit(note_id)

            asyncio.create_task(_retry_later())
        else:
            logger.error("笔记 #%s 补处理失败已达上限，标 failed（留待手动 reprocess）：%s", note_id, exc)
            conn = db.connect()
            try:
                conn.execute(
                    "UPDATE notes SET status='failed' WHERE id=? AND status='pending'", (note_id,)
                )
                conn.commit()
            finally:
                conn.close()

    def _process(self, note_id: int) -> None:
        conn = db.connect()
        try:
            note = db.fetch_note(conn, note_id)
            if not note:
                return
            # 1. 直存笔记补整理（§14 第 5 条）；失败抛异常走退避
            if note["status"] == "pending" and not note.get("title"):
                history = [{"role": "user", "content": note["raw"]}]
                data = llm.organize_conversation(history, force_json=True)["organized"]
                notes.apply_organized(conn, note_id, data)
                note = db.fetch_note(conn, note_id)
            # 2. FTS 同步（幂等；落库事务已同步过，这里防御性再同步）
            db.fts_sync(conn, note_id)
            # 3. 查重（M1 FTS 近似版；失败只记日志，不反噬入库，§21.2 #5）
            dup = None
            try:
                dup = dedup.check_duplicate(conn, note)
            except llm.LLMError as e:
                logger.warning("笔记 #%s 查重失败（不反噬，保持原状态）：%s", note_id, e)
            if dup and dup != note_id:
                conn.execute("UPDATE notes SET status='duplicate' WHERE id = ?", (note_id,))
            elif note["status"] == "pending":
                conn.execute("UPDATE notes SET status='processed' WHERE id = ?", (note_id,))
            conn.commit()
        finally:
            conn.close()
