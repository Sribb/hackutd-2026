"""The PI Web API connector against the fake server, plus the history backfill (CF-14).

The fake binds 127.0.0.1 on a free port and is always stopped. Live time is driven by
hand with live.step()."""
import asyncio
import copy
import socket
import time
from collections import deque

import pytest

from backend.app import history
from backend.app.connectors import KINDS
from backend.app.connectors.piwebapi import PiWebApiConnector, parse_pi_time
from backend.app.live import LiveField
from backend.app.scenario import load_scenario
from backend.fakes import piwebapi as fake_pi

PW = "FREEBOARD_CONN_PI_PASSWORD"
CID = "CONN_PI"


def wait_for(cond, timeout=8.0, every=0.05, step=None):
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
def fake():
    f = fake_pi.start()
    yield f
    f.stop()


@pytest.fixture(autouse=True)
def env(monkeypatch, fake):
    monkeypatch.setitem(KINDS, "piwebapi", PiWebApiConnector)
    monkeypatch.setenv(PW, fake.password)
    for var in ("JARVIS_CONFIRM", "JARVIS_MODE", "ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


def spec_for(base_url, commands=False, **config):
    spec = {
        "id": CID, "kind": "piwebapi",
        "config": {"base_url": base_url, "auth": "basic", "username": "freeboard", "password_env": PW,
                   "poll_ms": 100, "timeout_s": 2, **config},
        "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": fake_pi.TANK_6_LEVEL},
                    {"asset_id": "PUMP_6", "signal": "running", "address": fake_pi.PUMP_6_RUNNING},
                    {"asset_id": "PUMP_6", "signal": "flow_bbl_hr", "address": fake_pi.PUMP_6_FLOW},
                    {"asset_id": "VALVE_D_6", "signal": "position", "address": fake_pi.VALVE_D_6_POSITION}],
    }
    if commands:
        spec["commands"] = [{"asset_id": "VALVE_D_6", "command": "SET_VALVE", "address": "F1DPfbVALVED6CMD"}]
    return spec


def live_with(spec):
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    sc["connectors"] = [spec]
    return LiveField(sc)


def conn_status(lf):
    return next(c for c in lf.state()["connectors"] if c["id"] == CID)


def reading_of(lf, aid, sig):
    r = lf.latest.get((aid, sig))
    return r and r.get("source") == CID and r


@pytest.fixture
def live(fake):
    lf = live_with(spec_for(fake.base_url))
    yield lf
    lf.registry.close()
    for w in (fake_pi.TANK_6_LEVEL, fake_pi.PUMP_6_FLOW):
        fake.set_flags(w)
        fake.set_age(w, None)
    fake.set(fake_pi.TANK_6_LEVEL, 52.0)


# ---------- readings ----------

def test_readings_flow_end_to_end(fake, live):
    fake.set(fake_pi.TANK_6_LEVEL, 61.5)
    r = wait_for(lambda: (r := reading_of(live, "TANK_6", "level_pct")) and r["value"] == 61.5 and r,
                 step=lambda: live.step(0.05))
    assert r["value"] == 61.5
    wait_for(lambda: reading_of(live, "PUMP_6", "running") and reading_of(live, "VALVE_D_6", "position"),
             step=lambda: live.step(0.05))
    a = live.state()["assets"]
    assert a["TANK_6"]["status"] != "UNKNOWN" and a["TANK_6"]["level_pct"] == 61.5
    assert a["VALVE_D_6"]["position"] == "OPEN" and a["PUMP_6"]["status"] == "OK"
    conn = live.registry.get(CID)
    assert conn.quality[("TANK_6", "level_pct")]["quality"] == "good"
    s = conn_status(live)
    assert s["status"] == "connected" and "read only" in s["detail"]
    assert fake.password not in s["detail"]


@pytest.mark.parametrize("change, quality, word", [
    (lambda f, w: f.set_flags(w, good=False), "bad", "not Good"),
    (lambda f, w: f.set_flags(w, questionable=True), "uncertain", "Questionable"),
    (lambda f, w: f.set_flags(w, substituted=True), "uncertain", "Substituted"),
    (lambda f, w: f.set_digital(w, "Bad Input"), "bad", "Bad Input"),
    (lambda f, w: f.set_age(w, 900), "stale", "ago"),
    (lambda f, w: f.set(w, "NaN"), "bad", "finite"),
])
def test_untrusted_values_give_no_reading(fake, live, change, quality, word):
    w = fake_pi.TANK_6_LEVEL
    wait_for(lambda: reading_of(live, "TANK_6", "level_pct"), step=lambda: live.step(0.05))
    conn = live.registry.get(CID)
    change(fake, w)
    q = wait_for(lambda: (q := conn.quality[("TANK_6", "level_pct")])["quality"] != "good" and q)
    assert q["quality"] == quality and word in q["detail"]
    live.step(0.05)                         # drain anything that arrived before the change
    before = live.latest[("TANK_6", "level_pct")]["t"]
    for _ in range(6):                      # past stale_after_min (5): no good value ever arrives
        time.sleep(0.03)
        live.step(1.0)
    assert live.latest[("TANK_6", "level_pct")]["t"] == before
    a = live.state()["assets"]["TANK_6"]
    assert a["status"] == "UNKNOWN" and a["stale"]
    assert live.live_scenario()["initial_levels_pct"]["TANK_6"] is None   # the forecast never sees a number


def test_bad_quality_tank_shows_unknown_in_forecast_input(fake, live):
    wait_for(lambda: reading_of(live, "TANK_6", "level_pct"), step=lambda: live.step(0.05))
    fake.set_flags(fake_pi.TANK_6_LEVEL, good=False)
    conn = live.registry.get(CID)
    wait_for(lambda: conn.quality[("TANK_6", "level_pct")]["quality"] == "bad")
    live.step(0.05)
    for _ in range(6):
        live.step(1.0)
    assert live.live_scenario()["initial_levels_pct"]["TANK_6"] is None


# ---------- commands ----------

def test_writes_never_reach_pi_even_after_confirm(fake):
    lf = live_with(spec_for(fake.base_url, commands=True))
    try:
        writes_before = len(fake.writes)
        p = asyncio.run(lf.propose({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"},
                                   reason="test", source="console"))
        assert p["status"] == "PENDING" and len(fake.writes) == writes_before

        async def confirm():
            return await lf.confirm(p["id"], by="operator")

        done = asyncio.run(confirm())
        assert done["status"] == "FAILED" and "read only" in done["result"]
        assert done["connector_id"] == CID
        assert len(fake.writes) == writes_before
        assert "VALVE_D_6" not in lf.commanded
    finally:
        lf.registry.close()


# ---------- config ----------

@pytest.mark.parametrize("config, words", [
    ({"password_env": "PI_PASSWORD"}, "FREEBOARD_CONN_"),
    ({"base_url": "http://pi.example.com/piwebapi"}, "https"),
    ({"base_url": "https://user:pw@pi.example.com/piwebapi"}, "user name or password"),
    ({"auth": "basic", "username": ""}, "username"),
    ({"auth": "kerberos"}, "auth"),
    ({"poll_ms": 0}, "poll_ms"),
])
def test_bad_config_shows_unavailable(fake, config, words):
    config = {"base_url": fake.base_url, **config}
    lf = live_with(spec_for(config.pop("base_url"), **config))
    try:
        s = conn_status(lf)
        assert s["status"] == "unavailable" and s["detail"].startswith("bad config") and words in s["detail"]
        assert "pw@" not in s["detail"] and fake.password not in s["detail"]
    finally:
        lf.registry.close()


def test_missing_address_is_bad_config(fake):
    spec = spec_for(fake.base_url)
    spec["signals"][0].pop("address")
    lf = live_with(spec)
    try:
        assert "WebId" in conn_status(lf)["detail"]
    finally:
        lf.registry.close()


def test_wrong_password_is_unavailable_without_leaking(fake, monkeypatch):
    monkeypatch.setenv(PW, "not-the-password")
    lf = live_with(spec_for(fake.base_url))
    try:
        s = wait_for(lambda: (s := conn_status(lf))["detail"] and "401" in s["detail"] and s)
        assert s["status"] == "unavailable" and "not-the-password" not in s["detail"]
        lf.step(0.1)
        assert not reading_of(lf, "TANK_6", "level_pct")
        assert lf.registry.get(CID).quality[("TANK_6", "level_pct")]["quality"] == "comm_lost"
    finally:
        lf.registry.close()


def test_unset_secret_names_the_variable_only(fake, monkeypatch):
    monkeypatch.delenv(PW)
    lf = live_with(spec_for(fake.base_url))
    try:
        s = wait_for(lambda: (s := conn_status(lf))["detail"] and PW in s["detail"] and s)
        assert s["status"] == "unavailable"
    finally:
        lf.registry.close()


# ---------- link loss ----------

def test_stop_and_restart_reconnects(monkeypatch):
    f = fake_pi.start()
    port = f.port
    lf = None
    monkeypatch.setenv(PW, f.password)
    try:
        lf = live_with(spec_for(f.base_url))
        wait_for(lambda: reading_of(lf, "TANK_6", "level_pct"), step=lambda: lf.step(0.05))
        conn = lf.registry.get(CID)
        f.stop()
        wait_for(lambda: conn_status(lf)["status"] == "unavailable")
        assert all(q["quality"] == "comm_lost" for q in conn.quality.values())
        lf.step(0.05)
        t_last = lf.latest[("TANK_6", "level_pct")]["t"]
        time.sleep(0.3)
        lf.step(0.05)
        assert lf.latest[("TANK_6", "level_pct")]["t"] == t_last
        f = fake_pi.start(port=port, password=f.password)
        f.set(fake_pi.TANK_6_LEVEL, 47.0)
        wait_for(lambda: conn_status(lf)["status"] == "connected", timeout=15)
        r = wait_for(lambda: (r := reading_of(lf, "TANK_6", "level_pct")) and r["t"] > t_last and r,
                     step=lambda: lf.step(0.05))
        assert r["value"] == 47.0
    finally:
        if lf is not None:
            lf.registry.close()
        f.stop()


def test_close_returns_at_once_during_a_hung_request(monkeypatch):
    hang = socket.socket()
    hang.bind(("127.0.0.1", 0))
    hang.listen(5)          # accepts the connection, never answers
    try:
        port = hang.getsockname()[1]
        lf = live_with(spec_for(f"http://127.0.0.1:{port}/piwebapi", timeout_s=5))
        time.sleep(0.2)     # the poll is now waiting on the socket
        t0 = time.monotonic()
        lf.registry.close()
        assert time.monotonic() - t0 < 0.5
        assert conn_status(lf)["status"] == "unavailable"
    finally:
        hang.close()


def test_send_refuses_directly(live):
    res = live.registry.get(CID).send({"type": "SET_PUMP", "asset_id": "PUMP_6", "value": "OFF"})
    assert res["ok"] is False and "read only" in res["detail"]


# ---------- backfill ----------

def test_backfill_maps_the_archive_to_live_minutes(fake, live):
    fake.set_recorded(fake_pi.TANK_6_LEVEL, [(3000, 40.0), (1800, 45.0), (600, 50.0), (60, 51.0)])
    for _ in range(20):
        live.step(1.0)
    conn = live.registry.get(CID)
    rows = {(a, s): pts for a, s, pts in conn.backfill(60)}
    pts = rows[("TANK_6", "level_pct")]
    assert [v for _, v in pts] == [40.0, 45.0, 50.0, 51.0]
    now_t = live.t
    for (t, _), age in zip(pts, (3000, 1800, 600, 60)):
        assert abs(t - (now_t - age / 60.0)) < 0.1
    assert rows[("PUMP_6", "running")] == []        # no archive for it in the fake
    assert conn.last_backfill["failed"] == {}
    # asked for 11 minutes, only the last two values are that young
    short = {(a, s): p for a, s, p in conn.backfill(11)}[("TANK_6", "level_pct")]
    assert [v for _, v in short] == [50.0, 51.0]


def test_backfill_drops_untrusted_archive_values(fake, live):
    fake.set_recorded(fake_pi.TANK_6_LEVEL, [(900, 40.0), (600, {"Name": "I/O Timeout", "IsSystem": True}),
                                            (300, 9999.0)])
    spec = spec_for(fake.base_url, signal_map={"TANK_6.level_pct": {"range": [0, 100]}})
    lf = live_with(spec)
    try:
        pts = {(a, s): p for a, s, p in lf.registry.get(CID).backfill(30)}[("TANK_6", "level_pct")]
        assert [v for _, v in pts] == [40.0]
    finally:
        lf.registry.close()


def test_backfill_into_the_recorder(fake, live):
    fake.set_recorded(fake_pi.TANK_6_LEVEL, [(1200, 30.0), (600, 31.0)])
    for _ in range(30):
        live.step(1.0)
    wait_for(lambda: reading_of(live, "TANK_6", "level_pct"), step=lambda: live.step(0.05))
    rec = history.Recorder()
    rec.sync(live)
    recorded = rec.readings(("TANK_6", "level_pct"))
    assert recorded
    for aid, sig, pts in live.registry.get(CID).backfill(60):
        rec.backfill((aid, sig), pts)
    merged = rec.readings(("TANK_6", "level_pct"))
    assert merged[-len(recorded):] == recorded
    assert [v for _, v in merged[:2]] == [30.0, 31.0]
    assert [t for t, _ in merged] == sorted(t for t, _ in merged)


# ---------- Recorder.backfill ----------

def _recorder_with(series):
    rec = history.Recorder(retention_min=100)
    rec.started_t = 0.0
    rec._series[("TANK_1", "level_pct")] = deque(series)
    return rec


def test_recorder_backfill_merges_older_only_without_duplicates():
    rec = _recorder_with([(10.0, 50.0), (11.0, 51.0)])
    key = ("TANK_1", "level_pct")
    added = rec.backfill(key, [(8.0, 48.0), (9.0, 49.0), (10.0, 99.0), (12.0, 99.0), (9.0, 1.0)])
    assert added == 2
    assert rec.readings(key) == [(8.0, 48.0), (9.0, 49.0), (10.0, 50.0), (11.0, 51.0)]
    assert rec.backfill(key, [(8.0, 48.0), (9.0, 49.0)]) == 0
    assert rec.readings(key) == [(8.0, 48.0), (9.0, 49.0), (10.0, 50.0), (11.0, 51.0)]


def test_recorder_backfill_drops_gaps_junk_and_old():
    rec = _recorder_with([(10.0, 50.0)])
    key = ("TANK_1", "level_pct")
    added = rec.backfill(key, [(-200.0, 1.0), (5.0, None), (6.0, float("nan")), ("x", 2.0), (7.0, 47.0), None])
    assert added == 1 and rec.readings(key) == [(7.0, 47.0), (10.0, 50.0)]


def test_recorder_backfill_new_key_and_before_sync():
    rec = _recorder_with([])
    assert rec.backfill(("WELL_1", "flow_bbl_hr"), [(2.0, 5.0), (1.0, 4.0)]) == 2
    assert rec.readings(("WELL_1", "flow_bbl_hr")) == [(1.0, 4.0), (2.0, 5.0)]
    fresh = history.Recorder()
    assert fresh.backfill(("TANK_1", "level_pct"), [(1.0, 2.0)]) == 0


def test_parse_pi_time():
    assert parse_pi_time("2026-11-07T08:00:00Z") == parse_pi_time("2026-11-07T08:00:00.0000000Z")
    assert abs(parse_pi_time("2026-11-07T08:00:00.1234567Z") - parse_pi_time("2026-11-07T08:00:00Z") - 0.123456) < 1e-6
    assert parse_pi_time("2026-11-07T02:00:00-06:00") == parse_pi_time("2026-11-07T08:00:00Z")
    assert parse_pi_time("garbage") is None and parse_pi_time(None) is None


def test_bearer_token_is_sent_but_never_shown(fake, monkeypatch):
    monkeypatch.setenv("FREEBOARD_CONN_PI_TOKEN", "tok-secret-123")
    spec = spec_for(fake.base_url)
    spec["config"] = {"base_url": fake.base_url, "auth": "bearer", "token_env": "FREEBOARD_CONN_PI_TOKEN",
                      "poll_ms": 100, "timeout_s": 2}
    lf = live_with(spec)
    try:
        s = wait_for(lambda: (s := conn_status(lf))["detail"] and "401" in s["detail"] and s)
        assert s["status"] == "unavailable" and "tok-secret-123" not in str(lf.state())
    finally:
        lf.registry.close()
