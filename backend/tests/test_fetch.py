"""链接抓取安全与提取测试（设计文档 §6.6 / §9：SSRF 防护、降级、截断）。

网络相关（真实 DNS/HTTP）不在测试范围——SSRF 校验发生在任何网络动作之前，可纯本地验证。
"""

import pytest
from app import fetch
from app.api import extract_urls

# ---------------------------------------------------------------------------
# SSRF 防护（无网络动作）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
def test_reject_non_http_scheme(url):
    with pytest.raises(fetch.FetchError):
        fetch.fetch_page(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/",
        "http://localhost:9/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # 云元数据经典攻击面
        "http://[::1]:8000/",
    ],
)
def test_reject_private_and_loopback(url):
    with pytest.raises(fetch.FetchError):
        fetch.fetch_page(url)


# ---------------------------------------------------------------------------
# URL 提取
# ---------------------------------------------------------------------------


def test_extract_urls():
    text = "看 https://example.com/a?b=1 和 http://x.cn/2，以及末尾的 https://a.b/c。"
    urls = extract_urls(text)
    assert urls == ["https://example.com/a?b=1", "http://x.cn/2", "https://a.b/c"]


def test_extract_urls_dedup():
    assert extract_urls("https://a.b/1 https://a.b/1") == ["https://a.b/1"]


# ---------------------------------------------------------------------------
# HTML → markdown 提取（无网络）
# ---------------------------------------------------------------------------


def test_extract_strips_script_style_noscript():
    html = """<html><head><title>测试标题</title></head><body>
    <script>alert('xss')</script><style>body{color:red}</style><noscript>需要JS</noscript>
    <p>正文第一句。</p><p>正文第二句。</p></body></html>"""
    md, title = fetch._extract(html, "https://x.com")
    assert title == "测试标题"
    assert "xss" not in md and "color:red" not in md and "需要JS" not in md
    assert "正文第一句" in md and "正文第二句" in md


def test_extract_falls_back_to_metadata_when_no_body():
    """视频页/无正文页面：降级为标题 + 元数据（设计文档 §6.6）。"""
    html = """<html><head><title>B站某视频标题</title>
    <meta property="og:description" content="视频简介文字">
    </head><body><div id="app"></div></body></html>"""
    md, _ = fetch._extract(html, "https://b23.tv/abc")
    assert "B站某视频标题" in md
    assert "视频简介文字" in md


def test_fetched_message_format():
    """输出格式对齐 DSH：Fetched <url> (HTTP <n>) 头 + 正文 + 截断提示。"""
    result = fetch.FetchResult(
        url="https://a.b/c",
        status=200,
        title="t",
        markdown="x" * (fetch.config.FETCH_TEXT_LIMIT + 100),
        truncated=True,
    )
    msg = fetch.fetched_message(result)
    assert msg["content"].startswith("Fetched https://a.b/c (HTTP 200)")
    assert "已截断" in msg["content"]
