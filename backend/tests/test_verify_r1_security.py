"""Verification round 1 (#126 core): connector hosts on an operator's list, webhook secrets
only from FREEBOARD_CONN_ variables, connector counts capped before activation, and the
connector-level minor fixes (Sparkplug status and quality timing, OPC UA held values,
the SENSOR_STALE wording). Fakes bind 127.0.0.1 on free ports and are always stopped."""
import copy
import datetime as dt
import time
from types import SimpleNamespace as NS

import pytest

from backend.app import blueprints
from backend.app.connectors import KINDS, hosts
from backend.app.connectors.opcua_ua import OpcUaConnector
from backend.app.connectors.sparkplug import SparkplugConnector
from backend.app.live import LiveField
from backend.app.scenario import load_scenario
from backend.fakes import mqtt_broker
from backend.fakes import sparkplug as fake_sparkplug


def wait_for(pred, timeout=8.0, every=0.05, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setitem(KINDS, "sparkplug", SparkplugConnector)
    monkeypatch.setitem(KINDS, "opcua", OpcUaConnector)
    monkeypatch.delenv(hosts.ALLOWED_ENV, raising=False)
    for var in ("JARVIS_CONFIRM", "JARVIS_MODE", "ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


def status_with(spec):
    sc = copy.deepcopy(load_scenario())
    sc["connectors"] = [spec]
    lf = LiveField(sc)
    try:
        return next(c for c in lf.registry.status() if c["id"] == spec["id"]), lf
    finally:
        lf.registry.close()


def spb(host, port=1883, **config):
    return {"id": "CONN_SPB", "kind": "sparkplug",
            "config": {"host": host, "port": port, "group_id": "FREEBOARD", "host_id": "verify", **config},
            "signals": [{"asset_id": "TANK_3", "signal": "level_pct", "address": "FREEBOARD/EDGE_1/PLC_1/TANK_3/Level"}]}


# ---------- hosts ----------

def test_only_this_machine_by_default():
    assert hosts.allowed("127.0.0.1") and hosts.allowed("localhost") and hosts.allowed("::1")
    for h in ("169.254.169.254", "10.1.2.3", "metadata.internal", "192.168.1.5", "example.com", ""):
        assert not hosts.allowed(h), h


def test_the_operator_list(monkeypatch):
    monkeypatch.setenv(hosts.ALLOWED_ENV, "10.0.0.0/24, scada.plant.local .edge.local")
    assert hosts.allowed("10.0.0.7") and not hosts.allowed("10.0.1.7")
    assert hosts.allowed("SCADA.plant.local") and not hosts.allowed("other.plant.local")
    assert hosts.allowed("gw1.edge.local") and hosts.allowed("edge.local")
    assert not hosts.allowed("127.0.0.1")         # a list replaces the default
    monkeypatch.setenv(hosts.ALLOWED_ENV, "*")
    assert hosts.allowed("10.9.9.9") and hosts.allowed("broker.example.com")
    for h in ("169.254.169.254", "metadata.google.internal", "fe80::1"):
        assert not hosts.allowed(h), h            # "*" never means the metadata endpoint
    monkeypatch.setenv(hosts.ALLOWED_ENV, "*,169.254.169.254")
    assert hosts.allowed("169.254.169.254")       # only when listed on its own


@pytest.mark.parametrize("host", ["169.254.169.254", "10.1.2.3", "metadata.internal"])
def test_sparkplug_never_connects_to_a_host_not_allowed(host):
    st, lf = status_with(spb(host))
    assert st["status"] == "unavailable" and "not allowed" in st["detail"], st
    assert "connecting" not in st["detail"] and "reach the broker" not in st["detail"]
    assert type(lf.registry.get("CONN_SPB")).__name__ == "UnavailableConnector"   # no client, no thread


def test_opcua_never_connects_to_a_host_not_allowed():
    spec = {"id": "CONN_OPCUA", "kind": "opcua",
            "config": {"endpoint": "opc.tcp://10.1.2.3:4840", "cert_path_env": "FREEBOARD_CONN_C",
                       "key_path_env": "FREEBOARD_CONN_K"},
            "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "ns=2;s=TANK_6.Level"}]}
    st, _ = status_with(spec)
    assert st["status"] == "unavailable" and "not allowed" in st["detail"], st


# ---------- webhook ----------

def hook(**config):
    return {"id": "CONN_HOOK", "kind": "webhook", "config": config,
            "signals": [{"asset_id": "TANK_5", "signal": "level_pct"}],
            "commands": [{"asset_id": "VALVE_XO_34", "command": "SET_VALVE"}]}


@pytest.mark.parametrize("var", ["AWS_SECRET_ACCESS_KEY", "FREEBOARD_TOKEN", "SPACETIME_TOKEN", "PATH"])
def test_webhook_reads_no_variable_outside_freeboard_conn(monkeypatch, var):
    monkeypatch.setenv(var, "do-not-leak-this-value")
    st, lf = status_with(hook(url="http://127.0.0.1:9/cmd", token_env=var))
    assert st["status"] == "unavailable" and "FREEBOARD_CONN_" in st["detail"], st
    assert "do-not-leak-this-value" not in str(st)
    res = lf.registry.get("CONN_HOOK").send({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"})
    assert res["ok"] is False and "do-not-leak-this-value" not in str(res)


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data", "http://10.1.2.3/cmd",
                                 "ftp://127.0.0.1/x", "http://user:pw@127.0.0.1/x"])
def test_webhook_posts_only_to_an_allowed_url(url):
    st, _ = status_with(hook(url=url))
    assert st["status"] == "unavailable" and "bad config" in st["detail"], st
    assert "pw" not in st["detail"].replace("password", "")


def test_webhook_with_freeboard_conn_token_and_allowed_url_still_works(monkeypatch):
    monkeypatch.setenv("FREEBOARD_CONN_HOOK_TOKEN", "t0k")
    st, lf = status_with(hook(url="http://127.0.0.1:9/cmd", token_env="FREEBOARD_CONN_HOOK_TOKEN"))
    assert st["status"] == "connected", st
    assert lf.registry.get("CONN_HOOK").authorized("t0k")


# ---------- connector counts ----------

def test_a_blueprint_with_too_many_connectors_is_refused_before_anything_is_built(tmp_path):
    many = [spb("127.0.0.1") | {"id": f"CONN_S{i}"} for i in range(blueprints.MAX_CONNECTORS + 1)]
    errors, _, scen = blueprints.check_blueprint({"connectors": many}, tmp_path / "x.json")
    assert scen is None and any("connectors" in e and str(blueprints.MAX_CONNECTORS) in e for e in errors)
    big = spb("127.0.0.1")
    big["signals"] = big["signals"] * (blueprints.MAX_CONNECTOR_SIGNALS + 1)
    assert any("signals" in e for e in blueprints.connector_limits({"connectors": [big]}))
    big = spb("127.0.0.1")
    big["commands"] = [{"asset_id": "PUMP_3", "command": "SET_PUMP"}] * (blueprints.MAX_CONNECTOR_COMMANDS + 1)
    assert any("commands" in e for e in blueprints.connector_limits({"connectors": [big]}))
    assert blueprints.connector_limits({"connectors": [spb("127.0.0.1")]}) == []
    assert blueprints.connector_limits({}) == []


# ---------- Sparkplug: status and quality timing ----------

def test_sparkplug_status_and_quality_follow_the_edge_node():
    b = mqtt_broker.start()
    edge = fake_sparkplug.start(b.port, "FREEBOARD", "EDGE_1",
                                devices={"PLC_1": {"TANK_3/Level": ("Float", 80.2)}}, period_s=0.2)
    c = SparkplugConnector(spb("127.0.0.1", b.port, timeout_s=2, rebirth_min_interval_s=0.5,
                               signal_map={"TANK_3.level_pct": {"range": [0, 100]}}), None, lambda: 1.0)
    key = ("TANK_3", "level_pct")
    try:
        wait_for(lambda: any(r["value"] == pytest.approx(80.2) for r in c.read()), what="first value")
        assert c.status()["available"] is True

        # Out of range: not good at once. A good value then waits for read() to count.
        edge.set("PLC_1", "TANK_3/Level", 130.0)
        wait_for(lambda: c.quality[key]["quality"] == "out_of_range", what="out_of_range")
        c.read()
        edge.set("PLC_1", "TANK_3/Level", 70.2)
        wait_for(lambda: key in c._inbox._items, what="the new value in the inbox")
        assert c.quality[key]["quality"] == "out_of_range", "good before read() handed the value on"
        got = [r["value"] for r in c.read() if (r["asset_id"], r["signal"]) == key]
        assert got == [pytest.approx(70.2)] and c.quality[key]["quality"] == "good"

        # The only mapped edge node dies: unavailable, with the reason kept.
        edge.die()
        wait_for(lambda: c.quality[key]["quality"] == "comm_lost", what="NDEATH")
        st = c.status()
        assert st["available"] is False and "edge nodes online: none" in st["detail"], st
        edge.revive()
        wait_for(lambda: c.status()["available"], what="available after rebirth")
    finally:
        c.close()
        edge.stop()
        b.stop()


# ---------- OPC UA: a held value runs out on the live clock ----------

def test_opcua_held_value_runs_out_after_the_live_stale_window(monkeypatch):
    from backend.app.connectors import opcua_ua
    monkeypatch.setattr(opcua_ua.sm, "import_optional", lambda module, package: (None, "asyncua is off"))
    clock = {"t": 0.0}
    spec = {"id": "CONN_OPCUA", "kind": "opcua",
            "config": {"endpoint": "opc.tcp://127.0.0.1:1/x", "cert_path_env": "FREEBOARD_CONN_C",
                       "key_path_env": "FREEBOARD_CONN_K", "max_age_s": 300},
            "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "ns=2;s=TANK_6.Level"}]}
    conn = OpcUaConnector(spec, None, lambda: clock["t"])
    conn._client = object()                       # pretend a session is up
    now = dt.datetime.now(dt.timezone.utc)
    conn.notify("ns=2;s=TANK_6.Level", NS(StatusCode=None, Value=NS(Value=48.0), SourceTimestamp=now,
                                          ServerTimestamp=now))
    assert [r["value"] for r in conn.read()] == [48.0]
    clock["t"] = 4.9                              # still inside stale_after_min (5): held and re-read
    assert [r["value"] for r in conn.read()] == [48.0]
    clock["t"] = 8.0                              # 8 live minutes with no new value, seconds of wall time
    assert conn.read() == [], "a frozen tag outlived the live stale window"
    assert conn.quality[("TANK_6", "level_pct")]["quality"] == "stale"


# ---------- detect.py wording ----------

def test_sensor_stale_message_reads_well_before_any_reading():
    lf = LiveField(copy.deepcopy(load_scenario()))
    lf.detector.expected_keys.add(("TANK_3", "pressure_psi"))
    for _ in range(2):
        lf.step(1.0)
    msg = next(a["message"] for a in lf.detector.active.values()
               if a["asset_id"] == "TANK_3" and a["kind"] == "SENSOR_STALE")
    assert "since startup" in msg and "for since" not in msg, msg
    lf.registry.close()
