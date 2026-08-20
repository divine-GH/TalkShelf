"""pytest 共享夹具（设计文档 §12 测试约定：LLM 与 embedding 全部 mock）。

- 每个测试独立临时 SQLite（monkeypatch config.DATABASE_PATH，db.connect 动态读取）；
- LLM 层（llm._call_chat / llm.chat_json）默认 mock 为固定整理 JSON，不触网；
- embedding 层（embedding.embed_texts）默认 mock 为确定性伪向量（相同文本 → 相同向量），
  不触 Ollama；真实语义评测走 scripts/eval_retrieval.py（本机 Ollama）。
- 测试通过 TestClient 走完整 HTTP 层 + lifespan（含异步补做队列）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time

import numpy as np
import pytest
from app import config, db, embedding, llm
from fastapi.testclient import TestClient

# 固定整理 JSON（§6.2 示例形态；LLM mock 的统一输出）
ORGANIZED = {
    "title": "nginx 上传大文件限制",
    "content": "nginx client_max_body_size 默认 1M，上传超过 1M 的文件会被 413 拒绝。需要显式调大。",
    "kind": "note",
    "category": "技术",
    "tags": ["nginx", "部署"],
    "summary": "nginx client_max_body_size 默认 1M，导致上传大文件被拒。",
    "importance": 2,
    "entities": [{"type": "project", "name": "nginx"}],
    "source_url": None,
    "duplicate_of": None,
}


@pytest.fixture
def db_path(monkeypatch):
    # 不用 pytest 的 tmp_path / tempfile.mkdtemp：两者都用 os.mkdir(mode=0o700)，
    # Windows 上会生成创建者都无法访问的目录（见 AGENTS.md「目录 mode 陷阱」经验）。
    # Path.mkdir() 默认 mode=0o777，无此问题。
    d = config.BASE_DIR / f".nb-test-{time.time_ns():x}"
    d.mkdir()
    path = d / "test.db"
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    yield path
    # 队列 worker 的 to_thread 线程可能仍持有连接（文件占用导致删除失败），
    # 短重试 + 失败可见——绝不 ignore_errors 静默吞掉（曾因此残留 26 个目录）
    for _ in range(10):
        try:
            shutil.rmtree(d)
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"测试临时目录清理失败（可能文件被占用）: {d}")


@pytest.fixture
def client(db_path):
    from app.main import app

    with TestClient(app) as c:  # lifespan：建表 + 启动队列 + 扫描 pending
        yield c


@pytest.fixture
def conn(db_path):
    conn = db.connect()
    yield conn
    conn.close()


def pseudo(text: str) -> list[float]:
    """确定性伪向量：相同文本 → 相同向量（64 维、归一化）。conftest 与测试恢复 mock 共用。"""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "little")
    v = np.random.default_rng(seed).normal(size=64).astype("<f4")
    v /= np.linalg.norm(v) + 1e-9
    return v.tolist()


@pytest.fixture(autouse=True)
def emb_ok(monkeypatch):
    """embedding mock（autouse）：确定性伪向量，不触 Ollama。

    相同文本 → 相同向量，可测向量召回/查重的结构正确性；
    真实语义评测不在 pytest 内（scripts/eval_retrieval.py，本机 Ollama）。
    """
    monkeypatch.setattr(embedding, "embed_texts", lambda texts: [pseudo(t) for t in texts])


@pytest.fixture
def llm_ok(monkeypatch):
    """LLM 正常：对话输出固定整理 JSON；force_json 整理与查重走 chat_json（按 system prompt 区分）。"""

    def fake_chat(messages, **kwargs):
        return json.dumps(ORGANIZED, ensure_ascii=False)

    def fake_chat_json(messages, validate=None, **kwargs):
        if "查重判断器" in messages[0]["content"]:
            return {"duplicate_of": None}
        return dict(ORGANIZED)  # 整理路径（organize_conversation force_json）

    def fake_chat_tools(messages, tools, **kwargs):
        # 工具路径（搜索结果注入轮次）：LLM 默认不调工具，直接输出整理 JSON
        return json.dumps(ORGANIZED, ensure_ascii=False), []

    monkeypatch.setattr(llm, "_call_chat", fake_chat)
    monkeypatch.setattr(llm, "chat_json", fake_chat_json)
    monkeypatch.setattr(llm, "_chat_with_tools", fake_chat_tools)
    return ORGANIZED


@pytest.fixture
def llm_down(monkeypatch):
    """LLM 完全不可用（直存降级路径）。"""

    def boom(*a, **k):
        raise llm.LLMError("mock: DeepSeek 不可用")

    monkeypatch.setattr(llm, "_call_chat", boom)
    monkeypatch.setattr(llm, "chat_json", boom)
    monkeypatch.setattr(llm, "_chat_with_tools", boom)


def wait_for(predicate, timeout: float = 3.0, desc: str = "条件"):
    """轮询等待异步队列的副作用（补整理/查重/标 failed）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"等待超时：{desc}")


def note_status(path, note_id):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT status, title FROM notes WHERE id = ?", (note_id,)).fetchone()
    finally:
        conn.close()
    return row


def start_conversation(client, message: str) -> int:
    resp = client.post("/api/conversations", json={"message": message})
    assert resp.status_code == 200, resp.text
    return resp.json()["conversation_id"]
