import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.modules.correction.service import (archive_correction, record_line,
                                            submit_correction)

SETTINGS = {"length_m": 1000, "zero_a_m": 0, "zero_b_m": 0, "pair_tol_m": 4,
            "design_tol_m": 3, "reference_limit_db": "0.200"}
DESIGN = "joint,distance,unit,limit_db\nJ1,100,m,0.200\nJ2,300,m,0.200\n"


def material(a_rows, b_rows):
    a = "event,distance,unit,loss_db\n" + "\n".join(a_rows)
    b = "event,distance,unit,loss_db\n" + "\n".join(b_rows)
    return {"traces": {"a": a, "b": b, "design": DESIGN}, "settings": SETTINGS}


LINE_ONE = material(["A1,100,m,0.05", "A2,300,m,0.06"],
                    ["B1,900,m,0.05", "B2,700,m,0.06"])
LINE_TWO = material(["C1,100,m,0.05", "C2,300,m,0.05"],
                    ["D1,900,m,0.05", "D2,700,m,0.05"])
# 与 LINE_ONE 交换后：LINE_ONE 超限，LINE_TWO 回到合格；各自单独留档均合格
LINE_TWO_SWAPPED_BAD = {**LINE_TWO,
                        "traces": {**LINE_TWO["traces"],
                                   "b": "event,distance,unit,loss_db\nD1,900,m,0.40\nD2,700,m,0.40"}}


def register(client, code, payload):
    response = client.post(f"/api/lines/{code}/recordings", json=payload)
    assert response.status_code in (200, 201)
    return response.json


def line(client, code):
    return client.get(f"/api/lines/{code}").json


def test_line_recording_creates_versioned_effective_archive(client):
    first = register(client, "L1", LINE_ONE)
    assert first["version"] == 1 and first["created"] is True
    second = register(client, "L1", LINE_TWO)
    assert second["version"] == 2
    assert line(client, "L1")["inspection_id"] == second["inspection_id"]


def test_unknown_line_returns_404(client):
    response = client.get("/api/lines/nope")
    assert (response.status_code, response.json["code"]) == (404, "LINE_NOT_FOUND")


def test_correction_needs_two_distinct_lines(client):
    register(client, "L1", LINE_ONE)
    response = client.post("/api/corrections", json={"lines": ["L1", "L1"]})
    assert (response.status_code, response.json["code"]) == (422, "CORRECTION_NEEDS_TWO_LINES")


def test_missing_line_rejected(client):
    register(client, "L1", LINE_ONE)
    response = client.post("/api/corrections", json={"lines": ["L1", "GHOST"]})
    assert (response.status_code, response.json["code"]) == (404, "LINE_NOT_FOUND")


def test_swap_recomputes_both_lines_and_suspends_when_one_over_limit(client):
    register(client, "L1", LINE_ONE)
    register(client, "L2", LINE_TWO_SWAPPED_BAD)
    response = client.post("/api/corrections", json={"lines": ["L1", "L2"]})
    assert response.status_code == 201
    document = response.json
    assert document["status"] == "SUSPENDED"
    by_line = {row["line_code"]: row for row in document["lines"]}
    assert by_line["L1"]["approved"] is False
    assert "LOSS_OVER_LIMIT" in by_line["L1"]["blockers"]
    assert by_line["L2"]["approved"] is True
    # 签发页面能看到各自的复算结果
    assert by_line["L1"]["recalc"]["over_limit"] == 2
    assert by_line["L2"]["recalc"]["passed"] == 2
    # 暂缓期间两线路仍指向留档旧版
    assert line(client, "L1")["version"] == document["pinned"]["L1"]
    assert line(client, "L2")["version"] == document["pinned"]["L2"]


def test_suspended_correction_cannot_be_archived(client):
    register(client, "L1", LINE_ONE)
    register(client, "L2", LINE_TWO_SWAPPED_BAD)
    correction = client.post("/api/corrections", json={"lines": ["L1", "L2"]}).json
    response = client.post(f"/api/corrections/{correction['correction_id']}/archive")
    assert (response.status_code, response.json["code"]) == (422, "CORRECTION_LINE_OVER_LIMIT")
    # 两条线路的有效留档均未变化
    assert line(client, "L1")["inspection_id"] == correction["lines"][0]["pinned_inspection_id"]
    assert line(client, "L2")["inspection_id"] == correction["lines"][1]["pinned_inspection_id"]


def test_two_corrections_sharing_a_line_only_one_accepted(client):
    register(client, "L1", LINE_ONE)
    register(client, "L2", LINE_TWO)
    register(client, "L3", LINE_ONE)
    first = client.post("/api/corrections", json={"lines": ["L1", "L2"]})
    second = client.post("/api/corrections", json={"lines": ["L2", "L3"]})
    assert first.status_code == 201
    assert (second.status_code, second.json["code"]) == (409, "CORRECTION_LINE_BUSY")
    assert second.json["source"] == first.json["correction_id"]


def test_concurrent_corrections_on_same_line_publish_single_lock(client):
    register(client, "L1", LINE_ONE)
    register(client, "L2", LINE_TWO)
    register(client, "L3", LINE_ONE)
    db = client.application.config["DATABASE"]

    def open_correction(pair):
        app = client.application
        with app.app_context():
            try:
                return submit_correction(db, pair)["status"]
            except Exception as exc:  # pragma: no cover - asserted below
                return exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(open_correction, [["L1", "L2"], ["L2", "L3"], ["L1", "L3"]]))
    assert outcomes.count("READY_TO_ARCHIVE") == 1
    assert outcomes.count("CORRECTION_LINE_BUSY") == 2
    with sqlite3.connect(db) as raw:
        assert raw.execute("SELECT COUNT(*) FROM line_locks").fetchone()[0] == 2


def test_new_measurement_before_archive_returns_correction_for_recheck(client):
    register(client, "L1", LINE_ONE)
    register(client, "L2", LINE_TWO)
    correction = client.post("/api/corrections", json={"lines": ["L1", "L2"]}).json
    # 留档前任一线路录入了新测量记录
    register(client, "L2", LINE_ONE)
    response = client.post(f"/api/corrections/{correction['correction_id']}/archive")
    assert (response.status_code, response.json["code"]) == (409, "LINE_MEASUREMENT_CHANGED")
    returned = client.get(f"/api/corrections/{correction['correction_id']}").json
    assert returned["status"] == "RETURNED" and returned["return_reason"] == "NEW_MEASUREMENT_PENDING"


def test_returned_correction_recheck_then_archive_switches_both_lines(client):
    register(client, "L1", LINE_ONE)
    register(client, "L2", LINE_TWO)
    correction = client.post("/api/corrections", json={"lines": ["L1", "L2"]}).json
    cid = correction["correction_id"]
    # 留档前任一线路录入新测量：纠正单退回重核，两线路保持旧版
    register(client, "L1", LINE_TWO)
    blocked = client.post(f"/api/corrections/{cid}/archive")
    assert (blocked.status_code, blocked.json["code"]) == (409, "LINE_MEASUREMENT_CHANGED")
    old_versions = dict(correction["pinned"])
    # 新测量已并入有效留档，重核后以当前版本重新钉版并恢复可归档
    recheck = client.post(f"/api/corrections/{cid}/recheck")
    assert recheck.status_code == 200 and recheck.json["status"] == "READY_TO_ARCHIVE"
    assert recheck.json["pinned"]["L1"] == old_versions["L1"] + 1
    response = client.post(f"/api/corrections/{cid}/archive")
    assert response.status_code == 200 and response.json["status"] == "ARCHIVED"
    assert line(client, "L1")["version"] == old_versions["L1"] + 2
    assert line(client, "L2")["version"] == old_versions["L2"] + 1


def test_both_pass_archive_updates_both_lines_and_releases_locks(client):
    first = register(client, "L1", LINE_ONE)
    second = register(client, "L2", LINE_TWO)
    correction = client.post("/api/corrections", json={"lines": ["L1", "L2"]}).json
    assert correction["status"] == "READY_TO_ARCHIVE"
    response = client.post(f"/api/corrections/{correction['correction_id']}/archive")
    assert response.status_code == 200 and response.json["status"] == "ARCHIVED"
    assert line(client, "L1")["version"] == first["version"] + 1
    assert line(client, "L2")["version"] == second["version"] + 1
    # 新留档即交换远端原件后的复算结果
    assert line(client, "L1")["inspection_id"] == correction["lines"][0]["inspection_id"]
    with sqlite3.connect(client.application.config["DATABASE"]) as raw:
        assert raw.execute("SELECT COUNT(*) FROM line_locks").fetchone()[0] == 0
    # 锁释放后同一对线路可以再开纠正单
    again = client.post("/api/corrections", json={"lines": ["L1", "L2"]})
    assert again.status_code == 201


def test_certificate_uses_effective_archive_after_switch(client):
    register(client, "L1", LINE_ONE)
    register(client, "L2", LINE_TWO_SWAPPED_BAD)
    correction = client.post("/api/corrections", json={"lines": ["L1", "L2"]}).json
    assert correction["status"] == "SUSPENDED"
    # 暂缓期间签发依据旧留档：L1 自身合格，L2 自身即超限
    old_l1 = line(client, "L1")["inspection_id"]
    old_l2 = line(client, "L2")["inspection_id"]
    assert client.get(f"/api/certificate/{old_l1}").json["approved"] is True
    assert client.get(f"/api/certificate/{old_l2}").json["approved"] is False
    # 超限线路重新录入合格测量，纠正单退回重核；重核后两条都合格
    record_line(client.application.config["DATABASE"], "L2", LINE_TWO)
    cid = correction["correction_id"]
    blocked = client.post(f"/api/corrections/{cid}/archive")
    assert blocked.status_code == 409 and blocked.json["code"] == "LINE_MEASUREMENT_CHANGED"
    recheck = client.post(f"/api/corrections/{cid}/recheck").json
    assert recheck["status"] == "READY_TO_ARCHIVE"
    archived = client.post(f"/api/corrections/{cid}/archive").json
    assert archived["status"] == "ARCHIVED"
    # 签发以生效的留档为准：两线路都切到交换后的新版本，且均合格
    new_l1 = line(client, "L1")
    new_l2 = line(client, "L2")
    assert new_l1["inspection_id"] != old_l1 and new_l2["inspection_id"] != old_l2
    assert client.get(f"/api/certificate/{new_l1['inspection_id']}").json["approved"] is True
    assert client.get(f"/api/certificate/{new_l2['inspection_id']}").json["approved"] is True
    # 归档后纠正单与当前线路版本一致
    assert archived["pinned"] == {"L1": new_l1["version"], "L2": new_l2["version"]}


def test_archive_failure_is_atomic_both_lines_keep_old_version(client, monkeypatch):
    register(client, "L1", LINE_ONE)
    register(client, "L2", LINE_TWO)
    correction = client.post("/api/corrections", json={"lines": ["L1", "L2"]}).json
    pinned = dict(correction["pinned"])
    db = client.application.config["DATABASE"]
    from app.modules.correction import service

    def boom(db, manifest):
        raise sqlite3.OperationalError("simulated write interruption")

    monkeypatch.setattr(service, "publish_with", boom)
    with pytest.raises(sqlite3.OperationalError):
        archive_correction(db, correction["correction_id"])
    # 写入中断恢复：两条线路同见旧版
    assert line(client, "L1")["version"] == pinned["L1"]
    assert line(client, "L2")["version"] == pinned["L2"]
    stored = client.get(f"/api/corrections/{correction['correction_id']}").json
    assert stored["status"] == "READY_TO_ARCHIVE"


def test_concurrent_archives_switch_both_lines_once(tmp_path):
    from app.main import create_app
    app = create_app(tmp_path / "db.sqlite")
    db = app.config["DATABASE"]
    record_line(db, "L1", LINE_ONE)
    record_line(db, "L2", LINE_TWO)
    with app.app_context():
        correction = submit_correction(db, ["L1", "L2"])
        cid = correction["correction_id"]

    def attempt(_):
        with app.app_context():
            try:
                return archive_correction(db, cid)["status"]
            except Exception as exc:  # pragma: no cover - asserted below
                return getattr(exc, "code", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))
    assert sorted(outcomes) == ["ARCHIVED", "CORRECTION_NOT_ARCHIVABLE"]
    with sqlite3.connect(db) as raw:
        assert raw.execute("SELECT version FROM lines WHERE code='L1'").fetchone()[0] == 2
        assert raw.execute("SELECT version FROM lines WHERE code='L2'").fetchone()[0] == 2
        assert raw.execute("SELECT COUNT(*) FROM line_locks").fetchone()[0] == 0


def test_idempotent_line_recording_reuses_inspection(client):
    first = register(client, "L1", LINE_ONE)
    again = register(client, "L1", LINE_ONE)
    assert again["version"] == first["version"] + 1 and again["created"] is False
    assert again["inspection_id"] == first["inspection_id"]


def test_corrections_listing_and_detail(client):
    register(client, "L1", LINE_ONE)
    register(client, "L2", LINE_TWO)
    created = client.post("/api/corrections", json={"lines": ["L1", "L2"]}).json
    listing = client.get("/api/corrections").json["corrections"]
    assert [row["correction_id"] for row in listing] == [created["correction_id"]]
    detail = client.get(f"/api/corrections/{created['correction_id']}")
    assert detail.json["remote_source"] == "b"
    missing = client.get("/api/corrections/unknown")
    assert (missing.status_code, missing.json["code"]) == (404, "CORRECTION_NOT_FOUND")
