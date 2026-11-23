"""温谱导出（行数门闸 + 批次锁串行）。

行数门闸口径（甲路径，单轨拍板，禁止与影子手稿口径混用）：
封批代绑定点数 == 实际落地采样行数方可入温谱正式册（status='已导出'）；
不一致则整笔拒收，正式册零行、批次导出态零改。
任何非“已导出”的影子手稿永不晋升、永不下发为正式导出。
同批并发拉取以 batch:{id} 锁串行，后到者只留争用痕，不落第二份正式册。
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import acquire_business_lock, release_committed
from models import SpectrumIn

router = APIRouter(tags=["spectrum"])

OFFICIAL_STATUS = "已导出"


def landed_row_count(points: list) -> int:
    """落地采样行数接缝：默认与绑定点数逐点相等。

    缺行复现夹具可在测试中替换本函数，模拟落地环节短于绑定点数，
    以驱动行数门闸拒收。
    """
    return len(points)


@router.get("/api/spectrum")
def list_exports(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.*, b.batch_code,
                      json_array_length(COALESCE(b.temp_points,'[]')) AS bound_point_count
               FROM spectrum_exports s
               JOIN temp_batches b ON b.id=s.batch_id ORDER BY s.id DESC"""
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["row_gate_ok"] = (
                d["status"] == OFFICIAL_STATUS
                and d["point_count"] == d["bound_point_count"]
            )
            items.append(d)
        return {"items": items}


@router.post("/api/spectrum/export")
def export_spectrum(body: SpectrumIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"batch:{body.batch_id}"
    # 裸连接 + try/finally：异常路径自行回滚并自提交释放锁，
    # 不依赖 with 上下文在 HTTPException 下的提交/回滚行为。
    conn = connect()
    try:
        # 双人同批拉取：批次锁串行，后到仅存争用痕、不落第二份正式册。
        acquire_business_lock(conn, lock_key, user)
        batch = conn.execute(
            "SELECT * FROM temp_batches WHERE id=?", (body.batch_id,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        if batch["status"] != "已封批":
            raise HTTPException(409, "须先封批再导出温谱")
        points = json.loads(batch["temp_points"] or "[]")
        bound_n = len(points)
        # 行数门闸：逐点计数校验，落地行数与封批代绑定点数不一致即拒收。
        landed_n = landed_row_count(points)
        if landed_n != bound_n:
            # 正式册零行、导出态零改：不插任何导出记录、不改批次状态。
            write_audit(
                conn,
                user,
                "spectrum_export_rejected",
                batch["batch_code"],
                {
                    "reason": "行数门闸偏航",
                    "bound_point_count": bound_n,
                    "landed_row_count": landed_n,
                },
            )
            conn.commit()
            raise HTTPException(
                409,
                f"行数门闸拒收：落地行数{landed_n}≠绑定点数{bound_n}，正式册零行",
            )
        payload = json.dumps(
            {
                "batch_code": batch["batch_code"],
                "generation": batch["generation"],
                "points": points,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        ref = f"SP-{batch['batch_code']}-G{batch['generation']}"
        dup = conn.execute(
            "SELECT id FROM spectrum_exports WHERE export_ref=?", (ref,)
        ).fetchone()
        if dup:
            # 持批次锁仍撞上同代导出：后到争用仅存痕，不落第二份正式册。
            conn.execute(
                """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
                   VALUES (?,?,?,?,?)""",
                (lock_key, user, "争用", f"同代温谱已导出 ref={ref}", now_iso()),
            )
            conn.commit()
            raise HTTPException(409, "该代温谱已导出")
        cur = conn.execute(
            """INSERT INTO spectrum_exports(batch_id,export_ref,point_count,checksum,
               status,created_at) VALUES (?,?,?,?,?,?)""",
            (body.batch_id, ref, bound_n, checksum, OFFICIAL_STATUS, now_iso()),
        )
        write_audit(conn, user, "spectrum_export", ref, {"checksum": checksum})
        conn.commit()
        return {
            "id": cur.lastrowid,
            "export_ref": ref,
            "point_count": bound_n,
            "checksum": checksum,
            "status": OFFICIAL_STATUS,
        }
    finally:
        # 异常路径先回滚未决事务，再自提交释放批次锁，杜绝残锁。
        release_committed(conn, lock_key, user)
        conn.close()


@router.get("/api/spectrum/{export_ref}")
def get_export(export_ref: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, b.temp_points, b.batch_code,
                      json_array_length(COALESCE(b.temp_points,'[]')) AS bound_point_count
               FROM spectrum_exports s
               JOIN temp_batches b ON b.id=s.batch_id WHERE s.export_ref=?""",
            (export_ref,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "导出不存在")
        data = dict(row)
        data["temp_points"] = json.loads(data["temp_points"] or "[]")
        data["row_gate_ok"] = (
            data["status"] == OFFICIAL_STATUS
            and data["point_count"] == data["bound_point_count"]
        )
        return data
