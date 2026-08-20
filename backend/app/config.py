"""note-brain 配置层。

设计文档 §3.4「配置层清单」：把散落在各章节的配置项收集到一处。
所有配置项均可被同名环境变量覆盖（.env 由 python-dotenv 加载）。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根（backend/ 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 加载 .env（key 只存在家 PC，不入库不进 git，设计文档 §9）
load_dotenv(BASE_DIR / ".env")


# ---------------------------------------------------------------------------
# 版本信息（语义化版本；发版流程：bump APP_VERSION → CHANGELOG.md 追加 → git tag vX.Y.Z）
# ---------------------------------------------------------------------------
APP_NAME = "note-brain"
APP_VERSION = "0.7.1"  # 与最近的 vX.Y.Z tag 对应；发新版本时 bump


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# 数据库（设计文档 §4；拍板：note-brain/data/note-brain.db，不入库）
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "note-brain.db")))

# ---------------------------------------------------------------------------
# DeepSeek（设计文档 §6；chat-completions 基址）
# ---------------------------------------------------------------------------
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
LLM_PROVIDER = os.getenv(
    "LLM_PROVIDER", "deepseek"
)  # 对话/整理模型提供商（§29：providers.py 注册表）
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")  # §6.1 预设
LLM_TIMEOUT = _env_int("LLM_TIMEOUT", 60)  # 秒；对话/整理调用
LLM_MAX_RETRIES = 1  # §6.3：校验失败自动重试 1 次

# ---------------------------------------------------------------------------
# 分类体系（设计文档 §6.2；可配，8 类）
# ---------------------------------------------------------------------------
CATEGORIES: dict[str, str] = {
    "技术": "编程、工具、系统、网络相关",
    "工作": "职业、项目、会议、同事相关",
    "学习": "课程、读书、技能提升相关",
    "生活": "日常琐事、购物、家居、出行相关",
    "健康": "身体、锻炼、饮食、就医相关",
    "财务": "收支、理财、报销相关",
    "灵感": "值得记下来的点子、想法",
    "其他": "不属于以上分类的内容",
}

# ---------------------------------------------------------------------------
# 记录对话与整理（设计文档 §4.3 / §6.4）
# ---------------------------------------------------------------------------
# 链接正文抓取（§6.6）：进 LLM 前与落库共用同一份截断文本
FETCH_TEXT_LIMIT = _env_int("FETCH_TEXT_LIMIT", 20_000)  # 20KB
FETCH_TIMEOUT = _env_int("FETCH_TIMEOUT", 10)  # 秒
FETCH_MAX_BODY = _env_int("FETCH_MAX_BODY", 2 * 1024 * 1024)  # 2MB

# 材料（search_result 摘要 / fetched_page 正文）防御性上限，默认同 fetch_text_limit
MATERIAL_TEXT_LIMIT = _env_int("MATERIAL_TEXT_LIMIT", FETCH_TEXT_LIMIT)

# ---------------------------------------------------------------------------
# 联网搜索（设计文档 §6.5 / §22：DeepSeek 原生 web_search，Anthropic 兼容端点）
# ---------------------------------------------------------------------------
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic/v1")
SEARCH_MODEL = os.getenv("SEARCH_MODEL", LLM_MODEL)  # §22.4 #1：搜索沿用 deepseek-chat
SEARCH_MAX_RESULTS = _env_int("SEARCH_MAX_RESULTS", 8)  # 去重截断后保留条数
SEARCH_MAX_USES = _env_int("SEARCH_MAX_USES", 5)  # §22.4 #3：服务端不完全按声明，靠去重截断兜底
SEARCH_MAX_TOKENS = _env_int("SEARCH_MAX_TOKENS", 4096)
SEARCH_TIMEOUT = _env_int("SEARCH_TIMEOUT", 60)  # 搜索=一次完整模型轮次，比普通调用慢
# 搜索触发意图词（§6.5：用户明确要求才搜；命中即触发一次原生搜索）
SEARCH_TRIGGER_WORDS = (
    "查一下",
    "查一查",
    "搜一下",
    "搜一搜",
    "帮我查",
    "帮我搜",
    "搜索",
    "查查",
    "搜搜",
    "找找",
    "上网查",
    "查资料",
)

# ---------------------------------------------------------------------------
# 模型侧 web_fetch 工具（设计文档 §6.6 / §22.3：LLM 主动调用抓取，工具循环）
# ---------------------------------------------------------------------------
WEB_FETCH_TOOL_MAX_ROUNDS = _env_int("WEB_FETCH_TOOL_MAX_ROUNDS", 3)  # 单轮对话内工具循环上限

# ---------------------------------------------------------------------------
# Ollama embedding（设计文档 §6.1 / §14 第 7、8 条；M2 接入）
# ---------------------------------------------------------------------------
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")  # §10：只绑回环
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")  # 1024 维
EMBED_TIMEOUT = _env_int("EMBED_TIMEOUT", 30)  # 秒
EMBED_TEXT_LIMIT = _env_int("EMBED_TEXT_LIMIT", 6000)  # 单条笔记向量化文本上限（字符）

# ---------------------------------------------------------------------------
# 查重（设计文档 §6.2；M1 FTS 近似版，M2 升级向量版，Ollama 不可用退 FTS）
# ---------------------------------------------------------------------------
DEDUP_VECTOR_TOP_K = _env_int("DEDUP_VECTOR_TOP_K", 3)  # 向量召回候选数
DEDUP_FTS_TOP_K = _env_int("DEDUP_FTS_TOP_K", 3)  # FTS 召回候选数（降级路径）
DEDUP_QUERY_MAX_TERMS = _env_int("DEDUP_QUERY_MAX_TERMS", 8)  # 从新笔记提取的查询词上限

# ---------------------------------------------------------------------------
# 检索与问答（设计文档 §7）
# ---------------------------------------------------------------------------
VECTOR_TOP_K = _env_int("VECTOR_TOP_K", 8)  # 向量召回 Top-K
FTS_TOP_K = _env_int("FTS_TOP_K", 5)  # FTS 关键词召回 Top-K
RRF_K = _env_int("RRF_K", 60)  # RRF 融合常数（§7：score=Σ1/(k+rank)）
ASK_TOP_N = _env_int("ASK_TOP_N", 6)  # RRF 融合后取 Top-N 进 prompt
VECTOR_MIN_SIM = float(
    os.getenv("VECTOR_MIN_SIM", "0.4")
)  # Top-1 向量相似度阈值（§7：低于则视为召回不足）
MATERIALS_TOP_K = _env_int("MATERIALS_TOP_K", 5)  # 材料层兜底召回条数
SEARCH_HISTORY_LIMIT = _env_int("SEARCH_HISTORY_LIMIT", 50)  # 检索记录存储上限（条，超出删最早）

# ---------------------------------------------------------------------------
# 异步补做队列（设计文档 §5 / §14 第 5 条）
# ---------------------------------------------------------------------------
# 补处理失败指数退避（秒）：1m/5m/15m/1h/6h，最多 5 次；仍失败标 failed
BACKOFF_SCHEDULE = [60, 300, 900, 3600, 21600]

# ---------------------------------------------------------------------------
# 登录（设计文档 §9 + M3 拍板 §24）：.env 配 AUTH_PASSWORD 即全局启用（页面跳登录、API 401）
# ---------------------------------------------------------------------------
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "")  # 明文密码，启动时 argon2 哈希化；空 = 不启用登录
AUTH_SESSION_DAYS = _env_int("AUTH_SESSION_DAYS", 30)  # 会话有效期
AUTH_COOKIE_NAME = "NB_SESSION"
AUTH_COOKIE_SECURE = (
    os.getenv("AUTH_COOKIE_SECURE", "") == "1"
)  # 部署 HTTPS 时置 1（本地 http 必须关）
LOGIN_FAIL_LIMIT = _env_int("LOGIN_FAIL_LIMIT", 5)  # 窗口内失败次数阈值（§9：5 次/分钟锁 15 分钟）
LOGIN_FAIL_WINDOW = _env_int("LOGIN_FAIL_WINDOW", 60)  # 失败计数窗口（秒）
LOGIN_LOCK_SECONDS = _env_int("LOGIN_LOCK_SECONDS", 900)  # 锁定持续（秒 = 15 分钟）


def auth_enabled() -> bool:
    """登录是否启用：配置了 AUTH_PASSWORD 即启用（§24 拍板）。"""
    return bool(AUTH_PASSWORD)


# ---------------------------------------------------------------------------
# Web（设计文档 §8）
# ---------------------------------------------------------------------------
NOTES_PAGE_SIZE = _env_int("NOTES_PAGE_SIZE", 20)
STATS_TOP_TAGS = _env_int("STATS_TOP_TAGS", 15)  # 统计页标签 Top-N
