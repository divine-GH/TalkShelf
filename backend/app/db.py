"""SQLite 数据层：连接管理 + 建表 + FTS 同步函数。

设计文档 §4 数据模型。要点：
- 连接级 PRAGMA 必开：foreign_keys=ON（外键级联默认关闭）、journal_mode=WAL（读写并发更稳）。
- 建表脚本幂等（CREATE TABLE IF NOT EXISTS），单用户手动迁移（开工准备清单 §3.5 拍板：不用 Alembic）。
- FTS5 trigram 虚拟表独立于业务表（自带文本副本），rowid 与主表 id 一一对应；
  同步统一走应用层函数 fts_sync / fts_delete / material_fts_sync，调用方保证与业务写在同一事务内。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config

# 设计文档 §4 完整 schema（M1 全量建表；embeddings 表 M2 才写数据，先建好）
SCHEMA_SQL = """
-- 笔记主表
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    raw         TEXT NOT NULL,
    title       TEXT,
    category    TEXT,
    summary     TEXT,
    content     TEXT,
    importance  INTEGER DEFAULT 2,
    kind        TEXT NOT NULL DEFAULT 'note',
    source_url  TEXT,
    done_at     TEXT,
    status      TEXT DEFAULT 'pending',
    merged_into INTEGER,
    duplicate_of INTEGER,           -- 查重判定结果：疑似重复的目标笔记 id（M3 起落库，§6.2/§24）
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    status      TEXT NOT NULL DEFAULT 'draft',
    note_id     INTEGER REFERENCES notes(id) ON DELETE CASCADE,
    context_note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    kind            TEXT DEFAULT 'text',
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);

CREATE TABLE IF NOT EXISTS tags (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tags_note_id ON tags(note_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag     ON tags(tag);

CREATE TABLE IF NOT EXISTS entities (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    type    TEXT NOT NULL,
    name    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities(type, name);

CREATE TABLE IF NOT EXISTS embeddings (
    note_id INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    vector  BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS note_materials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id     INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    url         TEXT,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_materials_note ON note_materials(note_id);

-- FTS：trigram 分词（需 SQLite >= 3.34）；rowid 与 notes.id / note_materials.id 一一对应
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    raw, title, summary, content, category, tags,
    tokenize='trigram'
);

CREATE VIRTUAL TABLE IF NOT EXISTS materials_fts USING fts5(
    text,
    tokenize='trigram'
);

-- 登录会话（设计文档 §9：session 存 SQLite 表——可服务端注销、重启不掉线；不采用签名 cookie）
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    csrf_token  TEXT NOT NULL,          -- CSRF Token（§9：session 关联，页面注入 + 请求头校验）
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    expires_at  TEXT NOT NULL
);

-- 登录失败记录（§9：限速锁定落 SQLite——配合锁定生效、重启不失效）
CREATE TABLE IF NOT EXISTS login_failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """新建一个连接并执行连接级 PRAGMA（每次调用都执行，勿只依赖建库时的一次）。"""
    path = Path(db_path) if db_path else config.DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # 删除笔记时 tags/entities/embeddings/对话 级联清理
    conn.execute("PRAGMA journal_mode = WAL")  # 读写并发更稳
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """建表（幂等）+ 旧库增量迁移。测试/启动时调用。"""
    own = conn is None
    conn = conn or connect()
    try:
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)
        conn.commit()
    finally:
        if own:
            conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库增量迁移（CREATE TABLE IF NOT EXISTS 不会给已有表加列）。

    M3（§24）：notes 加 duplicate_of 列（查重判定目标落库，供详情页「疑似重复于 #id」与合并）。
    新增列必须走这里，否则老库（如 data/note-brain.db）不会带出新列。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
    if "duplicate_of" not in cols:
        conn.execute("ALTER TABLE notes ADD COLUMN duplicate_of INTEGER")


# ---------------------------------------------------------------------------
# FTS 同步函数（设计文档 §4：统一走应用层函数，调用方保证同一事务内调用）
# ---------------------------------------------------------------------------


def fts_sync(conn: sqlite3.Connection, note_id: int) -> None:
    """把笔记（含标签聚合）写入 notes_fts；rowid 与 notes.id 一一对应。"""
    conn.execute(
        """
        INSERT OR REPLACE INTO notes_fts(rowid, raw, title, summary, content, category, tags)
        VALUES (:id, :raw, :title, :summary, :content, :category,
                (SELECT group_concat(tag, ' ') FROM tags WHERE note_id = :id))
        """,
        {
            "id": note_id,
            "raw": _scalar(conn, "SELECT raw FROM notes WHERE id = ?", (note_id,)) or "",
            "title": _scalar(conn, "SELECT title FROM notes WHERE id = ?", (note_id,)) or "",
            "summary": _scalar(conn, "SELECT summary FROM notes WHERE id = ?", (note_id,)) or "",
            "content": _scalar(conn, "SELECT content FROM notes WHERE id = ?", (note_id,)) or "",
            "category": _scalar(conn, "SELECT category FROM notes WHERE id = ?", (note_id,)) or "",
        },
    )


def fts_delete(conn: sqlite3.Connection, note_id: int) -> None:
    """从 notes_fts 删除某笔记的行（物理删除 / 置 merged 出索引时用）。"""
    conn.execute("DELETE FROM notes_fts WHERE rowid = ?", (note_id,))


def material_fts_sync(conn: sqlite3.Connection, material_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO materials_fts(rowid, text) VALUES (?, ?)",
        (
            material_id,
            _scalar(conn, "SELECT text FROM note_materials WHERE id = ?", (material_id,)) or "",
        ),
    )


def material_fts_delete(conn: sqlite3.Connection, material_id: int) -> None:
    conn.execute("DELETE FROM materials_fts WHERE rowid = ?", (material_id,))


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> object | None:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# 查询辅助：notes 详情序列化（列表/详情共用）
# ---------------------------------------------------------------------------


def note_to_dict(row: sqlite3.Row, tags: list[str]) -> dict:
    d = dict(row)
    d["tags"] = tags
    return d


def fetch_note(conn: sqlite3.Connection, note_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if not row:
        return None
    tags = [
        r["tag"]
        for r in conn.execute("SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (note_id,))
    ]
    return note_to_dict(row, tags)
