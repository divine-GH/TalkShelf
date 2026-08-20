"""M2 embedding 接入测试：队列补算 / 启动扫描补向量 / 向量查重 / Ollama 失败降级（§14 第 8 条）。

LLM 与 embedding 全 mock（conftest：确定性伪向量），不触网。
"""

import time

from conftest import note_status, wait_for


def test_queue_backfills_embedding(client, llm_ok, db_path):
    """对话落库 → 队列补做管线：embeddings 表写入 float32 向量（§14 第 6 条顺序含 embedding）。"""
    conv_id = client.post(
        "/api/conversations", json={"message": "记一条：frp 隧道必须开 TLS"}
    ).json()["conversation_id"]
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    note_id = resp.json()["note"]["id"]

    wait_for(lambda: _has_embedding(db_path, note_id), desc="补算 embedding")
    vec = _load_embedding(db_path, note_id)
    assert len(vec) == 64, "mock 伪向量 64 维（真实 bge-m3 为 1024 维，BLOB float32）"


def test_startup_scan_backfills_old_notes(client, llm_ok, db_path, monkeypatch):
    """启动扫描补向量：Ollama 挂时保持 pending（§14 第 8 条），恢复后重启自动补向量并推进 processed。"""
    from app import embedding

    # 先让 embedding 一直失败 → 笔记保持 pending 但无向量
    def boom(*a, **k):
        raise embedding.EmbeddingError("mock: Ollama 不可用")

    monkeypatch.setattr(embedding, "embed_texts", boom)

    resp = client.post("/api/notes", json={"raw": "Ollama 挂掉期间记的笔记", "kind": "note"})
    note_id = resp.json()["note_id"]
    # LLM 整理完成（title 就绪）但 embedding 失败 → 保持 pending、无向量、不标 failed
    wait_for(
        lambda: (
            note_status(db_path, note_id)[1] is not None and not _has_embedding(db_path, note_id)
        ),
        desc="LLM 整理完成且无向量",
    )
    assert note_status(db_path, note_id)[0] == "pending", (
        "embedding 失败保持 pending（§14 第 8 条）"
    )

    # 恢复 Ollama，重启（lifespan 启动扫描补向量）
    from app.main import app
    from conftest import pseudo
    from fastapi.testclient import TestClient

    monkeypatch.setattr(embedding, "embed_texts", lambda texts: [pseudo(t) for t in texts])
    client.close()
    with TestClient(app):
        wait_for(lambda: _has_embedding(db_path, note_id), desc="重启扫描补向量")
    wait_for(lambda: note_status(db_path, note_id)[0] == "processed", desc="补向量后推进 processed")
    assert note_status(db_path, note_id)[0] == "processed"


def test_vector_dedup_marks_duplicate(client, llm_ok, db_path, monkeypatch):
    """查重升级向量版：相似笔记（相同整理文本 → 相同伪向量 → Top-1 命中）→ LLM 判重复 → duplicate。"""
    from app import llm

    resp = client.post(
        "/api/notes", json={"raw": "nginx client_max_body_size 默认 1M 上传限制", "kind": "note"}
    )
    old_id = resp.json()["note_id"]
    wait_for(
        lambda: note_status(db_path, old_id)[0] in ("processed", "duplicate"), desc="旧笔记处理完"
    )
    assert _has_embedding(db_path, old_id)

    monkeypatch.setattr(llm, "judge_duplicate", lambda new_summary, candidates: old_id)
    resp = client.post(
        "/api/notes",
        json={"raw": "nginx client_max_body_size 默认 1M 上传被拒的坑", "kind": "note"},
    )
    new_id = resp.json()["note_id"]
    wait_for(lambda: note_status(db_path, new_id)[0] == "duplicate", desc="向量查重标 duplicate")
    items = client.get("/api/notes").json()["items"]
    assert any(n["id"] == new_id for n in items), "命中重复仍不丢输入"


def test_dedup_falls_back_to_fts_when_no_embeddings(client, llm_ok, db_path, monkeypatch):
    """Ollama 挂但库内已有向量时：向量召回不可用 → 退化 FTS 近似版查重（§14 第 8 条）。"""
    from app import embedding, llm

    resp = client.post(
        "/api/notes", json={"raw": "查重降级测试：python 3.14 GIL 移除", "kind": "note"}
    )
    old_id = resp.json()["note_id"]
    wait_for(
        lambda: note_status(db_path, old_id)[0] in ("processed", "duplicate"), desc="旧笔记处理完"
    )

    # 查重时 Ollama 挂：check_duplicate 内部向量路抛 EmbeddingError → 退 FTS；judge 判重复
    monkeypatch.setattr(
        embedding,
        "vector_candidates",
        lambda *a, **k: (_ for _ in ()).throw(embedding.EmbeddingError("mock 挂了")),
    )
    monkeypatch.setattr(llm, "judge_duplicate", lambda new_summary, candidates: old_id)
    resp = client.post("/api/notes", json={"raw": "python 3.14 GIL 移除的细节", "kind": "note"})
    new_id = resp.json()["note_id"]
    wait_for(lambda: note_status(db_path, new_id)[0] == "duplicate", desc="FTS 降级查重")
    assert note_status(db_path, new_id)[0] == "duplicate"


def test_dedup_uses_fts_when_embedding_disabled(client, llm_ok, db_path, monkeypatch):
    """设置页关闭 embedding（§35）→ 查重直接走 FTS 近似版：不补向量、不碰向量召回。"""
    from app import embedding, llm

    client.put("/api/settings", json={"embedding_enabled": False})
    # 向量路一碰就炸：关闭后 check_duplicate 若走向量召回，处理会失败、本测试必挂
    monkeypatch.setattr(
        embedding,
        "vector_candidates",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("关闭后不应走向量查重")),
    )

    resp = client.post("/api/notes", json={"raw": "开关测试：sqlite WAL 模式", "kind": "note"})
    old_id = resp.json()["note_id"]
    wait_for(
        lambda: note_status(db_path, old_id)[0] in ("processed", "duplicate"), desc="旧笔记处理完"
    )
    assert not _has_embedding(db_path, old_id), "关闭时不应补算向量"

    monkeypatch.setattr(llm, "judge_duplicate", lambda new_summary, candidates: old_id)
    resp = client.post("/api/notes", json={"raw": "sqlite WAL 模式细节", "kind": "note"})
    new_id = resp.json()["note_id"]
    wait_for(
        lambda: note_status(db_path, new_id)[0] == "duplicate", desc="FTS 降级查重标 duplicate"
    )
    assert note_status(db_path, new_id)[0] == "duplicate"


def test_startup_scan_skips_missing_vectors_when_disabled(client, llm_ok, db_path, monkeypatch):
    """关闭 embedding 时启动扫描不补向量（§35）：缺向量老笔记不会反复空跑。"""
    import sqlite3
    import time

    from app import embedding
    from app.main import app
    from fastapi.testclient import TestClient

    # 先开着 embedding 造一条 processed 老笔记，再删掉向量模拟「缺向量老笔记」
    resp = client.post("/api/notes", json={"raw": "启动扫描开关测试笔记", "kind": "note"})
    note_id = resp.json()["note_id"]
    wait_for(lambda: note_status(db_path, note_id)[0] == "processed", desc="处理完成")
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM embeddings WHERE note_id = ?", (note_id,))
    conn.commit()
    conn.close()

    # 关闭 embedding 后重启：扫描不应再补向量（spy 一调就记）
    client.put("/api/settings", json={"embedding_enabled": False})
    calls = []
    orig = embedding.embed_note

    def spy(note):
        calls.append(note)
        return orig(note)

    monkeypatch.setattr(embedding, "embed_note", spy)
    client.close()
    with TestClient(app):
        time.sleep(0.3)  # 给 worker 空窗：若误提交会立即调 embed_note
    assert calls == [], "关闭时启动扫描不应补向量"


def test_embedding_failure_backoff_then_stays(client, llm_ok, db_path, monkeypatch):
    """embedding 持续失败：退避重试后不标 failed（区别于 LLM 失败 → failed），保持 pending（§14 第 8 条）。"""
    from app import config, embedding

    monkeypatch.setattr(config, "BACKOFF_SCHEDULE", [0.02, 0.02, 0.02, 0.02, 0.02])
    monkeypatch.setattr(
        embedding,
        "embed_texts",
        lambda *a, **k: (_ for _ in ()).throw(embedding.EmbeddingError("mock 一直挂")),
    )

    resp = client.post(
        "/api/notes", json={"raw": "embedding 一直失败也不该标 failed 的笔记", "kind": "note"}
    )
    note_id = resp.json()["note_id"]
    # LLM 整理完成（title 就绪）→ 5 次退避全部耗尽（0.02*5+余量）→ 仍保持 pending、不标 failed
    wait_for(lambda: note_status(db_path, note_id)[1] is not None, desc="整理完成")
    time.sleep(0.5)
    status, title = note_status(db_path, note_id)
    assert status == "pending", "embedding 失败保持 pending，不标 failed"
    assert title is not None, "LLM 整理结果已分段提交、不被回滚"


def _has_embedding(path, note_id) -> bool:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return (
            conn.execute("SELECT 1 FROM embeddings WHERE note_id = ?", (note_id,)).fetchone()
            is not None
        )
    finally:
        conn.close()


def _load_embedding(path, note_id):
    import sqlite3

    import numpy as np

    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT vector FROM embeddings WHERE note_id = ?", (note_id,)).fetchone()
    finally:
        conn.close()
    assert row, f"笔记 {note_id} 无向量"
    return np.frombuffer(row[0], dtype="<f4")
