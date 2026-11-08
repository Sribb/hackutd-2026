"""CF-7: alert priority from the forecast, root-cause grouping, and shelving with a timer."""
import asyncio
import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.forecast import forecast
from backend.app.live import LiveField
from backend.app.live_api import install
from backend.app.scenario import load_scenario

OP, PH = "op-secret-1", "phone-secret-2"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "JARVIS_MODE", "JARVIS_CONFIRM", "NTFY_TOPIC",
                "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN", "FREEBOARD_AUDIT_FILE"):
        monkeypatch.delenv(var, raising=False)


def healthy_scenario() -> dict:
    sc = copy.deepcopy(load_scenario())
    sc["failures"] = []
    return sc


def tripped(live: LiveField) -> dict:
    """Run healthy for 2 minutes, trip Pump 3, and return its PUMP_TRIP alert."""
    for _ in range(8):
        live.step(0.25)
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "TRIP", "reason": "test"})
    for _ in range(3):
        for a in live.step(0.25):
            if a["kind"] == "PUMP_TRIP" and a["active"]:
                return a
    raise AssertionError("no PUMP_TRIP within 3 readings")


def active(live: LiveField) -> list[dict]:
    return [a for a in live.alerts() if a["active"]]


def record(live: LiveField) -> list[tuple[str, dict]]:
    """Every /ws message the live layer broadcasts, as (type, payload)."""
    sent: list[tuple[str, dict]] = []
    real = live.hub.broadcast

    async def broadcast(typ, payload):
        sent.append((typ, copy.deepcopy(payload)))
        await real(typ, payload)

    live.hub.broadcast = broadcast
    return sent


# ---------- priority from the forecast ----------

def test_pump_3_trip_is_exactly_one_p1_timed_by_the_forecast():
    live = LiveField(healthy_scenario())
    trip = tripped(live)
    assert [a["id"] for a in active(live) if a["priority"] == "P1"] == [trip["id"]]
    assert trip["consequence_asset_id"] == "TANK_3" and trip["root_id"] is None

    fc = forecast(live.live_scenario(), [])
    shutdown = next(e for e in fc["events"] if e["type"] == "SHUTDOWN_LEVEL" and e["asset_id"] == "TANK_3")
    assert abs(trip["minutes_to_consequence"] - shutdown["t_min"]) <= 5, (trip, shutdown)
    assert trip["minutes_to_consequence_low"] <= 60 < 180
    assert trip["consequence_clock"] in trip["consequence"]
    # usd_at_risk: the shut-in wells' lost production to the horizon, plus their restarts.
    assert 0 < trip["usd_at_risk"] <= fc["cost"]["lost_production_usd"] + 50

    # And the field agrees: the simulated tank really reaches shutdown about then.
    t0, minutes = live.t, trip["minutes_to_consequence"]
    while live.value("TANK_3", "level_pct") < 90 and live.t - t0 < 180:
        live.step(1.0)
    assert abs((live.t - t0) - minutes) <= 5
    # The countdown follows the clock: the shutdown is now.
    assert trip["minutes_to_consequence"] <= 2 and trip["priority"] == "P1"


def test_tank_3_high_alerts_point_at_the_pump_trip():
    live = LiveField(healthy_scenario())
    trip = tripped(live)
    raised = []
    while live.t < 70 and not any(a["kind"] == "TANK_HIGH_HIGH" for a in raised):
        raised += [a for a in live.step(1.0) if a["active"]]
    high = next(a for a in raised if a["kind"] == "TANK_HIGH")
    hh = next(a for a in raised if a["kind"] == "TANK_HIGH_HIGH")
    assert high["asset_id"] == hh["asset_id"] == "TANK_3"
    assert high["severity"] == "WARNING" and hh["severity"] == "ALARM"
    assert high["root_id"] == trip["id"] and hh["root_id"] == trip["id"]
    # The flood is one cause: exactly one P1 root, everything else folds under it.
    roots = [a for a in active(live) if a["root_id"] is None and a["priority"] == "P1"]
    assert [a["id"] for a in roots] == [trip["id"]]
    assert all(a["root_id"] == trip["id"] for a in active(live) if a["id"] != trip["id"])


def test_priority_is_recomputed_as_the_forecast_changes():
    live = LiveField(healthy_scenario())
    trip = tripped(live)
    assert trip["priority"] == "P1"
    p = asyncio.run(live.propose({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}))
    assert asyncio.run(live.confirm(p["id"]))["status"] == "CONFIRMED"
    events = []
    for _ in range(12):
        live.step(0.5)
        events += [(e, a["id"], a["priority"]) for e, a in live.updates]
    # With the crossover open, Tank 3 shuts in later, so the trip drops to P2, and the
    # screens were told so with an `updated` alert.
    assert trip["priority"] == "P2" and trip["minutes_to_consequence"] > 90
    assert ("updated", trip["id"], trip["priority"]) in events


def test_a_stale_tank_is_never_ranked_safe():
    live = LiveField(healthy_scenario())
    for _ in range(4):
        live.step(1.0)
    live.registry.sim.inject({"asset_id": "TANK_2", "type": "SENSOR_STALE"})
    raised = []
    for _ in range(7):
        raised += live.step(1.0)
    stale = next(a for a in raised if a["kind"] == "SENSOR_STALE" and a["asset_id"] == "TANK_2")
    assert stale["priority"] == "P2" and stale["minutes_to_consequence"] is None
    assert "unknown" in stale["consequence"]


# ---------- shelving ----------

def make_app():
    app = FastAPI()
    live = install(app, healthy_scenario())
    return TestClient(app), live


def trip_over_http(c, headers=None) -> dict:
    assert c.post("/api/live/faults", json={"asset_id": "PUMP_3", "type": "TRIP"}, headers=headers).status_code == 200
    for _ in range(4):
        c.post("/api/live/sim", json={"advance_min": 0.5}, headers=headers)
    return next(a for a in c.get("/api/alerts").json()["alerts"] if a["kind"] == "PUMP_TRIP" and a["active"])


@pytest.mark.parametrize("body", [
    {"minutes": 30},                          # no reason
    {"minutes": 30, "reason": ""},
    {"minutes": 30, "reason": "   "},
    {"reason": "crew on the way"},            # no duration
    {"minutes": 14, "reason": "crew on the way"},
    {"minutes": 241, "reason": "crew on the way"},
])
def test_shelve_needs_a_reason_and_15_to_240_minutes(body):
    c, live = make_app()
    trip = trip_over_http(c)
    r = c.post(f"/api/alerts/{trip['id']}/shelve", json=body)
    assert r.status_code == 422, r.text
    assert c.get("/api/alerts").json()["alerts"][0]["shelved_until"] is None
    assert not [e for e in live.audit.entries() if e["event"] == "shelved"]


def test_shelve_unknown_or_cleared_alert():
    c, live = make_app()
    assert c.post("/api/alerts/alert_99/shelve", json={"minutes": 30, "reason": "x"}).status_code == 404
    trip = trip_over_http(c)
    c.post("/api/live/faults", json={"asset_id": "PUMP_3", "type": "REPAIR"})
    for _ in range(6):
        c.post("/api/live/sim", json={"advance_min": 0.5})
    assert not next(a for a in c.get("/api/alerts").json()["alerts"] if a["id"] == trip["id"])["active"]
    assert c.post(f"/api/alerts/{trip['id']}/shelve", json={"minutes": 30, "reason": "x"}).status_code == 409


def test_shelved_alert_comes_back_when_its_time_is_up():
    c, live = make_app()
    trip = trip_over_http(c)
    sent = record(live)
    t0 = live.t
    r = c.post(f"/api/alerts/{trip['id']}/shelve", json={"minutes": 15, "reason": "Crew is on the way to Pump 3"})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["shelve_reason"] == "Crew is on the way to Pump 3" and a["active"] is True
    assert a["shelved_until_t"] == pytest.approx(t0 + 15, abs=0.01) and a["shelved_until"].endswith("M")
    assert ("alert", "shelved") in [(t, p.get("event")) for t, p in sent]

    c.post("/api/live/sim", json={"advance_min": 14})
    assert next(x for x in c.get("/api/alerts").json()["alerts"] if x["id"] == trip["id"])["shelved_until_t"]
    assert not [p for t, p in sent if p.get("event") == "unshelved"]

    c.post("/api/live/sim", json={"advance_min": 1.5})
    back = next(x for x in c.get("/api/alerts").json()["alerts"] if x["id"] == trip["id"])
    assert back["active"] and back["shelved_until"] is None and back["shelve_reason"] is None
    msgs = [p for t, p in sent if t == "alert" and p.get("event") == "unshelved"]
    assert len(msgs) == 1 and msgs[0]["alert"]["id"] == trip["id"]

    log = [(e["event"], e.get("source"), e.get("why")) for e in live.audit.entries()
           if e["event"] in ("shelved", "unshelved")]
    assert log == [("shelved", "console", None), ("unshelved", "timer", "expired")]
    shelved = next(e for e in live.audit.entries() if e["event"] == "shelved")
    assert shelved["alert_id"] == trip["id"] and shelved["minutes"] == 15 and shelved["reason"]


def test_shelve_and_unshelve_by_hand_with_tokens(monkeypatch):
    monkeypatch.setenv("FREEBOARD_TOKEN", OP)
    monkeypatch.setenv("FREEBOARD_PHONE_TOKEN", PH)
    c, live = make_app()
    trip = trip_over_http(c, headers={"X-Freeboard-Token": OP})
    body = {"minutes": 60, "reason": "Known: crew dispatched", "by": "someone"}
    assert c.post(f"/api/alerts/{trip['id']}/shelve", json=body).status_code == 401
    r = c.post(f"/api/alerts/{trip['id']}/shelve", json=body, headers={"X-Freeboard-Token": PH})
    assert r.status_code == 200 and r.json()["shelved_by"] == "phone"
    assert c.post(f"/api/alerts/{trip['id']}/unshelve").status_code == 401
    r = c.post(f"/api/alerts/{trip['id']}/unshelve", headers={"X-Freeboard-Token": OP})
    assert r.status_code == 200 and r.json()["shelved_until"] is None
    assert c.post(f"/api/alerts/{trip['id']}/unshelve", headers={"X-Freeboard-Token": OP}).status_code == 409
    log = [(e["event"], e["source"], e["role"], e.get("why")) for e in live.audit.entries()
           if e["event"] in ("shelved", "unshelved")]
    assert log == [("shelved", "phone", "phone", None), ("unshelved", "console", "operator", "by hand")]


def test_an_alert_that_clears_while_shelved_ends_its_shelf():
    live = LiveField(healthy_scenario())
    trip = tripped(live)
    asyncio.run(live.shelve(trip["id"], 60, "crew on the way", source="console"))
    live.registry.sim.inject({"asset_id": "PUMP_3", "type": "REPAIR"})
    for _ in range(12):
        live.step(0.5)
    assert not trip["active"] and trip["shelved_until_t"] is None
    assert [e.get("why") for e in live.audit.entries() if e["event"] == "unshelved"] == ["resolved"]
