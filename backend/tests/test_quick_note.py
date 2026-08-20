"""快速记录测试（设计文档 §32）：原文立即落库，LLM 后台判断兴趣/收藏并整理。

- POST /api/quick-notes：202 立即返回；笔记 pending + quick=1 + kind 占位 'note'；
- 处理中（pending）：列表/详情 title 为空（浏览以 raw 占位显示）、quick 标记供「判断中…」徽标；
- 队列补整理：LLM 整理的 kind 覆盖占位（收藏 note / 兴趣 interest 两态）；
- 含链接：同步抓取正文注入归档对话，并复制进 note_materials（Tier 2 材料检索）；
- 抓取失败降级不阻塞；空消息 422。
"""

import time

from app import db, fetch, llm
from conftest import note_status, wait_for


def quick_note(client, message: str) -> int:
    resp = client.post("/api/quick-notes", json={"message": message})
    assert resp.status_code == 202, resp.text
    return resp.json()["note_id"]


def test_quick_note_placeholder_while_pending(client, llm_ok, monkeypatch, db_path):
    """处理中占位：pending + quick=1 + kind 占位 note + title 为空（浏览以 raw 占位）。"""

    def slow_chat_json(messages, validate=None, **kwargs):
        time.sleep(0.3)  # 模拟 LLM 处理中，保证能观察到 pending 占位态
        return dict(llm_ok)

    monkeypatch.setattr(llm, "chat_json", slow_chat_json)
    raw = "看到个新工具，有空去试试"
    note_id = quick_note(client, raw)
    assert note_status(db_path, note_id)[0] == "pending"

    detail = client.get(f"/api/notes/{note_id}").json()["note"]
    assert detail["quick"] == 1
    assert detail["status"] == "pending"
    assert detail["kind"] == "note"  # LLM 判断前占位
    assert detail["title"] is None  # 列表/详情回退显示 raw（用户原本输入占位）

    item = client.get("/api/notes").json()["items"][0]
    assert item["id"] == note_id and item["quick"] == 1

    # pending 即可检索（raw 进索引，§14 第 5 条）
    rows = client.get("/api/notes", params={"q": "新工具"}).json()["items"]
    assert any(n["id"] == note_id for n in rows)

    wait_for(lambda: note_status(db_path, note_id)[0] == "processed", desc="快速记录整理完成")


def test_quick_note_llm_decides_interest(client, llm_ok, monkeypatch, db_path):
    """LLM 判断为兴趣：处理完成后 kind 变为 interest，进入回顾清单。"""
    import json

    organized = dict(llm_ok)
    organized["kind"] = "interest"
    organized["title"] = "试试那个新工具"
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: dict(organized))  # force_json 整理路径
    monkeypatch.setattr(
        llm, "_call_chat", lambda *a, **k: json.dumps(organized, ensure_ascii=False)
    )

    note_id = quick_note(client, "看到个新工具，有空去试试")
    wait_for(lambda: note_status(db_path, note_id)[0] == "processed", desc="整理完成")
    detail = client.get(f"/api/notes/{note_id}").json()["note"]
    assert detail["kind"] == "interest"
    assert detail["title"] == "试试那个新工具"
    # 进入回顾页（兴趣清单）
    review = client.get("/api/review").json()
    assert any(n["id"] == note_id for n in review["pending"])


def test_quick_note_llm_decides_note(client, llm_ok, monkeypatch, db_path):
    """LLM 判断为收藏：kind 保持 note，不进兴趣清单。"""
    organized = dict(llm_ok)
    organized["kind"] = "note"
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: dict(organized))
    note_id = quick_note(client, "这个链接值得长期收藏 https://example.com/docs")
    wait_for(lambda: note_status(db_path, note_id)[0] == "processed", desc="整理完成")
    assert client.get(f"/api/notes/{note_id}").json()["note"]["kind"] == "note"
    review = client.get("/api/review").json()
    assert not any(n["id"] == note_id for n in review["pending"] + review["in_progress"])


def test_quick_note_fetches_url_and_material(client, llm_ok, monkeypatch, db_path):
    """含链接：同步抓正文注入归档对话 + 复制进 note_materials（Tier 2）。"""

    def fake_fetch(url):
        return fetch.FetchResult(
            url=url,
            status=200,
            title="测试页",
            markdown="快速记录抓取的正文内容，含 BGP 细节",
            truncated=False,
        )

    monkeypatch.setattr(fetch, "fetch_page", fake_fetch)
    note_id = quick_note(client, "看这个链接 https://example.com/bgp 记一下")
    wait_for(lambda: note_status(db_path, note_id)[0] == "processed", desc="整理完成")

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT kind, url, text FROM note_materials WHERE note_id = ?", (note_id,)
        ).fetchall()
        conv = conn.execute(
            "SELECT id FROM conversations WHERE note_id = ? AND status='archived'", (note_id,)
        ).fetchone()
        assert conv is not None, "快速记录归档对话存在（追溯）"
        kinds = [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM messages WHERE conversation_id = ?", (conv["id"],)
            ).fetchall()
        ]
    finally:
        conn.close()
    assert len(rows) == 1 and rows[0][0] == "fetched_page"
    assert rows[0][1] == "https://example.com/bgp"
    assert "BGP" in rows[0][2]
    assert "fetched_page" in kinds, "抓取材料注入归档对话（LLM 整理时可见）"


def test_quick_note_fetch_failure_degraded(client, llm_ok, monkeypatch, db_path):
    """抓取失败降级不阻塞：笔记照常落库处理。"""
    monkeypatch.setattr(
        fetch, "fetch_page", lambda url: (_ for _ in ()).throw(fetch.FetchError("mock 抓取失败"))
    )
    note_id = quick_note(client, "https://example.com/ 这个页面打不开也没关系")
    wait_for(lambda: note_status(db_path, note_id)[0] == "processed", desc="整理完成")
    assert client.get(f"/api/notes/{note_id}").json()["note"]["status"] == "processed"


def test_quick_note_empty_message_422(client):
    assert client.post("/api/quick-notes", json={"message": "   "}).status_code == 422


def test_quick_note_badge_on_pages(client, llm_ok, monkeypatch, db_path):
    """处理中：首页最近笔记/列表页/详情页显示「判断中…」；整理完成徽标消失。"""

    def slow_chat_json(messages, validate=None, **kwargs):
        time.sleep(0.4)  # 保持 pending，让三个页面都能观察到徽标
        return dict(llm_ok)

    monkeypatch.setattr(llm, "chat_json", slow_chat_json)
    note_id = quick_note(client, "徽标测试内容")
    assert "判断中" in client.get("/").text
    assert "判断中" in client.get("/notes").text
    assert "判断中" in client.get(f"/notes/{note_id}").text
    wait_for(lambda: note_status(db_path, note_id)[0] == "processed", desc="整理完成")
    assert "判断中" not in client.get("/notes").text  # 整理完成徽标消失
    assert "判断中" not in client.get(f"/notes/{note_id}").text
