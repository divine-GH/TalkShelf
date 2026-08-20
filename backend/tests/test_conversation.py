"""对话式记录全流程测试（设计文档 §4.3 / §6.4 / §12 M1 测试约定）。"""

import json

from conftest import start_conversation


def test_conversation_confirm_flow(client, llm_ok, db_path):
    """发起 → LLM 输出整理 JSON → 拍板落库 processed，元数据/材料/归档齐全。"""
    conv_id = start_conversation(
        client, "今天发现 nginx client_max_body_size 默认 1M，上传大文件被拒，帮我记下"
    )

    # LLM 回复（mock 固定整理 JSON，围栏剥离路径：包一层 ```json 围栏验证剥离）
    resp = client.get(f"/api/conversations/{conv_id}")
    msgs = resp.json()["messages"]
    assert msgs[-1]["role"] == "assistant"
    assert (
        json.loads(msgs[-1]["content"].replace("```json\n", "").replace("```", ""))["title"]
        == "nginx 上传大文件限制"
    )

    # 拍板 → 收藏
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    assert resp.status_code == 200, resp.text
    note = resp.json()["note"]
    assert note["status"] == "processed"
    assert note["category"] == "技术"
    assert note["kind"] == "note"
    assert note["raw"] == "今天发现 nginx client_max_body_size 默认 1M，上传大文件被拒，帮我记下"

    # 对话归档并关联 note_id
    conv = client.get(f"/api/conversations/{conv_id}").json()
    assert conv["status"] == "archived"
    assert conv["context_note_id"] is None

    # tags/entities 落库
    row = client.get("/api/notes").json()["items"][0]
    assert row["tags"] == ["nginx", "部署"]

    # 列表可检索（FTS）
    rows = client.get("/api/notes", params={"q": "上传大文件"}).json()["items"]
    assert any(n["id"] == note["id"] for n in rows)


def test_confirm_interest(client, llm_ok):
    conv_id = start_conversation(client, "看到个有意思的工具，有空去试试")
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "interest"})
    assert resp.json()["note"]["kind"] == "interest"
    # 列表 kind 过滤
    items = client.get("/api/notes", params={"kind": "interest"}).json()["items"]
    assert len(items) == 1


def test_confirm_force_json_when_last_reply_is_question(client, llm_ok, monkeypatch, db_path):
    """最后一条 assistant 消息是追问（非 JSON）→ 拍板时强制整理。"""
    from app import llm

    calls = {"n": 0}

    def fake_chat(messages, **kwargs):
        calls["n"] += 1
        # 第一次（追加消息）：返回追问文本（非 JSON）；之后（拍板 force_json）：返回整理 JSON
        if calls["n"] == 1:
            return "信息不太够，能多说一点吗？比如它是做什么用的？"
        return json.dumps(llm_ok, ensure_ascii=False)

    monkeypatch.setattr(llm, "_call_chat", fake_chat)
    conv_id = start_conversation(client, "帮我记下 nginx 的事")
    assert (
        "能多说一点吗"
        in client.get(f"/api/conversations/{conv_id}").json()["messages"][-1]["content"]
    )
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["note"]["status"] == "processed"
    assert resp.json()["note"]["title"] == "nginx 上传大文件限制"
    assert calls["n"] == 1  # 追问不重试（追问是正常回复，不是校验失败，§6.4）


def test_confirm_with_fetched_material_copied(client, llm_ok, monkeypatch, db_path):
    """对话中有抓取材料（fetched_page）→ 拍板后复制进 note_materials + materials_fts（§18.1 #2）。"""
    from app import fetch

    def fake_fetch(url):
        return fetch.FetchResult(
            url=url,
            status=200,
            title="测试页",
            markdown="这是一段抓取到的网页正文，包含专业词汇 BGP 配置细节",
            truncated=False,
        )

    monkeypatch.setattr(fetch, "fetch_page", fake_fetch)
    conv_id = start_conversation(client, "看这个链接 https://example.com/docs/bgp 挺有用，帮我记下")
    kinds = [m["kind"] for m in client.get(f"/api/conversations/{conv_id}").json()["messages"]]
    assert "fetched_page" in kinds  # 抓取材料注入对话（LLM 回复在其后，故不能断言最后一条）
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    note_id = resp.json()["note"]["id"]

    from app import db

    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM note_materials WHERE note_id = ?", (note_id,)).fetchall()
        assert len(rows) == 1
        assert rows[0]["kind"] == "fetched_page"
        assert rows[0]["url"] == "https://example.com/docs/bgp"
        assert "BGP" in rows[0]["text"]
        # 材料 FTS 可检索
        hit = conn.execute(
            "SELECT rowid FROM materials_fts WHERE materials_fts MATCH ?", ('"专业词汇"',)
        ).fetchall()
        assert hit
    finally:
        conn.close()


def test_discard_draft(client, llm_ok):
    conv_id = start_conversation(client, "这条我会放弃")
    resp = client.delete(f"/api/conversations/{conv_id}")
    assert resp.status_code == 204
    assert client.get(f"/api/conversations/{conv_id}").status_code == 404
    assert client.get("/api/conversations").json()["items"] == []


def test_draft_list_and_continue(client, llm_ok):
    conv_id = start_conversation(client, "第一段草稿")
    # 追加消息
    resp = client.post(f"/api/conversations/{conv_id}/messages", json={"message": "再补充一句"})
    assert resp.status_code == 200
    assert client.get("/api/conversations").json()["items"][0]["id"] == conv_id
    # 已归档对话不可再追加
    client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    resp = client.post(f"/api/conversations/{conv_id}/messages", json={"message": "晚了"})
    assert resp.status_code == 409


def test_confirm_twice_conflict(client, llm_ok):
    conv_id = start_conversation(client, "拍两次")
    client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    assert resp.status_code == 409


def test_correction_conversation_updates_note(client, llm_ok, db_path):
    """修正对话（context_note_id）：拍板更新目标笔记而非新建——raw 追加 + 元数据覆盖 + 归档（§4.3）。"""
    nid = start_conversation(client, "原始内容一句话")
    note_id = client.post(f"/api/conversations/{nid}/confirm", json={"kind": "note"}).json()[
        "note"
    ]["id"]

    resp = client.post(
        "/api/conversations",
        json={
            "message": "其实应该改一下分类",
            "context_note_id": note_id,
        },
    )
    assert resp.status_code == 200, resp.text
    conv2 = resp.json()["conversation_id"]
    resp = client.post(f"/api/conversations/{conv2}/confirm", json={"kind": "note"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["note"]["id"] == note_id  # 更新而非新建

    # raw 追加保留旧原话，元数据被新整理覆盖
    from app import db

    conn = db.connect()
    try:
        note = conn.execute(
            "SELECT raw, title, category FROM notes WHERE id = ?", (note_id,)
        ).fetchone()
        conv2_row = conn.execute(
            "SELECT status, note_id FROM conversations WHERE id = ?", (conv2,)
        ).fetchone()
    finally:
        conn.close()
    assert "原始内容一句话" in note["raw"]
    assert "其实应该改一下分类" in note["raw"]
    assert note["title"] == "nginx 上传大文件限制"
    assert conv2_row["status"] == "archived" and conv2_row["note_id"] == note_id
