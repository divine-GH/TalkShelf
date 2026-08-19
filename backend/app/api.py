"""API 层（设计文档 §5，M1 范围）+ Web 页面路由（§8）。

M1 端点：conversations 六件套、POST /api/notes（快捷直存）、GET /api/notes（列表检索）；
页面：首页（记录对话入口 + 最近笔记 + 草稿）、聊天页、笔记列表页。
M2+ 再补：/api/ask、/api/review、/api/notes/{id}（详情/PUT/DELETE）、/api/stats、export/import、登录。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import auth, config, db, fetch, llm, notes, queue as queue_mod, retrieval, web_search

logger = logging.getLogger(__name__)

router = APIRouter()

# URL 匹配排除中文/全角标点，避免把后续文本吃进 URL（如 "http://x.cn/2，以及…"）
_URL_RE = re.compile(r"https?://[^\s<>\"'，。；：！？、（）【】《》「」『』]+")
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


def extract_urls(message: str) -> list[str]:
    """从用户消息提取 http(s) URL（去重）。"""
    seen: set[str] = set()
    urls: list[str] = []
    for m in _URL_RE.findall(message):
        url = m.rstrip(".,;:!?)]}")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _step_conversation(conn: sqlite3.Connection, conv_id: int, message: str) -> dict:
    """追加一条用户消息 → 直抓链接正文 + 原生搜索（按意图词触发）→ LLM 回复（可调 web_fetch）→ 落消息。"""
    conv = _fetch_conversation(conn, conv_id)
    if conv["status"] != "draft":
        raise HTTPException(status_code=409, detail="对话已归档，不可继续")

    conn.execute(
        "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'user', 'text', ?)",
        (conv_id, message),
    )

    # 链接正文抓取（服务端直抓保留，§22.4 #6）：用户消息含 URL 即抓，失败降级不阻塞
    fetched_urls: list[str] = []
    for url in extract_urls(message):
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
    if web_search.should_search(message):
        try:
            items = web_search.search(message)
            content = web_search.results_to_material(items)
            conn.execute(
                "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'assistant', 'search_result', ?)",
                (conv_id, content),
            )
            searched = True
        except web_search.SearchError as e:
            logger.warning("搜索失败（降级，不阻塞）: %s", e)

    conn.execute(
        "UPDATE conversations SET updated_at = datetime('now','localtime') WHERE id = ?", (conv_id,)
    )

    context_note = db.fetch_note(conn, conv["context_note_id"]) if conv["context_note_id"] else None
    # 有搜索结果时声明 web_fetch 工具：LLM 可主动跟进抓全文（§6.6/§22.3）
    tools = [llm.WEB_FETCH_TOOL] if searched else None
    try:
        reply = llm.organize_conversation(_history(conn, conv_id), context_note=context_note, tools=tools)
    except llm.LLMError as e:
        logger.warning("LLM 不可用，对话降级直存模式: %s", e)
        degraded = "（AI 整理服务暂不可用：对话可继续，或直接拍板原文保存，稍后自动补整理。）"
        conn.execute(
            "INSERT INTO messages(conversation_id, role, kind, content) VALUES (?, 'assistant', 'text', ?)",
            (conv_id, degraded),
        )
        conn.commit()
        return {"reply": degraded, "degraded": True, "fetched": fetched_urls, "searched": searched}

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
    conn.commit()
    return {"reply": reply["text"], "organized": reply["organized"] is not None,
            "degraded": False, "fetched": fetched_urls, "searched": searched}


def _confirm(conn: sqlite3.Connection, conv_id: int, kind: str, rq: queue_mod.ReprocessQueue) -> dict:
    """拍板落库：优先用对话中已生成的整理 JSON；没有则强制整理一次；失败直存 pending。"""
    conv = _fetch_conversation(conn, conv_id)
    if conv["status"] != "draft":
        raise HTTPException(status_code=409, detail="对话已归档，不可拍板")
    msgs = _conv_messages(conn, conv_id)
    organized = notes.latest_organized(msgs)
    degraded = False
    if organized is None:
        context_note = db.fetch_note(conn, conv["context_note_id"]) if conv["context_note_id"] else None
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
    conn.commit()
    rq.submit(note["id"])  # 补做管线（FTS 幂等同步 + 查重；pending 另有 LLM 整理）
    return {"note": note, "degraded": degraded}


# ---------------------------------------------------------------------------
# 对话端点
# ---------------------------------------------------------------------------

@router.post("/api/conversations")
def create_conversation(body: dict, conn: ConnDep, rq: QueueDep, _auth: ApiAuthDep) -> dict:
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")
    context_note_id = body.get("context_note_id")
    cur = conn.execute(
        "INSERT INTO conversations(status, context_note_id) VALUES ('draft', ?)",
        (context_note_id,),
    )
    conv_id = cur.lastrowid
    result = _step_conversation(conn, conv_id, message)
    return {"conversation_id": conv_id, **result}


@router.post("/api/conversations/{conv_id}/messages")
def add_message(conv_id: int, body: dict, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    message = (body.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")
    return _step_conversation(conn, conv_id, message)


@router.post("/api/conversations/{conv_id}/confirm")
def confirm_conversation(conv_id: int, body: dict, conn: ConnDep, rq: QueueDep, _auth: ApiAuthDep) -> dict:
    kind = body.get("kind")
    if kind not in ("note", "interest"):
        raise HTTPException(status_code=422, detail="kind 须为 note 或 interest")
    return _confirm(conn, conv_id, kind, rq)


@router.delete("/api/conversations/{conv_id}")
def discard_conversation(conv_id: int, conn: ConnDep, _auth: ApiAuthDep) -> JSONResponse:
    conv = _fetch_conversation(conn, conv_id)
    if conv["status"] != "draft":
        raise HTTPException(status_code=409, detail="仅草稿可放弃")
    conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    conn.commit()
    return JSONResponse(status_code=204, content=None)


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
    return {"id": conv["id"], "status": conv["status"], "context_note_id": conv["context_note_id"],
            "created_at": conv["created_at"], "messages": msgs}


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
        tags = [t["tag"] for t in conn.execute(
            "SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (r["id"],))]
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
        d["tags"] = [t["tag"] for t in conn.execute(
            "SELECT tag FROM tags WHERE note_id = ? ORDER BY tag", (r["id"],))]
        (in_progress if r["done_at"] else pending).append(d)
    return {"pending": pending, "in_progress": in_progress}


@router.post("/api/notes/{note_id}/done")
def note_done(note_id: int, conn: ConnDep, _auth: ApiAuthDep) -> dict:
    """兴趣条目「去做」：置 done_at=now，进入进行中分区（§4.2）。"""
    row = _fetch_note_or_404(conn, note_id)
    _interest_or_409(row)
    conn.execute(
        "UPDATE notes SET done_at = datetime('now','localtime') WHERE id = ?", (note_id,)
    )
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
def delete_note(note_id: int, conn: ConnDep, _auth: ApiAuthDep) -> JSONResponse:
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
    return JSONResponse(status_code=204, content=None)


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
    return {
        "question": question,
        "answer": answer,
        "sources": result["notes"],
        "material_sources": result["materials"],
        "vector_ok": result["vector_ok"],
        "weak_recall": result["weak_recall"],
    }


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
    if not auth.verify_password(password):
        auth.record_failure(conn)
        conn.commit()
        raise HTTPException(status_code=401, detail="密码错误")
    auth.clear_failures(conn)
    session = auth.create_session(conn)
    conn.commit()
    response.set_cookie(
        config.AUTH_COOKIE_NAME, session["token"],
        max_age=config.AUTH_SESSION_DAYS * 86400,
        httponly=True, samesite="lax", secure=config.AUTH_COOKIE_SECURE, path="/",
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
        request, "login.html",
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
        request, "index.html",
        {"recent": recent, "drafts": [dict(d) for d in drafts], "categories": config.CATEGORIES,
         "active": "index"},
    )


@router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, _sess: PageAuthDep) -> HTMLResponse:
    return render(request, "ask.html", {"active": "ask"})


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, conn: ConnDep, _sess: PageAuthDep) -> HTMLResponse:
    data = review_list(conn, _auth=None)  # 页面路由已鉴权，内部调用无需再查
    return render(
        request, "review.html",
        {"pending": data["pending"], "in_progress": data["in_progress"], "active": "review"},
    )


@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
def chat_page(request: Request, conv_id: int, conn: ConnDep, _sess: PageAuthDep) -> HTMLResponse:
    conv = _fetch_conversation(conn, conv_id)
    msgs = _conv_messages(conn, conv_id)
    return render(
        request, "chat.html",
        {"conv": dict(conv), "messages": [dict(m) for m in msgs], "active": "index"},
    )


@router.get("/notes", response_class=HTMLResponse)
def notes_page(
    request: Request, conn: ConnDep, _sess: PageAuthDep,
    category: str | None = None, kind: str | None = None, q: str | None = None, page: int = 1,
) -> HTMLResponse:
    data = query_notes(conn, category=category, kind=kind, q=q, page=page)
    return render(
        request, "notes.html",
        {"data": data, "categories": config.CATEGORIES,
         "cur_category": category, "cur_kind": kind, "cur_q": q or "",
         "active": "notes"},
    )
