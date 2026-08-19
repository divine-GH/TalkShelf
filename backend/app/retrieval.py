"""检索层（设计文档 §7）：向量 + FTS 双路召回 → RRF 融合 → 材料层兜底。

流程（§7 主流程图）：
  1. 向量召回 Top-K=8（余弦；Ollama 不可用/无向量时整路跳过）；
  2. FTS5 trigram 关键词召回 Top-K=5（BM25；查询词 <3 字符时跳过）；
  3. 合并去重，RRF 融合：score = Σ 1/(60 + rank)，取 Top-N=6；
  4. 材料层兜底（Tier 2）：Top-1 向量相似度 < 0.4 或 FTS 路无命中时，
     追加 materials_fts 关键词召回 Top-5；命中不参与 RRF，附结果末尾并标注"命中于来源材料"。
"""
from __future__ import annotations

import logging
import sqlite3

import numpy as np

from . import config, db, embedding

logger = logging.getLogger(__name__)


def fts_search(conn: sqlite3.Connection, query: str, top_k: int) -> list[int]:
    """FTS5 trigram 关键词召回（BM25 排序）。查询词 <3 字符时跳过（trigram 限制，§4 关键点 3）。

    返回 notes.id 列表（含重复排序，交由 RRF 计算 rank；此处按 rank 升序已隐含）。
    材料层召回用同函数（materials_fts）。
    """
    words = [w for w in query.split() if len(w) >= 3]
    if not words:
        return []
    expr = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
    try:
        rows = conn.execute(
            "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (expr, top_k),
        ).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("FTS 检索查询失败（%s）", e)
        return []
    return [r["rowid"] for r in rows]


def materials_fts_search(conn: sqlite3.Connection, query: str, top_k: int) -> list[dict]:
    """材料层召回（Tier 2 兜底，§7）：materials_fts 关键词 Top-K，返回
    {id, note_id, kind, url, text} 列表（text 截断为摘要，供展示）。
    """
    words = [w for w in query.split() if len(w) >= 3]
    if not words:
        return []
    expr = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
    try:
        rows = conn.execute(
            """SELECT m.id, m.note_id, m.kind, m.url, m.text
               FROM materials_fts f JOIN note_materials m ON m.id = f.rowid
               WHERE materials_fts MATCH ? ORDER BY rank LIMIT ?""",
            (expr, top_k),
        ).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("材料 FTS 检索查询失败（%s）", e)
        return []
    return [dict(r) for r in rows]


def _rrf_fuse(note_ids: list[list[int]], k: int = config.RRF_K) -> list[int]:
    """RRF 融合（§7：score = Σ 1/(k + rank)，k=60）。输入为多路召回 id 列表（各自按序）。"""
    scores: dict[int, float] = {}
    for ranked in note_ids:
        for rank, note_id in enumerate(ranked, start=1):
            scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (k + rank)
    return [nid for nid, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


def _material_hits_to_sources(materials: list[dict]) -> list[dict]:
    """材料层命中转 sources 形态（标注"命中于来源材料"，归属其笔记，带 note_id）。"""
    out = []
    for m in materials:
        out.append({
            "note_id": m["note_id"],
            "material_id": m["id"],
            "kind": m["kind"],
            "url": m["url"],
            "snippet": (m["text"] or "")[:200],
            "from_material": True,
        })
    return out


def _note_hits_to_sources(conn: sqlite3.Connection, note_ids: list[int]) -> list[dict]:
    out = []
    for nid in note_ids:
        note = db.fetch_note(conn, nid)
        if not note:
            continue
        out.append({
            "id": nid,
            "title": note.get("title") or note.get("raw", "")[:40],
            "snippet": (note.get("summary") or note.get("content") or note.get("raw") or "")[:200],
            "url": note.get("source_url"),
            "from_material": False,
        })
    return out


def retrieve(conn: sqlite3.Connection, query: str) -> dict:
    """检索主流程（§7）：返回 {"notes", "materials", "vector_ok", "weak_recall"}。

    - vector_ok=False 表示向量路不可用（Ollama 挂/库内无向量），界面提示"语义检索暂不可用"（§14 第 8 条）；
    - notes 为 RRF 融合后的 Top-N（不含材料层命中）；
    - materials 为材料层兜底命中（可能为空），由调用方附在答案末尾；
    - weak_recall=True 表示召回不足（Top-1 相似度 < 阈值或两路均无命中），prompt 需明示（§7 兜底）。
    """
    query = (query or "").strip()
    if not query:
        return {"notes": [], "materials": [], "vector_ok": True, "weak_recall": True}

    # 1. 向量召回（Ollama 不可用整路跳过，§7 / §14 第 8 条）
    vector_ok = False
    vector_hits: list[int] = []
    top1_sim = 0.0
    try:
        qvec = np.asarray(embedding.embed_texts([query])[0], dtype="<f4")
        vectors = embedding.load_all_embeddings(conn)
        if vectors:
            vector_ok = True
            scored = embedding.cosine_top_k(qvec, vectors, config.VECTOR_TOP_K)
            vector_hits = [nid for nid, _ in scored]
            top1_sim = scored[0][1] if scored else 0.0
    except embedding.EmbeddingError as e:
        logger.info("向量检索不可用（%s），降级 FTS-only", e)

    # 2. FTS 关键词召回
    fts_hits = fts_search(conn, query, config.FTS_TOP_K)

    # 3. RRF 融合取 Top-N
    fused = _rrf_fuse([vector_hits, fts_hits], k=config.RRF_K)[: config.ASK_TOP_N]

    # 4. 材料层兜底（§7 触发条件：Top-1 相似度 < 0.4 或 FTS 无命中）
    materials: list[dict] = []
    need_fallback = (vector_ok and top1_sim < config.VECTOR_MIN_SIM) or not fts_hits
    if need_fallback:
        materials = materials_fts_search(conn, query, config.MATERIALS_TOP_K)

    weak_recall = (vector_ok and top1_sim < config.VECTOR_MIN_SIM) or not fused
    return {
        "notes": _note_hits_to_sources(conn, fused),
        "materials": _material_hits_to_sources(materials),
        "vector_ok": vector_ok,
        "weak_recall": weak_recall,
    }
