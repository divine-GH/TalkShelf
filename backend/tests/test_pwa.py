"""PWA 测试（设计文档 §38）：manifest / service worker / 图标 / 离线页 / 页面接入。

- manifest：正确 MIME（application/manifest+json）+ 安装必需字段（name/icons/start_url/display）；
- service worker：text/javascript + Service-Worker-Allowed: /（全站 scope 必需）+ 缓存离线页；
- 图标：PNG 签名 + MIME（192/512 + iOS 180）；
- 页面：base.html 正确挂 manifest / theme-color / apple-touch-icon / SW 注册脚本。
"""

import pytest


def test_manifest_served(client):
    """/static/manifest.webmanifest：安装必需字段齐全，图标含 192/512。"""
    resp = client.get("/static/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    data = resp.json()
    assert data["name"]
    assert data["short_name"] == "TalkShelf"
    assert data["lang"] == "zh-CN"
    assert data["start_url"] == "/"
    assert data["scope"] == "/"
    assert data["display"] == "standalone"
    assert data["theme_color"] == "#2f6fed"
    sizes = {i["sizes"] for i in data["icons"]}
    assert sizes == {"192x192", "512x512"}
    assert all(i["type"] == "image/png" for i in data["icons"])


def test_service_worker_served(client):
    """/static/sw.js：JS MIME + 全站 scope 头（注册 {scope: '/'} 的硬性要求）。"""
    resp = client.get("/static/sw.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/javascript")
    assert resp.headers.get("service-worker-allowed") == "/"
    assert "offline.html" in resp.text  # 离线兜底页在 SW 内


def test_service_worker_does_not_cache_api_pages(client):
    """SW 策略安全边界：只对 navigate 与 /static/ 两处 respondWith，页面/API 一律网络直行。"""
    sw = client.get("/static/sw.js").text
    assert "url.pathname.startsWith('/static/')" in sw
    assert sw.count("respondWith") == 2  # 仅 navigate 兜底 + static 缓存，无 API/页面分支


def test_offline_page_served(client):
    """/static/offline.html：服务器不可达时的兜底页（无外部依赖，内联样式）。"""
    resp = client.get("/static/offline.html")
    assert resp.status_code == 200
    assert "TalkShelf" in resp.text
    assert "</style>" in resp.text  # 内联样式，离线可用


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("apple-touch-icon.png", 180),
    ],
)
def test_icons_served(client, name, size):
    """图标：有效 PNG（签名 + IHDR 尺寸正确）+ image/png MIME。"""
    resp = client.get(f"/static/icons/{name}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR 第 9~16 字节 = 宽高（大端）；内容非空（实心图压缩后仍 > 300 字节）
    import struct

    w, h = struct.unpack(">II", resp.content[16:24])
    assert (w, h) == (size, size)
    assert len(resp.content) > 300
    assert resp.content[-8:-4] == b"IEND"


def test_index_page_has_pwa_links(client):
    """首页（base.html）接入 PWA：manifest / theme-color / apple-touch-icon / SW 注册。"""
    html = client.get("/").text
    assert 'rel="manifest"' in html
    assert 'name="theme-color"' in html
    assert 'rel="apple-touch-icon"' in html
    assert "navigator.serviceWorker.register" in html
