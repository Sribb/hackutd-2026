"""Sparkplug B connector against the fake broker and the fake edge node, through LiveField.

Everything binds 127.0.0.1 on free ports. Each test stops what it started."""
import asyncio
import copy
import time

import pytest

from backend.app.connectors import KINDS
from backend.app.connectors import sparkplug_codec as codec
from backend.app.connectors.sparkplug import SparkplugConnector, parse_address
from backend.app.live import LiveField
from backend.app.scenario import load_scenario
from backend.fakes import mqtt_broker
from backend.fakes import sparkplug as fake_sparkplug

GROUP, NODE, DEVICE = "FREEBOARD", "EDGE_1", "PLC_1"
DEVICES = {DEVICE: {
    "TANK_3/Level": ("Float", 62.0),
    "TANK_4/Level_mA": ("Float", 12.0),       # 4-20 mA, mapped to 0-100 % by signal_map
    "PUMP_3/Running": ("Boolean", True),
    "PUMP_3/Flow": ("Float", 118.0),
    "VALVE_D_6/Open": ("Boolean", True),
}}
NODE_METRICS = {"TANK_5/Level": ("Double", 41.0)}
DEVICE_SIGNALS = [("TANK_3", "level_pct"), ("TANK_4", "level_pct"), ("PUMP_3", "running"),
                  ("PUMP_3", "flow_bbl_hr"), ("VALVE_D_6", "position")]


def addr(metric: str, device: str = DEVICE) -> str:
    return f"{GROUP}/{NODE}/{device}/{metric}"


def make_spec(port: int, **config) -> dict:
    return {
        "id": "CONN_SPB", "kind": "sparkplug",
        "config": {"host": "127.0.0.1", "port": port, "group_id": GROUP, "host_id": "freeboard_test",
                   "timeout_s": 2, "rebirth_min_interval_s": 0.5,
                   "signal_map": {"TANK_4.level_pct": {"ma420": True, "eng": [0, 100]}}, **config},
        "signals": [
            {"asset_id": "TANK_3", "signal": "level_pct", "address": addr("TANK_3/Level")},
            {"asset_id": "TANK_4", "signal": "level_pct", "address": addr("TANK_4/Level_mA")},
            {"asset_id": "TANK_5", "signal": "level_pct", "address": f"{GROUP}/{NODE}//TANK_5/Level"},
            {"asset_id": "PUMP_3", "signal": "running", "address": addr("PUMP_3/Running")},
            {"asset_id": "PUMP_3", "signal": "flow_bbl_hr", "address": addr("PUMP_3/Flow")},
            {"asset_id": "VALVE_D_6", "signal": "position", "address": addr("VALVE_D_6/Open")},
        ],
        "commands": [
            {"asset_id": "VALVE_D_6", "command": "SET_VALVE", "address": addr("VALVE_D_6/Open")},
            {"asset_id": "PUMP_3", "command": "SET_PUMP", "address": addr("PUMP_3/Running")},
        ],
    }


def run(coro):
    return asyncio.run(coro)


def wait_for(pred, timeout: float = 8.0, step: float = 0.05, what: str = "condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setitem(KINDS, "sparkplug", SparkplugConnector)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def broker():
    b = mqtt_broker.start()
    yield b
    b.stop()


@pytest.fixture
def edge(broker):
    e = fake_sparkplug.start(broker.port, GROUP, NODE, devices=copy.deepcopy(DEVICES),
                             node_metrics=dict(NODE_METRICS), period_s=0.2)
    yield e
    e.stop()


@pytest.fixture
def make_live():
    made = []

    def build(spec: dict) -> LiveField:
        sc = copy.deepcopy(load_scenario())
        sc["failures"] = []
        sc["connectors"] = [spec]
        lf = LiveField(sc)
        made.append(lf)
        return lf
    yield build
    for lf in made:
        lf.registry.close()


def conn_of(lf: LiveField) -> SparkplugConnector:
    return lf.registry.get("CONN_SPB")


def wait_value(lf: LiveField, aid: str, sig: str, want, timeout: float = 8.0):
    def check():
        lf.step(0.01)
        return lf.value(aid, sig) == want
    wait_for(check, timeout, what=f"{aid}.{sig} == {want!r} (got {lf.value(aid, sig)!r})")


def status_of(lf: LiveField) -> dict:
    return next(c for c in lf.state()["connectors"] if c["id"] == "CONN_SPB")


def host_publishes(broker, kind: str) -> list[dict]:
    return [m for m in broker.log() if f"/{kind}/" in m["topic"] and str(m["client_id"] or "").startswith("freeboard")]


# ---------- readings ----------

def test_readings_flow_end_to_end(broker, edge, make_live):
    lf = make_live(make_spec(broker.port))
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    wait_value(lf, "TANK_4", "level_pct", 50.0)            # 12 mA on a 0-100 % 4-20 mA loop
    wait_value(lf, "TANK_5", "level_pct", 41.0)            # a node metric
    wait_value(lf, "PUMP_3", "running", True)
    wait_value(lf, "PUMP_3", "flow_bbl_hr", 118.0)
    wait_value(lf, "VALVE_D_6", "position", "OPEN")
    st = lf.state()
    assert st["assets"]["TANK_3"]["status"] != "UNKNOWN"
    assert st["assets"]["VALVE_D_6"]["status"] == "OK"
    assert status_of(lf)["status"] == "connected"
    c = conn_of(lf)
    assert all(q["quality"] == "good" for q in c.quality.values())

    # The node was born before the host subscribed: its data is ignored and a rebirth asked for.
    assert c.ignored.get("DDATA before a birth", 0) + c.ignored.get("NDATA before a birth", 0) >= 1
    assert edge.rebirth_requests >= 1

    # DDATA goes by alias only; changes come through.
    edge.set(DEVICE, "TANK_3/Level", 70.5)
    wait_value(lf, "TANK_3", "level_pct", 70.5)

    # Sparkplug 3.0 STATE: online, retained.
    state_msg = broker.retained()["spBv1.0/STATE/freeboard_test"]
    assert b'"online": true' in state_msg

    # Reading and starting up write nothing to the equipment.
    assert edge.writes == []
    assert host_publishes(broker, "DCMD") == []
    for m in host_publishes(broker, "NCMD"):
        metrics = codec.decode_payload(m["payload"])["metrics"]
        assert [(x["name"], x["value"]) for x in metrics] == [("Node Control/Rebirth", True)]


def test_bad_quality_gives_no_reading_and_unknown(broker, edge, make_live):
    lf = make_live(make_spec(broker.port))
    c = conn_of(lf)
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    wait_value(lf, "TANK_4", "level_pct", 50.0)

    edge.null(DEVICE, "TANK_3/Level")
    wait_for(lambda: c.quality[("TANK_3", "level_pct")]["quality"] == "null", what="null quality")
    edge.set(DEVICE, "TANK_4/Level_mA", 2.0)                # below 3.6 mA: NAMUR NE43 fault
    wait_for(lambda: c.quality[("TANK_4", "level_pct")]["quality"] == "fault", what="4-20 mA fault")
    edge.set(DEVICE, "PUMP_3/Flow", float("nan"))
    wait_for(lambda: c.quality[("PUMP_3", "flow_bbl_hr")]["quality"] not in ("good", "comm_lost"),
             what="NaN flow is not good")

    # From here on, none of the three gets a reading: never the last good value again.
    lf.step(0.01)
    marks = {k: lf.latest[k]["t"] for k in [("TANK_3", "level_pct"), ("TANK_4", "level_pct"),
                                            ("PUMP_3", "flow_bbl_hr")]}
    time.sleep(0.6)
    lf.step(6.0)                                            # past sensors.stale_after_min
    for k, t in marks.items():
        assert lf.latest[k]["t"] == t, k
    assets = lf.state()["assets"]
    assert assets["TANK_3"]["status"] == "UNKNOWN"
    assert assets["TANK_4"]["status"] == "UNKNOWN"
    assert assets["VALVE_D_6"]["status"] == "OK"            # still reporting, still fresh

    edge.set(DEVICE, "TANK_3/Level", 63.0)                  # a good value counts again
    wait_value(lf, "TANK_3", "level_pct", 63.0)
    assert c.quality[("TANK_3", "level_pct")]["quality"] == "good"


def test_retained_and_historical_messages_are_not_live(broker, edge, make_live):
    edge.publish_retained_stale(DEVICE, "TANK_3/Level", 99.0)
    lf = make_live(make_spec(broker.port))
    c = conn_of(lf)
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    assert c.ignored.get("retained DDATA (not fresh)", 0) >= 1

    edge.publish_historical(DEVICE, "TANK_3/Level", 55.0)
    wait_for(lambda: c.ignored.get("historical metric", 0) >= 1, what="historical metric ignored")
    edge.set(DEVICE, "TANK_3/Level", 64.0)
    wait_value(lf, "TANK_3", "level_pct", 64.0)
    seen = {v for _t, v in lf.history[("TANK_3", "level_pct")]}
    assert 99.0 not in seen and 55.0 not in seen
    edge.clear_retained(DEVICE)


def test_seq_gap_requests_rebirth(broker, edge, make_live):
    lf = make_live(make_spec(broker.port))
    c = conn_of(lf)
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    time.sleep(0.6)                                         # past the test's rebirth rate limit
    before = edge.rebirth_requests
    edge.skip_seq()
    edge.set(DEVICE, "TANK_3/Level", 77.0)                  # this message has the gap
    wait_for(lambda: edge.rebirth_requests > before, what="rebirth after a seq gap")
    assert c.ignored.get("seq gap", 0) >= 1
    wait_value(lf, "TANK_3", "level_pct", 77.0)             # it comes back in the DBIRTH
    assert c.quality[("TANK_3", "level_pct")]["quality"] == "good"


def test_rebirth_requests_are_rate_limited(broker, edge, make_live):
    lf = make_live(make_spec(broker.port, rebirth_min_interval_s=10))
    c = conn_of(lf)
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    asked = c.rebirths_requested
    assert asked == 1
    edge.skip_seq()
    edge.set(DEVICE, "TANK_3/Level", 80.0)
    wait_for(lambda: c.ignored.get("seq gap", 0) >= 1, what="seq gap seen")
    time.sleep(0.8)                                         # several more ignored messages
    assert c.rebirths_requested == asked                    # one per node per 10 s
    assert c.quality[("TANK_3", "level_pct")]["quality"] == "uncertain"
    lf.step(0.01)
    assert 80.0 not in {v for _t, v in lf.history[("TANK_3", "level_pct")]}


# ---------- deaths and the broker ----------

def test_node_death_and_rebirth(broker, edge, make_live):
    lf = make_live(make_spec(broker.port))
    c = conn_of(lf)
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    old_bdseq = edge.bdseq

    edge.die()                                              # the broker publishes the NDEATH will
    wait_for(lambda: all(q["quality"] == "comm_lost" for q in c.quality.values()), what="NDEATH")
    assert "NDEATH" in c.quality[("TANK_5", "level_pct")]["detail"]
    time.sleep(0.4)
    assert c.read() == []
    res = c.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
    assert res["ok"] is False and "nothing was sent" in res["detail"]
    assert edge.writes == []

    edge.revive()
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    wait_for(lambda: all(q["quality"] == "good" for q in c.quality.values()), what="good after rebirth")
    assert edge.bdseq == old_bdseq + 1

    # A late NDEATH from the old session (old bdSeq) changes nothing.
    late = codec.encode_payload({"timestamp": 1, "metrics": [
        {"name": "bdSeq", "datatype": "Int64", "value": old_bdseq}]})
    broker.publish(f"spBv1.0/{GROUP}/NDEATH/{NODE}", late)
    wait_for(lambda: c.ignored.get("NDEATH from an older session (bdSeq mismatch)", 0) >= 1, what="late NDEATH")
    assert all(q["quality"] == "good" for q in c.quality.values())


def test_broker_loss_then_reconnect(broker, make_live):
    port = broker.port
    e = fake_sparkplug.start(port, GROUP, NODE, devices=copy.deepcopy(DEVICES),
                             node_metrics=dict(NODE_METRICS), period_s=0.2)
    b2 = None
    try:
        lf = make_live(make_spec(port))
        c = conn_of(lf)
        wait_value(lf, "TANK_3", "level_pct", 62.0)
        broker.stop()
        wait_for(lambda: status_of(lf)["status"] == "unavailable", what="unavailable")
        assert all(q["quality"] == "comm_lost" for q in c.quality.values())
        assert c.read() == []
        b2 = mqtt_broker.start(port=port)
        e.set(DEVICE, "TANK_3/Level", 66.0)
        wait_value(lf, "TANK_3", "level_pct", 66.0, timeout=12)
        assert status_of(lf)["status"] == "connected"
    finally:
        e.stop()
        if b2 is not None:
            b2.stop()


def test_close_returns_quickly_and_says_offline(broker, edge, make_live):
    lf = make_live(make_spec(broker.port))
    c = conn_of(lf)
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    t0 = time.monotonic()
    c.close()
    assert time.monotonic() - t0 < 0.2
    assert c.status()["available"] is False
    wait_for(lambda: b'"online": false' in broker.retained().get("spBv1.0/STATE/freeboard_test", b""),
             what="STATE offline")


# ---------- writes ----------

def test_writes_only_through_confirm(broker, edge, make_live):
    lf = make_live(make_spec(broker.port))
    wait_value(lf, "VALVE_D_6", "position", "OPEN")

    p = run(lf.propose({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"},
                       reason="test", source="console"))
    assert p["status"] == "PENDING" and p["connector_id"] == "CONN_SPB"
    time.sleep(0.4)
    assert edge.writes == []
    assert host_publishes(broker, "DCMD") == []

    done = run(lf.confirm(p["id"], by="operator"))
    assert done["status"] == "CONFIRMED", done
    wait_for(lambda: len(edge.writes) == 1, what="one DCMD")
    time.sleep(0.4)
    assert len(edge.writes) == 1
    w = edge.writes[0]
    assert (w["type"], w["device"], w["metric"], w["value"], w["datatype"]) == \
        ("DCMD", DEVICE, "VALVE_D_6/Open", False, codec.BOOLEAN)
    dcmd = host_publishes(broker, "DCMD")
    assert len(dcmd) == 1 and dcmd[0]["qos"] == 0 and dcmd[0]["retain"] is False
    wait_value(lf, "VALVE_D_6", "position", "CLOSED")       # the next DDATA confirms it


def test_send_says_uncertain_when_publish_never_completes(broker, edge, make_live, monkeypatch):
    lf = make_live(make_spec(broker.port))
    c = conn_of(lf)
    wait_value(lf, "PUMP_3", "running", True)

    class Stuck:
        rc = 0

        def wait_for_publish(self, timeout=None):
            pass

        def is_published(self):
            return False

    monkeypatch.setattr(c._client, "publish", lambda *a, **k: Stuck())
    res = c.send({"type": "SET_PUMP", "asset_id": "PUMP_3", "value": "OFF"})
    assert res == {"ok": False, "uncertain": True,
                   "detail": "sent; delivery not confirmed - check the field before retrying"}
    bad = c.send({"type": "CHOKE_WELL", "asset_id": "PUMP_3", "value": 0.5})
    assert bad["ok"] is False


# ---------- config ----------

def test_bad_env_name_and_bad_addresses_show_unavailable(make_live, monkeypatch):
    monkeypatch.setenv("MY_BROKER_PASSWORD", "hunter2-not-for-logs")
    cases = [
        ({"username": "fb", "password_env": "MY_BROKER_PASSWORD"}, None, "FREEBOARD_CONN_"),
        ({}, "FREEBOARD/EDGE_1/+/TANK_3/Level", "wildcard"),
        ({}, "OTHER/EDGE_1/PLC_1/TANK_3/Level", "group_id"),
        ({}, "FREEBOARD/EDGE_1", "group/edge_node/device/metric"),
    ]
    for config, address, why in cases:
        spec = make_spec(1, **config)
        if address:
            spec["signals"][0]["address"] = address
        lf = make_live(spec)
        s = status_of(lf)
        assert s["status"] == "unavailable" and s["kind"] == "sparkplug"
        assert "bad config" in s["detail"] and why in s["detail"], s["detail"]
        assert "hunter2" not in s["detail"]


def test_password_from_freeboard_env(make_live, monkeypatch):
    b = mqtt_broker.start(username="fb", password="s3cret-pass")
    try:
        monkeypatch.setenv("FREEBOARD_CONN_SPB_PASSWORD", "s3cret-pass")
        good = make_live(make_spec(b.port, username="fb", password_env="FREEBOARD_CONN_SPB_PASSWORD"))
        wait_for(lambda: status_of(good)["status"] == "connected", what="login")
        monkeypatch.setenv("FREEBOARD_CONN_SPB_WRONG", "wrong-pass")
        bad = make_live(make_spec(b.port, username="fb", password_env="FREEBOARD_CONN_SPB_WRONG",
                                  client_id="freeboard-wrong"))
        wait_for(lambda: "refused" in status_of(bad)["detail"], what="refused login")
        assert status_of(bad)["status"] == "unavailable"
        assert "wrong-pass" not in status_of(bad)["detail"]
    finally:
        b.stop()


def test_parse_address():
    assert parse_address("G/E/D/A/b", "G", "x") == ("E", "D", "A/b")
    assert parse_address("G/E//Battery", "G", "x") == ("E", "", "Battery")
    for bad in ("G/E/#", "G/E/D", "G//D/m", "G/E/D/", "", None):
        with pytest.raises(ValueError):
            parse_address(bad, "G", "x")
