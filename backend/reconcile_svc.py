"""仓间配额 vs 在批差额对账 + 互证只读。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])


def _chamber_recon(conn, chamber) -> dict:
    cid = chamber["id"]
    quota = int(chamber["quota_batches"])
    active = conn.execute(
        """SELECT COUNT(*) c FROM temp_batches
           WHERE chamber_id=? AND status NOT IN ('已封批','作废')""",
        (cid,),
    ).fetchone()["c"]
    sealed = conn.execute(
        """SELECT COUNT(*) c FROM temp_batches
           WHERE chamber_id=? AND status='已封批'""",
        (cid,),
    ).fetchone()["c"]
    gap = quota - active
    ok = active <= quota
    return {
        "chamber_id": cid,
        "chamber_code": chamber["chamber_code"],
        "region": chamber["region"],
        "quota_batches": quota,
        "active_batches": active,
        "sealed_batches": sealed,
        "quota_left": gap,
        "balanced": ok,
        "proof": {
            "formula": "active_batches <= quota_batches",
            "left": active,
            "right": quota,
        },
    }


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        chambers = conn.execute("SELECT * FROM cold_chambers ORDER BY id").fetchall()
        items = [_chamber_recon(conn, c) for c in chambers]
        bad = [i for i in items if not i["balanced"]]
        return {
            "items": items,
            "summary": {
                "total": len(items),
                "balanced": len(items) - len(bad),
                "unbalanced": len(bad),
            },
        }


@router.get("/api/reconcile/{chamber_code}")
def reconcile_one(chamber_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        ch = conn.execute(
            "SELECT * FROM cold_chambers WHERE chamber_code=?", (chamber_code,)
        ).fetchone()
        if not ch:
            raise HTTPException(404, "仓间不存在")
        return _chamber_recon(conn, ch)


@router.get("/api/proof/batch/{batch_code}")
def batch_proof(batch_code: str, authorization: AuthHeader = None):
    """批次互证：封批代 + 温谱导出 + 开放游标。

    行数门闸单轨口径：正式册只认 status='已导出' 且落地行数与封批代
    绑定点数逐点相等的行；影子手稿（任何非“已导出”状态）永不晋升，
    不得让互证变绿。
    """
    require_user(authorization)
    with connect() as conn:
        batch = conn.execute(
            "SELECT * FROM temp_batches WHERE batch_code=?", (batch_code,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        open_exc = conn.execute(
            """SELECT COUNT(*) c FROM excursion_cursors
               WHERE batch_id=? AND status='开放'""",
            (batch["id"],),
        ).fetchone()["c"]
        bound_n = conn.execute(
            "SELECT json_array_length(COALESCE(temp_points,'[]')) n FROM temp_batches WHERE id=?",
            (batch["id"],),
        ).fetchone()["n"]
        export_rows = conn.execute(
            "SELECT point_count, status FROM spectrum_exports WHERE batch_id=?",
            (batch["id"],),
        ).fetchall()
        # 正式册：仅“已导出”行；影子手稿不晋升、不计入正式导出。
        official = [r for r in export_rows if r["status"] == "已导出"]
        shadow_n = len(export_rows) - len(official)
        landed_n = official[-1]["point_count"] if official else 0
        exports = len(official)
        sealed = batch["status"] == "已封批"
        spectrum_ok = sealed and exports >= 1 and landed_n == bound_n
        return {
            "batch_code": batch_code,
            "status": batch["status"],
            "generation": batch["generation"],
            "open_excursions": open_exc,
            "spectrum_exports": exports,
            "shadow_manuscripts": shadow_n,
            "bound_point_count": bound_n,
            "landed_point_count": landed_n,
            "sealed": sealed,
            "excursion_closed": open_exc == 0,
            "spectrum_matches_seal": spectrum_ok,
            "proof": {
                "formula": "sealed => 正式导出>=1 and 落地行数==绑定点数 and open_excursions==0",
                "sealed": sealed,
                "open_excursions": open_exc,
                "exports": exports,
                "bound_point_count": bound_n,
                "landed_point_count": landed_n,
            },
        }