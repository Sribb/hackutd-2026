"""Review fixes for the real connectors (#62): a command goes out at most once and is never
reported FAILED when it may have reached the device; an asset on real equipment is never
"handled" by the simulator; bad configs fail at load; shutdown never blocks the loop."""
import asyncio
import json
import socket
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.connectors import NoCommandPath
from backend.app.connectors.base import Connector, check_env_names
from backend.app.connectors.mqtt import MqttConnector
from backend.app.jarvis import Jarvis
from backend.app.live import ActionError
from backend.app.live_api import build_router
from backend.tests.test_connectors_real import (SAMPLE, blueprint, conn_status, field, live_factory,  # noqa: F401
                                                no_llm, plc, run, scada, wait)
from backend.tools.fake_common import LoopThread, free_port

UNCERTAIN = "sent; delivery not confirmed — check the field before retrying"


class BlackHolePlc:
    """A TCP server that takes Modbus requests and never answers: a PLC that hangs after
    the request left. It keeps every byte, so a test can count what was sent."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.received = b""
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._read, args=(c,), daemon=True).start()

    def _read(self, c: socket.socket) -> None:
        while True:
            try:
                data = c.recv(4096)
            except OSError:
                return
            if not data:
                return
            self.received += data

    def frames(self, function_code: int) -> int:
        """Modbus TCP frames with this function code (7-byte MBAP header, then the code)."""
        buf, n = self.received, 0
        while len(buf) >= 8:
            if buf[7] == function_code:
                n += 1
            buf = buf[6 + int.from_bytes(buf[4:6], "big"):]
        return n

    def close(self) -> None:
        for c in self.conns:
            c.close()
        self.sock.close()


@pytest.fixture
def hole():
    h = BlackHolePlc()
    yield h
    h.close()


def with_connectors(*specs) -> dict:
    raw = json.loads(SAMPLE.read_text())
    raw["connectors"] = list(specs)
    return raw


def wait_until(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def api(live) -> TestClient:
    app = FastAPI()
    app.include_router(build_router(live, Jarvis(live, forecast_fn=lambda sc, a: {}, plans_fn=lambda sc: {})))
    return TestClient(app)


def confirm_is_409(live, action_id: str) -> None:
    with pytest.raises(ActionError) as e:
        run(live.confirm(action_id))
    assert e.value.status == 409


# ---------- UNCERTAIN, never FAILED, never re-sent ----------

def test_mqtt_puback_timeout_is_uncertain_and_never_resent(field, scada, plc, live_factory):
    raw = blueprint(scada.port, plc.port)
    raw["connectors"][0]["config"]["timeout_s"] = 0.4
    live = live_factory(raw)
    conn = live.registry.get("CONN_SCADA")
    wait(live, lambda: conn_status(live)["CONN_SCADA"] == "connected", scada)
    scada.broker.drop_pubacks = True  # the device gets it; the acknowledgement never comes back

    p = run(live.propose({"type": "SET_PUMP", "asset_id": "PUMP_2", "value": "OFF"}))
    done = run(live.confirm(p["id"], by="night operator"))
    assert done["status"] == "UNCERTAIN", done
    assert UNCERTAIN in done["result"] and "pecos/pump2/cmd" in done["result"]
    assert "PUMP_2" not in live.commanded, "an unacknowledged command is not recorded as done"
    wait_until(lambda: len(scada.commands) == 1)

    # Never sent again: not by a second confirm, and not by paho after the link drops
    # (paho re-sends unacknowledged QoS 1 messages on reconnect unless we take it back).
    confirm_is_409(live, p["id"])
    scada.broker.drop_pubacks = False
    scada._bg.call(scada.broker.drop_clients)
    wait_until(lambda: not conn.connected)
    wait_until(lambda: conn.connected and scada.broker.clients >= 1, timeout=10)
    time.sleep(0.5)
    assert len(scada.commands) == 1, f"the command was re-sent after the reconnect: {scada.commands}"


def test_mqtt_publish_while_disconnected_is_failed_and_dropped(field, scada, plc, live_factory):
    """paho queues a QoS 1 publish made with no connection and sends it on reconnect.
    That would be a command going out later, by itself: take it back and say FAILED."""
    live = live_factory(blueprint(scada.port, plc.port))
    conn = live.registry.get("CONN_SCADA")
    wait(live, lambda: conn_status(live)["CONN_SCADA"] == "connected", scada)
    real_publish = conn.client.publish

    def publish_with_no_link(*a, **kw):
        conn.client._sock = None  # what paho sees between a drop and on_disconnect
        try:
            return real_publish(*a, **kw)
        finally:
            scada._bg.call(scada.broker.drop_clients)
    conn.client.publish = publish_with_no_link
    p = run(live.propose({"type": "SET_PUMP", "asset_id": "PUMP_3", "value": "OFF"}))
    done = run(live.confirm(p["id"]))
    assert done["status"] == "FAILED" and "Nothing was sent" in done["result"], done
    conn.client.publish = real_publish
    wait_until(lambda: conn.connected and scada.broker.clients >= 1, timeout=10)
    time.sleep(0.5)
    assert scada.commands == [], "a refused publish must not go out after the reconnect"


def test_modbus_write_with_no_reply_is_uncertain_and_sent_once(hole, live_factory):
    live = live_factory(with_connectors({
        "id": "CONN_PLC", "kind": "modbus",
        "config": {"host": "127.0.0.1", "port": hole.port, "timeout_s": 0.3, "poll_s": 30},
        "commands": [{"asset_id": "VALVE_XO_56", "command": "SET_VALVE", "address": "hr:10"}]}))
    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_56", "value": "OPEN"}))
    assert p["connector_id"] == "CONN_PLC" and p["status"] == "PENDING"
    done = run(live.confirm(p["id"]))
    assert done["status"] == "UNCERTAIN", done
    assert UNCERTAIN in done["result"] and "hr:10" in done["result"]
    time.sleep(0.5)
    assert hole.frames(0x06) == 1, "a write that timed out must never be retried"
    confirm_is_409(live, p["id"])


def test_unreachable_plc_is_failed_not_uncertain(live_factory):
    live = live_factory(with_connectors({
        "id": "CONN_PLC", "kind": "modbus", "config": {"host": "127.0.0.1", "port": free_port(), "timeout_s": 0.3},
        "commands": [{"asset_id": "VALVE_XO_56", "command": "SET_VALVE", "address": "hr:10"}]}))
    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_56", "value": "OPEN"}))
    done = run(live.confirm(p["id"]))
    assert done["status"] == "FAILED" and "Nothing was sent" in done["result"], done


# ---------- routing: real assets never fall back to the simulator ----------

def test_undeclared_command_on_a_real_asset_is_refused_not_simulated(monkeypatch, live_factory):
    monkeypatch.setenv("JARVIS_CONFIRM", "0")
    raw = blueprint(free_port(), free_port())
    plc_spec = raw["connectors"][1]
    plc_spec["commands"] = [c for c in plc_spec["commands"] if c["asset_id"] != "PUMP_6"]
    live = live_factory(raw)
    sim = live.registry.sim
    assert isinstance(live.registry.route({"type": "SET_PUMP", "asset_id": "PUMP_6"}), NoCommandPath)
    assert live.registry.route({"type": "SEND_TRUCK", "asset_id": "TRUCK_1"}) is sim
    # For a person, and for Jarvis with JARVIS_CONFIRM=0: FAILED at once, nothing sent anywhere.
    for source in ("console", "jarvis"):
        p = run(live.propose({"type": "SET_PUMP", "asset_id": "PUMP_6", "value": "OFF"}, source=source))
        assert p["status"] == "FAILED", p
        assert p["connector_id"] == "CONN_PLC"
        assert p["result"].startswith("no command path to PUMP_6 on CONN_PLC"), p["result"]
        assert sim.pump_on["PUMP_6"] is True, "the simulator must not pretend to turn it off"
        confirm_is_409(live, p["id"])


def test_assets_no_real_connector_touches_still_go_to_the_simulator(live_factory):
    live = live_factory(with_connectors({"id": "CONN_PLC", "kind": "modbus",
                                         "config": {"host": "127.0.0.1", "port": free_port()},
                                         "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "ir:1"}]}))
    reg = live.registry
    assert reg.route({"type": "SET_PUMP", "asset_id": "PUMP_6"}) is reg.sim
    assert reg.route({"type": "SET_VALVE", "asset_id": "VALVE_XO_56"}) is reg.sim
    assert reg.feeder("TANK_6").id == "CONN_PLC" and reg.feeder("PUMP_6") is None


def test_a_connector_that_failed_to_load_still_owns_its_assets(live_factory):
    raw = blueprint(free_port(), free_port())
    raw["connectors"][0]["config"]["password_env"] = "NOT_ALLOWED"
    live = live_factory(raw)
    assert not isinstance(live.registry.get("CONN_SCADA"), MqttConnector)
    p = run(live.propose({"type": "SET_PUMP", "asset_id": "PUMP_2", "value": "OFF"}))
    assert p["connector_id"] == "CONN_SCADA"
    assert run(live.confirm(p["id"]))["status"] == "FAILED"
    assert live.registry.sim.pump_on["PUMP_2"] is True


# ---------- fault injection ----------

def test_fault_injection_on_a_real_asset_is_409(live_factory):
    live = live_factory(blueprint(free_port(), free_port()))
    client = api(live)
    r = client.post("/api/live/faults", json={"asset_id": "PUMP_2", "type": "TRIP"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "fault injection only works on simulated assets" in detail and "CONN_SCADA" in detail
    assert "trip it at the device (e.g. type 'trip PUMP_2' in backend.tools.fake_field)" in detail
    assert "PUMP_2" not in live.registry.sim.tripped
    r = client.post("/api/live/faults", json={"asset_id": "tank_7", "type": "SENSOR_STALE"})
    assert r.status_code == 409 and "'stale TANK_7'" in r.json()["detail"]
    assert "TANK_7" not in live.registry.sim.dead_sensors


def test_fault_injection_on_a_simulated_asset_still_works(live_factory):
    live = live_factory(with_connectors({"id": "CONN_PLC", "kind": "modbus",
                                         "config": {"host": "127.0.0.1", "port": free_port()},
                                         "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "ir:1"}]}))
    r = api(live).post("/api/live/faults", json={"asset_id": "PUMP_2", "type": "TRIP"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "PUMP_2" in live.registry.sim.tripped


# ---------- MQTT config and freshness ----------

def test_retained_mqtt_value_is_not_fresh_until_a_live_message(field, scada, plc, live_factory):
    scada._bg.call(scada.broker.publish, "pecos/tank1/level", "55.0", True)
    live = live_factory(blueprint(scada.port, plc.port))
    conn = live.registry.get("CONN_SCADA")
    wait_until(lambda: conn.retained >= 1)
    live.step(0.25)
    assert ("TANK_1", "level_pct") not in live.latest
    assert live.state()["assets"]["TANK_1"]["status"] == "UNKNOWN"
    assert "retained" in conn.status()["detail"]
    scada._bg.call(scada.broker.publish, "pecos/tank1/level", "61.5", False)
    wait(live, lambda: live.value("TANK_1", "level_pct") == 61.5)
    assert live.latest[("TANK_1", "level_pct")]["source"] == "CONN_SCADA"


@pytest.mark.parametrize("where,address", [("signals", "tank/+/level"), ("signals", "#"),
                                           ("commands", "pump1/#")])
def test_mqtt_wildcard_address_is_rejected_at_load(where, address, live_factory):
    raw = blueprint(free_port(), free_port())
    raw["connectors"][0][where][0]["address"] = address
    live = live_factory(raw)
    assert not isinstance(live.registry.get("CONN_SCADA"), MqttConnector)
    (row,) = [c for c in live.state()["connectors"] if c["id"] == "CONN_SCADA"]
    assert row["status"] == "unavailable", row
    assert "wildcard" in row["detail"] and "pecos/" + address in row["detail"], row["detail"]
    assert live.state()["assets"]["TANK_1"]["status"] == "UNKNOWN"


def test_wildcard_topic_prefix_is_rejected_too(live_factory):
    raw = blueprint(free_port(), free_port())
    raw["connectors"][0]["config"]["topic_prefix"] = "+/"
    live = live_factory(raw)
    (row,) = [c for c in live.state()["connectors"] if c["id"] == "CONN_SCADA"]
    assert row["status"] == "unavailable" and "wildcard" in row["detail"]


def test_env_names_must_start_with_freeboard_conn(monkeypatch, live_factory):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-do-not-send")
    raw = blueprint(free_port(), free_port())
    raw["connectors"][0]["config"].update(username="night", password_env="ANTHROPIC_API_KEY")
    live = live_factory(raw)
    (row,) = [c for c in live.state()["connectors"] if c["id"] == "CONN_SCADA"]
    assert row["status"] == "unavailable" and "FREEBOARD_CONN_" in row["detail"], row
    assert "sk-do-not-send" not in json.dumps(live.state())
    # Webhooks follow the same rule.
    hook = live_factory(with_connectors({"id": "CONN_HOOK", "kind": "webhook",
                                         "config": {"url": "http://x", "token_env": "HOME"}}))
    (row,) = [c for c in hook.state()["connectors"] if c["id"] == "CONN_HOOK"]
    assert row["status"] == "unavailable" and "FREEBOARD_CONN_" in row["detail"]
    check_env_names({"password_env": "FREEBOARD_CONN_MQTT_PASSWORD", "port": 1883, "token_env": None})
    for bad in ("ANTHROPIC_API_KEY", "FREEBOARD_CONN_", "freeboard_conn_x", 5):
        with pytest.raises(ValueError):
            check_env_names({"password_env": bad})
    # secret() never reads anything else, even if the load check were bypassed.
    c = Connector.__new__(Connector)
    c.config = {"password_env": "ANTHROPIC_API_KEY"}
    assert c.secret("password") is None
    monkeypatch.setenv("FREEBOARD_CONN_OK", "yes")
    c.config = {"password_env": "FREEBOARD_CONN_OK"}
    assert c.secret("password") == "yes"


# ---------- shutdown ----------

def test_close_never_blocks_the_event_loop(hole, live_factory):
    live = live_factory(with_connectors(
        {"id": "CONN_PLC", "kind": "modbus", "config": {"host": "127.0.0.1", "port": hole.port, "timeout_s": 1.5},
         "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "ir:1"}]},
        {"id": "CONN_SCADA", "kind": "mqtt", "config": {"host": "127.0.0.1", "port": free_port()},
         "signals": [{"asset_id": "TANK_1", "signal": "level_pct", "address": "tank1/level"}]}))
    wait_until(lambda: hole.frames(0x04) >= 1)  # the poller is now waiting 1.5 s for a reply

    async def main():
        t0 = time.monotonic()
        pending = live.registry.close()
        blocked = time.monotonic() - t0
        ticks = 0

        async def count():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)
        counter = asyncio.create_task(count())
        for t in pending:
            await asyncio.to_thread(t.join, 5)
        counter.cancel()
        return blocked, pending, ticks
    blocked, pending, ticks = run(main())
    assert blocked < 0.2, f"close() blocked the event loop for {blocked:.2f} s"
    assert len(pending) == 2 and not any(t.is_alive() for t in pending)
    assert ticks > 5, "the loop kept running while the connectors closed"


def test_live_stop_finishes_cleanup(live_factory):
    live = live_factory(blueprint(free_port(), free_port()))
    plc_conn = live.registry.get("CONN_PLC")
    run(live.stop())
    assert plc_conn._stop.is_set() and not plc_conn._thread.is_alive()
    assert live.registry.get("CONN_SCADA").client is None


def test_loop_thread_stop_closes_the_loop():
    lt = LoopThread()
    lt.call(lambda: lt.loop.create_task(asyncio.sleep(60)))  # a task still pending at stop
    assert lt.run(asyncio.sleep(0, result=7)) == 7
    lt.stop()
    assert lt.loop.is_closed() and not lt.thread.is_alive()
    lt.stop()  # twice is fine
