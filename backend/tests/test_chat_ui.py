"""聊天页 UI 测试：外层类名（pre-wrap 空白修复）/ 材料默认折叠 / 整理预览卡 / 材料先到（§32）。"""

import json
import time

from app import fetch, llm
from conftest import conv_last_message, wait_for


def _reply_arrived(client, conv_id) -> bool:
    """LLM 回复落库（user 消息的 kind 也是 'text'，谓词必须同时看 role，防竞态误判）。"""
    m = conv_last_message(client, conv_id)
    return m.get("role") == "assistant" and m.get("kind") == "text"


def test_chat_ui_fixes(client, llm_ok, monkeypatch):
    """抓取材料 + 整理 JSON 回复落库后，聊天页渲染：无外层 msg-text、材料折叠、预览卡。"""
    from app import fetch as fetch_mod

    def fake_fetch(url):
        return fetch.FetchResult(
            url=url,
            status=200,
            title="测试页",
            markdown="这是一段抓取到的网页正文，包含专业词汇 BGP 配置细节",
            truncated=False,
        )

    monkeypatch.setattr(fetch_mod, "fetch_page", fake_fetch)
    conv_id = client.post(
        "/api/conversations", json={"message": "看这个链接 https://example.com/a 挺有用"}
    ).json()["conversation_id"]
    wait_for(
        lambda: _reply_arrived(client, conv_id),
        desc="LLM 回复落库",
        # 默认 3s 太紧：机器负载高（本机同时跑 uvicorn 等）时后台异步任务偶发 >3s 才排上线程
        # （全 mock 路径实测调度到执行约 0.1s，2026-08-25 偶发 4/5 失败，与业务改动无关）
        timeout=10.0,
    )
    page = client.get(f"/conversations/{conv_id}").text

    # 1) 外层不再带 msg-text（pre-wrap 空白 bug），列表区不再有大段落空行
    assert 'class="msg msg-user msg-kind-text"' in page
    assert 'class="msg msg-user msg-text"' not in page
    assert 'class="msg msg-assistant msg-kind-text"' in page

    # 2) 材料默认折叠：<details class="msg-material">（无 open 属性），正文在 pre 里
    assert '<details class="msg-material">' in page
    assert '<details class="msg-material" open' not in page
    assert "抓取的网页正文" in page

    # 3) 整理 JSON 渲染为预览卡，不再直接吐原始 JSON 文本
    assert 'class="msg-organized-card"' in page
    assert "nginx 上传大文件限制" in page  # 卡片标题
    assert "原始整理 JSON" in page  # 原始 JSON 收进折叠块，仍可查
    assert "建议收藏" in page

    # 4) §37：GET 消息带服务端统一解析的 organized（前端不再维护第二套 parseOrganized）
    last = conv_last_message(client, conv_id)
    assert last["organized"]["title"] == "nginx 上传大文件限制"
    assert last["organized"]["kind"] == "note"


def test_chat_assistant_md_rendered(client, llm_ok, monkeypatch):
    """§37：非整理 JSON 的 assistant 回复按 Markdown 渲染（API content_html + 页面 SSR |md）。"""
    from app import llm

    monkeypatch.setattr(
        llm,
        "_call_chat",
        lambda *a, **k: (
            "**加粗** 结论：`code` 示例，参考[链接](https://a.com/x)。<script>alert(1)</script>"
        ),
    )
    conv_id = client.post("/api/conversations", json={"message": "帮我总结一下"}).json()[
        "conversation_id"
    ]
    wait_for(lambda: _reply_arrived(client, conv_id), desc="LLM 回复落库")
    last = conv_last_message(client, conv_id)
    assert last["content_html"], "assistant 文本消息应带服务端渲染的 content_html"
    assert "<strong>加粗</strong>" in last["content_html"]
    assert "<code>code</code>" in last["content_html"]
    assert 'href="https://a.com/x"' in last["content_html"]
    # XSS：原始 HTML 只能作为文本显示（先转义再渲染，不能有可执行的 <script>）
    assert "<script" not in last["content_html"]
    assert "&lt;script&gt;" in last["content_html"]
    # 页面 SSR 与 API 同源渲染（同一 mdrender 函数）
    page = client.get(f"/conversations/{conv_id}").text
    assert 'class="msg-text msg-md"' in page
    assert "<strong>加粗</strong>" in page


def test_material_visible_before_reply(client, llm_ok, monkeypatch):
    """§32 材料先到：慢 LLM 期间轮询已能看到抓取材料（commit 提前）。"""
    from app import fetch as fetch_mod

    def fake_fetch(url):
        return fetch.FetchResult(
            url=url, status=200, title="测试页", markdown="正文", truncated=False
        )

    monkeypatch.setattr(fetch_mod, "fetch_page", fake_fetch)

    def slow_chat(messages, **kwargs):
        time.sleep(0.6)
        return json.dumps(llm_ok, ensure_ascii=False)

    monkeypatch.setattr(llm, "_call_chat", slow_chat)
    conv_id = client.post(
        "/api/conversations", json={"message": "URL https://example.com/b"}
    ).json()["conversation_id"]

    def material_arrived():
        msgs = client.get(f"/api/conversations/{conv_id}").json()["messages"]
        return any(m["kind"] == "fetched_page" for m in msgs)

    wait_for(material_arrived, desc="材料先可见")
    msgs = client.get(f"/api/conversations/{conv_id}").json()["messages"]
    assert msgs[-1]["kind"] == "fetched_page", "回复未到时最后一条是材料"
    wait_for(lambda: _reply_arrived(client, conv_id), desc="LLM 回复后到")
    kinds = [
        m["kind"]
        for m in client.get(f"/api/conversations/{conv_id}").json()["messages"]
        if m["role"] == "assistant"
    ]
    assert kinds == ["fetched_page", "text"]  # 材料在前、回复在后
