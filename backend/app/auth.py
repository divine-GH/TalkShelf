"""登录与会话（设计文档 §9 + M3 拍板 §24）。

- 密码：argon2 哈希（argon2-cffi，requirements 已含）；.env 配 AUTH_PASSWORD 明文，
  启动/首次校验时哈希化，明文不落库；
- session 存 SQLite 表（可服务端注销、重启不掉线），cookie HttpOnly + SameSite=Lax，
  Secure 标志由 AUTH_COOKIE_SECURE 配置（本地 http 必须关，部署 HTTPS 置 1）；
- 登录失败限速（§9：5 次/分钟锁 15 分钟）：失败记录落 SQLite，重启不失效；
- CSRF：登录时生成 csrf_token 存 session 表，页面注入 meta，非安全方法请求须带
  X-CSRF-Token 头（校验在 api.require_auth，见 api.py）。
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from . import config, settings

_hasher = PasswordHasher()
_password_hash_cache: str | None = None  # AUTH_PASSWORD 的 argon2 哈希（进程内缓存，不落库）


def _hash() -> str:
    global _password_hash_cache
    if _password_hash_cache is None:
        _password_hash_cache = _hasher.hash(config.AUTH_PASSWORD)
    return _password_hash_cache


def hash_password(password: str) -> str:
    """对明文密码做 argon2 哈希（设置页改密码落库用，§28）。"""
    return _hasher.hash(password)


def _db_hash(conn: sqlite3.Connection) -> str | None:
    """设置页改过的密码哈希（settings 表）；未改过返回 None。"""
    return settings.get(conn, settings.KEY_AUTH_PASSWORD_HASH)


def verify_password(conn: sqlite3.Connection, password: str) -> bool:
    """校验登录密码：settings 表哈希优先（设置页改过密码后覆盖 .env AUTH_PASSWORD）。

    密码错/未配置都返回 False。conn 来自调用方（login 端点），用于读 settings 覆盖。
    """
    if not config.auth_enabled():
        return False
    stored = _db_hash(conn)
    if stored is not None:
        try:
            return _hasher.verify(stored, password)
        except VerifyMismatchError:
            return False
        except Exception:  # noqa: BLE001 —— argon2 校验异常（哈希格式损坏等）按失败处理
            return False
    try:
        return _hasher.verify(_hash(), password)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001 —— argon2 校验异常（哈希格式损坏等）按失败处理
        return False


# ---------------------------------------------------------------------------
# session（SQLite 表）
# ---------------------------------------------------------------------------


def create_session(conn: sqlite3.Connection) -> dict:
    """新建会话：清过期行 + 插入（token, csrf_token, expires_at），返回两者。"""
    conn.execute("DELETE FROM sessions WHERE expires_at < datetime('now','localtime')")
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=config.AUTH_SESSION_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "INSERT INTO sessions(token, csrf_token, expires_at) VALUES (?, ?, ?)",
        (token, csrf, expires),
    )
    return {"token": token, "csrf_token": csrf}


def get_session(conn: sqlite3.Connection, token: str | None) -> dict | None:
    """按 token 取有效会话；不存在/过期返回 None。"""
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM sessions WHERE token = ? AND expires_at >= datetime('now','localtime')",
        (token,),
    ).fetchone()
    return dict(row) if row else None


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---------------------------------------------------------------------------
# 登录失败限速（§9：5 次/分钟锁 15 分钟；记录落 SQLite，重启不失效）
# ---------------------------------------------------------------------------


def _failure_count(conn: sqlite3.Connection, since: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM login_failures WHERE attempted_at >= ?", (since,)
    ).fetchone()[0]


def is_login_blocked(conn: sqlite3.Connection) -> bool:
    """锁定判定：近窗口内失败 >= 阈值 且 距最近一次失败未超过锁定时长。"""
    now = datetime.now()
    window_start = (now - timedelta(seconds=config.LOGIN_FAIL_WINDOW)).strftime("%Y-%m-%d %H:%M:%S")
    if _failure_count(conn, window_start) < config.LOGIN_FAIL_LIMIT:
        return False
    last = conn.execute("SELECT MAX(attempted_at) FROM login_failures").fetchone()[0]
    if not last:
        return False
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (now - last_dt).total_seconds() < config.LOGIN_LOCK_SECONDS


def lock_remaining_seconds(conn: sqlite3.Connection) -> int:
    """锁定剩余秒数（供 429 提示）。未锁定时返回 0。"""
    if not is_login_blocked(conn):
        return 0
    last = conn.execute("SELECT MAX(attempted_at) FROM login_failures").fetchone()[0]
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError, TypeError:
        return 0
    return max(0, config.LOGIN_LOCK_SECONDS - int((datetime.now() - last_dt).total_seconds()))


def record_failure(conn: sqlite3.Connection) -> None:
    """记一次失败，并清理过期记录（仅保留窗口+锁定时长内的，防表膨胀）。"""
    conn.execute("INSERT INTO login_failures(attempted_at) VALUES (datetime('now','localtime'))")
    cutoff = (
        datetime.now() - timedelta(seconds=config.LOGIN_FAIL_WINDOW + config.LOGIN_LOCK_SECONDS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM login_failures WHERE attempted_at < ?", (cutoff,))


def clear_failures(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM login_failures")
