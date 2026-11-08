"""Command bus, safety gate, kill switch, active field, blueprint lifecycle."""
import json
import os

import pytest
from fastapi.testclient import TestClient

os.environ["SIM_ACK_SCALE"] = "0"

from backend.app import commands, fields  # noqa: E402
from backend.app import blueprints as bp_mod  # noqa: E402
from backend.app.actuators import simulated  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.scenario import DEFAULT_SCENARIO, load_scenario  # noqa: E402

client = TestClient(app)
HUMAN = {"X-Operator": "mounish"}
XO = {"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(bp_mod, "STORE", tmp_path / "blueprints")
    fields.activate(load_scenario(DEFAULT_SCENARIO))
    commands.reset()
    yield
    fields.activate(load_scenario(DEFAULT_SCENARIO))
    commands.reset()


def propose(action, headers=None):
    r = client.post("/api/commands", json={"action": action, "reason": "test"}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def drain():
    while commands.run_once(simulated):
        pass


def shutdown_min(fc, tank="TANK_3"):
    return next((e["t_min"] for e in fc["events"] if e["type"] == "SHUTDOWN_LEVEL" and e["asset_id"] == tank), None)


# ---------- the AI never moves a machine by itself ----------

def test_agent_proposal_does_nothing_until_a_human_approves():
    cmd = propose(XO)
    assert cmd["status"] == "PROPOSED" and cmd["proposed_by"] == "agent" and cmd["check"]["ok"]
    drain()
    assert client.get("/api/state").json()["assets"]["VALVE_XO_34"]["position"] == "CLOSED"


def test_agent_cannot_approve():
    cmd = propose(XO)
    assert client.post(f"/api/commands/{cmd['id']}/approve").status_code == 403
    assert client.post(f"/api/commands/{cmd['id']}/approve", headers={"X-Operator": "agent-jarvis"}).status_code == 403


def test_approved_command_executes_and_changes_state_and_forecast():
    before = shutdown_min(client.post("/api/forecast", json={}).json())
    cmd = propose(XO)
    assert client.post(f"/api/commands/{cmd['id']}/approve", headers=HUMAN).json()["status"] == "APPROVED"
    drain()
    got = {c["id"]: c for c in client.get("/api/commands").json()["commands"]}[cmd["id"]]
    assert got["status"] == "ACKED" and got["approved_by"] == "mounish"
    st = client.get("/api/state").json()
    assert st["assets"]["VALVE_XO_34"]["position"] == "OPEN"
    assert st["applied_actions"][0]["asset_id"] == "VALVE_XO_34"
    after = shutdown_min(client.post("/api/forecast", json={}).json())
    assert after is None or after > before
    events = [a["event"] for a in client.get("/api/audit").json()["audit"]]
    assert events == ["PROPOSED", "APPROVED", "SENT", "ACKED"]


# ---------- safety gate ----------

def test_unsafe_command_is_refused_with_a_reason():
    # Closing Pump 1's discharge valve makes Tank 1 fill toward shutdown.
    cmd = propose({"type": "SET_VALVE", "asset_id": "VALVE_D_1", "value": "CLOSED"})
    assert not cmd["check"]["ok"]
    assert any("Tank 1" in p for p in cmd["check"]["problems"])
    r = client.post(f"/api/commands/{cmd['id']}/approve", headers=HUMAN)
    assert r.status_code == 422 and "Safety check failed" in r.json()["detail"]


def test_invalid_commands_are_refused():
    assert not propose({"type": "SET_PUMP", "asset_id": "PUMP_3", "value": "ON"})["check"]["ok"]  # tripped
    assert not propose({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 3})["check"]["ok"]
    assert not propose({"type": "LAUNCH", "asset_id": "TANK_3"})["check"]["ok"]
    assert not propose({"type": "RESET_PUMP", "asset_id": "PUMP_1"})["check"]["ok"]  # not tripped


def test_check_runs_again_at_approval():
    # Reopening Well 12 is harmless now (it's untouched) ...
    stale = propose({"type": "CHOKE_WELL", "asset_id": "WELL_12", "value": 1})
    assert stale["check"]["ok"]
    # ... but once the saving plan is carried out, undoing its choke would bring the shutdown back.
    for a in ({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"},
              {"type": "CHOKE_WELL", "asset_id": "WELL_12", "value": 0},
              {"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"},
              {"type": "SEND_TRUCK", "asset_id": "TRUCK_2", "target_id": "TANK_3"}):
        fields.apply(a)
    r = client.post(f"/api/commands/{stale['id']}/approve", headers=HUMAN)
    assert r.status_code == 422 and "Tank 3" in r.json()["detail"]


def test_reset_pump_repairs_the_trip():
    cmd = propose({"type": "RESET_PUMP", "asset_id": "PUMP_3"})
    assert cmd["check"]["ok"]
    client.post(f"/api/commands/{cmd['id']}/approve", headers=HUMAN)
    drain()
    st = client.get("/api/state").json()
    assert st["assets"]["PUMP_3"]["status"] == "OK" and st["incident"] is None
    assert shutdown_min(client.post("/api/forecast", json={}).json()) is None


# ---------- kill switch ----------

def test_halt_stops_the_queue_and_resume_needs_a_human():
    cmd = propose(XO)
    client.post(f"/api/commands/{cmd['id']}/approve", headers=HUMAN)
    assert client.post("/api/commands/halt", headers=HUMAN).json()["halted"] is True
    drain()
    assert client.get("/api/state").json()["assets"]["VALVE_XO_34"]["position"] == "CLOSED"
    assert client.post("/api/commands/resume").status_code == 403
    assert client.post("/api/commands/resume", headers=HUMAN).json()["halted"] is False
    drain()
    assert client.get("/api/state").json()["assets"]["VALVE_XO_34"]["position"] == "OPEN"


def test_reject():
    cmd = propose(XO)
    assert client.post(f"/api/commands/{cmd['id']}/reject", json={"reason": "no"}, headers=HUMAN).json()["status"] == "REJECTED"
    assert client.post(f"/api/commands/{cmd['id']}/approve", headers=HUMAN).status_code == 409


# ---------- plans become commands ----------

def test_plan_approval_proposes_one_command_per_step():
    plans = client.post("/api/plans", json={}).json()["plans"]
    out = client.post(f"/api/plans/{plans[0]['id']}/approve", headers=HUMAN).json()
    assert len(out["command_ids"]) == len(plans[0]["actions"]) == len(out["steps"])
    cmds = {c["id"]: c for c in client.get("/api/commands").json()["commands"]}
    assert all(cmds[c]["status"] == "PROPOSED" for c in out["command_ids"])
    for c in out["command_ids"]:
        client.post(f"/api/commands/{c}/approve", headers=HUMAN)
        drain()
    fc = client.post("/api/forecast", json={}).json()
    assert shutdown_min(fc) is None, "after the cheapest plan runs, Tank 3 must not shut down"
    # A new round of plans starts from what's been done.
    again = client.post("/api/plans", json={}).json()["plans"]
    assert again[0]["actions"] == [] and again[0]["keeps_running"]


# ---------- blueprints and the active field ----------

def upload(obj, name="field.json"):
    return client.post("/api/blueprints", files={"file": (name, json.dumps(obj).encode(), "application/json")})


def test_blueprint_upload_validate_activate_switches_everything():
    raw = json.loads(DEFAULT_SCENARIO.read_text(encoding="utf-8"))
    raw["field"]["tanks"] = raw["field"]["tanks"][:4]
    keep = {"TANK_1", "TANK_2", "TANK_3", "TANK_4"}
    f = raw["field"]
    f["pumps"] = [p for p in f["pumps"] if p["tank_id"] in keep]
    pumps = {p["id"] for p in f["pumps"]}
    f["valves"] = [v for v in f["valves"] if v["connects"][0] in pumps or set(v["connects"]) <= keep]
    f["wells"] = [w for w in f["wells"] if w["tank_id"] in keep]
    for t in f["trucks"]:
        t["minutes_to"] = {k: v for k, v in t["minutes_to"].items() if k in keep}
    raw["initial_levels_pct"] = {k: v for k, v in raw["initial_levels_pct"].items() if k in keep}

    r = upload(raw)
    assert r.status_code == 200, r.text
    bp = r.json()
    assert bp["status"] == "VALID" and bp["problems"] == []
    act = client.post(f"/api/blueprints/{bp['id']}/activate")
    assert act.status_code == 200 and act.json()["status"] == "ACTIVE"
    assert len(client.get("/api/field").json()["tanks"]) == 4
    st = client.get("/api/state").json()
    assert st["blueprint_id"] == bp["id"] and "TANK_5" not in st["assets"]
    assert client.post("/api/forecast", json={}).status_code == 200
    assert client.post("/api/plans", json={}).json()["plans"]
    assert client.get("/api/health").json()["blueprint_id"] == bp["id"]


def test_invalid_blueprint_cannot_be_activated_and_lists_problems():
    raw = json.loads(DEFAULT_SCENARIO.read_text(encoding="utf-8"))
    raw["field"]["tanks"][0].pop("capacity_bbl")
    bp = upload(raw).json()
    assert bp["status"] == "INVALID" and any("capacity_bbl" in p for p in bp["problems"])
    r = client.post(f"/api/blueprints/{bp['id']}/activate")
    assert r.status_code == 409
    # Fix it through PUT, then it activates.
    raw["field"]["tanks"][0]["capacity_bbl"] = 1000
    fixed = client.put(f"/api/blueprints/{bp['id']}", json=raw).json()
    assert fixed["status"] == "VALID"
    assert client.post(f"/api/blueprints/{bp['id']}/activate").status_code == 200


def test_switching_fields_cancels_stale_commands():
    cmd = propose(XO)
    raw = json.loads(DEFAULT_SCENARIO.read_text(encoding="utf-8"))
    bp = upload(raw).json()
    client.post(f"/api/blueprints/{bp['id']}/activate")
    r = client.post(f"/api/commands/{cmd['id']}/approve", headers=HUMAN)
    assert r.status_code == 409 and "changed" in r.json()["detail"]


def test_build_reports_clearly_without_blender(monkeypatch):
    monkeypatch.delenv("BLENDER_BIN", raising=False)
    monkeypatch.setattr(bp_mod, "_blender", lambda: None)
    bp = upload(json.loads(DEFAULT_SCENARIO.read_text(encoding="utf-8"))).json()
    r = client.post(f"/api/blueprints/{bp['id']}/build")
    assert r.status_code == 503
    assert client.get(f"/api/blueprints/{bp['id']}/field.glb").status_code == 404


def test_bad_uploads():
    assert client.post("/api/blueprints", files={"file": ("x.json", b"", "application/json")}).status_code == 400
    assert client.get("/api/blueprints/bp_nope").status_code == 404
    assert client.get("/api/blueprints/..%2F..%2Fetc").status_code == 404
