# TalkShelf

个人知识速记工具：记录零负担，整理交给 AI，查找用对话代替翻笔记。

<!-- 徽章：把 CI 徽章里的 OWNER/REPO 换成你的 GitHub 仓库路径（如 divine-GH/TalkShelf） -->
![CI](https://github.com/divine-GH/TalkShelf/actions/workflows/ci.yml/badge.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Ruff](https://img.shields.io/badge/code_style-ruff-black.svg)

设计唯一事实源：[设计文档.md](设计文档.md)（§1~§35）；里程碑 M1→M4 见 §12。

> 本项目是**本地单用户**应用：数据存本地 SQLite，运行即 `uvicorn --workers 1`
> （任务队列在进程内存，多 worker 会重复处理 pending，见设计文档 §5）。请勿当作多租户服务使用。

## 声明（作者与 AI 生成）

这是我（divine-GH）的**第一个项目**——从 0 到能跑通，是我做出「要不要留、往哪走」这些决策、并把体验调到顺手的成果。
项目的**代码与文档均由 AI 生成**（模型：DeepSeek V4 Flash 0731），我主要负责**决策**：定方向、拍板方案、验证效果、把控使用体验。
第一版必然有不足。若你发现问题、有改进建议，或想一起把它变得更好，非常欢迎提 **Issue / PR**，我会**虚心接受、认真对待**。

> 关于 `AGENTS.md`：这份指引 AI 助手在本工作区如何开发的说明文件，**不在本仓库、也不在 git 中**（它只存在于我的本地开发环境，且与两个并排的项目共用）。想了解这套 AI 辅助开发的流程/约定，欢迎**提 issue**，我很乐意分享。

## 当前进度（M3 完成）

- **Web 对话式记录**：像聊天一样输入 → LLM 理解/追问/整理 JSON → 用户拍板（收藏/兴趣）落库
- **草稿区**：对话进行中可继续、可放弃；未拍板不写 notes
- **链接正文抓取**（服务端直抓 + 模型侧 web_fetch 工具）：SSRF 防护、20KB 截断、失败降级
- **联网搜索**：DeepSeek 原生 web_search（意图词触发：查一下/搜一下/搜索…），结果注入对话标注来源
- **直存降级**：DeepSeek 不可用时原文照常入库（pending），恢复后自动补整理（退避 5 次 → failed）
- **Ollama embedding**（bge-m3）：向量化入库、启动扫描补算；Ollama 挂时检索自动降级 FTS-only、查重退 FTS 近似版；设置页可整体关闭「本地 Embedding」（没装 Ollama 的机器用，§35）
- **查重**（向量版）：命中标 duplicate 并记录目标 id（duplicate_of），Web 端提示"疑似重复于 #id"
- **Web 检索页**：向量 + FTS + RRF 融合 + 材料层兜底召回 → LLM 作答带引用（点击跳转原笔记）
- **检索记录**：提问成功自动保存（问题 + 答案 + 时间），默认上限 50 条（`SEARCH_HISTORY_LIMIT` 可配），超出自动删除最早记录；点击历史问题再次提问，可单条删除
- **浏览页**（笔记列表）：分类浏览、kind 筛选、关键词搜索（FTS + 双字词 LIKE 兜底）、分页
- **兴趣回顾页**：兴趣清单两分区（未决策：去做/留着/放弃；进行中：稍后/转收藏/删除）
- **笔记详情页**（M3）：完整编辑（保存触发重整理：重算向量/重建 FTS/重新查重）、修正对话入口、来源对话展开、合并/忽略交互（合并含 merged 出索引）、重新整理、删除
- **登录**（M3）：`.env` 配 `AUTH_PASSWORD` 即全局启用（argon2 + SQLite session + 失败限速 + CSRF）
- **统计页 + 每周总结**（M3）：分类/标签/时间分布；LLM 生成周报（失败降级纯统计）
- 142 个 pytest 用例（LLM/embedding 全 mock），真实 DeepSeek + Ollama 端到端冒烟通过

## 前置依赖（Prerequisites）

| 依赖 | 是否必装 | 说明 |
|---|---|---|
| **Python ≥ 3.11** | 必装 | 运行环境。见下方「SQLite 版本的坑」 |
| **SQLite ≥ 3.34** | 必装（随 Python 附带） | FTS5 **trigram** 分词的前提（设计文档 §4）。`CREATE VIRTUAL TABLE ... tokenize='trigram'` 需要它。启动时应用会校验并给出清晰错误。**装好后先确认**：`python -c "import sqlite3;print(sqlite3.sqlite_version)"` |
| **DeepSeek API Key** | 必装 | 对话/整理/问答/每周总结全靠它（`DEEPSEEK_API_KEY`，见 `.env.example`） |
| **Ollama + `bge-m3`** | 可选 | 语义检索 + 向量查重（`http://127.0.0.1:11434`）。**没装也能用**：设置页把「本地 Embedding」关闭（或 `.env` 设 `EMBEDDING_ENABLED=0`），自动退化为 FTS 检索 + 查重近似版 |

> **SQLite 版本的坑**：`sqlite3` 模块的 SQLite 版本取决于你的 Python 发行版，**不是** Python 版本本身。
> 绝大多数 Python 3.11+ 自带 ≥ 3.34，但部分 Linux 发行版/旧 Python 可能打包了旧 SQLite。
> 若启动报「SQLite >= 3.34 required」，需升级 Python 或发行版包（`apt/brew upgrade python3`、`python3-sqlite3` 等）。

## 安装（Install）

以下命令跨 Windows / macOS / Linux。**统一做法**：创建虚拟环境、激活它，后续一律用激活后的 `python`。

```powershell
# 第一步：从源码装依赖（可选：用 venv 隔离）
python -m venv .venv

# Windows（PowerShell）
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

# 安装运行时依赖（测试工具在 requirements-dev.txt）
pip install -r requirements.txt
pip install -r requirements-dev.txt        # pytest + ruff，开发/贡献者用
```

配置环境变量：

```powershell
copy .env.example .env        # Windows
# cp .env.example .env        # macOS / Linux
# 编辑 .env：至少填 DEEPSEEK_API_KEY；需要语义检索就设 EMBEDDING_ENABLED=1
```

> 注：本项目**从源码运行，不 `pip install .`**（不要当普通 Python 包安装）。依赖以 `requirements.txt` 为准。

> 若你系统装不了 venv 或不想用：直接把上面 `pip install -r requirements.txt` 装进你的 Python 环境，并把启动命令里的 `python` 换成你的解释器即可。这会使 `.env` 与 data 目录落在仓库内（见 config.py `BASE_DIR`）。

## 运行（Run）

**必须单 worker（`--workers 1`）**：异步补做队列在进程内存，多 worker 会重复处理 pending（设计文档 §5）。

Windows 一键启动（自动拉起 Ollama + 启动服务，Ctrl+C 停止）：

```powershell
start.ps1
```

macOS / Linux 一键启动：

```bash
make run
```

手动方式（先按上面激活 venv，再 `cd backend`）：

```powershell
# Windows PowerShell 5.1：控制台不解析 ANSI 颜色码，加 --no-use-colors 避免 [32mINFO[0m 乱码
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-use-colors
```

```bash
# macOS / Linux
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

- 数据文件：`data/talkshelf.db`（自动创建，已 gitignore）。
- **备份**：直接复制 `data/talkshelf.db` 即可。因为开启了 WAL 模式，**建议备份前先做 checkpoint**，把最近变更合并进主文件再复制：
  `python -c "import sqlite3;sqlite3.connect('data/talkshelf.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"`
  （更稳的做法是用 `sqlite3 data/talkshelf.db ".backup backup.db"`。）恢复 = 覆盖回 `data/talkshelf.db`。
- 访问：`http://127.0.0.1:8000`
- 想用手机/局域网访问：把 `--host` 改成 `0.0.0.0`（注意同时配置登录，见下）

## 登录（可选，M3）

`.env` 配置 `AUTH_PASSWORD=你的密码` 即启用登录（页面跳登录页、API 返 401）；不配置则关闭（本地开发零负担）。其它可选配置（会话天数、锁定参数）见 `.env.example`。部署 HTTPS 后把 `AUTH_COOKIE_SECURE=1`（本地 http 必须保持 0）。

## 测试

```powershell
cd backend
python -m pytest
```

LLM 与 embedding 全部 mock（`tests/conftest.py`：固定整理 JSON + 确定性伪向量），不触网、不花钱。

## 代码规范（lint / format）

静态检查（未使用导入/变量、未定义名字、过时写法、时区陷阱等）与排版统一用 [ruff](https://docs.astral.sh/ruff/)（配置：`ruff.toml`，采用 ruff 0.16 默认规则集，`target-version = "py314"`，line-length 100）：

下面的命令在**仓库根目录**运行（`ruff.toml` 在根）：

```powershell
python -m ruff check .            # lint
python -m ruff check . --fix      # 自动修复可安全修的问题
python -m ruff format .           # format：统一排版
```

测试/开发工具在 `requirements-dev.txt`（pytest、ruff）：`pip install -r requirements-dev.txt`。

## 检索回归评测（改检索代码后必跑）

```powershell
python scripts/eval_retrieval.py
```

种子库（24 条笔记 + 2 条材料）→ 真实 Ollama 算 embedding → 26 问逐条验证期望来源在召回 Top-N，报通过率（阈值 0.8，低于即 exit 1）。Ollama 不可用时自动退化为 FTS-only 关键词评测。

## 版本与更新记录

- 语义化版本：当前版本见 `backend/app/config.py` 的 `APP_VERSION`（`0.9.0`，与 git tag `v0.9.0` 对应）。
- `CHANGELOG.md`：Keep a Changelog 格式，每版本一节（`## [X.Y.Z] - 日期`），按版本/日期检索 `grep "\[0.9.0\]" CHANGELOG.md`；发版 = bump 版本号 → CHANGELOG 追加 → `git tag -a vX.Y.Z`。
- 版本探活：`GET /api/version`（免登录）→ `{"name": "TalkShelf", "version": "0.9.0"}`，部署后确认线上版本用。

## 目录结构

```
backend/app/        config（§3.4 配置收集）| db（建表+FTS 同步+迁移）| auth（登录/session/限速/CSRF）
                    llm（DeepSeek 调用 + 工具循环 + 周总结）| fetch（SSRF 防护抓取）| embedding（Ollama 向量）
                    retrieval（检索层）| web_search（原生搜索）| dedup（向量查重 + FTS 降级）| queue（异步补做）
                    notes（落库业务 + 合并/忽略/更新/重整理）| api（端点+页面路由）| main（入口，单 worker）
                    data/（种子笔记 + rag_eval_set.json 检索评测集）
backend/tests/      pytest（LLM/embedding mock；登录/详情/统计测试）
templates/          Jinja2 页面（记录/浏览/详情/检索/兴趣回顾/统计/设置/登录，移动端优先，中文）
static/             样式与少量原生 JS（含 fetch 自动带 CSRF 头）
scripts/            eval_retrieval.py（检索回归评测）| smoke_e2e.py（真实端到端冒烟，含 M3 链路）
                    smoke_deepseek_search.py（原生搜索冒烟）
data/               运行数据（不入库）
```

## 隐私说明（数据边界）

- **笔记数据在你本地**：内容主要存本地 SQLite（`data/talkshelf.db`）。
- **会离开本机的部分**：使用「整理 / 问答 / 每周总结」时，相关文本会发给 DeepSeek（用你的 API Key）；
  「联网搜索」会让模型调用 `web_search`；「链接抓取」会访问外部 URL。
- **想更保守**：可在设置页关闭「本地 Embedding」（不跑向量化）、不配置联网搜索、不抓取链接——
  这样除 DeepSeek 对话外，数据基本留在本地。

## 架构约束（公开仓库重要说明）

- **单用户**：本地 SQLite 单文件，无多租户/权限/并发扩展设计。
- **必须 `--workers 1`**：异步补做队列在进程内存，多 worker 会重复处理 pending。
- **数据主权在家**：所有数据存本地，LLM 调用走你的 DeepSeek key（联网）。抓取/搜索功能会上网，其余离线可用。

## 已知遗留（不影响使用）

- 早期 `os.mkdir(0o700)` 产生的 ACL 损坏目录（`backend/_probe700/`、`.pytest-tmp/`、`pytest-cache-files-*/` 等）已清理；相关路径仍保留在 `.gitignore` 防御。若再出现这类「创建者自己都进不去」的目录，用管理员手动删。

## 许可证

[MIT License](LICENSE)。第三方署名与参考声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
