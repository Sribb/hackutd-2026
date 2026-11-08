"""LoRaWAN uplink connector against the fake broker and the fake network server, through LiveField.

Everything binds 127.0.0.1 on free ports. Each test stops what it started."""
import asyncio
import copy
import time

import pytest

from backend.app.connectors import KINDS
from backend.app.connectors.lorawan import LorawanConnector, codec_error, lookup, parse_address, parse_time
from backend.app.live import LiveField
from backend.app.scenario import load_scenario
from backend.fakes import lorawan as fake_lorawan
from backend.fakes import mqtt_broker

TANK_EUI, PUMP_EUI = "a84041000181c2ee", "a84041000181c2ef"
DEVICES = {
    "tank-3-level": {"dev_eui": TANK_EUI, "decoded": {"level_pct": 62.0, "battery_v": 3.6,
                                                       "probe": {"ma": 12.0}}},
    "pump-3-flow": {"dev_eui": PUMP_EUI, "decoded": {"flow_bbl_hr": 118.0, "running": True}},
}
APP = "4f1d3c0e-5b7a-4c4e-9d0a-2b6f8e1c7a90"


def make_spec(port: int, network: str = "chirpstack", **config) -> dict:
    tank, pump = (TANK_EUI, PUMP_EUI) if network == "chirpstack" else ("tank-3-level", "pump-3-flow")
    return {
        "id": "CONN_LORA", "kind": "lorawan",
        "config": {"host": "127.0.0.1", "port": port, "network": network,
                   "application_id": APP if network == "chirpstack" else "freeboard-swd",
                   "signal_map": {"TANK_4.level_pct": {"ma420": True, "eng": [0, 100]}}, **config},
        "signals": [
            {"asset_id": "TANK_3", "signal": "level_pct", "address": f"{tank}/level_pct"},
            {"asset_id": "TANK_4", "signal": "level_pct", "address": f"{tank}/probe.ma"},
            {"asset_id": "PUMP_3", "signal": "flow_bbl_hr", "address": f"{pump}/flow_bbl_hr"},
            {"asset_id": "PUMP_3", "signal": "running", "address": f"{pump}/running"},
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
    monkeypatch.setitem(KINDS, "lorawan", LorawanConnector)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def broker():
    b = mqtt_broker.start()
    yield b
    b.stop()


@pytest.fixture
def ns(broker):
    f = fake_lorawan.start(broker.port, "chirpstack", APP, devices=copy.deepcopy(DEVICES), period_s=0.2)
    yield f
    f.stop()


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


def conn_of(lf: LiveField) -> LorawanConnector:
    return lf.registry.get("CONN_LORA")


def wait_value(lf: LiveField, aid: str, sig: str, want, timeout: float = 8.0):
    def check():
        lf.step(0.01)
        return lf.value(aid, sig) == want
    wait_for(check, timeout, what=f"{aid}.{sig} == {want!r} (got {lf.value(aid, sig)!r})")


def status_of(lf: LiveField) -> dict:
    return next(c for c in lf.state()["connectors"] if c["id"] == "CONN_LORA")


def q(c: LorawanConnector, aid: str, sig: str) -> str:
    return c.quality[(aid, sig)]["quality"]


# ---------- readings ----------

def test_chirpstack_readings_flow_end_to_end(broker, ns, make_live):
    lf = make_live(make_spec(broker.port))
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    wait_value(lf, "TANK_4", "level_pct", 50.0)            # 12 mA on a 0-100 % 4-20 mA loop, nested path
    wait_value(lf, "PUMP_3", "flow_bbl_hr", 118.0)
    wait_value(lf, "PUMP_3", "running", True)
    assert lf.state()["assets"]["TANK_3"]["status"] != "UNKNOWN"
    s = status_of(lf)
    assert s["status"] == "connected" and "ChirpStack" in s["detail"]
    ns.set("tank-3-level", "level_pct", 63.5)
    wait_value(lf, "TANK_3", "level_pct", 63.5)
    assert q(conn_of(lf), "TANK_3", "level_pct") == "good"


def test_ttn_readings_flow_end_to_end(broker, make_live):
    f = fake_lorawan.start(broker.port, "ttn", "freeboard-swd", devices=copy.deepcopy(DEVICES), period_s=0.2)
    try:
        lf = make_live(make_spec(broker.port, network="ttn"))
        wait_value(lf, "TANK_3", "level_pct", 62.0)
        wait_value(lf, "PUMP_3", "running", True)
        assert "v3/freeboard-swd@ttn/devices/" in f.sent[0]["topic"]
        assert "The Things Stack" in status_of(lf)["detail"]
        f.codec_error("tank-3-level")
        c = conn_of(lf)
        wait_for(lambda: q(c, "TANK_3", "level_pct") == "bad", what="missing decoded_payload is bad")
        assert "decoded_payload" in c.quality[("TANK_3", "level_pct")]["detail"]
    finally:
        f.stop()


def test_bad_values_give_no_reading_and_unknown(broker, ns, make_live):
    lf = make_live(make_spec(broker.port))
    c = conn_of(lf)
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    wait_value(lf, "PUMP_3", "flow_bbl_hr", 118.0)
    wait_value(lf, "TANK_4", "level_pct", 50.0)

    ns.codec_error("tank-3-level")                          # TANK_3 and TANK_4 lose their values
    wait_for(lambda: q(c, "TANK_3", "level_pct") == "bad", what="codec error is bad")
    assert q(c, "TANK_4", "level_pct") == "bad"
    ns.set("pump-3-flow", "flow_bbl_hr", -40.0)             # below the physical range
    wait_for(lambda: q(c, "PUMP_3", "flow_bbl_hr") == "out_of_range", what="negative flow")
    ns.set("pump-3-flow", "running", None)                  # a missing field
    wait_for(lambda: q(c, "PUMP_3", "running") == "null", what="missing field")

    lf.step(0.01)
    keys = [("TANK_3", "level_pct"), ("TANK_4", "level_pct"), ("PUMP_3", "flow_bbl_hr")]
    marks = {k: lf.latest[k]["t"] for k in keys}
    time.sleep(0.6)
    lf.step(6.0)                                            # past sensors.stale_after_min
    for k, t in marks.items():
        assert lf.latest[k]["t"] == t, k                    # never the last good value again
    assets = lf.state()["assets"]
    assert assets["TANK_3"]["status"] == "UNKNOWN"
    assert assets["TANK_4"]["status"] == "UNKNOWN"

    ns.codec_error("tank-3-level", None)                    # good uplinks count again
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    ns.set("tank-3-level", "level_pct", 140.0)              # 140 % of a tank is not a level
    wait_for(lambda: q(c, "TANK_3", "level_pct") == "out_of_range", what="level above 100 %")


def test_value_held_only_within_uplink_window(broker, make_live):
    f = fake_lorawan.start(broker.port, "chirpstack", APP, devices=copy.deepcopy(DEVICES), period_s=60)
    try:
        lf = make_live(make_spec(broker.port, uplink_interval_min=10))
        c = conn_of(lf)
        wait_for(lambda: status_of(lf)["status"] == "connected", what="connected")
        f.pause()
        n = c.uplinks
        assert f.uplink("tank-3-level")
        wait_for(lambda: c.uplinks > n, what="an uplink")
        sent = f.sent[-1]["time"]
        first = [r for r in c.read() if r["asset_id"] == "TANK_3"]
        assert first and first[0]["value"] == 62.0
        again = [r for r in c.read() if r["asset_id"] == "TANK_3"]
        assert again and again[0]["value"] == 62.0            # held between uplinks
        c.clock = lambda: sent + 899.0                        # still inside 1.5 x 10 min
        assert any(r["asset_id"] == "TANK_3" for r in c.read())
        c.clock = lambda: sent + 901.0                        # past it: never held longer
        assert [r for r in c.read() if r["asset_id"] in ("TANK_3", "TANK_4")] == []
        assert q(c, "TANK_3", "level_pct") == "stale"
        assert "uplink_interval_min" in c.quality[("TANK_3", "level_pct")]["detail"]
        assert c.read() == []
    finally:
        f.stop()


def test_no_hold_by_default(broker, make_live):
    f = fake_lorawan.start(broker.port, "chirpstack", APP, devices=copy.deepcopy(DEVICES), period_s=60)
    try:
        lf = make_live(make_spec(broker.port))
        c = conn_of(lf)
        wait_for(lambda: status_of(lf)["status"] == "connected", what="connected")
        f.pause()
        n = c.uplinks
        f.uplink("tank-3-level")
        wait_for(lambda: c.uplinks > n, what="an uplink")
        assert any(r["asset_id"] == "TANK_3" for r in c.read())
        assert c.read() == []                                 # one uplink, one reading
    finally:
        f.stop()


def test_old_replayed_and_retained_uplinks_are_not_fresh(broker, make_live):
    f = fake_lorawan.start(broker.port, "chirpstack", APP, devices=copy.deepcopy(DEVICES), period_s=60)
    try:
        f.pause()
        f.publish_retained("tank-3-level")                    # waiting on the broker for a new subscriber
        lf = make_live(make_spec(broker.port, uplink_interval_min=10))
        c = conn_of(lf)
        wait_for(lambda: c.ignored.get("a retained uplink"), what="retained uplink ignored")
        assert q(c, "TANK_3", "level_pct") == "stale"
        assert c.read() == []

        f.uplink("tank-3-level", shift_s=-1000)              # older than 1.5 x 10 min
        wait_for(lambda: c.ignored.get("an uplink outside its freshness window"), what="old uplink ignored")
        assert c.read() == [] and q(c, "TANK_3", "level_pct") == "stale"

        f.uplink("tank-3-level")
        wait_for(lambda: q(c, "TANK_3", "level_pct") == "good", what="a fresh uplink")
        assert any(r["asset_id"] == "TANK_3" for r in c.read())
        f.uplink("tank-3-level", shift_s=-30)                 # older than the one already taken
        wait_for(lambda: c.ignored.get("a replayed or duplicate uplink"), what="replay ignored")
    finally:
        f.stop()


# ---------- read-only ----------

def test_read_only_never_writes(broker, ns, make_live):
    lf = make_live(make_spec(broker.port))
    c = conn_of(lf)
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    res = c.send({"type": "SET_PUMP", "asset_id": "PUMP_3", "value": "OFF"})
    assert res["ok"] is False and "never writes" in res["detail"]

    # A command for an asset this connector reads goes to the simulator, never to the radio.
    p = run(lf.propose({"type": "SET_PUMP", "asset_id": "PUMP_3", "value": "OFF"}, reason="test", source="console"))
    assert p["status"] == "PENDING" and p["connector_id"] != "CONN_LORA"
    done = run(lf.confirm(p["id"], by="operator"))
    assert done["status"] == "CONFIRMED"
    time.sleep(0.3)
    assert ns.writes == []

    spec = make_spec(broker.port)
    spec["commands"] = [{"asset_id": "PUMP_3", "command": "SET_PUMP", "address": f"{PUMP_EUI}/running"}]
    s = status_of(make_live(spec))
    assert s["status"] == "unavailable" and "read-only" in s["detail"]


# ---------- connection ----------

def test_broker_loss_then_reconnect(broker, make_live):
    port = broker.port
    f = fake_lorawan.start(port, "chirpstack", APP, devices=copy.deepcopy(DEVICES), period_s=0.2)
    b2 = None
    try:
        lf = make_live(make_spec(port, uplink_interval_min=10))
        c = conn_of(lf)
        wait_value(lf, "TANK_3", "level_pct", 62.0)
        broker.stop()
        wait_for(lambda: status_of(lf)["status"] == "unavailable", what="unavailable")
        assert all(v["quality"] == "comm_lost" for v in c.quality.values())
        assert c.read() == []                                 # held values are dropped with the link
        b2 = mqtt_broker.start(port=port)
        f.set("tank-3-level", "level_pct", 66.0, send=False)
        wait_value(lf, "TANK_3", "level_pct", 66.0, timeout=15)
        assert status_of(lf)["status"] == "connected"
    finally:
        f.stop()
        if b2 is not None:
            b2.stop()


def test_device_goes_quiet_then_stale(broker, ns, make_live):
    lf = make_live(make_spec(broker.port))
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    ns.pause()
    time.sleep(0.4)
    lf.step(0.01)
    lf.step(6.0)
    assert lf.state()["assets"]["TANK_3"]["status"] == "UNKNOWN"
    ns.resume()
    wait_value(lf, "TANK_3", "level_pct", 62.0)


def test_close_returns_quickly(broker, ns, make_live):
    lf = make_live(make_spec(broker.port))
    c = conn_of(lf)
    wait_value(lf, "TANK_3", "level_pct", 62.0)
    t0 = time.monotonic()
    c.close()
    assert time.monotonic() - t0 < 0.2
    assert c.status()["available"] is False
    assert c.read() == []
    assert all(v["quality"] == "comm_lost" for v in c.quality.values())


# ---------- config ----------

def test_bad_config_shows_unavailable(make_live, monkeypatch):
    monkeypatch.setenv("MY_BROKER_PASSWORD", "hunter2-not-for-logs")
    cases = [
        ({"username": "fb", "password_env": "MY_BROKER_PASSWORD"}, None, "FREEBOARD_CONN_"),
        ({"network": "lora-cloud"}, None, "network"),
        ({}, "nothex/level_pct", "dev_eui"),
        ({}, f"{TANK_EUI}/+", "wildcard"),
        ({}, f"{TANK_EUI}", "path"),
    ]
    for config, address, why in cases:
        spec = make_spec(1, **config)
        if address:
            spec["signals"][0]["address"] = address
        s = status_of(make_live(spec))
        assert s["status"] == "unavailable" and s["kind"] == "lorawan"
        assert "bad config" in s["detail"] and why in s["detail"], s["detail"]
        assert "hunter2" not in s["detail"]


def test_password_from_freeboard_env(make_live, monkeypatch):
    b = mqtt_broker.start(username="freeboard-swd@ttn", password="NNSXS.secret-key")
    try:
        monkeypatch.setenv("FREEBOARD_CONN_TTN_KEY", "NNSXS.secret-key")
        good = make_live(make_spec(b.port, network="ttn", username="freeboard-swd@ttn",
                                   password_env="FREEBOARD_CONN_TTN_KEY"))
        wait_for(lambda: status_of(good)["status"] == "connected", what="login")
        monkeypatch.setenv("FREEBOARD_CONN_TTN_WRONG", "wrong-key")
        bad = make_live(make_spec(b.port, network="ttn", username="freeboard-swd@ttn",
                                  password_env="FREEBOARD_CONN_TTN_WRONG", client_id="freeboard-wrong"))
        wait_for(lambda: "refused" in status_of(bad)["detail"], what="refused login")
        assert "wrong-key" not in status_of(bad)["detail"]
        unset = make_live(make_spec(b.port, network="ttn", username="u", password_env="FREEBOARD_CONN_NOT_SET",
                                    client_id="freeboard-unset"))
        s = status_of(unset)
        assert s["status"] == "unavailable" and "FREEBOARD_CONN_NOT_SET" in s["detail"]
    finally:
        b.stop()


# ---------- helpers ----------

def test_parse_time_address_lookup_and_codec_error():
    t = parse_time("2026-11-07T07:47:00.123456789Z")
    assert t is not None and abs(t - parse_time("2026-11-07T01:47:00.123456-06:00")) < 1e-3
    assert parse_time("yesterday") is None and parse_time(None) is None
    assert parse_address("A84041000181C2EE/level_pct", "chirpstack", "x") == (TANK_EUI, ("level_pct",))
    assert parse_address("tank-3-level/a.0.b", "ttn", "x") == ("tank-3-level", ("a", "0", "b"))
    for bad in ("tank-3-level/level_pct", f"{TANK_EUI}/", f"{TANK_EUI}/a..b", "", None):
        with pytest.raises(ValueError):
            parse_address(bad, "chirpstack", "x")
    assert lookup({"a": [{"b": 3}]}, ("a", "0", "b")) == (True, 3)
    assert lookup({"a": [1]}, ("a", "4")) == (False, None)
    assert codec_error({"errors": ["bad length"]}) and codec_error("text")
    assert codec_error({"level_pct": 1, "warnings": ["low battery"]}) is None
