"""对话式检索测试（设计文档 §36）：检索会话创建/追问/工具/联网触发/降级/删除 + 与记录对话隔离。

覆盖点：
- 首轮自动检索注入（search_hits 结构化 JSON，含 query/sources/vector_ok/weak_recall）+ LLM 回复落库；
- 追问：第二轮注入 query 为最新用户消息；连续追问自动续轮；
- note_search 工具：LLM 工具调用 → 执行检索 → 结果落库为 search_hits（追溯）；
- 联网搜索：意图词触发 → search_result 落库 + 工具集包含 web_fetch（§23.3 受控语义）；
- LLM 不可用：降级错误文本落库，不 500；
- 会话列表/删除/隔离：检索会话不进草稿列表、不能被拍板确认；旧 /api/ask 已删除（404）。

LLM 与 embedding 全 mock（conftest），不触网；web_search.search 按需 monkeypatch。
"""

import json

from conftest import note_status, wait_for


def _mk_note(client, db_path, raw: str) -> int:
    resp = client.post("/api/notes", json={"raw": raw, "kind": "note"})
    assert resp.status_code == 202, resp.text
    note_id = resp.json()["note_id"]
    wait_for(
        lambda: note_status(db_path, note_id)[0] in ("processed", "duplicate"),
        desc=f"笔记 {note_id} 整理完成",
    )
    return note_id


def _conv_messages(client, conv_id) -> list[dict]:
    resp = client.get(f"/api/search/conversations/{conv_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["messages"]


def _hits_in(client, conv_id) -> list[dict]:
    """解析会话中的 search_hits 消息（结构化 JSON）。"""
    out = []
    for m in _conv_messages(client, conv_id):
        if m["kind"] != "search_hits":
            continue
        out.append(json.loads(m["content"]))
    return out


def _last_text(client, conv_id) -> dict:
    msgs = _conv_messages(client, conv_id)
    return msgs[-1] if msgs else {}


def _has_assistant_text(client, conv_id) -> bool:
    """是否有 assistant text 回复（user 消息的 kind 也是 'text'，谓词必须同时看 role）。"""
    last = _last_text(client, conv_id)
    return last.get("role") == "assistant" and last.get("kind") == "text"


def start_search(client, message: str) -> int:
    """创建检索会话并等待后台回复落库（§36：POST 立即返回，检索+回复异步生成）。"""
    resp = client.post("/api/search/conversations", json={"message": message})
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["conversation_id"]
    wait_for(
        lambda: _has_assistant_text(client, conv_id),
        desc=f"检索会话 #{conv_id} LLM 回复",
    )
    return conv_id


def test_search_conv_basic_flow(client, llm_ok, db_path):
    """全流程：入库笔记 → 检索会话 → 自动注入（search_hits）+ LLM 回复落库（§36）。"""
    _mk_note(client, db_path, "nginx client_max_body_size 默认 1M 上传大文件被拒 413 需要调大")
    conv_id = start_search(client, "我记的上传文件的坑是什么")

    hits_list = _hits_in(client, conv_id)
    assert hits_list, "应有一条自动检索注入（search_hits）"
    hits = hits_list[0]
    assert hits["query"] == "我记的上传文件的坑是什么"
    assert hits["sources"], "注入应有引用来源"
    assert any("id" in s and "title" in s for s in hits["sources"])
    assert hits["vector_ok"] is True
    assert isinstance(hits["weak_recall"], bool)

    last = _last_text(client, conv_id)
    assert last["role"] == "assistant" and last["kind"] == "text" and last["content"]


def test_search_conv_followup_retrieves_latest_query(client, llm_ok, db_path):
    """追问：第二轮自动注入的 query 是最新用户消息（多轮上下文，§36）。"""
    _mk_note(client, db_path, "nginx client_max_body_size 默认 1M 上传大文件被拒 413 需要调大")
    conv_id = start_search(client, "我记的上传文件的坑是什么")
    resp = client.post(
        f"/api/search/conversations/{conv_id}/messages",
        json={"message": "不是 nginx 那个，是群辉的"},
    )
    assert resp.status_code == 200, resp.text
    wait_for(
        lambda: len(_hits_in(client, conv_id)) >= 2 and _has_assistant_text(client, conv_id),
        desc="第二轮检索注入 + 回复",
    )
    assert _hits_in(client, conv_id)[1]["query"] == "不是 nginx 那个，是群辉的"


def test_search_conv_note_search_tool(client, llm_ok, db_path, monkeypatch):
    """note_search 工具：LLM 首轮声明工具调用 → 执行检索 → 结果落库（追溯），再续轮出最终回复。"""
    from app import llm

    _mk_note(client, db_path, "nginx client_max_body_size 默认 1M 上传大文件被拒 413 需要调大")
    calls = [
        (
            "先看看笔记库",
            [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "note_search",
                        "arguments": json.dumps({"query": "nginx 上传大文件限制"}),
                    },
                }
            ],
        ),
        ("找到了：上传被拒是默认 1M 限制造成的 [1]", []),
    ]

    def fake_tools(messages, tools, **kwargs):
        captured["tools"] = tools
        captured["saw_tool_result"] = any(m.get("role") == "tool" for m in messages)
        return calls.pop(0)

    captured = {}
    monkeypatch.setattr(llm, "_chat_with_tools", fake_tools)
    conv_id = start_search(client, "我记的上传文件的坑是什么")

    assert captured["tools"], "应声明工具"
    assert any(t["function"]["name"] == "note_search" for t in captured["tools"])
    assert captured["saw_tool_result"], "工具结果应回传给 LLM 继续生成"
    hits_list = _hits_in(client, conv_id)
    assert len(hits_list) == 2, "自动注入 1 条 + 工具检索 1 条"
    assert hits_list[1]["query"] == "nginx 上传大文件限制"
    assert _last_text(client, conv_id)["content"].startswith("找到了")


def test_search_conv_search_trigger_injects_and_web_fetch_tool(
    client, llm_ok, db_path, monkeypatch
):
    """联网搜索（§23.3 受控语义）：意图词命中 → search_result 落库，且工具集含 web_fetch。"""
    from app import llm, web_search

    monkeypatch.setattr(
        web_search,
        "search",
        lambda q: [{"url": "https://example.com/nginx-413", "title": "413 头", "page_age": "3天"}],
    )
    captured = {"tools": None}

    def fake_tools(messages, tools, **kwargs):
        captured["tools"] = tools
        return ("答案：有搜索结果 [1]", [])

    monkeypatch.setattr(llm, "_chat_with_tools", fake_tools)
    conv_id = start_search(client, "帮我查一下 nginx 413 报错原因")

    msgs = _conv_messages(client, conv_id)
    kinds = [m["kind"] for m in msgs]
    assert "search_result" in kinds, "意图词命中应触发并落库搜索结果"
    assert captured["tools"] and any(
        t["function"]["name"] == "web_fetch" for t in captured["tools"]
    ), "有搜索结果时应声明 web_fetch 工具（§22.3）"
    assert any(t["function"]["name"] == "note_search" for t in captured["tools"])


def test_search_conv_no_trigger_skips_web(client, llm_ok, db_path, monkeypatch):
    """无意图词 → 不触发联网搜索（§23.3 受控语义：不声明工具让模型自由搜）。"""
    from app import llm, web_search

    def boom(*a, **k):
        raise AssertionError("无意图词不应调用 web_search.search")

    monkeypatch.setattr(web_search, "search", boom)
    captured = {"tools": None}

    def fake_tools(messages, tools, **kwargs):
        captured["tools"] = tools
        return ("直接按笔记答 [1]", [])

    monkeypatch.setattr(llm, "_chat_with_tools", fake_tools)
    conv_id = start_search(client, "我记的上传文件的坑是什么")

    kinds = [m["kind"] for m in _conv_messages(client, conv_id)]
    assert "search_result" not in kinds
    assert captured["tools"] and len(captured["tools"]) == 1, "只声明 note_search，不含 web_fetch"


def test_search_conv_assistant_content_html(client, llm_ok, db_path, monkeypatch):
    """§37：检索会话 assistant 回复经 API 带服务端渲染 content_html（ask.html JS 渲染用）。"""
    from app import llm

    monkeypatch.setattr(
        llm, "_chat_with_tools", lambda *a, **k: ("**答案**：[1] `nginx` 默认 1M", [])
    )
    conv_id = start_search(client, "上传文件的坑")
    last = _last_text(client, conv_id)
    assert last["content_html"], "assistant 文本消息应带 content_html"
    assert "<strong>答案</strong>" in last["content_html"]
    assert "<code>nginx</code>" in last["content_html"]


def test_search_conv_llm_down_degraded(client, llm_down, db_path):
    """LLM 不可用：降级错误文本落库（轮次仍结束），会话不 500。

    注意 llm_down 下矩阵整理也不可用，此处不建笔记（检索注入空结果即可，不阻塞答复）。
    """
    resp = client.post("/api/search/conversations", json={"message": "上传文件的坑"})
    assert resp.status_code == 200, resp.text
    conv_id = resp.json()["conversation_id"]
    wait_for(
        lambda: _has_assistant_text(client, conv_id),
        desc="降级提示落库",
    )
    assert "暂不可用" in _last_text(client, conv_id)["content"]


def test_search_conv_empty_message_422(client, llm_ok):
    resp = client.post("/api/search/conversations", json={"message": "   "})
    assert resp.status_code == 422


def test_search_conv_list_newest_first(client, llm_ok, db_path, monkeypatch):
    """会话列表：新→旧 + preview 为首条提问 + message_count。"""
    from app import llm

    monkeypatch.setattr(llm, "_chat_with_tools", lambda *a, **k: ("答 [1]", []))
    id1 = start_search(client, "第一个问题 上传文件")
    id2 = start_search(client, "第二个问题 群辉同步")
    items = client.get("/api/search/conversations").json()["items"]
    assert [i["id"] for i in items] == [id2, id1]
    assert items[0]["preview"] == "第二个问题 群辉同步"
    assert items[0]["message_count"] >= 3  # user + search_hits + text
    assert items[0]["updated_at"]


def test_search_conv_delete(client, llm_ok, db_path, monkeypatch):
    """删除会话：204 无 body；重复删除 / 不存在 → 404；记录对话不受影响。"""
    from app import llm

    monkeypatch.setattr(llm, "_chat_with_tools", lambda *a, **k: ("答 [1]", []))
    from conftest import start_conversation

    conv_id = start_search(client, "要删除的问题 nginx")
    start_conversation(client, "记录对话不受影响测试")  # 记录对话会话
    resp = client.delete(f"/api/search/conversations/{conv_id}")
    assert resp.status_code == 204
    assert resp.content == b"", "204 禁止带 body（JSONResponse(204, None) 会发 b'null'，h11 报错）"
    assert client.get(f"/api/search/conversations/{conv_id}").status_code == 404
    assert client.delete(f"/api/search/conversations/{conv_id}").status_code == 404
    assert client.get("/api/search/conversations").json()["items"] == []


def test_search_conv_isolated_from_record_conversations(client, llm_ok, db_path):
    """隔离：检索会话不出现在记录草稿列表，且不能被拍板（§36 status='search'）。"""
    from conftest import start_conversation

    conv_id = start_search(client, "检索会话隔离测试 nginx")
    start_conversation(client, "记录草稿隔离测试")

    drafts = client.get("/api/conversations").json()["items"]
    assert all(c["id"] != conv_id for c in drafts), "检索会话不应出现在草稿列表"

    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    assert resp.status_code == 409, "检索会话不应可拍板"
    resp = client.delete(f"/api/conversations/{conv_id}")
    assert resp.status_code == 409, "检索会话不应可放弃（走检索会话删除端点）"


def test_old_ask_and_search_history_removed(client, llm_ok):
    """旧 /api/ask 与 /api/search-history 已删除（§36：升级为对话式检索，检索记录被会话取代）。"""
    assert client.post("/api/ask", json={"question": "x"}).status_code == 404
    assert client.get("/api/search-history").status_code == 404
    assert client.post("/api/settings/clear-search-history").status_code == 404


def test_search_conv_page_html(client, llm_ok):
    """检索页渲染：对话式元素（会话列表/对话区/新对话按钮）。"""
    resp = client.get("/ask")
    assert resp.status_code == 200
    assert "检索" in resp.text
    assert "ask-history" in resp.text
    assert "ask-chat-section" in resp.text
    assert "新对话" in resp.text
    assert "检索记录" not in resp.text, "旧「检索记录」区块已移除"
