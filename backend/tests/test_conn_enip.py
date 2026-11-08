"""The EtherNet/IP connector against the simulated Logix controller (fake: true).

The fake speaks no CIP; it serves the same read / write / get_tag_info calls as
pycomm3's LogixDriver on 127.0.0.1, on a free port, and every test stops it."""
import asyncio
import copy
import time

import pytest

from backend.app.connectors import KINDS
from backend.app.connectors.enip import EnipConnector, check_tag, tag_value
from backend.app.live import LiveField
from backend.app.scenario import load_scenario
from backend.fakes import enip as fake_enip

CID = "CONN_ENIP"


def wait_for(cond, timeout=8.0, every=0.03, step=None):
    """Poll cond() until it is truthy; `step` runs before each try (a live tick)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if step:
            step()
        value = cond()
        if value:
            return value
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout} s")


@pytest.fixture
def fake():
    f = fake_enip.start()
    yield f
    f.stop()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setitem(KINDS, "enip", EnipConnector)
    for var in ("JARVIS_CONFIRM", "JARVIS_MODE", "ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


def spec_for(port, **config):
    return {
        "id": CID, "kind": "enip",
        "config": {"fake": True, "host": "127.0.0.1", "port": port, "slot": 0, "poll_ms": 40,
                   "timeout_s": 1, "heartbeat_tag": "Heartbeat", "heartbeat_s": 1, **config},
        "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "Tank6_Level"},
                    {"asset_id": "TANK_5", "signal": "level_pct", "address": "Levels[2]"},
                    {"asset_id": "PUMP_6", "signal": "running", "address": "Program:Main.Pump6_Run"},
                    {"asset_id": "PUMP_6", "signal": "flow_bbl_hr", "address": "Pump6_Flow"},
                    {"asset_id": "VALVE_D_6", "signal": "position", "address": "Valve6_Open"}],
        "commands": [{"asset_id": "VALVE_D_6", "command": "SET_VALVE", "address": "Valve6_Cmd"},
                     {"asset_id": "PUMP_6", "command": "SET_PUMP", "address": "Pump6_Cmd"},
                     {"asset_id": "PUMP_5", "command": "SET_PUMP", "address": "Pump6_Speed"},
                     {"asset_id": "WELL_1", "command": "CHOKE_WELL", "address": "Well_Choke"},
                     {"asset_id": "PUMP_4", "command": "SET_PUMP", "address": "No_Such_Tag"}],
    }


def live_with(spec):
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    sc["connectors"] = [spec]
    return LiveField(sc)


def conn_of(lf):
    return lf.registry.get(CID)


def conn_status(lf):
    return next(c for c in lf.state()["connectors"] if c["id"] == CID)


def reading_of(lf, aid, sig):
    r = lf.latest.get((aid, sig))
    return r and r.get("source") == CID and r


@pytest.fixture
def live(fake):
    lf = live_with(spec_for(fake.port))
    yield lf
    lf.registry.close()


def step(lf):
    return lambda: lf.step(0.02)


# ---------- readings ----------

def test_readings_flow_end_to_end(fake, live):
    fake.set("Tank6_Level", 55.5)
    fake.set("Levels[2]", 33.0)
    wait_for(lambda: conn_status(live)["status"] == "connected", step=step(live))
    wait_for(lambda: (r := reading_of(live, "TANK_6", "level_pct")) and r["value"] == 55.5, step=step(live))
    wait_for(lambda: reading_of(live, "VALVE_D_6", "position") and reading_of(live, "PUMP_6", "running"),
             step=step(live))
    assert reading_of(live, "TANK_5", "level_pct")["value"] == 33.0
    a = live.state()["assets"]
    assert a["TANK_6"]["status"] != "UNKNOWN" and a["TANK_6"]["level_pct"] == 55.5
    assert a["VALVE_D_6"]["position"] == "OPEN" and a["PUMP_6"]["status"] == "OK"
    assert conn_of(live).quality[("TANK_6", "level_pct")]["quality"] == "good"
    assert "fake: no CIP" in conn_status(live)["detail"]


def test_scale_and_signal_map_apply(fake):
    spec = spec_for(fake.port)
    spec["signals"][0]["scale"] = 0.5
    spec["config"]["signal_map"] = {"PUMP_6.flow_bbl_hr": {"range": [0, 100]}}
    lf = live_with(spec)
    try:
        fake.set("Tank6_Level", 80.0)
        wait_for(lambda: (r := reading_of(lf, "TANK_6", "level_pct")) and r["value"] == 40.0, step=step(lf))
        # Pump6_Flow is 118, outside 0..100: no reading, quality says so.
        conn = conn_of(lf)
        wait_for(lambda: conn.quality.get(("PUMP_6", "flow_bbl_hr"), {}).get("quality") == "out_of_range")
        assert not reading_of(lf, "PUMP_6", "flow_bbl_hr")
    finally:
        lf.registry.close()


def test_tag_error_gives_no_reading_and_unknown(fake, live):
    wait_for(lambda: reading_of(live, "TANK_6", "level_pct"), step=step(live))
    conn = conn_of(live)
    fake.set_error("Tank6_Level", "Path destination unknown")
    wait_for(lambda: conn.quality[("TANK_6", "level_pct")]["quality"] == "bad", step=step(live))
    assert "Path destination unknown" in conn.quality[("TANK_6", "level_pct")]["detail"]
    last_t = live.latest[("TANK_6", "level_pct")]["t"]
    for _ in range(8):           # past stale_after_min: never the old value carried forward
        live.step(1.0)
        time.sleep(0.01)
    assert live.latest[("TANK_6", "level_pct")]["t"] == last_t
    tank = live.state()["assets"]["TANK_6"]
    assert tank["status"] == "UNKNOWN" and tank["stale"] is True
    assert live.latest[("VALVE_D_6", "position")]["t"] > last_t      # the others keep flowing
    fake.set_error("Tank6_Level", None)
    wait_for(lambda: live.state()["assets"]["TANK_6"]["status"] != "UNKNOWN", step=step(live))


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), "garbage", [1.0, 2.0]])
def test_nonsense_values_give_no_reading(fake, live, raw):
    conn = conn_of(live)
    wait_for(lambda: conn.quality.get(("TANK_6", "level_pct"), {}).get("quality") == "good")
    fake.set("Tank6_Level", raw)
    wait_for(lambda: conn.quality[("TANK_6", "level_pct")]["quality"] != "good")
    conn.read()
    time.sleep(0.1)
    assert all(r["asset_id"] != "TANK_6" for r in conn.read())


def test_frozen_heartbeat_withholds_everything(fake, live):
    conn = conn_of(live)
    wait_for(lambda: reading_of(live, "TANK_6", "level_pct"), step=step(live))
    fake.freeze_heartbeat(True)
    try:
        wait_for(lambda: conn_status(live)["status"] == "unavailable", timeout=5)
        assert "heartbeat" in conn_status(live)["detail"]
        conn.read()
        time.sleep(0.15)
        assert conn.read() == []
        assert all(q["quality"] == "comm_lost" for q in conn.quality.values())
        res = conn.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
        assert res["ok"] is False and "not sent" in res["detail"]
        assert fake.writes == []
    finally:
        fake.freeze_heartbeat(False)
    wait_for(lambda: conn_status(live)["status"] == "connected", timeout=5)


# ---------- writes only through a person ----------

def test_propose_sends_nothing_confirm_sends_once(fake, live):
    wait_for(lambda: conn_status(live)["status"] == "connected", step=step(live))

    async def go():
        p = await live.propose({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"},
                               reason="test", source="console")
        assert p["status"] == "PENDING"
        await asyncio.sleep(0.2)
        assert fake.writes == []
        done = await live.confirm(p["id"], by="operator")
        return done

    done = asyncio.run(go())
    assert done["status"] == "CONFIRMED", done
    assert fake.writes == [("Valve6_Cmd", False)]
    wait_for(lambda: live.state()["assets"]["VALVE_D_6"]["position"] == "CLOSED", step=step(live))
    time.sleep(0.2)
    assert fake.writes == [("Valve6_Cmd", False)]


def test_writes_take_the_tag_type(fake, live):
    wait_for(lambda: conn_status(live)["status"] == "connected", step=step(live))
    conn = conn_of(live)
    assert conn.send({"type": "SET_PUMP", "asset_id": "PUMP_6", "value": "OFF"})["ok"]
    assert conn.send({"type": "SET_PUMP", "asset_id": "PUMP_5", "value": "ON"})["ok"]
    assert conn.send({"type": "CHOKE_WELL", "asset_id": "WELL_1", "value": 0.25})["ok"]
    assert fake.writes == [("Pump6_Cmd", False), ("Pump6_Speed", 1), ("Well_Choke", 0.25)]
    assert fake.controller.tags["Program:Main.Pump6_Run"]["value"] is False
    res = conn.send({"type": "SET_PUMP", "asset_id": "PUMP_4", "value": "ON"})   # unknown tag
    assert res["ok"] is False and "not sent" in res["detail"]
    res = conn.send({"type": "SET_VALVE", "asset_id": "VALVE_D_1", "value": "OPEN"})  # not declared
    assert res["ok"] is False
    assert len(fake.writes) == 3


def test_tag_value_rules():
    assert tag_value("A", "BOOL", "SET_VALVE", True) is True
    assert tag_value("A", "DINT", "SET_PUMP", False) == 0
    assert tag_value("A", "REAL", "SET_PUMP", True) == 1.0
    assert tag_value("A.3", "DINT", "SET_PUMP", True) is True     # a bit of an integer
    with pytest.raises(ValueError):
        tag_value("A", "DINT", "CHOKE_WELL", 0.5)
    with pytest.raises(ValueError):
        tag_value("A", {"name": "UDT"}, "SET_PUMP", True)


def test_write_without_ack_is_uncertain_and_never_resent(fake, live):
    wait_for(lambda: conn_status(live)["status"] == "connected", step=step(live))
    conn = conn_of(live)
    fake.write_delay_s = 1.5
    t0 = time.monotonic()
    res = conn.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
    assert time.monotonic() - t0 < 2.5
    assert res == {"ok": False, "uncertain": True,
                   "detail": "sent; delivery not confirmed - check the field before retrying"}
    fake.write_delay_s = 0
    wait_for(lambda: len(fake.writes) == 1, timeout=3)
    wait_for(lambda: conn_status(live)["status"] == "connected", timeout=5)   # reconnected
    time.sleep(0.3)
    assert fake.writes == [("Valve6_Cmd", False)]


# ---------- config ----------

def test_bad_env_name_makes_connector_unavailable(fake):
    lf = live_with(spec_for(fake.port, password_env="PLC_PASSWORD"))
    try:
        st = conn_status(lf)
        assert st["status"] == "unavailable" and "FREEBOARD_CONN_" in st["detail"]
        assert "PLC_PASSWORD" not in st["detail"]
    finally:
        lf.registry.close()


@pytest.mark.parametrize("tag", ["Tank6_Level", "Program:Main.Pump6_Run", "Levels[2]", "M[1,2].Sts.3",
                                 "_x"])
def test_good_tags(tag):
    assert check_tag(tag, "t") == tag


@pytest.mark.parametrize("tag", ["", "6Tank", "Tank 6", "Levels[]", "Levels{3}", "Program:.X", "a..b",
                                 "x;drop", None, 5, "A" * 41])
def test_bad_tags_rejected(fake, tag):
    with pytest.raises(ValueError):
        check_tag(tag, "t")
    spec = spec_for(fake.port)
    spec["signals"][0]["address"] = tag
    lf = live_with(spec)
    try:
        st = conn_status(lf)
        assert st["status"] == "unavailable" and "bad config" in st["detail"]
    finally:
        lf.registry.close()


def test_fake_mode_needs_loopback(fake):
    lf = live_with(spec_for(fake.port, host="10.0.0.20"))
    try:
        assert conn_status(lf)["status"] == "unavailable"
    finally:
        lf.registry.close()


# ---------- the link ----------

def test_stop_and_restart_reconnects(fake, live):
    conn = conn_of(live)
    wait_for(lambda: reading_of(live, "TANK_6", "level_pct"), step=step(live))
    fake.stop()
    wait_for(lambda: conn_status(live)["status"] == "unavailable", timeout=5)
    assert all(q["quality"] == "comm_lost" for q in conn.quality.values())
    conn.read()
    time.sleep(0.2)
    assert conn.read() == []
    res = conn.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
    assert res["ok"] is False and "not sent" in res["detail"]
    fake.restart()
    wait_for(lambda: conn_status(live)["status"] == "connected", timeout=8)
    fake.set("Tank6_Level", 61.0)
    wait_for(lambda: (r := reading_of(live, "TANK_6", "level_pct")) and r["value"] == 61.0, step=step(live))
    assert fake.writes == []


def test_close_returns_quickly(fake):
    lf = live_with(spec_for(fake.port))
    conn = conn_of(lf)
    wait_for(lambda: conn.status()["available"])
    t0 = time.monotonic()
    conn.close()
    assert time.monotonic() - t0 < 0.2
    wait_for(lambda: not conn._thread.is_alive(), timeout=3)


def test_close_returns_quickly_with_no_controller():
    conn = EnipConnector(spec_for(1), None, lambda: 0.0)   # nothing listens on port 1
    wait_for(lambda: "can't reach" in conn.status()["detail"])
    t0 = time.monotonic()
    conn.close()
    assert time.monotonic() - t0 < 0.2


def test_real_driver_path_reports_unreachable_controller():
    """fake: false builds pycomm3's LogixDriver; with nothing listening it says so and retries."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    spec = spec_for(port, fake=False)
    conn = EnipConnector(spec, None, lambda: 0.0)
    try:
        assert "Logix controller at 127.0.0.1:%d/0" % port in conn.status()["detail"]
        wait_for(lambda: "can't reach" in conn.status()["detail"], timeout=10)
        assert conn.status()["available"] is False
        assert conn.read() == []
        assert all(q["quality"] == "comm_lost" for q in conn.quality.values())
    finally:
        conn.close()
