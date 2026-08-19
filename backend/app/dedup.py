"""查重（设计文档 §6.2）：M1 为 FTS 近似召回版（§12 / §21.1 #3），M2 接入 Ollama 后升级向量版。

流程：从新笔记提取查询词（英文/数字 token ≥3 字符 + 中文连续串 ≥3 字，trigram 前提）→
notes_fts 召回 Top-K（排除自身与 merged）→ LLM 判断 duplicate_of。
命中时笔记仍入库但标 status='duplicate'（绝不丢输入）；查重失败由调用方只记日志、不反噬（§21.2 #5）。
"""
from __future__ import annotations

import logging
import re
import sqlite3

from . import config, db, llm

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\.]{3,}|[\u4e00-\u9fff]{3,}")


def extract_terms(note: dict) -> list[str]:
    """按 标题 → 摘要 → 原文 → 正文 的顺序提取查询词，去重，取上限。"""
    texts = [
        note.get("title") or "",
        note.get("summary") or "",
        note.get("raw") or "",
        note.get("content") or "",
    ]
    seen: set[str] = set()
    terms: list[str] = []
    for text in texts:
        for tok in _TOKEN_RE.findall(text):
            if tok not in seen:
                seen.add(tok)
                terms.append(tok)
        if len(terms) >= config.DEDUP_QUERY_MAX_TERMS:
            break
    return terms[: config.DEDUP_QUERY_MAX_TERMS]


def fts_candidates(conn: sqlite3.Connection, note: dict) -> list[dict]:
    """FTS 近似召回 Top-K（BM25），排除自身与 merged。无查询词/查询失败返回空列表。"""
    terms = extract_terms(note)
    if not terms:
        return []
    query = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
    try:
        rows = conn.execute(
            "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, config.DEDUP_FTS_TOP_K + 1),
        ).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("FTS 查重查询失败（%s），跳过查重", e)
        return []
    out: list[dict] = []
    for row in rows:
        if row["rowid"] == note["id"]:
            continue
        cand = db.fetch_note(conn, row["rowid"])
        if cand and cand["status"] != "merged":
            out.append(cand)
        if len(out) >= config.DEDUP_FTS_TOP_K:
            break
    return out


def check_duplicate(conn: sqlite3.Connection, note: dict) -> int | None:
    """M1 查重：返回重复的旧笔记 id 或 None。LLM 失败抛 LLMError（调用方记日志不反噬）。"""
    candidates = fts_candidates(conn, note)
    if not candidates:
        return None
    summary = note.get("summary") or note.get("title") or note.get("raw", "")[:200]
    return llm.judge_duplicate(summary, candidates)
