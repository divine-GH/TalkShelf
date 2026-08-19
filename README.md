# note-brain

个人知识速记工具：记录零负担，整理交给 AI，查找用对话代替翻笔记。

设计唯一事实源：`设计文档.md`（§1~§23）；里程碑 M1→M4 见 §12。

## 当前进度（M2 完成）

- **Web 对话式记录**：像聊天一样输入 → LLM 理解/追问/整理 JSON → 用户拍板（收藏/兴趣）落库
- **草稿区**：对话进行中可继续、可放弃；未拍板不写 notes
- **链接正文抓取**（服务端直抓 + 模型侧 web_fetch 工具）：SSRF 防护、20KB 截断、失败降级
- **联网搜索**：DeepSeek 原生 web_search（意图词触发：查一下/搜一下/搜索…），结果注入对话标注来源
- **直存降级**：DeepSeek 不可用时原文照常入库（pending），恢复后自动补整理（退避 5 次 → failed）
- **Ollama embedding**（bge-m3）：向量化入库、启动扫描补算；Ollama 挂时检索自动降级 FTS-only、查重退 FTS 近似版
- **查重**（向量版）：命中标 duplicate，Web 端提示"疑似重复"
- **Web 问答页**：向量 + FTS + RRF 融合 + 材料层兜底召回 → LLM 作答带引用（点击跳转原笔记）
- **笔记列表页**：分类浏览、kind 筛选、关键词搜索（FTS + 双字词 LIKE 兜底）、分页
- **回顾页**：兴趣清单两分区（未决策：去做/留着/放弃；进行中：稍后/转收藏/删除）
- 66 个 pytest 用例（LLM/embedding 全 mock），真实 DeepSeek + Ollama 端到端冒烟通过

## 检索回归评测（改检索代码后必跑）

```powershell
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' scripts/eval_retrieval.py
```

种子库（24 条笔记 + 2 条材料）→ 真实 Ollama 算 embedding → 26 问逐条验证期望来源在召回 Top-N，
报通过率（阈值 0.8，低于即 exit 1）。Ollama 不可用时自动退化为 FTS-only 关键词评测。

## 启动（必须单 worker）

```powershell
cd backend
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

⚠️ `--workers 1` 是硬性要求：异步补做队列在进程内存，多 worker 会重复处理 pending（设计文档 §5）。
数据文件：`note-brain/data/note-brain.db`（自动创建，不入库；备份 = 复制该文件）。
依赖服务：Ollama（`http://127.0.0.1:11434`，需 `bge-m3` 模型；挂了自动降级，不影响使用）。

## 测试

```powershell
cd backend
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' -m pytest
```

LLM 与 embedding 全部 mock（`tests/conftest.py`：固定整理 JSON + 确定性伪向量），不触网、不花钱。

## 目录结构

```
backend/app/        config（§3.4 配置收集）| db（建表+FTS 同步）| llm（DeepSeek 调用 + 工具循环）
                    fetch（SSRF 防护抓取）| embedding（Ollama 向量）| retrieval（检索层）
                    web_search（原生搜索）| dedup（向量查重 + FTS 降级）| queue（异步补做）
                    notes（落库业务）| api（端点+页面路由）| main（入口，单 worker）
                    data/（种子笔记 + rag_eval_set.json 检索评测集）
backend/tests/      pytest（LLM/embedding mock）
templates/          Jinja2 页面（记录/笔记/问答/回顾，移动端优先，中文）
static/             样式与少量原生 JS
scripts/            eval_retrieval.py（检索回归评测）| smoke_e2e.py（真实端到端冒烟）
                    smoke_deepseek_search.py（原生搜索冒烟）
data/               运行数据（不入库）
```

## 已知遗留（不影响使用）

- `backend/_probe700/`、`backend/.pytest-tmp/`、`backend/pytest-cache-files-*/` 是 Windows
  `os.mkdir(0o700)` 产生的 ACL 损坏目录（沙箱内无法删除），已 gitignore；用管理员资源管理器手动删。
