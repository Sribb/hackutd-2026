import copy
import json

import pytest

from backend.app.planner import make_plans
from backend.app.scenario import DEFAULT_SCENARIO, TEAM_SCENARIO, ScenarioError, load_scenario, prepare
from backend.app.validate import problems


def raw_backend():
    return json.loads(DEFAULT_SCENARIO.read_text(encoding="utf-8"))


def test_shipped_scenarios_are_valid():
    assert problems(load_scenario(DEFAULT_SCENARIO)) == []
    if TEAM_SCENARIO.exists():
        assert problems(load_scenario(TEAM_SCENARIO)) == []


@pytest.mark.parametrize("breakage, expect", [
    (lambda d: d["field"]["tanks"][2].pop("capacity_bbl"), "TANK_3: `capacity_bbl`"),
    (lambda d: d["field"]["tanks"][2].update(shutdown_pct=150), "TANK_3: `shutdown_pct`"),
    (lambda d: d["field"]["pumps"][0].update(tank_id="TANK_99"), "PUMP_1: `tank_id`"),
    (lambda d: d["field"]["pumps"][0].pop("rate_bbl_hr"), "PUMP_1: `rate_bbl_hr`"),
    (lambda d: d["field"]["wells"][0].update(water_bbl_hr="lots"), "WELL_1: `water_bbl_hr`"),
    (lambda d: d["field"]["wells"][0].pop("lost_usd_hr"), "WELL_1: `lost_usd_hr`"),
    (lambda d: d["field"]["valves"][6].update(connects=["TANK_3", "WELL_1"]), "VALVE_XO_34: `connects`"),
    (lambda d: d["field"]["disposal_wells"][0].update(max_bbl_hr=0), "SWD_1: `max_bbl_hr`"),
    (lambda d: d["field"]["trucks"][0]["minutes_to"].update(TANK_99=10), "TANK_99"),
    (lambda d: d["field"]["tanks"].append(dict(d["field"]["tanks"][0])), "TANK_1 is used twice"),
    (lambda d: d["field"]["tanks"][0].update(id="tank 1"), "uppercase"),
    (lambda d: d["failures"][0].update(asset_id="PUMP_99"), "PUMP_99"),
    (lambda d: d["initial_levels_pct"].update(TANK_1=140), "TANK_1: starting level"),
    (lambda d: d["field"]["costs"].update(truck_usd_hr=-5), "truck_usd_hr"),
    (lambda d: d["field"].pop("wells"), "`wells` is missing"),
])
def test_every_missing_or_broken_number_is_reported(breakage, expect):
    d = raw_backend()
    breakage(d)
    with pytest.raises(ScenarioError) as err:
        prepare(d, "test")
    assert any(expect in p for p in err.value.problems), err.value.problems


def test_missing_level_is_allowed_but_never_safe():
    d = raw_backend()
    d["initial_levels_pct"].pop("TANK_2")
    s = prepare(d, "test")  # loads fine, and the forecast treats TANK_2 as unknown
    assert s["initial_levels_pct"].get("TANK_2") is None


def test_team_shape_errors_are_reported_not_raised_raw():
    with pytest.raises(ScenarioError) as err:
        prepare({"tanks": [{"id": "TANK_1"}], "clock_start": "late"}, "upload")
    assert err.value.problems
    with pytest.raises(ScenarioError) as err:
        prepare({"tanks": [{"id": "TANK_1"}], "clock_start": "01:00"}, "upload")
    assert any("capacity_bbl" in p for p in err.value.problems)


def test_bad_json_file(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{not json")
    with pytest.raises(ScenarioError, match="Not valid JSON"):
        load_scenario(p)


def test_prepare_does_not_mutate_team_input():
    if not TEAM_SCENARIO.exists():
        pytest.skip("scenario/field.json not in this checkout")
    d = json.loads(TEAM_SCENARIO.read_text(encoding="utf-8"))
    before = copy.deepcopy(d)
    prepare(d, "test")
    assert d == before


def test_plans_always_offer_a_robust_option_when_one_exists():
    for path in (DEFAULT_SCENARIO, TEAM_SCENARIO):
        if not path.exists():
            continue
        plans = make_plans(load_scenario(path))["plans"]
        assert any(p["robust"] for p in plans), [p["summary"] for p in plans]
        assert len(plans) <= 4
        cheapest = [p for p in plans if p["keeps_running"]]
        assert cheapest[0]["cost"]["total_usd"] == min(p["cost"]["total_usd"] for p in cheapest)
