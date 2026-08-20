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

- **版本管理基础设施**：`CHANGELOG.md`（本文件）+ git tag（v0.1.0/v0.2.0/v0.3.0）+ `config.APP_VERSION` +
  `GET /api/version`（免登录，部署探活 / 确认线上版本用）。

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
