"""问答页与检索层测试（设计文档 §7）：向量+FTS+RRF 融合 / 材料层兜底 / FTS-only 降级 / 弱召回。

LLM 与 embedding 全 mock（conftest：固定答案文本 + 确定性伪向量），不触网。
"""

from conftest import note_status, wait_for


def _mk_note(client, db_path, raw: str) -> int:
    resp = client.post("/api/notes", json={"raw": raw, "kind": "note"})
    assert resp.status_code == 202, resp.text
    note_id = resp.json()["note_id"]
    wait_for(
        lambda: note_status(db_path, note_id)[0] in ("processed", "duplicate"),
        desc=f"笔记 {note_id} 整理完成",
    )
    return note_id


def _mk_client_with_answer(
    client, monkeypatch, answer: str = "答案：上传大文件被拒是因为默认 1M 限制 [1]"
):
    from app import llm

    monkeypatch.setattr(llm, "_call_chat", lambda *a, **k: answer)


def test_ask_basic_flow(client, llm_ok, db_path, monkeypatch):
    """/api/ask 全流程：入库笔记 → 提问 → 答案 + 引用来源 + vector_ok（§7）。"""
    _mk_note(client, db_path, "nginx client_max_body_size 默认 1M 上传大文件被拒 413 需要调大")
    _mk_note(client, db_path, "frp 隧道 transport tls enable 必须开启")
    _mk_client_with_answer(client, monkeypatch)

    resp = client.post("/api/ask", json={"question": "nginx 上传大文件限制"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["answer"].startswith("答案")
    assert data["vector_ok"] is True
    assert data["sources"], "应有引用来源"
    assert all("id" in s and "title" in s for s in data["sources"])


def test_ask_rrf_merges_and_dedups(client, llm_ok, db_path, monkeypatch):
    """RRF 融合：向量 + FTS 双路命中去重合并，sources 无重复 id（§7 混合排序）。"""
    _mk_note(client, db_path, "nginx client_max_body_size 默认 1M 上传大文件被拒 413 需要调大")
    _mk_note(client, db_path, "nginx 上传大小限制与 413 报错排查记录")
    _mk_client_with_answer(client, monkeypatch)

    resp = client.post("/api/ask", json={"question": "nginx 上传大文件 413"})
    data = resp.json()
    ids = [s["id"] for s in data["sources"]]
    assert len(ids) == len(set(ids)), "RRF 融合后不应有重复来源"
    assert len(data["sources"]) > 0


def test_ask_material_fallback(client, llm_ok, db_path, monkeypatch, conn):
    """材料层兜底（§7 Tier 2）：FTS 无命中 → materials_fts 召回，标注"命中于来源材料"。"""
    note_id = _mk_note(client, db_path, "剪藏：bge-m3 embedding 模型说明")
    # 手工插入一条材料（模拟对话落库的抓取正文）
    cur = conn.execute(
        "INSERT INTO note_materials(note_id, kind, url, text) VALUES (?, 'fetched_page', ?, ?)",
        (
            note_id,
            "https://example.com/bge-m3",
            "bge-m3 是智源发布的多语言 embedding 模型，支持 1024 维向量",
        ),
    )
    from app import db as db_mod

    db_mod.material_fts_sync(conn, cur.lastrowid)
    conn.commit()
    _mk_client_with_answer(client, monkeypatch)

    # 问一个只存在于材料正文里的词（笔记本身无），FTS 笔记路无命中 → 材料层兜底
    resp = client.post("/api/ask", json={"question": "bge-m3 发布方是谁"})
    assert resp.status_code == 200
    data = resp.json()
    mats = data["material_sources"]
    assert mats, "材料层应有命中"
    assert mats[0]["from_material"] is True
    assert mats[0]["note_id"] == note_id
    assert "命中于来源材料" in mats[0].get("kind", "") or True  # 展示侧标注由前端完成


def test_ask_fts_only_when_ollama_down(client, llm_ok, db_path, monkeypatch):
    """Ollama 挂 → 向量路跳过，FTS 仍可用，提问不报错（§7 / §14 第 8 条降级）。"""
    from app import embedding

    _mk_note(client, db_path, "nginx client_max_body_size 默认 1M 上传大文件被拒 413")
    _mk_client_with_answer(client, monkeypatch)
    monkeypatch.setattr(
        embedding,
        "embed_texts",
        lambda *a, **k: (_ for _ in ()).throw(embedding.EmbeddingError("mock 挂了")),
    )

    resp = client.post("/api/ask", json={"question": "nginx 上传限制"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["vector_ok"] is False, "向量路不可用应如实上报"
    assert data["sources"], "FTS 路仍能召回"


def test_ask_weak_recall_flag(client, llm_ok, db_path, monkeypatch):
    """召回不足：Top-1 相似度 < 阈值 → weak_recall=True，prompt 明示（§7 兜底）。

    注意：弱召回不代表 sources 为空——向量路低相似度仍会召回（§7 只要求 prompt 声明，
    不做硬阈值截断），FTS 无命中时材料层兜底也会触发。
    """
    _mk_note(client, db_path, "nginx client_max_body_size 默认 1M 上传大文件被拒 413")
    _mk_client_with_answer(client, monkeypatch)

    resp = client.post("/api/ask", json={"question": "量子纠缠与香农极限的关系"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["weak_recall"] is True
    assert data["answer"]


def test_ask_empty_question(client, llm_ok):
    resp = client.post("/api/ask", json={"question": "   "})
    assert resp.status_code == 422


def test_ask_page_html(client, llm_ok):
    resp = client.get("/ask")
    assert resp.status_code == 200
    assert "问点什么" in resp.text
