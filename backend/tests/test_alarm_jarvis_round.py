"""Alarm and Jarvis round (#38): a shelved root never hides an unshelved P1, missing data
never lowers a priority, Jarvis plans from the one clock, and the crossover and no-plan
answers never propose or promise more than the readings support. Each test fails on the
code before this round."""
import asyncio
import copy
import sys
from types import SimpleNamespace

import pytest

from backend.app.detect import consequence_of
from backend.app.fieldmap import FieldMap
from backend.app.forecast import forecast
from backend.app.jarvis import Jarvis
from backend.app.live import LiveField
from backend.app.live_api import one_now_for
from backend.app.planner import make_plans
from backend.app.scenario import load_scenario

BLIND_5 = "I have no current reading for Tank 5, so I can't say it's safe."


@pytest.fixture(autouse=True)
def no_llm(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FREEBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl"))


def run(coro):
    return asyncio.run(coro)


def field(**levels) -> dict:
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    sc["initial_levels_pct"].update(levels)
    return sc


def step(live: LiveField, n: int) -> None:
    for _ in range(n):
        live.step(0.25)


def step_until(live: LiveField, key: tuple, limit: int = 400) -> dict:
    for _ in range(limit):
        if key in live.detector.active:
            return live.detector.active[key]
        live.step(0.25)
    raise AssertionError(f"{key} never raised")


def jarvis(live: LiveField, **kw) -> Jarvis:
    return Jarvis(live, forecast_fn=lambda sc, a: forecast(sc, a), plans_fn=lambda sc: make_plans(sc), **kw)


def ask(live: LiveField, q: str, **kw) -> dict:
    out = run(jarvis(live, **kw).handle(q))
    assert out["source"] == "rules", out
    return out


def trip_tank_3(level: float = 84.0, **levels) -> LiveField:
    live = LiveField(field(TANK_3=level, **levels))
    step(live, 8)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    step_until(live, ("PUMP_3", "PUMP_TRIP"))
    return live


# ---------- 1. a shelved root never hides an unshelved P1 under it ----------

def test_a_p1_raised_after_its_root_was_shelved_heads_the_group():
    live = trip_tank_3()
    trip = live.detector.active[("PUMP_3", "PUMP_TRIP")]
    run(live.shelve(trip["id"], 60, "crew on it", by="test"))
    hh = step_until(live, ("TANK_3", "TANK_HIGH_HIGH"))   # comes after the shelve, folds under the trip
    assert hh["root_id"] is not None and hh.get("shelved_until_t") is None
    assert hh["priority"] == "P1"

    j = jarvis(live)
    res = run(j.run_tool("list_alerts", {}, {"ui": [], "proposals": []}))
    g = res["groups"][0]
    assert g["root"]["id"] == hh["id"] and not g["root"]["shelved"], g
    assert g["root"]["priority"] == "P1"
    assert g["shelved_root"]["id"] == trip["id"] and g["shelved_root"]["shelve_reason"] == "crew on it"

    out = run(j.handle("What's failing?"))
    reply = out["reply"]
    assert reply.startswith("Pump 3 trip is shelved (crew on it)"), reply
    assert "Tank 3 high-high is still active. P1 since " in reply, reply
    assert "reaches shutdown at" in reply, reply
    assert "shelved alert not shown" not in reply, reply
    assert out["ui"][0]["asset_id"] == "TANK_3"


def test_a_shelved_root_with_every_child_shelved_stays_out_of_sight():
    live = trip_tank_3()
    step_until(live, ("TANK_3", "TANK_HIGH_HIGH"))
    for a in list(live.detector.active.values()):
        run(live.shelve(a["id"], 60, "crew on it", by="test"))
    res = run(jarvis(live).run_tool("list_alerts", {}, {"ui": [], "proposals": []}))
    assert all(g["root"]["shelved"] for g in res["groups"])


# ---------- 2. missing data never lowers a priority ----------

def test_a_trip_whose_tank_goes_unknown_stays_p1():
    live = trip_tank_3()
    trip = live.detector.active[("PUMP_3", "PUMP_TRIP")]
    step_until(live, ("TANK_3", "TANK_HIGH_HIGH"))
    assert trip["priority"] == "P1"
    live.registry.sim.inject({"asset_id": "TANK_3", "type": "SENSOR_STALE"})
    step(live, 30)
    assert live.state()["assets"]["TANK_3"]["status"] == "UNKNOWN"
    assert trip["priority"] == "P1", trip
    assert trip["consequence"] == "Tank 3 level unknown; can't rule out shutdown."
    hh = live.detector.active[("TANK_3", "TANK_HIGH_HIGH")]
    assert hh["priority"] == "P1", hh   # its own high-high, now unseen, doesn't drop either
    reply = ask(live, "What's failing?")["reply"]
    assert reply.startswith("P1 since "), reply
    assert "Tank 3 level unknown; can't rule out shutdown." in reply, reply
    assert "says Tank 3 level unknown" not in reply, reply   # not dressed up as a forecast


def test_consequence_of_a_trip_on_an_unknown_tank_is_p1():
    fmap = FieldMap.from_scenario(field())
    alert = {"asset_id": "PUMP_3", "kind": "PUMP_TRIP", "usd_at_risk": 4200}
    out = consequence_of(fmap, alert, {}, 360, 0.0, unknown_tanks={"TANK_3"})
    assert out["priority"] == "P1" and out["consequence_asset_id"] == "TANK_3"
    assert out["consequence"] == "Tank 3 level unknown; can't rule out shutdown."
    assert out["usd_at_risk"] == 4200   # last known dollars kept, never $0 for not looking
    assert consequence_of(fmap, alert, {}, 360, 0.0)["priority"] == "P3"   # a seen tank, no shutdown


# ---------- 3. one "now" ----------

def test_jarvis_forecasts_and_plans_from_the_one_clock():
    live = LiveField(field())
    step(live, 4)
    under_way = [{"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3", "at_min": 0.0}]
    seen: dict = {}

    def fc(sc, actions):
        seen["forecast"] = (sc, actions)
        return forecast(sc, actions)

    def plans(sc, base=None):
        seen["plans"] = (sc, base)
        return make_plans(sc)

    marker = live.live_scenario()
    marker["_one_clock"] = True
    j = Jarvis(live, forecast_fn=fc, plans_fn=plans, now_fn=lambda: (marker, under_way))
    run(j.run_tool("get_forecast", {"actions": [{"type": "SET_PUMP", "asset_id": "PUMP_2", "value": "OFF",
                                                  "target_id": None, "at_min": None}]},
                   {"ui": [], "proposals": []}))
    sc, actions = seen["forecast"]
    assert sc.get("_one_clock") and actions[0] == under_way[0] and actions[1]["asset_id"] == "PUMP_2"
    run(j.run_tool("get_plans", {}, {"ui": [], "proposals": []}))
    assert seen["plans"][0].get("_one_clock") and seen["plans"][1] == under_way


def test_one_now_for_uses_app_state_now_and_falls_back(monkeypatch):
    live = LiveField(field())
    sc = live.live_scenario()
    now = SimpleNamespace(scenario=sc, actions=({"type": "SET_PUMP", "asset_id": "PUMP_1", "value": "OFF"},),
                          source="live")
    app = SimpleNamespace(state=SimpleNamespace(live=live, now=SimpleNamespace(get=lambda a: now)))
    got = one_now_for(app, live)()
    assert got[0] is sc and got[1] == [now.actions[0]]
    static = SimpleNamespace(state=SimpleNamespace(
        live=live, now=SimpleNamespace(get=lambda a: SimpleNamespace(scenario=sc, actions=(), source="static"))))
    assert one_now_for(static, live)() is None   # a static clock isn't this live field
    monkeypatch.setitem(sys.modules, "backend.app.now", None)   # #123 not merged: no now module
    assert one_now_for(SimpleNamespace(state=SimpleNamespace(live=live)), live)() is None


# ---------- 4. crossovers and "no plan needed" ----------

def test_close_the_crossover_with_none_open_proposes_nothing():
    live = trip_tank_3()
    assert str(live.value("VALVE_XO_34", "position")).upper() == "CLOSED"
    out = ask(live, "close the crossover")
    assert out["reply"] == "No crossover is open." and out["proposals"] == []


def test_close_the_crossover_never_says_none_is_open_on_a_valve_nobody_sees():
    live = LiveField(field())
    step(live, 4)
    live.registry.sim.inject({"asset_id": "VALVE_XO_34", "type": "SENSOR_STALE"})
    step(live, 30)
    out = ask(live, "close the crossover")
    assert out["proposals"] == []
    assert out["reply"] != "No crossover is open."
    assert "I have no current reading for the Tank 3 to 4 crossover" in out["reply"], out["reply"]


def test_nothing_at_risk_says_no_plan_needed_never_a_zero_dollar_plan():
    live = LiveField(field())
    step(live, 8)
    out = ask(live, "what's my cheapest plan?")
    assert out["reply"].startswith("No plan needed: nothing reaches shutdown in the next 6 hours."), out["reply"]
    assert "Cheapest plan" not in out["reply"] and "$0" not in out["reply"]
    assert out["proposals"] == []


def test_no_plan_needed_is_never_said_while_a_tank_is_unknown():
    live = LiveField(field())
    step(live, 8)
    live.registry.sim.inject({"asset_id": "TANK_5", "type": "SENSOR_STALE"})
    step(live, 30)
    reply = ask(live, "what's my cheapest plan?")["reply"]
    assert BLIND_5 in reply, reply
    assert "No plan needed" not in reply and "Cheapest plan" not in reply, reply


def test_open_the_crossover_onto_a_fuller_tank_is_no_help():
    live = trip_tank_3(level=40.0, TANK_4=70.0)
    out = ask(live, "open the crossover")
    assert out["proposals"] == [], out
    assert out["reply"].startswith("No crossover helps Tank 3"), out["reply"]


def test_open_the_crossover_onto_a_tank_nobody_sees_is_no_help():
    live = trip_tank_3()
    live.registry.sim.inject({"asset_id": "TANK_4", "type": "SENSOR_STALE"})
    step(live, 30)
    out = ask(live, "open the crossover")
    assert out["proposals"] == [], out
    assert out["reply"].startswith("No crossover helps Tank 3"), out["reply"]


def test_open_the_crossover_onto_an_emptier_tank_is_proposed_and_waits():
    live = trip_tank_3()   # Tank 3 at 84%+, Tank 4 lower
    out = ask(live, "open the crossover")
    (p,) = out["proposals"]
    assert p["action"] == {"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}
    assert p["status"] == "PENDING"
