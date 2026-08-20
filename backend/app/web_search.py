"""DeepSeek 原生联网搜索（设计文档 §6.5 / §22；M2 接入，落地位置按代码布局放 backend/app/）。

要点（§22 实测结论优先于 §22.1 假设）：
- 端点：POST {ANTHROPIC_BASE_URL}/messages（与 chat-completions 的基址不同，§22.1 #1）；
- 头部：x-api-key + authorization: Bearer 都发 + anthropic-version: 2023-06-01（§22.1 #4）；
- 声明 tools:[{type:'web_search_20250305', name:'web_search', max_uses:5}]（§22.4 #3 默认 5）；
- 解析 web_search_tool_result 块内 web_search_result 项（url/title/page_age）；
  §22.4 #2 实测 text 块无 citations、摘录不可用 → snippet 一律省略；
- 按 URL 去重（剥 #fragment，§22.4 #7）+ 截断到 search_max_results；
- 无结果块按搜索失败处理（不从模型 prose 抓 URL，§22.4 #4）；
- 失败抛 SearchError，调用方降级：对话照常、只记日志（§6.5）。
"""

from __future__ import annotations

import logging
import urllib.parse

import httpx

from . import config

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"


class SearchError(Exception):
    """原生搜索失败（网络/HTTP/无结果块/超时）。调用方降级处理，不阻塞对话。"""


def _strip_fragment(url: str) -> str:
    """剥掉 URL 的 #fragment（§22.4 #7：服务端返回偶带 #1，影响去重与入库）。"""
    try:
        return urllib.parse.urldefrag(url).url
    except ValueError:
        return url


def _parse_result_blocks(blocks: list[dict]) -> list[dict]:
    """从 content 块提取 web_search_result 项（url/title/page_age），去 fragment。"""
    items: list[dict] = []
    seen: set[str] = set()
    for block in blocks:
        if block.get("type") != "web_search_tool_result":
            continue
        for item in block.get("content", []) or []:
            if item.get("type") != "web_search_result":
                continue
            url = _strip_fragment(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            items.append(
                {
                    "url": url,
                    "title": (item.get("title") or "").strip() or url,
                    "page_age": (item.get("page_age") or "").strip(),
                }
            )
    return items


def search(query: str) -> list[dict]:
    """执行一次原生搜索，返回去重截断后的 [{url, title, page_age}]。失败抛 SearchError。"""
    query = query.strip()
    if not config.DEEPSEEK_API_KEY:
        raise SearchError("DEEPSEEK_API_KEY 未配置")
    body = {
        "model": config.SEARCH_MODEL,
        "max_tokens": config.SEARCH_MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Perform a web search for the query: {query}"}
                ],
            }
        ],
        "tools": [
            {
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": config.SEARCH_MAX_USES,
            }
        ],
    }
    headers = {
        "x-api-key": config.DEEPSEEK_API_KEY,
        "authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "accept": "application/json",
    }
    try:
        with httpx.Client(timeout=config.SEARCH_TIMEOUT) as client:
            resp = client.post(f"{config.ANTHROPIC_BASE_URL}/messages", headers=headers, json=body)
    except httpx.HTTPError as e:
        raise SearchError(f"搜索请求失败: {e}") from e
    if resp.status_code != 200:
        raise SearchError(f"搜索 HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise SearchError(f"搜索响应非合法 JSON: {e}") from e
    items = _parse_result_blocks(payload.get("content", []) or [])
    if not items:
        # 无结果块 = 搜索失败（§22.4 #4：不从 prose 抓 URL，防幻觉）
        raise SearchError("搜索无结果块（模型未执行搜索）")
    return items[: config.SEARCH_MAX_RESULTS]


def results_to_material(items: list[dict]) -> str:
    """搜索结果 → 注入对话的文本（§22.1 #6：`- [标题](url)`，page_age 非空附日期）。

    摘录不可用（§22.4 #2），snippet 一律省略；不进 raw，作为 search_result 材料。
    这里再剥一次 fragment（解析路径与 mock/直传路径都保证干净，§22.4 #7）。
    """
    lines = []
    for it in items:
        url = _strip_fragment(it["url"])
        line = f"- [{it['title']}]({url})"
        if it.get("page_age"):
            line += f"（{it['page_age']}）"
        lines.append(line)
    return "\n".join(lines)


def should_search(message: str) -> bool:
    """搜索触发检测（§6.5：用户明确要求才搜；命中任一意图词即触发）。"""
    return any(w in message for w in config.SEARCH_TRIGGER_WORDS)
