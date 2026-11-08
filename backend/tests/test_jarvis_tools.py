"""Jarvis console tools (docs/JARVIS_CONSOLE.md sections 5 and 7, Track A).

Checks 1 (every fixture phrase) run in test_jarvis_phrases.py. Here: checks 2 to 7, the
directive whitelist, the new tools, the brief feed, and the preview endpoint.
"""
import asyncio
import copy
import sys
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import jarvis_tools as T
from backend.app.forecast import forecast
from backend.app.jarvis import TOOLS, Jarvis
from backend.app.live import LiveField
from backend.app.live_api import install
from backend.app.planner import make_plans
from backend.app.scenario import load_scenario

OP, PH, PIN = "op-secret-1", "phone-secret-2", "4321"
NEVER = ("all quiet", "all clear", "every tank")
BLIND5 = "I have no current reading for Tank 5, so I can't say it's safe."   # CF-11 (the spec said "I can't vouch")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC",
                "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN", "FREEBOARD_AUDIT_FILE"):
        monkeypatch.delenv(var, raising=False)


def run(coro):
    return asyncio.run(coro)


def healthy() -> dict:
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    return sc


def field(trip: bool = True) -> tuple[LiveField, Jarvis]:
    live = LiveField(healthy())
    if trip:
        live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP"})
        for _ in range(3):
            live.step(0.5)
        assert any(a["kind"] == "PUMP_TRIP" for a in live.detector.active.values())
    return live, Jarvis(live, forecast_fn=lambda sc, a: forecast(sc, a), plans_fn=lambda sc: make_plans(sc))


def app_client() -> tuple[TestClient, LiveField]:
    app = FastAPI()
    live = install(app, healthy())
    return TestClient(app), live


def h(tok):
    return {"X-Freeboard-Token": tok}


def trip_http(c, headers=None):
    assert c.post("/api/live/faults", json={"asset_id": "PUMP_3", "type": "TRIP"}, headers=headers).status_code == 200
    for _ in range(3):
        assert c.post("/api/live/sim", json={"advance_min": 0.5}, headers=headers).status_code == 200


# ---------- check 2: open pump 3 is a view; open the crossover is a proposal ----------

def test_open_pump_3_explodes_and_open_the_crossover_proposes():
    live, j = field()
    out = run(j.handle("open pump 3"))
    assert out["source"] == "rules" and out["proposals"] == []
    assert out["ui"][0]["action"] == "explode" and out["ui"][0]["asset_id"] == "PUMP_3"
    out = run(j.handle("open the crossover"))
    assert [p["action"]["type"] for p in out["proposals"]] == ["SET_VALVE"]
    assert out["proposals"][0]["status"] == "PENDING"
    assert "explode" not in [d["action"] for d in out["ui"]]
    assert live.proposals[out["proposals"][0]["id"]]["status"] == "PENDING"   # nothing was sent


# ---------- check 3: a stale tank is never vouched for ----------

def test_stale_tank_5_comes_first_and_is_never_called_safe():
    c, live = app_client()
    assert c.post("/api/live/faults", json={"asset_id": "TANK_5", "type": "SENSOR_STALE"}).status_code == 200
    assert c.post("/api/live/sim", json={"advance_min": 6}).status_code == 200
    left = c.post("/api/jarvis", json={"message": "time left"}).json()
    assert left["source"] == "rules" and left["reply"].startswith(BLIND5), left["reply"]
    for q in ("time left", "what's failing", "status", "how long"):
        reply = c.post("/api/jarvis", json={"message": q}).json()["reply"]
        assert not any(bad in reply.lower() for bad in NEVER), (q, reply)
    j = live.brief_feed.jarvis
    res = run(j.run_tool("list_sensors", {"asset_id": "TANK_5"}, {}))
    row = res["sensors"][0]
    assert row["stale"] is True and row["status"] == "UNKNOWN"
    level = next(s for s in row["signals"] if s["signal"] == "level_pct")
    assert level["stale"] is True and level["age_min"] >= 5
    assert "TANK_5" in res["stale"]
    brief = c.get("/api/jarvis/brief").json()
    assert brief["unknown_tanks"] == ["TANK_5"] and brief["countdown_text"].startswith(BLIND5)


def test_no_alarms_with_an_unknown_tank_is_not_all_clear():
    live, j = field(trip=False)
    live.registry.sim.inject({"asset_id": "TANK_5", "type": "SENSOR_STALE"})
    live.step(6)
    for a in list(live.detector.active.values()):   # say the stale alert was shelved
        a["shelved_until_t"] = live.t + 30
    reply = run(j.handle("what's failing"))["reply"]
    assert reply.startswith(BLIND5) and "1 shelved" in reply, reply
    assert not any(bad in reply.lower() for bad in NEVER), reply


# ---------- check 4: "why is tank 3 rising" stays rules with a key ----------

def test_why_is_tank_3_rising_is_rules_even_with_a_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-not-a-real-key")
    live, j = field()

    async def no_claude(*a, **k):
        raise AssertionError("Claude was called")

    monkeypatch.setattr(j, "_claude", no_claude)
    out = run(j.handle("Why is Tank 3 rising?"))
    assert out["source"] == "rules" and out["reply"].startswith("Tank 3"), out
    assert "Pump 3" in out["reply"] and "shutdown" in out["reply"]
    for q in ("open pump 3", "status of PUMP_3", "what if I open the crossover", "show tank 3's sensors"):
        assert run(j.handle(q))["source"] == "rules", q
    # A real open-ended question still goes to Claude.
    called = []

    async def claude(text, *a, **k):
        called.append(text)
        return "ok"

    monkeypatch.setattr(j, "_claude", claude)
    assert run(j.handle("Why open the crossover instead of sending a truck?"))["source"] == "claude"
    assert called


def test_pinch_status_of_an_id_answers_about_that_asset():
    live, j = field()
    for aid, start in (("PUMP_3", "Pump 3"), ("TANK_3", "Tank 3"), ("VALVE_XO_34", "The Tank 3 to 4 crossover"),
                       ("WELL_9", "Well 9"), ("SWD_1", "Disposal well 1")):
        out = run(j.handle(f"status of {aid}"))
        assert out["reply"].startswith(start), (aid, out["reply"])
        assert out["ui"][0]["asset_id"] == aid
    out = run(j.handle("What's going on with it?", ["PUMP_3"]))
    assert out["reply"].startswith("Pump 3") and out["ui"][0]["asset_id"] == "PUMP_3"


# ---------- check 5: tokens ----------

def test_tokens_jarvis_proposal_confirmed_by_phone_and_audited(monkeypatch):
    monkeypatch.setenv("OPERATOR_PIN", PIN)
    monkeypatch.setenv("FREEBOARD_PHONE_TOKEN", PH)
    c, live = app_client()
    assert c.post("/api/jarvis", json={"message": "open the crossover"}).status_code == 401
    assert c.post("/api/jarvis", json={"message": "open the crossover"}, headers=h(PH)).status_code == 403
    assert c.post("/api/jarvis/preview", json={"action": {"type": "SET_VALVE", "asset_id": "VALVE_XO_34",
                                                          "value": "OPEN"}}).status_code == 401
    trip_http(c, h(PIN))
    out = c.post("/api/jarvis", json={"message": "open the crossover"}, headers=h(PIN)).json()
    pid = out["proposals"][0]["id"]
    assert c.post(f"/api/actions/{pid}/confirm").status_code == 401
    r = c.post(f"/api/actions/{pid}/confirm", headers=h(PH))
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED" and r.json()["decided_by"] == "phone"
    log = [(e["event"], e["source"], e["role"]) for e in c.get("/api/audit").json()["entries"]
           if e.get("proposal_id") == pid]
    assert log[:2] == [("proposed", "jarvis", "operator"), ("confirmed", "phone", "phone")]


def test_untokened_ws_cannot_explode_and_tokened_ws_can(monkeypatch):
    monkeypatch.setenv("FREEBOARD_TOKEN", OP)
    c, live = app_client()
    with c.websocket_connect("/ws?role=web") as watcher:
        assert watcher.receive_json()["type"] == "state"
        with c.websocket_connect("/ws?role=phone") as anon:
            assert anon.receive_json()["type"] == "state"
            anon.send_json({"type": "ui", "action": "explode", "asset_id": "PUMP_3"})
            anon.send_json({"type": "ping"})
            assert anon.receive_json()["type"] == "pong"
        with c.websocket_connect(f"/ws?role=web&token={OP}") as steer:
            assert steer.receive_json()["type"] == "state"
            steer.send_json({"type": "ui", "action": "explode", "asset_id": "NOPE_1"})      # not in the field
            steer.send_json({"type": "ui", "action": "set_valve", "asset_id": "VALVE_XO_34"})  # not a view action
            steer.send_json({"type": "ui", "id": "mine_1", "action": "explode", "asset_id": "PUMP_3"})
            msg = steer.receive_json()
            assert msg["type"] == "ui" and msg["action"] == "explode" and msg["id"] == "mine_1"
        msg = watcher.receive_json()   # the first ui the watcher ever sees is the tokened explode
        assert msg["type"] == "ui" and msg["action"] == "explode" and msg["asset_id"] == "PUMP_3"
        assert msg["source"] == "web"


def test_phone_focus_still_works_without_tokens():
    c, live = app_client()
    with c.websocket_connect("/ws?role=phone") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "ui", "asset_id": "TANK_3"})   # #38's phone message: focus by default
        msg = ws.receive_json()
        assert msg["action"] == "focus" and msg["asset_id"] == "TANK_3" and msg["id"].startswith("ui_")


# ---------- check 6: shelve ----------

def test_shelve_needs_reason_and_minutes_and_comes_back():
    with TestClient(FastAPI()) as _:
        pass
    app = FastAPI()
    live = install(app, healthy())
    with TestClient(app) as c:
        trip_http(c)
        aid = next(a["id"] for a in c.get("/api/alerts").json()["alerts"] if a["kind"] == "PUMP_TRIP")
        assert c.post(f"/api/alerts/{aid}/shelve", json={"minutes": 30}).status_code == 422
        assert c.post(f"/api/alerts/{aid}/shelve", json={"minutes": 30, "reason": "  "}).status_code == 422
        assert c.post(f"/api/alerts/{aid}/shelve", json={"minutes": 10, "reason": "crew"}).status_code == 422
        with c.websocket_connect("/ws") as ws:
            ws.receive_json()
            r = c.post(f"/api/alerts/{aid}/shelve", json={"minutes": 15, "reason": "Crew is on it"})
            assert r.status_code == 200 and r.json()["shelved_until"] and r.json()["shelved_until_t"]
            for _ in range(4):
                c.post("/api/live/sim", json={"advance_min": 4})
            events = []
            for _ in range(200):
                m = ws.receive_json()
                if m["type"] == "alert" and m["alert"]["id"] == aid:
                    events.append(m["event"])
                    if m["event"] == "unshelved":
                        assert m["alert"]["shelved_until"] is None and m["alert"]["active"]
                        break
            assert events[0] == "shelved" and events[-1] == "unshelved"


def test_offer_shelve_and_ack_cards_validate():
    live, j = field()
    aid = next(a["id"] for a in live.alerts() if a["kind"] == "PUMP_TRIP")
    ctx: dict = {}
    assert run(j.run_tool("offer_shelve", {"alert_id": aid, "minutes": 30, "reason": ""}, ctx)) == \
        {"error": "Why? A shelve needs a reason."}
    assert "15 to 240" in run(j.run_tool("offer_shelve", {"alert_id": aid, "minutes": 10, "reason": "x"}, ctx))["error"]
    assert "No alert" in run(j.run_tool("offer_shelve", {"alert_id": "alert_99", "minutes": 30, "reason": "x"}, ctx))["error"]
    ok = run(j.run_tool("offer_shelve", {"alert_id": aid, "minutes": 45, "reason": "Crew on site"}, ctx))
    assert ok["ok"] and ctx["offers"][-1]["request"] == {"method": "POST", "path": f"/api/alerts/{aid}/shelve",
                                                         "body": {"minutes": 45, "reason": "Crew on site"}}
    assert ctx["offers"][-1]["press"] == "hold"
    assert run(j.run_tool("offer_ack", {"alert_id": aid}, ctx))["ok"] and ctx["offers"][-1]["press"] == "tap"
    assert not live.alerts()[0].get("acked") and live.alerts()[0]["shelved_until"] is None   # cards only
    out = run(j.handle("shelve the pump alarm for an hour since the crew is on it"))
    assert out["offers"][0]["minutes"] == 60 and out["offers"][0]["reason"] == "the crew is on it"


# ---------- check 7: the brief feed ----------

def _wait(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_ws_brief_follows_the_trip_alert_within_2_seconds():
    app = FastAPI()
    live = install(app, healthy())
    feed = live.brief_feed
    with TestClient(app) as c:
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "state"
            assert c.post("/api/live/faults", json={"asset_id": "PUMP_3", "type": "TRIP"}).status_code == 200
            t0 = time.monotonic()
            for _ in range(3):
                c.post("/api/live/sim", json={"advance_min": 0.5})
            assert _wait(lambda: feed.latest and feed.latest["countdown"]
                         and feed._task is not None and feed._task.done()), "no brief after the trip"
            assert time.monotonic() - t0 < 2.0
            seen_alert, brief = False, None
            for _ in range(100):
                m = ws.receive_json()
                if m["type"] == "alert" and m["alert"]["kind"] == "PUMP_TRIP" and m["event"] == "raised":
                    seen_alert = True
                elif m["type"] == "brief" and seen_alert and m["brief"]["countdown"]:
                    brief = m["brief"]
                    break
            assert brief is not None
            assert brief["countdown"]["asset_id"] == "TANK_3"
            assert set(brief) >= {"countdown", "unknown_tanks", "plans", "do_nothing_usd", "last_call"}
            assert brief["plans"]["cheapest"]["total_usd"] < brief["do_nothing_usd"]
            assert brief["plans"]["robust"]["robust"] is True
            # An incident opened: Last Call runs on its own and the brief comes again with it.
            assert brief["last_call_pending"] is True
            assert _wait(lambda: feed._lc_task is not None and feed._lc_task.done(), timeout=30)
            for _ in range(100):
                m = ws.receive_json()
                if m["type"] == "brief" and not m["brief"]["last_call_pending"]:
                    assert m["brief"]["last_call"] is not None
                    break
            cd = c.post("/api/jarvis", json={"message": "time left"}).json()
            j = feed.jarvis
            got = run(j.run_tool("get_countdown", {}, {}))
            assert got["countdown"]["clock"] == brief["countdown"]["clock"]
            assert got["countdown"]["asset_id"] == "TANK_3" and cd["reply"] == got["text"]
        # A new socket gets the latest brief right after the state.
        with c.websocket_connect("/ws") as ws2:
            assert ws2.receive_json()["type"] == "state"
            assert ws2.receive_json()["type"] == "brief"


def test_brief_endpoint_and_feed_do_not_compute_with_nobody_listening():
    c, live = app_client()
    b = c.get("/api/jarvis/brief").json()
    assert b["countdown"] is None and b["plans"] == {"cheapest": None, "robust": None} and b["last_call"] is None
    assert b["feed_ok"] is True and b["unknown_tanks"] == []
    trip_http(c)
    assert live.brief_feed._task is None   # no socket: no background work
    b = c.get("/api/jarvis/brief").json()
    assert b["countdown"]["asset_id"] == "TANK_3" and b["countdown"]["wells_lost"]


# ---------- directives ----------

def test_directive_whitelist():
    live, _ = field(trip=False)
    ok = lambda d: T.check_directive(live, d)[0]   # noqa: E731
    bad = lambda d: T.check_directive(live, d)[1]   # noqa: E731
    assert ok({"action": "focus", "asset_id": "pump_3"})["asset_id"] == "PUMP_3"
    assert bad({"action": "focus", "asset_id": "PUMP_99"})
    assert bad({"action": "confirm", "asset_id": "PUMP_3"}) and bad({"action": "SET_VALVE"})
    assert ok({"action": "show_sensors", "asset_id": "ALL"})["asset_id"] == "ALL"
    hl = ok({"action": "highlight", "args": {"ids": ["TANK_3", "SENSOR_TANK_3", "PIPE_TANK_3_PUMP_3"] * 10}})
    assert len(hl["args"]["ids"]) == 3 and hl["args"]["seconds"] == 20
    assert bad({"action": "highlight", "args": {"ids": ["TANK_3", "rm -rf"]}})
    assert ok({"action": "trace_pipes", "asset_id": "TANK_3", "direction": "upstream"})["args"]["direction"] == "upstream"
    assert bad({"action": "trace_pipes", "asset_id": "TANK_3", "args": {"direction": "sideways"}})
    assert ok({"action": "set_time", "args": {"t_min": 62}})["args"]["t_min"] == 60
    assert bad({"action": "set_time", "args": {"t_min": 400}}) and bad({"action": "set_time", "args": {}})
    assert ok({"action": "open_panel", "args": {"panel": "plans"}})["args"] == {"panel": "plans"}
    assert bad({"action": "open_panel", "args": {"panel": "connectors"}})
    w = ok({"action": "try_whatif", "args": {"actions": [{"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "open"}]}})
    assert w["args"]["actions"][0]["value"] == "OPEN" and w["args"]["actions"][0]["at_min"] == 20   # manual: the rover's lead
    assert bad({"action": "try_whatif", "args": {"actions": [{"type": "SET_VALVE", "asset_id": "PUMP_3", "value": "x"}]}})
    assert ok({"action": "collapse", "asset_id": "PUMP_3"}) == {"type": "ui", "action": "collapse", "args": {}}


def test_at_most_three_directives_per_answer_and_each_is_broadcast():
    live, j = field(trip=False)
    sent = []

    async def bc(typ, payload):
        sent.append((typ, payload))

    live.hub.broadcast = bc
    ctx: dict = {}
    ids = [run(j.run_tool(t, a, ctx)) for t, a in [
        ("ui_explode", {"asset_id": "PUMP_3"}), ("ui_explode", {"asset_id": "PUMP_3"}),   # repeat: same id
        ("ui_show_sensors", {"asset_id": "ALL"}), ("ui_trace_pipes", {"asset_id": "TANK_3", "direction": None}),
        ("ui_reset_view", {})]]
    assert ids[0] == ids[1] and ids[0]["ok"]
    assert "error" in ids[4] and len(ctx["ui"]) == 3
    assert [p["action"] for t, p in sent if t == "ui"] == ["explode", "show_sensors", "trace_pipes"]
    assert all(p["id"] == d["id"] for (t, p), d in zip(sent, ctx["ui"]))
    assert run(j.run_tool("ui_focus", {"asset_id": "NOPE"}, {}))["error"].startswith("Unknown asset")


def test_every_tool_schema_is_strict_and_complete():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    for t in TOOLS:
        s = t["input_schema"]
        assert t.get("strict") is True and s["additionalProperties"] is False, t["name"]
        assert sorted(s["required"]) == sorted(s["properties"]), t["name"]
    for n in ("get_state", "list_alerts", "get_countdown", "compare_whatif", "get_last_call", "get_audit",
              "list_sensors", "describe_parts", "execute_plan", "offer_ack", "offer_shelve", *T.UI_TOOLS):
        assert n in names, n
    assert "robust_only" in next(t for t in TOOLS if t["name"] == "get_plans")["input_schema"]["properties"]
    for forbidden in ("confirm", "reject", "set_speed", "inject_fault", "activate_blueprint"):
        assert not any(forbidden in n for n in names)
    assert "ui_explode" in Jarvis(LiveField(healthy()), forecast_fn=None, plans_fn=None).system


# ---------- tools ----------

def test_state_alerts_and_countdown_tools():
    live, j = field()
    st = run(j.run_tool("get_state", {}, {}))
    assert st["incident"]["asset_id"] == "PUMP_3" and st["feed_ok"] and st["status_counts"]["ALARM"] >= 2
    al = T.list_alerts(live)
    assert al["priority_source"] == "forecast" and al["alerts"][0]["priority"] == "P1"
    assert al["alerts"][0]["minutes_to_consequence"] and al["alerts"][0]["usd_at_risk"] > 0
    assert T.list_alerts(live, priority="P3")["alerts"] == []
    cd = run(j.run_tool("get_countdown", {}, {}))
    assert cd["countdown"]["asset_id"] == "TANK_3" and len(cd["countdown"]["wells_lost"]) == 4
    assert cd["countdown"]["low_clock"] and cd["text"].startswith("The forecast at ") and "Tank 3 reaches shutdown" in cd["text"]


def test_list_alerts_groups_related_alarms_under_the_root():
    live, j = field()
    for _ in range(80):     # let Tank 3 climb into its high alarms
        live.step(1.0)
    grouped = T.list_alerts(live)["alerts"]
    flat = T.list_alerts(live, group=False)["alerts"]
    root = next(a for a in grouped if a["kind"] == "PUMP_TRIP")
    assert root["related_count"] >= 1 and len(flat) > len(grouped)
    assert all(a["root_id"] is None or a["root_id"] not in {x["id"] for x in grouped} for a in grouped)


def test_compare_whatif_and_preview():
    live, j = field()
    res = run(j.run_tool("compare_whatif", {"actions": [
        {"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "target_id": None, "at_min": None}]}, {}))
    assert res["before"]["shutdown"]["asset_id"] == "TANK_3" and res["gained_min"] > 0
    assert res["actions"][0]["at_min"] == 20.0    # the manual crossover needs the rover
    assert res["text"].startswith("If you open the Tank 3 to 4 crossover")
    assert "error" in run(j.run_tool("compare_whatif", {"actions": [{"type": "SET_VALVE", "asset_id": "TANK_3"}]}, {}))
    c, _ = app_client()
    trip_http(c)
    r = c.post("/api/jarvis/preview", json={"action": {"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"}})
    assert r.status_code == 200 and r.json()["after"]["cost_usd"] is not None
    assert c.post("/api/jarvis/preview", json={"action": {"type": "SEND_TRUCK", "asset_id": "TANK_1"}}).status_code == 422
    assert c.get("/api/actions").json()["proposals"] == []   # a preview proposes nothing


def test_execute_plan_proposes_one_step_at_a_time():
    live, j = field()
    out = run(j.handle("run the plan"))
    assert len(out["proposals"]) == 1 and "Step 1 of" in out["reply"]
    p1 = out["proposals"][0]
    assert p1["plan_step"] == 1 and p1["reason"].startswith("Plan step 1 of")
    again = run(j.handle("run the plan"))
    assert again["proposals"] == [] and "still waiting" in again["reply"]
    assert [p["status"] for p in live.proposals.values()] == ["PENDING"]
    run(live.confirm(p1["id"], by="console"))
    nxt = run(j.handle("run the plan"))
    assert len(nxt["proposals"]) == 1 and nxt["proposals"][0]["plan_step"] == 2, nxt["reply"]
    assert run(j.handle("don't run the plan"))["proposals"] == []
    assert run(j.handle("fix it?"))["proposals"] == []
    assert "no longer offered" in run(j.run_tool("execute_plan", {"plan_id": "plan_nope"}, {}))["error"]


def test_plans_robust_only_sensors_parts_audit():
    live, j = field()
    allp = run(j.run_tool("get_plans", {}, {}))["plans"]
    robust = run(j.run_tool("get_plans", {"robust_only": True}, {}))["plans"]
    assert robust and all(p["robust"] for p in robust) and len(robust) < len(allp)
    s = run(j.run_tool("list_sensors", {"asset_id": None}, {}))
    assert s["stale"] == [] and s["stale_after_min"] == 5
    tank = next(r for r in s["sensors"] if r["asset_id"] == "TANK_3")
    assert tank["nodes"] == ["SENSOR_TANK_3", "TANK_3_lt", "TANK_3_lshh"] and tank["connectors"] == ["CONN_SIM"]
    parts = run(j.run_tool("describe_parts", {"asset_id": "PUMP_3"}, {}))
    rotor = next(p for p in parts["parts"] if p["key"] == "rotor")
    assert rotor["label"] == "motor fan" and rotor["signal"] == "running" and rotor["value"] is False
    lshh = next(p for p in run(j.run_tool("describe_parts", {"asset_id": "TANK_3"}, {}))["parts"] if p["key"] == "lshh")
    assert "not wired to the console" in lshh["what"]
    assert run(j.run_tool("describe_parts", {"asset_id": "VALVE_XO_34"}, {}))["kind"] == "valve_xo"
    run(j.handle("open the crossover"))
    audit = run(j.run_tool("get_audit", {"asset_id": "VALVE_XO_34"}, {}))
    assert [e["event"] for e in audit["entries"]] == ["proposed"]
    assert "proposed" in run(j.handle("who opened the crossover"))["reply"]


def test_last_call_tool_and_its_missing_module(monkeypatch):
    live, j = field()
    lc = run(j.run_tool("get_last_call", {}, {}))
    if "error" not in lc:   # lastcall/ is on this branch
        assert lc["cheapest"]["minutes_left"] >= 0 and lc["cheapest"]["expires_clock"].endswith("M")
    monkeypatch.setitem(sys.modules, "lastcall.core", None)   # import now fails
    assert "isn't installed" in run(j.run_tool("get_last_call", {}, {}))["error"]
    boom = types.ModuleType("lastcall.core")

    def last_call(*a, **k):
        raise RuntimeError("broken")

    boom.last_call = last_call
    monkeypatch.setitem(sys.modules, "lastcall.core", boom)
    assert "Last Call failed" in run(j.run_tool("get_last_call", {}, {}))["error"]
    assert "Last Call failed" in run(j.handle("last call"))["reply"]


def test_lost_feed_is_never_all_clear():
    live, j = field(trip=False)
    real = live.registry.status

    def status():
        return real() + [{"id": "CONN_SCADA", "kind": "mqtt", "status": "unavailable", "detail": "broker down"}]

    live.registry.status = status
    reply = run(j.handle("what's failing"))["reply"]
    assert "CONN_SCADA" in reply and not any(bad in reply.lower() for bad in NEVER), reply
    assert run(j.run_tool("get_state", {}, {}))["feed_ok"] is False


def test_uncertain_send_is_not_failed():
    live, j = field(trip=False)
    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}))
    live.registry.sim.send = lambda a: {"ok": False, "uncertain": True, "detail": "No acknowledgement."}
    done = run(live.confirm(p["id"], by="console"))
    assert done["status"] == "UNCERTAIN" and "VALVE_XO_34" not in live.commanded
    assert [e["status"] for e in live.audit.entries() if e["event"] == "result"] == ["UNCERTAIN"]
