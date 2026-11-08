"""D2 Jarvis eval, scripted half (ROADMAP D2, CF-11): 20 questions on the rules engine with
no API key, in the demo's own wordings. Each checks the right numbers, that every number
names its source and age, the right camera focus, that an unknown tank is never called
safe, and that nothing reaches equipment without a person's confirm.

The free-form half needs a key; the Claude path gets the same facts from its tools, so the
last cases check the tool results and the system prompt carry the source and age too.
"""
import asyncio
import copy
import re

import pytest

from backend.app.forecast import clock_at, forecast
from backend.app.jarvis import TOOLS, Jarvis
from backend.app.live import LiveField
from backend.app.planner import make_plans
from backend.app.scenario import load_scenario

BLIND = "I have no current reading for Tank 5, so I can't say it's safe."


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


def tripped() -> LiveField:
    """Pump 3 trips with Tank 3 at 84%: the trip, then Tank 3's high and high-high alarms."""
    live = LiveField(field(TANK_3=84.0))
    step(live, 8)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "eval"})
    step(live, 40)
    kinds = {(a["asset_id"], a["kind"]) for a in live.detector.active.values()}
    assert {("PUMP_3", "PUMP_TRIP"), ("TANK_3", "TANK_HIGH"), ("TANK_3", "TANK_HIGH_HIGH")} <= kinds, kinds
    return live


def blind_tank_5(live: LiveField) -> LiveField:
    live.registry.sim.inject({"asset_id": "TANK_5", "type": "SENSOR_STALE"})
    step(live, 30)
    assert live.state()["assets"]["TANK_5"]["status"] == "UNKNOWN"
    return live


def jarvis(live: LiveField) -> Jarvis:
    return Jarvis(live, forecast_fn=lambda sc, a: forecast(sc, a), plans_fn=lambda sc: make_plans(sc))


def ask(live: LiveField, q: str) -> dict:
    out = run(jarvis(live).handle(q))
    assert out["source"] == "rules", out
    return out


def trip_alert(live: LiveField) -> dict:
    return live.detector.active[("PUMP_3", "PUMP_TRIP")]


def clock(live: LiveField, t=None) -> str:
    return live.fmap.clock_text(live.t if t is None else t)


# ---------- "What's failing?" ----------

def test_d2_01_whats_failing_leads_with_the_p1_root_and_its_clock():
    live = tripped()
    a = trip_alert(live)
    out = ask(live, "What's failing?")
    reply = out["reply"]
    assert reply.startswith(f"P1 since {clock_at(a['clock'], 0)}: Pump 3 tripped"), reply
    assert "Pump 3 tripped" in reply, reply
    assert f"The forecast at {clock(live, live._prio_t)} says {a['consequence']}" in reply, reply
    assert f"That's {int(round(a['consequence_t'] - live.t))} min from now." in reply, reply
    assert a["consequence_clock"] in reply, reply
    assert out["ui"][0]["asset_id"] == "PUMP_3"


def test_d2_02_whats_failing_folds_the_consequences_into_a_count():
    live = tripped()
    reply = ask(live, "What's failing?")["reply"]
    related = sum(1 for x in live.detector.active.values() if x["id"] != trip_alert(live)["id"])
    assert f"{related} related alarms." in reply, reply
    # The tank's high-level rows are not read out one by one.
    assert "over its high level" not in reply and "over its high-high level" not in reply, reply


def test_d2_03_whats_failing_names_the_age_of_the_reading_it_cites():
    live = tripped()
    live.t += 20 / 60  # 20 live seconds since the last reading
    reply = ask(live, "What's failing?")["reply"]
    assert "(reading 20 s old)" in reply, reply


def test_d2_04_whats_failing_with_a_blind_tank_never_calls_it_safe():
    live = blind_tank_5(tripped())
    reply = ask(live, "What's failing?")["reply"]
    assert reply.startswith("P1 since "), reply   # the P1 root still leads
    assert BLIND in reply, reply
    assert not re.search(r"Tank 5 (is at|stays|is fine|is safe)", reply), reply


def test_d2_05_all_quiet_says_when():
    live = LiveField(field())
    step(live, 4)
    assert ask(live, "What's failing?")["reply"] == f"All quiet at {clock(live)}. No active alerts."


def test_d2_06_a_shelved_root_is_named_not_read_out_and_never_hides_its_p1():
    live = tripped()
    run(live.shelve(trip_alert(live)["id"], 30, "crew on the way", by="eval"))
    reply = ask(live, "What's failing?")["reply"]
    assert "Pump 3 tripped" not in reply, reply   # the shelved trip's message isn't read out
    # ...but Tank 3's high-high, folded under it and not shelved, still leads as the P1.
    assert reply.startswith("Pump 3 trip is shelved (crew on the way)"), reply
    assert "Tank 3 high-high is still active. P1 since " in reply, reply


def test_d2_06b_a_fully_shelved_group_is_counted_not_read_out():
    live = tripped()
    for a in list(live.detector.active.values()):
        run(live.shelve(a["id"], 30, "crew on the way", by="eval"))
    reply = ask(live, "What's failing?")["reply"]
    assert "Pump 3" not in reply and "P1" not in reply, reply
    assert "1 shelved alert not shown." in reply, reply


# ---------- "Why is Tank 3 rising, and what's my cheapest fix?" ----------

def test_d2_07_why_is_tank_3_rising_gives_the_reading_age_cause_and_forecast_time():
    live = tripped()
    live.t += 20 / 60
    out = ask(live, "Why is Tank 3 rising, and what's my cheapest fix?")
    reply = out["reply"]
    lvl = live.value("TANK_3", "level_pct")
    assert f"Tank 3 is at {lvl:.0f}% (reading 20 s old)" in reply, reply
    assert "Pump 3 tripped" in reply, reply
    assert f"The forecast at {clock(live)} says Tank 3 reaches shutdown at" in reply, reply
    assert out["ui"][0]["asset_id"] == "TANK_3"


def test_d2_08_cheapest_fix_names_the_planner_run_and_the_right_dollars():
    live = tripped()
    out = ask(live, "Why is Tank 3 rising, and what's my cheapest fix?")
    reply = out["reply"]
    cheapest = make_plans(live.live_scenario())["plans"][0]
    assert f"The planner ran at {clock(live)} on the live readings." in reply, reply
    assert f"Cheapest plan, ${cheapest['cost']['total_usd']:,.0f}: {cheapest['summary']}" in reply, reply
    assert out["ui"][-1]["asset_id"] == cheapest["actions"][0]["asset_id"]


def test_d2_09_explaining_a_fix_proposes_nothing_and_sends_nothing():
    live = tripped()
    out = ask(live, "Why is Tank 3 rising, and what's my cheapest fix?")
    assert out["proposals"] == []
    assert live.registry.sim.truck_jobs == []
    assert live.registry.sim.valve_open["VALVE_XO_34"] is False


def test_d2_10_cheapest_fix_with_a_blind_tank_says_so_first():
    live = blind_tank_5(tripped())
    reply = ask(live, "what's my cheapest fix?")["reply"]
    assert reply.startswith(BLIND), reply


# ---------- "How long do we have?" ----------

def test_d2_11_how_long_gives_the_forecast_run_time_and_minutes():
    live = tripped()
    out = ask(live, "How long do we have?")
    reply = out["reply"]
    sd = next(e for e in forecast(live.live_scenario(), [])["events"] if e["type"] == "SHUTDOWN_LEVEL")
    assert reply.startswith(f"The forecast at {clock(live)} says {sd['text']}"), reply
    assert f"That's {int(round(sd['t_min']))} min from now." in reply, reply
    assert out["ui"][-1]["asset_id"] == "TANK_3"


def test_d2_12_how_long_with_a_blind_tank_says_it_first():
    live = blind_tank_5(tripped())
    reply = ask(live, "How long do we have?")["reply"]
    assert reply.startswith(BLIND), reply
    assert "at risk" in reply and "Tank 3 reaches shutdown at" in reply, reply


def test_d2_13_how_long_on_a_healthy_field_with_a_blind_tank_vouches_only_for_the_seen():
    live = blind_tank_5(LiveField(field()))
    reply = ask(live, "How long do we have?")["reply"]
    assert reply.startswith(BLIND), reply
    assert "no tank with a fresh reading reaches shutdown" in reply, reply
    assert "no tank reaches shutdown" not in reply.lower(), reply


# ---------- a stale tank, asked about directly ----------

def test_d2_14_a_stale_tank_gets_the_sentence_and_no_level():
    live = blind_tank_5(LiveField(field()))
    out = ask(live, "how's tank 5")
    reply = out["reply"]
    assert reply.startswith(BLIND), reply
    assert "%" not in reply, reply   # no level we don't have
    assert re.search(r"Its last reading is \d+ (s|min) old\.", reply), reply
    assert out["ui"] == [{"type": "ui", "action": "focus", "asset_id": "TANK_5"}]


def test_d2_15_what_am_i_looking_at_on_a_blind_tank():
    live = blind_tank_5(LiveField(field()))
    out = run(jarvis(live).handle("what am I looking at", selected=["TANK_5"]))
    assert out["reply"].startswith(BLIND), out["reply"]


# ---------- readings name their age and connector ----------

def test_d2_16_pump_and_valve_answers_carry_the_reading_age():
    live = tripped()
    assert "Pump 3 is stopped, moving 0 barrels an hour (reading 0 s old)." in ask(live, "show pump 3")["reply"]
    live.t += 45 / 60
    assert "crossover is closed (reading 45 s old)." in ask(live, "show me the Tank 3 to 4 crossover")["reply"]


def test_d2_17_a_reading_from_real_equipment_names_its_connector():
    live = LiveField(field())
    step(live, 4)
    live.latest[("TANK_3", "level_pct")]["source"] = "CONN_HOOK"
    live.t += 2.0
    reply = ask(live, "how's tank 3")["reply"]
    assert "(reading from CONN_HOOK, 2 min old)" in reply, reply


# ---------- never a command without a person ----------

def test_d2_18_open_the_crossover_waits_for_a_person():
    live = tripped()
    out = ask(live, "open the crossover")
    (p,) = out["proposals"]
    assert p["status"] == "PENDING" and "waiting for your confirmation" in out["reply"]
    assert live.registry.sim.valve_open["VALVE_XO_34"] is False
    run(live.confirm(p["id"], by="eval"))
    assert live.registry.sim.valve_open["VALVE_XO_34"] is True


# ---------- the Claude path sees the same sources ----------

def test_d2_19_tool_results_carry_source_and_age():
    live = blind_tank_5(tripped())
    j = jarvis(live)
    ctx = {"ui": [], "proposals": []}
    fc = run(j.run_tool("get_forecast", {"actions": []}, ctx))
    assert fc["as_of"] == clock(live) and "forecast run at" in fc["source"] and fc["unknown_tanks"] == ["TANK_5"]
    plans = run(j.run_tool("get_plans", {}, ctx))
    assert plans["as_of"] == clock(live) and plans["unknown_tanks"] == ["TANK_5"]
    tank3 = run(j.run_tool("explain_asset", {"asset_id": "TANK_3"}, ctx))
    assert tank3["reading"]["age_s"] == 0 and tank3["reading"]["current"] is True and "say" not in tank3
    tank5 = run(j.run_tool("explain_asset", {"asset_id": "TANK_5"}, ctx))
    assert tank5["say"] == BLIND and tank5["reading"]["current"] is False and tank5["reading"]["age_s"] >= 300
    alerts = run(j.run_tool("list_alerts", {}, ctx))
    first = alerts["groups"][0]
    assert first["root"]["asset_id"] == "PUMP_3" and first["root"]["priority"] == "P1"
    assert first["related"] == first["related_count"] == 2 and first["root"]["minutes_from_now"] is not None
    assert alerts["unknown_tanks"] == ["TANK_5"]
    assert ctx["proposals"] == [] and live.registry.sim.truck_jobs == []


def test_d2_20_system_prompt_asks_for_source_age_and_the_blind_sentence():
    live = LiveField(field())
    j = jarvis(live)
    assert "(reading 20 s old)" in j.system and "The forecast at 1:52 AM says" in j.system
    assert "I have no current reading for Tank 5, so I can't say it's safe." in j.system
    assert "list_alerts" in j.system and "related alarms" in j.system
    assert any(t["name"] == "list_alerts" for t in TOOLS)
