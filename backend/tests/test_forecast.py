import time

from fastapi.testclient import TestClient

from backend.app.forecast import clock_at, forecast, nice
from backend.app.main import app
from backend.app.planner import make_plans
from backend.app.scenario import load_scenario

client = TestClient(app)
XO = {"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "at_min": 20}


def shutdowns(fc):
    return {e["asset_id"]: e for e in fc["events"] if e["type"] == "SHUTDOWN_LEVEL"}


def test_clock_and_names():
    assert clock_at("01:47", 60) == "2:47 AM"
    assert clock_at("23:30", 45) == "12:15 AM"
    assert clock_at("11:50", 15) == "12:05 PM"
    assert nice("VALVE_XO_34") == "the Tank 3 to 4 crossover"
    assert nice("WELL_12") == "Well 12"


def test_do_nothing_matches_the_story():
    fc = forecast(load_scenario(), [])
    ev = shutdowns(fc)["TANK_3"]
    assert abs(ev["t_min"] - 60) <= 2, "Tank 3 should shut down about 1 hour after 1:47 AM"
    assert ev["t_min_low"] < ev["t_min"] < ev["t_min_high"]
    assert "2:47 AM" in ev["text"]
    assert len(ev["wells_lost"]) == 4
    assert set(shutdowns(fc)) == {"TANK_3"}
    assert fc["timeline"][0]["tanks"]["TANK_3"] == 71.2
    assert fc["timeline"][-1]["wells_online"] == 20
    assert fc["cost"]["lost_production_usd"] > 0 and fc["cost"]["trucking_usd"] == 0


def test_forecast_starts_from_current_level_not_empty():
    s = load_scenario()
    s["initial_levels_pct"]["TANK_3"] = 85.0
    assert shutdowns(forecast(s, []))["TANK_3"]["t_min"] < 20
    s["initial_levels_pct"]["TANK_3"] = 20.0
    assert shutdowns(forecast(s, [])).get("TANK_3", {"t_min": 999})["t_min"] > 200


def test_timeline_shape():
    fc = forecast(load_scenario(), [], horizon=360, step=5)
    assert [r["t_min"] for r in fc["timeline"]][:3] == [0, 5, 10]
    assert fc["timeline"][-1]["t_min"] == 360
    assert len(fc["timeline"]) == 73
    fc = forecast(load_scenario(), [], horizon=62, step=5)
    assert fc["timeline"][-1]["t_min"] == 62


def test_crossover_delays_shutdown_and_hits_swd_limit():
    fc = forecast(load_scenario(), [XO])
    assert shutdowns(fc)["TANK_3"]["t_min"] > 80
    assert any(e["type"] == "SWD_AT_CAPACITY" and e["asset_id"] == "SWD_2" for e in fc["events"])
    assert max(r["tanks"]["TANK_4"] for r in fc["timeline"]) < 90


def test_truck_removes_water_and_costs_money():
    base = forecast(load_scenario(), [])
    fc = forecast(load_scenario(), [{"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3", "at_min": 0}])
    assert shutdowns(fc)["TANK_3"]["t_min"] > shutdowns(base)["TANK_3"]["t_min"]
    assert fc["cost"]["trucking_usd"] > 0 and fc["cost"]["disposal_usd"] == 130
    assert any(e["type"] == "TRUCK_ARRIVES" and e["t_min"] == 35 for e in fc["events"])


def test_choke_value_is_fraction_kept():
    s = load_scenario()
    half = forecast(s, [{"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 0.5, "at_min": 0}])
    shut = forecast(s, [{"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 0, "at_min": 0}])
    assert shutdowns(shut)["TANK_3"]["t_min"] > shutdowns(half)["TANK_3"]["t_min"]
    assert half["timeline"][1]["wells_online"] == 24 and shut["timeline"][1]["wells_online"] == 23


def test_bad_actions_warn_and_never_crash():
    fc = forecast(load_scenario(), [
        {"type": "SET_PUMP", "asset_id": "PUMP_3", "value": "ON", "at_min": 0},
        {"type": "NOPE", "asset_id": "X"},
        {"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 2},
        {"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "SWD_1"},
    ])
    assert len(fc["warnings"]) == 4
    assert abs(shutdowns(fc)["TANK_3"]["t_min"] - 60) <= 2


def test_missing_level_is_never_safe():
    s = load_scenario()
    del s["initial_levels_pct"]["TANK_4"]
    fc = forecast(s, [XO])
    assert fc["timeline"][5]["tanks"]["TANK_4"] is None
    assert any("TANK_4" in w for w in fc["warnings"])
    for p in make_plans(s)["plans"]:
        assert "VALVE_XO_34" not in [a["asset_id"] for a in p["actions"]]
        assert p["robust"] is False and p["unknown_tanks"] == ["TANK_4"]
        assert "check it in person" in p["summary"]


def test_plans_cheapest_first_and_actually_work():
    plans = make_plans(load_scenario())["plans"]
    assert 1 <= len(plans) <= 3
    costs = [p["cost"]["total_usd"] for p in plans if p["keeps_running"]]
    assert costs == sorted(costs)
    do_nothing = forecast(load_scenario(), [])["cost"]["total_usd"]
    for p in plans:
        fc = forecast(load_scenario(), p["actions"], with_range=False)
        assert bool(shutdowns(fc)) is (not p["keeps_running"])
        assert fc["cost"]["total_usd"] == p["cost"]["total_usd"]
        if p["keeps_running"]:
            assert p["cost"]["total_usd"] < do_nothing


def test_plans_respect_lead_times():
    for p in make_plans(load_scenario())["plans"]:
        for a in p["actions"]:
            if a["asset_id"] == "VALVE_XO_34":
                assert a["at_min"] == 20


def test_calm_field_still_returns_a_plan():
    s = load_scenario()
    s["failures"] = []
    plans = make_plans(s)["plans"]
    assert len(plans) == 1 and plans[0]["actions"] == [] and plans[0]["keeps_running"]


def test_api_endpoints_end_to_end():
    t0 = time.monotonic()
    r = client.post("/api/forecast", json={"horizon_minutes": 360, "step_minutes": 5, "actions": [XO]})
    assert r.status_code == 200 and (time.monotonic() - t0) < 0.5
    assert set(r.json()) == {"timeline", "events", "cost", "warnings"}
    assert client.post("/api/forecast", json={}).status_code == 200
    assert client.post("/api/forecast", json={"horizon_minutes": 0}).status_code == 422

    plans = client.post("/api/plans", json={}).json()["plans"]
    assert plans
    out = client.post(f"/api/plans/{plans[0]['id']}/approve", json={}).json()
    assert [s["n"] for s in out["steps"]] == list(range(1, len(plans[0]["actions"]) + 1))
    assert {s["asset_id"] for s in out["steps"]} == {a["asset_id"] for a in plans[0]["actions"]}
    assert out["radio_script"].startswith("Field, this is control.")
    assert client.post("/api/plans/plan_nope/approve").status_code == 404


def test_field_passes_lead_times_and_costs():
    body = client.get("/api/field").json()
    assert body["lead_min"]["SET_VALVE_manual"] == 20
    assert body["costs"]["truck_usd_hr"] > 0
