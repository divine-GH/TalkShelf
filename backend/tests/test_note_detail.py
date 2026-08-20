"""笔记详情页测试（M3，设计文档 §8 / §6.2 / §5）。

覆盖：详情数据（来源对话 + 查重目标）、PUT 完整编辑（触发重整理：删向量重算 embedding）、
PUT 校验（未知字段/category 非法/merged 拒绝）、合并（raw 并入目标 + 软删除 merged +
同事务出索引 + 目标重整理）、忽略（duplicate → processed）、重新整理（清元数据重跑管线）、
详情页 HTML、修正对话 confirm 后目标向量重算。
"""

import sqlite3

from app import llm
from conftest import note_status, wait_for


def _mk_note(client, raw: str) -> int:
    resp = client.post("/api/notes", json={"raw": raw, "kind": "note"})
    assert resp.status_code == 202
    return resp.json()["note_id"]


def _embedding_bytes(db_path, note_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT vector FROM embeddings WHERE note_id = ?", (note_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _status_and_dup(db_path, note_id):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status, duplicate_of FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        return (row[0], row[1]) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 详情数据
# ---------------------------------------------------------------------------


def test_get_detail_with_conversations_and_duplicate_target(client, llm_ok, db_path):
    """详情：来源对话（对话式记录归档）+ duplicate_target（手工标 duplicate）。"""
    conv_id = None
    resp = client.post("/api/conversations", json={"message": "nginx 上传大文件被 413 拒绝"})
    conv_id = resp.json()["conversation_id"]
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    note_id = resp.json()["note"]["id"]
    wait_for(lambda: _embedding_bytes(db_path, note_id) is not None, desc="补做完成")

    data = client.get(f"/api/notes/{note_id}").json()
    assert data["note"]["id"] == note_id
    assert data["note"]["entities"] is not None
    assert any(c["id"] == conv_id for c in data["conversations"]), "来源对话应含归档的对话"
    msgs = data["conversations"][0]["messages"]
    assert any(m["role"] == "user" for m in msgs)

    # 手工标 duplicate（模拟队列查重命中）→ duplicate_target 出现
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE notes SET status='duplicate', duplicate_of=? WHERE id=?", (note_id, note_id)
        )
        conn.commit()
    finally:
        conn.close()
    data = client.get(f"/api/notes/{note_id}").json()
    assert data["duplicate_target"]["id"] == note_id


def test_get_detail_404(client):
    assert client.get("/api/notes/999999").status_code == 404


# ---------------------------------------------------------------------------
# PUT 完整编辑（§5：触发重整理）
# ---------------------------------------------------------------------------


def test_put_updates_fields_and_rebuilds_embedding(client, llm_ok, db_path):
    nid = _mk_note(client, "原始内容：nginx 上传限制")
    wait_for(lambda: _embedding_bytes(db_path, nid) is not None, desc="首次向量")
    old_vec = _embedding_bytes(db_path, nid)
    assert old_vec is not None

    resp = client.put(
        f"/api/notes/{nid}",
        json={
            "raw": "改过的原文：python 协程 asyncio 用法",
            "title": "新标题",
            "category": "技术",
            "tags": ["python", "asyncio"],
            "summary": "新摘要",
            "content": "新正文内容",
            "importance": 3,
            "kind": "note",
            "source_url": "https://example.com/asyncio",
        },
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["title"] == "新标题"
    assert set(updated["tags"]) == {"python", "asyncio"}  # 读取按字母序，比较集合
    assert updated["raw"] == "改过的原文：python 协程 asyncio 用法"

    # FTS 同步：新原文可检索
    assert client.get("/api/notes", params={"q": "asyncio"}).json()["total"] >= 1

    # 重整理管线：向量已删并重算（内容变了 → 向量不同）
    wait_for(
        lambda: (
            _embedding_bytes(db_path, nid) is not None and _embedding_bytes(db_path, nid) != old_vec
        ),
        desc="向量重算",
    )
    assert _embedding_bytes(db_path, nid) != old_vec


def test_put_validation_errors(client, llm_ok, db_path):
    nid = _mk_note(client, "校验测试")
    wait_for(lambda: note_status(db_path, nid)[0] in ("processed", "duplicate"), desc="处理完")
    assert client.put(f"/api/notes/{nid}", json={"created_at": "x"}).status_code == 422
    assert client.put(f"/api/notes/{nid}", json={"category": "不存在的分类"}).status_code == 422
    assert client.put(f"/api/notes/{nid}", json={"kind": "todo"}).status_code == 422
    assert client.put(f"/api/notes/{nid}", json={"importance": 9}).status_code == 422
    assert client.put(f"/api/notes/{nid}", json={"raw": ""}).status_code == 422
    assert client.put(f"/api/notes/{nid}", json={"source_url": "ftp://x"}).status_code == 422


def test_put_merged_note_rejected(client, llm_ok, db_path):
    a = _mk_note(client, "目标笔记")
    wait_for(lambda: note_status(db_path, a)[0] in ("processed", "duplicate"), desc="A 处理完")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE notes SET status='merged', merged_into=? WHERE id=?", (a, a))
        conn.commit()
    finally:
        conn.close()
    assert client.put(f"/api/notes/{a}", json={"title": "x"}).status_code == 409


# ---------------------------------------------------------------------------
# 合并 / 忽略（§6.2）
# ---------------------------------------------------------------------------


def test_merge_flow(client, llm_ok, db_path, monkeypatch):
    """合并：raw 并入目标 → 目标重整理；本条 merged + 同事务出索引（notes_fts/embeddings）。"""
    old = _mk_note(client, "旧笔记：nginx 上传大文件限制")
    wait_for(
        lambda: note_status(db_path, old)[0] in ("processed", "duplicate"), desc="旧笔记处理完"
    )
    old_vec = _embedding_bytes(db_path, old)

    new = _mk_note(client, "新笔记：nginx client_max_body_size 调大")
    monkeypatch.setattr(llm, "judge_duplicate", lambda summary, cands: old)  # 查重命中旧笔记
    wait_for(
        lambda: _status_and_dup(db_path, new) == ("duplicate", old), desc="查重命中落 duplicate_of"
    )

    # 详情页有重复目标
    data = client.get(f"/api/notes/{new}").json()
    assert data["duplicate_target"]["id"] == old

    resp = client.post(f"/api/notes/{new}/merge")
    assert resp.status_code == 200
    target = resp.json()
    assert target["id"] == old
    assert "旧笔记" in target["raw"] and "新笔记" in target["raw"], "raw 应追加进目标"

    # 本条软删除 + 出索引（§6.2 检查点：notes_fts / embeddings 同事务清理）
    assert _status_and_dup(db_path, new) == ("merged", old)
    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM notes_fts WHERE rowid=?", (new,)).fetchone()[0] == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM embeddings WHERE note_id=?", (new,)).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT merged_into FROM notes WHERE id=?", (new,)).fetchone()[0] == old
    finally:
        conn.close()

    # 目标重整理：向量重算（raw 变了 → 向量不同）
    wait_for(
        lambda: (
            _embedding_bytes(db_path, old) is not None and _embedding_bytes(db_path, old) != old_vec
        ),
        desc="目标向量重算",
    )
    assert _embedding_bytes(db_path, old) != old_vec
    # merged 笔记不再出现在列表
    assert client.get("/api/notes").json()["total"] == 1


def test_merge_without_target_conflict(client, llm_ok, db_path):
    nid = _mk_note(client, "无目标笔记")
    wait_for(lambda: note_status(db_path, nid)[0] in ("processed", "duplicate"), desc="处理完")
    # 手工置 duplicate 但 duplicate_of 为空（老数据场景）
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE notes SET status='duplicate' WHERE id=?", (nid,))
        conn.commit()
    finally:
        conn.close()
    assert client.post(f"/api/notes/{nid}/merge").status_code == 409


def test_ignore_flow(client, llm_ok, db_path, monkeypatch):
    """忽略：duplicate → processed，清 duplicate_of。"""
    old = _mk_note(client, "目标笔记")
    wait_for(lambda: note_status(db_path, old)[0] in ("processed", "duplicate"), desc="处理完")
    new = _mk_note(client, "疑似重复的新笔记")
    monkeypatch.setattr(llm, "judge_duplicate", lambda summary, cands: old)
    wait_for(lambda: _status_and_dup(db_path, new) == ("duplicate", old), desc="查重命中")

    resp = client.post(f"/api/notes/{new}/ignore")
    assert resp.status_code == 200
    assert resp.json()["status"] == "processed"
    assert _status_and_dup(db_path, new) == ("processed", None)
    # 非 duplicate 不可忽略
    assert client.post(f"/api/notes/{old}/ignore").status_code == 409


# ---------------------------------------------------------------------------
# 重新整理（§5 reprocess）
# ---------------------------------------------------------------------------


def test_reprocess_clears_and_reorganizes(client, llm_ok, db_path):
    nid = _mk_note(client, "直存内容等待整理")
    wait_for(lambda: note_status(db_path, nid)[0] in ("processed", "duplicate"), desc="首次整理完")
    assert note_status(db_path, nid)[1]  # 有 title

    resp = client.post(f"/api/notes/{nid}/reprocess")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending" and resp.json()["title"] is None

    # 队列重新整理（LLM mock 固定 JSON）→ processed + 元数据恢复
    wait_for(
        lambda: (
            note_status(db_path, nid)[0] in ("processed", "duplicate")
            and note_status(db_path, nid)[1]
        ),
        desc="重新整理完",
    )
    assert _embedding_bytes(db_path, nid) is not None


# ---------------------------------------------------------------------------
# 修正对话（§4.3：confirm 拍板更新目标 + 重整理）
# ---------------------------------------------------------------------------


def test_correction_confirm_rebuilds_embedding(client, llm_ok, db_path):
    nid = _mk_note(client, "待修正笔记")
    wait_for(lambda: _embedding_bytes(db_path, nid) is not None, desc="首次向量")
    old_vec = _embedding_bytes(db_path, nid)

    resp = client.post(
        "/api/conversations",
        json={
            "message": "补充：把标题改成修正后的标题",
            "context_note_id": nid,
        },
    )
    conv_id = resp.json()["conversation_id"]
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    assert resp.status_code == 200
    assert resp.json()["note"]["id"] == nid, "修正对话拍板应更新原笔记而非新建"

    # 修正后目标向量重算（raw 追加了用户新话 → 向量不同）
    wait_for(
        lambda: (
            _embedding_bytes(db_path, nid) is not None and _embedding_bytes(db_path, nid) != old_vec
        ),
        desc="修正后向量重算",
    )
    assert _embedding_bytes(db_path, nid) != old_vec


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


def test_detail_page_html(client, llm_ok, db_path):
    nid = _mk_note(client, "页面渲染测试")
    wait_for(lambda: note_status(db_path, nid)[0] in ("processed", "duplicate"), desc="处理完")
    resp = client.get(f"/notes/{nid}")
    assert resp.status_code == 200
    for fragment in ("完整编辑", "修正对话", "来源对话", "重新整理", "删除"):
        assert fragment in resp.text, f"详情页缺少: {fragment}"
    assert client.get("/notes/999999").status_code == 404
