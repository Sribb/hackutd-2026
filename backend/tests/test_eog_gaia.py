"""gaia on a dataset (backend/app/eog/gaia.py) and the one guarded line in Jarvis.run_tool."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.eog import dataset_api, gaia, service
from backend.app.jarvis import Jarvis
from backend.app.main import app
from backend.tests.eog_fixture import write_dataset

REAL_ROOT = os.environ.get("FREEBOARD_EOG_DIR", "")
client = TestClient(app)

# jarvis.py's own top-level keys per tool (Jarvis.run_tool), which gaia's answers keep
JARVIS_KEYS = {
    "list_alerts": {"as_of", "open", "groups", "unknown_tanks"},
    "explain_asset": {"asset_id", "name", "kind", "clock", "as_of", "live", "reading", "design", "alerts"},
    "get_forecast": {"now", "as_of", "source", "events", "cost", "warnings", "unknown_tanks"},
    "get_plans": {"as_of", "source", "unknown_tanks", "plans"},
}
ROW_KEYS = {"id", "asset_id", "name", "kind", "severity", "priority", "message", "raised_at", "consequence",
            "consequence_clock", "minutes_to_consequence", "minutes_from_now", "usd_at_risk", "forecast_at",
            "reading", "root_id", "acked", "shelved", "shelved_until", "shelve_reason"}
PLAN_KEYS = {"id", "summary", "actions", "total_usd", "keeps_running", "robust", "cannot_save"}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(dataset_api, "PREBUILD", False)
    for var in ("FREEBOARD_EOG_DATASET", "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    service.SERVICE.reset()
    gaia.clear()
    yield
    service.SERVICE.reset()
    gaia.clear()


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    base = tmp_path / "eog"
    write_dataset(base / "alpha", seed=3, hours=24, repair_hours=5,
                  problems=[{"type": "pump_failure", "facility": "Q7_FACILITY_A", "onset_h": 21.5, "level_pct": 60}])
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    return base


def run(tool: str, args: dict = None) -> dict:
    return asyncio.run(gaia.run_tool(tool, args or {}, {"ui": [], "proposals": []}))


def bare_jarvis() -> Jarvis:
    """A Jarvis without a live field: its own tools answer {"own": tool}, so a test sees who answered."""
    j = object.__new__(Jarvis)
    j.live = None
    j._alert_groups = lambda: {"own": "list_alerts"}
    return j


def test_inactive_until_a_dataset_is_on_screen(root):
    assert not gaia.active()                      # FREEBOARD_EOG_DIR alone does not switch gaia
    client.get("/api/eog/alerts")                 # nor does the /api/eog console reading it
    assert not gaia.active()
    assert client.post("/api/dataset/load", json={"folder": "alpha"}).status_code == 200
    assert gaia.active()
    gaia.clear()
    assert not gaia.active()


def test_inactive_when_dataset_mode_is_off(root, monkeypatch):
    gaia.use("alpha")
    monkeypatch.delenv("FREEBOARD_EOG_DIR")
    assert not gaia.active()


def test_jarvis_hands_the_read_tools_to_gaia_only_while_active(root):
    j = bare_jarvis()
    ctx = {"ui": [], "proposals": []}
    assert asyncio.run(j.run_tool("list_alerts", {}, ctx)) == {"own": "list_alerts"}
    client.post("/api/dataset/load", json={"folder": "alpha"})
    got = asyncio.run(j.run_tool("list_alerts", {}, ctx))
    assert JARVIS_KEYS["list_alerts"] <= set(got) and got["dataset"] == "alpha"
    # focus_camera stays jarvis's own (it reads the live field, None here)
    with pytest.raises(AttributeError):
        asyncio.run(j.run_tool("focus_camera", {"asset_id": "Q7_TANK_A"}, ctx))


def test_tools_keep_jarvis_keys_and_use_sponsor_ids(root):
    client.post("/api/dataset/load", json={"folder": "alpha"})
    la = run("list_alerts")
    assert JARVIS_KEYS["list_alerts"] <= set(la)
    assert "alpha" in la["source"] and la["as_of"].endswith("UTC")
    fails = [g for g in la["groups"] if g["root"]["kind"] == "PUMP_FAILURE"]
    assert fails and ROW_KEYS <= set(fails[0]["root"]) and fails[0]["root"]["asset_id"] == "Q7_PUMP_A"
    assert set(la["groups"][0]) >= {"root", "related", "related_count", "related_ids", "shelved_root"}

    ex = run("explain_asset", {"asset_id": "q7_tank_a"})
    assert JARVIS_KEYS["explain_asset"] <= set(ex)
    assert ex["kind"] == "tank" and ex["live"]["status"] == "ALARM" and "shutdown_pct" in ex["design"]
    assert run("explain_asset", {"asset_id": "TANK_3"}) == {"error": "Unknown asset TANK_3."}
    assert run("explain_asset", {"asset_id": "Q7_PUMP_A"})["live"]["running"] is False

    fc = run("get_forecast", {"actions": []})
    assert JARVIS_KEYS["get_forecast"] <= set(fc)
    sd = [e for e in fc["events"] if e["type"] == "SHUTDOWN_LEVEL"]
    assert sd and sd[0]["asset_id"] == "Q7_TANK_A" and "UTC" in sd[0]["text"]
    assert run("get_forecast", {"actions": [{"type": "SEND_TRUCK"}]})["warnings"][0].startswith("This dataset is a recording")

    pl = run("get_plans")
    assert JARVIS_KEYS["get_plans"] <= set(pl)
    assert pl["plans"] and all(PLAN_KEYS <= set(p) for p in pl["plans"])
    eog = client.get("/api/eog/plans").json()
    want = {it["event"]["id"]: round(it["plan"]["cost"], 2) for it in eog["plans"]}
    assert {p["id"]: p["total_usd"] for p in pl["plans"]} == want
    assert all("asset_id" in a for p in pl["plans"] for a in p["actions"])


def test_propose_never_makes_an_equipment_action(root):
    client.post("/api/dataset/load", json={"folder": "alpha"})
    ctx = {"ui": [], "proposals": []}
    j = bare_jarvis()                               # live is None: a real proposal would crash
    res = asyncio.run(j.run_tool("propose_action", {"type": "SEND_TRUCK", "asset_id": "TRUCK_1"}, ctx))
    assert res == {"error": gaia.RECORDING} and ctx["proposals"] == []
    assert "/api/eog/plans/{event_id}/approve" in res["error"]


def test_sponsor_snapshot_plans(monkeypatch):
    base = Path(REAL_ROOT) if REAL_ROOT else None
    if base is None or not (base / "history" / "snapshot_2026-10-10T0900").is_dir():
        pytest.skip("FREEBOARD_EOG_DIR does not point at the sponsor repo")
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    client.post("/api/dataset/load", json={"folder": "history/snapshot_2026-10-10T0900"})
    pl = run("get_plans")
    s4 = sorted(p["total_usd"] for p in pl["plans"] if p["facility_id"].startswith("S4_"))
    assert s4 == [905.0, 975.31, 1215.0]
    assert "snapshot_2026-10-10T0900" in pl["source"] and pl["as_of"] == "2026-10-10 09:00 UTC"
