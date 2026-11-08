"""#105: control token on writes, cross-site refusal, upload limits, never-falsely-safe radio."""
import json

import pytest
from fastapi.testclient import TestClient

from backend.app import auth, blueprints
from backend.app.main import app
from backend.app.planner import radio_outcome
from backend.app.scenario import TEAM_SCENARIO

client = TestClient(app)
FIELD = json.loads(TEAM_SCENARIO.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for env in ("FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN", "FREEBOARD_ALLOWED_HOSTS"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(blueprints, "DATA_DIR", tmp_path / "bp")
    monkeypatch.setattr(blueprints, "find_blender", lambda: None)
    yield
    client.post("/api/blueprints/default/activate", headers={"X-Freeboard-Token": "op-secret"})


def tokens(monkeypatch):
    monkeypatch.setenv("FREEBOARD_TOKEN", "op-secret")
    monkeypatch.setenv("FREEBOARD_PHONE_TOKEN", "phone-secret")


# ---------- token ----------

def test_no_tokens_set_keeps_todays_behavior():
    assert client.post("/api/plans", json={}).status_code == 200
    assert auth.role_for(None) == "open"


@pytest.mark.parametrize("path", ["/api/plans", "/api/forecast", "/api/spacetime/sync", "/api/blueprints",
                                  "/api/blueprints/default/activate"])
def test_every_write_needs_the_token_once_set(monkeypatch, path):
    tokens(monkeypatch)
    r = client.post(path, json={})
    assert r.status_code == 401 and r.json() == {"detail": "Enter the operator PIN to send commands."}
    assert client.post(path, json={}, headers={"X-Freeboard-Token": "wrong"}).status_code == 401


def test_reads_stay_open(monkeypatch):
    tokens(monkeypatch)
    for path in ("/api/field", "/api/state", "/api/health", "/api/blueprints"):
        assert client.get(path).status_code == 200, path


def test_operator_token_and_pin_work(monkeypatch):
    tokens(monkeypatch)
    monkeypatch.setenv("OPERATOR_PIN", "4821")
    plans = client.post("/api/plans", json={}, headers={"X-Freeboard-Token": "op-secret"})
    assert plans.status_code == 200
    pid = plans.json()["plans"][0]["id"]
    assert client.post(f"/api/plans/{pid}/approve").status_code == 401
    assert client.post(f"/api/plans/{pid}/approve", headers={"X-Freeboard-Token": "4821"}).status_code == 200


def test_phone_token_only_confirms_rejects_and_acks(monkeypatch):
    tokens(monkeypatch)
    phone = {"X-Freeboard-Token": "phone-secret"}
    r = client.post("/api/plans", json={}, headers=phone)
    assert r.status_code == 403 and "operator token" in r.json()["detail"]
    assert client.post("/api/blueprints/default/activate", headers=phone).status_code == 403
    assert auth.PHONE_PATHS.match("/api/actions/act_3/confirm")
    assert auth.PHONE_PATHS.match("/api/alerts/al_9/ack")
    assert not auth.PHONE_PATHS.match("/api/actions")
    assert auth.role_for("phone-secret") == "phone" and auth.role_for("op-secret") == "operator"


def test_cors_still_lets_the_console_read_a_401(monkeypatch):
    tokens(monkeypatch)
    r = client.post("/api/plans", json={}, headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ---------- cross-site ----------

def test_cross_site_writes_are_refused_even_without_tokens():
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/plans", json={}, headers=evil).status_code == 403
    r = client.post("/api/blueprints", content=json.dumps({"blueprint": FIELD}),
                    headers={**evil, "Content-Type": "text/plain"})
    assert r.status_code == 403
    assert client.post("/api/plans", json={}, headers={"Origin": "null"}).status_code == 403


def test_same_host_and_local_origins_are_allowed():
    assert client.post("/api/plans", json={}, headers={"Origin": "http://127.0.0.1:5173"}).status_code == 200
    # npm run demo --lan: the console at http://192.168.1.20:5173 calls the API at :8000 on the same host
    r = client.post("/api/plans", json={}, headers={"Origin": "http://192.168.1.20:5173", "Host": "192.168.1.20:8000"})
    assert r.status_code == 200


# ---------- uploads ----------

def test_text_plain_upload_is_refused_and_activates_nothing():
    r = client.post("/api/blueprints", content=json.dumps({"blueprint": FIELD}),
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert client.get("/api/blueprints").json()["active"] == "default"


def test_upload_cap_is_enforced_while_streaming(monkeypatch):
    monkeypatch.setattr(blueprints, "MAX_BYTES", 1000)
    big = json.dumps({"blueprint": FIELD}).encode()
    assert len(big) > 1000

    def body():  # chunked, no Content-Length to trust
        for i in range(0, len(big), 256):
            yield big[i:i + 256]

    r = client.post("/api/blueprints", content=body(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    lie = client.post("/api/blueprints", content=big,
                      headers={"Content-Type": "application/json", "Content-Length": str(len(big))})
    assert lie.status_code == 413


def test_json_and_multipart_still_work():
    assert client.post("/api/blueprints", json={"blueprint": FIELD}).status_code == 200
    r = client.post("/api/blueprints", files={"file": ("field.json", json.dumps(FIELD).encode(), "application/json")})
    assert r.status_code == 200


# ---------- never falsely safe ----------

def test_radio_never_says_every_tank_when_a_tank_is_unknown_or_plan_not_robust():
    base = {"keeps_running": True, "cannot_save": []}
    assert "every tank" in radio_outcome({**base, "robust": True, "unknown_tanks": []})
    for plan in ({**base, "robust": False, "unknown_tanks": []},
                 {**base, "robust": False, "unknown_tanks": ["TANK_5"]}):
        line = radio_outcome(plan)
        assert "every tank" not in line.lower(), line
    assert "Tank 5" in radio_outcome({**base, "robust": False, "unknown_tanks": ["TANK_5"]})
    assert "15%" in radio_outcome({**base, "robust": False, "unknown_tanks": []})


def test_approve_radio_script_matches_the_plan():
    plans = client.post("/api/plans", json={}).json()["plans"]
    for p in plans:
        radio = client.post(f"/api/plans/{p['id']}/approve").json()["radio_script"]
        if not p["robust"]:
            assert "every tank stays below shutdown" not in radio.lower(), radio


# ---------- SpacetimeDB writer lock (D2) ----------

def test_spacetime_push_claims_the_writer_role_first(monkeypatch):
    from backend.app import spacetime_sync
    from backend.app.scenario import load_scenario, state_payload

    calls = []
    monkeypatch.setattr(spacetime_sync, "_config", lambda: ("http://x", "db", "tok"))
    monkeypatch.setattr(spacetime_sync, "call_reducer", lambda r, a, timeout=3.0: calls.append(r))
    monkeypatch.setattr(spacetime_sync, "_claimed", False)
    assert spacetime_sync.push_state(state_payload(load_scenario()))
    assert calls[0] == "claim_writer" and "upsert_asset" in calls
    spacetime_sync.push_state(state_payload(load_scenario()))
    assert calls.count("claim_writer") == 1


def test_spacetime_module_locks_every_live_state_writer():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "spacetime" / "src" / "index.ts").read_text(encoding="utf-8")
    for reducer in ("upsert_asset", "set_clock", "set_plan", "mark_step_done", "clear_actions"):
        body = src.split(f"export const {reducer} =", 1)[1].split("export const", 1)[0]
        assert "requireWriter(ctx)" in body, reducer
