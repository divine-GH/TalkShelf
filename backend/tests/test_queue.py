"""直存降级 / 异步补做管线 / 查重 / 退避测试（设计文档 §14 第 5 条、§6.2、§21.2 #5）。"""

import json

from conftest import conv_last_message, note_status, wait_for


def test_direct_create_note_pending_then_processed(client, llm_ok, db_path):
    """POST /api/notes：202 + pending → 队列补整理（标题等元数据）→ processed。"""
    resp = client.post(
        "/api/notes", json={"raw": "直存一条：python 3.14 的 GIL 移除细节", "kind": "note"}
    )
    assert resp.status_code == 202
    note_id = resp.json()["note_id"]
    assert note_status(db_path, note_id)[0] == "pending"

    wait_for(lambda: note_status(db_path, note_id)[0] == "processed", desc="补整理完成")
    status, title = note_status(db_path, note_id)
    assert status == "processed"
    assert title == "nginx 上传大文件限制"  # mock 整理结果


def test_pending_note_searchable_immediately(client, llm_down, db_path):
    """降级期间（LLM 挂）：直存笔记 raw 立即可检索（§14 第 5 条：FTS 同步不依赖 LLM）。"""
    resp = client.post(
        "/api/notes", json={"raw": "降级期间记的东西：frp 隧道要开 TLS", "kind": "note"}
    )
    note_id = resp.json()["note_id"]
    assert note_status(db_path, note_id)[0] == "pending"
    items = client.get("/api/notes", params={"q": "隧道要开"}).json()["items"]
    assert any(n["id"] == note_id for n in items), "pending 笔记也应可检索"


def test_degraded_confirm_goes_pending_then_recovers(client, llm_down, db_path, monkeypatch):
    """拍板时 LLM 挂 → 直存 pending（返回 degraded 提示）→ LLM 恢复 → 队列退避重试自动补整理（§14 第 5 条）。

    ⚠️ 必须缩短 BACKOFF_SCHEDULE：默认退避首档 60s，wait_for 3s 等不到重试（M1 遗留的沉睡测试 bug）。
    """
    from app import config

    monkeypatch.setattr(config, "BACKOFF_SCHEDULE", [0.05, 0.05, 0.05, 0.05, 0.05])
    conv_id = client.post("/api/conversations", json={"message": "DeepSeek 挂了也能记"}).json()[
        "conversation_id"
    ]
    # 对话中 LLM 挂：后台降级提示（§32 异步生成，轮询等待）
    wait_for(
        lambda: "AI 整理服务暂不可用" in conv_last_message(client, conv_id).get("content", ""),
        desc="后台降级提示",
    )
    resp = client.post(f"/api/conversations/{conv_id}/messages", json={"message": "再补一句"})
    assert resp.status_code == 200
    wait_for(
        lambda: "AI 整理服务暂不可用" in conv_last_message(client, conv_id).get("content", ""),
        desc="第二轮降级提示（连发消息自动续轮）",
    )
    # 拍板 → 直存 pending（confirm 仍同步返回 degraded 提示）
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    assert resp.status_code == 200
    assert resp.json()["degraded"] is True
    note_id = resp.json()["note"]["id"]
    assert note_status(db_path, note_id)[0] == "pending"

    # LLM 恢复：mock 成功输出 → 队列退避重试后补整理
    from app import llm

    organized = {
        "title": "恢复后的标题",
        "content": None,
        "kind": "note",
        "category": "技术",
        "tags": ["恢复"],
        "summary": "恢复后的摘要",
        "importance": 2,
        "entities": [],
        "source_url": None,
        "duplicate_of": None,
    }
    monkeypatch.setattr(
        llm, "_call_chat", lambda *a, **k: json.dumps(organized, ensure_ascii=False)
    )
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: dict(organized))  # force_json 整理路径
    monkeypatch.setattr(llm, "judge_duplicate", lambda *a, **k: None)
    wait_for(
        lambda: note_status(db_path, note_id)[0] in ("processed", "duplicate"), desc="恢复后补整理"
    )
    status, title = note_status(db_path, note_id)
    assert status == "processed"
    assert title == "恢复后的标题"


def test_backoff_then_failed(client, db_path, monkeypatch):
    """补整理持续失败 → 退避重试 5 次 → 标 failed（§14 第 5 条；failed 不自动重试）。"""
    from app import config, llm

    monkeypatch.setattr(config, "BACKOFF_SCHEDULE", [0.05, 0.05, 0.05, 0.05, 0.05])
    monkeypatch.setattr(
        llm, "_call_chat", lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("mock 一直挂"))
    )

    resp = client.post("/api/notes", json={"raw": "永远整理不出来的笔记", "kind": "note"})
    note_id = resp.json()["note_id"]
    wait_for(
        lambda: note_status(db_path, note_id)[0] == "failed", timeout=5.0, desc="退避耗尽标 failed"
    )
    assert note_status(db_path, note_id)[1] is None  # 无元数据


def test_duplicate_marked_async(client, llm_ok, db_path, monkeypatch):
    """查重（M1 FTS 近似版）：新笔记与旧笔记重复 → 标 duplicate，且不丢输入（§6.2）。"""
    from app import llm

    # 旧笔记先入库（mock 查重判不重复）
    resp = client.post(
        "/api/notes", json={"raw": "nginx client_max_body_size 默认 1M 上传限制", "kind": "note"}
    )
    old_id = resp.json()["note_id"]
    wait_for(
        lambda: note_status(db_path, old_id)[0] in ("processed", "duplicate"), desc="旧笔记处理完"
    )

    # 新笔记与旧笔记相似；mock 查重判定重复 → 旧笔记 id（只替换 judge_duplicate，不碰整理路径）
    monkeypatch.setattr(llm, "judge_duplicate", lambda new_summary, candidates: old_id)
    resp = client.post(
        "/api/notes",
        json={"raw": "nginx client_max_body_size 默认 1M 上传被拒的坑", "kind": "note"},
    )
    new_id = resp.json()["note_id"]
    wait_for(lambda: note_status(db_path, new_id)[0] == "duplicate", desc="查重标 duplicate")

    status, _ = note_status(db_path, new_id)
    assert status == "duplicate"
    # 不丢输入：仍在列表（M1 只提示不拦截）
    items = client.get("/api/notes").json()["items"]
    assert any(n["id"] == new_id for n in items)


def test_duplicate_failure_not_fatal(client, llm_ok, db_path, monkeypatch):
    """查重失败（LLM 挂）只记日志、笔记保持 processed，不反噬（§21.2 #5）。"""
    from app import llm

    monkeypatch.setattr(
        llm, "judge_duplicate", lambda *a, **k: (_ for _ in ()).throw(llm.LLMError("查重挂了"))
    )
    resp = client.post("/api/notes", json={"raw": "不会被查重影响入库的笔记内容", "kind": "note"})
    note_id = resp.json()["note_id"]
    wait_for(
        lambda: note_status(db_path, note_id)[0] in ("processed", "duplicate"), desc="补处理完成"
    )
    status, _ = note_status(db_path, note_id)
    assert status == "processed", "查重失败不反噬，笔记保持 processed"


def test_startup_rescans_pending(client, llm_down, db_path, monkeypatch):
    """应用重启：启动扫描 pending 补处理（§14 第 5 条；failed 不自动重试）。"""
    resp = client.post("/api/notes", json={"raw": "重启前遗留的 pending 笔记", "kind": "note"})
    note_id = resp.json()["note_id"]
    assert note_status(db_path, note_id)[0] == "pending"

    # 关闭旧客户端（lifespan 结束），LLM 恢复后重启
    client.close()
    from app import llm
    from app.main import app
    from fastapi.testclient import TestClient

    organized = {
        "title": "重启补处理标题",
        "content": None,
        "kind": "note",
        "category": "学习",
        "tags": ["重启"],
        "summary": "重启补处理摘要",
        "importance": 2,
        "entities": [],
        "source_url": None,
        "duplicate_of": None,
    }
    monkeypatch.setattr(
        llm, "_call_chat", lambda *a, **k: json.dumps(organized, ensure_ascii=False)
    )
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: dict(organized))  # force_json 整理路径
    monkeypatch.setattr(llm, "judge_duplicate", lambda *a, **k: None)
    with TestClient(app):  # lifespan 启动扫描 pending
        wait_for(lambda: note_status(db_path, note_id)[0] == "processed", desc="重启后补处理")
        assert note_status(db_path, note_id)[1] == "重启补处理标题"
