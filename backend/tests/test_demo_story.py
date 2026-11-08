"""The connectors demo plays its story: calm at the start, then the scripted PUMP_3 trip
at live minute 3, and nothing else (verification round 1).

The fakes run on 127.0.0.1 on free ports, in lockstep with a LiveField on the demo
blueprint, and are always stopped."""
import json
import socket
import time
from pathlib import Path

import pytest

import backend.app.main  # noqa: F401 - registers the opcua and sparkplug connector kinds
from backend.app.live import LiveField
from backend.app.scenario import load_scenario
from backend.fakes.__main__ import DEMO_BLUEPRINT, DemoFakes, Story

STEP_MIN = 0.25


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def quiet_env(monkeypatch):
    for var in ("JARVIS_CONFIRM", "JARVIS_MODE", "ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)


def test_story_numbers_come_from_the_demo_blueprint():
    st = Story(json.loads(DEMO_BLUEPRINT.read_text(encoding="utf-8")))
    assert st.start_pct == {"TANK_3": 71.76, "TANK_6": 50.0}
    assert st.trip_at_min == 3.0
    # A running pump drains at (wells in - pump rate): 94 - 150 bbl/hr on a 500 bbl tank.
    assert st.fill["TANK_3"] - st.drain["TANK_3"] == pytest.approx((94 - 150) / 500 * 100 / 60)
    assert st.fill["TANK_6"] - st.drain["TANK_6"] == pytest.approx((92 - 150) / 500 * 100 / 60)
    # After the trip TANK_3 fills from about 71 % to the 90 % shutdown in about an hour (2:47).
    assert 55 <= (90 - 71.2) / st.fill["TANK_3"] <= 65


def test_demo_starts_calm_with_only_the_scripted_pump_3_trip(monkeypatch, tmp_path):
    fakes = DemoFakes(opcua_port=free_port(), sparkplug_port=free_port(), workdir=str(tmp_path), period_s=0.2)
    lf = None
    try:
        for k, v in fakes.env().items():
            monkeypatch.setenv(k, v)
        bp = json.loads(DEMO_BLUEPRINT.read_text(encoding="utf-8"))
        for c in bp["connectors"]:
            if c["kind"] == "opcua":
                c["config"]["endpoint"] = fakes.opcua.endpoint
                c["config"]["publish_ms"] = 100
            else:
                c["config"]["port"] = fakes.sparkplug.port
        path = tmp_path / "demo.json"
        path.write_text(json.dumps(bp), encoding="utf-8")
        lf = LiveField(load_scenario(path), bp)

        # Nothing moves before the backend's host is online; then the story starts. The story
        # stays at minute 0 until both connectors have delivered their first readings.
        end = time.monotonic() + 15
        while time.monotonic() < end:
            fakes.step(0.0)
            lf.step(0.0)
            if (fakes.t_min is not None
                    and lf.latest.get(("TANK_6", "level_pct"), {}).get("source") == "CONN_OPCUA"
                    and lf.latest.get(("TANK_3", "level_pct"), {}).get("source") == "CONN_SPARKPLUG"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("the demo connectors never delivered readings")
        assert lf.value("TANK_3", "level_pct") == pytest.approx(71.76, abs=0.01)
        assert lf.value("TANK_6", "level_pct") == pytest.approx(50.0, abs=0.01)

        watched = ("TANK_3", "PUMP_3", "TANK_6", "PUMP_6", "VALVE_D_3", "VALVE_D_6")
        alarms: dict = {}
        trip_t = None
        while lf.t < 7.0:
            fakes.step(STEP_MIN)
            time.sleep(0.3)          # let both fakes publish the new values
            lf.step(STEP_MIN)
            for a in lf.detector.active.values():
                if a["severity"] == "ALARM":
                    alarms.setdefault(a["asset_id"], (round(lf.t, 2), a["clock"], a["message"]))
            st = lf.state()["assets"]
            if lf.t < 3.0:
                bad = {aid: st[aid]["status"] for aid in watched if st[aid]["status"] not in ("OK", "WARNING")}
                assert not bad and not alarms, f"not calm at {lf.fmap.clock(lf.t)}: {bad} {alarms}"
            if trip_t is None and "PUMP_3" in alarms:
                trip_t = lf.t
        assert fakes.tripped
        assert set(alarms) == {"PUMP_3"}, alarms
        t, clock, msg = alarms["PUMP_3"]
        assert 3.0 <= t <= 4.0 and clock in ("01:47", "01:48"), alarms
        assert "reports stopped" in msg
        st = lf.state()["assets"]
        assert st["PUMP_6"]["status"] == "OK" and st["TANK_6"]["status"] == "OK"
        assert st["TANK_3"]["status"] == "ALARM"          # its only pump is down
        assert lf.value("PUMP_3", "running") is False and lf.value("PUMP_3", "flow_bbl_hr") == 0.0
        # TANK_3 drained until the trip and fills after it; TANK_6 drains the whole time.
        h3 = list(lf.history[("TANK_3", "level_pct")])
        before = [v for tt, v in h3 if tt <= 3.0]
        after = [v for tt, v in h3 if tt >= 4.5]
        assert before[-1] < 71.76 and after[-1] > after[0]
        assert lf.value("TANK_6", "level_pct") < 50.0
    finally:
        if lf is not None:
            lf.registry.close()
        fakes.stop()
