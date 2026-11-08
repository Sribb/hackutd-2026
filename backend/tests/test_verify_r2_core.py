"""Verification round 2 (#126 core): webhook values go through signalmap, the simulator never
vouches for a field asset, a broken well meter is not a shut-in well, the history refuses
impossible levels, a frozen OPC UA tag goes stale on time, no token reaches the server log,
numeric host spellings can't reach cloud metadata, and the demo fakes follow the backend's
live clock."""
import copy
import datetime as dt
import json
import logging
import math
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace as NS

import pytest

import backend.app.main  # noqa: F401 - installs the log filter, registers opcua and sparkplug
from backend.app import history, log_redact, now
from backend.app.connectors import KINDS, hosts
from backend.app.connectors import signalmap as sm
from backend.app.connectors.base import Connector
from backend.app.connectors.opcua_ua import OpcUaConnector, _short
from backend.app.live import LiveField
from backend.app.scenario import load_scenario


class Scripted(Connector):
    """A field connector the test drives, with a quality dict like the #126 connectors.
    It bypasses signalmap, so it can hand the live layer anything."""

    kind = "scripted"

    def __init__(self, spec, fmap, now_fn):
        super().__init__(spec, fmap, now_fn)
        self.values = {key: 51.7 for key in self.signals}
        self.good = True
        self.quality = {}

    def read(self):
        if not self.good:
            return []
        out = []
        for (aid, sig), v in self.values.items():
            self.quality[(aid, sig)] = {"quality": "good", "detail": "", "t": round(self.now(), 3)}
            out.append(self.reading(aid, sig, v))
        return out

    def drop(self):
        self.good = False
        for key in self.signals:
            self.quality[key] = {"quality": "comm_lost", "detail": "link down", "t": round(self.now(), 3)}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for var in ("FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN", "JARVIS_CONFIRM", "NTFY_TOPIC",
                "FREEBOARD_CONN_ALLOWED_HOSTS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FREEBOARD_ONE_CLOCK", "1")
    monkeypatch.setitem(KINDS, "scripted", Scripted)


def live_with(*specs):
    sc = copy.deepcopy(load_scenario())
    sc["connectors"] = [copy.deepcopy(s) for s in specs]
    lf = LiveField(sc)
    lf.step(1.0)
    return lf, NS(state=NS(live=lf))


def conn(lf, cid):
    return next(c for c in lf.registry.connectors if c.id == cid)


def hook(*signals):
    return {"id": "CONN_HOOK", "kind": "webhook", "config": {},
            "signals": [{"asset_id": a, "signal": s} for a, s in signals]}


# ---------- webhook values go through signalmap ----------

def test_a_numeric_string_level_is_a_number_and_every_tick_still_runs():
    lf, app_ = live_with(hook(("TANK_5", "level_pct")))
    try:
        res = conn(lf, "CONN_HOOK").push([{"asset_id": "TANK_5", "signal": "level_pct", "value": "61.0"}])
        assert res["accepted"] == ["TANK_5.level_pct"], res
        for _ in range(12):                   # past the 10-minute rate window's start
            lf.step(1.0)                       # raised TypeError in slope_bbl_hr before round 2
            conn(lf, "CONN_HOOK").push([{"asset_id": "TANK_5", "signal": "level_pct", "value": 61.5}])
        assert all(isinstance(v, float) for _, v in lf.history[("TANK_5", "level_pct")])
        assert now.state(app_)["assets"]["TANK_5"]["status"] != "UNKNOWN"
    finally:
        lf.registry.close()


@pytest.mark.parametrize("raw", ["abc", float("nan"), float("inf"), True, None])
def test_a_value_that_is_not_a_number_is_rejected_and_unknown_at_once(raw):
    lf, app_ = live_with(hook(("TANK_5", "level_pct")))
    try:
        c = conn(lf, "CONN_HOOK")
        c.push([{"asset_id": "TANK_5", "signal": "level_pct", "value": 61.0}])
        lf.step(0.5)
        res = c.push([{"asset_id": "TANK_5", "signal": "level_pct", "value": raw}])
        assert res["accepted"] == [] and len(res["rejected"]) == 1, res
        json.dumps(res, allow_nan=False)        # the answer is valid JSON even for NaN
        lf.step(0.5)
        a = now.state(app_)["assets"]["TANK_5"]
        assert a["status"] == "UNKNOWN" and a["level_pct"] is None, a
        assert "CONN_HOOK reports level_pct" in a["reason"]
        assert lf.value("TANK_5", "level_pct") == 61.0     # the live layer never saw the bad value
    finally:
        lf.registry.close()


@pytest.mark.parametrize("word", ["TRIPPED", "off", "Stopped", "0", 0, False])
def test_a_tripped_pump_word_counts_as_stopped_and_raises_pump_trip(word):
    lf, app_ = live_with(hook(("PUMP_5", "running"), ("PUMP_5", "flow_bbl_hr")))
    try:
        c = conn(lf, "CONN_HOOK")
        res = c.push([{"asset_id": "PUMP_5", "signal": "running", "value": word},
                      {"asset_id": "PUMP_5", "signal": "flow_bbl_hr", "value": "202"}])
        assert len(res["accepted"]) == 2, res
        lf.step(0.5)
        assert lf.value("PUMP_5", "running") is False and lf.value("PUMP_5", "flow_bbl_hr") == 202.0
        assert now.pump_running(lf, {"id": "PUMP_5", "rate_bbl_hr": 220}) is False
        for _ in range(4):                       # the detector confirms over a few readings
            c.push([{"asset_id": "PUMP_5", "signal": "running", "value": word}])
            lf.step(0.5)
        kinds = {(a["asset_id"], a["kind"]) for a in lf.detector.active.values()}
        assert ("PUMP_5", "PUMP_TRIP") in kinds, kinds
        assert now.state(app_)["assets"]["PUMP_5"]["status"] == "ALARM"
    finally:
        lf.registry.close()


def test_an_unknown_run_word_is_rejected():
    lf, _ = live_with(hook(("PUMP_5", "running")))
    try:
        res = conn(lf, "CONN_HOOK").push([{"asset_id": "PUMP_5", "signal": "running", "value": "MAYBE"}])
        assert res["accepted"] == [] and "not an on/off value" in res["rejected"][0]["error"]
        assert conn(lf, "CONN_HOOK").quality[("PUMP_5", "running")]["quality"] == "bad"
    finally:
        lf.registry.close()


# ---------- the simulator never vouches for a field asset ----------

def test_a_simulated_flow_never_stands_in_for_a_field_pump_run_signal():
    spec = {"id": "CONN_SCRIPT", "kind": "scripted",
            "signals": [{"asset_id": "PUMP_5", "signal": "running", "address": "a"}]}
    lf, app_ = live_with(spec)
    try:
        c = conn(lf, "CONN_SCRIPT")
        c.values[("PUMP_5", "running")] = True
        lf.step(0.5)
        assert lf.latest[("PUMP_5", "flow_bbl_hr")]["source"] == lf.registry.sim.id   # the sim keeps the flow
        pump = {"id": "PUMP_5", "rate_bbl_hr": 220, "tank_id": "TANK_5"}
        assert now.pump_running(lf, pump) is True
        c.drop()
        lf.step(0.5)
        assert now.pump_running(lf, pump) is None, "the simulated flow vouched for the real pump"
        n = now.get(app_)
        assert {"type": "SET_PUMP", "asset_id": "PUMP_5", "value": "OFF", "at_min": 0.0} in n.actions
        assert any("Pump 5" in w for w in n.scenario["live_assumptions"]), n.scenario["live_assumptions"]
        assert now.state(app_)["assets"]["PUMP_5"]["status"] == "UNKNOWN"
        assert now.pump_running(lf, {"id": "PUMP_1", "rate_bbl_hr": 220}) is True   # sim-only pumps unchanged
    finally:
        lf.registry.close()


# ---------- a broken well meter is not a shut-in well ----------

@pytest.mark.parametrize("raw", [-40.0, float("nan"), float("inf")])
def test_a_broken_well_flow_keeps_the_design_rate_with_a_warning(raw):
    spec = {"id": "CONN_SCRIPT", "kind": "scripted",
            "signals": [{"asset_id": "WELL_9", "signal": "flow_bbl_hr", "address": "a"}]}
    lf, app_ = live_with(spec)
    try:
        conn(lf, "CONN_SCRIPT").values[("WELL_9", "flow_bbl_hr")] = raw
        lf.step(0.5)
        sc = now.get(app_).scenario
        well = next(w for w in sc["field"]["wells"] if w["id"] == "WELL_9")
        assert well["water_bbl_hr"] == 50, well
        assert any("Well 9" in w and "design rate" in w for w in sc["live_assumptions"]), sc["live_assumptions"]
    finally:
        lf.registry.close()


def test_a_small_negative_flow_is_noise_and_reads_as_zero():
    spec = {"id": "CONN_SCRIPT", "kind": "scripted",
            "signals": [{"asset_id": "WELL_9", "signal": "flow_bbl_hr", "address": "a"}]}
    lf, app_ = live_with(spec)
    try:
        conn(lf, "CONN_SCRIPT").values[("WELL_9", "flow_bbl_hr")] = -0.4
        lf.step(0.5)
        sc = now.get(app_).scenario
        assert next(w for w in sc["field"]["wells"] if w["id"] == "WELL_9")["water_bbl_hr"] == 0.0
        assert not any("Well 9" in w for w in sc["live_assumptions"])
    finally:
        lf.registry.close()


def test_signalmap_gives_a_backwards_flow_out_of_range():
    smap = sm.SignalMap({"signals": [{"asset_id": "WELL_9", "signal": "flow_bbl_hr"}]})
    assert smap.apply("WELL_9", "flow_bbl_hr", -40.0).quality == sm.OUT_OF_RANGE
    assert smap.apply("WELL_9", "flow_bbl_hr", -0.5).value == -0.5
    ranged = sm.SignalMap({"signals": [{"asset_id": "WELL_9", "signal": "flow_bbl_hr"}],
                           "config": {"signal_map": {"WELL_9.flow_bbl_hr": {"range": [-100, 100]}}}})
    assert ranged.apply("WELL_9", "flow_bbl_hr", -40.0).value == -40.0   # an explicit range wins


# ---------- /api/history refuses what /api/state refuses ----------

@pytest.mark.parametrize("raw", [-20.0, 130.0, float("nan")])
def test_history_records_an_impossible_level_as_null(raw):
    spec = {"id": "CONN_SCRIPT", "kind": "scripted",
            "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "a"}]}
    lf, app_ = live_with(spec)
    try:
        rec = history.Recorder()
        rec.sync(lf)
        conn(lf, "CONN_SCRIPT").values[("TANK_6", "level_pct")] = raw
        lf.step(0.5)
        rec.sync(lf)
        last = rec.readings(("TANK_6", "level_pct"))[-1]
        assert last[1] is None, last
        assert now.state(app_)["assets"]["TANK_6"]["level_pct"] is None
        conn(lf, "CONN_SCRIPT").values[("TANK_6", "level_pct")] = 100.3      # inside the slack: clamped
        lf.step(0.5)
        rec.sync(lf)
        assert rec.readings(("TANK_6", "level_pct"))[-1][1] == 100.0
    finally:
        lf.registry.close()


# ---------- a frozen OPC UA tag goes stale at stale_after_min ----------

def test_a_frozen_opcua_tag_gets_sensor_stale_at_stale_after_min(monkeypatch):
    from backend.app.connectors import opcua_ua
    monkeypatch.setattr(opcua_ua.sm, "import_optional", lambda module, package: (None, "asyncua is off"))
    spec = {"id": "CONN_OPCUA", "kind": "opcua",
            "config": {"endpoint": "opc.tcp://127.0.0.1:1/x", "cert_path_env": "FREEBOARD_CONN_C",
                       "key_path_env": "FREEBOARD_CONN_K"},
            "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "ns=2;s=TANK_6.Level"}]}
    lf, app_ = live_with(spec)
    try:
        c = conn(lf, "CONN_OPCUA")
        c._client = object()                     # pretend a session is up
        stamp = dt.datetime.now(dt.timezone.utc)
        c.notify("ns=2;s=TANK_6.Level", NS(StatusCode=None, Value=NS(Value=48.0), SourceTimestamp=stamp,
                                           ServerTimestamp=stamp))
        lf.step(0.05)
        t0 = lf.latest[("TANK_6", "level_pct")]["t"]
        seen = {}
        while lf.t < t0 + 7.0:
            lf.step(0.25)
            if lf.detector.is_stale("TANK_6", "level_pct"):
                seen.setdefault("live stale", lf.t - t0)
            if any(a["asset_id"] == "TANK_6" and a["kind"] == "SENSOR_STALE" for a in lf.detector.active.values()):
                seen.setdefault("SENSOR_STALE", lf.t - t0)
            if now.state(app_)["assets"]["TANK_6"]["status"] == "UNKNOWN":
                seen.setdefault("/api/state UNKNOWN", lf.t - t0)
        assert set(seen) == {"live stale", "SENSOR_STALE", "/api/state UNKNOWN"}, seen
        assert all(5.0 <= v <= 5.6 for v in seen.values()), seen
        assert lf.latest[("TANK_6", "level_pct")]["t"] == t0      # never re-read with a fresh t
    finally:
        lf.registry.close()


# ---------- OPC UA: secrets and pins ----------

@pytest.mark.parametrize("path", [r"C:\ops\keys\client_key.pem", "C:/ops/keys/client_key.pem",
                                  "/etc/freeboard/client_key.pem"])
def test_a_key_path_from_the_environment_never_shows_in_an_error(monkeypatch, path):
    monkeypatch.setenv("FREEBOARD_CONN_K", path)
    config = {"key_path_env": "FREEBOARD_CONN_K"}
    for e in (FileNotFoundError(2, "No such file or directory", path), OSError(f"can't open {path}"),
              RuntimeError(repr(path))):
        text = _short(e, config)
        assert "client_key" not in text and "$FREEBOARD_CONN_K" in text, text


def test_an_unpinned_remote_opcua_endpoint_is_refused(monkeypatch):
    from backend.app.connectors import opcua_ua
    monkeypatch.setattr(opcua_ua.sm, "import_optional", lambda module, package: (None, "asyncua is off"))
    monkeypatch.setenv("FREEBOARD_CONN_ALLOWED_HOSTS", "10.0.0.5")
    spec = {"id": "CONN_OPCUA", "kind": "opcua",
            "config": {"endpoint": "opc.tcp://10.0.0.5:4840", "cert_path_env": "FREEBOARD_CONN_C",
                       "key_path_env": "FREEBOARD_CONN_K"},
            "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "ns=2;s=TANK_6.Level"}]}
    with pytest.raises(ValueError, match="server_cert_path_env"):
        OpcUaConnector(copy.deepcopy(spec), None, lambda: 0.0)
    pinned = copy.deepcopy(spec)
    pinned["config"]["server_cert_path_env"] = "FREEBOARD_CONN_S"
    OpcUaConnector(pinned, None, lambda: 0.0)
    allowed = copy.deepcopy(spec)
    allowed["config"]["allow_unpinned_server_cert"] = True
    OpcUaConnector(allowed, None, lambda: 0.0)
    monkeypatch.delenv("FREEBOARD_CONN_ALLOWED_HOSTS")
    local = copy.deepcopy(spec)
    local["config"]["endpoint"] = "opc.tcp://127.0.0.1:4840"
    OpcUaConnector(local, None, lambda: 0.0)                 # the demo fakes on this machine


# ---------- hosts: every spelling of a metadata address is refused ----------

@pytest.mark.parametrize("host", ["169.254.169.254", "2852039166", "0xa9fea9fe", "0251.0376.0251.0376",
                                  "169.254.43518", "0xa9.0xfe.0xa9.0xfe", "169.254.169.254.", "fd00:ec2::254",
                                  "[fd00:ec2::254]", "::ffff:169.254.169.254", "::a9fe:a9fe", "64:ff9b::a9fe:a9fe",
                                  "2002:a9fe:a9fe::1", "100.100.100.200", "168.63.129.16",
                                  "metadata.google.internal"])
def test_star_never_allows_a_metadata_address_in_any_spelling(monkeypatch, host):
    monkeypatch.setenv(hosts.ALLOWED_ENV, "*")
    assert not hosts.allowed(host)
    with pytest.raises(ValueError, match="not allowed"):
        hosts.check_url(f"http://{host if ':' not in host or host.startswith('[') else '[' + host + ']'}/latest/")


def test_star_still_allows_ordinary_hosts_and_numeric_spellings_match_the_list(monkeypatch):
    monkeypatch.setenv(hosts.ALLOWED_ENV, "*")
    assert all(hosts.allowed(h) for h in ("10.0.0.5", "scada.plant.local", "127.0.0.1", "::1"))
    monkeypatch.setenv(hosts.ALLOWED_ENV, "10.0.0.5")
    assert hosts.allowed("167772165") and hosts.allowed("0x0a000005")     # both are 10.0.0.5
    assert not hosts.allowed("167772166")
    monkeypatch.delenv(hosts.ALLOWED_ENV)
    assert hosts.allowed("2130706433")                                    # 127.0.0.1
    assert not hosts.allowed("2852039166")


# ---------- no token in the server log ----------

def test_the_log_filter_masks_tokens_in_uvicorn_records():
    rec = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, '%s - "WebSocket %s" [accepted]',
                            ("('127.0.0.1', 5000)", "/ws?role=web&token=s3cret-operator-token"), None)
    log_redact.RedactTokens().filter(rec)
    line = rec.getMessage()
    assert "s3cret" not in line and "/ws?role=web&token=***" in line, line
    assert log_redact.redact("/api/x?a=1&api_key=k1&password=p2&role=phone") == \
        "/api/x?a=1&api_key=***&password=***&role=phone"
    assert all(log_redact._FILTER in logging.getLogger(n).filters for n in log_redact.LOGGERS)


def test_both_log_filters_in_either_order_never_show_the_token():
    """After the merge with main, 126's log_redact and the live layer's own filter
    (live_api.RedactTokens) both sit on uvicorn's loggers. In either order the token is gone
    and the rest of the line stays."""
    from backend.app import live_api
    for first, second in ((log_redact.RedactTokens(), live_api.RedactTokens()),
                          (live_api.RedactTokens(), log_redact.RedactTokens())):
        rec = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, '%s - "WebSocket %s" [accepted]',
                                ("('127.0.0.1', 5000)", "/ws?role=web&token=s3cret-operator-token"), None)
        first.filter(rec)
        second.filter(rec)
        line = rec.getMessage()
        assert "s3cret" not in line and re.search(r"/ws\?role=web&token=(\*\*\*|\[redacted\])\" \[accepted\]", line), line
    assert any(isinstance(f, live_api.RedactTokens) for f in logging.getLogger("uvicorn.error").filters)
    assert log_redact._FILTER in logging.getLogger("uvicorn.error").filters


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_ws_token_never_reaches_uvicorns_log():
    import asyncio

    import uvicorn
    import websockets
    from fastapi import FastAPI, WebSocket

    lines: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    grab = Grab()
    for n in ("uvicorn.error", "uvicorn.access"):
        logging.getLogger(n).addHandler(grab)
        logging.getLogger(n).setLevel(logging.INFO)
    mini = FastAPI()

    @mini.websocket("/ws")
    async def ws(sock: WebSocket):
        if sock.query_params.get("token") != "good-token-123":
            await sock.close(code=1008)
            return
        await sock.accept()
        await sock.send_text("hi")
        await sock.close()

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(mini, host="127.0.0.1", port=port, log_config=None, lifespan="off"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        end = time.monotonic() + 10
        while not server.started and time.monotonic() < end:
            time.sleep(0.05)
        assert server.started

        async def go():
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws?role=web&token=good-token-123") as c:
                await c.recv()
            try:
                async with websockets.connect(f"ws://127.0.0.1:{port}/ws?role=web&token=WRONG999"):
                    pass
            except Exception:  # noqa: BLE001 - refused, as expected
                pass

        asyncio.run(go())
        end = time.monotonic() + 3
        while time.monotonic() < end and not any("WebSocket" in x for x in lines):
            time.sleep(0.05)
    finally:
        server.should_exit = True
        th.join(5)
        for n in ("uvicorn.error", "uvicorn.access"):
            logging.getLogger(n).removeHandler(grab)
    text = "\n".join(lines)
    # main's live layer puts its own filter on the same loggers (live_api.install_log_redaction,
    # mask "[redacted]"). Whichever filter runs last picks the mask; either way the token is gone.
    assert re.search(r"WebSocket /ws\?role=web&token=(\*\*\*|\[redacted\])", text), text
    assert "good-token-123" not in text and "WRONG999" not in text, text


# ---------- the demo fakes follow the backend's live clock ----------

def test_the_fakes_move_by_the_backends_live_minutes_and_wait_before_the_story(tmp_path):
    from backend.fakes import opcua as fake_opcua
    from backend.fakes.__main__ import DemoFakes

    clock = {"t": 10.0}
    fakes = DemoFakes(opcua_port=_free_port(), sparkplug_port=_free_port(), workdir=str(tmp_path), period_s=0.2,
                      speed=4.0, clock=lambda: clock["t"])
    try:
        # Before the story: no value is served as good, so no running pump shows over a frozen tank.
        assert all(fakes.opcua.quality[n] == "waiting" for n in fake_opcua.READS)
        assert fakes.opcua.values["TANK_6.Level"] == fakes.story.start_pct["TANK_6"]
        fakes.start_story()
        assert all(fakes.opcua.quality[n] == "good" for n in fake_opcua.READS)
        fakes.step()                                   # reads the clock: nothing to move yet
        assert fakes.t_min == 0.0
        time.sleep(0.3)                                # wall time alone moves nothing (speed 4 would)
        fakes.step()
        assert fakes.t_min == 0.0
        clock["t"] = 12.0
        fakes.step()
        assert fakes.t_min == pytest.approx(2.0)
        st = fakes.story
        want = st.start_pct["TANK_3"] + (st.fill["TANK_3"] - st.drain["TANK_3"]) * 2.0
        assert fakes.sparkplug.get("PLC_1", "TANK_3/Level") == pytest.approx(want, abs=1e-3)
        clock["t"] = None                              # backend unreachable: the story waits
        fakes.step()
        assert fakes.t_min == pytest.approx(2.0)
        clock["t"] = 11.0                              # a backend that restarted: never backwards
        fakes.step()
        assert fakes.t_min == pytest.approx(2.0)
        clock["t"] = 12.5
        fakes.step()
        assert fakes.t_min == pytest.approx(3.5) and fakes.tripped
    finally:
        fakes.stop()


def test_backend_clock_reads_t_from_the_live_state():
    from backend.fakes.__main__ import backend_clock

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"t": 3.5, "clock": "01:47"}).encode() if self.path == "/api/live/state" else b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        assert backend_clock(f"http://127.0.0.1:{srv.server_port}")() == 3.5
    finally:
        srv.shutdown()
        srv.server_close()
    assert backend_clock(f"http://127.0.0.1:{_free_port()}", timeout_s=0.3)() is None
    assert math.isfinite(3.5)
