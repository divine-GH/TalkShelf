"""统计页与每周总结测试（M3，设计文档 §5 / §8）。

- GET /api/stats：分类/标签/时间分布，merged 不计入；
- POST /api/weekly：LLM 生成周报（mock）；LLM 不可用降级纯统计（degraded=True）；
- /stats 页面渲染。
"""
import sqlite3

from app import llm
from conftest import note_status, wait_for


def _mk_note(client, raw: str, kind: str = "note") -> int:
    resp = client.post("/api/notes", json={"raw": raw, "kind": kind})
    assert resp.status_code == 202
    return resp.json()["note_id"]


def test_stats_structure(client, llm_ok, db_path, conn):
    a = _mk_note(client, "nginx 上传限制")
    b = _mk_note(client, "python asyncio 协程")
    c = _mk_note(client, "想试试手冲咖啡", kind="interest")
    wait_for(lambda: note_status(db_path, a)[0] in ("processed", "duplicate") and
             note_status(db_path, b)[0] in ("processed", "duplicate") and
             note_status(db_path, c)[0] in ("processed", "duplicate"), desc="三条处理完")

    data = client.get("/api/stats").json()
    assert data["total"] == 3
    cats = {x["category"]: x["count"] for x in data["by_category"]}
    assert cats.get("技术") == 3, cats
    assert isinstance(data["top_tags"], list)
    assert isinstance(data["by_month"], list) and data["by_month"]

    # merged 软删除态不计入统计
    conn.execute("UPDATE notes SET status='merged', merged_into=? WHERE id=?", (a, a))
    conn.commit()
    data = client.get("/api/stats").json()
    assert data["total"] == 2


def test_weekly_summary_with_llm(client, llm_ok, db_path, monkeypatch):
    nid = _mk_note(client, "本周记录：nginx 上传限制调大")
    wait_for(lambda: note_status(db_path, nid)[0] in ("processed", "duplicate"), desc="处理完")
    monkeypatch.setattr(llm, "weekly_summary", lambda notes: "本周共记录 1 条笔记，主题是 nginx。")
    resp = client.post("/api/weekly")
    assert resp.status_code == 200
    data = resp.json()
    assert data["degraded"] is False
    assert data["note_count"] == 1
    assert "nginx" in data["summary"]


def test_weekly_summary_degraded(client, llm_down):
    # LLM 不可用：直存笔记保持 pending（退避重试），不等待处理——周总结直接降级
    _mk_note(client, "本周记录一条")
    resp = client.post("/api/weekly")
    assert resp.status_code == 200, "LLM 挂时周总结不报错，降级纯统计"
    data = resp.json()
    assert data["degraded"] is True
    assert "本周共记录 1 条笔记" in data["summary"]


def test_stats_page_html(client, llm_ok):
    resp = client.get("/stats")
    assert resp.status_code == 200
    for fragment in ("本周总结", "分类分布", "热门标签", "时间分布"):
        assert fragment in resp.text, f"统计页缺少: {fragment}"
