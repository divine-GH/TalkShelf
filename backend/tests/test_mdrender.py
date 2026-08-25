"""mdrender 单元测试（设计文档 §37：服务端 Markdown 渲染 + XSS 防护）。"""

from __future__ import annotations

from app import mdrender


def test_basic_inline():
    out = mdrender.render_markdown("**加粗** 与 `code` 和 [链接](https://a.com/x)")
    assert "<strong>加粗</strong>" in out
    assert "<code>code</code>" in out
    assert 'href="https://a.com/x"' in out


def test_headings_lists_code_fence_table():
    out = mdrender.render_markdown(
        "# 标题\n\n- 项一\n- 项二\n\n```python\nprint('hi')\n```\n\n"
        "| 列A | 列B |\n| --- | --- |\n| 1 | 2 |"
    )
    assert "<h1>标题</h1>" in out
    assert "<li>项一</li>" in out
    assert "<pre>" in out and "print('hi')" in out
    assert "<table>" in out and "<th>列A</th>" in out


def test_single_newline_br():
    out = mdrender.render_markdown("第一行\n第二行")
    assert "<br" in out


def test_blank_or_none():
    assert mdrender.render_markdown("") == ""
    assert mdrender.render_markdown(None) == ""


def test_raw_html_escaped_as_text():
    out = mdrender.render_markdown("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_javascript_href_neutralized():
    out = mdrender.render_markdown("[点我](javascript:alert(1))")
    assert "javascript:" not in out
    assert 'href="#"' in out


def test_data_href_neutralized():
    out = mdrender.render_markdown("[x](data:text/html;base64,AAA)")
    assert 'href="#"' in out


def test_image_removed():
    out = mdrender.render_markdown("![图](https://x.com/a.png)")
    assert "<img" not in out


def test_http_link_kept_and_scheme_case_insensitive():
    out = mdrender.render_markdown("[A](HTTP://x.com) [B](https://x.com?a=1&b=2)")
    assert 'href="HTTP://x.com"' in out
    assert 'href="https://x.com?a=1' in out


def test_safe_href_whitelist():
    assert mdrender.safe_href("https://a.com") == "https://a.com"
    assert mdrender.safe_href("http://a.com") == "http://a.com"
    assert mdrender.safe_href("mailto:a@b.c") == "mailto:a@b.c"
    assert mdrender.safe_href("/relative") == "/relative"
    assert mdrender.safe_href("#fragment") == "#fragment"
    assert mdrender.safe_href("javascript:alert(1)") is None
    assert mdrender.safe_href("DATA:text/html;base64,AAA") is None
    assert mdrender.safe_href("vbscript:x") is None
    assert mdrender.safe_href("file:///etc/passwd") is None
    assert mdrender.safe_href("") is None
    assert mdrender.safe_href(None) is None
