# Contributing to note-brain

感谢你有兴趣一起把 note-brain 做得更好！这个项目由 AI 辅助开发（见 README「声明」），
我很欢迎外部贡献——提 Issue 或 PR 都好，我会**虚心接受、认真对待**。

## 环境要求

- Python >= 3.11（且 SQLite >= 3.34，FTS5 trigram 前提，见 README「前置依赖」）
- Git

## 本地开发

```bash
# 克隆你自己的 fork 并进入目录（提交 PR 用 fork）
git clone <your-fork> note-brain && cd note-brain

# 创建虚拟环境并装依赖
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1   |   macOS / Linux: source .venv/bin/activate
pip install -r requirements-dev.txt      # 含运行依赖 + pytest + ruff
```

> 本项目**从源码运行，不 `pip install .`**。依赖以 `requirements.txt` / `requirements-dev.txt` 为准。

## 跑测试

```bash
cd backend
python -m pytest -q
```

LLM 与 embedding 全部 mock（`tests/conftest.py`），不触网、不花钱。

## lint / format

从仓库**根目录**运行（`ruff.toml` 在根）：

```bash
python -m ruff check .          # 需零告警才可提交
python -m ruff format .         # 格式化
```

提交前确保这三项全绿（CI 也会跑）：`ruff check .` 零告警、`ruff format --check .` 通过、`pytest` 全过。

## 跑起来（本地验证）

```bash
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
# macOS / Linux 也可：make run   （Windows 一键：start.ps1）
```

⚠️ **必须单 worker**（`--workers 1`）：异步补做队列在进程内存，多 worker 会重复处理 pending（设计文档 §5）。

## 提交与 PR

- **分支**：`git checkout -b feat/xxx`（或 `fix/xxx`）。
- **提交信息用中文**，一行说清目的；涉及版本改动按语义化版本（见 `CHANGELOG.md`）。
- **提交前**：`ruff check .`、`ruff format --check .`、`cd backend && python -m pytest -q` 全部通过。
- **提 PR**：说明改了什么、为什么、如何验证；附上测试/lint 结果；解决一个 Issue 时在描述里 `Fixes #编号`。

## 代码约定

- 中文注释/文档字符串为主；动最少的代码完成目标。
- FastAPI 端点用 `sync def`（后端自动进线程池）；SQLite 连接每请求新建（`get_conn` 负责关闭）。
- 新增配置项时**同步 `.env.example` 与注释**（变量名以 `config.py` 为准，别和旧名混用）。
- 改 schema 给旧表加列走 `db._migrate`（`CREATE TABLE IF NOT EXISTS` 不会给旧表加列）。
- **版本号递增**：只自行增 patch 位（`0.8.x`）；major / minor 需先与维护者确认（见「发版流程」）。

## 更多

- 行为/交互层面的改动**先提 Issue 讨论**，再动手，避免返工。
- 总体架构与设计决策见 `设计文档.md`（§1~§35），改动机前请先读相关章节。
- 本仓库的 `AGENTS.md`（AI 助手开发约定）**不在仓库内**（见 README 声明）；想看可提 issue。
