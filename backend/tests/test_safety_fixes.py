"""Round-3 safety fixes on #38: PIN brute force, tokens out of logs, reload races,
webhook UNCERTAIN, and audit gaps. Each test fails on the code before the fix."""
import asyncio
import copy
import json
import logging
import threading
import time

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import auth, live_api
from backend.app.live import RELOADED_BEFORE_SEND, LiveField
from backend.app.live_api import install
from backend.tests.test_live import HOOK_RAW, healthy_scenario, hooked_field, jarvis_for, without_tank_6

OP, PH, PIN = "op-secret-1", "phone-secret-2", "482193"
TRUCK = {"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"}
VALVE = {"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC",
                "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN", "FREEBOARD_AUDIT_FILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setenv("FREEBOARD_TOKEN", OP)
    monkeypatch.setenv("OPERATOR_PIN", PIN)
    monkeypatch.setenv("FREEBOARD_PHONE_TOKEN", PH)


class AsIP:
    """ASGI wrapper: the X-Test-IP header (or ?ip=) becomes the client address, so one
    TestClient can stand in for several devices."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            ip = dict(scope.get("headers") or []).get(b"x-test-ip")
            if ip:
                scope = {**scope, "client": (ip.decode(), 50000)}
        await self.app(scope, receive, send)


def make_app():
    app = FastAPI()
    live = install(app, healthy_scenario())
    return TestClient(AsIP(app)), live


def h(tok=None, ip="10.0.0.9"):
    out = {"X-Test-IP": ip}
    if tok is not None:
        out["X-Freeboard-Token"] = tok
    return out


# ---------- 1. the PIN can't be brute-forced ----------

def test_five_wrong_pins_lock_that_ip_and_others_keep_working(tokens):
    c, live = make_app()
    for guess in ("111111", "222222", "333333", "444444", "555555"):
        r = c.post("/api/live/sim", json={"paused": True}, headers=h(guess, "10.0.0.66"))
        assert r.status_code == 401, r.text
    # Locked: even the right PIN is refused without being checked.
    r = c.post("/api/live/sim", json={"paused": True}, headers=h(PIN, "10.0.0.66"))
    assert r.status_code == 429 and r.json() == {"detail": "Too many wrong PINs. Wait 60 s."}
    assert r.headers["retry-after"] == "60"
    # The console on another IP still works with the right PIN.
    assert c.post("/api/live/sim", json={"paused": True}, headers=h(PIN, "10.0.0.2")).status_code == 200
    failed = [e for e in live.audit.entries() if e["event"] == "auth_failed"]
    assert len(failed) == 5 and {e["source"] for e in failed} == {"10.0.0.66"}
    assert failed[-1]["locked_s"] == 60 and failed[0]["path"] == "/api/live/sim"
    dumped = json.dumps(live.audit.entries())
    assert not any(g in dumped for g in ("111111", "555555", PIN))   # never the value tried


def test_a_missing_token_is_a_prompt_not_a_guess(tokens):
    c, _ = make_app()
    for _ in range(8):
        assert c.post("/api/live/sim", json={}, headers=h(None, "10.0.0.7")).status_code == 401
    assert c.post("/api/live/sim", json={}, headers=h(PIN, "10.0.0.7")).status_code == 200


def test_a_phone_token_does_not_reset_the_count_while_guessing_the_pin(tokens):
    c, _ = make_app()
    ip = "10.0.0.50"
    for g in ("1", "2", "3", "4"):
        assert c.post("/api/live/sim", json={}, headers=h(g, ip)).status_code == 401
    assert c.post("/api/live/sim", json={}, headers=h(PH, ip)).status_code == 403
    assert c.post("/api/live/sim", json={}, headers=h("5", ip)).status_code == 401
    assert c.post("/api/live/sim", json={}, headers=h(PIN, ip)).status_code == 429


def test_the_lock_doubles_up_to_15_minutes():
    now = [1000.0]
    t = auth.Throttle(clock=lambda: now[0])
    locks = []
    for _ in range(6):
        for _ in range(5):
            got = t.failed("x")
        locks.append(got)
        assert t.wait_s("x") == got
        now[0] += got + 1   # wait it out
        assert t.wait_s("x") == 0
    assert locks == [60, 120, 240, 480, 900, 900]
    now[0] += 901   # quiet for 15 min after the lock: back to 60 s
    for _ in range(5):
        got = t.failed("x")
    assert got == 60


def test_the_global_cap_locks_new_ips_but_not_one_that_used_a_correct_token():
    now = [0.0]
    t = auth.Throttle(clock=lambda: now[0])
    t.succeeded("console")
    for i in range(30):
        t.failed(f"10.1.0.{i}")   # one guess each: no single IP is locked
    assert t.wait_s("10.9.9.9") == 60
    assert t.wait_s("console") == 0


def test_ws_tokens_are_throttled_too(tokens):
    c, live = make_app()
    for g in ("1", "2", "3", "4", "5"):
        with c.websocket_connect(f"/ws?token={g}", headers={"X-Test-IP": "10.0.0.77"}) as ws:
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 1008
    with c.websocket_connect(f"/ws?token={PH}", headers={"X-Test-IP": "10.0.0.77"}) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 1008 and "Too many wrong PINs" in (e.value.reason or "")
    assert sum(e["event"] == "auth_failed" for e in live.audit.entries()) == 5


def test_require_role_sets_request_state_role(tokens):
    app = FastAPI()

    @app.post("/x")
    def x(request: Request, caller: auth.Caller = Depends(auth.require_role("phone"))) -> dict:
        return {"state_role": request.state.role}

    assert TestClient(app).post("/x", headers={"X-Freeboard-Token": PH}).json() == {"state_role": "phone"}


# ---------- 2. tokens stay out of the logs ----------

def until_pong(ws) -> list[dict]:
    ws.send_json({"type": "ping"})
    out = []
    while True:
        m = ws.receive_json()
        if m["type"] == "pong":
            return out
        out.append(m)


def test_ws_auth_with_a_first_message(tokens):
    c, _ = make_app()
    with c.websocket_connect("/ws?role=phone") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "auth", "token": PH})
        got = until_pong(ws)
        assert {"type": "auth", "ok": True, "role": "phone"}.items() <= next(
            m for m in got if m["type"] == "auth").items()
        ws.send_json({"type": "ui", "action": "focus", "asset_id": "TANK_3"})
        assert any(m["type"] == "ui" and m["asset_id"] == "TANK_3" for m in until_pong(ws))


def test_ws_first_message_with_a_wrong_token_is_closed(tokens):
    c, _ = make_app()
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "auth", "token": "nope"})
        ws.send_json({"type": "ping"})
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 1008


def test_ws_auth_only_as_the_first_message_and_in_time(tokens, monkeypatch):
    c, _ = make_app()
    with c.websocket_connect("/ws") as ws:   # not first: refused, stays read-only
        assert ws.receive_json()["type"] == "state"
        until_pong(ws)
        ws.send_json({"type": "auth", "token": PH})
        (msg,) = [m for m in until_pong(ws) if m["type"] == "auth"]
        assert msg["ok"] is False
        ws.send_json({"type": "ui", "action": "focus", "asset_id": "TANK_3"})
        assert not any(m["type"] == "ui" for m in until_pong(ws))
    monkeypatch.setattr(live_api, "WS_AUTH_S", -1.0)   # too late
    with c.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "auth", "token": PH})
        (msg,) = [m for m in until_pong(ws) if m["type"] == "auth"]
        assert msg["ok"] is False


def test_ws_auth_with_a_subprotocol(tokens):
    c, _ = make_app()
    with c.websocket_connect("/ws?role=phone", subprotocols=["freeboard", f"freeboard.{PH}"]) as ws:
        assert ws.accepted_subprotocol == "freeboard"
        assert ws.receive_json()["type"] == "state"
        ws.send_json({"type": "ui", "action": "focus", "asset_id": "TANK_3"})
        assert any(m["type"] == "ui" for m in until_pong(ws))
    with c.websocket_connect("/ws", subprotocols=["freeboard", "freeboard.nope"]) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 1008


def test_uvicorn_logs_never_show_a_token(caplog):
    from uvicorn.logging import AccessFormatter
    make_app()   # install() adds the filter
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    access = logging.getLogger("uvicorn.access")
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/ws?role=phone&token=482193", "1.1", 101)
    logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" [accepted]', "127.0.0.1:5001",
                                            "/ws?token=op-secret-1&role=web")
    logging.getLogger("uvicorn.error").info("headers %s", [(b"x-freeboard-token", b"op-secret-1"),
                                                           (b"sec-websocket-protocol", b"freeboard, freeboard.482193")])
    assert "482193" not in caplog.text and "op-secret-1" not in caplog.text, caplog.text
    assert "token=[redacted]" in caplog.text and "role=phone" in caplog.text
    # uvicorn's own access formatter still gets the argument shapes it expects.
    line = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s').format(caplog.records[0])
    assert "/ws?role=phone&token=[redacted]" in line and "101" in line


# ---------- 3. reload races ----------

def reload_on(live, status: str, scenario, raw=None):
    """Make the hub run live.reload(...) when a proposal broadcast with `status` goes out."""
    before = live.hub.broadcast

    async def broadcast(typ, payload):
        await before(typ, payload)
        if typ == "proposal" and payload["proposal"]["status"] == status and not getattr(live, "_did", False):
            live._did = True
            live.reload(scenario, raw)

    live.hub.broadcast = broadcast


def test_a_reload_between_claim_and_send_sends_nothing_to_real_equipment():
    live, sent, _ = hooked_field()
    p = run(live.propose(VALVE))
    reload_on(live, "SENDING", healthy_scenario(), copy.deepcopy(HOOK_RAW))
    done = run(live.confirm(p["id"], by="console"))
    assert sent == []
    assert done["status"] == "FAILED" and done["result"] == RELOADED_BEFORE_SEND
    assert "VALVE_XO_34" not in live.commanded
    result = [e for e in live.audit.entries() if e["event"] == "result"][-1]
    assert result["status"] == "FAILED" and result["proposal_id"] == p["id"]


def test_a_reload_between_claim_and_send_moves_no_simulated_valve():
    live = LiveField(healthy_scenario())
    old_sim = live.registry.sim
    p = run(live.propose(VALVE))
    reload_on(live, "SENDING", healthy_scenario())
    done = run(live.confirm(p["id"]))
    assert done["status"] == "FAILED" and done["result"] == RELOADED_BEFORE_SEND
    assert old_sim.valve_open["VALVE_XO_34"] is False and live.registry.sim.valve_open["VALVE_XO_34"] is False
    assert "VALVE_XO_34" not in live.commanded


def test_a_reload_during_the_send_keeps_its_result_but_not_commanded():
    live = LiveField(healthy_scenario(), copy.deepcopy(HOOK_RAW))
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        live.reload(healthy_scenario(), copy.deepcopy(HOOK_RAW))   # lands while the POST is out
        return httpx.Response(200)

    live.registry.get("CONN_HOOK").transport = httpx.MockTransport(handler)
    p = run(live.propose(VALVE))
    done = run(live.confirm(p["id"]))
    assert len(sent) == 1 and done["status"] == "CONFIRMED"
    assert "VALVE_XO_34" not in live.commanded


@pytest.mark.parametrize("change", ["url", "address"])
def test_a_reload_that_changes_the_connector_config_closes_the_proposal(change):
    live = LiveField(healthy_scenario(), copy.deepcopy(HOOK_RAW))
    p = run(live.propose(VALVE))
    raw = copy.deepcopy(HOOK_RAW)
    if change == "url":
        raw["connectors"][0]["config"]["url"] = "http://other-gateway.local/cmd"
    else:
        raw["connectors"][0]["commands"][0]["address"] = "xo99/cmd"
    live.reload(healthy_scenario(), raw)
    assert p["status"] == "REJECTED" and p["decided_by"] == "blueprint"
    assert "CONN_HOOK is set up differently" in p["result"]


def test_a_reload_with_the_same_config_keeps_the_proposal():
    live = LiveField(healthy_scenario(), copy.deepcopy(HOOK_RAW))
    p = run(live.propose(VALVE))
    live.reload(healthy_scenario(), copy.deepcopy(HOOK_RAW))
    assert p["status"] == "PENDING"


@pytest.mark.parametrize("change", ["route", "config"])
def test_confirm_refuses_when_the_command_path_changed(change):
    live, sent, _ = hooked_field()
    p = run(live.propose(VALVE))
    hook = live.registry.get("CONN_HOOK")
    if change == "route":
        hook.commands.clear()   # it would now go to the simulator
    else:
        hook.config["url"] = "http://other-gateway.local/cmd"
    with pytest.raises(live_api.ActionError) as e:
        run(live.confirm(p["id"]))
    assert e.value.status == 409 and "the command path changed; propose again" in e.value.detail
    assert sent == [] and p["status"] == "PENDING" and live.registry.sim.valve_open["VALVE_XO_34"] is False


def loop_in_thread():
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    return loop, th


def test_reload_timeout_calls_the_reload_off_instead_of_applying_it_later():
    live = LiveField(healthy_scenario())
    loop, th = loop_in_thread()
    live.loop = loop
    try:
        reload = live_api.make_reload(live, jarvis_for(live), timeout_s=0.2)
        loop.call_soon_threadsafe(time.sleep, 0.8)   # the loop is busy
        with pytest.raises(live_api.ReloadTimeout):
            reload(without_tank_6())
        time.sleep(1.0)   # the loop is free again
        assert "TANK_6" in live.fmap.all_ids() and live.generation == 0
    finally:
        loop.call_soon_threadsafe(loop.stop)
        th.join(2)


def test_reload_that_started_returns_its_changes_even_past_the_timeout():
    live = LiveField(healthy_scenario())

    async def slow_broadcast(typ, payload):
        await asyncio.sleep(0.4)

    live.hub.broadcast = slow_broadcast
    loop, th = loop_in_thread()
    live.loop = loop
    try:
        changes = live_api.make_reload(live, jarvis_for(live), timeout_s=0.1)(without_tank_6())
        assert changes["assets"] == len(live.fmap.all_ids()) and "TANK_6" not in live.fmap.all_ids()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        th.join(2)


# ---------- 4. webhook: an answer that never came back is UNCERTAIN ----------

def webhook_field(raise_exc):
    live = LiveField(healthy_scenario(), copy.deepcopy(HOOK_RAW))
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        raise raise_exc(request)

    live.registry.get("CONN_HOOK").transport = httpx.MockTransport(handler)
    return live, calls


@pytest.mark.parametrize("exc", [
    lambda r: httpx.ReadTimeout("timed out", request=r),
    lambda r: httpx.ReadError("[Errno 54] Connection reset by peer", request=r),
    lambda r: httpx.RemoteProtocolError("Server disconnected without sending a response.", request=r),
])
def test_webhook_no_answer_after_the_post_is_uncertain_and_never_resent(exc):
    live, calls = webhook_field(exc)
    p = run(live.propose(VALVE))
    done = run(live.confirm(p["id"]))
    assert done["status"] == "UNCERTAIN", done
    assert "check the field" in done["result"] and "Couldn't reach" not in done["result"]
    assert "VALVE_XO_34" not in live.commanded
    with pytest.raises(live_api.ActionError):   # and nothing sends it again
        run(live.confirm(p["id"]))
    assert len(calls) == 1
    assert [e["status"] for e in live.audit.entries() if e["event"] == "result"] == ["UNCERTAIN"]


def test_webhook_connect_error_is_failed_couldnt_reach():
    live, calls = webhook_field(lambda r: httpx.ConnectError("[Errno 61] Connection refused", request=r))
    p = run(live.propose(VALVE))
    done = run(live.confirm(p["id"]))
    assert done["status"] == "FAILED" and done["result"].startswith("Couldn't reach CONN_HOOK")


# ---------- 5. audit gaps ----------

def test_faults_and_sim_controls_are_audited(tokens):
    c, live = make_app()
    assert c.post("/api/live/faults", json={"asset_id": "PUMP_3", "type": "TRIP", "reason": "demo"},
                  headers=h(OP)).status_code == 200
    assert c.post("/api/live/faults", json={"asset_id": "NOPE", "type": "TRIP"}, headers=h(OP)).status_code == 422
    assert c.post("/api/live/sim", json={"speed": 2, "paused": False}, headers=h(PIN)).status_code == 200
    fault, bad, sim = live.audit.entries()
    assert (fault["event"], fault["source"], fault["role"], fault["asset_id"], fault["fault"], fault["ok"],
            fault["reason"]) == ("fault", "console", "operator", "PUMP_3", "TRIP", True, "demo")
    assert bad["event"] == "fault" and bad["ok"] is False and "Can't apply" in bad["result"]
    assert (sim["event"], sim["role"], sim["speed"], sim["paused"]) == ("sim", "operator", 2, False)
