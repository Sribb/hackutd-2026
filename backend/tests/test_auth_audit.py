"""Control tokens (#103, CF-15) and the audit log (CF-10).

Each test builds its own app with live_api.install, so tokens set here can't leak into
the shared app the other tests use."""
import asyncio
import copy
import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import auth
from backend.app.audit import AuditLog
from backend.app.live import LiveField
from backend.app.live_api import install
from backend.app.scenario import load_scenario

OP, PH, PIN = "op-secret-1", "phone-secret-2", "4321"
DENIED = {"detail": "Enter the operator PIN to send commands."}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC",
                "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN", "FREEBOARD_AUDIT_FILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setenv("FREEBOARD_TOKEN", OP)
    monkeypatch.setenv("FREEBOARD_PHONE_TOKEN", PH)


def healthy_scenario() -> dict:
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    return sc


def make_app():
    app = FastAPI()
    live = install(app, healthy_scenario())
    return TestClient(app), live


def h(tok):
    return {"X-Freeboard-Token": tok}


TRUCK = {"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"}


# ---------- tokens unset: exactly as before ----------

def test_no_token_configured_is_open_as_before():
    c, live = make_app()
    assert c.post("/api/live/faults", json={"asset_id": "PUMP_3", "type": "TRIP"}).status_code == 200
    assert c.post("/api/live/sim", json={"paused": True}).status_code == 200
    p = c.post("/api/actions", json=TRUCK).json()
    out = c.post(f"/api/actions/{p['id']}/confirm", json={"by": "Dana"}).json()
    assert out["status"] == "CONFIRMED" and out["decided_by"] == "Dana"   # body.by, as today
    q = c.post("/api/actions", json={**TRUCK, "asset_id": "TRUCK_2"}).json()
    assert c.post(f"/api/actions/{q['id']}/reject").json()["decided_by"] is None
    with c.websocket_connect("/ws?role=phone") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "ui", "action": "focus", "asset_id": "TANK_3"})
        msg = ws.receive_json()
        assert msg["type"] == "ui" and msg["asset_id"] == "TANK_3"


# ---------- tokens set ----------

def test_writes_need_a_token_and_reads_stay_open(tokens):
    c, live = make_app()
    for path, body in [("/api/actions", TRUCK), ("/api/live/faults", {"asset_id": "PUMP_3"}),
                       ("/api/live/sim", {"paused": True}), ("/api/jarvis", {"message": "what's failing"}),
                       ("/api/actions/act_1/confirm", {}), ("/api/actions/act_1/reject", {}),
                       ("/api/alerts/alert_1/ack", {})]:
        for headers in ({}, h("wrong"), h("")):
            r = c.post(path, json=body, headers=headers)
            assert r.status_code == 401 and r.json() == DENIED, (path, headers, r.text)
    for path in ("/api/live/state", "/api/alerts", "/api/actions", "/api/audit", "/api/live/connectors"):
        assert c.get(path).status_code == 200, path
    assert live.proposals == {} and len(live.audit) == 0


def test_confirm_without_token_sends_nothing(tokens):
    c, live = make_app()
    p = c.post("/api/actions", json=TRUCK, headers=h(OP)).json()
    r = c.post(f"/api/actions/{p['id']}/confirm", json={"by": "operator"})
    assert r.status_code == 401 and r.json() == DENIED
    assert live.proposals[p["id"]]["status"] == "PENDING" and live.registry.sim.truck_jobs == []


def test_phone_can_confirm_but_not_inject_faults(tokens):
    c, live = make_app()
    for path, body in [("/api/live/faults", {"asset_id": "PUMP_3", "type": "TRIP"}),
                       ("/api/live/sim", {"speed": 30}), ("/api/actions", TRUCK),
                       ("/api/jarvis", {"message": "send truck 1 to tank 3"})]:
        r = c.post(path, json=body, headers=h(PH))
        assert r.status_code == 403, (path, r.text)
    assert live.proposals == {} and live.speed != 30
    assert c.post("/api/live/faults", json={"asset_id": "PUMP_3", "type": "TRIP"}, headers=h(OP)).status_code == 200

    p = c.post("/api/actions", json=TRUCK, headers=h(OP)).json()
    out = c.post(f"/api/actions/{p['id']}/confirm", json={"by": "operator"}, headers=h(PH)).json()
    # The body says "operator", the token says phone: the token wins.
    assert out["status"] == "CONFIRMED" and out["decided_by"] == "phone"
    assert live.registry.sim.truck_jobs
    q = c.post("/api/actions", json={**TRUCK, "asset_id": "TRUCK_2"}, headers=h(OP)).json()
    assert c.post(f"/api/actions/{q['id']}/reject", json={"by": "console"}, headers=h(PH)).json()["decided_by"] == "phone"


def test_operator_token_and_pin_decide_as_console(monkeypatch):
    monkeypatch.setenv("OPERATOR_PIN", PIN)   # the PIN alone turns the lock on
    c, live = make_app()
    assert c.post("/api/actions", json=TRUCK).status_code == 401
    p = c.post("/api/actions", json=TRUCK, headers=h(PIN)).json()
    out = c.post(f"/api/actions/{p['id']}/confirm", json={"by": "phone"}, headers=h(PIN)).json()
    assert out["decided_by"] == "console"
    monkeypatch.setenv("FREEBOARD_TOKEN", OP)
    assert c.post("/api/live/sim", json={"paused": False}, headers=h(OP)).status_code == 200
    assert c.post("/api/live/sim", json={"paused": False}, headers=h(PIN)).status_code == 200


def test_ws_needs_a_valid_token_to_steer_the_screens(tokens):
    c, live = make_app()
    # No token: read-only. State arrives, a `ui` message is ignored, ping still answers.
    with c.websocket_connect("/ws?role=phone") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "ui", "action": "focus", "asset_id": "TANK_3"})
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
    # A wrong token is closed with 1008.
    with c.websocket_connect("/ws?role=phone&token=nope") as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 1008
    # The phone token may point every console at an asset.
    with c.websocket_connect(f"/ws?role=phone&token={PH}") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "ui", "action": "focus", "asset_id": "TANK_3"})
        msg = ws.receive_json()
        assert msg["type"] == "ui" and msg["asset_id"] == "TANK_3"


def test_require_role_works_on_any_route(tokens):
    app = FastAPI()

    @app.post("/x")
    def x(caller: auth.Caller = Depends(auth.require_role("operator"))) -> dict:
        return {"role": caller.role, "device": caller.device}

    @app.post("/y")
    def y(caller: auth.Caller = Depends(auth.require_role("phone"))) -> dict:
        return {"role": caller.role}

    c = TestClient(app)
    assert c.post("/x").status_code == 401
    assert c.post("/x", headers=h(PH)).status_code == 403
    assert c.post("/x", headers=h(OP)).json() == {"role": "operator", "device": "console"}
    assert c.post("/y", headers=h(PH)).json() == {"role": "phone"}
    assert c.post("/y", headers=h(OP)).json() == {"role": "operator"}
    with pytest.raises(ValueError):
        auth.require_role("admin")


# ---------- audit ----------

def test_audit_lists_the_demo_path_in_order(tokens):
    """Pump 3 trips, Jarvis proposes the crossover, the phone acks and confirms."""
    c, live = make_app()
    assert c.post("/api/live/faults", json={"asset_id": "PUMP_3", "type": "TRIP", "reason": "demo"},
                  headers=h(OP)).status_code == 200
    for _ in range(4):
        c.post("/api/live/sim", json={"advance_min": 0.5}, headers=h(OP))
    trip = next(a for a in c.get("/api/alerts").json()["alerts"] if a["kind"] == "PUMP_TRIP")
    acked = c.post(f"/api/alerts/{trip['id']}/ack", headers=h(PH)).json()
    assert acked["acked"] is True and acked["acked_by"] == "phone"

    r = c.post("/api/jarvis", json={"message": "Open the crossover"}, headers=h(OP))
    assert r.status_code == 200, r.text
    (p,) = r.json()["proposals"]
    assert p["status"] == "PENDING"
    out = c.post(f"/api/actions/{p['id']}/confirm", json={"by": "operator"}, headers=h(PH)).json()
    assert out["status"] == "CONFIRMED"

    entries = c.get("/api/audit").json()["entries"]
    assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))
    steps = [(e["event"], e["source"], e["role"], e.get("status")) for e in entries]
    assert steps == [
        ("acked", "phone", "phone", None),
        ("proposed", "jarvis", "operator", "PENDING"),
        ("confirmed", "phone", "phone", "SENDING"),
        ("result", "phone", "phone", "CONFIRMED"),
    ], steps
    ack, proposed, confirmed, result = entries
    assert ack["asset_id"] == "PUMP_3" and ack["alert_id"] == trip["id"]
    assert proposed["proposal_id"] == confirmed["proposal_id"] == result["proposal_id"] == p["id"]
    assert proposed["asset_id"] == p["action"]["asset_id"] and confirmed["by"] == "phone"
    assert all(e["clock"] and e["t"].endswith("+00:00") and e["live_t"] is not None for e in entries)
    # A second ack is not a new event; `since` skips what a poller already has.
    c.post(f"/api/alerts/{trip['id']}/ack", headers=h(PH))
    assert len(c.get("/api/audit").json()["entries"]) == 4
    assert [e["event"] for e in c.get("/api/audit?since=2").json()["entries"]] == ["confirmed", "result"]


def test_audit_records_reject_and_other_lanes_can_append(tokens):
    c, live = make_app()
    p = c.post("/api/actions", json=TRUCK, headers=h(OP)).json()
    c.post(f"/api/actions/{p['id']}/reject", headers=h(OP))
    c.app.state.audit.append("approved", source="console", role="operator", plan_id="plan_1")
    events = [(e["event"], e["source"], e["role"]) for e in c.get("/api/audit").json()["entries"]]
    assert events == [("proposed", "console", "operator"), ("rejected", "console", "operator"),
                      ("approved", "console", "operator")]
    assert c.get("/api/audit").json()["entries"][-1]["plan_id"] == "plan_1"


def test_audit_file_is_append_only_jsonl(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FREEBOARD_AUDIT_FILE", str(path))
    live = LiveField(healthy_scenario())
    p = asyncio.run(live.propose(TRUCK, source="console", role="operator"))
    asyncio.run(live.confirm(p["id"], by="console", role="operator", source="console"))
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["event"] for x in lines] == ["proposed", "confirmed", "result"]
    assert lines == live.audit.entries()
    # A relative path lands under backend/data/.
    monkeypatch.setenv("FREEBOARD_AUDIT_FILE", "audit-test.jsonl")
    assert AuditLog().path.parent.name == "data"


# ---------- Jarvis never vouches for a blind tank ----------

def test_how_long_until_shutdown_never_vouches_for_an_unknown_tank():
    from backend.app.forecast import forecast
    from backend.app.jarvis import Jarvis
    from backend.app.planner import make_plans
    live = LiveField(healthy_scenario())
    for _ in range(4):
        live.step(1.0)
    jarvis = Jarvis(live, forecast_fn=lambda sc, a: forecast(sc, a), plans_fn=lambda sc: make_plans(sc))
    before = asyncio.run(jarvis.handle("How long until shutdown?"))["reply"]
    live.registry.sim.inject({"asset_id": "TANK_2", "type": "SENSOR_STALE"})
    for _ in range(7):
        live.step(1.0)
    assert live.state()["assets"]["TANK_2"]["status"] == "UNKNOWN"
    out = asyncio.run(jarvis.handle("How long until shutdown?"))
    reply = out["reply"]
    assert "No tank reaches shutdown" not in reply, reply
    assert "can't vouch for Tank 2" in reply and "at risk" in reply, reply
    assert "TANK_2 has no level reading" not in reply, reply   # said once, in plain words
    if "No tank reaches shutdown" in before:   # nothing else at risk: say only the tanks with readings are fine
        assert "No tank with a fresh reading reaches shutdown" in reply
        assert out["ui"][-1]["asset_id"] == "TANK_2"
