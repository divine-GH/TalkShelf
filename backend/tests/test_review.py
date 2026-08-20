"""回顾页测试（设计文档 §4.2 + M2 决策：未决策/进行中两分区状态机）。

状态机：kind='interest' 且 done_at IS NULL → 未决策；done_at 非空 → 进行中。
动作：去做（置 done_at）/ 留着（无操作）/ 放弃（DELETE）；稍后（清 done_at）/
转收藏（kind→note，done_at 保留）/ 删除（DELETE，含 FTS 清理）。
"""

from conftest import note_status, wait_for


def _mk_interest(client, raw: str) -> int:
    resp = client.post("/api/notes", json={"raw": raw, "kind": "interest"})
    assert resp.status_code == 202
    return resp.json()["note_id"]


def test_review_partitions(client, llm_ok, db_path):
    """GET /api/review：未决策与进行中两分区（done_at 判定）。"""
    a = _mk_interest(client, "想试试手冲咖啡")
    b = _mk_interest(client, "想学游泳")
    wait_for(lambda: note_status(db_path, a)[0] in ("processed", "duplicate"), desc="a 处理完")
    wait_for(lambda: note_status(db_path, b)[0] in ("processed", "duplicate"), desc="b 处理完")

    resp = client.post(f"/api/notes/{a}/done")
    assert resp.status_code == 200
    assert resp.json()["done_at"]

    data = client.get("/api/review").json()
    pending_ids = [n["id"] for n in data["pending"]]
    progress_ids = [n["id"] for n in data["in_progress"]]
    assert b in pending_ids and a not in pending_ids
    assert a in progress_ids and b not in progress_ids


def test_done_snooze_roundtrip(client, llm_ok, db_path):
    """去做 → 进行中；稍后 → 回未决策（清 done_at）。"""
    nid = _mk_interest(client, "想试试手冲咖啡")
    wait_for(lambda: note_status(db_path, nid)[0] in ("processed", "duplicate"), desc="处理完")

    client.post(f"/api/notes/{nid}/done")
    assert client.get("/api/review").json()["in_progress"]

    resp = client.post(f"/api/notes/{nid}/snooze")
    assert resp.status_code == 200
    assert resp.json()["done_at"] is None
    data = client.get("/api/review").json()
    assert any(n["id"] == nid for n in data["pending"])
    assert not any(n["id"] == nid for n in data["in_progress"])


def test_convert_to_note_keeps_done_at(client, llm_ok, db_path):
    """转收藏：kind→note（退出回顾清单），done_at 保留作历史。"""
    nid = _mk_interest(client, "想试试手冲咖啡")
    wait_for(lambda: note_status(db_path, nid)[0] in ("processed", "duplicate"), desc="处理完")
    client.post(f"/api/notes/{nid}/done")

    resp = client.post(f"/api/notes/{nid}/convert")
    assert resp.status_code == 200
    assert resp.json()["kind"] == "note"
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT kind, done_at FROM notes WHERE id = ?", (nid,)).fetchone()
    finally:
        conn.close()
    assert row[0] == "note"
    assert row[1] is not None, "done_at 保留作历史（做过的时间）"
    assert not any(n["id"] == nid for n in client.get("/api/review").json()["pending"])


def test_delete_note_and_fts_cleanup(client, llm_ok, db_path, conn):
    """放弃/删除：物理删除 + notes_fts / materials_fts 行清理（虚拟表无外键，§4 同步约定）。"""
    from app import db as db_mod

    nid = _mk_interest(client, "不想留着的兴趣")
    wait_for(lambda: note_status(db_path, nid)[0] in ("processed", "duplicate"), desc="处理完")
    cur = conn.execute(
        "INSERT INTO note_materials(note_id, kind, url, text) VALUES (?, 'fetched_page', ?, ?)",
        (nid, "https://example.com/x", "测试材料正文内容"),
    )
    db_mod.material_fts_sync(conn, cur.lastrowid)
    conn.commit()

    resp = client.delete(f"/api/notes/{nid}")
    assert resp.status_code == 204

    conn2 = __import__("sqlite3").connect(db_path)
    try:
        assert conn2.execute("SELECT COUNT(*) FROM notes WHERE id = ?", (nid,)).fetchone()[0] == 0
        assert (
            conn2.execute(
                "SELECT COUNT(*) FROM note_materials WHERE note_id = ?", (nid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn2.execute(
                "SELECT COUNT(*) FROM materials_fts WHERE rowid = ?", (cur.lastrowid,)
            ).fetchone()[0]
            == 0
        ), "materials_fts 孤儿行必须清理"
        assert (
            conn2.execute("SELECT COUNT(*) FROM notes_fts WHERE rowid = ?", (nid,)).fetchone()[0]
            == 0
        )
    finally:
        conn2.close()


def test_review_guards(client, llm_ok, db_path):
    """守卫：done/snooze/convert 仅限 interest；删除不存在 → 404。"""
    nid = _mk_interest(client, "普通兴趣")
    wait_for(lambda: note_status(db_path, nid)[0] in ("processed", "duplicate"), desc="处理完")
    # 转收藏后就不再是 interest
    client.post(f"/api/notes/{nid}/convert")
    assert client.post(f"/api/notes/{nid}/done").status_code == 409
    assert client.post(f"/api/notes/{nid}/snooze").status_code == 409
    assert client.post(f"/api/notes/{nid}/convert").status_code == 409
    assert client.delete("/api/notes/999999").status_code == 404


def test_review_page_html(client, llm_ok):
    resp = client.get("/review")
    assert resp.status_code == 200
    assert "未决策" in resp.text and "进行中" in resp.text
