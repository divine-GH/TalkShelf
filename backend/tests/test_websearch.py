"""联网搜索与模型侧 web_fetch 工具测试（设计文档 §6.5/§6.6/§22；M2）。

- 搜索触发：意图词命中才搜（§6.5），失败降级不阻塞；
- 搜索结果注入 kind='search_result' 消息，拍板落库复制进 note_materials（带来源 URL）；
- web_fetch 工具循环：LLM 主动调用抓全文 → tool 材料落库（fetched_page）；
- 工具循环上限：达 WEB_FETCH_TOOL_MAX_ROUNDS 后终止。
LLM / 搜索 / 抓取全 mock，不触网。
"""

import json
import sqlite3

from app import fetch, llm, web_search
from app.fetch import FetchResult

SEARCH_ITEMS = [
    {"url": "https://example.com/a", "title": "结果A", "page_age": "2026-08-19"},
    {"url": "https://example.com/b#frag", "title": "结果B", "page_age": ""},
]


def _fake_fetch(url):
    return FetchResult(url=url, status=200, title="页面", markdown="页面正文内容", truncated=False)


def test_should_search_trigger_words(monkeypatch):
    assert web_search.should_search("顺便查一下 nginx 最新版本")
    assert web_search.should_search("帮我搜一下 frp 的坑")
    assert web_search.should_search("搜索一下 go 语言 1.24")
    assert web_search.should_search("查查资料")
    assert not web_search.should_search("记一下 nginx 配置")
    assert not web_search.should_search("")


def test_search_injected_and_materialized(client, llm_ok, db_path, monkeypatch):
    """意图词触发搜索 → search_result 消息入库（markdown 格式）→ 拍板后材料落库带 URL。"""
    monkeypatch.setattr(web_search, "search", lambda q: SEARCH_ITEMS)
    conv_id = client.post("/api/conversations", json={"message": "查一下 frp 最新版本"}).json()[
        "conversation_id"
    ]

    resp = client.get(f"/api/conversations/{conv_id}").json()
    sr = [m for m in resp["messages"] if m["kind"] == "search_result"]
    assert len(sr) == 1
    assert "- [结果A](https://example.com/a)（2026-08-19）" in sr[0]["content"]
    assert "- [结果B](https://example.com/b)" in sr[0]["content"], "fragment 已剥离"

    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    assert resp.status_code == 200
    note_id = resp.json()["note"]["id"]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT kind, url, text FROM note_materials WHERE note_id = ?", (note_id,)
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, "search_result 复制进 note_materials"
    assert rows[0][0] == "search_result"
    assert rows[0][1] == "https://example.com/a", "材料 URL 从 markdown 链接提取第一条"


def test_no_trigger_no_search(client, llm_ok, monkeypatch):
    """无意图词不触发搜索（§6.5：用户明确要求才搜）。"""

    def boom(*a, **k):
        raise AssertionError("不应触发搜索")

    monkeypatch.setattr(web_search, "search", boom)
    conv_id = client.post("/api/conversations", json={"message": "记一下 frp 配置"}).json()[
        "conversation_id"
    ]
    resp = client.get(f"/api/conversations/{conv_id}").json()
    assert not any(m["kind"] == "search_result" for m in resp["messages"])


def test_search_failure_degraded(client, llm_ok, monkeypatch):
    """搜索失败（§6.5 降级）：对话照常进行，不阻塞、无 search_result 消息。"""
    monkeypatch.setattr(
        web_search, "search", lambda q: (_ for _ in ()).throw(web_search.SearchError("mock 挂了"))
    )
    resp = client.post("/api/conversations", json={"message": "查一下 DeepSeek 新闻"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["searched"] is False
    assert data["degraded"] is False, "搜索失败不影响对话"
    resp = client.get(f"/api/conversations/{data['conversation_id']}").json()
    assert not any(m["kind"] == "search_result" for m in resp["messages"])


def test_web_fetch_tool_loop(client, llm_ok, db_path, monkeypatch):
    """搜索结果注入轮次声明 web_fetch 工具：LLM 调用抓全文 → 工具材料落库（fetched_page）。"""
    calls = {"n": 0}

    def fake_tools(messages, tools, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "", [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "web_fetch",
                        "arguments": json.dumps({"url": "https://example.com/a"}),
                    },
                }
            ]
        return json.dumps(dict(llm_ok), ensure_ascii=False), []

    monkeypatch.setattr(llm, "_chat_with_tools", fake_tools)
    monkeypatch.setattr(fetch, "fetch_page", _fake_fetch)
    monkeypatch.setattr(web_search, "search", lambda q: SEARCH_ITEMS)

    conv_id = client.post("/api/conversations", json={"message": "查一下 frp 文档"}).json()[
        "conversation_id"
    ]
    assert calls["n"] == 2, "第一轮工具调用 + 第二轮最终回复"

    resp = client.get(f"/api/conversations/{conv_id}").json()
    fp = [m for m in resp["messages"] if m["kind"] == "fetched_page"]
    assert len(fp) == 1, "工具循环的抓取结果落库 messages"
    assert "Fetched https://example.com/a (HTTP 200)" in fp[0]["content"]

    # 拍板 → 工具抓取的材料也进 note_materials（Tier 2）
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    note_id = resp.json()["note"]["id"]
    conn = sqlite3.connect(db_path)
    try:
        kinds = [
            r[0]
            for r in conn.execute(
                "SELECT kind FROM note_materials WHERE note_id = ?", (note_id,)
            ).fetchall()
        ]
    finally:
        conn.close()
    assert "fetched_page" in kinds and "search_result" in kinds


def test_web_fetch_tool_loop_cap(client, llm_ok, monkeypatch):
    """工具循环上限：LLM 一直请求工具 → 工具执行受 WEB_FETCH_TOOL_MAX_ROUNDS 约束（不无限循环）。

    总 LLM 调用 = 初始 1 次 + 上限 N 轮续调；工具执行次数 = N。
    """
    from app import config

    monkeypatch.setattr(config, "WEB_FETCH_TOOL_MAX_ROUNDS", 3)
    calls = {"n": 0}

    def always_tools(messages, tools, **kwargs):
        calls["n"] += 1
        return "", [
            {
                "id": f"c-{calls['n']}",
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "arguments": json.dumps({"url": "https://example.com/x"}),
                },
            }
        ]

    monkeypatch.setattr(llm, "_chat_with_tools", always_tools)
    fetches = {"n": 0}

    def counting_fetch(url):
        fetches["n"] += 1
        return _fake_fetch(url)

    monkeypatch.setattr(fetch, "fetch_page", counting_fetch)
    monkeypatch.setattr(web_search, "search", lambda q: SEARCH_ITEMS)

    resp = client.post("/api/conversations", json={"message": "查一下 xxx"})
    assert resp.status_code == 200, "达上限后正常返回（LLM 最后输出被截断也无妨）"
    assert calls["n"] == 1 + 3, "初始 1 次 + 上限 3 轮续调"
    assert fetches["n"] == 3, "工具执行次数 = 上限"
    assert resp.json()["reply"] == "", "无最终文本时返回空串而非报错"


def test_web_fetch_failure_passthrough(client, llm_ok, monkeypatch):
    """web_fetch 抓取失败：错误信息回传 LLM（tool 消息），对话继续、不落库失败材料。"""
    calls = {"n": 0}

    def fake_tools(messages, tools, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "", [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "web_fetch",
                        "arguments": json.dumps({"url": "http://10.0.0.5/"}),
                    },
                }
            ]
        return json.dumps(dict(llm_ok), ensure_ascii=False), []

    monkeypatch.setattr(llm, "_chat_with_tools", fake_tools)
    monkeypatch.setattr(
        fetch,
        "fetch_page",
        lambda url: (_ for _ in ()).throw(fetch.FetchError("SSRF 拒绝内网地址")),
    )
    monkeypatch.setattr(web_search, "search", lambda q: SEARCH_ITEMS)

    conv_id = client.post("/api/conversations", json={"message": "查一下 frp"}).json()[
        "conversation_id"
    ]
    resp = client.get(f"/api/conversations/{conv_id}").json()
    assert not any(m["kind"] == "fetched_page" for m in resp["messages"]), "失败抓取不落库"
