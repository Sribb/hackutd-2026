"""The live layer (#38): connectors, detection, human-confirmed actions, /ws, Jarvis rules."""
import asyncio
import copy
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.connectors import stubs
from backend.app.connectors.stubs import MqttConnector
from backend.app.connectors.webhook import WebhookConnector
from backend.app.fieldmap import FieldMap
from backend.app.live import ActionError, LiveField
from backend.app.main import app
from backend.app.scenario import load_scenario


ROOT = Path(__file__).resolve().parents[2]


def run(coro):
    return asyncio.run(coro)


def healthy_scenario() -> dict:
    """The placeholder field with every pump running (no scripted trip)."""
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    return sc


def tick(live: LiveField, n: int = 1, dt: float = 0.25) -> list[dict]:
    out = []
    for _ in range(n):
        out += live.step(dt)
    return out


def trips(alerts):
    return [a for a in alerts if a["kind"] == "PUMP_TRIP" and a["active"]]


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


# ---------- detection ----------

def test_no_false_alerts_while_healthy():
    live = LiveField(healthy_scenario())
    assert tick(live, 40) == []
    assert all(a["status"] != "ALARM" for a in live.state()["assets"].values())


def test_pump_trip_alerts_within_3_readings():
    live = LiveField(healthy_scenario())
    tick(live, 8)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    raised = []
    for n in range(1, 4):
        raised = trips(tick(live))
        if raised:
            break
    assert raised and raised[0]["asset_id"] == "PUMP_3", "no PUMP_TRIP within 3 readings"
    a = raised[0]
    assert set(a) >= {"id", "asset_id", "kind", "severity", "message", "t"}
    assert a["severity"] == "ALARM"
    st = live.state()["assets"]
    assert st["PUMP_3"]["status"] == "ALARM" and st["TANK_3"]["status"] == "ALARM"


def test_pump_trip_from_flow_alone():
    """No run-status signal: zero flow while it should run is enough."""
    live = LiveField(healthy_scenario())
    live.registry.sim.skip.add(("PUMP_3", "running"))
    tick(live, 8)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP"})
    got = [trips(tick(live)) for _ in range(3)]
    assert any(got)
    assert "flow" in next(x for x in got if x)[0]["message"]


def test_stale_sensor_is_unknown_never_ok():
    live = LiveField(healthy_scenario())
    tick(live, 4, dt=1.0)
    assert live.state()["assets"]["TANK_2"]["status"] == "OK"
    live.registry.sim.inject({"asset_id": "TANK_2", "type": "SENSOR_STALE"})
    changed = tick(live, 7, dt=1.0)
    tank = live.state()["assets"]["TANK_2"]
    assert tank["status"] == "UNKNOWN" and tank["stale"] is True
    assert any(a["kind"] == "SENSOR_STALE" and a["asset_id"] == "TANK_2" for a in changed)
    # The forecast sees no level for it rather than an old, safe-looking one.
    assert live.live_scenario()["initial_levels_pct"]["TANK_2"] is None


def test_unknown_starting_level_is_unknown():
    sc = healthy_scenario()
    del sc["initial_levels_pct"]["TANK_5"]
    live = LiveField(sc)
    tick(live)
    assert live.state()["assets"]["TANK_5"]["status"] == "UNKNOWN"


def test_valve_mismatch():
    live = LiveField(healthy_scenario())
    live.registry.sim.inject({"asset_id": "VALVE_XO_34", "type": "STUCK_VALVE"})
    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}))
    assert run(live.confirm(p["id"]))["status"] == "CONFIRMED"
    changed = tick(live, 8)
    assert any(a["kind"] == "VALVE_MISMATCH" and a["asset_id"] == "VALVE_XO_34" for a in changed)
    assert live.state()["assets"]["VALVE_XO_34"]["status"] == "WARNING"


# ---------- human-confirmed actions ----------

def test_proposal_does_nothing_until_confirmed():
    live = LiveField(healthy_scenario())
    tick(live)
    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}, reason="relieve Tank 3"))
    assert p["status"] == "PENDING" and p["action"] == {"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}
    assert set(p) >= {"id", "action", "reason", "status", "created_t"}
    tick(live, 5)
    assert live.registry.sim.valve_open["VALVE_XO_34"] is False
    assert live.state()["assets"]["VALVE_XO_34"]["position"] == "CLOSED"

    done = run(live.confirm(p["id"], by="test"))
    assert done["status"] == "CONFIRMED" and done["result"]
    tick(live)
    assert live.state()["assets"]["VALVE_XO_34"]["position"] == "OPEN"
    with pytest.raises(ActionError) as e:
        run(live.confirm(p["id"]))
    assert e.value.status == 409


def test_rejected_proposal_never_runs():
    live = LiveField(healthy_scenario())
    p = run(live.propose({"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"}))
    assert run(live.reject(p["id"]))["status"] == "REJECTED"
    tick(live, 4)
    assert live.registry.sim.truck_jobs == []


def test_bad_proposal_is_refused():
    live = LiveField(healthy_scenario())
    with pytest.raises(ActionError) as e:
        run(live.propose({"type": "SET_VALVE", "asset_id": "PUMP_3", "value": "OPEN"}))
    assert e.value.status == 422


# ---------- a proposal is sent at most once (two screens, confirm vs reject) ----------

HOOK_RAW = {"connectors": [{"id": "CONN_HOOK", "kind": "webhook", "config": {"url": "http://gateway.local/cmd"},
                            "signals": [], "commands": [{"asset_id": "VALVE_XO_34", "command": "SET_VALVE",
                                                         "address": "xo34/cmd"}]}]}


def hooked_field(statuses_seen: list | None = None):
    """A field whose crossover valve is on a (slow) webhook, plus the list of commands it got.
    Every hub broadcast yields to the event loop, like a real socket send."""
    live = LiveField(healthy_scenario(), copy.deepcopy(HOOK_RAW))
    sent, lock = [], threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if statuses_seen is not None:
            statuses_seen.extend(p["status"] for p in live.proposals.values())
        time.sleep(0.05)  # the equipment takes a moment, so the other request arrives mid-send
        with lock:
            sent.append(body)
        return httpx.Response(200, json={"ok": True})

    live.registry.get("CONN_HOOK").transport = httpx.MockTransport(handler)
    broadcasts = []

    async def broadcast(typ, payload):
        if typ == "proposal":
            broadcasts.append(payload["proposal"]["status"])
        await asyncio.sleep(0)

    live.hub.broadcast = broadcast
    return live, sent, broadcasts


def valve_open(live):
    return run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}))


def test_two_concurrent_confirms_send_once():
    mid_send = []
    live, sent, broadcasts = hooked_field(mid_send)
    p = valve_open(live)

    async def both():
        return await asyncio.gather(live.confirm(p["id"], by="console"), live.confirm(p["id"], by="phone"),
                                    return_exceptions=True)

    results = run(both())
    ok = [r for r in results if isinstance(r, dict)]
    errs = [r for r in results if isinstance(r, ActionError)]
    assert len(ok) == 1 and len(errs) == 1, results
    assert errs[0].status == 409
    assert ok[0]["status"] == "CONFIRMED" and ok[0]["decided_by"] == "console"
    assert len(sent) == 1
    assert mid_send == ["SENDING"]  # claimed before the connector was called
    assert broadcasts == ["PENDING", "SENDING", "CONFIRMED"]  # every screen sees it go out


@pytest.mark.parametrize("reject_first", [False, True])
def test_reject_racing_confirm_never_both(reject_first):
    live, sent, _ = hooked_field()
    p = valve_open(live)

    async def race():
        calls = [live.confirm(p["id"], by="console"), live.reject(p["id"], by="phone")]
        return await asyncio.gather(*(reversed(calls) if reject_first else calls), return_exceptions=True)

    results = run(race())
    ok = [r for r in results if isinstance(r, dict)]
    errs = [r for r in results if isinstance(r, ActionError)]
    assert len(ok) == 1 and len(errs) == 1 and errs[0].status == 409, results
    final = live.proposals[p["id"]]["status"]
    if final == "REJECTED":
        assert sent == []
    else:
        assert final == "CONFIRMED" and len(sent) == 1
    assert final == ("REJECTED" if reject_first else "CONFIRMED")


def test_confirm_after_reject_is_409_and_sends_nothing():
    live, sent, _ = hooked_field()
    p = valve_open(live)
    assert run(live.reject(p["id"]))["status"] == "REJECTED"
    with pytest.raises(ActionError) as e:
        run(live.confirm(p["id"]))
    assert e.value.status == 409
    assert sent == [] and live.proposals[p["id"]]["status"] == "REJECTED"


def test_reject_while_sending_is_409():
    live, sent, _ = hooked_field()
    p = valve_open(live)

    async def go():
        task = asyncio.create_task(live.confirm(p["id"]))
        await asyncio.sleep(0.01)  # the send is in flight
        assert live.proposals[p["id"]]["status"] == "SENDING"
        with pytest.raises(ActionError) as e:
            await live.reject(p["id"])
        assert e.value.status == 409
        return await task

    assert run(go())["status"] == "CONFIRMED" and len(sent) == 1


def test_send_that_raises_ends_uncertain_not_stuck():
    """A real connector that raised may already have sent it: UNCERTAIN, never FAILED."""
    live, sent, _ = hooked_field()

    def boom(_):
        raise RuntimeError("socket closed")

    live.registry.get("CONN_HOOK").send = boom
    p = valve_open(live)
    done = run(live.confirm(p["id"]))
    assert done["status"] == "UNCERTAIN" and "socket closed" in done["result"]
    assert "check the field" in done["result"]
    assert "VALVE_XO_34" not in live.commanded


def test_simulator_send_that_raises_is_failed():
    live = LiveField(healthy_scenario())

    def boom(_):
        raise RuntimeError("bug")

    live.registry.sim.send = boom
    p = run(live.propose({"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"}))
    assert run(live.confirm(p["id"]))["status"] == "FAILED"


def test_auto_confirm_skips_a_proposal_a_person_already_decided(monkeypatch):
    """JARVIS_CONFIRM=0: if a person rejects while the proposal is being announced, it stays rejected."""
    monkeypatch.setenv("JARVIS_CONFIRM", "0")
    live = LiveField(healthy_scenario())
    assert live.auto_confirm_sim

    async def broadcast(typ, payload):
        if typ == "proposal" and payload["proposal"]["status"] == "PENDING":
            await live.reject(payload["proposal"]["id"], by="phone")
        await asyncio.sleep(0)

    live.hub.broadcast = broadcast
    p = run(live.propose({"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"}, source="jarvis"))
    assert p["status"] == "REJECTED"
    assert live.registry.sim.truck_jobs == []


def test_auto_confirm_still_confirms_sim_commands(monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIRM", "0")
    live = LiveField(healthy_scenario())
    p = run(live.propose({"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"}, source="jarvis"))
    assert p["status"] == "CONFIRMED" and p["decided_by"] == "jarvis"
    confirmed = [e for e in live.audit.entries() if e["event"] == "confirmed"]
    assert confirmed[-1]["source"] == "jarvis" and confirmed[-1]["auto"] == "JARVIS_CONFIRM=0"


def test_auto_confirm_never_sends_to_real_equipment(monkeypatch):
    monkeypatch.setenv("JARVIS_CONFIRM", "0")
    live, sent, _ = hooked_field()
    live.auto_confirm_sim = True
    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}, source="jarvis"))
    assert p["status"] == "PENDING" and sent == []


# ---------- connectors ----------

def test_mqtt_stub_without_library_is_unavailable(monkeypatch):
    def missing(mod):
        raise ImportError(f"No module named {mod!r}")

    monkeypatch.setattr(stubs.importlib, "import_module", missing)
    raw = {**json.loads(json.dumps({"connectors": [{
        "id": "CONN_SCADA", "kind": "mqtt", "config": {"host": "10.0.0.5", "password_env": "MQTT_PASSWORD"},
        "signals": [{"asset_id": "TANK_3", "signal": "level_pct", "address": "tank3/level"}],
        "commands": [{"asset_id": "VALVE_XO_34", "command": "SET_VALVE", "address": "xo34/cmd"}]}]}))}
    live = LiveField(healthy_scenario(), raw)
    conn = live.registry.get("CONN_SCADA")
    assert isinstance(conn, MqttConnector)
    st = conn.status()
    assert st["available"] is False and "paho-mqtt" in st["detail"]
    (row,) = [c for c in live.state()["connectors"] if c["id"] == "CONN_SCADA"]
    assert row["status"] == "unavailable" and row["kind"] == "mqtt"
    assert conn.read() == []
    # A command routed to it fails cleanly instead of crashing.
    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}))
    assert p["connector_id"] == "CONN_SCADA"
    assert run(live.confirm(p["id"]))["status"] == "FAILED"
    # Its tank gets no readings, so it shows UNKNOWN, never a simulated OK.
    tick(live, 3)
    assert live.state()["assets"]["TANK_3"]["status"] == "UNKNOWN"


def test_webhook_connector_posts_the_command(monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("HOOK_TOKEN", "s3cret")
    raw = {"connectors": [{"id": "CONN_HOOK", "kind": "webhook",
                           "config": {"url": "http://gateway.local/cmd", "token_env": "HOOK_TOKEN"},
                           "signals": [{"asset_id": "VALVE_XO_34", "signal": "position", "address": "xo34/pos"}],
                           "commands": [{"asset_id": "VALVE_XO_34", "command": "SET_VALVE", "address": "xo34/cmd"}]}]}
    live = LiveField(healthy_scenario(), raw)
    hook = live.registry.get("CONN_HOOK")
    assert isinstance(hook, WebhookConnector)
    hook.transport = httpx.MockTransport(handler)

    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}))
    assert seen == []  # nothing sent before confirm
    assert run(live.confirm(p["id"]))["status"] == "CONFIRMED"
    url, auth, body = seen[0]
    assert url == "http://gateway.local/cmd" and auth == "Bearer s3cret"
    assert body["command"] == "SET_VALVE" and body["asset_id"] == "VALVE_XO_34" and body["value"] == "OPEN"
    assert body["address"] == "xo34/cmd"

    # Readings pushed in reach the live state; undeclared signals are refused.
    assert not hook.authorized("wrong") and hook.authorized("s3cret")
    res = hook.push([{"address": "xo34/pos", "value": "OPEN"}, {"asset_id": "TANK_3", "signal": "level_pct", "value": 5}])
    assert res["accepted"] == ["VALVE_XO_34.position"] and len(res["rejected"]) == 1
    tick(live)
    assert live.state()["assets"]["VALVE_XO_34"]["position"] == "OPEN"


def test_fieldmap_reads_both_shapes():
    for path in (ROOT / "backend/data/scenario.json", ROOT / "scenario/field.json"):
        m = FieldMap.from_scenario(load_scenario(path))
        assert m.pump_valve["PUMP_3"] == "VALVE_D_3" and m.pump_swd["PUMP_3"]
        assert [x["id"] for x in m.crossovers] == ["VALVE_XO_34"]


# ---------- HTTP and /ws ----------

client = TestClient(app)


def test_ws_delivers_state():
    with client.websocket_connect("/ws?role=web") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "state"
        assert "PUMP_3" in msg["state"]["assets"] and msg["state"]["clock"]
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_jarvis_rules_answer_without_api_key():
    r = client.post("/api/jarvis", json={"message": "What's failing?"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "rules" and body["reply"]
    assert set(body) >= {"reply", "source", "ui", "proposals"}

    r = client.post("/api/jarvis", json={"message": "what am I looking at", "selected_asset_ids": ["TANK_3"]})
    assert r.status_code == 200 and "Tank 3" in r.json()["reply"]
    assert r.json()["ui"] == [{"type": "ui", "action": "focus", "asset_id": "TANK_3"}]


def test_jarvis_llm_path_without_key_is_503():
    r = client.post("/api/jarvis", json={"message": "Write me a haiku about brine"})
    assert r.status_code == 503
    assert r.json() == {"detail": "Jarvis needs ANTHROPIC_API_KEY on the server."}


def test_jarvis_proposes_then_human_confirms_over_http():
    r = client.post("/api/jarvis", json={"message": "send truck 2 to tank 3"})
    assert r.status_code == 200
    (p,) = r.json()["proposals"]
    assert p["status"] == "PENDING"
    ids = [x["id"] for x in client.get("/api/actions").json()["proposals"]]
    assert p["id"] in ids
    assert client.post(f"/api/actions/{p['id']}/confirm").json()["status"] == "CONFIRMED"
    assert client.post(f"/api/actions/{p['id']}/confirm").status_code == 409
    assert client.post("/api/actions/act_nope/reject").status_code == 404


def test_live_endpoints_and_existing_api_unchanged():
    st = client.get("/api/live/state").json()
    assert {"clock", "assets"} <= set(st)
    assert all(a["status"] in {"OK", "WARNING", "ALARM", "OFFLINE", "UNKNOWN"} for a in st["assets"].values())
    assert "alerts" in client.get("/api/alerts").json()
    assert client.get("/api/state").json()["clock"] == "01:47"


def test_live_state_connectors_and_reading_age():
    live = LiveField(healthy_scenario())
    tick(live, 2)
    st = live.state()
    (sim,) = st["connectors"]
    assert {k: sim[k] for k in ("id", "kind", "status")} == {"id": "CONN_SIM", "kind": "sim", "status": "connected"}
    tank = st["assets"]["TANK_3"]
    assert tank["age_min"] == 0 and tank["last_reading_t"].endswith("+00:00") and tank["stale"] is False
    assert st["assets"]["TRUCK_1"]["age_min"] is None and st["assets"]["TRUCK_1"]["last_reading_t"] is None


# ---------- regressions from Ronak's run against the real API (board #6715030068) ----------

def test_tank_rate_turns_positive_after_a_trip():
    """The observed fill rate is recomputed every tick, so it can't stay at the pre-trip (negative) value."""
    live = LiveField(healthy_scenario())
    tick(live, 40)  # 10 sim-minutes of a healthy, draining tank
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    tick(live, 60)  # 15 sim-minutes with the pump stopped
    rate = live.state()["assets"]["TANK_3"].get("observed_bbl_hr")
    assert rate is not None and rate > 0, f"Tank 3 should be filling after the trip, got {rate}"


def test_jarvis_why_is_tank_rising_names_cause_and_shutdown_time():
    from backend.app.forecast import forecast
    from backend.app.jarvis import Jarvis
    from backend.app.planner import make_plans
    live = LiveField(healthy_scenario())
    tick(live, 8)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    tick(live, 8)
    jarvis = Jarvis(live, forecast_fn=lambda sc, actions: forecast(sc, actions), plans_fn=lambda sc: make_plans(sc))
    out = run(jarvis.handle("Why is Tank 3 rising?"))
    reply = out["reply"]
    assert out["source"] == "rules"
    assert "Pump 3" in reply, reply
    assert "reaches shutdown at" in reply, reply


# ---------- regressions from Sriyan's IoT run: crossover choice, fix plans, rate sign, reload ----------

def jarvis_for(live: LiveField):
    from backend.app.forecast import forecast
    from backend.app.jarvis import Jarvis
    from backend.app.planner import make_plans
    return Jarvis(live, forecast_fn=lambda sc, actions: forecast(sc, actions), plans_fn=lambda sc: make_plans(sc))


def many_crossovers(levels: dict | None = None, pump_rates: dict | None = None) -> dict:
    """The placeholder field plus crossovers 1-2, 2-3, and 5-6, with 1-2 listed first,
    so 'the first crossover' is never the right answer by accident."""
    sc = healthy_scenario()

    def xo(a: int, b: int) -> dict:
        return {"id": f"VALVE_XO_{a}{b}", "connects": [f"TANK_{a}", f"TANK_{b}"], "max_bbl_hr": 90, "manual": True}

    sc["field"]["valves"] = [xo(1, 2), xo(2, 3), *sc["field"]["valves"], xo(5, 6)]
    sc["initial_levels_pct"].update(levels or {})
    for p in sc["field"]["pumps"]:
        p["rate_bbl_hr"] = (pump_rates or {}).get(p["id"], p["rate_bbl_hr"])
    return sc


def proposed_valve(out: dict) -> str:
    (p,) = out["proposals"]
    assert p["status"] == "PENDING" and p["action"]["type"] == "SET_VALVE" and p["action"]["value"] == "OPEN"
    return p["action"]["asset_id"]


@pytest.mark.parametrize("wording, valve", [
    ("open the Tank 3 to 4 crossover", "VALVE_XO_34"),
    ("Open the crossover from tank 3 to 4", "VALVE_XO_34"),
    ("open the crossover between 3 and 4", "VALVE_XO_34"),
    ("open the crossover between 5 and 6", "VALVE_XO_56"),
    ("open the crossover between tanks two and three", "VALVE_XO_23"),
    ("please open the tank 2 and tank 1 crossover", "VALVE_XO_12"),
])
def test_open_the_crossover_picks_the_named_tanks(wording, valve):
    live = LiveField(many_crossovers())
    tick(live, 2)
    out = run(jarvis_for(live).handle(wording))
    assert out["source"] == "rules"
    assert proposed_valve(out) == valve, out["reply"]


def test_open_the_crossover_follows_the_tripped_pump():
    live = LiveField(many_crossovers())
    tick(live, 8)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    tick(live, 4)
    assert [a["asset_id"] for a in trips(live.alerts())] == ["PUMP_3"]
    # Tank 3's neighbors: Tank 2 at 55.5%, Tank 4 at 42%. Tank 4 has more room.
    assert proposed_valve(run(jarvis_for(live).handle("open the crossover"))) == "VALVE_XO_34"


def test_open_the_crossover_prefers_the_neighbor_with_most_headroom():
    live = LiveField(many_crossovers(levels={"TANK_4": 86.0, "TANK_2": 40.0}))
    tick(live, 8)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    tick(live, 4)
    assert proposed_valve(run(jarvis_for(live).handle("Open the crossover."))) == "VALVE_XO_23"


def test_a_busy_disposal_well_is_not_a_pump_trip():
    """Pumps 4, 5, and 6 all running ask SWD_2 for 660 bbl/hr; it takes 520, so each moves
    less than its rate. That is the field working, not three tripped pumps (as after the
    crossover lifts Tank 4 above its pump's low level)."""
    sc = healthy_scenario()
    sc["initial_levels_pct"]["TANK_4"] = 86.0
    live = LiveField(sc)
    assert trips(tick(live, 60)) == []
    assert all(live.state()["assets"][p]["status"] == "OK" for p in ("PUMP_4", "PUMP_5", "PUMP_6"))


def test_open_the_crossover_without_a_trip_uses_the_earliest_forecast_shutdown():
    # Pump 5 can't keep up with its wells, so Tank 5 is the first to reach shutdown. No trip alert.
    live = LiveField(many_crossovers(levels={"TANK_5": 86.0}, pump_rates={"PUMP_5": 100}))
    tick(live, 4)
    assert not trips(live.alerts())
    assert proposed_valve(run(jarvis_for(live).handle("open the crossover"))) == "VALVE_XO_56"


def test_open_the_crossover_named_pair_with_none_says_so():
    live = LiveField(many_crossovers())
    out = run(jarvis_for(live).handle("open the crossover between tank 1 and 6"))
    assert out["proposals"] == [] and out["reply"] == "There's no crossover between Tank 1 and Tank 6."


def test_open_the_tank_3_to_4_crossover_over_http_without_a_key():
    """The exact wording 503'd: the asset lookup grabbed 'tank 3' and the rules gave up."""
    r = client.post("/api/jarvis", json={"message": "open the Tank 3 to 4 crossover"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "rules"
    (p,) = body["proposals"]
    assert p["action"] == {"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}
    assert p["status"] == "PENDING"  # waits for a person
    assert body["ui"][-1]["asset_id"] == "VALVE_XO_34"
    assert client.post(f"/api/actions/{p['id']}/reject").json()["status"] == "REJECTED"


def test_show_the_tank_3_to_4_crossover_describes_the_valve():
    live = LiveField(many_crossovers())
    tick(live)
    out = run(jarvis_for(live).handle("show me the Tank 3 to 4 crossover"))
    assert out["ui"] == [{"type": "ui", "action": "focus", "asset_id": "VALVE_XO_34"}]
    assert "crossover is closed" in out["reply"], out["reply"]


def test_why_is_tank_rising_and_cheapest_fix_adds_the_plans():
    from backend.app.planner import make_plans
    live = LiveField(healthy_scenario())
    tick(live, 8)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    tick(live, 8)
    out = run(jarvis_for(live).handle("Why is Tank 3 rising, and what's my cheapest fix?"))
    reply = out["reply"]
    assert out["source"] == "rules"
    assert "Pump 3" in reply and "reaches shutdown at" in reply, reply  # the why
    plans = make_plans(live.live_scenario())["plans"]
    cheapest = plans[0]
    assert f"Cheapest plan, ${cheapest['cost']['total_usd']:,.0f}: {cheapest['summary']}" in reply, reply
    robust = next(p for p in plans if p["robust"])
    assert robust["id"] != cheapest["id"]
    assert f"Cheapest robust plan, ${robust['cost']['total_usd']:,.0f}: {robust['summary']}" in reply, reply
    # The camera starts on the tank and ends on the cheapest plan's first step.
    assert out["ui"][0]["asset_id"] == "TANK_3"
    assert out["ui"][-1]["asset_id"] == cheapest["actions"][0]["asset_id"]
    assert out["proposals"] == []  # explaining a plan proposes nothing


def test_rate_right_after_a_trip_never_shows_the_draining_sign():
    """10 minutes of draining, then a trip: the next minutes must not say the rising tank is falling."""
    live = LiveField(healthy_scenario())
    tick(live, 40)
    assert live.state()["assets"]["TANK_3"]["observed_bbl_hr"] < 0
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    for _ in range(16):  # 4 sim-minutes, every tick
        tick(live)
        tank = live.state()["assets"]["TANK_3"]
        assert tank["observed_bbl_hr"] is None or tank["observed_bbl_hr"] > 0, tank
        assert tank["expected_bbl_hr"] == 188.0, tank  # Wells 9 to 12 in, no pump out
    reply = run(jarvis_for(live).handle("Why is Tank 3 rising?"))["reply"]
    assert "changing +" in reply and "changing -" not in reply, reply


def test_jarvis_omits_a_rate_that_disagrees_with_the_expected_sign():
    live = LiveField(healthy_scenario())
    tick(live, 4)
    live.detector.rates["TANK_3"] = {"observed_bbl_hr": -26.0, "expected_bbl_hr": 188.0}
    reply = run(jarvis_for(live).handle("how's tank 3"))["reply"]
    assert "Tank 3 is at" in reply and "changing" not in reply, reply
    live.detector.rates["TANK_3"] = {"observed_bbl_hr": 150.0, "expected_bbl_hr": 188.0}
    assert "changing +150 barrels an hour" in run(jarvis_for(live).handle("how's tank 3"))["reply"]


# ---------- the live layer follows the active blueprint ----------

def without_tank_6() -> dict:
    sc = healthy_scenario()
    f = sc["field"]
    f["tanks"] = [t for t in f["tanks"] if t["id"] != "TANK_6"]
    f["pumps"] = [p for p in f["pumps"] if p["id"] != "PUMP_6"]
    f["wells"] = [w for w in f["wells"] if w["tank_id"] != "TANK_6"]
    f["valves"] = [v for v in f["valves"] if v["id"] != "VALVE_D_6"]
    for tr in f["trucks"]:
        tr.get("minutes_to", {}).pop("TANK_6", None)
    sc["initial_levels_pct"].pop("TANK_6", None)
    return sc


def test_reload_follows_a_new_field_and_closes_what_is_gone():
    live = LiveField(healthy_scenario())
    closed = []
    live.registry.close = lambda: closed.append("old")
    tick(live, 8)
    live.registry.sim.inject({"asset_id": "PUMP_6", "type": "TRIP", "reason": "test"})
    tick(live, 4)
    (alert,) = trips(live.alerts())
    gone = run(live.propose({"type": "CHOKE_WELL", "asset_id": "WELL_21", "value": 0.5}))
    kept = run(live.propose({"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"}))
    t, clock = live.t, live.fmap.clock(live.t)

    changes = live.reload(without_tank_6())

    assert closed == ["old"]  # the old connectors were stopped
    assert live.t == t and live.fmap.clock(live.t) == clock  # the clock carries on
    st = live.state()
    assert "TANK_6" not in st["assets"] and "PUMP_6" not in st["assets"] and "TANK_5" in st["assets"]
    assert alert["active"] is False and "Pump 6 is not in the activated blueprint" in alert["resolved_reason"]
    assert [a["id"] for a in changes["resolved_alerts"]] == [alert["id"]]
    assert gone["status"] == "REJECTED" and gone["decided_by"] == "blueprint"
    assert "Well 21 is not in the activated blueprint" in gone["result"]
    assert [p["id"] for p in changes["closed_proposals"]] == [gone["id"]]
    with pytest.raises(ActionError) as e:  # a closed proposal can never be confirmed
        run(live.confirm(gone["id"]))
    assert e.value.status == 409
    # What still exists keeps working, and still only on a person's confirm.
    assert kept["status"] == "PENDING"
    assert run(live.confirm(kept["id"], by="test"))["status"] == "CONFIRMED"
    assert len(live.registry.sim.truck_jobs) == 1
    tick(live, 8)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    tick(live, 4)
    assert [a["asset_id"] for a in trips(live.alerts())] == ["PUMP_3"]
    ids = [a["id"] for a in live.alerts()]
    assert len(ids) == len(set(ids))  # alert ids keep counting, never reused


def test_reload_closes_a_proposal_that_would_now_go_to_real_equipment():
    live = LiveField(healthy_scenario())
    p = valve_open(live)
    assert p["connector_id"] == "CONN_SIM"
    live.reload(healthy_scenario(), copy.deepcopy(HOOK_RAW))
    assert p["status"] == "REJECTED" and "CONN_HOOK" in p["result"] and "Propose it again" in p["result"]
    assert live.registry.route(p["action"]).id == "CONN_HOOK"


def test_live_reload_hook_from_a_worker_thread_runs_on_the_loop_and_broadcasts():
    """#64's _use_scenario is a sync endpoint, so it calls the hook from a worker thread."""
    from backend.app.live_api import make_reload
    live = LiveField(healthy_scenario())
    jarvis = jarvis_for(live)
    assert "TANK_6" in jarvis.system
    sent = []

    async def broadcast(typ, payload):
        sent.append((typ, threading.get_ident()))

    live.hub.broadcast = broadcast
    reload = make_reload(live, jarvis)

    async def main():
        live.start()
        try:
            return threading.get_ident(), await asyncio.to_thread(reload, without_tank_6())
        finally:
            await live.stop()

    loop_thread, changes = run(main())
    assert changes["assets"] == len(live.fmap.all_ids())
    assert "TANK_6" not in live.state()["assets"]
    assert "TANK_6" not in jarvis.system  # Claude's asset list follows too
    assert ("state", loop_thread) in sent  # a fresh state went out, from the live loop


def test_app_state_live_reload_is_installed():
    assert callable(app.state.live_reload)
    before = app.state.live
    changes = app.state.live_reload(load_scenario())
    assert app.state.live is before and changes["assets"] == len(before.fmap.all_ids())
    assert client.get("/api/live/state").json()["assets"]["TANK_3"]["level_pct"] is not None
