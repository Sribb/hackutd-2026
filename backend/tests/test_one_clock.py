"""#42 one clock: state, forecast, plans, approve, and Last Call start from the live field.

These run main.py's app with its live layer and drive the live clock by hand
(live.step), so nothing depends on wall time.
"""
import json

import pytest
from fastapi.testclient import TestClient

from backend.app import main, now
from backend.app.forecast import build_plant, forecast
from backend.app.main import app
from backend.app.scenario import load_scenario

client = TestClient(app)
STATUSES = {"OK", "WARNING", "ALARM", "OFFLINE", "UNKNOWN"}


@pytest.fixture(autouse=True)
def live(monkeypatch):
    for var in ("FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN", "JARVIS_CONFIRM", "JARVIS_MODE",
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FREEBOARD_ONE_CLOCK", "1")
    lv = app.state.live
    app.state.live_reload(load_scenario())
    lv.proposals.clear()
    lv.commanded.clear()
    main._plans.clear()
    yield lv
    app.state.live_reload(load_scenario())
    lv.proposals.clear()
    lv.commanded.clear()
    main._plans.clear()
    now.set_static(main.SCENARIO)


def advance(lv, minutes: float, dt: float = 1.0) -> None:
    for _ in range(int(round(minutes / dt))):
        lv.step(dt)


def shutdown(fc: dict, tank: str = "TANK_3"):
    return next((e for e in fc["events"] if e["type"] == "SHUTDOWN_LEVEL" and e["asset_id"] == tank), None)


def static_shutdown_min() -> float:
    sc = load_scenario()
    return shutdown(forecast(sc, [], with_range=False))["t_min"]


# ---------- one moment everywhere ----------

def test_state_is_the_live_field_in_the_contract_shape(live):
    advance(live, 5)
    st = client.get("/api/state").json()
    lv = client.get("/api/live/state").json()
    assert st["clock_source"] == "live" and st["clock"] == lv["clock"]
    for aid, a in lv["assets"].items():
        assert st["assets"][aid]["status"] == a["status"], aid
    assert all(a["status"] in STATUSES for a in st["assets"].values())
    assert st["assets"]["PUMP_3"]["status"] == "ALARM"
    assert client.get("/api/health").json()["clock_source"] == "live"


def test_forecast_and_plans_start_from_the_live_clock(live):
    advance(live, 5)
    clock = client.get("/api/state").json()["clock"]
    fc = client.post("/api/forecast", json={}).json()
    assert fc["clock"] == clock and fc["clock_source"] == "live"
    hit = shutdown(fc)
    assert hit is not None, "Pump 3 is tripped, so Tank 3 must still reach shutdown"
    # Five live minutes have passed, so the shutdown is about five minutes closer.
    assert abs((hit["t_min"] + 5) - static_shutdown_min()) <= 10
    plans = client.post("/api/plans", json={}).json()
    assert plans["clock"] == clock and plans["plans"][0]["keeps_running"]


def test_a_trip_counts_before_its_alarm_is_confirmed(live):
    """Never falsely safe: the first reading of a stopped pump already counts."""
    advance(live, 1)
    n = now.get(app)
    off = {a["asset_id"] for a in n.actions if a["type"] == "SET_PUMP" and a["value"] == "OFF"}
    tripped = {f["asset_id"] for f in n.scenario.get("failures", [])}
    assert "PUMP_3" in off | tripped
    assert shutdown(client.post("/api/forecast", json={}).json()) is not None


def test_a_stale_tank_has_no_level(live):
    """Never falsely safe: a stale level is shown as UNKNOWN with no level, and the
    forecast treats that tank as unknown instead of safe."""
    advance(live, 2)
    live.registry.sim.inject({"asset_id": "TANK_5", "type": "SENSOR_STALE"})
    advance(live, 10)
    st = client.get("/api/state").json()["assets"]["TANK_5"]
    assert st["stale"] and st["status"] == "UNKNOWN"
    assert st["level_pct"] is None and st["last_level_pct"] is not None
    assert now.get(app).scenario["initial_levels_pct"]["TANK_5"] is None


# ---------- approve: proposals, a person confirms, the field changes ----------

def test_approve_proposes_each_step_and_nothing_moves_until_a_person_confirms(live):
    advance(live, 5)
    plans = client.post("/api/plans", json={}).json()["plans"]
    plan = plans[0]
    assert plan["keeps_running"] and plan["actions"]
    out = client.post(f"/api/plans/{plan['id']}/approve").json()
    assert len(out["proposals"]) == len(plan["actions"]) == len(out["steps"])
    acts = {p["id"]: p for p in client.get("/api/actions").json()["proposals"]}
    assert all(acts[pid]["status"] == "PENDING" and acts[pid]["source"] == "plan" for pid in out["proposals"])
    advance(live, 1)
    assert shutdown(client.post("/api/forecast", json={}).json()) is not None, "nothing confirmed, nothing changed"

    # Approving again doesn't duplicate what's still waiting.
    again = client.post(f"/api/plans/{plan['id']}/approve").json()
    assert again["proposals"] == out["proposals"]

    for pid in out["proposals"]:
        r = client.post(f"/api/actions/{pid}/confirm", json={"by": "operator"})
        assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text
    advance(live, 2)

    st = client.get("/api/state").json()["assets"]
    for a in plan["actions"]:
        if a["type"] == "SET_VALVE":
            assert st[a["asset_id"]]["position"] == a["value"]
    fc = client.post("/api/forecast", json={}).json()
    assert shutdown(fc) is None, "after the plan is confirmed, Tank 3 must not shut down"
    after = client.post("/api/plans", json={}).json()["plans"]
    assert after[0]["actions"] == [] and after[0]["keeps_running"], "nothing left to do"


def test_a_truck_on_the_road_arrives_on_its_real_timetable_and_is_never_sent_twice(live):
    advance(live, 5)
    p = client.post("/api/actions", json={"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"}).json()
    assert client.post(f"/api/actions/{p['id']}/confirm", json={}).json()["status"] == "CONFIRMED"
    full = float(next(t for t in load_scenario()["field"]["trucks"] if t["id"] == "TRUCK_1")["minutes_to"]["TANK_3"])
    advance(live, 3)
    n = now.get(app)
    trip = next(a for a in n.actions if a["type"] == "SEND_TRUCK" and a["asset_id"] == "TRUCK_1")
    assert trip["arrive_in"] == pytest.approx(max(0.0, full - 3), abs=1.01)
    fc = client.post("/api/forecast", json={}).json()
    arrive = next(e for e in fc["events"] if e["type"] == "TRUCK_ARRIVES" and e["asset_id"] == "TRUCK_1")
    assert arrive["t_min"] == pytest.approx(trip["arrive_in"], abs=1.01)
    for plan in client.post("/api/plans", json={}).json()["plans"]:
        assert not any(a["type"] == "SEND_TRUCK" and a["asset_id"] == "TRUCK_1" for a in plan["actions"])


def test_last_call_plans_from_the_same_moment(live):
    if main.LASTCALL != "mounted":
        pytest.skip("lastcall/ is not on this branch")
    advance(live, 5)
    clock = client.get("/api/state").json()["clock"]
    assert main.lastcall_scenario()["clock"] == clock
    assert client.post("/api/lastcall", json={}).status_code == 200


def test_activating_a_blueprint_moves_the_live_field(live, monkeypatch):
    seen = []
    real = app.state.live_reload
    monkeypatch.setattr(app.state, "live_reload", lambda sc: (seen.append(sc), real(sc))[1])
    assert client.post("/api/blueprints/default/activate").status_code == 200
    assert seen, "activation must reload the live layer, or the clock would follow the old field"


# ---------- no live layer: approving records the plan ----------

def test_static_clock_records_approved_plans(monkeypatch):
    monkeypatch.setenv("FREEBOARD_ONE_CLOCK", "0")
    now.set_static(load_scenario())
    plan = client.post("/api/plans", json={}).json()["plans"][0]
    out = client.post(f"/api/plans/{plan['id']}/approve").json()
    assert out["recorded_actions"] == len(plan["actions"]) and "proposals" not in out
    st = client.get("/api/state").json()
    assert st["clock_source"] == "static" and len(st["applied_actions"]) == len(plan["actions"])
    for a in plan["actions"]:
        if a["type"] == "SET_VALVE":
            assert st["assets"][a["asset_id"]]["position"] == a["value"]
    assert shutdown(client.post("/api/forecast", json={}).json()) is None
    again = client.post("/api/plans", json={}).json()["plans"][0]
    assert again["actions"] == [] and again["keeps_running"]


# ---------- the forecast's trips already under way ----------

def test_forecast_takes_a_trip_already_under_way():
    sc = load_scenario()
    plant = build_plant(sc)
    tid = next(iter(plant.trucks))
    target = next(iter(plant.trucks[tid]["minutes_to"]))
    fc = forecast(sc, [{"type": "SEND_TRUCK", "asset_id": tid, "target_id": target, "at_min": 0,
                        "arrive_in": 4, "load_min": 6, "capacity_bbl": 30, "haul_min": 10}], with_range=False)
    ev = next(e for e in fc["events"] if e["type"] == "TRUCK_ARRIVES")
    assert ev["t_min"] == 4
    # Hauling to disposal: busy for 25 more minutes, so a new trip starts after that.
    fc = forecast(sc, [{"type": "SEND_TRUCK", "asset_id": tid, "target_id": target, "at_min": 0, "busy_min": 25},
                       {"type": "SEND_TRUCK", "asset_id": tid, "target_id": target, "at_min": 0}], with_range=False)
    ev = next(e for e in fc["events"] if e["type"] == "TRUCK_ARRIVES")
    assert ev["t_min"] == pytest.approx(25 + plant.trucks[tid]["minutes_to"][target])


def test_spacetime_module_accepts_every_status_the_state_can_show():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "spacetime" / "src" / "index.ts").read_text(encoding="utf-8")
    line = next(ln for ln in src.splitlines() if ln.startswith("const STATUSES"))
    assert all(f"'{s}'" in line for s in STATUSES), json.dumps(line)
