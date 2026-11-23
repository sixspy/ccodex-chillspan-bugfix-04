"""仓间锁与争用登记（公共并发面）。"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = now_iso()
    row = conn.execute(
        "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO chamber_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (lock_key, holder, now),
        )
        return True
    if row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder']}", now),
    )
    return False


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM chamber_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def release_committed(conn, lock_key: str, holder: str) -> None:
    """异常路径专用：先回滚未决事务再释放锁并立即提交。

    普通业务在同一事务里随业务 commit 释放；HTTPException 路径连接待
    回滚，必须先 rollback 再 DELETE 并显式提交，否则 DELETE 会被
    with 退出时的异常回滚撤回，造成批次锁残留。
    """
    if conn.in_transaction:
        conn.rollback()
    conn.execute(
        "DELETE FROM chamber_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )
    conn.commit()


def require_lock(conn, lock_key: str, holder: str) -> None:
    if not try_acquire(conn, lock_key, holder):
        raise HTTPException(409, "争用：后到只记争用，不写业务")


def acquire_business_lock(conn, lock_key: str, holder: str) -> None:
    """业务串行锁：争用只记 contention_log，不落第二份业务数据。

    锁行 INSERT 立即提交；并发写在 SQLite 写锁下串行，后到者撞主键
    被转成确定性的 409 争用并留痕，而不是把 IntegrityError 抛给调用方。
    调用方须在 finally 中以 release() 释放。
    """
    try:
        conn.execute(
            "INSERT INTO chamber_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (lock_key, holder, now_iso()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        row = conn.execute(
            "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
        ).fetchone()
        holder_now = row["holder"] if row else "未知"
        conn.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, holder, "争用", f"持有者={holder_now}", now_iso()),
        )
        conn.commit()
        raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM chamber_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        write_audit(conn, user, "lock_acquire", body.lock_key, {"ok": ok})
        conn.commit()
        if not ok:
            raise HTTPException(409, "争用：未能获得锁")
        return {"ok": True, "lock_key": body.lock_key, "holder": user}


@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
        return {"ok": True}