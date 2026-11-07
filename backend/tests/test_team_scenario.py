import pytest

from backend.app.forecast import forecast
from backend.app.planner import make_plans
from backend.app.scenario import TEAM_SCENARIO, from_team_shape, load_scenario, state_payload

MINI = {
    "clock_start": "01:44",
    "tanks": [{"id": "TANK_3", "capacity_bbl": 500, "shutdown_pct": 90, "warning_pct": 80, "pump_low_pct": 40},
              {"id": "TANK_4", "capacity_bbl": 500, "shutdown_pct": 90, "warning_pct": 80, "pump_low_pct": 40}],
    "pumps": [{"id": "PUMP_3", "tank_id": "TANK_3", "rate_bbl_hr": 150},
              {"id": "PUMP_4", "tank_id": "TANK_4", "rate_bbl_hr": 150}],
    "valves": [{"id": "VALVE_D_3", "connects": ["PUMP_3", "SWD_1"], "default": "OPEN"},
               {"id": "VALVE_D_4", "connects": ["PUMP_4", "SWD_1"], "default": "OPEN"},
               {"id": "VALVE_XO_34", "connects": ["TANK_3", "TANK_4"], "default": "CLOSED",
                "max_bbl_hr": 200, "bbl_hr_per_pct": 20, "manual": True}],
    "wells": [{"id": "WELL_9", "tank_id": "TANK_3", "water_bbl_hr": 94, "lost_usd_hr": 300},
              {"id": "WELL_13", "tank_id": "TANK_4", "water_bbl_hr": 60, "lost_usd_hr": 300}],
    "disposal_wells": [{"id": "SWD_1", "max_bbl_hr": 450}],
    "trucks": [{"id": "TRUCK_1", "capacity_bbl": 130, "minutes_to": {"TANK_3": 35},
                "load_minutes": 20, "minutes_to_disposal": 30, "usd_hr": 110}],
    "costs": {"disposal_usd_bbl": 1.25, "truck_usd_hr": 110, "well_restart_usd": 500},
    "initial": {"tank_pct": {"TANK_3": 71.76, "TANK_4": 60.0}},
    "faults": [{"asset_id": "PUMP_3", "type": "TRIP", "at_min": 3, "reason": "Pump tripped on motor overload."}],
}


def test_team_shape_converts(tmp_path):
    import json
    path = tmp_path / "field.json"
    path.write_text(json.dumps(MINI))
    s = load_scenario(path)
    assert s["start_clock"] == "01:47"
    assert s["failures"][0]["summary"] == "Pump 3 failed. Tank 3 is filling."
    assert s["field"]["trucks"][0]["load_min"] == 20
    # 3 minutes of pumping (150 bbl/hr out, 94 in) before the trip: 71.76% -> about 71.2%.
    assert abs(s["initial_levels_pct"]["TANK_3"] - 71.2) < 0.05
    st = state_payload(s)
    assert st["assets"]["VALVE_XO_34"]["position"] == "CLOSED"
    assert st["assets"]["PUMP_3"]["status"] == "ALARM"
    ev = next(e for e in forecast(s, [])["events"] if e["type"] == "SHUTDOWN_LEVEL")
    # (90 - 71.2)% of 500 bbl at 94 bbl/hr is 60 minutes: 2:47 AM.
    assert ev["asset_id"] == "TANK_3" and abs(ev["t_min"] - 60) < 1.0 and "2:47 AM" in ev["text"]
    assert make_plans(s)["plans"]


def test_conversion_does_not_touch_the_input():
    import copy
    before = copy.deepcopy(MINI)
    from_team_shape(MINI)
    assert MINI == before


@pytest.mark.skipif(not TEAM_SCENARIO.exists(), reason="scenario/field.json not in this checkout yet")
def test_real_team_scenario_runs():
    s = load_scenario(TEAM_SCENARIO)
    fc = forecast(s, [])
    assert fc["timeline"] and not any("Ignored" in w for w in fc["warnings"])
    assert make_plans(s)["plans"]
