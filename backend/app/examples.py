"""输入框随机示例占位文案（§8 Web 页面）。

每次渲染记录页/问答页时从 data/examples_{kind}.txt 随机挑一条作为输入框
placeholder（刷新即换）；文件缺失、为空或读取失败时回退内置静态文案。

示例文件约定：每行一条，`#` 开头为注释行，UTF-8 编码；改文件立即生效（每次调用实时读）。
"""

from __future__ import annotations

import random
from pathlib import Path

_EXAMPLES_DIR = Path(__file__).resolve().parent / "data"

# 回退文案：示例文件不可用时与原来的静态 placeholder 完全一致
_FALLBACK: dict[str, str] = {
    "record": "例如：今天发现 nginx client_max_body_size 默认 1M，上传大文件被拒，帮我记下 https://example.com/…",
    "ask": "例如：我上次记的那个上传文件的坑是啥？",
}


def _load(kind: str) -> list[str]:
    """读取某类示例（每行一条，跳过空行与 # 注释行）；文件缺失/读取失败返回空列表。"""
    path = _EXAMPLES_DIR / f"examples_{kind}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def random_example(kind: str) -> str:
    """随机返回一条示例（实时读文件）；无有效示例时回退内置文案。"""
    items = _load(kind)
    return random.choice(items) if items else _FALLBACK[kind]
