"""A plan is scored again at approval time (#175): never approve one whose numbers moved.

Legacy mode: POST /api/plans/{id}/approve re-scores the cached plan from now (planner.rescore).
Dataset mode: POST /api/eog/plans/{event_id}/approve with plan_at re-scores the plan shown then.
Either way a plan that no longer keeps every tank below shutdown, or whose cost moved by more
than $1, gets a 409 with one sentence and the fresh plan, and nothing is approved.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import now
from backend.app.eog import plan as eog_plan
from backend.app.eog import service
from backend.app.main import app
from backend.app.planner import make_plans, rescore, stale_reason
from backend.app.scenario import load_scenario
from backend.tests.eog_fixture import write_dataset

REAL_ROOT = os.environ.get("FREEBOARD_EOG_DIR", "")   # read before any test changes the env
client = TestClient(app)


# ---------- legacy mode (static clock) ----------

@pytest.fixture
def static(monkeypatch):
    monkeypatch.setenv("FREEBOARD_ONE_CLOCK", "0")
    monkeypatch.delenv("FREEBOARD_EOG_DIR", raising=False)
    for var in ("FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    sc = load_scenario()
    now.set_static(sc)
    yield sc
    now.set_static(load_scenario())


def applied() -> int:
    return len(client.get("/api/state").json().get("applied_actions", []))


def test_rescore_of_an_unchanged_plan_is_the_same_plan(static):
    for p in make_plans(static)["plans"]:
        again = rescore(static, p)
        assert again["id"] == p["id"] and again["cost"] == p["cost"] and again["cannot_save"] == p["cannot_save"]
        assert stale_reason(p, again, "01:47") is None


def test_unchanged_plan_is_approved(static):
    plan = client.post("/api/plans", json={}).json()["plans"][0]
    r = client.post(f"/api/plans/{plan['id']}/approve")
    assert r.status_code == 200 and r.json()["recorded_actions"] == len(plan["actions"])


def test_cost_moved_is_409_with_the_fresh_plan_and_nothing_recorded(static):
    plans = client.post("/api/plans", json={}).json()["plans"]
    first, second = plans[0], plans[1]
    assert client.post(f"/api/plans/{first['id']}/approve").status_code == 200   # its actions are now under way
    before = applied()
    r = client.post(f"/api/plans/{second['id']}/approve")
    assert r.status_code == 409, r.text
    body = r.json()
    assert isinstance(body["detail"], str) and "out of date" in body["detail"] and "Nothing was approved" in body["detail"]
    assert body["plan"]["id"] == second["id"]
    assert abs(body["plan"]["cost"]["total_usd"] - second["cost"]["total_usd"]) > 1
    assert body["was"]["cost"] == second["cost"] and body["plans"] and body["clock"]
    assert applied() == before                                                  # nothing approved
    # The cache now holds the re-scored plan: approving it again goes ahead on the new numbers.
    r = client.post(f"/api/plans/{second['id']}/approve")
    assert r.status_code == 200 and r.json()["plan_id"] == second["id"]


def test_a_plan_that_no_longer_saves_a_tank_is_409(static):
    plan = client.post("/api/plans", json={}).json()["plans"][0]
    assert plan["keeps_running"] and plan["actions"]
    worse = copy.deepcopy(static)
    worse["initial_levels_pct"]["TANK_3"] = 89.5      # half a point under shutdown: no lead time is short enough
    now.set_static(worse)
    r = client.post(f"/api/plans/{plan['id']}/approve")
    assert r.status_code == 409, r.text
    body = r.json()
    assert "no longer keeps every tank below shutdown" in body["detail"] and "Tank 3" in body["detail"]
    assert "TANK_3" in body["plan"]["cannot_save"] and not body["plan"]["keeps_running"]
    assert applied() == 0


def test_stale_reason_rules():
    old = {"id": "plan_x", "cost": {"total_usd": 1000.0}, "cannot_save": []}
    assert stale_reason(old, {**old, "cost": {"total_usd": 1001.0}}, "02:00") is None        # $1 is not a move
    assert "$1,002" in stale_reason(old, {**old, "cost": {"total_usd": 1001.5}}, "02:00")
    assert "Tank 3" in stale_reason(old, {**old, "cannot_save": ["TANK_3"]}, "02:00")
    # A best-effort plan that already couldn't save Tank 3 isn't stale for that alone.
    best = {**old, "cannot_save": ["TANK_3"]}
    assert stale_reason(best, dict(best), "02:00") is None


# ---------- dataset mode ----------

@pytest.fixture
def dataset(tmp_path, monkeypatch):
    for var in ("FREEBOARD_EOG_DATASET", "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    base = tmp_path / "eog"
    write_dataset(base / "alpha", seed=3, hours=24, repair_hours=5,
                  problems=[{"type": "pump_failure", "facility": "Q7_FACILITY_A", "onset_h": 21.5, "level_pct": 60}])
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    service.SERVICE.reset()
    assert client.post("/api/eog/datasets/activate", json={"id": "alpha"}).status_code == 200
    yield base
    service.SERVICE.reset()


def failure() -> dict:
    return next(a for a in client.get("/api/eog/alerts").json()["alerts"] if a["category"] == "pump_failure")


def test_plan_at_the_approval_moment_changes_nothing(dataset):
    a = failure()
    at = client.get("/api/eog/clock").json()["at"]
    plain = client.post(f"/api/eog/plans/{a['event_id']}/approve", json={}).json()
    same = client.post(f"/api/eog/plans/{a['event_id']}/approve", json={"plan_at": at}).json()
    assert {k: same[k] for k in ("summary", "actions", "cost", "steps")} == {k: plain[k] for k in ("summary", "actions", "cost", "steps")}
    assert client.post(f"/api/eog/plans/{a['event_id']}/approve", json={"plan_at": "soon"}).status_code == 422


def test_no_plan_showing_then_is_409(dataset):
    a = failure()
    start = client.get("/api/eog/clock").json()["start"]
    r = client.post(f"/api/eog/plans/{a['event_id']}/approve", json={"plan_at": start + 3600})
    assert r.status_code == 409 and "was showing" in r.json()["detail"]
    assert r.json()["plan"]["summary"] == a["action"]["summary"] and r.json()["seen"] is None
    assert failure()["approval"] is None                              # nothing approved


def test_a_shown_plan_that_now_trips_is_409(dataset, monkeypatch):
    a = failure()
    at = client.get("/api/eog/clock").json()["at"]
    real = eog_plan.simulate

    def trips(ds, fid, start, level, repair_end, actions, **kw):
        out = real(ds, fid, start, level, repair_end, actions, **kw)
        return {**out, "trip_at": int(start) + 600}                 # pretend the late start trips in 10 min
    monkeypatch.setattr(eog_plan, "simulate", trips)
    r = client.post(f"/api/eog/plans/{a['event_id']}/approve", json={"plan_at": at - 300})
    assert r.status_code == 409, r.text
    body = r.json()
    assert "no longer keeps" in body["detail"] and body["rescored"]["trips"] is True
    assert body["plan"]["summary"] == a["action"]["summary"] and body["seen"]["plan_at"] == at - 300
    assert failure()["approval"] is None


def test_a_shown_plan_whose_cost_moved_is_409(dataset, monkeypatch):
    a = failure()
    at = client.get("/api/eog/clock").json()["at"]
    real = eog_plan.simulate
    monkeypatch.setattr(eog_plan, "simulate",
                        lambda *x, **k: {**real(*x, **k), "trip_at": None, "cost": real(*x, **k)["cost"] + 250.0})
    r = client.post(f"/api/eog/plans/{a['event_id']}/approve", json={"plan_at": at - 300})
    assert r.status_code == 409 and "is out of date" in r.json()["detail"]
    assert failure()["approval"] is None


# ---------- the sponsor's data ----------

@pytest.fixture
def snapshot(monkeypatch):
    base = Path(REAL_ROOT) if REAL_ROOT else None
    if base is None or not (base / "history" / "snapshot_2026-10-10T0900").is_dir():
        pytest.skip("FREEBOARD_EOG_DIR does not point at the sponsor repo")
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    for var in ("FREEBOARD_EOG_DATASET", "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    service.SERVICE.reset()
    assert client.post("/api/eog/datasets/activate", json={"id": "history/snapshot_2026-10-10T0900"}).status_code == 200
    yield 1791622800          # the snapshot's now, 2026-10-10 09:00 UTC
    service.SERVICE.reset()


def test_sponsor_snapshot_rescore(snapshot):
    t = snapshot
    # A trucks-only plan shown an hour earlier still holds at 09:00: approved, same cost.
    r = client.post("/api/eog/plans/EV-0019/approve", json={"plan_at": t - 3600})
    assert r.status_code == 200 and r.json()["cost"] == 905.0 and r.json()["approval"]["cost"] == 905.0
    # The 10:49 shut-in shown at 08:55 costs $1,212 from 09:00, not $1,208: 409 with the 09:00 plan.
    r = client.post("/api/eog/plans/EV-0020/approve", json={"plan_at": t - 300})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["detail"].startswith("The plan shown at 08:55") and "$1,212, not $1,208" in body["detail"]
    assert body["plan"]["cost"] == 1215.0 and body["seen"]["cost"] == pytest.approx(1207.5)
    assert body["rescored"]["actions"][0]["start_after_min"] == 109       # same clock time, 10:49
    # The same plan read at 09:00 is approved as before.
    assert client.post("/api/eog/plans/EV-0020/approve", json={"plan_at": t}).status_code == 200
    # EV-0021's shut-in keeps its 13:51 clock time from 08:00 and still holds: approved at $975.31.
    r = client.post("/api/eog/plans/EV-0021/approve", json={"plan_at": t - 3600})
    assert r.status_code == 200 and r.json()["cost"] == 975.31
    assert r.json()["actions"][1]["start_after_min"] == 291
