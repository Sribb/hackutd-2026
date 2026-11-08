"""The DNP3 connector against the fake outstation (JSON lines on 127.0.0.1, free ports).

Every fake a test starts is stopped, and every connector is closed."""
import asyncio
import copy
import time

import pytest

from backend.app.connectors import KINDS
from backend.app.connectors.dnp3 import Dnp3Connector
from backend.app.live import LiveField
from backend.app.scenario import load_scenario
from backend.fakes import dnp3 as fake_dnp3

UNCERTAIN = {"ok": False, "uncertain": True,
             "detail": "sent; delivery not confirmed - check the field before retrying"}


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


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setitem(KINDS, "dnp3", Dnp3Connector)
    for var in ("JARVIS_CONFIRM", "JARVIS_MODE", "ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def fake():
    f = fake_dnp3.start()
    yield f
    f.stop()


def spec_for(port, **config):
    return {
        "id": "CONN_DNP3", "kind": "dnp3",
        "config": {"backend": "fake", "host": "127.0.0.1", "port": port, "integrity_s": 30, "event_s": 0.2,
                   "timeout_s": 1, "select_timeout_s": 1, "reconnect_initial_s": 0.2, "reconnect_max_s": 0.5,
                   **config},
        "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "AI:0"},
                    {"asset_id": "PUMP_6", "signal": "flow_bbl_hr", "address": "AI:1"},
                    {"asset_id": "PUMP_6", "signal": "running", "address": "BI:0"},
                    {"asset_id": "VALVE_D_6", "signal": "position", "address": "BI:1"}],
        "commands": [{"asset_id": "VALVE_D_6", "command": "SET_VALVE", "address": "CROB:0"},
                     {"asset_id": "PUMP_6", "command": "SET_PUMP", "address": "CROB:1"},
                     {"asset_id": "WELL_1", "command": "CHOKE_WELL", "address": "AO:0"}],
    }


def live_with(spec):
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    sc["connectors"] = [spec]
    return LiveField(sc)


def conn_status(lf):
    return next(c for c in lf.state()["connectors"] if c["id"] == "CONN_DNP3")


def reading_of(lf, aid, sig):
    r = lf.latest.get((aid, sig))
    return r and r.get("source") == "CONN_DNP3" and r


@pytest.fixture
def live(fake):
    lf = live_with(spec_for(fake.port))
    yield lf
    lf.registry.close()


@pytest.fixture
def conn(fake):
    c = Dnp3Connector(spec_for(fake.port), None, lambda: 0.0)
    wait_for(lambda: c.status()["available"])
    yield c
    c.close()


def values(conn, sig):
    return [r["value"] for r in conn.read() if r["signal"] == sig]


# ---------- readings ----------

def test_readings_flow_end_to_end(fake, live):
    tick = lambda: live.step(0.05)  # noqa: E731
    wait_for(lambda: conn_status(live)["status"] == "connected", step=tick)
    assert "integrity every 30 s" in conn_status(live)["detail"]
    wait_for(lambda: reading_of(live, "TANK_6", "level_pct") and reading_of(live, "VALVE_D_6", "position"),
             step=tick)
    a = live.state()["assets"]
    assert a["TANK_6"]["level_pct"] == 50.0 and a["TANK_6"]["status"] != "UNKNOWN"
    assert a["VALVE_D_6"]["position"] == "OPEN" and a["PUMP_6"]["status"] == "OK"
    assert fake.requests[:2] == ["unsolicited", "integrity"]
    # A change arrives (unsolicited is on by default), and event polls keep running.
    fake.set("AI:0", 61.5)
    wait_for(lambda: live.latest[("TANK_6", "level_pct")]["value"] == 61.5, step=tick)
    wait_for(lambda: fake.requests.count("events") >= 2)
    q = live.registry.get("CONN_DNP3").quality[("TANK_6", "level_pct")]
    assert q["quality"] == "good"


def test_event_poll_delivers_changes_when_unsolicited_is_off(fake):
    c = Dnp3Connector(spec_for(fake.port, unsolicited=False), None, lambda: 0.0)
    try:
        wait_for(lambda: c.status()["available"])
        assert "unsolicited off" in c.status()["detail"]
        fake.set("AI:1", 99.0)
        assert fake.events, "with unsolicited off the event waits for a poll"
        wait_for(lambda: 99.0 in values(c, "flow_bbl_hr"))
    finally:
        c.close()


def test_unsolicited_report_arrives_between_polls(fake):
    c = Dnp3Connector(spec_for(fake.port, event_s=30), None, lambda: 0.0)
    try:
        wait_for(lambda: c.status()["available"])
        n_polls = fake.requests.count("events")
        fake.set("BI:1", False)
        wait_for(lambda: "CLOSED" in values(c, "position"), timeout=2)
        assert fake.requests.count("events") == n_polls, "it came unsolicited, not from a poll"
    finally:
        c.close()


def test_held_value_repeats_while_polls_are_answered(conn):
    wait_for(lambda: 50.0 in values(conn, "level_pct"))
    time.sleep(0.5)                           # no change at the outstation
    assert values(conn, "level_pct") == [50.0]


@pytest.mark.parametrize("addr, sig, flags, online, quality", [
    ("AI:0", "level_pct", ("COMM_LOST",), True, "comm_lost"),
    ("AI:0", "level_pct", ("RESTART",), True, "bad"),
    ("AI:0", "level_pct", ("LOCAL_FORCED",), True, "uncertain"),
    ("AI:0", "level_pct", ("OVER_RANGE",), True, "out_of_range"),
    ("AI:0", "level_pct", ("REFERENCE_ERR",), True, "bad"),
    ("AI:0", "level_pct", (), False, "bad"),
    ("BI:1", "position", ("CHATTER_FILTER",), True, "uncertain"),
])
def test_bad_flags_give_no_value(fake, conn, addr, sig, flags, online, quality):
    asset = "TANK_6" if sig == "level_pct" else "VALVE_D_6"
    wait_for(lambda: conn.quality.get((asset, sig), {}).get("quality") == "good")
    fake.set_flags(addr, *flags, online=online)
    wait_for(lambda: conn.quality[(asset, sig)]["quality"] == quality)
    assert "DNP3 flags" in conn.quality[(asset, sig)]["detail"]
    conn.read()
    time.sleep(0.4)                           # several event polls
    assert values(conn, sig) == []
    fake.set_flags(addr)                      # healthy again
    wait_for(lambda: values(conn, sig))


def test_bad_flags_show_unknown_in_live_state(fake, live):
    tick = lambda: live.step(0.05)  # noqa: E731
    wait_for(lambda: reading_of(live, "TANK_6", "level_pct"), step=tick)
    fake.set_flags("AI:0", "COMM_LOST")
    c = live.registry.get("CONN_DNP3")
    wait_for(lambda: c.quality[("TANK_6", "level_pct")]["quality"] == "comm_lost", step=tick)
    last_t = live.latest[("TANK_6", "level_pct")]["t"]
    for _ in range(8):
        live.step(1.0)
        time.sleep(0.02)
    assert live.latest[("TANK_6", "level_pct")]["t"] == last_t, "never the last good value carried forward"
    tank = live.state()["assets"]["TANK_6"]
    assert tank["status"] == "UNKNOWN" and tank["stale"] is True
    assert live.latest[("VALVE_D_6", "position")]["t"] > last_t   # good points keep flowing


def test_old_buffered_event_is_never_a_reading(fake, conn):
    wait_for(lambda: 50.0 in values(conn, "level_pct"))
    fake.add_event("AI:0", 10.0, age_s=900)   # older than max_age_s (60)
    fake.add_event("AI:1", 5.0, age_s=-900)   # stamped in the future
    time.sleep(0.5)
    assert 10.0 not in values(conn, "level_pct") and 5.0 not in values(conn, "flow_bbl_hr")
    assert values(conn, "level_pct") == [50.0], "the newer value stays"


def test_old_event_with_nothing_held_marks_stale(fake):
    f = fake_dnp3.start(points={"BI:1": True})
    c = Dnp3Connector(spec_for(f.port, unsolicited=False), None, lambda: 0.0)
    try:
        wait_for(lambda: c.status()["available"])
        f.add_event("AI:0", 10.0, age_s=900)
        wait_for(lambda: c.quality.get(("TANK_6", "level_pct"), {}).get("quality") == "stale")
        assert values(c, "level_pct") == []
    finally:
        c.close()
        f.stop()


def test_event_older_than_the_last_event_is_skipped(fake, conn):
    wait_for(lambda: 50.0 in values(conn, "level_pct"))
    fake.add_event("AI:0", 70.0, age_s=1)
    wait_for(lambda: 70.0 in values(conn, "level_pct"))
    fake.add_event("AI:0", 20.0, age_s=10)    # happened before the 70
    time.sleep(0.4)
    assert values(conn, "level_pct") == [70.0]


# ---------- writes ----------

def test_writes_reach_the_fake_only_through_confirm(fake, live):
    wait_for(lambda: conn_status(live)["status"] == "connected", step=lambda: live.step(0.05))

    async def flow():
        p = await live.propose({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"},
                               reason="test", source="console")
        assert p["status"] == "PENDING" and p["connector_id"] == "CONN_DNP3"
        await asyncio.sleep(0.4)
        assert fake.selects == [] and fake.writes == [], "propose() must not touch the outstation"
        return await live.confirm(p["id"], by="operator")

    done = asyncio.run(flow())
    assert done["status"] == "CONFIRMED", done
    assert fake.selects == [{"kind": "CROB", "index": 0, "code": "LATCH_OFF"}]
    assert fake.writes == [{"kind": "CROB", "index": 0, "code": "LATCH_OFF"}]
    assert fake.direct_operates == [] and "direct_operate" not in fake.requests
    wait_for(lambda: live.latest.get(("VALVE_D_6", "position"), {}).get("value") == "CLOSED",
             step=lambda: live.step(0.05))
    time.sleep(0.4)
    assert len(fake.writes) == 1 and fake.requests.count("operate") == 1, "sent exactly once"


def test_pump_and_choke_commands(fake, conn):
    assert conn.send({"type": "SET_PUMP", "asset_id": "PUMP_6", "value": "OFF"})["ok"] is True
    assert conn.send({"type": "CHOKE_WELL", "asset_id": "WELL_1", "value": 0.25})["ok"] is True
    assert fake.writes == [{"kind": "CROB", "index": 1, "code": "LATCH_OFF"},
                           {"kind": "AO", "index": 0, "value": 0.25}]
    wait_for(lambda: False in values(conn, "running"))


def test_pulse_style_and_choke_scale(fake):
    c = Dnp3Connector(spec_for(fake.port, crob_style="pulse", choke_full_scale=100), None, lambda: 0.0)
    try:
        wait_for(lambda: c.status()["available"])
        assert c.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "OPEN"})["ok"] is True
        assert c.send({"type": "CHOKE_WELL", "asset_id": "WELL_1", "value": 0.5})["ok"] is True
        assert fake.writes == [{"kind": "CROB", "index": 0, "code": "PULSE_ON_CLOSE"},
                               {"kind": "AO", "index": 0, "value": 50.0}]
    finally:
        c.close()


@pytest.mark.parametrize("tamper", [{"index": 1}, {"code": "LATCH_ON"}, {"kind": "AO"}])
def test_select_echo_mismatch_operates_nothing(fake, conn, tamper):
    fake.select_tamper = tamper
    res = conn.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
    assert res["ok"] is False and "nothing was operated" in res["detail"], res
    assert "uncertain" not in res
    time.sleep(0.3)
    assert fake.writes == [] and "operate" not in fake.requests and len(fake.selects) == 1


def test_select_refused_operates_nothing(fake, conn):
    fake.select_status = 7                    # LOCAL: the device is in local control
    res = conn.send({"type": "SET_PUMP", "asset_id": "PUMP_6", "value": "OFF"})
    assert res["ok"] is False and "LOCAL" in res["detail"] and "nothing was operated" in res["detail"]
    assert "operate" not in fake.requests and fake.writes == []


def test_select_with_no_answer_operates_nothing(fake, conn):
    fake.silent = True
    t0 = time.monotonic()
    res = conn.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
    assert time.monotonic() - t0 < 2.5        # bounded by timeout_s (1), plus waiting for the channel
    assert res["ok"] is False and "nothing was operated" in res["detail"], res
    assert "operate" not in fake.requests and fake.writes == []


def test_late_operate_answer_is_uncertain_and_not_resent(fake, conn):
    fake.operate_delay_s = 2.0                # longer than what is left of timeout_s (1)
    t0 = time.monotonic()
    res = conn.send({"type": "SET_PUMP", "asset_id": "PUMP_6", "value": "OFF"})
    assert time.monotonic() - t0 < 1.8
    assert res == UNCERTAIN
    time.sleep(1.2)
    assert len(fake.writes) == 1 and fake.requests.count("operate") == 1, "never sent again"


def test_operate_echo_mismatch_is_not_ok(fake, conn):
    fake.operate_tamper = {"index": 7}
    res = conn.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
    assert res["ok"] is False and res.get("uncertain") is True and "check the field" in res["detail"]
    assert fake.requests.count("operate") == 1 and fake.requests.count("select") == 1


def test_operate_refused_is_not_ok(fake, conn):
    fake.operate_tamper = {"status": 6}       # HARDWARE_ERROR
    res = conn.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
    assert res["ok"] is False and "HARDWARE_ERROR" in res["detail"] and "nothing moved" in res["detail"]
    assert fake.requests.count("operate") == 1


def test_undeclared_command_and_no_link_send_nothing(fake):
    port = fake.port
    fake.stop()                               # nothing listens now
    c = Dnp3Connector(spec_for(port), None, lambda: 0.0)
    try:
        res = c.send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
        assert res["ok"] is False and "nothing was sent" in res["detail"]
        res = c.send({"type": "SET_VALVE", "asset_id": "VALVE_D_1", "value": "CLOSED"})
        assert res["ok"] is False and "no SET_VALVE point" in res["detail"]
        assert c.read() == []
    finally:
        c.close()


# ---------- config ----------

def test_non_freeboard_env_name_shows_unavailable(fake):
    lf = live_with(spec_for(fake.port, auth_key_env="DNP3_AUTH_KEY"))
    try:
        st = conn_status(lf)
        assert st["status"] == "unavailable" and "FREEBOARD_CONN_" in st["detail"], st
    finally:
        lf.registry.close()


def test_real_backend_is_unavailable_with_the_reason(fake):
    spec = spec_for(fake.port)
    del spec["config"]["backend"]
    lf = live_with(spec)
    try:
        st = conn_status(lf)
        assert st["status"] == "unavailable" and "dnp3-python" in st["detail"], st
        res = lf.registry.get("CONN_DNP3").send({"type": "SET_VALVE", "asset_id": "VALVE_D_6", "value": "CLOSED"})
        assert res["ok"] is False
        assert fake.requests == []
    finally:
        lf.registry.close()


@pytest.mark.parametrize("change, words", [
    ({"signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "BI:0"}]}, "AI point"),
    ({"signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "40001"}]}, "AI:3"),
    ({"commands": [{"asset_id": "WELL_1", "command": "CHOKE_WELL", "address": "CROB:2"}]}, "AO point"),
    ({"commands": [{"asset_id": "TRUCK_1", "command": "SEND_TRUCK", "address": "CROB:2"}]}, "writes"),
    ({"config": {"backend": "fake", "host": "10.0.0.5", "port": 20000}}, "127.0.0.1"),
    ({"config": {"backend": "fake", "host": "127.0.0.1", "port": 20000, "event_s": 0}}, "event_s"),
])
def test_bad_config_is_refused(change, words):
    lf = live_with({**spec_for(20000), **change})
    try:
        st = conn_status(lf)
        assert st["status"] == "unavailable" and words in st["detail"], st
    finally:
        lf.registry.close()


# ---------- link loss ----------

def test_silent_outstation_is_comm_lost_then_recovers(fake, conn):
    wait_for(lambda: values(conn, "level_pct"))
    fake.silent = True
    wait_for(lambda: not conn.status()["available"], timeout=5)
    st = conn.status()
    assert "no answer" in st["detail"] and "reconnecting" in st["detail"]
    assert all(q["quality"] == "comm_lost" for q in conn.quality.values()) and len(conn.quality) == 4
    assert conn.read() == []
    fake.silent = False
    wait_for(lambda: conn.status()["available"], timeout=8)
    wait_for(lambda: values(conn, "level_pct"))
    assert conn.quality[("TANK_6", "level_pct")]["quality"] == "good"


def test_stop_and_restart_reconnects():
    f = fake_dnp3.start()
    lf = live_with(spec_for(f.port))
    c = lf.registry.get("CONN_DNP3")
    f2 = None
    tick = lambda: lf.step(0.05)  # noqa: E731
    try:
        wait_for(lambda: reading_of(lf, "TANK_6", "level_pct"), step=tick)
        f.stop()
        wait_for(lambda: conn_status(lf)["status"] == "unavailable", step=tick)
        assert "reconnecting" in conn_status(lf)["detail"]
        assert all(q["quality"] == "comm_lost" for q in c.quality.values())
        last = {k: r["t"] for k, r in lf.latest.items() if r.get("source") == "CONN_DNP3"}
        for _ in range(3):
            lf.step(0.5)
        assert {k: r["t"] for k, r in lf.latest.items() if r.get("source") == "CONN_DNP3"} == last
        f2 = fake_dnp3.start(port=f.port)
        f2.set("AI:0", 44.0, event=False)
        wait_for(lambda: conn_status(lf)["status"] == "connected", step=tick)
        wait_for(lambda: lf.latest[("TANK_6", "level_pct")]["value"] == 44.0, step=tick)
        assert lf.latest[("TANK_6", "level_pct")]["t"] > last[("TANK_6", "level_pct")]
    finally:
        lf.registry.close()
        f.stop()
        if f2:
            f2.stop()


def test_close_returns_at_once(fake):
    c = Dnp3Connector(spec_for(fake.port), None, lambda: 0.0)
    wait_for(lambda: c.status()["available"])
    t0 = time.monotonic()
    c.close()
    assert time.monotonic() - t0 < 0.2
    assert c.read() == [] and c.status()["available"] is False
    wait_for(lambda: not c._thread.is_alive(), timeout=3)
