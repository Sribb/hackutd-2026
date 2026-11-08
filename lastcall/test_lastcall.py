"""Run with: python3 -m pytest lastcall  (or python3 -m lastcall.test_lastcall)"""

import copy
import os

from lastcall import core, sim

SCENARIO = core.load_scenario(os.path.join(os.path.dirname(__file__), "scenario.json"))


def test_forecast_starts_from_current_level():
    # Tank 3: 712 bbl, shutdown 900, inflow 180 bbl/hr, pump down -> 62.7 min.
    fc = sim.forecast(SCENARIO, [])
    hit = [e for e in fc["events"] if e["asset_id"] == "TANK_3"][0]
    assert abs(hit["t_min"] - 62.7) < 0.5


def test_fuller_tank_shuts_down_sooner():
    full = copy.deepcopy(SCENARIO)
    next(t for t in full["tanks"] if t["id"] == "TANK_3")["level_pct"] = 85.0
    t_full = sim.forecast(full, [])["events"][0]["t_min"]
    t_base = sim.forecast(SCENARIO, [])["events"][0]["t_min"]
    assert t_full < t_base


def test_cheap_slow_options_expire_first():
    r = core.last_call(SCENARIO)
    ladder = r["ladder"]
    assert len(ladder) >= 2
    # Each rung lives longer and costs more than the one before.
    for a, b in zip(ladder, ladder[1:]):
        assert b["expires_min"] >= a["expires_min"]
        assert b["usd_at_start"] >= a["usd_at_start"]
    # The point of no return is before the shutdown.
    assert r["point_of_no_return"]["t_min"] < r["shutdown"]["t_min"]


def test_every_option_really_works_at_its_expiry_and_not_after():
    r = core.last_call(SCENARIO)
    plans = {p["id"]: p for p in core.candidate_plans(SCENARIO, ["TANK_3"])}
    for o in r["options"]:
        p = plans[o["plan_id"]]
        ok = sim.forecast(SCENARIO, core.schedule(p, o["expires_min"]))
        late = sim.forecast(SCENARIO, core.schedule(p, o["expires_min"] + 2))
        assert not ok["events"], o["summary"]
        assert late["events"], o["summary"]


def test_calm_field_reports_calm():
    calm = copy.deepcopy(SCENARIO)
    for p in calm["pumps"]:
        p["status"] = "OK"
    assert core.last_call(calm)["status"] == "CALM"


def test_no_readings_is_unknown_never_calm():
    blind = copy.deepcopy(SCENARIO)
    for t in blind["tanks"]:
        t["level_pct"] = None
    out = core.last_call(blind)
    assert out["status"] == "UNKNOWN"
    assert out["unknown_tanks"] == sorted(t["id"] for t in blind["tanks"])
    assert "can't vouch" in out["text"]


def test_calm_field_with_one_blind_tank_is_unknown():
    calm = copy.deepcopy(SCENARIO)
    for p in calm["pumps"]:
        p["status"] = "OK"
    calm["tanks"][0]["level_pct"] = None
    out = core.last_call(calm)
    assert out["status"] == "UNKNOWN"
    assert out["unknown_tanks"] == [calm["tanks"][0]["id"]]


def test_incident_names_the_blind_tank():
    inc = copy.deepcopy(SCENARIO)
    blind = next(t for t in inc["tanks"] if t["id"] == "TANK_1")
    blind["level_pct"] = None
    out = core.last_call(inc)
    assert out["status"] == "INCIDENT"
    assert out["unknown_tanks"] == ["TANK_1"]
    assert "Tank 1" in out["headline"] and "can't vouch" in out["warnings"][0]


def test_crossover_cannot_drown_the_neighbor():
    # A plan that keeps Tank 3 alive by overfilling Tank 4 must not count as working.
    tight = copy.deepcopy(SCENARIO)
    next(t for t in tight["tanks"] if t["id"] == "TANK_4")["level_pct"] = 88.0
    r = core.last_call(tight)
    for o in r["options"]:
        plan = next(p for p in core.candidate_plans(tight, ["TANK_3"]) if p["id"] == o["plan_id"])
        fc = sim.forecast(tight, core.schedule(plan, 0))
        assert not any(e["asset_id"] == "TANK_4" for e in fc["events"])


def test_reads_scenario_field_json_shape():
    # scenario/field.json (Vinny's lane) starts before the fault and names things differently.
    fj = {
        "clock_start": "01:44",
        "faults": [{"asset_id": "PUMP_3", "type": "TRIP", "at_min": 3, "reason": "Pump tripped."}],
        "initial": {"tank_pct": {t["id"]: t["level_pct"] for t in SCENARIO["tanks"]}},
        "tanks": [{k: t[k] for k in ("id", "capacity_bbl", "shutdown_pct")} for t in SCENARIO["tanks"]],
        "pumps": [{k: p[k] for k in ("id", "tank_id", "rate_bbl_hr")} for p in SCENARIO["pumps"]],
        "valves": [{"id": "VALVE_D_1", "connects": ["PUMP_1", "SWD_1"], "default": "OPEN"},
                   {"id": "VALVE_XO_34", "connects": ["TANK_3", "TANK_4"], "default": "CLOSED",
                    "max_bbl_hr": 90, "manual": True}],
        "wells": SCENARIO["wells"], "disposal_wells": SCENARIO["disposal_wells"],
        "trucks": [{"id": t["id"], "capacity_bbl": t["capacity_bbl"], "minutes_to": t["minutes_to"],
                    "load_minutes": 20, "minutes_to_disposal": 30} for t in SCENARIO["trucks"]],
        "lead_min": SCENARIO["lead_min"], "costs": SCENARIO["costs"],
    }
    s = core.from_field_json(fj)
    assert s["clock"] == "01:47"
    assert next(p for p in s["pumps"] if p["id"] == "PUMP_3")["status"] == "ALARM"
    r = core.last_call(s)
    assert r["status"] == "INCIDENT" and r["shutdown"]["asset_id"] == "TANK_3"
    # A tank with no reading must stop the run, never be treated as safe.
    bad = copy.deepcopy(fj)
    del bad["initial"]["tank_pct"]["TANK_5"]
    try:
        core.from_field_json(bad)
        assert False, "missing level accepted"
    except ValueError:
        pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
