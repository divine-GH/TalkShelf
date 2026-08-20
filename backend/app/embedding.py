"""Ollama embedding 接入（设计文档 §6.1 / §14 第 7、8 条；M2）。

- 调本地 Ollama /api/embed（bge-m3，1024 维），失败抛 EmbeddingError；
- 向量存 embeddings 表（BLOB float32，§4：np.frombuffer 零拷贝）；
- 全量加载到内存算余弦（个人万级毫秒级，§4「向量检索实现」）；
- 检索侧降级策略（§7 / §14 第 8 条）：Ollama 不可用 → 调用方跳过向量路走 FTS，不报错。
"""

from __future__ import annotations

import logging
import sqlite3

import httpx
import numpy as np

from . import config, db, settings

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Ollama embedding 调用失败（网络/HTTP/模型缺失）。调用方按降级策略处理。"""


def _embed_model() -> str:
    """当前生效的 embedding 模型（设置页可改，§28；未改回落 .env EMBED_MODEL）。"""
    return settings.resolve_str(settings.KEY_EMBED_MODEL, config.EMBED_MODEL)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """调 Ollama 批量计算 embedding。失败一律抛 EmbeddingError。"""
    if not texts:
        return []
    body = {"model": _embed_model(), "input": texts}
    try:
        with httpx.Client(timeout=config.EMBED_TIMEOUT) as client:
            resp = client.post(f"{config.OLLAMA_URL}/api/embed", json=body)
    except httpx.HTTPError as e:
        raise EmbeddingError(f"Ollama 连接失败: {e}") from e
    if resp.status_code != 200:
        raise EmbeddingError(f"Ollama HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    embeds = data.get("embeddings")
    if not isinstance(embeds, list) or len(embeds) != len(texts):
        raise EmbeddingError("Ollama 返回的 embeddings 数量与输入不符")
    return embeds


def _note_embedding_text(note: dict) -> str:
    """向量化用的文本组装：标题/摘要/正文/原文/标签，截断到上限（§7 Tier 1 检索面字段）。"""
    tags = " ".join(note.get("tags") or [])
    parts = [
        f"标题：{note.get('title') or ''}",
        f"摘要：{note.get('summary') or ''}",
        f"正文：{note.get('content') or ''}",
        f"原文：{note.get('raw') or ''}",
        f"标签：{tags}",
    ]
    return "\n".join(parts)[: config.EMBED_TEXT_LIMIT]


def embed_note(note: dict) -> np.ndarray:
    """计算单条笔记的 embedding，返回 float32 向量。失败抛 EmbeddingError。"""
    vecs = embed_texts([_note_embedding_text(note)])
    return np.asarray(vecs[0], dtype="<f4")


def save_embedding(conn: sqlite3.Connection, note_id: int, vec: np.ndarray) -> None:
    """写入 embeddings 表（BLOB float32；INSERT OR REPLACE 幂等）。"""
    conn.execute(
        "INSERT OR REPLACE INTO embeddings(note_id, vector) VALUES (?, ?)",
        (note_id, np.asarray(vec, dtype="<f4").tobytes()),
    )


def load_all_embeddings(conn: sqlite3.Connection) -> dict[int, np.ndarray]:
    """全量加载 {note_id: float32 向量}（零拷贝 np.frombuffer）。"""
    rows = conn.execute("SELECT note_id, vector FROM embeddings").fetchall()
    return {r["note_id"]: np.frombuffer(r["vector"], dtype="<f4") for r in rows}


def cosine_top_k(
    query_vec: np.ndarray,
    vectors: dict[int, np.ndarray],
    top_k: int,
    *,
    exclude_ids: set[int] | None = None,
) -> list[tuple[int, float]]:
    """余弦相似度 Top-K，返回 [(note_id, score)] 降序。query_vec 先归一化。"""
    exclude = exclude_ids or set()
    q = np.asarray(query_vec, dtype="<f4")
    q = q / (np.linalg.norm(q) + 1e-9)
    scored: list[tuple[int, float]] = []
    for note_id, vec in vectors.items():
        if note_id in exclude or len(vec) != len(q):
            continue
        v = vec / (np.linalg.norm(vec) + 1e-9)
        scored.append((note_id, float(np.dot(q, v))))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def vector_candidates(
    conn: sqlite3.Connection,
    note: dict,
    top_k: int = config.DEDUP_VECTOR_TOP_K,
) -> list[dict]:
    """查重用向量召回候选（排除自身与 merged）。

    新笔记的 embedding 通常已在库中（队列管线先算后查重）；不在库时现场算一条。
    Ollama 失败抛 EmbeddingError（调用方降级 FTS 近似版）。
    """
    vectors = load_all_embeddings(conn)
    if not vectors:
        raise EmbeddingError("库内无任何 embedding")
    own = conn.execute("SELECT vector FROM embeddings WHERE note_id = ?", (note["id"],)).fetchone()
    query_vec = np.frombuffer(own["vector"], dtype="<f4") if own else embed_note(note)
    hits = cosine_top_k(query_vec, vectors, top_k, exclude_ids={note["id"]})
    out: list[dict] = []
    for note_id, _score in hits:
        cand = db.fetch_note(conn, note_id)
        if cand and cand["status"] != "merged":
            out.append(cand)
        if len(out) >= top_k:
            break
    return out
