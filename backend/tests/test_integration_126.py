"""#126 integration: the connector kinds main.py registers, connector quality in /api/state
and the forecast, pumps with no fresh reading, and the demo blueprint against its fakes.

Fakes bind 127.0.0.1 on free ports and are always stopped."""
import asyncio
import copy
import json
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app import now
from backend.app.connectors import KINDS, extra
from backend.app.live import LiveField
from backend.app.scenario import load_scenario

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "backend" / "data" / "samples" / "connectors_demo.json"


def wait_for(cond, timeout=15.0, every=0.05, step=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if step:
            step()
        value = cond()
        if value:
            return value
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout} s")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def quiet_env(monkeypatch):
    for var in ("JARVIS_CONFIRM", "JARVIS_MODE", "ANTHROPIC_API_KEY", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FREEBOARD_ONE_CLOCK", "1")


# ---------- extra.py ----------

def test_landed_kinds_are_registered():
    from backend.app.connectors.opcua_ua import OpcUaConnector
    from backend.app.connectors.sparkplug import SparkplugConnector
    import backend.app.main  # noqa: F401 - main imports extra before the live layer

    assert KINDS["opcua"] is OpcUaConnector
    assert KINDS["sparkplug"] is SparkplugConnector
    assert extra.FAILED == {}


def test_a_connector_module_that_fails_to_import_stays_registered_as_unavailable(monkeypatch):
    monkeypatch.setitem(extra.LANDED, "sparkplug", ("no_such_module_126", "Nope", "paho-mqtt"))
    saved = dict(KINDS)
    try:
        failed = extra.register()
        assert "sparkplug" in failed and "pip install" in failed["sparkplug"]
        sc = copy.deepcopy(load_scenario())
        sc["connectors"] = [{"id": "CONN_SP", "kind": "sparkplug", "config": {},
                             "signals": [{"asset_id": "TANK_3", "signal": "level_pct", "address": "x"}]}]
        lf = LiveField(sc)
        st = next(s for s in lf.registry.status() if s["id"] == "CONN_SP")
        assert st["status"] == "unavailable" and "pip install" in st["detail"]
        assert st["kind"] == "sparkplug"
        lf.registry.close()
    finally:
        KINDS.clear()
        KINDS.update(saved)
        extra.FAILED.clear()


# ---------- now.py: connector quality and pumps ----------

class QualityOnly:
    """Stands in for a field connector: only its quality dict matters to now.py."""

    kind = "test"
    signals: dict = {}
    commands: dict = {}

    def __init__(self, quality):
        self.id = "CONN_TEST"
        self.quality = quality

    def status(self):
        return {"id": self.id, "kind": self.kind, "available": True, "detail": ""}

    def read(self):
        return []

    def advance(self, dt_min):
        pass

    def close(self):
        pass


def live_app():
    sc = copy.deepcopy(load_scenario())
    lf = LiveField(sc)
    lf.step(1.0)
    return lf, SimpleNamespace(state=SimpleNamespace(live=lf))


def bad(q="bad", detail="StatusCode BadSensorFailure"):
    return {"quality": q, "detail": detail, "t": 0.0}


def test_good_quality_changes_nothing():
    lf, app = live_app()
    before = now.state(app)
    lf.registry.connectors.append(QualityOnly({("TANK_3", "level_pct"): {"quality": "good", "detail": "", "t": 0}}))
    after = now.state(app)
    assert after["assets"]["TANK_3"] == before["assets"]["TANK_3"]


def test_bad_tank_level_is_unknown_at_once_and_has_no_level():
    lf, app = live_app()
    assert now.state(app)["assets"]["TANK_6"]["level_pct"] is not None
    lf.registry.connectors.append(QualityOnly({("TANK_6", "level_pct"): bad("fault", "4-20 mA input at 2.1 mA")}))
    a = now.state(app)["assets"]["TANK_6"]
    assert a["status"] == "UNKNOWN" and a["level_pct"] is None and a["last_level_pct"] is not None
    assert "CONN_TEST" in a["reason"] and "fault" in a["reason"]
    assert a["quality"] == {"level_pct": "fault"}
    assert now.get(app).scenario["initial_levels_pct"]["TANK_6"] is None


def test_bad_valve_position_counts_as_closed_with_a_warning():
    lf, app = live_app()
    lf.registry.connectors.append(QualityOnly({("VALVE_D_6", "position"): bad("comm_lost", "session lost")}))
    n = now.get(app)
    v = next(v for v in n.scenario["field"]["valves"] if v["id"] == "VALVE_D_6")
    assert v["position"] == "CLOSED"
    assert any("no fresh position reading" in w for w in n.scenario["live_assumptions"])
    a = now.state(app)["assets"]["VALVE_D_6"]
    assert a["status"] == "UNKNOWN" and a["position"] is None and "comm_lost" in a["reason"]


def test_bad_pump_counts_as_off_and_shows_unknown():
    lf, app = live_app()
    lf.registry.connectors.append(QualityOnly({("PUMP_6", "running"): bad("uncertain"),
                                               ("PUMP_6", "flow_bbl_hr"): bad("uncertain")}))
    n = now.get(app)
    assert {"type": "SET_PUMP", "asset_id": "PUMP_6", "value": "OFF", "at_min": 0.0} in n.actions
    assert any("Pump 6" in w or "PUMP_6" in w for w in n.scenario["live_assumptions"])
    a = now.state(app)["assets"]["PUMP_6"]
    assert a["status"] == "UNKNOWN" and "CONN_TEST" in a["reason"]


def test_bad_well_flow_keeps_the_design_rate():
    lf, app = live_app()
    design = next(w for w in load_scenario()["field"]["wells"] if w["id"] == "WELL_1")["water_bbl_hr"]
    lf.registry.connectors.append(QualityOnly({("WELL_1", "flow_bbl_hr"): bad("stale", "retained message")}))
    w = next(w for w in now.get(app).scenario["field"]["wells"] if w["id"] == "WELL_1")
    assert w["water_bbl_hr"] == design
    assert now.state(app)["assets"]["WELL_1"]["status"] == "UNKNOWN"


def test_a_pump_with_no_fresh_run_or_flow_is_unknown_not_ok():
    lf, app = live_app()
    pid = "PUMP_1"
    assert now.state(app)["assets"][pid]["status"] == "OK"
    lf.latest.pop((pid, "running"), None)
    lf.latest.pop((pid, "flow_bbl_hr"), None)
    lf.latest[(pid, "motor_amps")] = {"asset_id": pid, "signal": "motor_amps", "value": 31.0, "t": lf.t,
                                      "wall": "now", "source": "CONN_TEST"}
    assert lf.state()["assets"][pid]["status"] == "OK"     # live.py alone would say OK
    a = now.state(app)["assets"][pid]
    assert a["status"] == "UNKNOWN" and "No fresh run or flow reading" in a["reason"]


# ---------- the demo blueprint ----------

def demo() -> dict:
    return json.loads(DEMO.read_text(encoding="utf-8"))


def test_demo_blueprint_matches_the_schema_on_main(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    got = subprocess.run(["git", "show", "origin/main:docs/blueprint.schema.json"], cwd=ROOT,
                         capture_output=True, text=True, timeout=20)
    if got.returncode != 0:
        pytest.skip("origin/main isn't fetched in this checkout")
    schema_file = tmp_path / "blueprint.schema.json"
    schema_file.write_text(got.stdout, encoding="utf-8")
    validator = jsonschema.Draft202012Validator(json.loads(schema_file.read_text(encoding="utf-8")))
    errors = [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(demo())]
    assert errors == []


def test_demo_blueprint_names_only_freeboard_conn_variables_and_one_owner_per_signal():
    bp = demo()
    team = json.loads((ROOT / "scenario" / "field.json").read_text(encoding="utf-8"))
    for k in ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks"):
        assert bp[k] == team[k]
    owners: dict = {}
    for c in bp["connectors"]:
        for key, var in c["config"].items():
            if key.endswith("_env"):
                assert var.startswith("FREEBOARD_CONN_"), key
        for s in c["signals"]:
            assert (s["asset_id"], s["signal"]) not in owners
            owners[(s["asset_id"], s["signal"])] = c["id"]
    assert {c["kind"] for c in bp["connectors"]} == {"opcua", "sparkplug"}


def test_demo_blueprint_against_the_fakes(monkeypatch, tmp_path):
    """Readings arrive from both connectors, bad quality is UNKNOWN at once, and a
    confirmed valve command reaches its fake exactly once."""
    from backend.fakes.__main__ import DemoFakes

    fakes = DemoFakes(opcua_port=free_port(), sparkplug_port=free_port(), workdir=str(tmp_path), period_s=0.2)
    lf = None
    try:
        for k, v in fakes.env().items():
            monkeypatch.setenv(k, v)
        bp = demo()
        for c in bp["connectors"]:
            if c["kind"] == "opcua":
                c["config"]["endpoint"] = fakes.opcua.endpoint
                c["config"]["publish_ms"] = 100
            else:
                c["config"]["port"] = fakes.sparkplug.port
        path = tmp_path / "demo.json"
        path.write_text(json.dumps(bp), encoding="utf-8")
        sc = load_scenario(path)
        lf = LiveField(sc, bp)
        app = SimpleNamespace(state=SimpleNamespace(live=lf))

        def step():
            lf.step(0.01)

        wait_for(lambda: lf.latest.get(("TANK_6", "level_pct"), {}).get("source") == "CONN_OPCUA"
                 and lf.latest.get(("TANK_3", "level_pct"), {}).get("source") == "CONN_SPARKPLUG"
                 and lf.latest.get(("VALVE_D_3", "position"), {}).get("source") == "CONN_SPARKPLUG", step=step)
        st = now.state(app)["assets"]
        assert st["TANK_6"]["status"] != "UNKNOWN" and st["TANK_3"]["status"] != "UNKNOWN"
        assert {c["id"]: c["status"] for c in lf.registry.status()}["CONN_OPCUA"] == "connected"

        fakes.opcua.set_quality("TANK_6.Level", "bad")
        wait_for(lambda: now.state(app)["assets"]["TANK_6"]["status"] == "UNKNOWN", step=step)
        a = now.state(app)["assets"]["TANK_6"]
        assert a["level_pct"] is None and "CONN_OPCUA" in a["reason"]

        async def act():
            p = await lf.propose({"type": "SET_VALVE", "asset_id": "VALVE_D_3", "value": "CLOSED"},
                                 reason="test", source="console")
            assert p["status"] == "PENDING" and fakes.sparkplug.writes == []
            return await lf.confirm(p["id"], by="operator")

        done = asyncio.run(act())
        assert done["status"] == "CONFIRMED", done
        wait_for(lambda: fakes.sparkplug.writes, step=step)
        time.sleep(0.5)
        valve_writes = [w for w in fakes.sparkplug.writes if "VALVE_D_3" in str(w.get("metric"))]
        assert len(valve_writes) == 1 and valve_writes[0]["value"] is False
        assert fakes.opcua.writes == []
    finally:
        if lf is not None:
            lf.registry.close()
        fakes.stop()
