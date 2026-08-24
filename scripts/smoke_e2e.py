"""TalkShelf M2 端到端真实冒烟（真实 DeepSeek + 真实 Ollama，花几分钱）。

验证链路：对话式记录（真实整理 JSON）→ 拍板落库 → 队列补做（真实 embedding + 向量查重）
→ /api/ask 真实检索 + 真实作答。用临时库（DATABASE_PATH 覆盖），不碰真实数据。

用法（仓库根目录）：
    & '.venv\\Scripts\\python.exe' scripts/smoke_e2e.py
退出码：0 全部通过；非 0 冒烟失败。
"""

import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "backend"))

sys.stdout.reconfigure(encoding="utf-8")  # Windows 管道 GBK 乱码防护

from app import config, db
from fastapi.testclient import TestClient

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))


def main() -> int:
    if not config.DEEPSEEK_API_KEY:
        print("[FAIL] .env 未配置 DEEPSEEK_API_KEY，无法冒烟")
        return 2

    # 临时库（Path.mkdir 无 0o700 坑；保留供排查）
    tmp = BASE / f".nb-smoke-e2e-{time.time_ns():x}"
    tmp.mkdir()
    os.environ["DATABASE_PATH"] = str(tmp / "smoke.db")
    # config 模块已导入（DATABASE_PATH 是模块级常量）——直接覆盖
    config.DATABASE_PATH = tmp / "smoke.db"

    from app.main import app

    with TestClient(app) as client:
        # 1. 对话式记录（真实整理：LLM 理解 → 整理 JSON；§32 起回复后台异步生成）
        resp = client.post(
            "/api/conversations",
            json={
                "message": "nginx client_max_body_size 默认 1M，上传大文件直接 413，要把 http 块里调大到 100m"
            },
        )
        check("发起对话（真实整理）", resp.status_code == 200, resp.text[:200])
        conv_id = resp.json()["conversation_id"]
        deadline = time.time() + 60
        reply = None
        while time.time() < deadline:
            msgs = client.get(f"/api/conversations/{conv_id}").json()["messages"]
            last = msgs[-1] if msgs else {}
            if last.get("role") == "assistant" and last.get("kind") == "text":
                reply = last["content"]
                break
            time.sleep(1)
        check("对话回复为整理 JSON", reply is not None, f"reply={str(reply)[:80]!r}")

        # 2. 拍板落库（收藏）
        resp = client.post(f"/api/conversations/{conv_id}/confirm", json={"kind": "note"})
        check("拍板落库", resp.status_code == 200, resp.text[:200])
        note_id = resp.json()["note"]["id"]

        # 2b. 快速记录（§32）：原文立即落库，LLM 后台判断兴趣/收藏并整理
        resp = client.post("/api/quick-notes", json={"message": "想试试手冲咖啡，有空研究下"})
        check("快速记录 202", resp.status_code == 202, resp.text[:200])
        quick_id = resp.json()["note_id"]
        deadline = time.time() + 60
        quick = None
        while time.time() < deadline:
            quick = client.get(f"/api/notes/{quick_id}").json()["note"]
            if quick["status"] in ("processed", "duplicate"):
                break
            time.sleep(1)
        check(
            "快速记录整理完成",
            quick["status"] in ("processed", "duplicate"),
            f"status={quick.get('status')}",
        )
        check(
            "快速记录 kind 由 LLM 判断",
            quick["kind"] in ("note", "interest"),
            f"kind={quick.get('kind')}",
        )

        # 3. 队列补做：真实 embedding（bge-m3）+ 向量查重（真实 DeepSeek）
        deadline = time.time() + 120
        while time.time() < deadline:
            conn = db.connect()
            try:
                has_emb = (
                    conn.execute(
                        "SELECT 1 FROM embeddings WHERE note_id = ?", (note_id,)
                    ).fetchone()
                    is not None
                )
                status = conn.execute(
                    "SELECT status FROM notes WHERE id = ?", (note_id,)
                ).fetchone()[0]
            finally:
                conn.close()
            if has_emb and status in ("processed", "duplicate"):
                break
            time.sleep(1)
        check("队列补做 embedding（真实 Ollama bge-m3）", has_emb)
        check("笔记状态", status in ("processed", "duplicate"), f"status={status}")

        # 4. 问答：真实检索（向量 + FTS + RRF）+ 真实作答
        resp = client.post("/api/ask", json={"question": "上传大文件报 413 是什么原因？"})
        check("问答 200", resp.status_code == 200, resp.text[:200])
        ask = resp.json()
        check("向量检索可用", ask.get("vector_ok") is True)
        check("召回来源非空", bool(ask.get("sources")), f"sources={ask['sources']}")
        check("答案含引用/内容", len(ask.get("answer") or "") > 20, ask.get("answer", "")[:80])

        # 5. 回顾页：把这条 note 转成 interest 再走一遍分区（快速验证）
        # （兴趣条目另造一条，验证 review API）
        resp = client.post("/api/notes", json={"raw": "想试试手冲咖啡", "kind": "interest"})
        interest_id = resp.json()["note_id"]
        time.sleep(3)  # 等队列起步（LLM 整理 + embedding 异步）
        resp = client.post(f"/api/notes/{interest_id}/done")
        check("兴趣条目「去做」", resp.status_code == 200)
        review = client.get("/api/review").json()
        check("回顾两分区", "pending" in review and "in_progress" in review)
        check("进行中分区含该条目", any(n["id"] == interest_id for n in review["in_progress"]))

        # 6. M3 详情页：详情 API（含来源对话）+ 页面渲染
        resp = client.get(f"/api/notes/{note_id}")
        detail = resp.json()
        check("详情 API 200", resp.status_code == 200)
        check("详情含来源对话", len(detail.get("conversations") or []) >= 1)
        resp = client.get(f"/notes/{note_id}")
        check("详情页 HTML 200", resp.status_code == 200, resp.text[:60].replace("\n", " "))

        # 7. M3 完整编辑（PUT）：改标题 → 真实 Ollama 重算向量
        resp = client.put(f"/api/notes/{note_id}", json={"title": "冒烟改过的标题"})
        check("PUT 编辑 200", resp.status_code == 200, resp.text[:200])
        deadline = time.time() + 120
        vec_changed = False
        while time.time() < deadline:
            conn = db.connect()
            try:
                vec = conn.execute(
                    "SELECT vector FROM embeddings WHERE note_id = ?", (note_id,)
                ).fetchone()
            finally:
                conn.close()
            if vec is not None:
                vec_changed = True
                break
            time.sleep(1)
        check("编辑后向量重算（真实 Ollama）", vec_changed)
        resp = client.get(f"/api/notes/{note_id}")
        check("编辑生效", resp.json()["note"]["title"] == "冒烟改过的标题")

        # 8. M3 重新整理（reprocess）：清元数据 → 真实 LLM 重整理 → 恢复
        resp = client.post(f"/api/notes/{note_id}/reprocess")
        check("reprocess 200", resp.status_code == 200)
        deadline = time.time() + 120
        reorg = False
        while time.time() < deadline:
            status, title = note_status_db(note_id)
            if status in ("processed", "duplicate") and title:
                reorg = True
                break
            time.sleep(1)
        check("重新整理完成（真实 LLM + Ollama）", reorg)

        # 9. M3 统计 + 每周总结（真实 LLM）
        resp = client.get("/api/stats")
        st = resp.json()
        check(
            "stats 200 且含分布",
            resp.status_code == 200 and "by_category" in st and "by_month" in st,
        )
        resp = client.post("/api/weekly")
        wk = resp.json()
        check(
            "每周总结生成（真实 LLM）",
            resp.status_code == 200 and wk.get("degraded") is False,
            f"degraded={wk.get('degraded')} summary={wk.get('summary', '')[:60]!r}",
        )

        # 10. M3 登录：.env 未配 AUTH_PASSWORD 则跳过（配了则走真实登录）
        if config.auth_enabled():
            resp = client.get("/notes", follow_redirects=False)
            check("未登录页面重定向", resp.status_code == 303)
            resp = client.post("/api/login", json={"password": config.AUTH_PASSWORD})
            check("登录成功", resp.status_code == 200, resp.text[:200])
            check("登录后可访问", client.get("/notes").status_code == 200)
        else:
            check("登录（.env 未配 AUTH_PASSWORD，跳过）", True)

    print(f"\n结果：PASS={PASS} FAIL={FAIL}（临时库：{config.DATABASE_PATH}，保留供排查）")
    return 0 if FAIL == 0 else 1


def note_status_db(note_id: int):
    """读真实库（冒烟用）的笔记状态与标题。"""
    conn = db.connect()
    try:
        row = conn.execute("SELECT status, title FROM notes WHERE id = ?", (note_id,)).fetchone()
        return (row["status"], row["title"]) if row else (None, None)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
