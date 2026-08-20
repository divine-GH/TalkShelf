"""模型提供商注册表（设置页「对话/整理模型」：模型提供商 + 可选模型列表）。

背景（§29）：
- 全部提供商均为 OpenAI chat-completions 兼容端点（llm.py 只认 /chat/completions，
  与设计文档 §6.1 一致；联网搜索仍是 DeepSeek 专属 Anthropic 兼容端点，见 §22）；
- 可选模型列表：GET {base_url}/models，Authorization: Bearer <key>，
  响应 OpenAI 标准 {"data": [{"id": ...}]}；已实测 DeepSeek /models 可用
  （2026-08-20 返回 deepseek-v4-flash / deepseek-v4-pro），其余提供商同为
  OpenAI 兼容标准端点（无 key 探测均返回 401=端点存在）；
- API Key 只从环境变量（.env 由 config 加载）读取，key 只存在家 PC、不入库不进 git（§9）；
  设置页不提供 key 输入框，其他提供商需在 .env 配置对应 key 才能拉取模型列表并调用。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

MODELS_TIMEOUT = 10  # 模型列表拉取超时（秒）


class ProviderError(Exception):
    """模型列表拉取失败（无 key / 网络 / HTTP 非 200）。调用方回落内置列表。"""


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    base_url: str  # chat-completions 基址（不含 /chat/completions；/models 同基址）
    api_key_env: str  # .env 中的 API Key 变量名
    fallback_models: tuple[str, ...]  # 拉取失败（无 key/网络）时的内置可选模型


# 注册表：id → 提供商。设置页下拉按此顺序渲染，llm.py 按当前 provider 选 base_url + key。
PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        fallback_models=(
            "deepseek-chat",
            "deepseek-reasoner",
            "deepseek-v4-flash",
            "deepseek-v4-pro",
        ),
    ),
    "openai": Provider(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        fallback_models=(
            "gpt-5",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "gpt-4.1-mini",
            "o3",
            "o4-mini",
        ),
    ),
    "openrouter": Provider(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        fallback_models=(
            "openai/gpt-4o",
            "anthropic/claude-sonnet-4.5",
            "deepseek/deepseek-chat-v3-0324",
            "qwen/qwen3-235b-a22b",
        ),
    ),
    "moonshot": Provider(
        id="moonshot",
        name="Moonshot（Kimi）",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        fallback_models=(
            "kimi-k2-0711-preview",
            "kimi-k2-turbo-preview",
            "kimi-latest",
            "moonshot-v1-8k",
            "moonshot-v1-32k",
            "moonshot-v1-128k",
        ),
    ),
    "zhipu": Provider(
        id="zhipu",
        name="智谱（GLM）",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key_env="ZHIPU_API_KEY",
        fallback_models=(
            "glm-4.6",
            "glm-4.5",
            "glm-4.5-air",
            "glm-4-plus",
            "glm-4-air",
            "glm-4-flash",
            "glm-4-long",
        ),
    ),
    "qwen": Provider(
        id="qwen",
        name="通义千问（DashScope）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        fallback_models=(
            "qwen-max",
            "qwen-plus",
            "qwen-turbo",
            "qwen3-max",
            "qwen3-plus",
            "qwen3-turbo",
            "qwen-long",
        ),
    ),
    "siliconflow": Provider(
        id="siliconflow",
        name="SiliconFlow（硅基流动）",
        base_url="https://api.siliconflow.cn/v1",
        api_key_env="SILICONFLOW_API_KEY",
        fallback_models=(
            "deepseek-ai/DeepSeek-V3",
            "deepseek-ai/DeepSeek-R1",
            "Qwen/Qwen3-235B-A22B",
            "Qwen/Qwen3-30B-A3B",
            "Qwen/Qwen2.5-72B-Instruct",
            "THUDM/GLM-4-9B-0414",
        ),
    ),
}


def get(provider_id: str) -> Provider:
    """按 id 取提供商；未知 id 回落 DeepSeek（防御：配置层只下发注册表内 id）。"""
    return PROVIDERS.get(provider_id, PROVIDERS["deepseek"])


def options() -> list[dict]:
    """设置页下拉选项：[{id, name}, ...]（按注册表顺序）。"""
    return [{"id": p.id, "name": p.name} for p in PROVIDERS.values()]


def api_key(provider: Provider) -> str:
    """该提供商的 API Key（环境变量；.env 由 config 启动时加载，key 不入库不进 git）。"""
    return os.getenv(provider.api_key_env, "")


def fetch_models(provider_id: str, timeout: float = MODELS_TIMEOUT) -> list[str]:
    """GET {base_url}/models 拉取模型 id 列表（OpenAI 兼容标准）。失败抛 ProviderError。

    设置页「可选模型」用；调用方（api.py）失败时回落 Provider.fallback_models 并附原因。
    """
    p = get(provider_id)
    if not api_key(p):
        raise ProviderError(f"未配置 API Key（.env 的 {p.api_key_env}）")
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(
                f"{p.base_url}/models",
                headers={"Authorization": f"Bearer {api_key(p)}"},
            )
    except httpx.HTTPError as e:
        raise ProviderError(f"模型列表请求失败: {e}") from e
    if resp.status_code != 200:
        raise ProviderError(f"模型列表 HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise ProviderError("模型列表响应非合法 JSON") from e
    ids = [
        m["id"]
        for m in (data.get("data") or [])
        if isinstance(m, dict) and isinstance(m.get("id"), str)
    ]
    return ids
