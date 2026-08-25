"""安全加固测试（M4 部署：docs 关闭、安全响应头、改密码吊销其他会话）。

- /docs、/redoc、/openapi.json 已关闭：公网不暴露 API 形状（M4 部署加固）；
- 安全响应头：X-Content-Type-Options / X-Frame-Options / CSP(frame-ancestors) /
  Referrer-Policy 全响应下发；HSTS 仅在 HTTPS 场景（CF 边缘经 X-Forwarded-Proto 透传）下发；
- 修改密码：当前会话保留，其他所有会话立即失效（「登出其他设备」语义）。
"""

import re

import pytest
from app import auth, config


@pytest.fixture
def auth_on(monkeypatch):
    """启用登录（配 AUTH_PASSWORD）+ 重置 argon2 哈希缓存（模块级缓存跨测试会串）。"""
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    monkeypatch.setattr(auth, "_password_hash_cache", None)
    return "secret"


# ---------------------------------------------------------------------------
# /docs 与 /openapi.json 关闭
# ---------------------------------------------------------------------------


def test_docs_and_openapi_disabled(client):
    """/docs 与 /openapi.json 关闭：公网不暴露 API 形状（仅 API 形状，非数据，但没必要暴露）。"""
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# ---------------------------------------------------------------------------
# 安全响应头
# ---------------------------------------------------------------------------


def test_security_headers_on_pages(client):
    """页面响应带安全头：nosniff、禁止 iframe、frame-ancestors 无、Referrer-Policy。"""
    resp = client.get("/")
    assert resp.status_code == 200
    h = resp.headers
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in h.get("content-security-policy", "")
    assert h.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_security_headers_on_api(client):
    """API 响应同样带安全头（中间件全响应生效，不只页面）。"""
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in resp.headers.get("content-security-policy", "")


def test_hsts_only_when_https(client):
    """HSTS 只在 HTTPS 场景下发：本地 http 不下发；CF 边缘透传 X-Forwarded-Proto: https 时下发。"""
    assert "strict-transport-security" not in client.get("/").headers
    resp = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert resp.headers.get("strict-transport-security", "").startswith("max-age=")


# ---------------------------------------------------------------------------
# 改密码吊销其他会话
# ---------------------------------------------------------------------------


def _csrf_from_page(client) -> str:
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "页面必须注入 csrf-token meta"
    return m.group(1)


def test_change_password_invalidates_other_sessions(client, auth_on):
    """改密码吊销其他会话：模拟两设备各持一个登录会话，改密后 B 失效、当前 A 保留。"""
    assert client.post("/api/login", json={"password": "secret"}).status_code == 200
    cookie_a = client.cookies.get(config.AUTH_COOKIE_NAME)
    assert cookie_a, "必须下发 session cookie"
    # 第二台「设备」登录 → 会话 B（cookie jar 只留一个，取出后切回 A）
    assert client.post("/api/login", json={"password": "secret"}).status_code == 200
    cookie_b = client.cookies.get(config.AUTH_COOKIE_NAME)
    assert cookie_b and cookie_b != cookie_a, "两次登录应产生不同会话"

    client.cookies.set(config.AUTH_COOKIE_NAME, cookie_a)
    csrf = _csrf_from_page(client)
    resp = client.post(
        "/api/settings/password",
        json={"old_password": "secret", "new_password": "newpass123"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    # 当前会话 A 保留（换密码请求本身不被登出）；其他会话 B 已被吊销
    assert client.get("/api/notes").status_code == 200
    client.cookies.set(config.AUTH_COOKIE_NAME, cookie_b)
    assert client.get("/api/notes").status_code == 401, "其他会话应被吊销"

    # 新密码可登录；旧密码失效（DB 哈希优先 + 会话已清）
    client.cookies.clear()
    assert client.post("/api/login", json={"password": "newpass123"}).status_code == 200
    assert client.post("/api/login", json={"password": "secret"}).status_code == 401
