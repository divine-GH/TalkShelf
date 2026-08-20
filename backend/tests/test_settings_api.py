"""设置 API 测试（设计文档 §28：settings 表键值覆盖 .env 默认值，改完立即生效、重启不丢）。

- GET /api/settings：默认值（回落 config/.env）、DB 覆盖后生效值、分类体系、版本；
- PUT /api/settings：白名单校验（非法键/值 422）、部分更新、value=None 恢复默认；
- 每周总结开关：关闭后 /api/weekly 不调 LLM、纯统计文本；
- 默认分类：直存（POST /api/notes）与拍板降级直存都打上兜底分类；重置后不启用；
- 模型/检索参数：覆盖后 llm/embedding/search 实际读取生效值；
- 数据管理：清空检索记录、failed 笔记列表 + 重试（复用 /api/notes/{id}/reprocess）；
- 修改登录密码：旧密码校验、新密码落库后覆盖 .env 密码。
"""

import re
import sqlite3

import pytest
from app import auth, config, embedding, llm, providers, retrieval, settings
from conftest import start_conversation, wait_for


@pytest.fixture
def auth_on(monkeypatch):
    """启用登录（配 AUTH_PASSWORD）+ 重置 argon2 哈希缓存（模块级缓存跨测试会串）。"""
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    monkeypatch.setattr(auth, "_password_hash_cache", None)
    return "secret"


def _has_embedding(path, note_id) -> bool:
    conn = sqlite3.connect(path)
    try:
        return (
            conn.execute("SELECT 1 FROM embeddings WHERE note_id = ?", (note_id,)).fetchone()
            is not None
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET / PUT /api/settings
# ---------------------------------------------------------------------------


def test_get_settings_defaults(client):
    data = client.get("/api/settings").json()
    assert data["weekly_llm"] is True
    assert data["default_category"] == ""
    assert data["llm_provider"] == config.LLM_PROVIDER
    assert data["llm_model"] == config.LLM_MODEL
    assert data["embed_model"] == config.EMBED_MODEL
    assert data["search_model"] == config.SEARCH_MODEL
    assert data["vector_top_k"] == config.VECTOR_TOP_K
    assert data["fts_top_k"] == config.FTS_TOP_K
    assert data["ask_top_n"] == config.ASK_TOP_N
    assert data["vector_min_sim"] == config.VECTOR_MIN_SIM
    assert data["materials_top_k"] == config.MATERIALS_TOP_K
    assert data["password_set"] is False
    assert set(data["categories"]) == set(config.CATEGORIES)
    assert data["app_version"] == config.APP_VERSION


def test_put_settings_update_and_reset(client):
    resp = client.put(
        "/api/settings",
        json={
            "weekly_llm": False,
            "default_category": "工作",
            "llm_provider": "openai",
            "llm_model": "gpt-4o",
            "vector_top_k": 3,
            "vector_min_sim": 0.3,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["weekly_llm"] is False
    assert data["default_category"] == "工作"
    assert data["llm_provider"] == "openai"
    assert data["llm_model"] == "gpt-4o"
    assert data["vector_top_k"] == 3
    assert data["vector_min_sim"] == 0.3
    # 部分更新 + None 恢复默认（未重置的保持）
    data = client.put(
        "/api/settings", json={"vector_top_k": None, "llm_model": None, "llm_provider": None}
    ).json()
    assert data["vector_top_k"] == config.VECTOR_TOP_K
    assert data["llm_model"] == config.LLM_MODEL
    assert data["llm_provider"] == config.LLM_PROVIDER
    assert data["default_category"] == "工作"
    assert data["weekly_llm"] is False


def test_put_settings_validation(client):
    cases = [
        {"nope": 1},  # 未知键
        {"default_category": "不存在的分类"},
        {"llm_provider": "不存在的提供商"},
        {"vector_top_k": 0},
        {"vector_top_k": "abc"},
        {"vector_min_sim": 2},
        {"weekly_llm": "yes"},
        {"llm_model": ""},
        {"llm_model": "x" * 101},
    ]
    for body in cases:
        resp = client.put("/api/settings", json=body)
        assert resp.status_code == 422, f"应 422: {body}"


# ---------------------------------------------------------------------------
# 每周总结开关
# ---------------------------------------------------------------------------


def test_weekly_llm_toggle(client, llm_ok, monkeypatch):
    calls = []

    def fake_weekly(notes):
        calls.append(notes)
        return "本周共记录 N 条笔记（LLM 生成）。"

    monkeypatch.setattr(llm, "weekly_summary", fake_weekly)
    # 关闭 → 不调 LLM，纯统计文本
    client.put("/api/settings", json={"weekly_llm": False})
    data = client.post("/api/weekly").json()
    assert data["llm"] is False
    assert data["degraded"] is False
    assert data["summary"].startswith("本周共记录")
    assert calls == []
    # 恢复 → 走 LLM
    client.put("/api/settings", json={"weekly_llm": True})
    data = client.post("/api/weekly").json()
    assert data["llm"] is True
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 默认分类（直存笔记兜底）
# ---------------------------------------------------------------------------


def test_default_category_on_direct_save(client, llm_down):
    client.put("/api/settings", json={"default_category": "工作"})
    resp = client.post("/api/notes", json={"raw": "今天开会定了三件事", "kind": "note"})
    assert resp.status_code == 202
    note = client.get(f"/api/notes/{resp.json()['note_id']}").json()["note"]
    assert note["category"] == "工作"
    assert note["status"] == "pending"  # LLM 补整理前不改变状态


def test_default_category_on_degraded_confirm(client, llm_down):
    client.put("/api/settings", json={"default_category": "生活"})
    conv_id = start_conversation(client, "周末去买菜")
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    assert resp.status_code == 200
    note = resp.json()["note"]
    assert note["category"] == "生活"
    assert note["status"] == "pending"


def test_default_category_reset(client, llm_down):
    client.put("/api/settings", json={"default_category": "工作"})
    client.put("/api/settings", json={"default_category": ""})  # 恢复"不启用"
    resp = client.post("/api/notes", json={"raw": "随手记", "kind": "note"})
    note = client.get(f"/api/notes/{resp.json()['note_id']}").json()["note"]
    assert note["category"] is None


def test_organized_note_not_affected_by_default_category(client, llm_ok):
    """LLM 正常整理时分类以 LLM 为准，默认分类只兜底直存。"""
    client.put("/api/settings", json={"default_category": "工作"})
    conv_id = start_conversation(client, "nginx 上传大文件限制")
    resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
    note = resp.json()["note"]
    assert note["category"] == "技术"  # llm_ok 固定整理 JSON 的分类


# ---------------------------------------------------------------------------
# 模型 / 检索参数生效
# ---------------------------------------------------------------------------


def test_model_override_resolution(client):
    client.put("/api/settings", json={"llm_model": "deepseek-reasoner"})
    assert settings.resolve_str(settings.KEY_LLM_MODEL, config.LLM_MODEL) == "deepseek-reasoner"
    client.put("/api/settings", json={"llm_model": None})
    assert settings.resolve_str(settings.KEY_LLM_MODEL, config.LLM_MODEL) == config.LLM_MODEL


def test_llm_provider_resolution(client, monkeypatch):
    """llm_provider 覆盖后，llm 层实际按新提供商取 base_url 与 key（§29）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    client.put("/api/settings", json={"llm_provider": "openai"})
    assert settings.resolve_str(settings.KEY_LLM_PROVIDER, config.LLM_PROVIDER) == "openai"
    p = llm._llm_provider()
    assert p.id == "openai"
    assert p.base_url == "https://api.openai.com/v1"
    assert providers.api_key(p) == "sk-openai-test"
    # 恢复默认回落 DeepSeek
    client.put("/api/settings", json={"llm_provider": None})
    assert llm._llm_provider().id == config.LLM_PROVIDER


def test_llm_provider_missing_key_error(client, monkeypatch):
    """切换提供商但 .env 无对应 key 时，报错信息指出缺失的环境变量名（不暴露任何值）。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client.put("/api/settings", json={"llm_provider": "openai"})
    with pytest.raises(llm.LLMError) as ei:
        llm._call_chat([{"role": "user", "content": "hi"}])
    assert "OPENAI_API_KEY" in str(ei.value)


def test_settings_models_endpoint(client, monkeypatch):
    """GET /api/settings/models：成功 source=api；失败回落内置列表 source=fallback；未知提供商 422。"""
    monkeypatch.setattr(providers, "fetch_models", lambda pid: ["m-a", "m-b"])
    data = client.get("/api/settings/models?provider=openai").json()
    assert data["provider"] == "openai"
    assert data["provider_name"] == "OpenAI"
    assert data["models"] == ["m-a", "m-b"]
    assert data["source"] == "api"

    def boom(pid):
        raise providers.ProviderError("无 key")

    monkeypatch.setattr(providers, "fetch_models", boom)
    data = client.get("/api/settings/models?provider=zhipu").json()
    assert data["source"] == "fallback"
    assert data["models"] == list(providers.get("zhipu").fallback_models)
    assert "无 key" in data["detail"]

    assert client.get("/api/settings/models?provider=nope").status_code == 422


def test_retrieval_params_override(client, llm_ok, db_path, monkeypatch):
    """检索参数覆盖后，向量/FTS 召回的 top_k 实际使用设置值。"""
    resp = client.post("/api/notes", json={"raw": "nginx 上传大文件限制", "kind": "note"})
    note_id = resp.json()["note_id"]
    wait_for(lambda: _has_embedding(db_path, note_id), desc="补算 embedding")

    captured = {}
    orig_cosine = embedding.cosine_top_k

    def spy_cosine(qvec, vectors, top_k, **kw):
        captured["vector_k"] = top_k
        return orig_cosine(qvec, vectors, top_k, **kw)

    orig_fts = retrieval.fts_search

    def spy_fts(conn, query, top_k):
        captured["fts_k"] = top_k
        return orig_fts(conn, query, top_k)

    monkeypatch.setattr(embedding, "cosine_top_k", spy_cosine)
    monkeypatch.setattr(retrieval, "fts_search", spy_fts)
    client.put("/api/settings", json={"vector_top_k": 3, "fts_top_k": 2})
    resp = client.post("/api/ask", json={"question": "nginx 上传"})
    assert resp.status_code == 200
    assert captured.get("vector_k") == 3, f"向量 Top-K 未生效: {captured}"
    assert captured.get("fts_k") == 2, f"FTS Top-K 未生效: {captured}"


# ---------------------------------------------------------------------------
# 数据管理
# ---------------------------------------------------------------------------


def test_clear_search_history(client, llm_ok):
    assert client.post("/api/ask", json={"question": "nginx 上传"}).status_code == 200
    assert len(client.get("/api/search-history").json()["items"]) == 1
    data = client.post("/api/settings/clear-search-history").json()
    assert data["deleted"] == 1
    assert client.get("/api/search-history").json()["items"] == []


def test_failed_notes_list_and_retry(client, llm_ok, conn, db_path):
    resp = client.post("/api/notes", json={"raw": "会失败的笔记", "kind": "note"})
    note_id = resp.json()["note_id"]
    # 直接置 failed（真实场景是补处理重试 5 次耗尽，测试不等 6 小时退避）
    conn.execute("UPDATE notes SET status='failed' WHERE id=?", (note_id,))
    conn.commit()
    items = client.get("/api/settings/failed-notes").json()["items"]
    assert any(n["id"] == note_id for n in items), "failed 笔记应出现在列表"
    # 重试：复用 /api/notes/{id}/reprocess → pending → 队列补整理完成
    resp = client.post(f"/api/notes/{note_id}/reprocess")
    assert resp.status_code == 200
    wait_for(lambda: _status(db_path, note_id) == "processed", desc="重试后补整理完成")
    assert client.get("/api/settings/failed-notes").json()["items"] == []


def _status(path, note_id):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT status FROM notes WHERE id = ?", (note_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 修改登录密码（settings 表哈希覆盖 .env AUTH_PASSWORD）
# ---------------------------------------------------------------------------


def _csrf_from_page(client) -> str:
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "页面必须注入 csrf-token meta"
    return m.group(1)


def test_change_password(client, auth_on):
    assert client.post("/api/login", json={"password": "secret"}).status_code == 200
    csrf = _csrf_from_page(client)

    def post_pwd(old, new):
        return client.post(
            "/api/settings/password",
            json={"old_password": old, "new_password": new},
            headers={"X-CSRF-Token": csrf},
        )

    # 当前密码错误 → 401；新密码过短 → 422
    assert post_pwd("wrong", "newpass123").status_code == 401
    assert post_pwd("secret", "short").status_code == 422
    # 成功修改
    assert post_pwd("secret", "newpass123").status_code == 200
    assert client.get("/api/settings").json()["password_set"] is True
    # 新密码可登录；旧密码（.env AUTH_PASSWORD）失效——DB 哈希优先
    client.post("/api/logout", headers={"X-CSRF-Token": csrf})
    assert client.post("/api/login", json={"password": "newpass123"}).status_code == 200
    assert client.post("/api/login", json={"password": "secret"}).status_code == 401


def test_change_password_requires_login(client, auth_on):
    """未登录调改密码 → 401（走 ApiAuthDep）。"""
    resp = client.post(
        "/api/settings/password", json={"old_password": "secret", "new_password": "newpass123"}
    )
    assert resp.status_code == 401
