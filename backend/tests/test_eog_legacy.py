"""The legacy routes in dataset mode (#153): /api/field, /api/state, /api/forecast, /api/plans,
approve and /api/lastcall on the active EOG dataset, and unchanged with dataset mode off.

Synthetic datasets always; the sponsor's snapshot when FREEBOARD_EOG_DIR points at the sponsor
repo (skipped otherwise). Every number is checked against the /api/eog routes, which use the
same Service, so the console and /api/eog can never disagree.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import spacetime_sync
from backend.app.eog import legacy, legacy_api, service
from backend.app.main import app
from backend.tests.eog_fixture import write_dataset

REAL_ROOT = os.environ.get("FREEBOARD_EOG_DIR", "")   # read before any test changes the env
SNAP = "history/snapshot_2026-10-10T0900"
PROBLEMS = [
    {"type": "pump_failure", "facility": "Q7_FACILITY_A", "onset_h": 46.0, "level_pct": 55},
    {"type": "pump_failure", "facility": "Q7_FACILITY_B", "onset_h": 46.5, "level_pct": 70},
    {"type": "pipeline_leak", "facility": "Q8_FACILITY_A", "onset_h": 30.0, "end_h": 60.0},
]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    for var in ("FREEBOARD_EOG_DATASET", "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN", "SPACETIME_DB"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FREEBOARD_ONE_CLOCK", "0")
    service.SERVICE.reset()
    yield
    service.SERVICE.reset()


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    base = tmp_path / "eog"
    write_dataset(base / "busy", seed=3, hours=48, problems=PROBLEMS, repair_hours=5)
    write_dataset(base / "calm", seed=2, hours=24)
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    monkeypatch.setenv("FREEBOARD_EOG_DATASET", "busy")
    return TestClient(app)


def ok(r, status: int = 200) -> dict:
    assert r.status_code == status, r.text
    return r.json()


def test_off_means_legacy(monkeypatch):
    monkeypatch.delenv("FREEBOARD_EOG_DIR", raising=False)
    c = TestClient(app)
    f = ok(c.get("/api/field"))
    assert "mode" not in f and any(t["id"].startswith("TANK_") for t in f["tanks"])
    st = ok(c.get("/api/state"))
    assert "mode" not in st and st.get("clock_source") != "dataset"
    assert ok(c.post("/api/plans", json={}))["plans"][0]["id"].startswith("plan")
    assert c.get("/api/eog/lastcall").status_code == 503


def test_folder_without_datasets_stays_legacy(tmp_path, monkeypatch):
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(tmp_path / "empty"))
    assert "mode" not in ok(TestClient(app).get("/api/field"))


def test_field_is_the_network(client):
    f = ok(client.get("/api/field"))
    ds = service.SERVICE.dataset()
    assert f["mode"] == "dataset" and f["dataset"] == "busy" and f["valves"] == []
    assert {t["id"] for t in f["tanks"]} == {x.tank_id for x in ds.facilities.values()}
    assert {s["id"] for s in f["systems"]} == set(ds.systems)
    for s in f["systems"]:
        assert len(s["truck_ids"]) == ds.systems[s["id"]].fleet_size
        assert s["glb"] == f"/api/dataset/field.glb?system={s['id']}"
    t = next(x for x in f["trucks"] if x["system_id"] == "Q7")
    fa = ds.facilities["Q7_FACILITY_A"]
    assert t["minutes_to"]["Q7_TANK_A"] == fa.response_min and t["cycle_min"]["Q7_TANK_A"] == fa.cycle_min
    p = next(x for x in f["pumps"] if x["id"] == fa.pump_id)
    assert p["rate_bbl_hr"] == pytest.approx(fa.max_flow_bpd / 24, abs=1e-3) and p["tank_id"] == fa.tank_id
    w = next(x for x in f["wells"] if x["id"] == fa.wells[0].id)
    assert w["lost_usd_hr"] == pytest.approx(ds.revenue_per_hour(w["id"]), abs=0.01)


def test_state_statuses_and_incident(client):
    st = ok(client.get("/api/state"))
    eog = ok(client.get("/api/eog/state"))
    alerts = ok(client.get("/api/eog/alerts"))["alerts"]
    assert st["mode"] == "dataset" and st["clock"] == legacy.hhmm(service.SERVICE.dataset(), eog["now"])
    a = st["assets"]
    for f in ("A", "B"):
        assert a[f"Q7_PUMP_{f}"]["status"] == "ALARM" and a[f"Q7_TANK_{f}"]["status"] == "ALARM"
        assert a[f"Q7_TANK_{f}"]["level_pct"] == pytest.approx(eog["assets"][f"Q7_TANK_{f}"]["readings"]["level_pct"], abs=0.01)
    assert a["Q8_PIPELINE_A"]["status"] == "WARNING" and "inspector" in a["Q8_PIPELINE_A"]["reason"]
    assert a["Q7_TRUCK_1"]["status"] == "OK"
    assert st["incident"]["root_asset_id"] == alerts[0]["asset"] and st["incident"]["event_id"] == alerts[0]["event_id"]
    assert all(x["status"] in ("OK", "WARNING", "ALARM", "OFFLINE", "UNKNOWN") for x in a.values())


def test_missing_readings_are_unknown_and_shut_in_is_offline():
    ls = legacy._legacy_status
    assert ls("tank", {"status": "WARNING", "readings": {"level_pct": None}}) == "UNKNOWN"
    assert ls("tank", {"status": "WARNING", "readings": {"level_pct": 86.0}}) == "WARNING"
    assert ls("pump", {"status": "WARNING", "readings": {"status": None}}) == "UNKNOWN"
    assert ls("pump", {"status": "INSPECT", "readings": {"status": 1}}) == "WARNING"
    assert ls("pipeline", {"status": "WARNING", "readings": {"pressure_psi": None}}) == "UNKNOWN"
    assert ls("well", {"status": "ALARM", "readings": {"status": 0}}) == "OFFLINE"
    assert ls("well", {"status": "WARNING", "readings": {"status": None}}) == "UNKNOWN"


def test_plans_are_the_eog_plans(client):
    p = ok(client.post("/api/plans", json={}))
    eog = {x["event"]["id"]: x["plan"] for x in ok(client.get("/api/eog/plans"))["plans"]}
    assert len(p["plans"]) == len(eog) == 2
    costs = [x["cost"]["total_usd"] for x in p["plans"]]
    assert costs == sorted(costs)
    used = []
    for x in p["plans"]:
        e = eog[x["id"]]
        assert x["event_id"] == x["id"] and x["cost"]["total_usd"] == e["cost"] and x["summary"] == e["summary"]
        assert x["expires_at"] == e["expires_at"] and x["keeps_running"] == (not e["trips"])
        used += [a["asset_id"] for a in x["actions"] if a["type"] == "SEND_TRUCK"]
    assert len(used) == len(set(used))           # the joint plan never sends one truck twice


def test_forecast_countdown_and_replay(client):
    alerts = {a["event_id"]: a for a in ok(client.get("/api/eog/alerts"))["alerts"]}
    fc = ok(client.post("/api/forecast", json={}))
    now = fc["now"]
    for e in fc["events"]:
        cd = alerts[e["event_id"]]["countdown"]
        assert e["type"] == "SHUTDOWN_LEVEL" and e["at"] == cd["trips_at"]
        assert e["t_min"] == pytest.approx((cd["trips_at"] - now) / 60, abs=0.05)
        assert e["t_min_low"] <= e["t_min"] <= e["t_min_high"]
    assert [e["t_min"] for e in fc["events"]] == sorted(e["t_min"] for e in fc["events"])
    assert fc["timeline"][0]["t_min"] == 0 and fc["cost"]["disposal_usd"] == 0
    plans = ok(client.post("/api/plans", json={}))["plans"]
    for x in plans:                              # replaying a plan costs what the plan says
        r = ok(client.post("/api/forecast", json={"actions": x["actions"]}))
        assert r["cost_by_tank"][x["tank_id"]]["total_usd"] == pytest.approx(x["cost"]["total_usd"], abs=0.01)


@pytest.mark.parametrize("action,word", [
    ({"type": "SET_VALVE", "asset_id": "Q7_TANK_A", "value": "OPEN"}, "sponsor"),
    ({"type": "SET_PUMP", "asset_id": "Q7_PUMP_A", "value": "ON"}, "sponsor"),
    ({"type": "CHOKE_WELL", "asset_id": "Q7_WELL_A1", "value": 0.5}, "partial"),
    ({"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "Q7_TANK_A"}, "no truck"),
    ({"type": "SEND_TRUCK", "asset_id": "Q8_TRUCK_1", "target_id": "Q7_TANK_A"}, "belongs to"),
])
def test_forecast_refuses_what_the_sponsor_cannot_do(client, action, word):
    r = client.post("/api/forecast", json={"actions": [action]})
    assert r.status_code == 422 and word in r.json()["detail"]


def test_approve_records_on_eog(client):
    plans = ok(client.post("/api/plans", json={}))["plans"]
    x = max(plans, key=lambda p: len(p["actions"]))
    out = ok(client.post(f"/api/plans/{x['id']}/approve"))
    assert [s["n"] for s in out["steps"]] == list(range(1, len(out["steps"]) + 1))
    ids = {s["asset_id"] for s in out["steps"]}
    assert {a["asset_id"] for a in x["actions"]} <= ids and x["asset_id"] in ids
    assert out["radio_script"].startswith("Field, this is control.")
    alerts = {a["event_id"]: a for a in ok(client.get("/api/eog/alerts"))["alerts"]}
    assert alerts[x["id"]]["approval"]["by"] == "console"
    assert client.post("/api/plans/EV-9999/approve").status_code == 404


def test_lastcall_from_the_planner(client):
    lc = ok(client.post("/api/lastcall", json={}))
    assert lc["status"] == "INCIDENT" and lc["headline"]
    eog = {x["event"]["id"]: x for x in ok(client.get("/api/eog/plans"))["plans"]}
    order = [a["event_id"] for a in ok(client.get("/api/eog/alerts"))["alerts"] if a["category"] == "pump_failure"]
    assert [o["plan_id"] for o in lc["options"]] == order
    for o in lc["options"]:
        assert o["expires_at"] == eog[o["plan_id"]]["plan"]["expires_at"]
        assert o["cost"]["total_usd"] == eog[o["plan_id"]]["plan"]["cost"]
        rows = [{k: r[k] for k in ("delay_min", "start_ts", "cost", "summary", "trucks_free")}
                for r in lc["curves"][o["plan_id"]]]
        assert rows == eog[o["plan_id"]]["wait_curve"]
    assert lc["shutdown"]["event_id"] == order[0] and lc["curve"] == lc["curves"][order[0]]
    assert lc["do_nothing_usd"] == ok(client.post("/api/forecast", json={}))["cost"]["total_usd"]
    assert ok(client.get("/api/eog/lastcall"))["status"] == "INCIDENT"


def test_calm_dataset(client):
    ok(client.post("/api/eog/datasets/activate", json={"id": "calm"}))
    assert ok(client.get("/api/state"))["incident"] is None
    p = ok(client.post("/api/plans", json={}))
    assert p["plans"] == [] and "nothing to plan" in p["note"]
    assert ok(client.post("/api/forecast", json={}))["events"] == []
    assert ok(client.post("/api/lastcall", json={}))["status"] == "CALM"


@pytest.fixture
def one_failure(tmp_path, monkeypatch):
    """A calm field until Q7_PUMP_B fails at hour 20 (from 60 %, so its tank trips before the
    repair). Returns (client, the failure as /api/eog/events lists it at the last row)."""
    base = tmp_path / "eog"
    write_dataset(base / "onefail", seed=4, hours=24, repair_hours=5, problems=[
        {"type": "pump_failure", "facility": "Q7_FACILITY_B", "onset_h": 20.0, "level_pct": 60}])
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    c = TestClient(app)
    ev = next(e for e in ok(c.get("/api/eog/events"))["events"] if e["category"] == "pump_failure")
    return c, ev


def _calm(c, ev):
    assert ok(c.get("/api/alerts"))["alerts"] == []
    assert ok(c.get("/api/live/state"))["assets"]["Q7_PUMP_B"]["status"] == "OK"
    st = ok(c.get("/api/state"))
    assert st["incident"] is None
    assert st["assets"]["Q7_PUMP_B"]["status"] == "OK" and st["assets"]["Q7_TANK_B"]["status"] == "OK"
    assert ok(c.post("/api/plans", json={}))["plans"] == []
    assert ok(c.post("/api/lastcall", json={}))["status"] == "CALM"
    return st


def _incident(c, ev):
    st = ok(c.get("/api/state"))
    assert st["incident"]["root_asset_id"] == "Q7_PUMP_B" and st["incident"]["event_id"] == ev["event_id"]
    assert st["assets"]["Q7_PUMP_B"]["status"] == "ALARM" and st["assets"]["Q7_TANK_B"]["status"] == "ALARM"
    assert [p["asset_id"] for p in ok(c.post("/api/plans", json={}))["plans"]] == ["Q7_PUMP_B"]
    assert [e["asset_id"] for e in ok(c.post("/api/forecast", json={}))["events"]] == ["Q7_TANK_B"]
    assert ok(c.post("/api/lastcall", json={}))["status"] == "INCIDENT"
    al = ok(c.get("/api/alerts"))["alerts"]
    assert [(a["id"], a["asset_id"], a["severity"]) for a in al] == [(ev["event_id"], "Q7_PUMP_B", "ALARM")]
    live = ok(c.get("/api/live/state"))
    assert live["clock"] == st["clock"] and live["assets"]["Q7_TANK_B"]["status"] == "ALARM"
    return st


def test_legacy_routes_follow_the_clock_cursor(one_failure):
    """The judge tour: the cursor 15 min before the onset is calm; stepped one row past the
    onset (as a poll would see it), the same routes show the incident."""
    c, ev = one_failure
    onset = ev["onset_unix"]
    clock = ok(c.post("/api/eog/clock", json={"at": onset - 900, "play": False}))
    before = _calm(c, ev)
    assert before["now"] == clock["now"] < onset
    ok(c.post("/api/eog/clock", json={"at": onset + 300}))
    after = _incident(c, ev)
    assert after["now"] == onset + 300 and after["clock"] == legacy.hhmm(service.SERVICE.dataset(), onset + 300)


def test_legacy_routes_follow_the_clock_while_playing(one_failure, monkeypatch):
    """POST /api/eog/clock {"at": onset - 900, "play": true, "speed": 3}: 3 data-minutes per real
    second, so the failure arrives 5 real seconds in and shows by the next 5-minute row."""
    c, ev = one_failure
    real = [1000.0]
    monkeypatch.setattr(service.SERVICE, "real", lambda: real[0])
    ok(c.post("/api/eog/clock", json={"at": ev["onset_unix"] - 900, "play": True, "speed": 3}))
    _calm(c, ev)
    real[0] += 4.0                                 # 12 data-minutes: still before the onset
    _calm(c, ev)
    real[0] += 3.0                                 # 21 data-minutes: one row past the onset
    st = _incident(c, ev)
    assert ev["onset_unix"] <= st["now"] <= ev["onset_unix"] + 600
    assert ok(c.get("/api/eog/state"))["now"] == st["now"]     # the same view as /api/eog


def test_alerts_are_the_eog_alerts(client):
    eog = ok(client.get("/api/eog/alerts"))["alerts"]
    al = ok(client.get("/api/alerts"))["alerts"]
    assert [a["id"] for a in al] == [a["event_id"] for a in eog]
    ds = service.SERVICE.dataset()
    for a, e in zip(al, eog):
        assert a["active"] is True and a["asset_id"] == e["asset"] and a["message"] == e["title"] + "."
        if e["category"] == "pump_failure":
            cd = e["countdown"]
            assert a["severity"] == "ALARM" and a["consequence_asset_id"] == cd["tank_id"]
            if cd["trips_at"] is not None:
                assert a["consequence_clock"] == legacy.hhmm(ds, cd["trips_at"])
                assert a["minutes_to_consequence"] == round((cd["trips_at"] - ok(client.get("/api/state"))["now"]) / 60)
                assert a["priority"] in ("P1", "P2", "P3") and a["usd_at_risk"] > 0
        else:
            assert a["severity"] == "WARNING" and "inspector" in a["consequence"]
    leak = next(a for a in al if a["category"] == "pipeline_leak")
    out = ok(client.post(f"/api/alerts/{leak['id']}/ack", json={"by": "Dana"}))
    assert out["acked"] is True
    assert next(a for a in ok(client.get("/api/alerts"))["alerts"] if a["id"] == leak["id"])["acked"] is True
    assert client.post("/api/alerts/EV-9999/ack", json={}).status_code == 404


def _drop_last_reading(folder: Path, asset: str, metric: str) -> None:
    path = folder / "telemetry.csv"
    lines = path.read_text().splitlines()
    last = lines[-1].split(",")[1]
    keep = [ln for ln in lines if not (ln.split(",")[1:2] == [last] and f",{asset},{metric}," in ln + ",")]
    assert len(keep) == len(lines) - 1
    path.write_text("\n".join(keep) + "\n")


def test_live_state_reads_the_dataset_with_ages(tmp_path, monkeypatch):
    base = tmp_path / "eog"
    write_dataset(base / "gap", seed=2, hours=24)
    _drop_last_reading(base / "gap", "Q8_TANK_B", "level_pct")
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    c = TestClient(app)
    live = ok(c.get("/api/live/state"))
    st = ok(c.get("/api/state"))
    assert live["mode"] == "dataset" and live["clock"] == st["clock"] and live["connectors"][0]["kind"] == "dataset"
    gap = live["assets"]["Q8_TANK_B"]
    assert gap["status"] == "UNKNOWN" and gap["stale"] is True and gap["age_min"] == 5.0 and gap["level_pct"] is None
    assert st["assets"]["Q8_TANK_B"]["status"] == "UNKNOWN"
    fine = live["assets"]["Q7_TANK_A"]
    assert fine["status"] == "OK" and fine["stale"] is False and fine["age_min"] == 0.0
    assert live["assets"]["Q7_DISPOSAL"]["status"] == "UNKNOWN"      # no readings at all: never OK
    assert all(a["status"] != "OK" or a["kind"] == "truck" or a["age_min"] == 0.0 for a in live["assets"].values())
    assert live["paused"] is True and live["speed"] == service.SERVICE.clock.speed


def test_demo_field_holds_still_in_dataset_mode(client, monkeypatch):
    import asyncio
    import types

    from backend.app import live as live_mod, spacetime_mirror
    assert live_mod.HOLD is legacy_api.on and spacetime_mirror.HOLD is legacy_api.on
    assert legacy_api.on() is True
    monkeypatch.setattr(live_mod, "TICK_S", 0.01)
    ticks = []

    async def tick(dt):
        ticks.append(dt)
    fake = types.SimpleNamespace(paused=False, speed=1.0, tick=tick)

    async def run_for(coro, s=0.1):
        try:
            await asyncio.wait_for(coro, s)
        except asyncio.TimeoutError:
            pass
    asyncio.run(run_for(live_mod.LiveField._loop(fake)))
    assert ticks == []                            # dataset mode: the demo field stands still
    sent = []
    monkeypatch.setattr(spacetime_mirror.Mirror, "tick", lambda self, app: sent.append(1) or asyncio.sleep(0))
    asyncio.run(run_for(spacetime_mirror.run(app, 0.01)))
    assert sent == []                             # and its mirror sends nothing
    monkeypatch.delenv("FREEBOARD_EOG_DIR")
    asyncio.run(run_for(live_mod.LiveField._loop(fake)))
    asyncio.run(run_for(spacetime_mirror.run(app, 0.01)))
    assert ticks and sent                         # dataset mode off: both run again


def test_spacetime_push_on_activation_and_cursor(client, monkeypatch):
    sent = []
    monkeypatch.setattr(spacetime_sync, "enabled", lambda: True)
    monkeypatch.setattr(spacetime_sync, "push_state", lambda st: sent.append(st) or True)
    pusher = legacy_api.Pusher()
    assert pusher.push_once() and sent[-1]["mode"] == "dataset" and sent[-1]["dataset"] == "busy"
    assert not pusher.push_once()                # nothing moved: no second push
    ok(client.post("/api/eog/clock", json={"at": sent[-1]["now"] - 3600}))
    assert pusher.push_once() and sent[-1]["now"] < sent[0]["now"]
    ok(client.post("/api/eog/datasets/activate", json={"id": "calm"}))
    assert pusher.push_once() and sent[-1]["dataset"] == "calm"


def test_spacetime_push_failure_never_raises(client, monkeypatch):
    monkeypatch.setattr(spacetime_sync, "enabled", lambda: True)
    monkeypatch.setattr(spacetime_sync, "push_state", lambda st: False)
    pusher = legacy_api.Pusher()
    assert pusher.push_once() is False and pusher.push_once() is False   # retried on the next check


# ---------- the sponsor's data (skipped unless FREEBOARD_EOG_DIR points at it) ----------
@pytest.fixture
def sponsor(monkeypatch) -> TestClient:
    base = Path(REAL_ROOT) if REAL_ROOT else None
    if base is None or not (base / SNAP / "telemetry.csv").exists():
        pytest.skip("FREEBOARD_EOG_DIR does not point at the sponsor repo")
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    c = TestClient(app)
    ok(c.post("/api/eog/datasets/activate", json={"id": SNAP}))
    return c


def test_sponsor_snapshot_on_the_console_routes(sponsor):
    st = ok(sponsor.get("/api/state"))
    for f in ("A", "C", "D"):
        assert st["assets"][f"S4_PUMP_{f}"]["status"] == "ALARM" and st["assets"][f"S4_TANK_{f}"]["status"] == "ALARM"
    plans = ok(sponsor.post("/api/plans", json={}))["plans"]
    assert [p["cost"]["total_usd"] for p in plans] == [905.0, 975.31, 1215.0]
    eog = {a["event_id"]: a["action"]["cost"] for a in ok(sponsor.get("/api/eog/alerts"))["alerts"]
           if a["category"] == "pump_failure"}
    assert {p["id"]: p["cost"]["total_usd"] for p in plans} == eog
    fc = ok(sponsor.post("/api/forecast", json={}))
    assert {e["asset_id"] for e in fc["events"] if e["type"] == "SHUTDOWN_LEVEL"} == {"S4_TANK_A", "S4_TANK_C", "S4_TANK_D"}
    assert ok(sponsor.post("/api/lastcall", json={}))["status"] == "INCIDENT"
    for p in plans:
        r = ok(sponsor.post("/api/forecast", json={"actions": p["actions"]}))
        assert r["cost_by_tank"][p["tank_id"]]["total_usd"] == pytest.approx(p["cost"]["total_usd"], abs=0.01)
