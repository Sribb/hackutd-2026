"""End-to-end tests for the Freeboard backend. Run from the repo root:

    .venv/bin/python -m pytest backend/tests -q
"""
import os

os.environ["FREEBOARD_AUTOSTART"] = "0"   # drive time by hand with /api/sim/advance
os.environ["JARVIS_MODE"] = "rules"       # deterministic, no API calls
os.environ["NTFY_TOPIC"] = ""             # never push from tests

import pytest
from fastapi.testclient import TestClient

from backend.app.field import load_field
from backend.app.main import app
from backend.app.sim import Plant, forecast


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/api/demo/reset")
        yield c


def tripped_plant():
    p = Plant(load_field())
    for _ in range(3):
        p.step(1)
    p.apply({"type": "TRIP_PUMP", "asset_id": "PUMP_3"})
    return p


# ---------------------------------------------------------------- physics
def test_forecast_starts_from_current_level_and_hits_247():
    p = tripped_plant()
    fc = forecast(p, [])
    sd = fc["summary"]["first_shutdown"]
    assert sd["asset_id"] == "TANK_3"
    assert sd["clock"] == "02:47"
    assert sd["t_min_low"] < sd["t_min"] < sd["t_min_high"]
    assert fc["summary"]["wells_lost"] == ["WELL_10", "WELL_11", "WELL_12", "WELL_9"]
    assert fc["cost"]["lost_production_usd"] > 0


def test_full_tank_and_empty_tank_differ():
    """CauldronWatch reported 500 min for both 95% and 5%. Ours must not."""
    f = load_field()
    hi, lo = Plant(f), Plant(f)
    for p, pct in ((hi, 89.0), (lo, 10.0)):
        p.level["TANK_3"] = pct / 100 * 500
        p.apply({"type": "TRIP_PUMP", "asset_id": "PUMP_3"})
    t_hi = forecast(hi, [])["summary"]["first_shutdown"]["t_min"]
    t_lo = forecast(lo, [])["summary"]["first_shutdown"]["t_min"]
    assert t_hi < 5
    assert abs(t_lo - 400 / 94 * 60) < 2  # 10% -> 90% is 400 bbl at 94 bbl/hr


def test_actions_change_the_future():
    p = tripped_plant()
    base = forecast(p, [])
    fixed = forecast(p, [
        {"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "at_min": 5},
        {"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3", "at_min": 0},
        {"type": "SEND_TRUCK", "asset_id": "TRUCK_2", "target_id": "TANK_3", "at_min": 0},
    ])
    assert not base["summary"]["keeps_running"]
    assert fixed["summary"]["keeps_running"]
    assert fixed["cost"]["trucking_usd"] > 0 and fixed["summary"]["hauled_bbl"] > 200


def test_bad_actions_warn_instead_of_crashing():
    p = tripped_plant()
    fc = forecast(p, [{"type": "SET_VALVE", "asset_id": "VALVE_NOPE", "value": "OPEN"},
                      {"type": "SET_PUMP", "asset_id": "PUMP_3", "value": "ON"}])
    assert any("Unknown valve" in w for w in fc["warnings"])
    assert any("tripped" in w for w in fc["warnings"])


# ---------------------------------------------------------------- API + IoT loop
def test_contract_endpoints(client):
    field = client.get("/api/field").json()
    assert {t["id"] for t in field["tanks"]} == {f"TANK_{i}" for i in range(1, 7)}
    st = client.get("/api/state").json()
    assert st["clock"] == "01:44" and "assets" in st


def test_detects_pump_failure_from_sensors_and_counts_down(client):
    client.post("/api/sim/advance", json={"minutes": 6})
    st = client.get("/api/state").json()
    assert st["incident"]["root_asset_id"] == "PUMP_3"
    assert "No flow since 1:47 AM" in st["incident"]["reason"]
    assert st["assets"]["PUMP_3"]["status"] == "ALARM"
    assert st["assets"]["TANK_3"]["status"] == "ALARM"
    assert st["countdown"]["clock"] in ("02:46", "02:47", "02:48")
    alerts = client.get("/api/alerts").json()["alerts"]
    assert any(a["severity"] == "CRITICAL" and "Pump 3" in a["title"] for a in alerts)


def test_plans_approve_and_execute(client):
    client.post("/api/sim/advance", json={"minutes": 6})
    plans = client.post("/api/plans", json={}).json()["plans"]
    assert plans and plans[0]["keeps_running"]
    assert any(p["robust"] for p in plans)
    assert plans == sorted(plans, key=lambda p: (not p["keeps_running"], p["cost"]["total_usd"])) or plans[-1]["robust"]
    res = client.post(f"/api/plans/{plans[0]['id']}/approve", json={"by": "test"}).json()
    assert [s["n"] for s in res["steps"]] == list(range(1, len(res["steps"]) + 1))
    assert "Field, this is control." in res["radio_script"]
    client.post("/api/sim/advance", json={"minutes": 25})  # past the 20 min manual-valve lead time
    cmds = client.get("/api/commands").json()["commands"]
    assert any(c["source"] == "plan" for c in cmds)
    assert client.get("/api/state").json()["countdown"] is None or \
        not client.get("/api/state").json()["countdown"]["certain"]


def test_device_telemetry_drives_detection(client):
    """A real device (e.g. a phone or ESP32) reporting zero flow on a healthy pump trips the detector."""
    client.post("/api/demo/fault", json={"asset_id": "PUMP_3", "type": "REPAIR"})
    r = client.post("/api/telemetry", json={"source": "esp32-1", "readings": [
        {"asset_id": "PUMP_1", "metric": "flow_bbl_hr", "value": 0, "hold_min": 30}]}).json()
    assert r["accepted"] == ["PUMP_1.flow_bbl_hr"]
    client.post("/api/sim/advance", json={"minutes": 3})
    st = client.get("/api/state").json()
    assert st["assets"]["PUMP_1"]["status"] == "ALARM"


def test_sensor_dropout_is_never_safe(client):
    client.post("/api/demo/fault", json={"asset_id": "TANK_5", "type": "SENSOR_DROPOUT"})
    client.post("/api/sim/advance", json={"minutes": 8})
    st = client.get("/api/state").json()
    assert st["assets"]["TANK_5"]["status"] in ("WARNING", "ALARM")
    assert any("TANK_5" in w or "Tank 5" in w for w in st["warnings"])
    fc = client.post("/api/forecast", json={"actions": []}).json()
    assert fc["warnings"]


def test_commands_need_confirmation_when_asked(client):
    client.post("/api/sim/advance", json={"minutes": 6})
    pend = client.post("/api/commands", json={"type": "SET_VALVE", "asset_id": "VALVE_XO_34",
                                              "value": "OPEN", "confirm": True}).json()
    assert pend["status"] == "PENDING"
    assert client.get("/api/telemetry").json()["valves"]["VALVE_XO_34"] == "CLOSED"
    done = client.post(f"/api/commands/{pend['id']}/confirm", json={"by": "Vinay"}).json()
    assert done["status"] == "DONE"
    assert client.get("/api/telemetry").json()["valves"]["VALVE_XO_34"] == "OPEN"
    bad = client.post("/api/commands", json={"type": "SET_VALVE", "asset_id": "PUMP_1", "value": "OPEN"})
    assert bad.status_code == 422


# ---------------------------------------------------------------- realtime + Jarvis
def test_websocket_phone_and_console_stay_in_sync(client):
    client.post("/api/sim/advance", json={"minutes": 6})
    with client.websocket_connect("/ws?role=web&name=console") as web, \
            client.websocket_connect("/ws?role=phone&name=Vinay") as phone:
        hello = web.receive_json()
        assert hello["type"] == "hello" and hello["data"]["state"]["incident"]
        phone.receive_json()  # phone hello

        phone.send_json({"type": "jarvis", "text": "send truck one to tank three", "rid": "r1"})
        seen_web, pending_id = set(), None
        for _ in range(40):
            m = web.receive_json()
            seen_web.add(m["type"])
            if m["type"] == "pending_command":
                pending_id = m["data"]["id"]
            if m["type"] == "jarvis":
                assert m["data"]["source"] == "phone" and "Waiting for your confirmation" in m["data"]["reply"]
                break
        assert {"jarvis_heard", "pending_command", "ui"} <= seen_web
        assert pending_id

        web.send_json({"type": "confirm", "id": pending_id, "by": "console"})
        for _ in range(40):
            m = phone.receive_json()
            if m["type"] == "command":
                assert m["data"]["action"]["asset_id"] == "TRUCK_1" and m["data"]["status"] == "DONE"
                break
        else:
            pytest.fail("phone never saw the confirmed command")


@pytest.mark.parametrize("text,expect", [
    ("what's failing?", "Pump 3 failed"),
    ("how long do we have", "reaches shutdown"),
    ("show me tank 3", "Tank 3 is at"),
    ("fast forward 2 hours", "Showing"),
    ("what's the cheapest fix", "Cheapest plan"),
    ("open the crossover", "Waiting for your confirmation"),
    ("choke well eleven to 50%", "Choke Well 11 to 50%"),
    ("approve plan one", "Waiting for your confirmation"),
])
def test_jarvis_rules(client, text, expect):
    client.post("/api/sim/advance", json={"minutes": 6})
    r = client.post("/api/jarvis", json={"text": text, "by": "test"}).json()
    assert expect in r["reply"], r["reply"]
    assert r["engine"] == "rules"
