"""GET /api/version：版本探活端点（免登录，登录启用时也不拦截——部署探活/确认线上版本用）。"""

import pytest
from app import auth, config


@pytest.fixture
def auth_on(monkeypatch):
    """启用登录（配 AUTH_PASSWORD）+ 重置 argon2 哈希缓存（模块级缓存跨测试会串，同 test_auth）。"""
    monkeypatch.setattr(config, "AUTH_PASSWORD", "secret")
    monkeypatch.setattr(auth, "_password_hash_cache", None)
    return "secret"


def test_version_endpoint_public(client):
    """免登录可访问，返回应用名与当前版本号（= config.APP_VERSION）。"""
    resp = client.get("/api/version")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "note-brain"
    assert data["version"] == config.APP_VERSION


def test_version_endpoint_accessible_with_auth_on(client, auth_on):
    """登录启用时版本端点仍开放（探活不依赖登录态，业务 API 仍被拦截）。"""
    assert client.get("/api/notes").status_code == 401  # 对照组：业务 API 已拦截
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json()["version"] == config.APP_VERSION
