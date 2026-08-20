"""API 层（设计文档 §5，M1 范围）+ Web 页面路由（§8）。

M1 端点：conversations 六件套、POST /api/notes（快捷直存）、GET /api/notes（列表检索）；
页面：首页（记录对话入口 + 最近笔记 + 草稿）、聊天页、笔记列表页。
M2+ 再补：/api/ask、/api/review、/api/notes/{id}（详情/PUT/DELETE）、/api/stats、export/import、登录。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import (
    auth,
    config,
    db,
    examples,
    fetch,
    llm,
    notes,
    providers,
    retrieval,
    settings,
    web_search,
)
from . import queue as queue_mod

logger = logging.getLogger(__name__)

router = APIRouter()

TEMPLATES = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
TEMPLATES.env.autoescape = True  # Starlette 默认不开 autoescape，必须显式开启


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------


def get_conn() -> sqlite3.Connection:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_queue(request: Request) -> queue_mod.ReprocessQueue:
    return request.app.state.queue


ConnDep = Annotated[sqlite3.Connection, Depends(get_conn)]
QueueDep = Annotated[queue_mod.ReprocessQueue, Depends(get_queue)]


def _current_session(request: Request, conn: sqlite3.Connection) -> dict | None:
    """取当前请求会话（登录未启用时恒为 None）。"""
    if not config.auth_enabled():
        return None
    return auth.get_session(conn, request.cookies.get(config.AUTH_COOKIE_NAME))


def require_auth(request: Request, conn: ConnDep) -> dict | None:
    """API 鉴权依赖（§24：配了密码就全局启用）：未登录 401；非安全方法校验 CSRF Token。

    登录未启用（AUTH_PASSWORD 未配置）时放行——本地开发/测试零负担。
    """
    if not config.auth_enabled():
        request.state.csrf_token = None
        return None
    session = _current_session(request, conn)
    if not session:
        raise HTTPException(status_code=401, detail="未登录")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        header = request.headers.get("x-csrf-token", "")
        if header != session["csrf_token"]:
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
    request.state.csrf_token = session["csrf_token"]
    return session


def require_page(request: Request, conn: ConnDep) -> dict | None:
    """页面路由鉴权依赖：未登录重定向登录页（带 next 回跳）；登录态注入 csrf_token。

    依赖里不能 raise RedirectResponse（非异常），用 303 + Location 头实现跳转。
    """
    if not config.auth_enabled():
        request.state.csrf_token = None
        return None
    session = _current_session(request, conn)
    if not session:
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/login?next={quote(request.url.path)}"},
        )
    request.state.csrf_token = session["csrf_token"]
    return session


ApiAuthDep = Annotated[dict | None, Depends(require_auth)]
PageAuthDep = Annotated[dict | None, Depends(require_page)]


def render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    """统一页面渲染：注入模板公共变量（登录启用状态；csrf_token 由鉴权依赖写入 request.state）。"""
    ctx = dict(ctx)
    ctx["auth_enabled"] = config.auth_enabled()
    return TEMPLATES.TemplateResponse(request, name, ctx)


def _fetch_conversation(conn: sqlite3.Connection, conv_id: int) -> sqlite3.Row:
    conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    if not conv:
        raise HTTPException(status_code=404, detail="对话不存在")
    return conv


def _conv_messages(conn: sqlite3.Connection, conv_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)
    ).fetchall()


def _history(conn: sqlite3.Connection, conv_id: int) -> list[dict]:
    return [
        {"role": m["role"], "kind": m["kind"], "content": m["content"], "url": None}
        for m in _conv_messages(conn, conv_id)
    ]


# ---------------------------------------------------------------------------
# 对话后台整理（§32）：端点只落用户消息立即返回，LLM 回复在事件循环后台生成
# ---------------------------------------------------------------------------


class ConversationRunner:
    """对话后台整理调度器：sync 端点（线程池线程）→ 事件循环任务。

    - kick() 线程安全（GIL 集合操作 + call_soon_threadsafe），同一对话同时只跑一个任务；
    - _run 逐轮处理：一轮 = 回复「最近一条 assistant 文本回复之后」的全部用户消息，
      用户连发多条时自动续轮，直到没有待回复消息（§32）；
    - 与异步补做队列（queue.ReprocessQueue）同生命周期：uvicorn 必须单 worker（进程内任务）。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._active: set[int] = set()  # 正在处理/已排队调度的对话 id
        self._jobs: set[asyncio.Task] = set()

    def kick(self, conv_id: int) -> None:
        """请求处理某对话；已在处理/排队中则忽略（防重复调度）。"""
        if conv_id in self._active:
            return
        self._active.add(conv_id)
        self.loop.call_soon_threadsafe(self._spawn, conv_id)

    def _spawn(self, conv_id: int) -> None:
        job = asyncio.create_task(self._run(conv_id), name=f"conv-{conv_id}")
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    async def _run(self, conv_id: int) -> None:
        try:
            while _has_pending_round(conv_id):
                try:
                    await asyncio.to_thread(_process_round, conv_id)
                except Exception as e:  # noqa: BLE001 —— 单轮失败记日志后停止（LLM/抓取/搜索失败已在轮内降级）
                    logger.error("对话 #%s 后台整理失败，停止本对话任务: %s", conv_id, e)
                    break
        finally:
            self._active.discard(conv_id)

    async def stop(self) -> None:
        """关停（lifespan）：取消全部任务并等待退出（测试间不残留跨用例任务）。"""
        for job in list(self._jobs):
            job.cancel()
        for job in list(self._jobs):
            try:
                await job
            except asyncio.CancelledError:
                pass


def _has_pending_round(conv_id: int) -> bool:
    """是否有待处理的对话轮次：draft 且存在「最近一条 assistant 文本回复之后」的用户消息。"""
    conn = db.connect()
    try:
        conv = conn.execute("SELECT status FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if not conv or conv["status"] != "draft":
            return False
        row = conn.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM messages m
                   WHERE m.conversation_id = ?
                     AND m.role = 'user' AND m.kind = 'text'
                     AND m.id > COALESCE((SELECT MAX(id) FROM messages
                                          WHERE conversation_id = ? AND role = 'assistant' AND kind = 'text'), 0)
               ) AS has_pending""",
            (conv_id, conv_id),
        ).fetchone()
        return bool(row["has_pending"])
    finally:
        conn.close()


def _process_round(conv_id: int) -> None:
    """后台处理一轮对话（§32）：抓取 + 搜索 + LLM 回复 → 落消息。

    一轮 = 回复「最近一条 assistant 文本回复之后」的全部用户消息（含其间到达的新消息，
    避免重复回复：回复插入后新消息就不再位于回复之后）；已归档/放弃的对话直接返回
    （与拍板/删除的竞态由 status='draft' 检查兜住）。
    """
    conn = db.connect()
    try:
        conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if not conv or conv["status"] != "draft":
            return  # 已归档/放弃（拍板/删除竞态）：不再处理
        last_reply = conn.execute(
            "SELECT MAX(id) AS mid FROM messages WHERE conversation_id=? AND role='assistant' AND kind='text'",
            (conv_id,),
        ).fetchone()
        last_reply_id = last_reply["mid"] or 0
        pending = conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? AND role='user' AND kind='text' AND id > ? ORDER BY id",
            (conv_id, last_reply_id),
        ).fetchall()
        if not pending:
            return

        # 链接正文抓取（服务端直抓保留，§22.4 #6）：待处理消息含 URL 即抓，失败降级不阻塞
        fetched_urls: list[str] = []
        for m in pending:
            for url in fetch.extract_urls(m["content"]):
                try:
                    result = fetch.fetch_page(url)
                    msg = fetch.fetched_message(result)
                    conn.execute(
                        "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'assistant', 'fetched_page', ?)",
                        (conv_id, msg["content"]),
                    )
                    fetched_urls.append(url)
                except fetch.FetchError as e:
                    logger.warning("抓取失败（降级，不阻塞）%s: %s", url, e)

        # 原生联网搜索（§6.5：用户明确要求才搜；失败降级不阻塞，对话照常）
        searched = False
        if any(web_search.should_search(m["content"]) for m in pending):
            try:
                items = web_search.search(pending[-1]["content"])
                content = web_search.results_to_material(items)
                conn.execute(
                    "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'assistant', 'search_result', ?)",
                    (conv_id, content),
                )
                searched = True
            except web_search.SearchError as e:
                logger.warning("搜索失败（降级，不阻塞）: %s", e)

        context_note = (
            db.fetch_note(conn, conv["context_note_id"]) if conv["context_note_id"] else None
        )
        # 有搜索结果时声明 web_fetch 工具：LLM 可主动跟进抓全文（§6.6/§22.3）
        tools = [llm.WEB_FETCH_TOOL] if searched else None
        try:
            reply = llm.organize_conversation(
                _history(conn, conv_id), context_note=context_note, tools=tools
            )
        except llm.LLMError as e:
            logger.warning("LLM 不可用，对话降级直存模式: %s", e)
            degraded = "（AI 整理服务暂不可用：对话可继续，或直接拍板原文保存，稍后自动补整理。）"
            conn.execute(
                "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'assistant', 'text', ?)",
                (conv_id, degraded),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = datetime('now','localtime') WHERE id = ?",
                (conv_id,),
            )
            conn.commit()
            return

        # 工具循环里执行的抓取落库（追溯 + Tier 2 材料检索；LLM 已见同一份文本）
        for m in reply.get("tool_materials", []):
            conn.execute(
                "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'assistant', 'fetched_page', ?)",
                (conv_id, m["content"]),
            )
        conn.execute(
            "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'assistant', 'text', ?)",
            (conv_id, reply["text"]),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = datetime('now','localtime') WHERE id = ?",
            (conv_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _confirm(
    conn: sqlite3.Connection, conv_id: int, kind: str, rq: queue_mod.ReprocessQueue
) -> dict:
    """拍板落库：优先用对话中已生成的整理 JSON；没有则强制整理一次；失败直存 pending。"""
    conv = _fetch_conversation(conn, conv_id)
    if conv["status"] != "draft":
        raise HTTPException(status_code=409, detail="对话已归档，不可拍板")
    msgs = _conv_messages(conn, conv_id)
    organized = notes.latest_organized(msgs)
    degraded = False
    if organized is None:
        context_note = (
            db.fetch_note(conn, conv["context_note_id"]) if conv["context_note_id"] else None
        )
        try:
            organized = llm.organize_conversation(
                _history(conn, conv_id), context_note=context_note, force_json=True
            )["organized"]
            conn.execute(
                "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'assistant', 'text', ?)",
                (conv_id, json.dumps(organized, ensure_ascii=False)),
            )
        except llm.LLMError as e:
            logger.warning("拍板时整理失败，直存 pending: %s", e)
            organized = None
            degraded = True
    try:
        note = notes.confirm_conversation(conn, conv_id, kind, organized)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except notes.ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if conv["context_note_id"]:
        # 修正对话更新了目标笔记（raw/元数据变化）→ 删向量强制重算（§4.3 重整理管线；M2 曾漏）
        conn.execute("DELETE FROM embeddings WHERE note_id = ?", (conv["context_note_id"],))
    conn.commit()
    rq.submit(note["id"])  # 补做管线（FTS 幂等同步 + 查重；pending 另有 LLM 整理）
    return {"note": note, "degraded": degraded}


# ---------------------------------------------------------------------------
# 版本信息（发版约定：config.APP_VERSION bump → CHANGELOG.md 追加 → git tag）
# ---------------------------------------------------------------------------


@router.get("/api/version")
def version_info() -> dict:
    """应用版本与名称。免登录：部署探活 / 确认线上跑的是哪个版本（不泄露业务数据）。"""
    return {"name": config.APP_NAME, "version": config.APP_VERSION}


# ---------------------------------------------------------------------------
# 对话端点
# ---------------------------------------------------------------------------


@router.post("/api/conversations")
def create_conversation(body: dict, conn: ConnDep, request: Request, _auth: ApiAuthDep) -> dict:
    """发起记录对话（§32）：用户消息立即落库并返回，LLM 回复后台异步生成。

    对话页打开时最后一条是用户消息 → 显示「思考中…」并轮询直到回复到达。
    """
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")
    context_note_id = body.get("context_note_id")
    cur = conn.execute(
        "INSERT INTO conversations(status, context_note_id, updated_at) VALUES ('draft', ?, datetime('now','localtime'))",
        (context_note_id,),
    )
    conv_id = cur.lastrowid
    conn.execute(
        "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'user', 'text', ?)",
        (conv_id, message),
    )
    conn.commit()
    request.app.state.conv_runner.kick(conv_id)
    return {"conversation_id": conv_id}


@router.post("/api/conversations/{conv_id}/messages")
def add_message(
    conv_id: int, body: dict, conn: ConnDep, request: Request, _auth: ApiAuthDep
) -> dict:
    """追加一条用户消息（§32）：立即返回，LLM 回复后台异步生成（连发消息自动续轮）。"""
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")
    conv = _fetch_conversation(conn, conv_id)
    if conv["status"] != "draft":
        raise HTTPException(status_code=409, detail="对话已归档，不可继续")
    conn.execute(
        "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'user', 'text', ?)",
        (conv_id, message),
    )
    conn.execute(
        "UPDATE conversations SET updated_at = datetime('now','localtime') WHERE id = ?", (conv_id,)
    )
    conn.commit()
    request.app.state.conv_runner.kick(conv_id)
    return {"conversation_id": conv_id}


@router.post("/api/conversations/{conv_id}/confirm")
def confirm_conversation(
    conv_id: int, body: dict, conn: ConnDep, rq: QueueDep, _auth: ApiAuthDep
) -> dict:
    kind = body.get("kind")
    if kind not in ("note", "interest"):
        raise HTTPException(status_code=422, detail="kind 须为 note 或 interest")
    return _confirm(conn, conv_id, kind, rq)


@router.delete("/api/conversations/{conv_id}")
def discard_conversation(conv_id: int, conn: ConnDep, _auth: ApiAuthDep) -> Response:
    conv = _fetch_conversation(conn, conv_id)
    if conv["status"] != "draft":
        raise HTTPException(status_code=409, detail="仅草稿可放弃")
    conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    conn.commit()
    # 204 禁止带 body：JSONResponse(204, content=None) 会发 4 字节 "null"，
    # h11 按 RFC 7230 §3.3.3 强制 204 空 body → LocalProtocolError（删记录/删笔记同坑）
    return Response(status_code=204)


@router.get("/api/conversations")
def list_conversations(conn: ConnDep, _auth: ApiAuthDep) -> dict:
    rows = conn.execute(
        """SELECT c.id, c.status, c.context_note_id, c.created_at, c.updated_at,
                  (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
                  (SELECT content FROM messages m WHERE m.conversation_id = c.id
                    AND m.role='user' AND m.kind='text' ORDER BY m.id DESC LIMIT 1) AS last_user_text
           FROM conversations c
           WHERE c.status = 'draft'
           ORDER BY c.updated_at DESC, c.id DESC"""
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["preview"] = (d.pop("last_user_text") or "")[:60]
        items.append(d)
    return {"items": items}


@router.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: int, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    conv = _fetch_conversation(conn, conv_id)
    msgs = [
        {
            "id": m["id"],
            "role": m["role"],
            "kind": m["kind"],
            "content": m["content"],
            "created_at": m["created_at"],
        }
        for m in _conv_messages(conn, conv_id)
    ]
    return {
        "id": conv["id"],
        "status": conv["status"],
        "context_note_id": conv["context_note_id"],
        "created_at": conv["created_at"],
        "messages": msgs,
    }


# ---------------------------------------------------------------------------
# 笔记端点
# ---------------------------------------------------------------------------


@router.post("/api/notes", status_code=202)
def create_note(body: dict, conn: ConnDep, rq: QueueDep, _auth: ApiAuthDep) -> dict:
    raw = (body.get("raw") or "").strip()
    kind = body.get("kind", "note")
    if not raw:
        raise HTTPException(status_code=422, detail="raw 不能为空")
    if kind not in ("note", "interest"):
        raise HTTPException(status_code=422, detail="kind 须为 note 或 interest")
    note = notes.create_note_direct(conn, raw, kind)
    conn.commit()
    rq.submit(note["id"])
    return {"note_id": note["id"]}


@router.post("/api/quick-notes", status_code=202)
def create_quick_note(body: dict, conn: ConnDep, rq: QueueDep, _auth: ApiAuthDep) -> dict:
    """快速记录（§32）：原文立即落库，LLM 后台判断兴趣/收藏并整理，不进对话/确认页。

    处理中（pending + quick）列表/详情以用户原话（raw）占位显示 + 「判断中…」徽标；
    LLM 判断完成后 kind 与元数据更新为整理结果。
    """
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")
    note = notes.create_quick_note(conn, message)
    conn.commit()
    rq.submit(note["id"])  # 补做管线：LLM 判断 kind + 整理 → embedding → 查重 → FTS
    return {"note_id": note["id"]}


def query_notes(
    conn: sqlite3.Connection,
    *,
    category: str | None = None,
    kind: str | None = None,
    importance: int | None = None,
    q: str | None = None,
    page: int = 1,
) -> dict:
    """笔记列表查询：SQL 等值过滤（category/kind/importance）+ q 检索（FTS trigram + LIKE 兜底，§7）。"""
    where = ["status != 'merged'"]
    params: list = []
    if category:
        where.append("category = ?")
        params.append(category)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if importance:
        where.append("importance = ?")
        params.append(importance)

    if q and q.strip():
        words = [w for w in re.split(r"\s+", q.strip()) if w]
        ids: set[int] | None = None
        for w in words:
            hit: set[int] = set()
            if len(w) >= 3:  # trigram 只匹配 >= 3 字符（§4 关键点 3）
                try:
                    rows = conn.execute(
                        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?",
                        ('"' + w.replace('"', '""') + '"',),
                    ).fetchall()
                    hit |= {r["rowid"] for r in rows}
                except sqlite3.OperationalError:
                    pass
            # 双字词/单字词兜底：LIKE（§7「双字词列表搜索兜底」）；tags 用 EXISTS 子查询
            like = f"%{w}%"
            rows = conn.execute(
                """SELECT id FROM notes WHERE raw LIKE ? OR title LIKE ? OR summary LIKE ?
                       OR content LIKE ?
                       OR EXISTS (SELECT 1 FROM tags t WHERE t.note_id = notes.id AND t.tag LIKE ?)""",
                (like, like, like, like, like),
            ).fetchall()
            hit |= {r["id"] for r in rows}
            ids = hit if ids is None else (ids & hit)  # 多词 AND
        if ids:
            placeholders = ",".join("?" * len(ids))
            where.append(f"id IN ({placeholders})")
            params.extend(sorted(ids))
        elif ids is not None:
            where.append("0 = 1")  # 多词 AND 无交集 → 空结果

    where_sql = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM notes WHERE {where_sql}", params).fetchone()[0]
    page = max(1, page)
    offset = (page - 1) * config.NOTES_PAGE_SIZE
    rows = conn.execute(
        f"""SELECT * FROM notes WHERE {where_sql}
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
        params + [config.NOTES_PAGE_SIZE, offset],
    ).fetchall()
    items = []
    for r in rows:
        tags = [
            t["tag"]
            for t in conn.execute("SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (r["id"],))
        ]
        items.append(db.note_to_dict(r, tags))
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": config.NOTES_PAGE_SIZE,
        "pages": (total + config.NOTES_PAGE_SIZE - 1) // config.NOTES_PAGE_SIZE,
    }


@router.get("/api/notes")
def list_notes(
    conn: ConnDep,
    category: str | None = None,
    kind: str | None = None,
    importance: int | None = None,
    q: str | None = None,
    page: int = 1,
    _auth: ApiAuthDep = None,
) -> dict:
    return query_notes(conn, category=category, kind=kind, importance=importance, q=q, page=page)


# ---------------------------------------------------------------------------
# 回顾端点（设计文档 §4.2 / M2 决策：kind='interest' 按 done_at 分区）
#   未决策（done_at IS NULL）：去做 → 置 done_at；留着 → 无操作；放弃 → DELETE
#   进行中（done_at 非空）：稍后 → 清 done_at 回未决策；转收藏 → kind 改 note（done_at 保留作历史）
# ---------------------------------------------------------------------------


def _fetch_note_or_404(conn: sqlite3.Connection, note_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return row


def _interest_or_409(row: sqlite3.Row) -> None:
    if row["kind"] != "interest":
        raise HTTPException(status_code=409, detail="仅兴趣清单条目可执行此操作")


@router.get("/api/review")
def review_list(conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """每周回顾：兴趣清单两分区（§4.2 / M2 决策记录）。"""
    rows = conn.execute(
        "SELECT * FROM notes WHERE kind='interest' AND status != 'merged' ORDER BY created_at DESC"
    ).fetchall()
    pending, in_progress = [], []
    for r in rows:
        d = dict(r)
        d["tags"] = [
            t["tag"]
            for t in conn.execute("SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (r["id"],))
        ]
        (in_progress if r["done_at"] else pending).append(d)
    return {"pending": pending, "in_progress": in_progress}


@router.post("/api/notes/{note_id}/done")
def note_done(note_id: int, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """兴趣条目「去做」：置 done_at=now，进入进行中分区（§4.2）。"""
    row = _fetch_note_or_404(conn, note_id)
    _interest_or_409(row)
    conn.execute("UPDATE notes SET done_at = datetime('now','localtime') WHERE id = ?", (note_id,))
    conn.commit()
    return {"note_id": note_id, "done_at": db.fetch_note(conn, note_id)["done_at"]}


@router.post("/api/notes/{note_id}/snooze")
def note_snooze(note_id: int, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """「稍后」：清 done_at，回退到未决策分区（M2 决策：进行中 → 稍后）。"""
    row = _fetch_note_or_404(conn, note_id)
    _interest_or_409(row)
    conn.execute("UPDATE notes SET done_at = NULL WHERE id = ?", (note_id,))
    conn.commit()
    return {"note_id": note_id, "done_at": None}


@router.post("/api/notes/{note_id}/convert")
def note_convert(note_id: int, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """「转收藏」：kind 改 note，done_at 保留作历史（做过的时间，M2 决策）。"""
    row = _fetch_note_or_404(conn, note_id)
    _interest_or_409(row)
    conn.execute("UPDATE notes SET kind = 'note' WHERE id = ?", (note_id,))
    conn.commit()
    return {"note_id": note_id, "kind": "note"}


@router.delete("/api/notes/{note_id}")
def delete_note(note_id: int, conn: ConnDep, _auth: ApiAuthDep) -> Response:
    """删除笔记（回顾页「放弃/删除」；外键级联清理 tags/entities/embeddings/对话/材料）。"""
    _fetch_note_or_404(conn, note_id)
    # notes_fts / materials_fts 是虚拟表（无外键），必须手动清理同一事务内（§4 FTS 同步约定）
    db.fts_delete(conn, note_id)
    conn.execute(
        "DELETE FROM materials_fts WHERE rowid IN (SELECT id FROM note_materials WHERE note_id = ?)",
        (note_id,),
    )
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 笔记详情端点（M3，设计文档 §8 详情页 / §6.2 合并忽略 / §5 PUT、reprocess）
# ---------------------------------------------------------------------------


def _note_detail(conn: sqlite3.Connection, note_id: int) -> dict:
    """详情数据：笔记 + 查重目标 + 来源对话（archived 且 note_id 关联，含修正对话）。"""
    note = db.fetch_note(conn, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    note["entities"] = [
        dict(e)
        for e in conn.execute(
            "SELECT type, name FROM entities WHERE note_id = ? ORDER BY name", (note_id,)
        )
    ]
    dup_target = None
    if note.get("duplicate_of"):
        t = db.fetch_note(conn, note["duplicate_of"])
        if t:
            dup_target = {"id": t["id"], "title": t["title"], "status": t["status"]}
    conversations = []
    convs = conn.execute(
        """SELECT id, status, context_note_id, created_at, updated_at
           FROM conversations WHERE note_id = ? AND status = 'archived' ORDER BY id""",
        (note_id,),
    ).fetchall()
    for c in convs:
        msgs = [
            dict(m)
            for m in conn.execute(
                "SELECT id, role, kind, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
                (c["id"],),
            )
        ]
        conversations.append({**dict(c), "messages": msgs})
    return {"note": note, "duplicate_target": dup_target, "conversations": conversations}


@router.get("/api/notes/{note_id}")
def get_note_detail(note_id: int, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """单条详情（含来源对话；merged 笔记也返回——页面展示「已合并至 #x」引导）。"""
    return _note_detail(conn, note_id)


def _validate_put_fields(body: dict) -> dict:
    """PUT 字段白名单 + 取值校验（§5：任意字段；系统字段不可改）。"""
    unknown = set(body) - notes.UPDATABLE_FIELDS
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"不允许更新的字段: {', '.join(sorted(unknown))}"
        )
    fields: dict = {}
    for k in ("raw", "title", "summary", "content", "source_url", "done_at"):
        if k in body:
            v = body[k]
            fields[k] = (
                None if v is None or (isinstance(v, str) and not v.strip()) else str(v).strip()
            )
    if "category" in body:
        cat = body["category"]
        if cat is not None and cat not in config.CATEGORIES:
            raise HTTPException(status_code=422, detail=f"category 不在体系内: {cat}")
        fields["category"] = cat
    if "kind" in body:
        kind = body["kind"]
        if kind not in ("note", "interest"):
            raise HTTPException(status_code=422, detail="kind 须为 note 或 interest")
        fields["kind"] = kind
    if "tags" in body:
        tags = body["tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise HTTPException(status_code=422, detail="tags 须为字符串列表")
        fields["tags"] = tags
    if "importance" in body:
        imp = body["importance"]
        if imp not in (1, 2, 3):
            raise HTTPException(status_code=422, detail="importance 须为 1/2/3")
        fields["importance"] = imp
    if (
        "source_url" in fields
        and fields["source_url"] is not None
        and not fields["source_url"].startswith(("http://", "https://"))
    ):
        raise HTTPException(status_code=422, detail="source_url 须为 http(s) URL 或空")
    return fields


@router.put("/api/notes/{note_id}")
def update_note_api(
    note_id: int, body: dict, conn: ConnDep, rq: QueueDep, _auth: ApiAuthDep
) -> dict:
    """完整编辑（§5）：更新任意字段 → 触发重整理（删向量重算 embedding + 重建 FTS + 重新查重）。"""
    note = db.fetch_note(conn, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    if note["status"] == "merged":
        raise HTTPException(status_code=409, detail="已合并的笔记不可编辑")
    fields = _validate_put_fields(body)
    try:
        updated = notes.update_note(conn, note_id, fields)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    conn.execute("DELETE FROM embeddings WHERE note_id = ?", (note_id,))  # 强制重算向量
    conn.commit()
    rq.submit(note_id, dedup=True)
    return updated


@router.post("/api/notes/{note_id}/reprocess")
def reprocess_note_api(note_id: int, conn: ConnDep, rq: QueueDep, _auth: ApiAuthDep) -> dict:
    """手动重新整理（§5）：清元数据 + 置 pending + 删向量，队列完整重跑（整理→embedding→查重→FTS）。"""
    try:
        note = notes.reprocess_note(conn, note_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except notes.ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    conn.commit()
    rq.submit(note_id, dedup=True)
    return note


@router.post("/api/notes/{note_id}/merge")
def merge_note_api(note_id: int, conn: ConnDep, rq: QueueDep, _auth: ApiAuthDep) -> dict:
    """查重「合并」（§6.2）：raw 并入 duplicate_of 目标 → 目标重整理；本条软删除 merged + 出索引。

    返回合并后的目标笔记（详情页可跳转）。
    """
    note = db.fetch_note(conn, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    target_id = note.get("duplicate_of")
    if not target_id:
        raise HTTPException(
            status_code=409, detail="缺少合并目标（查重未记录 duplicate_of，请先忽略或删除）"
        )
    try:
        target = notes.merge_note(conn, note_id, target_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    conn.commit()
    rq.submit(target_id, dedup=True)  # 目标 raw 变了：重算 embedding + 重新查重（§6.2）
    return target


@router.post("/api/notes/{note_id}/ignore")
def ignore_note_api(note_id: int, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """查重「忽略」（§6.2）：duplicate → processed，清 duplicate_of（用户判定不重复）。"""
    note = db.fetch_note(conn, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="笔记不存在")
    if note["status"] != "duplicate":
        raise HTTPException(status_code=409, detail="仅疑似重复的笔记可忽略")
    updated = notes.ignore_duplicate(conn, note_id)
    conn.commit()
    return updated


# ---------------------------------------------------------------------------
# 统计与每周总结（设计文档 §5 / §8 统计页；M3）
# ---------------------------------------------------------------------------


@router.get("/api/stats")
def stats(conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """统计（§5：按分类/标签/时间分布）；merged 软删除态不计入。"""
    scope = "status != 'merged'"
    total = conn.execute(f"SELECT COUNT(*) FROM notes WHERE {scope}").fetchone()[0]
    by_category = [
        {"category": r["category"] or "未分类", "count": r["c"]}
        for r in conn.execute(
            f"SELECT category, COUNT(*) AS c FROM notes WHERE {scope} "
            "GROUP BY category ORDER BY c DESC, category"
        )
    ]
    top_tags = [
        {"tag": r["tag"], "count": r["c"]}
        for r in conn.execute(
            "SELECT t.tag, COUNT(*) AS c FROM tags t JOIN notes n ON n.id = t.note_id "
            "WHERE n.status != 'merged' GROUP BY t.tag ORDER BY c DESC, t.tag LIMIT ?",
            (config.STATS_TOP_TAGS,),
        )
    ]
    # 近 12 个月（含当月，按 created_at 字符串前缀聚合——格式恒为 YYYY-MM-DD HH:MM:SS）
    by_month = [
        {"month": r["m"], "count": r["c"]}
        for r in conn.execute(
            f"SELECT substr(created_at, 1, 7) AS m, COUNT(*) AS c FROM notes WHERE {scope} "
            "GROUP BY m ORDER BY m DESC LIMIT 12"
        )
    ]
    by_month.reverse()
    return {"total": total, "by_category": by_category, "top_tags": top_tags, "by_month": by_month}


def _weekly_stats_text(conn: sqlite3.Connection, note_count: int) -> str:
    """纯统计周报文本（LLM 关闭/失败降级共用，§28）。"""
    cat_lines = "、".join(
        f"{c['category']} {c['count']} 条" for c in stats(conn, _auth=None)["by_category"]
    )
    return f"本周共记录 {note_count} 条笔记" + (f"（{cat_lines}）" if cat_lines else "") + "。"


@router.post("/api/weekly")
def weekly_summary_api(conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """本周总结（§5，M3 拍板；§28 起可关）：LLM 基于近 7 天笔记生成中文周报；失败降级纯统计文本。

    设置页「每周总结用 AI 生成」关闭时直接返回纯统计（省 token、离线可用），llm=False。
    """
    rows = conn.execute(
        "SELECT * FROM notes WHERE status != 'merged' "
        "AND created_at >= datetime('now', 'localtime', '-7 days') ORDER BY created_at"
    ).fetchall()
    notes_list = [
        db.note_to_dict(
            r,
            [
                t["tag"]
                for t in conn.execute(
                    "SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (r["id"],)
                )
            ],
        )
        for r in rows
    ]
    note_count = len(notes_list)
    if not settings.get_bool(conn, settings.KEY_WEEKLY_LLM, True):
        # 已关闭 AI 周报：不调 LLM，直接纯统计（§28）
        return {
            "summary": _weekly_stats_text(conn, note_count),
            "note_count": note_count,
            "degraded": False,
            "llm": False,
        }
    try:
        summary = llm.weekly_summary(notes_list)
        return {"summary": summary, "note_count": note_count, "degraded": False, "llm": True}
    except llm.LLMError as e:
        logger.warning("每周总结生成失败，降级纯统计: %s", e)
        return {
            "summary": _weekly_stats_text(conn, note_count),
            "note_count": note_count,
            "degraded": True,
            "llm": True,
        }


# ---------------------------------------------------------------------------
# 问答端点（设计文档 §7：单轮无状态；向量+FTS+RRF+材料层兜底 + LLM 作答带引用）
# ---------------------------------------------------------------------------


@router.post("/api/ask")
def ask_question(body: dict, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="question 不能为空")
    result = retrieval.retrieve(conn, question)
    try:
        answer = llm.answer_question(
            question, result["notes"], result["materials"], result["weak_recall"]
        )
    except llm.LLMError as e:
        logger.warning("问答生成失败: %s", e)
        raise HTTPException(status_code=502, detail=f"问答服务不可用: {e}") from e
    db.add_search_history(conn, question, answer)  # 检索记录：成功才落，上限裁剪在 db 层（§27）
    conn.commit()
    return {
        "question": question,
        "answer": answer,
        "sources": result["notes"],
        "material_sources": result["materials"],
        "vector_ok": result["vector_ok"],
        "weak_recall": result["weak_recall"],
    }


# ---------------------------------------------------------------------------
# 检索记录（检索页历史：/api/ask 成功自动落一条；SEARCH_HISTORY_LIMIT 上限裁剪；单条删除）
# ---------------------------------------------------------------------------


@router.get("/api/search-history")
def search_history_list(conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """检索记录列表（新→旧，最多 SEARCH_HISTORY_LIMIT 条）。"""
    return {"items": db.list_search_history(conn), "limit": config.SEARCH_HISTORY_LIMIT}


@router.delete("/api/search-history/{record_id}")
def search_history_delete(record_id: int, conn: ConnDep, _auth: ApiAuthDep) -> Response:
    """删除单条检索记录（不存在返回 404）。"""
    if not db.delete_search_history(conn, record_id):
        raise HTTPException(status_code=404, detail="检索记录不存在")
    conn.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 设置端点（设计文档 §28：settings 表键值覆盖 .env 默认值，改完立即生效、重启不丢）
# ---------------------------------------------------------------------------

# PUT /api/settings 允许的键 → 类型（None 表示恢复默认 = 删除覆盖行）
SETTINGS_FIELDS: dict[str, type] = {
    "weekly_llm": bool,
    "embedding_enabled": bool,  # §35：本地 embedding 总开关（关 = 向量化/向量检索全跳过）
    "default_category": str,
    "llm_provider": str,
    "llm_model": str,
    "embed_model": str,
    "search_model": str,
    "vector_top_k": int,
    "fts_top_k": int,
    "ask_top_n": int,
    "vector_min_sim": float,
    "materials_top_k": int,
}


def _settings_payload(conn: sqlite3.Connection) -> dict:
    """GET /api/settings 与设置页共用的响应体（生效值 + 元信息）。"""
    return {
        **settings.effective(conn),
        "categories": config.CATEGORIES,
        "app_version": config.APP_VERSION,
    }


def _validate_setting(key: str, value: object) -> None:
    """单键取值校验；非法抛 HTTPException(422)。"""
    if key in ("weekly_llm", "embedding_enabled"):
        if not isinstance(value, bool):
            raise HTTPException(status_code=422, detail=f"{key} 须为布尔值")
    elif key == "default_category":
        if not (value == "" or value in config.CATEGORIES):
            raise HTTPException(status_code=422, detail=f"default_category 不在分类体系内: {value}")
    elif key == "llm_provider":
        if value not in providers.PROVIDERS:
            raise HTTPException(status_code=422, detail=f"llm_provider 不在提供商注册表内: {value}")
    elif key in ("llm_model", "embed_model", "search_model"):
        if not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise HTTPException(status_code=422, detail=f"{key} 须为 1~100 字符的模型名")
    elif key in ("vector_top_k", "fts_top_k", "ask_top_n", "materials_top_k"):
        if type(value) is not int or not (1 <= value <= 50):
            raise HTTPException(status_code=422, detail=f"{key} 须为 1~50 的整数")
    elif key == "vector_min_sim":
        bad = (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not (0.0 <= value <= 1.0)
        )
        if bad:
            raise HTTPException(status_code=422, detail="vector_min_sim 须为 0~1 的数字")


@router.get("/api/settings")
def get_settings(conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """当前生效设置（DB 覆盖 + .env 默认值）+ 分类体系 + 版本。"""
    return _settings_payload(conn)


@router.put("/api/settings")
def put_settings(body: dict, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """更新设置：键白名单校验，value=None 恢复默认（删除覆盖行），其余按类型校验后落库。

    部分更新（只传要改的键）；改完立即生效（各使用点实时读表），无需重启。
    """
    unknown = set(body) - set(SETTINGS_FIELDS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"不支持的设置项: {', '.join(sorted(unknown))}")
    for key, value in body.items():
        if value is None:
            settings.delete(conn, key)  # 恢复默认
            continue
        _validate_setting(key, value)
        # 布尔值规范化为 "1"/"0" 存储（str(True)="True" 与读取侧解析不一致）
        settings.set_value(
            conn, key, "1" if value is True else "0" if value is False else str(value)
        )
    conn.commit()
    return _settings_payload(conn)


@router.post("/api/settings/clear-search-history")
def clear_search_history(conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """清空检索记录（数据管理）。返回删除条数。"""
    cur = conn.execute("DELETE FROM search_history")
    conn.commit()
    return {"deleted": cur.rowcount}


@router.get("/api/settings/models")
def settings_models(provider: str, _auth: ApiAuthDep) -> dict:
    """拉取指定提供商的模型列表（GET {base}/models，Bearer .env key），设置页「可选模型」用。

    失败（无 key/网络/HTTP 错误）回落内置 fallback_models：source="fallback" + detail 原因；
    成功 source="api"。登录启用时走 ApiAuthDep（app.js 自动带 CSRF 头，GET 无碍）。
    """
    if provider not in providers.PROVIDERS:
        raise HTTPException(status_code=422, detail=f"未知提供商: {provider}")
    p = providers.get(provider)
    try:
        models = providers.fetch_models(provider)
        return {"provider": provider, "provider_name": p.name, "models": models, "source": "api"}
    except providers.ProviderError as e:
        logger.warning("模型列表拉取失败（回落内置列表）：%s", e)
        return {
            "provider": provider,
            "provider_name": p.name,
            "models": list(p.fallback_models),
            "source": "fallback",
            "detail": str(e),
        }


@router.get("/api/settings/failed-notes")
def failed_notes(conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """补处理失败（status='failed'）的笔记列表（数据管理：查看 + 重试入口）。"""
    rows = conn.execute(
        "SELECT id, title, raw, created_at FROM notes WHERE status = 'failed' "
        "ORDER BY id DESC LIMIT 50"
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/api/settings/password")
def change_password(body: dict, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """修改登录密码（§28）：校验当前密码 → 新密码 argon2 哈希落 settings 表（优先于 .env AUTH_PASSWORD）。"""
    if not config.auth_enabled():
        raise HTTPException(status_code=422, detail="未启用登录（.env 配置 AUTH_PASSWORD 后可用）")
    old = body.get("old_password") or ""
    new = body.get("new_password") or ""
    if not auth.verify_password(conn, old):
        raise HTTPException(status_code=401, detail="当前密码错误")
    if len(new) < 8:
        raise HTTPException(status_code=422, detail="新密码至少 8 位")
    settings.set_value(conn, settings.KEY_AUTH_PASSWORD_HASH, auth.hash_password(new))
    conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 登录端点（设计文档 §9 + M3 拍板 §24：AUTH_PASSWORD 配置即全局启用）
# ---------------------------------------------------------------------------


@router.post("/api/login")
def login(body: dict, conn: ConnDep, response: Response) -> dict:
    """登录：argon2 校验密码 → 建 SQLite session → Set-Cookie（HttpOnly + SameSite=Lax）。

    失败限速（§9：5 次/分钟锁 15 分钟，记录落 SQLite 重启不失效）。
    登录端点本身无 CSRF 需求（不读 cookie，SameSite=Lax 已挡跨站表单）。
    """
    if not config.auth_enabled():
        raise HTTPException(status_code=403, detail="未启用登录（未配置 AUTH_PASSWORD）")
    if auth.is_login_blocked(conn):
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，已锁定，请 {auth.lock_remaining_seconds(conn)} 秒后再试",
        )
    password = body.get("password") or ""
    if not auth.verify_password(conn, password):
        auth.record_failure(conn)
        conn.commit()
        raise HTTPException(status_code=401, detail="密码错误")
    auth.clear_failures(conn)
    session = auth.create_session(conn)
    conn.commit()
    response.set_cookie(
        config.AUTH_COOKIE_NAME,
        session["token"],
        max_age=config.AUTH_SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=config.AUTH_COOKIE_SECURE,
        path="/",
    )
    return {"ok": True}


@router.post("/api/logout")
def logout(request: Request, conn: ConnDep, response: Response, _auth: ApiAuthDep) -> dict:
    """登出：删 SQLite session + 清 cookie（走鉴权 → 自动带 CSRF 校验）。"""
    auth.delete_session(conn, request.cookies.get(config.AUTH_COOKIE_NAME))
    conn.commit()
    response.delete_cookie(config.AUTH_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, conn: ConnDep) -> HTMLResponse:
    """登录页：已登录直接回首页。"""
    if config.auth_enabled() and _current_session(request, conn):
        return RedirectResponse("/", status_code=303)
    return render(
        request,
        "login.html",
        {"active": None, "next": request.query_params.get("next", "/")},
    )


# ---------------------------------------------------------------------------
# Web 页面（§8：服务端渲染 + 少量原生 JS，移动端优先，中文）
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def index_page(request: Request, conn: ConnDep, _sess: PageAuthDep) -> HTMLResponse:
    recent = query_notes(conn, page=1)["items"][:10]
    drafts = conn.execute(
        """SELECT c.id, c.updated_at,
                  (SELECT content FROM messages m WHERE m.conversation_id = c.id
                    AND m.role='user' AND m.kind='text' ORDER BY m.id DESC LIMIT 1) AS preview
           FROM conversations c WHERE c.status='draft' ORDER BY c.updated_at DESC"""
    ).fetchall()
    return render(
        request,
        "index.html",
        {
            "recent": recent,
            "drafts": [dict(d) for d in drafts],
            "categories": config.CATEGORIES,
            "active": "index",
            "record_placeholder": examples.random_example("record"),
        },
    )


@router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, _sess: PageAuthDep) -> HTMLResponse:
    return render(
        request, "ask.html", {"active": "ask", "ask_placeholder": examples.random_example("ask")}
    )


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, conn: ConnDep, _sess: PageAuthDep) -> HTMLResponse:
    data = review_list(conn, _auth=None)  # 页面路由已鉴权，内部调用无需再查
    return render(
        request,
        "review.html",
        {"pending": data["pending"], "in_progress": data["in_progress"], "active": "review"},
    )


@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
def chat_page(request: Request, conv_id: int, conn: ConnDep, _sess: PageAuthDep) -> HTMLResponse:
    conv = _fetch_conversation(conn, conv_id)
    msgs = _conv_messages(conn, conv_id)
    return render(
        request,
        "chat.html",
        {"conv": dict(conv), "messages": [dict(m) for m in msgs], "active": "index"},
    )


@router.get("/notes", response_class=HTMLResponse)
def notes_page(
    request: Request,
    conn: ConnDep,
    _sess: PageAuthDep,
    category: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    data = query_notes(conn, category=category, kind=kind, q=q, page=page)
    return render(
        request,
        "notes.html",
        {
            "data": data,
            "categories": config.CATEGORIES,
            "cur_category": category,
            "cur_kind": kind,
            "cur_q": q or "",
            "active": "notes",
        },
    )


@router.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, conn: ConnDep, _sess: PageAuthDep) -> HTMLResponse:
    """统计页（§8：分类分布 + 标签 + 时间分布 + 每周总结入口）。"""
    data = stats(conn, _auth=None)
    return render(request, "stats.html", {**data, "active": "stats"})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn: ConnDep, _sess: PageAuthDep) -> HTMLResponse:
    """设置页（§28：通用/模型/检索/数据管理/安全/关于；服务端渲染当前生效值）。"""
    return render(
        request,
        "settings.html",
        {
            "active": "settings",
            "app_version": config.APP_VERSION,
            "categories": config.CATEGORIES,
            "providers": providers.options(),
            "llm_model_default": config.LLM_MODEL,  # 设置页「（默认值）」标注用（§30）
            "search_model_default": config.SEARCH_MODEL,  # 联网搜索模型 .env 默认（同上）
            "s": settings.effective(conn),
        },
    )


@router.get("/notes/{note_id}", response_class=HTMLResponse)
def note_detail_page(
    request: Request, note_id: int, conn: ConnDep, _sess: PageAuthDep
) -> HTMLResponse:
    """笔记详情页（§8：展示 + 完整编辑 + 修正入口 + 来源对话 + 合并/忽略 + 重新整理 + 删除）。"""
    data = _note_detail(conn, note_id)
    return render(
        request,
        "note_detail.html",
        {**data, "categories": config.CATEGORIES, "active": "notes"},
    )
