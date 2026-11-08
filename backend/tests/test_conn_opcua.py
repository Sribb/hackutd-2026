"""The OPC UA connector against the fake server, over an encrypted session.

Every test that talks to a server uses Basic256Sha256 SignAndEncrypt with username
login. The fakes bind 127.0.0.1 on free ports and are always stopped."""
import asyncio
import copy
import time

import pytest

from backend.app.connectors import KINDS
from backend.app.connectors.opcua_ua import OpcUaConnector
from backend.app.live import LiveField
from backend.app.scenario import load_scenario
from backend.fakes import opcua as fake_opcua

ENV = {"cert": "FREEBOARD_CONN_OPCUA_CERT", "key": "FREEBOARD_CONN_OPCUA_KEY",
       "server": "FREEBOARD_CONN_OPCUA_SERVER_CERT", "password": "FREEBOARD_CONN_OPCUA_PASSWORD"}


def wait_for(cond, timeout=10.0, every=0.05, step=None):
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


@pytest.fixture(scope="module")
def certdir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("opcua-certs"))


@pytest.fixture(scope="module")
def fake(certdir):
    f = fake_opcua.start(workdir=certdir)
    yield f
    f.stop()


@pytest.fixture(autouse=True)
def env(monkeypatch, fake):
    monkeypatch.setitem(KINDS, "opcua", OpcUaConnector)
    monkeypatch.setenv(ENV["cert"], fake.client_cert)
    monkeypatch.setenv(ENV["key"], fake.client_key)
    monkeypatch.setenv(ENV["server"], fake.server_cert)
    monkeypatch.setenv(ENV["password"], fake.password)
    for var in ("JARVIS_CONFIRM", "JARVIS_MODE", "ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


def spec_for(endpoint, **config):
    return {
        "id": "CONN_OPCUA", "kind": "opcua",
        "config": {"endpoint": endpoint, "security_policy": "Basic256Sha256", "security_mode": "SignAndEncrypt",
                   "cert_path_env": ENV["cert"], "key_path_env": ENV["key"], "server_cert_path_env": ENV["server"],
                   "username": "freeboard", "password_env": ENV["password"], "publish_ms": 100, "timeout_s": 3,
                   **config},
        "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "ns=2;s=TANK_6.Level"},
                    {"asset_id": "PUMP_6", "signal": "running", "address": "ns=2;s=PUMP_6.Running"},
                    {"asset_id": "PUMP_6", "signal": "flow_bbl_hr", "address": "ns=2;s=PUMP_6.Flow"},
                    {"asset_id": "VALVE_D_6", "signal": "position", "address": "ns=2;s=VALVE_D_6.Position"}],
        "commands": [{"asset_id": "VALVE_D_6", "command": "SET_VALVE", "address": "ns=2;s=VALVE_D_6.Command"},
                     {"asset_id": "PUMP_6", "command": "SET_PUMP", "address": "ns=2;s=PUMP_6.Command"}],
    }


def live_with(spec):
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    sc["connectors"] = [spec]
    return LiveField(sc)


def conn_of(lf):
    return lf.registry.get("CONN_OPCUA")


def conn_status(lf):
    return next(c for c in lf.state()["connectors"] if c["id"] == "CONN_OPCUA")


@pytest.fixture
def live(fake):
    lf = live_with(spec_for(fake.endpoint))
    yield lf
    lf.registry.close()
    fake.set_quality("TANK_6.Level", "good")
    fake.set_age("TANK_6.Level", 0)
    fake.set("VALVE_D_6.Command", True)
    fake.write_delay_s = 0


def reading_of(lf, aid, sig):
    r = lf.latest.get((aid, sig))
    return r and r.get("source") == "CONN_OPCUA" and r


# ---------- readings ----------

def test_readings_flow_end_to_end(fake, live):
    fake.set("TANK_6.Level", 55.0)
    wait_for(lambda: conn_status(live)["status"] == "connected", step=lambda: live.step(0.05))
    r = wait_for(lambda: (r := reading_of(live, "TANK_6", "level_pct")) and r["value"] >= 55.0 and r,
                 step=lambda: live.step(0.05))
    assert 55.0 <= r["value"] < 56.0
    wait_for(lambda: reading_of(live, "PUMP_6", "running") and reading_of(live, "VALVE_D_6", "position"),
             step=lambda: live.step(0.05))
    a = live.state()["assets"]
    assert a["TANK_6"]["status"] != "UNKNOWN" and 55.0 <= a["TANK_6"]["level_pct"] < 56.0
    assert a["VALVE_D_6"]["position"] == "OPEN" and a["PUMP_6"]["status"] == "OK"
    assert conn_of(live).quality[("TANK_6", "level_pct")]["quality"] == "good"
    assert "SignAndEncrypt" in conn_status(live)["detail"]


def test_bad_quality_gives_no_reading_and_unknown(fake, live):
    wait_for(lambda: reading_of(live, "TANK_6", "level_pct"), step=lambda: live.step(0.05))
    conn = conn_of(live)
    fake.set_quality("TANK_6.Level", "bad")
    wait_for(lambda: conn.quality[("TANK_6", "level_pct")]["quality"] == "bad", step=lambda: live.step(0.05))
    assert "BadSensorFailure" in conn.quality[("TANK_6", "level_pct")]["detail"]
    # From now on no TANK_6 level arrives, so it goes stale and shows UNKNOWN, never the old value.
    last_t = live.latest[("TANK_6", "level_pct")]["t"]
    for _ in range(8):
        live.step(1.0)
        time.sleep(0.02)
    assert live.latest[("TANK_6", "level_pct")]["t"] == last_t
    tank = live.state()["assets"]["TANK_6"]
    assert tank["status"] == "UNKNOWN" and tank["stale"] is True
    # Signals with good quality keep flowing.
    assert live.latest[("VALVE_D_6", "position")]["t"] > last_t
    # Good quality again: the level comes back.
    fake.set_quality("TANK_6.Level", "good")
    wait_for(lambda: live.state()["assets"]["TANK_6"]["status"] != "UNKNOWN", step=lambda: live.step(0.05))


@pytest.mark.parametrize("quality", ["uncertain", "comm_lost"])
def test_uncertain_and_comm_lost_status_give_no_value(fake, live, quality):
    conn = conn_of(live)
    wait_for(lambda: conn.quality.get(("TANK_6", "level_pct"), {}).get("quality") == "good",
             step=lambda: live.step(0.05))
    fake.set_quality("TANK_6.Level", quality)
    wait_for(lambda: conn.quality[("TANK_6", "level_pct")]["quality"] == quality)
    conn.read()
    assert all(r["signal"] != "level_pct" for r in conn.read())


def test_old_source_timestamp_is_stale(fake, live):
    conn = conn_of(live)
    wait_for(lambda: conn.quality.get(("TANK_6", "level_pct"), {}).get("quality") == "good",
             step=lambda: live.step(0.05))
    fake.set_age("TANK_6.Level", 900)         # older than max_age_s (300)
    wait_for(lambda: conn.quality[("TANK_6", "level_pct")]["quality"] == "stale")
    conn.read()
    assert all(r["signal"] != "level_pct" for r in conn.read())


# ---------- writes ----------

def test_writes_reach_the_fake_only_through_confirm(fake, live):
    wait_for(lambda: conn_status(live)["status"] == "connected", step=lambda: live.step(0.05))
    before = len(fake.writes)

    async def flow():
        p = await live.propose({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"},
                               reason="test", source="console")
        assert p["status"] == "PENDING" and p["connector_id"] == "CONN_OPCUA"
        await asyncio.sleep(0.5)
        assert len(fake.writes) == before, "propose() must not write"
        return await live.confirm(p["id"], by="operator")

    done = asyncio.run(flow())
    assert done["status"] == "CONFIRMED", done
    new = fake.writes[before:]
    assert len(new) == 1
    assert new[0]["node"] == "ns=2;s=VALVE_D_6.Command" and new[0]["value"] is False
    assert new[0]["variant"] == "Boolean"
    wait_for(lambda: live.latest.get(("VALVE_D_6", "position"), {}).get("value") == "CLOSED",
             step=lambda: live.step(0.05))
    time.sleep(0.5)
    assert len(fake.writes) == before + 1, "a write is sent exactly once"


def test_write_with_late_answer_is_uncertain_and_not_resent(fake, live):
    conn = conn_of(live)
    wait_for(lambda: conn.status()["available"], step=lambda: live.step(0.05))
    before = len(fake.writes)
    fake.write_delay_s = 4.0                  # longer than timeout_s (3)
    t0 = time.monotonic()
    res = conn.send({"type": "SET_PUMP", "asset_id": "PUMP_6", "value": "OFF"})
    assert time.monotonic() - t0 < 4.0
    assert res == {"ok": False, "uncertain": True,
                   "detail": "sent; delivery not confirmed - check the field before retrying"}
    fake.write_delay_s = 0
    wait_for(lambda: len(fake.writes) > before, timeout=8)
    time.sleep(1.0)
    assert len(fake.writes) == before + 1, "an uncertain write is never sent again"
    fake.set("PUMP_6.Command", True)


def test_no_write_without_a_session(fake):
    port = fake_opcua.free_port()             # nothing listens here
    conn = OpcUaConnector(spec_for(f"opc.tcp://127.0.0.1:{port}/freeboard/"), None, lambda: 0.0)
    try:
        res = conn.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
        assert res["ok"] is False and "nothing was sent" in res["detail"]
        assert conn.status()["available"] is False
        assert conn.read() == []
    finally:
        conn.close()


# ---------- config and secrets ----------

def test_non_freeboard_env_name_shows_unavailable(fake):
    spec = spec_for(fake.endpoint, password_env="OPCUA_PASSWORD")
    lf = live_with(spec)
    try:
        st = conn_status(lf)
        assert st["status"] == "unavailable"
        assert "FREEBOARD_CONN_" in st["detail"] and fake.password not in st["detail"]
    finally:
        lf.registry.close()


@pytest.mark.parametrize("config, words", [
    ({"security_mode": "None"}, "allow_insecure"),
    ({"security_mode": "None", "allow_insecure": True, "endpoint": "opc.tcp://10.0.0.5:4840"}, "127.0.0.1"),
    ({"endpoint": "http://127.0.0.1:4840"}, "opc.tcp"),
    ({"endpoint": "opc.tcp://user:pw@127.0.0.1:4840"}, "credentials"),
    ({"cert_path_env": None}, "cert_path_env"),
    ({"security_policy": "Basic128Rsa15"}, "security_policy"),
])
def test_bad_config_is_refused(fake, config, words):
    lf = live_with(spec_for(fake.endpoint, **config) if "endpoint" not in config
                   else spec_for(config.pop("endpoint"), **config))
    try:
        st = conn_status(lf)
        assert st["status"] == "unavailable" and words in st["detail"], st
    finally:
        lf.registry.close()


def test_missing_secret_is_named_not_shown(fake, monkeypatch):
    monkeypatch.delenv(ENV["password"])
    conn = OpcUaConnector(spec_for(fake.endpoint), None, lambda: 0.0)
    try:
        st = wait_for(lambda: (s := conn.status())["detail"] != "connecting" and s)
        assert st["available"] is False and ENV["password"] in st["detail"]
    finally:
        conn.close()


# ---------- link loss ----------

def test_stop_and_restart_reconnects(certdir, monkeypatch):
    f = fake_opcua.start(workdir=certdir)
    monkeypatch.setenv(ENV["password"], f.password)
    lf = live_with(spec_for(f.endpoint))
    conn = conn_of(lf)
    f2 = None
    try:
        wait_for(lambda: reading_of(lf, "TANK_6", "level_pct"), step=lambda: lf.step(0.05))
        f.stop()
        wait_for(lambda: conn_status(lf)["status"] == "unavailable", step=lambda: lf.step(0.05))
        assert "reconnecting" in conn_status(lf)["detail"]
        assert all(q["quality"] == "comm_lost" for q in conn.quality.values())
        assert conn.read() == []
        n = len([r for r in lf.latest.values() if r.get("source") == "CONN_OPCUA"])
        last = {k: r["t"] for k, r in lf.latest.items() if r.get("source") == "CONN_OPCUA"}
        for _ in range(3):
            lf.step(0.5)
        assert {k: r["t"] for k, r in lf.latest.items() if r.get("source") == "CONN_OPCUA"} == last and n
        f2 = fake_opcua.start(port=f.port, workdir=certdir, password=f.password)
        wait_for(lambda: conn_status(lf)["status"] == "connected", timeout=20, step=lambda: lf.step(0.05))
        wait_for(lambda: lf.latest[("TANK_6", "level_pct")]["t"] > last[("TANK_6", "level_pct")],
                 step=lambda: lf.step(0.05))
        assert conn.quality[("TANK_6", "level_pct")]["quality"] == "good"
    finally:
        lf.registry.close()
        f.stop()
        if f2:
            f2.stop()


def test_close_returns_at_once(fake):
    conn = OpcUaConnector(spec_for(fake.endpoint), None, lambda: 0.0)
    wait_for(lambda: conn.status()["available"])
    t0 = time.monotonic()
    conn.close()
    assert time.monotonic() - t0 < 0.2
    wait_for(lambda: not conn._thread.is_alive(), timeout=5)
