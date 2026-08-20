"""链接正文抓取（设计文档 §6.6，M1 服务端直抓过渡版，§22.4 #6 定案）。

实现要点：
- 仅允许 http/https；DNS 解析后拒绝内网/回环/链路本地/组播地址；
- 不自动跟随重定向，逐跳手动校验（每跳重新解析 + 校验，最多 2 跳）——防 302 带进内网；
- 连接时按解析后 IP 直连（Host 头 + httpcore 的 sni_hostname 扩展），防 DNS rebinding 绕过；
- 超时 10s + 响应体上限 2MB（流式读取即断）；正文提取后截断到 fetch_text_limit（默认 20KB）；
- HTML→markdown 用 markdownify（bs4 预剥离 script/style/noscript，§22.4 #5 实测对齐 DSH）；
- 正文过短（无正文可抓，如视频页）降级为标题 + 页面元数据；任何失败抛 FetchError，由调用方降级、不阻塞记录。
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import urllib.parse
from dataclasses import dataclass

import httpx
import markdownify
from bs4 import BeautifulSoup

from . import config

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 2
MIN_BODY_CHARS = 50  # 正文少于该字符数视为"无正文"，降级为标题+元数据

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# URL 匹配排除中文/全角标点，避免把后续文本吃进 URL（如 "http://x.cn/2，以及…"）
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；：！？、（）【】《》「」『』]+")


class FetchError(Exception):
    """抓取失败（网络错误/SSRF 拒绝/超时/超限）。调用方降级处理，不阻塞记录。"""


def extract_urls(message: str) -> list[str]:
    """从用户消息提取 http(s) URL（去重）。抓取/快速记录共用。"""
    seen: set[str] = set()
    urls: list[str] = []
    for m in _URL_RE.findall(message):
        url = m.rstrip(".,;:!?)]}")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


@dataclass
class FetchResult:
    url: str  # 最终 URL（跟随跳转后）
    status: int
    title: str | None
    markdown: str  # 截断后的 markdown 正文（不含 Fetched 头）
    truncated: bool


def _is_forbidden(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _resolve_and_pin(url: str) -> tuple[str, str, str]:
    """解析 host → 校验 IP 非内网 → 返回 (IP 直连 URL, Host 头, SNI)。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"仅支持 http/https: {parsed.scheme}")
    host = parsed.hostname
    if not host:
        raise FetchError("URL 缺少主机名")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"DNS 解析失败: {e}") from e
    if not infos:
        raise FetchError("DNS 无解析结果")
    ip = infos[0][4][0].split("%")[0]  # 去掉 IPv6 scope id
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError as e:
        raise FetchError(f"非法 IP: {ip}") from e
    if _is_forbidden(addr):
        raise FetchError(f"拒绝内网/回环/链路本地地址: {ip}")
    ip_fmt = f"[{ip}]" if addr.version == 6 else ip
    host_header = host if port in (80, 443) else f"{host}:{port}"
    ip_url = f"{parsed.scheme}://{ip_fmt}:{port}{parsed.path or '/'}" + (
        f"?{parsed.query}" if parsed.query else ""
    )
    return ip_url, host_header, host


def _fetch_once(client: httpx.Client, url: str) -> httpx.Response:
    ip_url, host_header, sni = _resolve_and_pin(url)
    resp = client.get(
        ip_url,
        headers={"Host": host_header, "User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8"},
        extensions={"sni_hostname": sni},
    )
    return resp


def _read_limited(resp: httpx.Response) -> bytes:
    total = 0
    chunks: list[bytes] = []
    for chunk in resp.iter_bytes():
        total += len(chunk)
        if total > config.FETCH_MAX_BODY:
            raise FetchError("响应体超过上限 2MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _extract(html: str, final_url: str) -> tuple[str, str | None]:
    """HTML → markdown 正文 + 标题。正文过短时降级为标题 + 页面元数据（视频链接场景，§6.6）。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        title = og_title["content"].strip() or title
    md = markdownify.markdownify(str(soup), strip=["img"])
    md = "\n".join(line.rstrip() for line in md.splitlines()).strip()
    if len(md) < MIN_BODY_CHARS:
        # 正文过短（如视频页）：降级为「标题 + 页面元数据 + 原有短正文」合成，不丢弃已有正文（§6.6）
        desc = soup.find("meta", attrs={"name": "description"})
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        desc_text = None
        for cand in (og_desc, desc):
            if cand and cand.get("content"):
                desc_text = cand["content"].strip()
                break
        parts = [title or final_url]
        if desc_text:
            parts.append(desc_text)
        if md:
            parts.append(md)
        md = "\n\n".join(parts)
    return md, title


def fetch_page(url: str) -> FetchResult:
    """抓取链接正文。失败一律抛 FetchError。"""
    current = url.strip()
    with httpx.Client(follow_redirects=False, timeout=config.FETCH_TIMEOUT) as client:
        for hop in range(MAX_REDIRECTS + 1):
            try:
                resp = _fetch_once(client, current)
            except httpx.HTTPError as e:
                raise FetchError(f"请求失败: {e}") from e
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    raise FetchError(f"HTTP {resp.status_code} 无 Location")
                current = urllib.parse.urljoin(current, loc)
                if not urllib.parse.urlsplit(current).scheme in ("http", "https"):
                    raise FetchError("重定向到非 http/https 地址")
                continue  # 逐跳重新解析 + 校验
            break
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}")
    body = _read_limited(resp)
    try:
        html = body.decode("utf-8", errors="replace")
        md, title = _extract(html, current)
    except Exception as e:  # noqa: BLE001 —— 转换失败降级原文（§22.3 健壮性第 2 点）
        logger.warning("HTML→markdown 转换失败，降级原文: %s", e)
        md = body.decode("utf-8", errors="replace")[: config.FETCH_TEXT_LIMIT]
        title = current
    truncated = len(md) > config.FETCH_TEXT_LIMIT
    if truncated:
        md = md[: config.FETCH_TEXT_LIMIT]
    return FetchResult(
        url=current, status=resp.status_code, title=title, markdown=md, truncated=truncated
    )


def fetched_message(result: FetchResult) -> dict:
    """组装注入对话的 fetched_page 消息（对齐 DSH 输出格式：Fetched <url> (HTTP <n>) 头 + 正文 + 截断提示）。"""
    head = f"Fetched {result.url} (HTTP {result.status})"
    body = result.markdown
    if result.truncated:
        body += f"\n\n（正文过长，已截断至前 {config.FETCH_TEXT_LIMIT} 字符）"
    return {
        "role": "assistant",
        "kind": "fetched_page",
        "url": result.url,
        "content": head + "\n\n" + body,
    }
