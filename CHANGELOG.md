# Changelog

note-brain 的版本更新记录。格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

与 git 的分工：提交粒度看 `git log`，版本粒度看本文件，git tag（`vX.Y.Z`）是两者之间的锚点
（如 `git log v0.2.0..v0.3.0` 查看某版本包含哪些提交）。按版本/日期检索直接 grep 标题行：
`grep "\[0.3.0\]" CHANGELOG.md`、`grep "2026-08" CHANGELOG.md`。

发版流程：bump `backend/app/config.py` 的 `APP_VERSION` → 本文件顶部（`## [Unreleased]` 下方）追加
版本条目 → `git tag -a vX.Y.Z -m "…"`（中文消息）。

## [Unreleased]

### Added

- **快速记录（首页「⚡ 快速记录」按钮，§32）**：输入后不进对话页/确认页，原文立即落库
  （POST /api/quick-notes），LLM 后台自主判断兴趣/收藏并整理；处理中浏览（首页最近笔记/列表页/
  详情页）以用户原话占位显示 + 「判断中…」徽标，完成徽标消失、kind 变为兴趣/收藏；含链接时
  同步抓取正文（失败降级），补整理时 LLM 可见材料并可被 Tier 2 材料检索命中。notes 表新增
  `quick` 列（老库自动迁移）。

### Changed

- **记录对话异步化（§32）**：POST /api/conversations、追加消息端点只落用户消息立即返回，
  LLM 回复后台异步生成；对话页 LLM 未回复完成时显示「思考中…」气泡并自动轮询刷新
  （抓取/搜索材料实时追加），连发消息自动续轮各得一次回复；确认（拍板）流程与降级路径不变。
- `extract_urls` 从 api.py 移至 fetch.py（api/notes 共用）。

## [0.6.1] - 2026-08-20

### Changed

- **联网搜索模型可选（设置页）**：模型配置「联网搜索模型」子块的模型字段从只读改为可选——
  下拉列表来自 DeepSeek 模型接口（失败回落内置列表），附「自定义模型…」手动输入（确认/取消，
  回车确认）；当前生效模型不在列表时标「（默认值）/（当前值）」（默认值取 .env `SEARCH_MODEL`，
  渲染上下文新增 `search_model_default`）。**提供商仍固定 DeepSeek**（原生 web_search），
  下拉保持禁用仅一项。
- 设置表单恢复提交 `search_model`（PUT /api/settings 白名单本就含该键）；「已修改但未保存」
  标注覆盖该行；可选模型交互抽成 `initModelSelect` 工厂，对话/整理与联网搜索共用。

## [0.6.0] - 2026-08-20

### Changed

- **设置页强制手动保存**：去掉「改完立即生效」表述（模型配置/检索配置提示、保存成功消息），
  统一为「修改后需点击「保存设置」才生效」；可修改的设置项改动后在该项右侧标注
  「已修改但未保存」（服务端渲染标注 + JS 与保存基线比对，保存成功后清除）。
- **自定义模型改为「确认/取消」交互**：选择「自定义模型…」后显示文本输入 + 确认/取消按钮
  （回车等同确认；不再失焦即确认，避免误触）；取消恢复进入前的选择、空输入等同取消。
- **下拉「（当前值）」标注区分默认值**：当前生效模型不在已获取列表时，若为 .env 默认模型
  （deepseek-chat）标注「（默认值）」，非默认仍标「（当前值）」。
- **自定义模型复用列表项**：输入与已获取模型列表（`GET /api/settings/models`）中的模型相同时
  直接选中该选项、不新增条目；仅真正的新模型才插入下拉。

### Added

- 设置页渲染上下文新增 `llm_model_default`（config 默认模型，供下拉「（默认值）」标注用）。

## [0.5.1] - 2026-08-20

### Fixed

- **设置页「可选模型」点击无下拉**：datalist 在输入框已有值且与列表项完全匹配时不弹菜单
  （浏览器已知行为）——改为 `<select>` 下拉：模型列表作为选项、点击即出；当前生效模型不在
  列表时保留为「（当前值）」选项；附「自定义模型…」入口（选择后切换文本输入、回车确认回落
  到下拉），保存时未选择/未输入会提示。

## [0.5.0] - 2026-08-20

### Added

- **模型配置重构（设置页）**：模型配置/检索配置改为**默认折叠**（原生 `<details>`，点击展开），
  解决设置页过长；模型配置拆三个子块：
  - **对话/整理模型**：模型提供商下拉（7 家 OpenAI 兼容端点：DeepSeek / OpenAI / OpenRouter /
    Moonshot Kimi / 智谱 GLM / 通义千问 / SiliconFlow）+ 可选模型列表——切换提供商自动调
    `GET {base}/models`（Bearer .env key）拉取，失败回落内置列表并提示；模型不在列表可直接输入；
  - **Ollama 模型**：本地 Embedding 模型名（原样保留）；
  - **联网搜索模型**：固定 DeepSeek（原生 web_search），只读展示 + 「暂不支持修改」提示。
- **提供商注册表**（`backend/app/providers.py`）：每家含基址、.env key 变量名、内置兜底模型列表；
  API Key 只从 .env 读（设置页不提供 key 输入框，key 不入库不进 git）；DeepSeek `/models` 已实测
  可用（返回 deepseek-v4-flash / deepseek-v4-pro），其余提供商为参考实现（同 OpenAI 兼容标准，
  端点存在性已探测）。
- **新设置项 `llm_provider`**：settings 表覆盖 .env `LLM_PROVIDER`（默认 deepseek），
  llm.py 按当前提供商取 base_url + key；缺 key 报错只指出缺失的环境变量名。
- `GET /api/settings/models?provider=<id>`：拉取指定提供商模型列表（`source=api` 成功 /
  `fallback` 回落内置列表并附原因 / 未知提供商 422）。

### Changed

- 设置表单不再提交 `search_model`（联网搜索模型暂不支持修改；后端字段保留兼容）。
- 测试新增 4 个（提供商解析、缺 key 报错、模型列表端点三态、页面折叠与只读断言），
  全量 120 个 pytest 通过。

## [0.4.0] - 2026-08-20

### Added

- **设置页**：`/settings` 从占位页落地为完整设置页（设计文档 §28）——
  - **通用**：每周总结用 AI 生成开关（关闭后统计页只显示统计文本，省 token、离线可用）、
    默认分类（直存/待整理笔记立即打上兜底分类，LLM 补整理后以 LLM 为准）；
  - **模型配置**：对话/整理模型、Embedding 模型、联网搜索模型名；
  - **检索配置**：向量/关键词召回 Top-K、融合取 Top-N、相似度阈值、材料兜底条数；
  - **数据管理**：清空检索记录、补处理失败（failed）笔记列表与重试（复用 reprocess）；
  - **修改登录密码**：argon2 哈希落库（settings 表），优先于 `.env` 的 `AUTH_PASSWORD`；
  - 全部设置存 SQLite `settings` 键值表，**改完立即生效、重启不丢**，`.env` 值作默认兜底
    （`GET/PUT /api/settings`，登录启用时走鉴权 + CSRF）。
- **检索记录**：检索页（/ask）提问结果下方新增历史记录——提问成功自动保存（问题 + 答案 + 时间），
  默认上限 50 条（`.env` 可配 `SEARCH_HISTORY_LIMIT`），超出自动删除最早记录；点击历史问题可再次提问，
  每条可单独删除（`GET /api/search-history` / `DELETE /api/search-history/{id}`）。
- **版本管理基础设施**：`CHANGELOG.md`（本文件）+ git tag（v0.1.0/v0.2.0/v0.3.0）+ `config.APP_VERSION` +
  `GET /api/version`（免登录，部署探活 / 确认线上版本用）。
- **开发工具链**：引入 ruff（lint `ruff check` + format `ruff format`，配置见 `ruff.toml`，默认规则集 + line-length 100）；
  测试/开发依赖拆分到 `requirements-dev.txt`（pytest 移出运行时依赖）。

### Changed

- **顶栏重构**：主功能按钮「记录」「检索」+「更多功能」折叠按钮三枚并排一行（nav 改 flex）；
  折叠菜单收进浏览笔记/兴趣回顾/统计/设置，退出登录也移入；「更多功能」按钮不带下三角。
- **功能更名**：「笔记」→「浏览（笔记）」、「问答」→「检索」、「回顾」→「兴趣回顾」
  （URL 不变，`/notes`、`/ask`、`/review`）。
- **静态资源缓存策略**：`/static` 响应加 `Cache-Control: no-cache`（浏览器每次重新校验，未变则 304）——
  修复 StaticFiles 无 Cache-Control 导致浏览器启发式缓存旧 app.js/style.css、
  「更多功能」按钮点击无反应且样式不生效的问题（§26.4 根因）。
- **「更多功能」下拉样式收敛**：扁平卡片风（微阴影 + 1px 边框 + 12px 圆角，与全局一致）；
  按钮加 `appearance: none` 重置默认外观（iOS Safari 等不再显示原生按钮样式）。
- **每周总结响应结构**：`POST /api/weekly` 新增 `llm` 字段（关闭 AI 周报时 `false`），
  统计页区分「已关闭」与「AI 降级」两种提示。

### Fixed

- **204 响应带 body 修复**：删检索记录/删笔记/放弃草稿三处 `JSONResponse(status_code=204, content=None)`
  会发送 4 字节 `b"null"` body，违反 RFC 7230 §3.3.3（204 必须空 body），h11 报
  `Too much data for declared Content-Length` 并记录 `Exception in ASGI application`——
  统一改为 `Response(status_code=204)`（空 body，不写 Content-Length）。测试补断言 `resp.content == b""`。
- **启动脚本乱码修复**：`start.ps1` 与 README 手动启动命令增加 `--no-use-colors`——Windows PowerShell 5.1 控制台不解析 ANSI 颜色码，会原样打印 `[32mINFO[0m` 之类的转义序列。

## [0.3.0] - 2026-08-20

### Added

- **笔记详情页**：完整编辑（保存触发删向量重算 / 重建 FTS / 重新查重）、修正对话入口、来源对话展开、
  合并与忽略交互（合并含 merged 出索引）、重新整理、删除。
- **登录**：`.env` 配 `AUTH_PASSWORD` 即全局启用——argon2 + SQLite session + CSRF Token + 失败限速
  （5 次/分锁 15 分钟）。
- **统计页 + 每周总结**：分类/标签 Top15/近 12 月分布（merged 不计入）；LLM 生成中文周报，失败降级纯统计。
- **查重目标落库**：notes 加 `duplicate_of` 列（旧库 ALTER 自动迁移），Web 端提示「疑似重复于 #id」，合并直接使用。
- **一键启动**：`start.ps1`（自动拉起 Ollama serve + uvicorn 单 worker）。

### Changed

- 设计文档 §24 决策记录 + 正文修订标注 M3 完成；`.env.example` 键名修正（`OLLAMA_URL`/`EMBED_MODEL`，
  删除未实现的 `SEARCH_PROVIDER`；变量名以 config.py 为准）。
- 文档遗留修订（§25 记录）：修复 10 处历史遗留——citations 摘录/max_uses 与实测冲突、§3.4 配置层清单、
  sessions/login_failures 表、回顾两分区状态机同步等。

### Fixed

- 修正对话拍板后目标笔记向量重算（修 M2 遗留：内容变了但检索一直用旧向量）。

## [0.2.0] - 2026-08-19

### Added

- **问答**：检索层 + `POST /api/ask` + Web 问答页（向量 + FTS + RRF 融合 + 材料层兜底，LLM 作答带引用）。
- **向量查重**：Ollama bge-m3 embedding 接入（队列补算；Ollama 挂时查重降级 FTS 近似版、检索降级 FTS-only）。
- **检索回归评测集**：种子库（24 笔记 + 2 材料）+ 26 问评测集 + `scripts/eval_retrieval.py` 真实语义评测。
- **联网搜索**：DeepSeek 原生 web_search（意图词触发）+ 模型侧 web_fetch 工具循环。
- **回顾页**：兴趣清单两分区状态机（未决策：去做/留着/放弃；进行中：稍后/转收藏/删除）。

## [0.1.0] - 2026-08-19

### Added

- **对话式记录**：conversations 端点（多轮对话 + LLM 理解/追问/整理 JSON + 拍板/放弃 + 修正更新 + 直存降级）。
- **数据层与 LLM 层**：设计文档 §4 全量建表 + FTS 同步 + DeepSeek 对话式整理 + 链接抓取（SSRF 防护/截断）
  + FTS 近似查重 + 异步补做队列（指数退避/启动扫描）。
- **Web 页面**：首页（记录对话入口 + 最近笔记 + 草稿区）、聊天页（多轮/拍板/放弃）、笔记列表页
  （分类/kind/搜索/分页），移动端优先。
- **测试与文档**：41 个 pytest 用例（LLM 全 mock）+ README（单 worker 启动/测试/目录结构）。

### Fixed

- 测试临时目录清理改为可见失败（短重试 + 抛错），杜绝 `rmtree(ignore_errors=True)` 静默残留。
