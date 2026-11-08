"""Real MQTT and Modbus connectors (#62) against the local fake SCADA and PLC, in-process on free ports."""
import asyncio
import copy
import importlib
import json
import time
from pathlib import Path

import pytest

from backend.app.connectors.base import coerce
from backend.app.connectors.modbus import ModbusConnector, command_raw, parse_address
from backend.app.connectors.mqtt import MqttConnector, parse_payload
from backend.app.live import LiveField
from backend.app.scenario import load_scenario
from backend.tools.fake_common import FieldSim, find_connector, free_port
from backend.tools.fake_plc_modbus import FakePlc
from backend.tools.fake_scada_mqtt import FakeScadaMqtt
from backend.tools.mqtt_broker import topic_matches

SAMPLE = Path(__file__).resolve().parents[2] / "scenario" / "samples" / "sample1_pecos_mqtt_modbus.json"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


def blueprint(mqtt_port: int, modbus_port: int) -> dict:
    raw = json.loads(SAMPLE.read_text())
    for spec in raw["connectors"]:
        spec["config"]["port"] = mqtt_port if spec["kind"] == "mqtt" else modbus_port
        if spec["kind"] == "modbus":
            spec["config"]["poll_s"] = 0.05
        if spec["kind"] == "mqtt":
            spec["config"]["timeout_s"] = 2
    return raw


@pytest.fixture
def field():
    return FieldSim(SAMPLE)


@pytest.fixture
def scada(field):
    dev = FakeScadaMqtt(field, find_connector(field.raw, "mqtt"), port=free_port()).start_in_thread()
    yield dev
    dev.stop_thread()


@pytest.fixture
def plc(field):
    dev = FakePlc(field, find_connector(field.raw, "modbus"), port=free_port()).start_in_thread()
    yield dev
    dev.stop_thread()


@pytest.fixture
def live_factory():
    made = []

    def make(raw: dict) -> LiveField:
        live = LiveField(load_scenario(SAMPLE), raw)
        made.append(live)
        return live

    yield make
    for live in made:
        live.registry.close()


def wait(live: LiveField, cond, scada=None, timeout: float = 8.0, dt: float = 0.25):
    """Step the live field (and re-publish the fake SCADA) until cond() holds."""
    end = time.monotonic() + timeout
    changed = []
    while time.monotonic() < end:
        if scada is not None:
            scada.publish_now()
        time.sleep(0.05)
        changed += live.step(dt)
        if cond():
            return changed
    raise AssertionError(f"timed out; connectors: {live.state()['connectors']}")


def conn_status(live: LiveField) -> dict:
    return {c["id"]: c["status"] for c in live.state()["connectors"]}


# ---------- end to end over both protocols ----------

def test_readings_arrive_over_mqtt_and_modbus(field, scada, plc, live_factory):
    live = live_factory(blueprint(scada.port, plc.port))
    assert isinstance(live.registry.get("CONN_SCADA"), MqttConnector)
    assert isinstance(live.registry.get("CONN_PLC"), ModbusConnector)

    def arrived():
        return all(live.latest.get(k) for k in [("TANK_1", "level_pct"), ("PUMP_1", "running"),
                                                ("TANK_6", "level_pct"), ("VALVE_XO_56", "position"),
                                                ("WELL_20", "flow_bbl_hr")])
    wait(live, arrived, scada)
    assert conn_status(live) == {"CONN_SIM": "connected", "CONN_SCADA": "connected", "CONN_PLC": "connected"}
    assert live.latest[("TANK_1", "level_pct")]["source"] == "CONN_SCADA"
    assert live.latest[("TANK_6", "level_pct")]["source"] == "CONN_PLC"
    st = live.state()["assets"]
    assert st["TANK_1"]["level_pct"] == pytest.approx(field.sim.pct("TANK_1"), abs=1.0)
    assert st["TANK_6"]["level_pct"] == pytest.approx(field.sim.pct("TANK_6"), abs=0.6)  # scale 0.1 + noise
    assert st["PUMP_1"]["running"] is True and st["VALVE_XO_56"]["position"] == "CLOSED"
    assert st["WELL_20"]["flow_bbl_hr"] == pytest.approx(field.fmap.wells["WELL_20"]["water_bbl_hr"], abs=0.11)
    assert st["TANK_1"]["status"] == "OK" and st["TANK_6"]["status"] in ("OK", "WARNING")


def test_plc_side_trip_raises_pump_trip(field, scada, plc, live_factory):
    live = live_factory(blueprint(scada.port, plc.port))
    wait(live, lambda: live.value("PUMP_5", "running") is True, scada)
    field.inject({"asset_id": "PUMP_5", "type": "TRIP", "reason": "tripped on the PLC"})
    plc.publish_now()
    assert plc.register("di:0") is False and plc.register("ir:10") == 0
    changed = wait(live, lambda: ("PUMP_5", "PUMP_TRIP") in live.detector.active, scada)
    (alert,) = [a for a in changed if a["kind"] == "PUMP_TRIP" and a["active"]]
    assert alert["asset_id"] == "PUMP_5" and alert["severity"] == "ALARM"
    assert live.state()["assets"]["PUMP_5"]["status"] == "ALARM"


def test_confirmed_set_valve_becomes_a_register_write(field, scada, plc, live_factory):
    live = live_factory(blueprint(scada.port, plc.port))
    wait(live, lambda: conn_status(live)["CONN_PLC"] == "connected", scada)
    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_56", "value": "OPEN"}))
    assert p["status"] == "PENDING" and p["connector_id"] == "CONN_PLC"
    time.sleep(0.2)
    assert plc.writes == [], "a proposal must not touch the PLC"

    done = run(live.confirm(p["id"], by="test"))
    assert done["status"] == "CONFIRMED", done["result"]
    assert "hr:10" in done["result"]
    (w,) = plc.writes
    assert {k: w[k] for k in ("table", "address", "value", "asset_id", "command")} == \
        {"table": "hr", "address": 10, "value": 1, "asset_id": "VALVE_XO_56", "command": "SET_VALVE"}
    assert plc.register("hr:10") == 1 and field.sim.valve_open["VALVE_XO_56"] is True
    wait(live, lambda: live.value("VALVE_XO_56", "position") == "OPEN", scada)
    assert live.state()["assets"]["VALVE_XO_56"]["status"] == "OK"


def test_confirmed_set_pump_is_published_over_mqtt(field, scada, plc, live_factory):
    live = live_factory(blueprint(scada.port, plc.port))
    wait(live, lambda: conn_status(live)["CONN_SCADA"] == "connected", scada)
    p = run(live.propose({"type": "SET_PUMP", "asset_id": "PUMP_2", "value": "OFF"}))
    assert p["connector_id"] == "CONN_SCADA" and scada.commands == []
    done = run(live.confirm(p["id"]))
    assert done["status"] == "CONFIRMED", done["result"]
    deadline = time.monotonic() + 3
    while not scada.commands and time.monotonic() < deadline:
        time.sleep(0.02)
    (cmd,) = scada.commands
    assert cmd["topic"] == "pecos/pump2/cmd" and cmd["payload"] == "OFF"
    assert field.sim.pump_on["PUMP_2"] is False
    wait(live, lambda: live.value("PUMP_2", "running") is False, scada)
    assert live.state()["assets"]["PUMP_2"]["status"] == "OFFLINE"  # commanded off, so no trip alarm


def test_auto_confirm_never_reaches_real_equipment(monkeypatch, field, scada, plc, live_factory):
    """JARVIS_CONFIRM=0 only skips the confirm for the simulator."""
    monkeypatch.setenv("JARVIS_CONFIRM", "0")
    live = live_factory(blueprint(scada.port, plc.port))
    wait(live, lambda: set(conn_status(live).values()) == {"connected"}, scada)
    for action in ({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"},
                   {"type": "SET_PUMP", "asset_id": "PUMP_1", "value": "OFF"},
                   {"type": "CHOKE_WELL", "asset_id": "WELL_16", "value": 0}):
        p = run(live.propose(action, source="jarvis"))
        assert p["status"] == "PENDING", p
    truck = run(live.propose({"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_6"}, source="jarvis"))
    assert truck["status"] == "CONFIRMED" and truck["connector_id"] == "CONN_SIM"
    time.sleep(0.3)
    assert plc.writes == [] and scada.commands == []


def test_unreachable_endpoints_are_unavailable_and_unknown(live_factory):
    t0 = time.monotonic()
    live = live_factory(blueprint(free_port(), free_port()))
    assert time.monotonic() - t0 < 1.0, "an unreachable endpoint must not block startup"

    def both_failed():
        details = {c["id"]: c for c in live.state()["connectors"]}
        return all(details[i]["status"] == "unavailable" and details[i]["detail"] for i in ("CONN_SCADA", "CONN_PLC"))
    wait(live, both_failed)
    rows = {c["id"]: c for c in live.state()["connectors"]}
    assert "127.0.0.1" in rows["CONN_PLC"]["detail"]
    st = live.state()["assets"]
    for aid in ("TANK_1", "TANK_6", "PUMP_1", "PUMP_6", "VALVE_XO_56", "WELL_20"):
        assert st[aid]["status"] == "UNKNOWN", (aid, st[aid])
    # Assets nobody declared still come from the simulator.
    assert st["TRUCK_1"]["status"] == "OK"
    for action in ({"type": "SET_VALVE", "asset_id": "VALVE_XO_56", "value": "OPEN"},
                   {"type": "SET_PUMP", "asset_id": "PUMP_1", "value": "OFF"}):
        p = run(live.propose(action))
        done = run(live.confirm(p["id"]))
        assert done["status"] == "FAILED" and done["result"], done


def test_plc_restart_goes_unavailable_then_reconnects(field, scada, plc, live_factory):
    live = live_factory(blueprint(scada.port, plc.port))
    wait(live, lambda: conn_status(live)["CONN_PLC"] == "connected", scada)
    plc.stop_thread()
    wait(live, lambda: conn_status(live)["CONN_PLC"] == "unavailable", scada)
    # UNKNOWN as soon as the link is lost, not only once the last reading ages out.
    tank = live.state()["assets"]["TANK_6"]
    assert tank["status"] == "UNKNOWN" and tank["stale"] is True, tank
    assert tank["reason"].startswith("Lost the link to CONN_PLC (modbus)")
    assert live.live_scenario()["initial_levels_pct"]["TANK_6"] is None
    assert live.state()["assets"]["TANK_1"]["status"] == "OK"  # the MQTT side is unaffected
    again = FakePlc(field, find_connector(field.raw, "modbus"), port=plc.port).start_in_thread()
    try:
        wait(live, lambda: live.state()["assets"]["TANK_6"]["status"] in ("OK", "WARNING"), scada)
        assert conn_status(live)["CONN_PLC"] == "connected"
    finally:
        again.stop_thread()


def test_bad_quality_register_goes_unknown(field, scada, plc, live_factory):
    live = live_factory(blueprint(scada.port, plc.port))
    wait(live, lambda: live.state()["assets"]["TANK_7"]["status"] in ("OK", "WARNING"), scada)
    field.inject({"asset_id": "TANK_7", "type": "SENSOR_STALE"})
    plc.publish_now()
    assert plc.register("ir:2") == 65535
    wait(live, lambda: live.state()["assets"]["TANK_7"]["status"] == "UNKNOWN", scada, dt=1.0)
    assert live.state()["assets"]["TANK_7"]["stale"] is True


def test_missing_libraries_are_unavailable(monkeypatch, live_factory):
    real = importlib.import_module

    def missing(name, *a, **kw):
        if name.startswith(("paho", "pymodbus")):
            raise ImportError(f"No module named {name!r}")
        return real(name, *a, **kw)

    monkeypatch.setattr(importlib, "import_module", missing)
    live = live_factory(blueprint(1883, 5020))
    rows = {c["id"]: c for c in live.state()["connectors"]}
    assert rows["CONN_SCADA"]["status"] == rows["CONN_PLC"]["status"] == "unavailable"
    assert "paho-mqtt" in rows["CONN_SCADA"]["detail"] and "pymodbus" in rows["CONN_PLC"]["detail"]
    p = run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_56", "value": "OPEN"}))
    assert run(live.confirm(p["id"]))["status"] == "FAILED"


def test_missing_secret_env_is_unavailable(monkeypatch, live_factory):
    monkeypatch.delenv("PECOS_MQTT_PASSWORD", raising=False)
    raw = blueprint(free_port(), free_port())
    raw["connectors"][0]["config"].update(username="night", password_env="PECOS_MQTT_PASSWORD")
    live = live_factory(raw)
    (row,) = [c for c in live.state()["connectors"] if c["id"] == "CONN_SCADA"]
    assert row["status"] == "unavailable" and "PECOS_MQTT_PASSWORD" in row["detail"]


# ---------- the small pieces ----------

def test_parse_address_and_command_encoding():
    assert parse_address("hr:12") == ("hr", 12) and parse_address("12") == ("hr", 12)
    assert parse_address("IR:3") == ("ir", 3) and parse_address("coil:0") == ("coil", 0)
    for bad in ("", None, "xx:1", "hr:70000", "hr:abc"):
        with pytest.raises(ValueError):
            parse_address(bad)
    assert command_raw({"type": "SET_VALVE", "value": "OPEN"}) == 1
    assert command_raw({"type": "SET_PUMP", "value": "OFF"}) == 0
    assert command_raw({"type": "CHOKE_WELL", "value": 0.5}) == 50
    assert command_raw({"type": "SET_VALVE", "value": "maybe"}) is None


def test_coerce_and_payloads():
    assert coerce("level_pct", "72.5") == 72.5 and coerce("level_pct", "nan") is None
    assert coerce("level_pct", True) is None and coerce("flow_bbl_hr", "abc") is None
    assert coerce("running", "1") is True and coerce("running", "STOPPED") is False and coerce("running", 0) is False
    assert coerce("position", 1) == "OPEN" and coerce("position", "closed") == "CLOSED"
    assert coerce("position", "sideways") is None
    assert parse_payload(b"72.4") == 72.4 and parse_payload(b'{"value": "OPEN"}') == "OPEN"
    assert parse_payload(b" ON ") == "ON"
    assert topic_matches("pecos/#", "pecos/tank1/level") and topic_matches("pecos/+/level", "pecos/tank1/level")
    assert not topic_matches("pecos/+", "pecos/tank1/level") and not topic_matches("other/#", "pecos/x")


def test_sample_blueprint_splits_the_field():
    raw = json.loads(SAMPLE.read_text())
    kinds = {c["id"]: c["kind"] for c in raw["connectors"]}
    assert kinds == {"CONN_SCADA": "mqtt", "CONN_PLC": "modbus"}
    owned = {}
    for c in raw["connectors"]:
        for s in c["signals"]:
            assert (s["asset_id"], s["signal"]) not in owned, "a signal has one owner"
            owned[(s["asset_id"], s["signal"])] = c["id"]
        assert not ({"password", "token", "secret", "api_key", "key"} & set(c["config"]))
    assert all(owned[(f"TANK_{n}", "level_pct")] == "CONN_SCADA" for n in range(1, 5))
    assert all(owned[(f"PUMP_{n}", "running")] == "CONN_SCADA" for n in range(1, 5))
    assert all(owned[(f"TANK_{n}", "level_pct")] == "CONN_PLC" for n in range(5, 9))
    plain = json.loads((SAMPLE.parent / "sample1_pecos_8tank.json").read_text())
    for key in ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "faults", "initial"):
        assert raw[key] == plain[key], f"{key} must match sample 1"
    assert copy.deepcopy(raw)  # loads as a scenario too
    assert load_scenario(SAMPLE)["field"]["tanks"]
