"""检索记录（检索页历史，§27）：/api/ask 成功自动落记录 + 上限裁剪 + 列表 + 单条删除。

LLM 与 embedding 全 mock（conftest），不触网。
"""

from app import config


def _ask(client, monkeypatch, question: str, answer: str = "测试答案"):
    from app import llm

    monkeypatch.setattr(llm, "_call_chat", lambda *a, **k: answer)
    resp = client.post("/api/ask", json={"question": question})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _history(client) -> list[dict]:
    resp = client.get("/api/search-history")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["limit"] == config.SEARCH_HISTORY_LIMIT
    return data["items"]


def test_ask_saves_search_history(client, llm_ok, monkeypatch):
    """提问成功 → 自动落一条检索记录（问题 + 答案 + 时间）。"""
    _ask(client, monkeypatch, "nginx 上传限制", "答案：默认 1M")
    items = _history(client)
    assert len(items) == 1
    assert items[0]["question"] == "nginx 上传限制"
    assert items[0]["answer"] == "答案：默认 1M"
    assert items[0]["created_at"]


def test_search_history_newest_first(client, llm_ok, monkeypatch):
    """列表按新→旧排序。"""
    _ask(client, monkeypatch, "问题一")
    _ask(client, monkeypatch, "问题二")
    assert [i["question"] for i in _history(client)] == ["问题二", "问题一"]


def test_search_history_limit_trims_oldest(client, llm_ok, monkeypatch):
    """超出 SEARCH_HISTORY_LIMIT → 自动删除最早记录，只保留最新 N 条。"""
    monkeypatch.setattr(config, "SEARCH_HISTORY_LIMIT", 3)
    for i in range(5):
        _ask(client, monkeypatch, f"问题 {i}")
    assert [i["question"] for i in _history(client)] == ["问题 4", "问题 3", "问题 2"]


def test_search_history_delete(client, llm_ok, monkeypatch):
    """单条删除：删除后列表减少；重复删除 / 不存在 → 404。"""
    _ask(client, monkeypatch, "问题一")
    _ask(client, monkeypatch, "问题二")
    rid = _history(client)[0]["id"]  # 最新的「问题二」

    resp = client.delete(f"/api/search-history/{rid}")
    assert resp.status_code == 204
    assert [i["question"] for i in _history(client)] == ["问题一"]

    resp = client.delete(f"/api/search-history/{rid}")
    assert resp.status_code == 404
    resp = client.delete("/api/search-history/99999")
    assert resp.status_code == 404


def test_ask_invalid_does_not_save_history(client, llm_ok):
    """空问题 422 → 不落检索记录。"""
    resp = client.post("/api/ask", json={"question": "   "})
    assert resp.status_code == 422
    assert _history(client) == []


def test_ask_page_has_history_section(client, llm_ok):
    """检索页渲染：含「检索记录」区块。"""
    resp = client.get("/ask")
    assert resp.status_code == 200
    assert "检索记录" in resp.text
    assert "history-list" in resp.text
