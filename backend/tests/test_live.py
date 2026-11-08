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


def test_send_that_raises_ends_failed_not_stuck():
    live, sent, _ = hooked_field()

    def boom(_):
        raise RuntimeError("socket closed")

    live.registry.get("CONN_HOOK").send = boom
    p = valve_open(live)
    done = run(live.confirm(p["id"]))
    assert done["status"] == "FAILED" and "socket closed" in done["result"]
    assert "VALVE_XO_34" not in live.commanded


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
    assert p["status"] == "CONFIRMED" and p["decided_by"] == "JARVIS_CONFIRM=0"


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
