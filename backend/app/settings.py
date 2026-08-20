"""运行时设置（设计文档 §28）：settings 表键值覆盖 config 的 .env 默认值，改完立即生效、重启不丢。

- 键值一律以字符串存储，读取侧按类型转换（get_str / get_bool / get_int / get_float）；
- 未设置的键返回 None / 默认值，调用方回落到 config.py 的 .env 默认值（.env 仍是"出厂默认"）；
- 设置项白名单与取值校验在 api.py（PUT /api/settings），本模块只负责存取与类型转换；
- 无 conn 的模块（llm / embedding / web_search）用 resolve_str 自开短连接读取
  （SQLite 本地单用户，连接开销可忽略；WAL 下并发只读安全）。
"""

from __future__ import annotations

import sqlite3

from . import config, db

# 键名（settings 表的 key；值一律存字符串）。新增设置项：这里加键 + api.py 白名单 + effective()
KEY_WEEKLY_LLM = (
    "weekly_llm"  # "1"/"0"：每周总结是否用 LLM 生成（关闭 = 纯统计，省 token、离线可用）
)
KEY_DEFAULT_CATEGORY = "default_category"  # 直存（待整理）笔记兜底分类；"" = 不启用
KEY_LLM_PROVIDER = (
    "llm_provider"  # 对话/整理模型提供商 id（providers.py 注册表；未改回落 .env LLM_PROVIDER）
)
KEY_LLM_MODEL = "llm_model"  # 对话/整理模型名（随 llm_provider 的提供商 API 调用）
KEY_EMBED_MODEL = "embed_model"  # Ollama embedding 模型名
KEY_EMBEDDING_ENABLED = (
    "embedding_enabled"  # "1"/"0"：本地 embedding 总开关（§35；关 = 向量化/向量检索全跳过）
)
KEY_SEARCH_MODEL = "search_model"  # 联网搜索模型名
KEY_VECTOR_TOP_K = "vector_top_k"  # 向量召回 Top-K
KEY_FTS_TOP_K = "fts_top_k"  # FTS 关键词召回 Top-K
KEY_ASK_TOP_N = "ask_top_n"  # RRF 融合后取 Top-N 进 prompt
KEY_VECTOR_MIN_SIM = "vector_min_sim"  # Top-1 向量相似度阈值（弱召回判定）
KEY_MATERIALS_TOP_K = "materials_top_k"  # 材料层兜底召回条数
KEY_AUTH_PASSWORD_HASH = (
    "auth_password_hash"  # 设置页改密码后的 argon2 哈希（优先于 .env AUTH_PASSWORD）
)


def get(conn: sqlite3.Connection, key: str) -> str | None:
    """读原始字符串值；未设置返回 None（调用方回落默认）。"""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_value(conn: sqlite3.Connection, key: str, value: str) -> None:
    """写入/覆盖（upsert）。不自行 commit——调用方开事务，与业务写保持原子。"""
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, datetime('now','localtime')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )


def delete(conn: sqlite3.Connection, key: str) -> None:
    """删除（恢复默认）。不自行 commit。"""
    conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def get_str(conn: sqlite3.Connection, key: str, default: str) -> str:
    v = get(conn, key)
    return v if v is not None else default


def get_bool(conn: sqlite3.Connection, key: str, default: bool) -> bool:
    v = get(conn, key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def get_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    v = get(conn, key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def get_float(conn: sqlite3.Connection, key: str, default: float) -> float:
    v = get(conn, key)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def resolve_str(key: str, default: str) -> str:
    """无 conn 上下文（llm/embedding/web_search 等模块）读取字符串设置：自开短连接。"""
    conn = db.connect()
    try:
        return get_str(conn, key, default)
    finally:
        conn.close()


def effective(conn: sqlite3.Connection) -> dict:
    """当前生效设置全集（DB 覆盖 + config 默认），供 GET /api/settings 与设置页渲染。"""
    return {
        "weekly_llm": get_bool(conn, KEY_WEEKLY_LLM, True),
        "default_category": get_str(conn, KEY_DEFAULT_CATEGORY, ""),
        "llm_provider": get_str(conn, KEY_LLM_PROVIDER, config.LLM_PROVIDER),
        "llm_model": get_str(conn, KEY_LLM_MODEL, config.LLM_MODEL),
        "embed_model": get_str(conn, KEY_EMBED_MODEL, config.EMBED_MODEL),
        "embedding_enabled": get_bool(conn, KEY_EMBEDDING_ENABLED, config.EMBEDDING_ENABLED),
        "search_model": get_str(conn, KEY_SEARCH_MODEL, config.SEARCH_MODEL),
        "vector_top_k": get_int(conn, KEY_VECTOR_TOP_K, config.VECTOR_TOP_K),
        "fts_top_k": get_int(conn, KEY_FTS_TOP_K, config.FTS_TOP_K),
        "ask_top_n": get_int(conn, KEY_ASK_TOP_N, config.ASK_TOP_N),
        "vector_min_sim": get_float(conn, KEY_VECTOR_MIN_SIM, config.VECTOR_MIN_SIM),
        "materials_top_k": get_int(conn, KEY_MATERIALS_TOP_K, config.MATERIALS_TOP_K),
        "password_set": get(conn, KEY_AUTH_PASSWORD_HASH) is not None,
    }
