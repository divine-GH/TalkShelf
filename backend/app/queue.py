"""异步补做队列（设计文档 §5 / §14 第 5、8 条）：进程内 asyncio 队列 + 单 worker。

⚠️ 队列在进程内存中：uvicorn 必须固定单 worker（--workers 1），多 worker 会重复处理 pending。

管线（§14 第 6 条顺序：结构化 → embedding → 查重 → FTS；M2 起含 embedding）：
  1. pending 直存笔记：LLM 补整理（失败走指数退避 1m/5m/15m/1h/6h，最多 5 次 → failed）；
  2. embedding 补算（缺向量才算；失败退避重试但**不标 failed**——保持状态，恢复后由启动扫描/下次提交补，
     §14 第 8 条「恢复后补 embedding」语义）；
  3. FTS 同步（幂等，不依赖 LLM）；
  4. 查重（M2 向量版，Ollama 不可用退 FTS 近似版）：**只对新笔记跑一次**——submit 带 dedup 标记，
     启动扫描补向量的老笔记不查重（防误标）；命中标 duplicate；失败只记日志不反噬（§21.2 #5）。
启动时扫描 status='pending' 与「已 processed 但缺向量」的笔记补做；failed 不自动重试（留待手动 reprocess，§15.5 #4）。
"""

from __future__ import annotations

import asyncio
import logging

from . import config, db, dedup, embedding, llm, notes

logger = logging.getLogger(__name__)


class ReprocessQueue:
    def __init__(self) -> None:
        self._q: asyncio.Queue[tuple[int, bool]] = asyncio.Queue()  # (note_id, dedup)
        self._worker: asyncio.Task | None = None
        self._retries: dict[int, int] = {}
        self._inflight: set[int] = set()
        self._queued: set[int] = set()
        self._no_dedup: set[int] = set()  # 补向量老笔记：查重跳过（防误标）

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

    def submit(self, note_id: int, dedup: bool = True) -> None:
        if note_id in self._inflight or note_id in self._queued:
            return
        self._queued.add(note_id)
        if not dedup:
            self._no_dedup.add(note_id)
        self._q.put_nowait((note_id, dedup))

    def scan_pending(self) -> None:
        """应用启动时补做（§14 第 5、8 条）：pending 全量 + processed 缺向量。"""
        conn = db.connect()
        try:
            rows = conn.execute(
                """SELECT id, status FROM notes WHERE status = 'pending'
                   UNION
                   SELECT id, status FROM notes WHERE status = 'processed'
                     AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.note_id = notes.id)"""
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            # pending（待整理，未查过重）→ dedup；processed 缺向量（老笔记）→ 只补向量不查重
            self.submit(row["id"], dedup=(row["status"] == "pending"))

    # ------------------------------------------------------------------ worker
    async def _run(self) -> None:
        while True:
            note_id, dedup = await self._q.get()
            self._queued.discard(note_id)
            self._inflight.add(note_id)
            try:
                await asyncio.to_thread(self._process, note_id, dedup)
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
            logger.warning(
                "笔记 #%s 补处理失败（第 %d 次，%ds 后重试）：%s", note_id, attempt, delay, exc
            )

            async def _retry_later() -> None:
                await asyncio.sleep(delay)
                self.submit(note_id, dedup=note_id not in self._no_dedup)

            asyncio.create_task(_retry_later())
        elif isinstance(exc, embedding.EmbeddingError):
            # embedding 失败不标 failed：保持状态，恢复后由启动扫描/下次提交补（§14 第 8 条）
            logger.error(
                "笔记 #%s embedding 补算失败已达上限，保持原状态（Ollama 恢复后重启自动补）：%s",
                note_id,
                exc,
            )
        else:
            logger.error(
                "笔记 #%s 补处理失败已达上限，标 failed（留待手动 reprocess）：%s", note_id, exc
            )
            conn = db.connect()
            try:
                conn.execute(
                    "UPDATE notes SET status='failed' WHERE id=? AND status='pending'", (note_id,)
                )
                conn.commit()
            finally:
                conn.close()

    def _process(self, note_id: int, run_dedup: bool) -> None:
        conn = db.connect()
        try:
            note = db.fetch_note(conn, note_id)
            if not note:
                return
            # 1. 直存笔记补整理（§14 第 5 条）；失败抛 LLMError 走退避 → failed。
            #    分段提交：整理结果先落库——后续阶段失败不回滚已完成的 LLM 成果（幂等重试不重花 token）。
            #    历史优先取归档对话消息（含抓取/搜索材料，§32 快速记录），无则回落 raw-only。
            if note["status"] == "pending" and not note.get("title"):
                history = notes.conversation_history(conn, note_id) or [
                    {"role": "user", "content": note["raw"]}
                ]
                data = llm.organize_conversation(history, force_json=True)["organized"]
                # 快速记录：kind（兴趣/收藏）由 LLM 判断并覆盖占位值（§32）；普通直存不动 kind
                notes.apply_organized(conn, note_id, data, set_kind=bool(note.get("quick")))
                conn.commit()
                note = db.fetch_note(conn, note_id)
            # 2. embedding 补算（缺向量才算；失败抛 EmbeddingError 走退避但不标 failed，§14 第 8 条）
            has_emb = conn.execute(
                "SELECT 1 FROM embeddings WHERE note_id = ?", (note_id,)
            ).fetchone()
            if not has_emb:
                vec = embedding.embed_note(note)
                embedding.save_embedding(conn, note_id, vec)
                conn.commit()
            # 3. FTS 同步（幂等；落库事务已同步过，这里防御性再同步）
            db.fts_sync(conn, note_id)
            # 4. 查重（仅新笔记一次；失败只记日志，不反噬入库，§21.2 #5）
            #    命中时落库 duplicate_of（§24：供详情页「疑似重复于 #id」提示与合并交互）
            if run_dedup:
                dup = None
                try:
                    dup = dedup.check_duplicate(conn, note)
                except llm.LLMError as e:
                    logger.warning("笔记 #%s 查重失败（不反噬，保持原状态）：%s", note_id, e)
                if dup and dup != note_id:
                    conn.execute(
                        "UPDATE notes SET status='duplicate', duplicate_of=? WHERE id = ?",
                        (dup, note_id),
                    )
                elif note["status"] == "pending":
                    conn.execute("UPDATE notes SET status='processed' WHERE id = ?", (note_id,))
            elif note["status"] == "pending":
                # 只补向量路径（启动扫描的老笔记/embedding 恢复后）：不查重，但推进状态
                conn.execute("UPDATE notes SET status='processed' WHERE id = ?", (note_id,))
            conn.commit()
        finally:
            conn.close()
