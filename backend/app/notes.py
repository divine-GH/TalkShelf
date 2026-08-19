"""笔记落库业务：对话拍板 / 快捷入口 / 修正更新 共用的写入逻辑。

所有函数不自行 commit——由调用方（API 层）开启并提交事务，保证原子性
（笔记 + 标签 + 实体 + 材料 + FTS 行 + 对话归档 一次提交）。
"""
from __future__ import annotations

import re
import sqlite3

from . import config, db, llm

_FETCHED_URL_RE = re.compile(r"^Fetched (\S+)")
_MD_LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")

# messages 表无 url 列（设计文档 §4）：fetched_page 的来源 URL 在 content 的 Fetched 头里；
# search_result 的来源 URL 在 markdown 链接里（- [标题](url)，多条取第一条）
def _material_url(kind: str, content: str) -> str | None:
    if kind == "fetched_page":
        m = _FETCHED_URL_RE.match(content)
        return m.group(1) if m else None
    if kind == "search_result":
        m = _MD_LINK_RE.search(content)
        return m.group(1) if m else None
    return None


class ConflictError(Exception):
    """状态冲突（如对话已归档再拍板）。"""


def apply_organized(conn: sqlite3.Connection, note_id: int, data: dict) -> None:
    """把 LLM 整理结果写入元数据 + 标签 + 实体，并同步 FTS（设计文档 §4.3 重整理管线）。"""
    conn.execute(
        """UPDATE notes SET title=?, category=?, summary=?, content=?, importance=?,
                  source_url=?, processed_at=datetime('now','localtime') WHERE id=?""",
        (
            data["title"],
            data["category"],
            data["summary"],
            data.get("content"),
            data["importance"],
            data.get("source_url"),
            note_id,
        ),
    )
    conn.execute("DELETE FROM tags WHERE note_id = ?", (note_id,))
    conn.executemany(
        "INSERT INTO tags(note_id, tag) VALUES (?, ?)",
        [(note_id, t) for t in data["tags"]],
    )
    conn.execute("DELETE FROM entities WHERE note_id = ?", (note_id,))
    conn.executemany(
        "INSERT INTO entities(note_id, type, name) VALUES (?, ?, ?)",
        [(note_id, e["type"], e["name"]) for e in (data.get("entities") or [])],
    )
    db.fts_sync(conn, note_id)


def copy_materials(conn: sqlite3.Connection, note_id: int, msgs: list[sqlite3.Row]) -> None:
    """对话材料（抓取正文/搜索结果）复制进 note_materials + materials_fts（设计文档 §4.3/§7.1 Tier 2）。"""
    for m in msgs:
        if m["kind"] not in ("fetched_page", "search_result"):
            continue
        text = m["content"]
        if m["kind"] == "fetched_page" and text.startswith("Fetched "):
            # 剥离 "Fetched <url> (HTTP <n>)" 头，落库正文与 LLM 所见一致
            text = text.split("\n", 1)[1].lstrip("\n") if "\n" in text else ""
        text = text[: config.MATERIAL_TEXT_LIMIT]
        cur = conn.execute(
            "INSERT INTO note_materials(note_id, kind, url, text) VALUES (?, ?, ?, ?)",
            (note_id, m["kind"], _material_url(m["kind"], m["content"]), text),
        )
        db.material_fts_sync(conn, cur.lastrowid)


def _user_text(msgs: list[sqlite3.Row]) -> str:
    """对话中用户的全部原话（按序拼接）——notes.raw 的语义（设计文档 §4.3）。"""
    return "\n".join(m["content"] for m in msgs if m["role"] == "user" and m["kind"] == "text").strip()


def confirm_conversation(
    conn: sqlite3.Connection,
    conv_id: int,
    kind: str,
    organized: dict | None,
) -> dict:
    """用户拍板：普通对话写入 notes；修正对话（context_note_id 非空）更新目标笔记（§4.3）。

    organized 为 None 时（LLM 不可用/整理失败）走直存模式：status='pending'，队列补整理（§14 第 5 条）。
    返回 note dict；调用方决定是否提交队列补处理。
    """
    conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    if not conv:
        raise KeyError(f"对话不存在: {conv_id}")
    if conv["status"] != "draft":
        raise ConflictError(f"对话已归档/放弃: {conv_id}")
    msgs = conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)
    ).fetchall()
    user_text = _user_text(msgs)

    if conv["context_note_id"]:
        # 修正对话：更新而非新建（raw 追加 + 元数据覆盖 + 重整理；不查重——更新非新建，§4.3）
        target_id = conv["context_note_id"]
        target = db.fetch_note(conn, target_id)
        if not target:
            raise KeyError(f"被修正笔记不存在: {target_id}")
        new_raw = (target["raw"] + "\n" + user_text).strip() if user_text else target["raw"]
        conn.execute("UPDATE notes SET raw = ? WHERE id = ?", (new_raw, target_id))
        if organized:
            apply_organized(conn, target_id, organized)
        else:
            db.fts_sync(conn, target_id)  # raw 变了也要重同步（FTS 同步不依赖 LLM，§14 第 5 条）
        copy_materials(conn, target_id, msgs)
        conn.execute(
            "UPDATE conversations SET status='archived', note_id=?, updated_at=datetime('now','localtime') WHERE id=?",
            (target_id, conv_id),
        )
        conn.execute(
            "UPDATE notes SET status='processed' WHERE id=? AND status='pending'", (target_id,)
        )
        return db.fetch_note(conn, target_id)

    # 普通对话：写入新笔记
    status = "processed" if organized else "pending"
    cur = conn.execute(
        "INSERT INTO notes(raw, kind, status, source_url) VALUES (?, ?, ?, ?)",
        (user_text, kind, status, (organized or {}).get("source_url")),
    )
    note_id = cur.lastrowid
    if organized:
        apply_organized(conn, note_id, organized)
    else:
        # 直存模式：raw 照常同步进 FTS、可检索（§14 第 5 条：FTS 同步不依赖 LLM）
        db.fts_sync(conn, note_id)
    copy_materials(conn, note_id, msgs)
    conn.execute(
        "UPDATE conversations SET status='archived', note_id=?, updated_at=datetime('now','localtime') WHERE id=?",
        (note_id, conv_id),
    )
    return db.fetch_note(conn, note_id)


def create_note_direct(conn: sqlite3.Connection, raw: str, kind: str) -> dict:
    """POST /api/notes 快捷入口：等价「发起对话+立即拍板」的直存形态（§5）。

    202 返回 + 异步补做（LLM 整理 + 查重）；同时归档一条对话便于追溯。
    """
    raw = raw.strip()
    cur = conn.execute(
        "INSERT INTO notes(raw, kind, status) VALUES (?, ?, 'pending')", (raw, kind)
    )
    note_id = cur.lastrowid
    db.fts_sync(conn, note_id)  # 直存也可检索（§14 第 5 条）
    conv_cur = conn.execute(
        "INSERT INTO conversations(status, note_id) VALUES ('archived', ?)", (note_id,)
    )
    conn.execute(
        "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'user', 'text', ?)",
        (conv_cur.lastrowid, raw),
    )
    return db.fetch_note(conn, note_id)


def latest_organized(msgs: list[sqlite3.Row]) -> dict | None:
    """从对话最近的 assistant 消息里解析整理 JSON（围栏剥离 + 校验），失败返回 None。"""
    for m in reversed(msgs):
        if m["role"] == "assistant" and m["kind"] == "text":
            try:
                data = llm.parse_json(m["content"])
                llm.validate_organized(data)
                return data
            except Exception:  # noqa: BLE001 —— 非整理 JSON 的回复（追问）跳过
                return None
    return None


# ---------------------------------------------------------------------------
# M3：详情页操作（合并/忽略/更新/重新整理）——设计文档 §6.2 / §5 / §8
# 与 confirm_conversation 相同约定：不自行 commit，由 API 层开事务；FTS 同事务维护。
# ---------------------------------------------------------------------------

# PUT 允许更新的字段白名单（§5：任意字段；status/created_at 等系统字段不可改）
UPDATABLE_FIELDS = {
    "raw", "title", "category", "summary", "content", "tags",
    "importance", "kind", "source_url", "done_at",
}


def update_note(conn: sqlite3.Connection, note_id: int, fields: dict) -> dict:
    """PUT 更新任意字段（§5）+ tags 重建 + FTS 同步（同事务）。

    校验调用方已完成（category 在体系内、importance 1..3、kind 合法等）；
    embedding 重算 / 重新查重由 API 层删向量 + 提交队列完成（§5 重整理管线）。
    """
    if "raw" in fields and not str(fields.get("raw") or "").strip():
        raise ValueError("raw 不能为空")
    if "tags" in fields:
        tags = fields.pop("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            raise ValueError("tags 须为非空字符串列表")
        conn.execute("DELETE FROM tags WHERE note_id = ?", (note_id,))
        conn.executemany(
            "INSERT INTO tags(note_id, tag) VALUES (?, ?)",
            [(note_id, t.strip()) for t in tags],
        )
    if fields:
        pairs = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE notes SET {pairs} WHERE id = ?", (*fields.values(), note_id))
    db.fts_sync(conn, note_id)
    return db.fetch_note(conn, note_id)


def merge_note(conn: sqlite3.Connection, note_id: int, target_id: int) -> dict:
    """查重「合并」（§6.2）：新笔记 raw 追加进旧笔记 → 软删除新笔记。

    - 旧笔记：raw 追加（保留用户原话）、FTS 重建；embedding 重算 / 查重由 API 层删向量 + 提交队列；
    - 新笔记：置 status='merged' + merged_into=target_id（审计痕迹，不物理删行）；
      ⚠️ 同一事务内必须删 notes_fts 行与 embeddings 行——merged 出索引（§6.2 检查点，
      查询侧 status 过滤只是表象，索引侧不清理则检索仍会命中）。
    """
    target = db.fetch_note(conn, target_id)
    if not target or target["status"] == "merged":
        raise KeyError(f"合并目标笔记不存在或已合并: {target_id}")
    note = db.fetch_note(conn, note_id)
    if not note or note["status"] == "merged":
        raise KeyError(f"被合并笔记不存在或已合并: {note_id}")
    if note_id == target_id:
        raise ValueError("不能合并到自身")
    new_raw = (target["raw"] + "\n" + note["raw"]).strip()
    conn.execute("UPDATE notes SET raw = ? WHERE id = ?", (new_raw, target_id))
    db.fts_sync(conn, target_id)
    conn.execute("DELETE FROM embeddings WHERE note_id = ?", (target_id,))  # raw 变了必须重算
    conn.execute("DELETE FROM embeddings WHERE note_id = ?", (note_id,))
    conn.execute(
        "UPDATE notes SET status='merged', merged_into=? WHERE id = ?", (target_id, note_id)
    )
    db.fts_delete(conn, note_id)  # merged 出索引（§6.2 检查点）
    return db.fetch_note(conn, target_id)


def ignore_duplicate(conn: sqlite3.Connection, note_id: int) -> dict:
    """查重「忽略」（§6.2）：duplicate → processed，清 duplicate_of（用户判定不重复）。"""
    conn.execute(
        "UPDATE notes SET status='processed', duplicate_of=NULL WHERE id = ?", (note_id,)
    )
    return db.fetch_note(conn, note_id)


def reprocess_note(conn: sqlite3.Connection, note_id: int) -> dict:
    """手动重新整理（§5 reprocess）：清空元数据 + 置 pending + 删向量。

    队列补做管线见 §14 第 6 条（结构化 → embedding → 查重 → FTS）；
    仅 merged 笔记拒绝（已软删除，出索引）。
    """
    note = db.fetch_note(conn, note_id)
    if not note:
        raise KeyError(f"笔记不存在: {note_id}")
    if note["status"] == "merged":
        raise ConflictError("已合并的笔记不可重新整理")
    conn.execute(
        """UPDATE notes SET title=NULL, category=NULL, summary=NULL, content=NULL,
                  importance=2, source_url=NULL, duplicate_of=NULL, status='pending',
                  processed_at=NULL WHERE id = ?""",
        (note_id,),
    )
    conn.execute("DELETE FROM tags WHERE note_id = ?", (note_id,))
    conn.execute("DELETE FROM entities WHERE note_id = ?", (note_id,))
    conn.execute("DELETE FROM embeddings WHERE note_id = ?", (note_id,))
    db.fts_sync(conn, note_id)
    return db.fetch_note(conn, note_id)
