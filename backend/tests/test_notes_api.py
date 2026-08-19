"""笔记列表检索测试（设计文档 §7：FTS trigram + 双字词 LIKE 兜底 + 等值过滤）。"""
import json

from conftest import ORGANIZED, start_conversation


def make_note(client, monkeypatch, raw: str, kind: str = "note",
              category: str | None = None, title: str | None = None):
    """走对话 + 拍板（LLM mock 输出可覆盖的整理 JSON），落一条 processed 笔记。"""
    from app import llm

    data = dict(ORGANIZED)
    if category:
        data["category"] = category
    if title:
        data["title"] = title
        data["summary"] = f"关于「{title}」的摘要"  # 避免共享 ORGANIZED summary 造成检索串扰
    monkeypatch.setattr(llm, "_call_chat", lambda *a, **k: json.dumps(data, ensure_ascii=False))
    conv_id = start_conversation(client, raw)
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": kind})
    assert resp.status_code == 200, resp.text
    return resp.json()["note"]["id"]


def test_list_filters_category_kind(client, llm_ok, monkeypatch):
    make_note(client, monkeypatch, "技术类笔记：nginx 配置", category="技术", title="nginx 配置技巧")
    make_note(client, monkeypatch, "生活类笔记：周末买菜清单", category="生活", title="周末买菜清单",
              kind="interest")

    tech = client.get("/api/notes", params={"category": "技术"}).json()
    assert tech["total"] == 1 and tech["items"][0]["category"] == "技术"
    interest = client.get("/api/notes", params={"kind": "interest"}).json()
    assert interest["total"] == 1 and interest["items"][0]["kind"] == "interest"
    both = client.get("/api/notes", params={"category": "生活", "kind": "interest"}).json()
    assert both["total"] == 1


def test_list_q_trigram_and_two_char_fallback(client, llm_ok, monkeypatch):
    make_note(client, monkeypatch, "今天发现 nginx client_max_body_size 默认 1M 的坑",
              title="nginx 上传限制排查")
    make_note(client, monkeypatch, "完全无关的买菜清单", title="周末买菜")

    # 3+ 字词：trigram 命中（title 含连续串「上传限制」）
    items = client.get("/api/notes", params={"q": "上传限制"}).json()["items"]
    assert len(items) == 1 and items[0]["title"] == "nginx 上传限制排查"
    # 2 字词：trigram 无法命中，LIKE 兜底（§7 双字词兜底）
    items = client.get("/api/notes", params={"q": "买菜"}).json()["items"]
    assert len(items) == 1 and items[0]["title"] == "周末买菜"


def test_list_pagination(client, llm_ok, monkeypatch):
    for i in range(5):
        make_note(client, monkeypatch, f"分页测试笔记第 {i} 条内容", title=f"分页笔记 {i}")
    data = client.get("/api/notes", params={"page": 1}).json()
    assert data["total"] == 5
    assert data["page_size"] == 20
    assert data["pages"] == 1


def test_list_q_no_result(client, llm_ok, monkeypatch):
    make_note(client, monkeypatch, "某条笔记", title="某条笔记")
    data = client.get("/api/notes", params={"q": "绝对不存在的词xyz"}).json()
    assert data["total"] == 0


def test_notes_page_html(client, llm_ok, monkeypatch):
    make_note(client, monkeypatch, "页面渲染测试笔记", title="页面渲染测试标题")
    resp = client.get("/notes")
    assert resp.status_code == 200
    assert "页面渲染测试标题" in resp.text
    assert "记录" in resp.text


def test_index_page_html(client, llm_ok):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "记点什么" in resp.text
