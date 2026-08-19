"""登录测试（设计文档 §9 + M3 拍板 §24：AUTH_PASSWORD 配置即全局启用）。

- 未配置密码 → 登录关闭，页面/API 免鉴权（本地开发/既有 66 测试不受影响）；
- 配置密码 → 页面未登录重定向 /login、API 未登录 401；登录成功 Set-Cookie 后放行；
- 失败限速：窗口内失败达阈值 → 429 锁定（落 SQLite，重启不失效）；
- CSRF：登录后非安全方法须带 X-CSRF-Token，缺失/不符 → 403；
- 登出：删 session + 清 cookie。
"""
import pytest

from app import auth, config


@pytest.fixture
def auth_on(monkeypatch):
    """启用登录（配 AUTH_PASSWORD）+ 重置 argon2 哈希缓存（模块级缓存跨测试会串）。"""
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    monkeypatch.setattr(auth, "_password_hash_cache", None)
    return "secret"


def _login(client, password: str, **kw):
    return client.post("/api/login", json={"password": password}, **kw)


# ---------------------------------------------------------------------------
# 关闭态（默认）
# ---------------------------------------------------------------------------

def test_login_disabled_by_default(client, llm_ok):
    """未配置 AUTH_PASSWORD：登录端点 403，页面与 API 免鉴权可访问。"""
    assert client.post("/api/login", json={"password": "x"}).status_code == 403
    assert client.get("/notes").status_code == 200
    assert client.get("/api/notes").status_code == 200


# ---------------------------------------------------------------------------
# 启用态：鉴权与登录流程
# ---------------------------------------------------------------------------

def test_pages_redirect_and_api_401_when_logged_out(client, auth_on):
    resp = client.get("/notes", follow_redirects=False)
    assert resp.status_code == 303 and "/login" in resp.headers["location"]
    assert client.get("/api/notes").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303  # 页面一律重定向


def test_login_success_sets_cookie_and_grants_access(client, auth_on):
    resp = _login(client, "secret")
    assert resp.status_code == 200
    cookie = resp.cookies.get(config.AUTH_COOKIE_NAME)
    assert cookie, "必须下发 session cookie"
    assert resp.headers.get("set-cookie", "").lower().find("httponly") >= 0
    assert client.get("/api/notes").status_code == 200
    assert client.get("/notes").status_code == 200


def test_login_wrong_password(client, auth_on):
    assert _login(client, "wrong").status_code == 401
    assert client.get("/api/notes").status_code == 401


def test_login_page_renders_and_redirects_when_authed(client, auth_on):
    assert client.get("/login").status_code == 200
    _login(client, "secret")
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/"


def test_logout_invalidates_session(client, auth_on):
    _login(client, "secret")
    # 登出是非安全方法，需要 CSRF 头
    csrf = _csrf_from_page(client)
    resp = client.post("/api/logout", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    assert client.get("/api/notes").status_code == 401


# ---------------------------------------------------------------------------
# 失败限速（§9：5 次/分钟锁 15 分钟）
# ---------------------------------------------------------------------------

def test_login_rate_limit_blocks(client, auth_on, db_path, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_FAIL_LIMIT", 3)
    monkeypatch.setattr(config, "LOGIN_FAIL_WINDOW", 3600)
    monkeypatch.setattr(config, "LOGIN_LOCK_SECONDS", 3600)
    for _ in range(3):
        assert _login(client, "wrong").status_code == 401
    resp = _login(client, "secret")
    assert resp.status_code == 429, "锁定期间即使密码正确也拒绝"
    assert "已锁定" in resp.json()["detail"]
    # 失败记录确实落 SQLite（重启不失效的载体是表，不是进程内存）
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM login_failures").fetchone()[0]
    finally:
        conn.close()
    assert n >= 3
    # 锁定状态持续（再次尝试仍 429）
    assert _login(client, "secret").status_code == 429


def test_login_success_clears_failures(client, auth_on, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_FAIL_LIMIT", 3)
    monkeypatch.setattr(config, "LOGIN_FAIL_WINDOW", 3600)
    monkeypatch.setattr(config, "LOGIN_LOCK_SECONDS", 3600)
    _login(client, "wrong")
    _login(client, "wrong")
    assert _login(client, "secret").status_code == 200  # 未达阈值前成功 → 计数清零
    # 再错 3 次不触发锁定（因为已清零）→ 需要 3 次后才锁
    for _ in range(3):
        _login(client, "wrong")
    assert _login(client, "secret").status_code == 429


# ---------------------------------------------------------------------------
# CSRF（§9：非安全方法须带 X-CSRF-Token）
# ---------------------------------------------------------------------------

def _csrf_from_page(client) -> str:
    html = client.get("/").text
    import re
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "页面必须注入 csrf-token meta"
    return m.group(1)


def test_csrf_required_for_mutations(client, auth_on, llm_ok):
    _login(client, "secret")
    csrf = _csrf_from_page(client)
    # 无头 → 403
    resp = client.post("/api/notes", json={"raw": "x", "kind": "note"})
    assert resp.status_code == 403
    # 错误头 → 403
    resp = client.post("/api/notes", json={"raw": "x", "kind": "note"}, headers={"X-CSRF-Token": "bad"})
    assert resp.status_code == 403
    # 正确头 → 放行（202）
    resp = client.post("/api/notes", json={"raw": "x", "kind": "note"}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 202
    # GET 免 CSRF
    assert client.get("/api/notes").status_code == 200
