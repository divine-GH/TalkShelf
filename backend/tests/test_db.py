"""数据层测试：建表、FTS 同步、trigram 检索（设计文档 §4）。"""

import pytest
from app import db


def test_schema_creates_all_tables(conn):
    db.init_db(conn)
    tables = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','virtual table')"
        )
    }
    assert {
        "notes",
        "conversations",
        "messages",
        "tags",
        "entities",
        "embeddings",
        "note_materials",
        "notes_fts",
        "materials_fts",
    } <= tables


def test_foreign_keys_cascade(conn):
    db.init_db(conn)
    cur = conn.execute("INSERT INTO notes(raw, kind, status) VALUES ('x', 'note', 'processed')")
    nid = cur.lastrowid
    conn.execute("INSERT INTO tags(note_id, tag) VALUES (?, 't1')", (nid,))
    conn.execute("INSERT INTO conversations(status, note_id) VALUES ('archived', ?)", (nid,))
    conn.commit()
    conn.execute("DELETE FROM notes WHERE id = ?", (nid,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM tags WHERE note_id = ?", (nid,)).fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM conversations WHERE note_id = ?", (nid,)).fetchone()[0]
        == 0
    )


def test_trigram_fts_sync_and_search(conn):
    db.init_db(conn)
    cur = conn.execute(
        "INSERT INTO notes(raw, title, category, status) VALUES ('nginx 上传大文件被拒', '测试标题', '技术', 'processed')"
    )
    nid = cur.lastrowid
    conn.execute("INSERT INTO tags(note_id, tag) VALUES (?, '部署')", (nid,))
    db.fts_sync(conn, nid)
    conn.commit()

    # 中文 3 字以上词 trigram 命中
    rows = conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?", ('"上传大文件"',)
    ).fetchall()
    assert [r["rowid"] for r in rows] == [nid]

    # tags 聚合列可检索（3+ 字词；2 字词 trigram 不命中，见下方断言）
    conn.execute("INSERT INTO tags(note_id, tag) VALUES (?, '部署配置')", (nid,))
    db.fts_sync(conn, nid)
    conn.commit()
    rows = conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?", ('"部署配置"',)
    ).fetchall()
    assert [r["rowid"] for r in rows] == [nid]

    # 2 字词 trigram 不命中（设计文档 §4 关键点 3：靠 LIKE 兜底，检索层测试覆盖）
    rows = conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?", ('"上传"',)
    ).fetchall()
    assert rows == []

    # 删除路径
    db.fts_delete(conn, nid)
    conn.commit()
    rows = conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?", ('"上传大文件"',)
    ).fetchall()
    assert rows == []


def test_materials_fts(conn):
    db.init_db(conn)
    cur = conn.execute("INSERT INTO notes(raw, kind, status) VALUES ('x', 'note', 'processed')")
    nid = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO note_materials(note_id, kind, url, text) VALUES (?, 'fetched_page', 'https://a.b', '抓取的网页正文内容很长')",
        (nid,),
    )
    db.material_fts_sync(conn, cur.lastrowid)
    conn.commit()
    rows = conn.execute(
        "SELECT rowid FROM materials_fts WHERE materials_fts MATCH ?", ('"网页正文"',)
    ).fetchall()
    assert rows, "materials_fts 应命中"


def test_journal_mode_wal(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_ensure_sqlite_version_ok(monkeypatch):
    # 版本满足（>= 3.34）时不抛
    monkeypatch.setattr(db, "_sqlite_version_tuple", lambda: (3, 34, 0))
    db.ensure_sqlite_version()


def test_ensure_sqlite_version_raises_on_too_old(monkeypatch):
    # 版本过旧（< 3.34）时抛清晰错误（FTS5 trigram 前提）
    monkeypatch.setattr(db, "_sqlite_version_tuple", lambda: (3, 32, 0))
    with pytest.raises(RuntimeError, match="SQLite >= 3.34"):
        db.ensure_sqlite_version()
