"""LLM 输出 Markdown → 安全 HTML（展示层渲染，设计文档 §37）。

渲染管线（防 XSS 的关键是顺序）：
1. **先 ``html.escape`` 再交给 python-markdown**——该库默认原样透传块级原始 HTML
   （``<script>`` 会直接进入输出），先转义后原始 HTML 只能作为可见文本显示，不可能执行；
2. 渲染扩展 fenced_code / tables / nl2br（LLM 输出常见形态：代码块、表格、单换行 prose）；
3. 渲染后按协议白名单重写 ``href``（只留 http/https/mailto、``#`` 片段、相对路径），
   ``javascript:`` / ``data:`` 等一律替换为 ``#``；
4. 移除全部 ``<img>``（LLM 输出几乎不含图；防 ``data:``/``file:`` 等危险 src）。

边界（§37「明确不做」）：裸 URL 不自动成链接（python-markdown 无内置 autolink，
DeepSeek 通常输出 ``[标题](url)`` 形式）；用户消息不渲染（保持纯文本 pre-wrap）。
"""

from __future__ import annotations

import html
import re

import markdown as _md

_ALLOWED_HREF_SCHEMES = re.compile(r"^(?:https?|mailto):", re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]*)"')
_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)


def safe_href(url: str | None) -> str | None:
    """URL 协议白名单：合法返回原 URL，非法返回 None（调用方替换为 ``#``）。

    允许：http/https/mailto 协议（忽略大小写）、``#`` 片段、``/`` 开头的相对路径。
    拒绝：javascript:、data:、vbscript:、file:、空值等一切其他形式。
    """
    u = (url or "").strip()
    if not u:
        return None
    if u.startswith(("#", "/")):
        return u
    if _ALLOWED_HREF_SCHEMES.match(u):
        return u
    return None


def _rewrite_href(m: re.Match[str]) -> str:
    value = html.unescape(m.group(1))
    fixed = safe_href(value)
    if fixed is None:
        return 'href="#"'
    return f'href="{html.escape(fixed, quote=True)}"'


def render_markdown(text: str | None) -> str:
    """将 LLM 回复的 Markdown 文本渲染为安全的 HTML（透传描述见模块 docstring）。"""
    escaped = html.escape(text or "", quote=False)
    out = _md.markdown(escaped, extensions=["fenced_code", "tables", "nl2br"])
    out = _IMG_RE.sub("", out)
    out = _HREF_RE.sub(_rewrite_href, out)
    return out
