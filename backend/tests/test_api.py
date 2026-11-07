import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import spacetime_sync
from backend.app.main import app
from backend.app.scenario import load_scenario, state_payload

ROOT = Path(__file__).resolve().parents[2]
STATUSES = {"OK", "WARNING", "ALARM", "OFFLINE"}
client = TestClient(app)


def contract_ids() -> set[str]:
    """Asset IDs from the Asset IDs table in docs/CONTRACT.md, minus the _LEVEL meshes."""
    text = (ROOT / "docs" / "CONTRACT.md").read_text(encoding="utf-8")
    section = text.split("## Asset IDs", 1)[1].split("\n## ", 1)[0]
    ids: set[str] = set()
    for prefix, lo, hi in re.findall(r"`([A-Z_]+_)(\d+)`\s+to\s+`[A-Z_]+_(\d+)`", section):
        ids.update(f"{prefix}{n}" for n in range(int(lo), int(hi) + 1))
    section = re.sub(r"`[A-Z0-9_]+`\s+to\s+`[A-Z0-9_]+`", "", section)
    ids.update(re.findall(r"`([A-Z][A-Z0-9_]+)`", section))
    return {i for i in ids if not i.endswith("_LEVEL")}


def test_field_has_contract_keys_and_ids():
    r = client.get("/api/field")
    assert r.status_code == 200
    body = r.json()
    for key in ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks"):
        assert key in body
    ids = {x["id"] for key in body for x in body[key]}
    assert ids <= contract_ids()
    assert len(body["tanks"]) == 6 and len(body["wells"]) == 24


def test_state_shape_and_failure():
    body = client.get("/api/state").json()
    assert body["clock"] == "01:47"
    assert set(body["assets"]) <= contract_ids()
    assert all(a["status"] in STATUSES for a in body["assets"].values())
    assert body["assets"]["PUMP_3"]["status"] == "ALARM"
    assert body["assets"]["TANK_3"]["status"] == "ALARM"
    assert body["assets"]["VALVE_XO_34"]["position"] == "CLOSED"
    assert body["incident"]["root_asset_id"] == "PUMP_3"


def test_missing_level_is_never_safe():
    scenario = load_scenario()
    del scenario["initial_levels_pct"]["TANK_5"]
    tank = state_payload(scenario)["assets"]["TANK_5"]
    assert tank["status"] != "OK"
    assert tank["level_pct"] is None


def test_spacetime_calls_match_module_signatures():
    calls = spacetime_sync.state_to_calls(state_payload(load_scenario()))
    assert calls[0] == ("set_clock", ["01:47", "PUMP_3", "Pump 3 failed. Tank 3 is filling."])
    upserts = {args[0]: args for name, args in calls if name == "upsert_asset"}
    assert upserts["TANK_3"] == ["TANK_3", "ALARM", 71.2, "", ""]
    assert upserts["PUMP_3"][2] == spacetime_sync.NO_LEVEL
    assert all(len(args) == 5 for name, args in calls if name == "upsert_asset")


def test_spacetime_off_by_default(monkeypatch):
    monkeypatch.delenv("SPACETIME_DB", raising=False)
    assert spacetime_sync.push_state({"assets": {}}) is False


def test_spacetime_down_does_not_raise(monkeypatch):
    monkeypatch.setenv("SPACETIME_DB", "freeboard")
    monkeypatch.setenv("SPACETIME_URL", "http://127.0.0.1:9")  # nothing listens here
    assert spacetime_sync.push_state(state_payload(load_scenario())) is False
    assert spacetime_sync.status["last_error"]
