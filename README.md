# note-brain

个人知识速记工具：记录零负担，整理交给 AI，查找用对话代替翻笔记。

设计唯一事实源：`设计文档.md`（§1~§35）；里程碑 M1→M4 见 §12。

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

## 登录（M3，可选）

在 `.env` 配置 `AUTH_PASSWORD=你的密码` 即启用登录（页面跳登录页、API 返 401）；
不配置则登录关闭（本地开发零负担）。其它可选配置（会话天数、锁定参数）见 `.env.example`。
部署 HTTPS 后把 `AUTH_COOKIE_SECURE=1`（本地 http 必须保持 0）。

## 检索回归评测（改检索代码后必跑）

```powershell
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' scripts/eval_retrieval.py
```

种子库（24 条笔记 + 2 条材料）→ 真实 Ollama 算 embedding → 26 问逐条验证期望来源在召回 Top-N，
报通过率（阈值 0.8，低于即 exit 1）。Ollama 不可用时自动退化为 FTS-only 关键词评测。

## 启动（必须单 worker）

**一键启动**：右键 `start.ps1` → 「使用 PowerShell 运行」（自动拉起 Ollama 并启动服务，Ctrl+C 停止）。

手动方式：

```powershell
cd backend
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-use-colors
```

`--no-use-colors`：Windows PowerShell 5.1 控制台不解析 ANSI 颜色码（会原样打印 `[32mINFO[0m` 乱码），故统一禁用颜色。

⚠️ `--workers 1` 是硬性要求：异步补做队列在进程内存，多 worker 会重复处理 pending（设计文档 §5）。
数据文件：`note-brain/data/note-brain.db`（自动创建，不入库；备份 = 复制该文件）。
依赖服务：Ollama（`http://127.0.0.1:11434`，需 `bge-m3` 模型；挂了自动降级，不影响使用；没装可在设置页关闭「本地 Embedding」）。

## 测试

```powershell
cd backend
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' -m pytest
```

LLM 与 embedding 全部 mock（`tests/conftest.py`：固定整理 JSON + 确定性伪向量），不触网、不花钱。

## 代码规范（lint / format）

静态检查（未使用导入/变量、未定义名字、过时写法、时区陷阱等）与排版统一用
[ruff](https://docs.astral.sh/ruff/)（配置：`ruff.toml`，采用 ruff 0.16 默认规则集，line-length 100）：

```powershell
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' -m ruff check .          # lint
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' -m ruff check . --fix    # 自动修复可安全修的问题
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' -m ruff format .         # format：统一排版
```

测试/开发工具在 `requirements-dev.txt`（pytest、ruff）：`pip install -r requirements-dev.txt`。

## 版本与更新记录

- 语义化版本：当前版本见 `backend/app/config.py` 的 `APP_VERSION`（`0.7.6`，与 git tag `v0.7.6` 对应）。
- `CHANGELOG.md`：Keep a Changelog 格式，每版本一节（`## [X.Y.Z] - 日期`），按版本/日期检索
  `grep "\[0.7.6\]" CHANGELOG.md`；发版 = bump 版本号 → CHANGELOG 追加 → `git tag -a vX.Y.Z`。
- 版本探活：`GET /api/version`（免登录）→ `{"name": "note-brain", "version": "0.7.6"}`，部署后确认线上版本用。

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

## 已知遗留（不影响使用）

- 早期 `os.mkdir(0o700)` 产生的 ACL 损坏目录（`backend/_probe700/`、`.pytest-tmp/`、`pytest-cache-files-*/` 等）已清理；
  相关路径仍保留在 `.gitignore` 防御。若再出现这类「创建者自己都进不去」的目录，用管理员手动删。
