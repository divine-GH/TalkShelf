"""note-brain 检索回归评测（设计文档 §12 M2 / §15.3 #1）。

用法（在仓库根目录，venv 已激活）：
    & '.venv\\Scripts\\python.exe' scripts/eval_retrieval.py            # 临时库 + 种子数据 + 评测
    & '.venv\\Scripts\\python.exe' scripts/eval_retrieval.py --keep     # 保留临时库便于排查
    & '.venv\\Scripts\\python.exe' scripts/eval_retrieval.py --threshold 0.9

行为：
1. 建临时 SQLite（backend 同级 .nb-eval-* 目录，Path.mkdir 无 0o700 坑），灌种子笔记 + 材料，
   真实 Ollama 批量算 embedding（bge-m3；Ollama 不可用时打印警告，退化为 FTS-only 评测）；
2. 逐问跑 retrieval.retrieve（与 /api/ask 同一检索层），检查期望来源是否在 Top-N；
3. 输出每题 pass/fail + 通过率，低于阈值 exit 1（CI/提交前可挂）。

注意：本脚本测的是"检索召回层"（防检索悄悄退化），不调 LLM 作答、不花钱。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "backend"))

from app import config, db, embedding, retrieval  # noqa: E402

EVAL_SET_PATH = BASE / "backend" / "app" / "data" / "rag_eval_set.json"


def seed(conn) -> dict[str, int]:
    """灌种子笔记 + 材料，批量算 embedding（真实 Ollama）。返回 title → note_id。"""
    from app.data.seed_notes import MATERIAL_OWNERS, SEED_MATERIALS, SEED_NOTES

    ids: list[int] = []
    for kind, cat, title, summary, content, raw, url in SEED_NOTES:
        cur = conn.execute(
            """INSERT INTO notes(raw, kind, status, source_url, title, category, summary, content,
                                 processed_at) VALUES (?,?,?,?,?,?,?,?, datetime('now','localtime'))""",
            (raw, kind, "processed", url, title, cat, summary, content),
        )
        ids.append(cur.lastrowid)
        db.fts_sync(conn, cur.lastrowid)
    for i, (kind, url, text) in enumerate(SEED_MATERIALS):
        cur = conn.execute(
            "INSERT INTO note_materials(note_id, kind, url, text) VALUES (?,?,?,?)",
            (ids[MATERIAL_OWNERS[i]], kind, url, text),
        )
        db.material_fts_sync(conn, cur.lastrowid)
    conn.commit()

    notes = [db.fetch_note(conn, i) for i in ids]
    vecs = embedding.embed_texts([embedding._note_embedding_text(n) for n in notes])
    for nid, v in zip(ids, vecs):
        embedding.save_embedding(conn, nid, v)
    conn.commit()
    return {n["title"]: n["id"] for n in (db.fetch_note(conn, i) for i in ids)}


def run_eval(conn, title_to_id: dict[str, int], threshold: float) -> int:
    eval_set = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
    questions = eval_set["questions"]
    print(f"评测集：{eval_set['total']} 题（通过阈值 {eval_set['pass_threshold']}，本次 --threshold {threshold}）\n")

    passed = 0
    for q in questions:
        r = retrieval.retrieve(conn, q["question"])
        top_k = q.get("top_k", 6)
        expected = q["expected_source_contains"]
        if q["expected_source_type"] == "note":
            nid = title_to_id.get(expected)
            hit = nid is not None and any(s["id"] == nid for s in r["notes"][:top_k])
        else:
            hit = any(expected in (m.get("snippet") or "") for m in r["materials"])
        mark = "PASS" if hit else "FAIL"
        if hit:
            passed += 1
        print(f"[{mark}] {q['id']} {q['question']}")
        if not hit:
            got = [s["id"] for s in r["notes"][:top_k]]
            print(f"       期望: {expected}（{q['expected_source_type']}）"
                  f" 实际 Top-{top_k} 笔记: {got} 材料命中: {len(r['materials'])} 条")

    rate = passed / len(questions)
    print(f"\n通过率：{passed}/{len(questions)} = {rate:.0%}（阈值 {threshold:.0%}）")
    return 0 if rate >= threshold else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="note-brain 检索回归评测")
    ap.add_argument("--keep", action="store_true", help="保留临时库（默认评测后删除）")
    ap.add_argument("--threshold", type=float, default=None, help="通过率阈值（默认读评测集 pass_threshold）")
    args = ap.parse_args()

    d = config.BASE_DIR / f".nb-eval-{time.time_ns():x}"
    d.mkdir()  # 不用 tempfile.mkdtemp（0o700 坑，见 AGENTS.md 经验）
    db_path = d / "eval.db"
    conn = db.connect(db_path)
    try:
        db.init_db(conn)
        title_to_id = seed(conn)
        print(f"种子：{len(title_to_id)} 条笔记 + 2 条材料，embedding 已入库（{config.EMBED_MODEL}）\n")

        eval_set = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
        threshold = args.threshold if args.threshold is not None else eval_set["pass_threshold"]
        return run_eval(conn, title_to_id, threshold)
    except embedding.EmbeddingError as e:
        print(f"⚠️ Ollama embedding 不可用（{e}），本次为 FTS-only 关键词评测（向量路跳过）。")
        eval_set = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
        threshold = args.threshold if args.threshold is not None else eval_set["pass_threshold"]
        return run_eval(conn, {}, threshold)
    finally:
        conn.close()
        if not args.keep:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
