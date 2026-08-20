"""DeepSeek 原生联网搜索冒烟脚本（设计文档 §6.5 / §22.1，开工准备清单 §四）。

验证点：
1. DEEPSEEK_API_KEY 在 Anthropic 兼容端点（api.deepseek.com/anthropic/v1）可用；
2. 指定模型是否支持 web_search_20250305 服务端工具（模型名核对，§22.1 #4）；
3. 返回结构：web_search_tool_result 块 + text 块 citations[] 摘录（DSH provider.ts 假设）；
4. 无结果块的报错路径。

用法：
    python scripts/smoke_deepseek_search.py [--model deepseek-chat] [--query "测试查询"]
不打印 API key。
"""

import argparse
import os
import sys
import time

import httpx
from dotenv import load_dotenv

# Windows 管道重定向下保证 UTF-8 输出（避免 GBK 乱码）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "https://api.deepseek.com/anthropic/v1"
API_VERSION = "2023-06-01"
MAX_TOKENS = 4096
MAX_USES = 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model", default="deepseek-chat", help="Anthropic 格式模型名（默认 deepseek-chat）"
    )
    parser.add_argument(
        "--query",
        default="DeepSeek 官方 API 联网搜索 web_search_20250305 支持哪些模型",
        help="搜索查询词",
    )
    parser.add_argument(
        "--env",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        help=".env 文件路径（默认项目根目录）",
    )
    args = parser.parse_args()

    load_dotenv(args.env)
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print(f"[FAIL] .env 中未找到非空 DEEPSEEK_API_KEY（{args.env}）")
        return 2

    endpoint = f"{BASE_URL}/messages"
    body = {
        "model": args.model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Perform a web search for the query: {args.query}"}
                ],
            }
        ],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_USES}],
    }
    headers = {
        "x-api-key": api_key,
        "authorization": f"Bearer {api_key}",
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
        "accept": "application/json",
    }
    print(f"[*] model={args.model}  endpoint={endpoint}")
    print(f"[*] query={args.query}  max_uses={MAX_USES}  max_tokens={MAX_TOKENS}")
    t0 = time.time()

    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(endpoint, headers=headers, json=body)
    except httpx.HTTPError as exc:
        print(f"[FAIL] 请求异常: {exc}")
        return 1

    elapsed = time.time() - t0
    print(f"[*] HTTP {resp.status_code}  ({elapsed:.1f}s)")

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:500]
        print(f"[FAIL] 非 200 响应: {detail}")
        return 1

    try:
        payload = resp.json()
    except ValueError:
        print(f"[FAIL] 响应体不是合法 JSON: {resp.text[:500]}")
        return 1

    blocks = payload.get("content", [])
    result_blocks = [b for b in blocks if b.get("type") == "web_search_tool_result"]
    print(f"[*] content 块数={len(blocks)}  web_search_tool_result 块数={len(result_blocks)}")

    if not result_blocks:
        # 无结果块 = 搜索失败（DSH 严格模式：报错而非从文本抓 URL）
        text_blocks = [b for b in blocks if b.get("type") == "text"]
        for tb in text_blocks:
            print(f"[*] text 块摘录: {tb.get('text', '')[:300]!r}")
        print("[FAIL] 响应中没有 web_search_tool_result 块——该模型可能不支持原生搜索，或搜索未触发")
        return 1

    # 解析 web_search_result 项（url/title/page_age）
    sources = []
    for rb in result_blocks:
        for item in rb.get("content", []):
            if item.get("type") == "web_search_result" and item.get("url"):
                sources.append(item)

    # 摘录取 text 块 citations[]（按 URL 关联，DSH citationSnippets 逻辑）
    citations = {}
    for tb in blocks:
        if tb.get("type") != "text":
            continue
        for cite in tb.get("citations", []) or []:
            url = cite.get("url") or ""
            text = cite.get("cited_text") or ""
            if url and text and url not in citations:
                citations[url] = text

    print(f"[*] 去重前 web_search_result 项={len(sources)}  摘录数={len(citations)}")
    seen = set()
    shown = 0
    for item in sources:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        title = item.get("title") or "(无标题)"
        page_age = item.get("page_age") or ""
        snippet = citations.get(url) or "(无摘录)"
        print(f"\n  - {title}")
        print(f"    url: {url}")
        if page_age:
            print(f"    page_age: {page_age}")
        print(f"    snippet: {snippet[:200]}")
        shown += 1
        if shown >= 8:
            break

    # 失败路径检查：模拟错误模型名（可选，默认跳过）
    print(
        f"\n[OK] 冒烟通过：{args.model} 在 Anthropic 兼容端点触发原生搜索，去重后 {len(seen)} 条结果"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
