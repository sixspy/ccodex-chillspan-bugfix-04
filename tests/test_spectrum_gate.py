"""BUGFIX-04 行数门闸红测队列（先于补丁落库，先红后绿）。

口径单轨拍板：温谱导出行数绑定走甲路径——
采样点数与落地行数不一致则温谱正式册零行拒收、导出态零改；
影子手稿（非“已导出”状态）永不晋升、永不下发为正式导出；
同批并发拉取须批次锁串行，后到仅存争用痕、不落第二份正式册。

红测甲 test_short_row_rejected_no_official_register：盯短行仍落正式册。
红测乙 test_shadow_manuscript_not_issued_as_official：盯影子手稿未晋升却已下发。
争用红测 test_concurrent_spectrum_export_serialized_by_batch_lock：
盯二人同批无锁各下一份正式导出。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db, now_iso


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _sealed_batch(c: TestClient, h: dict, code: str, n: int = 5) -> dict:
    """造一个合法封批批次：CK-HN-03 探头校准有效、温点在带内、无开放游标。"""
    chambers = c.get("/api/chambers", headers=h).json()["items"]
    ch = next(x for x in chambers if x["chamber_code"] == "CK-HN-03")
    probe = c.get(f"/api/probes?chamber_id={ch['id']}", headers=h).json()["items"][0]
    points = [{"t": f"{8 + i:02d}:00", "c": 3.0 + i * 0.5} for i in range(n)]
    batch = c.post(
        "/api/batches",
        headers=h,
        json={
            "chamber_id": ch["id"],
            "probe_id": probe["id"],
            "cargo_ref": f"行数门闸夹具 {code}",
            "temp_points": points,
            "batch_code": code,
        },
    ).json()
    seal = c.post("/api/seals", headers=h, json={"batch_id": batch["id"]})
    assert seal.status_code == 200
    return batch


def _export_rows(batch_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM spectrum_exports WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# 红测甲：缺行复现夹具——落地采样行短于封批代绑定点数时，正式册零行拒收。
def test_short_row_rejected_no_official_register(monkeypatch):
    import spectrum_svc

    c = TestClient(app)
    h = _auth(c)
    batch = _sealed_batch(c, h, "TB-GATE-A-001", n=5)

    # 复现夹具：落地环节缺 2 行（埋雷口径 short_n = 绑定点数 - 2）。
    # 红期该接缝尚未被导出口径调用，自然复现“短行仍标已导出”；
    # 补丁后接缝被行数门闸调用，注入即触发拒收。
    monkeypatch.setattr(
        spectrum_svc,
        "landed_row_count",
        lambda points: len(points) - 2,
        raising=False,
    )

    r = c.post(
        "/api/spectrum/export", headers=h, json={"batch_id": batch["id"]}
    )

    # 缺行导出三断言：
    # 1) 门闸拒收（不得回 2xx 成功回执）
    assert r.status_code == 409
    # 2) 温谱正式册零行：没有任何导出行落库
    assert _export_rows(batch["id"]) == []
    # 3) 导出态零改：批次仍停在已封批，未被导出动作推动状态
    detail = c.get(f"/api/batches/{batch['id']}", headers=h).json()["batch"]
    assert detail["status"] == "已封批"


# 红测乙：影子导出手稿未晋升不得对外下发为正式导出。
def test_shadow_manuscript_not_issued_as_official():
    c = TestClient(app)
    h = _auth(c)
    batch = _sealed_batch(c, h, "TB-GATE-B-001", n=4)
    bid = batch["id"]

    # 夹具：直接塞一份未晋升的影子手稿（4 个绑定点只落 2 行，状态非已导出）。
    with connect() as conn:
        conn.execute(
            """INSERT INTO spectrum_exports(batch_id,export_ref,point_count,checksum,
               status,created_at) VALUES (?,?,?,?,?,?)""",
            (bid, f"SP-SHADOW-{bid}", 2, "shadow0000000000", "影子手稿", now_iso()),
        )
        conn.commit()

    listed = c.get("/api/spectrum", headers=h).json()["items"]
    mine = [x for x in listed if x["batch_id"] == bid]
    proof = c.get("/api/proof/batch/TB-GATE-B-001", headers=h).json()

    # 1) 影子手稿同源可读但保持真身：该批次无任何“已导出”正式行下发
    assert len(mine) == 1
    assert mine[0]["status"] == "影子手稿"
    assert all(x["status"] != "已导出" for x in mine)
    # 2) 互证口径不得把未晋升手稿判成齐全：spectrum_matches_seal 必须为 False
    assert proof["spectrum_matches_seal"] is False
    # 3) 互证须单轨暴露绑定点数与落地行数，二者不得粉饰成相等
    assert proof["bound_point_count"] == 4
    assert proof["landed_point_count"] != 4


# 争用红测：双人同批并发拉温谱，须批次锁串行；后到只存争用痕、不落第二份正式册。
def test_concurrent_spectrum_export_serialized_by_batch_lock():
    c = TestClient(app)
    h = _auth(c, "admin")
    batch = _sealed_batch(c, h, "TB-GATE-C-001", n=3)
    bid = batch["id"]
    barrier = Barrier(2)

    def pull(user: str) -> int:
        cc = TestClient(app)
        hh = _auth(cc, user)
        barrier.wait(timeout=10)  # 双请求同刻放行，强制争用同把批次锁
        r = cc.post(
            "/api/spectrum/export", headers=hh, json={"batch_id": bid}
        )
        return r.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(pull, ["admin", "ops"]))

    # 1) 一胜一拒：只允许一份正式导出成功
    assert sorted(codes) == [200, 409]
    # 2) 正式册仅一份，且为已导出
    rows = _export_rows(bid)
    assert len(rows) == 1
    assert rows[0]["status"] == "已导出"
    # 3) 后到争用仅存痕：contention_log 留有该批次锁的争用记录
    with connect() as conn:
        cont = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?",
            (f"batch:{bid}",),
        ).fetchone()["c"]
    assert cont >= 1


# 收工核验：血缘/对账/温谱/批次/封批五处前台须同源读出绑定点数、落地行数与门闸态。
def test_five_views_same_source_row_gate(monkeypatch):
    import spectrum_svc

    c = TestClient(app)
    h = _auth(c)
    batch = _sealed_batch(c, h, "TB-GATE-D-001", n=6)
    bid = batch["id"]

    # 先成一笔全量正式导出。
    ok = c.post("/api/spectrum/export", headers=h, json={"batch_id": bid})
    assert ok.status_code == 200

    # 再以缺行夹具拉一次：门闸拒收，导出态零改、正式册不加行。
    monkeypatch.setattr(
        spectrum_svc, "landed_row_count", lambda points: len(points) - 1
    )
    rejected = c.post("/api/spectrum/export", headers=h, json={"batch_id": bid})
    assert rejected.status_code == 409

    # 登记一条血缘边，使血缘视图纳入该批次。
    edge = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_batch_id": bid,
            "to_batch_id": None,
            "edge_type": "分装衍生",
            "detail": "门闸同源核验",
        },
    )
    assert edge.status_code == 200

    # 1) 批次视图
    blist = [b for b in c.get("/api/batches", headers=h).json()["items"] if b["id"] == bid][0]
    bdetail = c.get(f"/api/batches/{bid}", headers=h).json()["batch"]
    # 2) 封批视图
    seal = [s for s in c.get("/api/seals", headers=h).json()["items"] if s["batch_id"] == bid][0]
    # 3) 温谱视图
    sp = [x for x in c.get("/api/spectrum", headers=h).json()["items"] if x["batch_id"] == bid]
    # 4) 血缘视图
    lg = [e for e in c.get("/api/lineage", headers=h).json()["items"] if e["from_batch_id"] == bid][0]
    # 5) 对账视图（批次互证）
    proof = c.get("/api/proof/batch/TB-GATE-D-001", headers=h).json()

    # 五处绑定点数同源 = 6
    assert blist["bound_point_count"] == 6
    assert bdetail["bound_point_count"] == 6
    assert seal["bound_point_count"] == 6
    assert sp[0]["bound_point_count"] == 6
    assert lg["from_bound_point_count"] == 6
    assert proof["bound_point_count"] == 6

    # 五处落地行数同源 = 6，缺行拒收未污染正式册
    assert blist["landed_point_count"] == 6
    assert bdetail["landed_point_count"] == 6
    assert seal["landed_point_count"] == 6
    assert sp[0]["point_count"] == 6
    assert lg["from_landed_point_count"] == 6
    assert proof["landed_point_count"] == 6

    # 门闸态五处同源为齐
    assert blist["row_gate_ok"] is True
    assert bdetail["row_gate_ok"] is True
    assert seal["row_gate_ok"] is True
    assert sp[0]["row_gate_ok"] is True
    assert lg["from_row_gate_ok"] is True
    assert proof["spectrum_matches_seal"] is True

    # 正式册始终只有一笔，缺行那笔零行拒收
    assert len(sp) == 1
    assert sp[0]["status"] == "已导出"


# 门闸拒收不得残留批次锁：缺行拒收后同批点数补齐仍须能整笔导出成功。
def test_rejection_releases_batch_lock(monkeypatch):
    import spectrum_svc

    c = TestClient(app)
    h = _auth(c)
    batch = _sealed_batch(c, h, "TB-GATE-E-001", n=5)
    bid = batch["id"]

    monkeypatch.setattr(
        spectrum_svc, "landed_row_count", lambda points: len(points) - 1
    )
    rejected = c.post(
        "/api/spectrum/export", headers=h, json={"batch_id": bid}
    )
    assert rejected.status_code == 409

    # 残锁检查：chamber_locks 中不得再持有该批次锁（连接即查即关，勿占库锁）
    conn = connect()
    try:
        left = conn.execute(
            "SELECT COUNT(*) c FROM chamber_locks WHERE lock_key=?",
            (f"batch:{bid}",),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert left == 0

    # 直接还原接缝而不用 monkeypatch.undo()：后者会把 db.DB_PATH 也回滚到
    # 只读的默认库 /workspace/data/app.db，导致后续请求连错库。
    monkeypatch.setattr(
        spectrum_svc, "landed_row_count", lambda points: len(points)
    )
    ok = c.post("/api/spectrum/export", headers=h, json={"batch_id": bid})
    assert ok.status_code == 200
    assert ok.json()["point_count"] == 5
    rows = _export_rows(bid)
    assert len(rows) == 1 and rows[0]["status"] == "已导出"
