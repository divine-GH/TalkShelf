# note-brain

个人知识速记工具：记录零负担，整理交给 AI，查找用对话代替翻笔记。

设计唯一事实源：`设计文档.md`（§1~§22）；里程碑 M1→M4 见 §12。

## 当前进度（M1 完成）

- **Web 对话式记录**：像聊天一样输入 → LLM 理解/追问/整理 JSON → 用户拍板（收藏/兴趣）落库
- **草稿区**：对话进行中可继续、可放弃；未拍板不写 notes
- **链接正文抓取**（服务端直抓版，M2 升级模型侧 web_fetch）：SSRF 防护、20KB 截断、失败降级
- **直存降级**：DeepSeek 不可用时原文照常入库（pending），恢复后自动补整理（退避 5 次 → failed）
- **查重**（M1 FTS 近似版，M2 升向量版）：命中标 duplicate，Web 端提示"疑似重复"
- **笔记列表页**：分类浏览、kind 筛选、关键词搜索（FTS + 双字词 LIKE 兜底）、分页
- 41 个 pytest 用例（LLM 全 mock），真实 DeepSeek 端到端冒烟通过

## 启动（必须单 worker）

```powershell
cd backend
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

⚠️ `--workers 1` 是硬性要求：异步补做队列在进程内存，多 worker 会重复处理 pending（设计文档 §5）。
数据文件：`note-brain/data/note-brain.db`（自动创建，不入库；备份 = 复制该文件）。

## 测试

```powershell
cd backend
& 'E:\note-brain\note-brain\.venv\Scripts\python.exe' -m pytest
```

LLM 全部 mock（`tests/conftest.py` 固定整理 JSON），不触网、不花钱。

## 目录结构

```
backend/app/        config（§3.4 配置收集）| db（建表+FTS 同步）| llm（DeepSeek 调用）
                    fetch（SSRF 防护抓取）| dedup（M1 FTS 查重）| queue（异步补做）
                    notes（落库业务）| api（端点+页面路由）| main（入口，单 worker）
backend/tests/      pytest（LLM mock）
templates/          Jinja2 页面（移动端优先，中文）
static/             样式与少量原生 JS
data/               运行数据（不入库）
```

## 已知遗留（不影响使用）

- `backend/_probe700/`、`backend/.pytest-tmp/`、`backend/pytest-cache-files-*/` 是 Windows
  `os.mkdir(0o700)` 产生的 ACL 损坏目录（沙箱内无法删除），已 gitignore；用管理员资源管理器手动删。
