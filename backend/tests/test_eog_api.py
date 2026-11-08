"""Dataset mode API (/api/eog): backend/app/eog/api.py and service.py.

service.py calls the real detect.py and plan.py. Most tests here swap in small stand-ins with the
same signatures (Fakes) so they test the API layer alone: fixed events and plans, call counts,
injected NaN and errors. test_real_modules_behind_the_api runs the real modules on the synthetic
dataset, and the sponsor-data tests run them on every history folder. A synthetic dataset written
to tmp_path always runs (its ids look nothing like the sponsor's S1-S5). The sponsor data runs too
when FREEBOARD_EOG_DIR points at it and is skipped otherwise. The app never reads an answer key;
the tests here read them only as fixtures.
"""
from __future__ import annotations

import csv
import importlib
import importlib.abc
import importlib.util
import json
import math
import os
import shutil
import sys
import threading
import time
import types
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app.eog import api, service
from backend.app.eog import detect as real_detect
from backend.app.eog import plan as real_plan
from backend.app.eog.dataset import load
from backend.app.eog.model import Event, Plan, iso
from backend.app.main import app

PKG = "backend.app.eog"
REAL_ROOT = os.environ.get("FREEBOARD_EOG_DIR", "")   # read before any test changes the env
client = TestClient(app)

# ---------- a synthetic dataset ----------
T0 = 1699999800          # any 5-minute boundary; nothing here is tied to the sponsor week
STEP = 300
ROWS = 48
NETWORK = {
    "defaults": {"tank_limits_pct": {"warning": 80, "shutdown": 90}},
    "systems": [{
        "id": "Z9", "center": [10.0, 20.0], "truck_yard": [10.1, 20.1],
        "disposal_site": {"id": "Z9_DISPOSAL", "lat": 10.2, "lon": 20.2, "pressure_psi": 900},
        "facilities": [
            {"id": "Z9_FAC_A", "lat": 10.01, "lon": 20.01,
             "wells": [{"id": "Z9_WELL_A1", "baseline_water_bpd": 1200, "oil_bpd": 100, "gas_mcfd": 400},
                       {"id": "Z9_WELL_A2", "baseline_water_bpd": 600, "oil_bpd": 50, "gas_mcfd": 100}],
             "tank": {"id": "Z9_TANK_A", "capacity_bbl": 500},
             "pump": {"id": "Z9_PUMP_A", "max_flow_bpd": 3000, "start_level_pct": 60, "stop_level_pct": 20},
             "pipeline": {"id": "Z9_PIPE_A", "max_bpd": 3500, "max_pressure_psi": 1500}},
            {"id": "Z9_FAC_B", "lat": 10.03, "lon": 20.04,
             "wells": [{"id": "Z9_WELL_B1", "baseline_water_bpd": 800, "oil_bpd": 200, "gas_mcfd": 700},
                       {"id": "Z9_WELL_B2", "baseline_water_bpd": 400, "oil_bpd": 20, "gas_mcfd": 50}],
             "tank": {"id": "Z9_TANK_B", "capacity_bbl": 400},
             "pump": {"id": "Z9_PUMP_B", "max_flow_bpd": 2500, "start_level_pct": 65, "stop_level_pct": 25},
             "pipeline": {"id": "Z9_PIPE_B", "max_bpd": 3000, "max_pressure_psi": 1500}},
        ]}],
}
OPS = {
    "prices": {"oil_per_bbl": 70, "gas_per_mcf": 2.5},
    "expected_repair_hours": {"pump_failure": 5},
    "emergency_trucking": {"truck_capacity_bbl": 120, "cost_per_trip": 800},
    "systems": {"Z9": {"emergency_fleet_size": 3, "facilities": {
        "Z9_FAC_A": {"response_min": 40, "cycle_min": 100},
        "Z9_FAC_B": {"response_min": 55, "cycle_min": 120}}}},
}
# Events the fake detector reports besides pump faults (rows are telemetry row numbers).
SPECS = [
    {"category": "pump_degradation", "system": "Z9", "facility": "Z9_FAC_B", "asset": "Z9_PUMP_B", "onset": 10, "end": 20},
    {"category": "pipeline_leak", "system": "Z9", "facility": "Z9_FAC_B", "asset": "Z9_PIPE_B", "onset": 40, "end": None},
]


def ts(row: int) -> int:
    return T0 + row * STEP


def readings(r: int) -> list[tuple[str, str, float]]:
    """Facility A's pump faults from row 24 (tank climbing); B's pump faults for rows 26-28,
    its tank sits at warning (rows 30-32) and at shutdown with its wells shut in (rows 33-35)."""
    out = []
    a_fault = r >= 24
    out += [("Z9_WELL_A1", "water_bpd", 1200.0), ("Z9_WELL_A1", "status", 1),
            ("Z9_WELL_A2", "water_bpd", 600.0), ("Z9_WELL_A2", "status", 1),
            ("Z9_FAC_A", "inflow_bpd", 1800.0),
            ("Z9_TANK_A", "level_pct", 55 + (r - 24) * 1.25 if a_fault else 40.0 + 2 * (r % 4)),
            ("Z9_PUMP_A", "status", 2 if a_fault else 1), ("Z9_PUMP_A", "flow_bpd", 0.0 if a_fault else 2500.0),
            ("Z9_PUMP_A", "discharge_psi", 5000.0 if r == 7 else (900.0 if a_fault else 1050.0)),
            ("Z9_PIPE_A", "receive_flow_bpd", 0.0 if a_fault else 2500.0),
            ("Z9_PIPE_A", "pressure_psi", 900.0 if a_fault else 950.0)]
    shut = 33 <= r < 36
    b_status = 2 if 26 <= r < 29 else (1 if r >= 36 else 0)
    b_flow = 2000.0 if b_status == 1 else 0.0
    out += [("Z9_WELL_B1", "water_bpd", 0.0 if shut else 800.0), ("Z9_WELL_B1", "status", 0 if shut else 1),
            ("Z9_WELL_B2", "water_bpd", 0.0 if shut else 400.0), ("Z9_WELL_B2", "status", 0 if shut else 1),
            ("Z9_FAC_B", "inflow_bpd", 0.0 if shut else 1200.0),
            ("Z9_TANK_B", "level_pct", 40.0 if r < 30 else 85.0 if r < 33 else 90.0 if r < 36 else 50.0),
            ("Z9_PUMP_B", "status", b_status), ("Z9_PUMP_B", "flow_bpd", b_flow),
            ("Z9_PUMP_B", "discharge_psi", 1050.0 if b_status == 1 else 900.0),
            ("Z9_PIPE_B", "receive_flow_bpd", b_flow * (0.8 if r >= 40 else 1.0)),
            ("Z9_PIPE_B", "pressure_psi", 950.0 if b_status == 1 else 900.0)]
    return out


def write_dataset(folder: Path, rows: int = ROWS, network: dict = NETWORK, ops: dict = OPS) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "network.json").write_text(json.dumps(network), encoding="utf-8")
    (folder / "ops.json").write_text(json.dumps(ops), encoding="utf-8")
    with open(folder / "telemetry.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "ts_unix", "asset_id", "metric", "value"])
        for r in range(rows):
            for asset, metric, value in readings(r):
                w.writerow([iso(ts(r)), ts(r), asset, metric, value])
    return folder


# ---------- stand-ins with the real signatures, for tests of the API layer alone ----------

class Fakes:
    """detect and plan stand-ins. Pump failures come from runs of pump status 2 (generic, so they
    also work on the sponsor data) and, like detect.detect's, come with their countdown; SPECS add
    a worn pump and a leak."""

    def __init__(self, specs: Optional[list] = None):
        self.specs = specs if specs is not None else []
        self.calls = {k: 0 for k in ("detect", "countdown", "plan_failures", "alternatives", "wait_curve",
                                     "alerts_order", "simulate")}
        self.trucks_free: list = []
        self.modes: list = []
        self.simulated: list = []
        self.mutate = None           # optional f(list[Event]) -> list[Event], e.g. to inject NaN
        self.plan_actions = None     # optional f(event) -> actions for the now plans
        self.detect_delay = 0.0

    # detect
    def detect(self, ds):
        self.calls["detect"] += 1
        if self.detect_delay:
            time.sleep(self.detect_delay)
        found = []
        for f in ds.facilities.values():
            col, run = ds.values(f.pump_id, "status"), None
            for i in range(ds.end):
                if col[i] == 2 and run is None:
                    run = i
                if run is not None and (col[i] != 2 or i == ds.end - 1):
                    ended = col[i] != 2
                    found.append(self._failure(ds, f, int(ds.times[run]), int(ds.times[i]) if ended else None))
                    run = None
        for s in self.specs:
            onset = ts(s["onset"])
            if onset > ds.now:
                continue
            end = ts(s["end"]) if s["end"] is not None and ts(s["end"]) <= ds.now else None
            found.append(Event(id="", category=s["category"], system=s["system"], facility=s["facility"],
                               asset=s["asset"], onset=onset, end=end, status="resolved" if end else "ongoing",
                               confidence=0.7, evidence=[f"{s['category']} seen in the readings"], detected_at=onset))
        found.sort(key=lambda e: (e.onset, e.asset))
        for n, e in enumerate(found, 1):
            e.id = f"EV-{n:04d}"
            if e.category == "pump_failure" and e.status == "ongoing":
                self.countdown(ds, e)          # as detect.detect: events come with their countdown
        return self.mutate(found) if self.mutate else found

    @staticmethod
    def _failure(ds, f, onset: int, end: Optional[int]) -> Event:
        level = ds.value_at(f.tank_id, "level_pct", end if end is not None else None)
        return Event(id="", category="pump_failure", system=f.system, facility=f.id, asset=f.pump_id,
                     onset=onset, end=end, status="resolved" if end else "ongoing", confidence=0.95,
                     evidence=["The pump reports a fault and its flow is zero."], detected_at=onset,
                     level_at_onset_pct=ds.value_at(f.tank_id, "level_pct", onset), level_pct=level,
                     repair_end=int(onset + ds.repair_hours * 3600), tripped=bool(level is not None and level >= ds.shutdown_pct),
                     lost_revenue_per_hour=ds.facility_revenue_per_hour(f.id))

    def countdown(self, ds, ev):
        self.calls["countdown"] += 1
        f = ds.facilities[ev.facility]
        level = ds.value_at(f.tank_id, "level_pct")
        if level is None:                 # no reading: no countdown, never a made-up one
            ev.level_pct = ev.hours_to_limit = ev.trips_at = None
            return
        rate = f.baseline_water_bpd / 24.0 / f.capacity_bbl * 100.0   # % per hour with the pump off
        hours = max(0.0, (ds.shutdown_pct - level) / rate)
        ev.level_pct = level
        ev.hours_to_limit = {"p50": round(hours, 2)}
        trip = int(ds.now + hours * 3600)
        ev.trips_at = trip if trip < ev.repair_end else None

    # plan
    def plan_failures(self, ds, events, start_ts=None, busy=None, mode="now"):
        self.calls["plan_failures"] += 1
        self.modes.append((mode, [e.id for e in events]))
        out = {}
        for ev in events:
            start = ds.now if mode == "now" else ev.onset
            fleet = ds.systems[ev.system].fleet_size
            actions = (self.plan_actions(ev) if self.plan_actions else
                       [{"type": "dispatch_truck", "facility_id": ev.facility, "trucks": 1, "trips_per_truck": 1}])
            out[ev.id] = Plan(event_id=ev.id, facility_id=ev.facility, system=ev.system, start=start, actions=actions,
                              cost=ds.truck_cost, trips=False, trip_at=None, peak_level_pct=88.0,
                              summary=f"1 truck, 1 trip (~${ds.truck_cost:,.0f})", truck_cost=ds.truck_cost,
                              trucks_free=fleet, fleet_size=fleet, expires_at=start + 1800)
        return out

    def alternatives(self, ds, event, start_ts=None, trucks_free=None):
        self.calls["alternatives"] += 1
        self.trucks_free.append((event.id, trucks_free))
        return [Plan(event_id=event.id, facility_id=event.facility, system=event.system, start=ds.now,
                     actions=[{"type": "none"}], cost=5000.0, trips=True, trip_at=ds.now + 3600,
                     peak_level_pct=ds.shutdown_pct, summary="do nothing (~$5,000)", auto_shut_in_cost=5000.0)]

    def wait_curve(self, ds, events, event_id, step_min=5, until=None):
        self.calls["wait_curve"] += 1      # plan.wait_curve's row shape, where start_ts is a ts_unix number
        return [{"delay_min": k * step_min, "start_ts": ds.now + k * step_min * 60, "cost": 800.0 + 50 * k,
                 "summary": "1 truck, 1 trip (~$800)", "trucks_free": 3 - (k > 1)} for k in range(4)]

    def alerts_order(self, events, plans):
        self.calls["alerts_order"] += 1
        fails = [e for e in events if e.category == "pump_failure"]
        p = lambda e: (e.hours_to_limit or {}).get("p50")   # noqa: E731
        fails.sort(key=lambda e: (p(e) if isinstance(p(e), (int, float)) and math.isfinite(p(e)) else math.inf,
                                  -(e.lost_revenue_per_hour or 0)))
        return fails + [e for e in events if e.category != "pump_failure"]

    def simulate(self, ds, facility_id, start_ts, level_pct, repair_end, actions, inflow_bpd=None, wells_on=None):
        self.calls["simulate"] += 1
        self.simulated.append({"facility_id": facility_id, "start_ts": start_ts, "level_pct": level_pct,
                               "repair_end": repair_end, "actions": actions})
        trips = sum(a["trucks"] * a["trips_per_truck"] for a in actions if a["type"] == "dispatch_truck")
        return {"cost": trips * ds.truck_cost if actions else 5000.0, "trips": not actions,
                "peak_level_pct": level_pct + (5 if actions else 20)}

    def install(self, monkeypatch) -> "Fakes":
        """Point service's detect and plan at these stand-ins for one test (monkeypatch undoes it)."""
        monkeypatch.setattr(service, "detect", SimpleNamespace(detect=self.detect))
        monkeypatch.setattr(service, "plan", SimpleNamespace(
            plan_failures=self.plan_failures, alternatives=self.alternatives, wait_curve=self.wait_curve,
            alerts_order=self.alerts_order, simulate=self.simulate))
        return self


# ---------- fixtures ----------

@pytest.fixture(autouse=True)
def fresh_service(monkeypatch):
    for var in ("FREEBOARD_EOG_DATASET", "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    service.SERVICE.reset()
    monkeypatch.setattr(service.SERVICE, "poll_s", 0.02)
    monkeypatch.setattr(service.SERVICE, "real", time.monotonic)
    yield
    service.SERVICE.reset()


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """A datasets root holding `alpha` (48 rows) and `beta` (the same week cut at 30 rows)."""
    base = tmp_path / "eog"
    write_dataset(base / "alpha")
    write_dataset(base / "beta", rows=30)
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    return base


@pytest.fixture
def fakes(root, monkeypatch) -> Fakes:
    return Fakes(SPECS).install(monkeypatch)


def ok(r, status: int = 200) -> dict:
    assert r.status_code == status, r.text
    return strict(r.text)


def strict(text: str):
    """Parse as strict JSON: NaN or Infinity anywhere fails."""
    def no(name):
        raise AssertionError(f"{name} in a payload")
    return json.loads(text, parse_constant=no)


def sentence(detail) -> None:
    assert isinstance(detail, str) and detail.endswith("."), detail
    assert detail.count(". ") == 0, f"more than one sentence: {detail}"


def recv(ws, timeout: float = 5.0) -> dict:
    box: dict = {}

    def run():
        try:
            box["msg"] = ws.receive_json()
        except Exception as e:  # noqa: BLE001
            box["err"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        pytest.fail(f"no WebSocket message within {timeout} s")
    if "err" in box:
        raise box["err"]
    return box["msg"]


def routes() -> list[tuple[str, str]]:
    out = []
    for r in api.router.routes:
        if isinstance(r, APIRoute):
            out += [(m, r.path.replace("{event_id}", "EV-0001")) for m in sorted(r.methods)]
        elif isinstance(r, APIWebSocketRoute):
            out.append(("WS", r.path))
    return out


# ---------- dataset mode off ----------

def test_every_route_is_mounted_on_the_app():
    mounted = {(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    expected = {("GET", "/api/eog/datasets"), ("POST", "/api/eog/datasets/activate"), ("GET", "/api/eog/clock"),
                ("POST", "/api/eog/clock"), ("GET", "/api/eog/network"), ("GET", "/api/eog/state"),
                ("GET", "/api/eog/alerts"), ("GET", "/api/eog/events"), ("GET", "/api/eog/plans"),
                ("POST", "/api/eog/whatif"), ("GET", "/api/eog/series"), ("GET", "/api/eog/handoff"),
                ("POST", "/api/eog/plans/{event_id}/approve")}
    assert expected <= mounted
    assert any(isinstance(r, APIWebSocketRoute) and r.path == "/api/eog/ws" for r in app.routes)


def test_off_every_route_answers_503_with_one_sentence(monkeypatch):
    monkeypatch.delenv("FREEBOARD_EOG_DIR", raising=False)
    checked = 0
    for method, path in routes():
        if method == "WS":
            continue
        r = client.request(method, path, json={} if method == "POST" else None, params={"at": "garbage"})
        assert r.status_code == 503, (method, path, r.text)
        assert r.json()["detail"] == service.OFF
        checked += 1
    assert checked == 13
    sentence(service.OFF)
    assert "FREEBOARD_EOG_DIR" in service.OFF


def test_off_websocket_says_why_and_closes(monkeypatch):
    monkeypatch.delenv("FREEBOARD_EOG_DIR", raising=False)
    with client.websocket_connect("/api/eog/ws") as ws:
        assert recv(ws) == {"type": "error", "status": 503, "detail": service.OFF}
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 1013


def test_off_when_the_root_is_not_a_folder_or_has_no_dataset(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(tmp_path / "nowhere"))
    r = client.get("/api/eog/clock")
    assert r.status_code == 503 and "FREEBOARD_EOG_DIR" in r.json()["detail"]
    sentence(r.json()["detail"])
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(tmp_path / "empty"))
    service.SERVICE.reset()
    r = client.get("/api/eog/datasets")
    assert r.status_code == 503 and "telemetry.csv" in r.json()["detail"]


# ---------- datasets ----------

def test_datasets_list_default_and_activate(root):
    body = ok(client.get("/api/eog/datasets"))
    assert body["enabled"] is True
    assert [d["id"] for d in body["datasets"]] == ["alpha", "beta"]
    assert body["active"] == "alpha"          # neither test nor history/full_week: the first found
    assert body["clock"]["last"] == ts(ROWS - 1) and body["clock"]["at"] == ts(ROWS - 1)

    body = ok(client.post("/api/eog/datasets/activate", json={"id": "beta"}))
    assert body["active"] == "beta"
    assert body["clock"]["last"] == ts(29) and body["clock"]["now"] == ts(29) and body["clock"]["playing"] is False
    assert ok(client.get("/api/eog/clock"))["dataset"] == "beta"

    r = client.post("/api/eog/datasets/activate", json={"id": "gamma"})
    assert r.status_code == 404
    sentence(r.json()["detail"])
    assert "alpha" in r.json()["detail"] and "beta" in r.json()["detail"]
    for bad in ({}, {"id": 7}, {"id": "  "}):
        r = client.post("/api/eog/datasets/activate", json=bad)
        assert r.status_code == 422, bad
        sentence(r.json()["detail"])
    for raw in ("[1, 2]", "{not json", '"beta"'):
        r = client.post("/api/eog/datasets/activate", content=raw, headers={"content-type": "application/json"})
        assert r.status_code == 422 and r.json()["detail"] == "The request body must be a JSON object."
    r = client.post("/api/eog/datasets/activate", json={"id": "beta", "force": True})
    assert r.status_code == 422 and "'force'" in r.json()["detail"]


def test_a_broken_dataset_is_listed_but_cannot_be_activated(root):
    broken = root / "broken"
    shutil.copytree(root / "beta", broken)
    (broken / "telemetry.csv").write_text("when,what\n1,2\n", encoding="utf-8")
    body = ok(client.get("/api/eog/datasets"))
    assert "broken" in [d["id"] for d in body["datasets"]]
    r = client.post("/api/eog/datasets/activate", json={"id": "broken"})
    assert r.status_code == 422 and "telemetry.csv columns" in r.json()["detail"]
    assert ok(client.get("/api/eog/datasets"))["active"] == "alpha"


def test_default_prefers_env_then_test_then_full_week(tmp_path, monkeypatch):
    base = tmp_path / "repo"
    write_dataset(base / "aaa", rows=12)
    write_dataset(base / "history" / "full_week", rows=12)
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    assert ok(client.get("/api/eog/datasets"))["active"] == "history/full_week"
    write_dataset(base / "test", rows=12)
    service.SERVICE.reset()
    assert ok(client.get("/api/eog/datasets"))["active"] == "test"
    monkeypatch.setenv("FREEBOARD_EOG_DATASET", "aaa")
    service.SERVICE.reset()
    assert ok(client.get("/api/eog/datasets"))["active"] == "aaa"
    monkeypatch.setenv("FREEBOARD_EOG_DATASET", "nope")
    service.SERVICE.reset()
    body = ok(client.get("/api/eog/datasets"))
    assert body["active"] == "test" and "nope" in body["warnings"][0]
    # A root one level up: ids gain a prefix and the defaults still win.
    monkeypatch.delenv("FREEBOARD_EOG_DATASET")
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(tmp_path))
    service.SERVICE.reset()
    assert ok(client.get("/api/eog/datasets"))["active"] == "repo/test"


# ---------- clock and ?at= ----------

def test_parse_ts_forms_and_garbage():
    p = service.parse_ts
    assert p(None) is None and p("") is None and p("   ") is None
    assert p("1700000100") == 1700000100 and p(1700000100.5) == 1700000100.5
    assert p("1.7000001e9") == 1700000100
    assert p("1700000100000") == 1700000100          # milliseconds, as JavaScript sends them
    assert p("2023-11-14T22:15:00Z") == 1699999800 + 300
    assert p("2023-11-14T22:15:00z") == 1699999800 + 300
    assert p("2023-11-14T22:15:00") == 1699999800 + 300      # no offset: UTC
    assert p("2023-11-15T03:15:00+05:00") == 1699999800 + 300
    assert p("2023-11-14T22:15:00.000Z") == 1699999800 + 300  # JavaScript's toISOString()
    assert p(" 2023-11-14 22:15 ") == 1699999800 + 300
    for bad in ("garbage", "nan", "inf", "-Infinity", "1e400", "2023-13-40T00:00:00Z", True, [1]):
        with pytest.raises(service.BadInput) as e:
            p(bad)
        sentence(e.value.sentence)


def test_at_is_clamped_and_garbage_is_422(fakes):
    low = ok(client.get("/api/eog/state", params={"at": ts(-100)}))
    assert (low["at"], low["now"]) == (ts(0), ts(0))
    high = ok(client.get("/api/eog/state", params={"at": ts(10_000)}))
    assert (high["at"], high["now"]) == (ts(ROWS - 1), ts(ROWS - 1))
    clock = ok(client.post("/api/eog/clock", json={"at": ts(10_000)}))
    assert clock["at"] == ts(ROWS - 1) and clock["live"] is True
    clock = ok(client.post("/api/eog/clock", json={"at": -5}))
    assert clock["at"] == clock["start"] == ts(0)
    body = ok(client.get("/api/eog/state", params={"at": ts(10) + 299}))
    assert body["now"] == ts(10) and body["at"] == ts(10) + 299
    assert ok(client.get("/api/eog/state", params={"at": iso(ts(12))}))["now"] == ts(12)
    for path in ("/api/eog/datasets", "/api/eog/clock", "/api/eog/network", "/api/eog/state", "/api/eog/alerts",
                 "/api/eog/events", "/api/eog/plans", "/api/eog/handoff"):
        r = client.get(path, params={"at": "yesterday-ish"})
        assert r.status_code == 422, path
        sentence(r.json()["detail"])
    r = client.get("/api/eog/series", params={"asset_id": "Z9_TANK_A", "at": "nan"})
    assert r.status_code == 422


def test_clock_cursor_is_the_default_at_of_every_get(fakes):
    body = ok(client.post("/api/eog/clock", json={"at": iso(ts(20))}))
    assert (body["at"], body["now"], body["playing"]) == (ts(20), ts(20), False)
    assert body["start"] == ts(0) and body["last"] == ts(ROWS - 1) and body["step_s"] == 300
    for path in ("/api/eog/state", "/api/eog/alerts", "/api/eog/events", "/api/eog/plans", "/api/eog/handoff",
                 "/api/eog/network"):
        assert ok(client.get(path))["now"] == ts(20), path
    series = ok(client.get("/api/eog/series", params={"asset_id": "Z9_TANK_A", "metric": "level_pct"}))
    assert series["points"][-1][0] == ts(20)
    # A GET with its own ?at= never moves the cursor.
    assert ok(client.get("/api/eog/state", params={"at": ts(5)}))["now"] == ts(5)
    assert ok(client.get("/api/eog/clock"))["at"] == ts(20)


def test_autoplay_moves_with_real_time_and_pauses_at_the_end(fakes, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(service.SERVICE, "real", lambda: clock["t"])
    body = ok(client.post("/api/eog/clock", json={"at": ts(0), "play": True, "speed": 5}))
    assert body["playing"] is True and body["speed"] == 5
    clock["t"] += 1.0                         # 1 real second at 5 data-minutes per second
    assert ok(client.get("/api/eog/clock"))["at"] == ts(1)
    clock["t"] += 0.5
    c = ok(client.get("/api/eog/clock"))
    assert c["at"] == ts(1) + 150 and c["now"] == ts(1)
    assert ok(client.get("/api/eog/state"))["now"] == ts(1)
    body = ok(client.post("/api/eog/clock", json={"speed": 60}))   # a speed change keeps the position
    assert body["at"] == ts(1) + 150 and body["playing"] is True
    clock["t"] += 1.0
    assert ok(client.get("/api/eog/clock"))["at"] == ts(1) + 150 + 3600
    body = ok(client.post("/api/eog/clock", json={"play": False}))
    frozen = body["at"]
    clock["t"] += 100.0
    assert ok(client.get("/api/eog/clock"))["at"] == frozen
    ok(client.post("/api/eog/clock", json={"play": True}))
    clock["t"] += 10_000.0
    c = ok(client.get("/api/eog/clock"))
    assert c["at"] == ts(ROWS - 1) and c["playing"] is False and c["live"] is True
    body = ok(client.post("/api/eog/clock", json={"play": True}))
    assert body["playing"] is False and "nothing to play" in body["note"]


def test_step_and_range_come_from_the_telemetry(tmp_path, monkeypatch):
    """A dataset on a 10-minute grid with one missing row: step_s is read from the file, and
    the range is its first and last ts_unix whatever the system clock says."""
    base = tmp_path / "grid"
    folder = write_dataset(base / "ten", rows=10)
    lines = (folder / "telemetry.csv").read_text(encoding="utf-8").splitlines()
    out = [lines[0]]
    for ln in lines[1:]:
        t = int(ln.split(",")[1])
        row = (t - T0) // STEP
        if row % 2 == 0 and row != 6:                  # keep rows 0, 2, 4, 8: a 600 s grid with a gap
            out.append(ln)
    (folder / "telemetry.csv").write_text("\n".join(out) + "\n", encoding="utf-8")
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    c = ok(client.get("/api/eog/clock"))
    assert (c["start"], c["last"], c["now"], c["step_s"]) == (ts(0), ts(8), ts(8), 600)
    assert service.step_of(service.array("q", [5])) == service.STEP_S     # one row: SPEC's 5 minutes
    assert service.step_of(service.array("q", [0, 300, 600, 1200, 1500])) == 300


def test_concurrent_first_requests_parse_the_dataset_once(root, monkeypatch):
    real_load, live, peak = service.dsmod.load, [0], [0]
    lock = threading.Lock()

    def slow_load(path, ds_id=None):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        try:
            return real_load(path, ds_id)
        finally:
            with lock:
                live[0] -= 1
    monkeypatch.setattr(service.dsmod, "load", slow_load)
    threads = [threading.Thread(target=service.SERVICE.dataset) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert peak[0] == 1


def test_clock_rejects_bad_input_with_sentences(fakes):
    for bad in ({"speed": 0}, {"speed": -2}, {"speed": "fast"}, {"speed": 1e9}, {"play": "maybe"},
                {"at": "soon"}, {"rewind": True}):
        r = client.post("/api/eog/clock", json=bad)
        assert r.status_code == 422, bad
        sentence(r.json()["detail"])
    r = client.post("/api/eog/clock", content='{"speed": NaN}', headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert ok(client.post("/api/eog/clock", content=b""))["at"] == ts(ROWS - 1)   # empty body: no change


# ---------- network and state ----------

def test_network_comes_from_the_files(fakes):
    body = ok(client.get("/api/eog/network"))
    (s,) = body["systems"]
    net = NETWORK["systems"][0]
    assert s["id"] == "Z9" and s["center"] == net["center"] and s["truck_yard"] == net["truck_yard"]
    assert s["disposal"] == {"id": "Z9_DISPOSAL", "lat": 10.2, "lon": 20.2, "pressure_psi": 900}
    assert s["fleet_size"] == 3 and s["facility_ids"] == ["Z9_FAC_A", "Z9_FAC_B"]
    facs = {f["id"]: f for f in body["facilities"]}
    for nf in net["facilities"]:
        f, o = facs[nf["id"]], OPS["systems"]["Z9"]["facilities"][nf["id"]]
        assert (f["lat"], f["lon"]) == (nf["lat"], nf["lon"])
        assert f["tank"]["capacity_bbl"] == nf["tank"]["capacity_bbl"]
        assert (f["tank"]["warning_pct"], f["tank"]["shutdown_pct"]) == (80, 90)
        assert f["tank"]["shutdown_bbl"] == nf["tank"]["capacity_bbl"] * 0.9
        assert f["pump"] == {"id": nf["pump"]["id"], **{k: nf["pump"][k] for k in ("max_flow_bpd", "start_level_pct", "stop_level_pct")}}
        assert f["pipeline"]["id"] == nf["pipeline"]["id"] and f["pipeline"]["disposal_id"] == "Z9_DISPOSAL"
        assert (f["response_min"], f["cycle_min"]) == (o["response_min"], o["cycle_min"])
    wells = {w["id"]: w for w in body["wells"]}
    a1 = wells["Z9_WELL_A1"]
    assert a1["revenue_per_day"] == 100 * 70 + 400 * 2.5
    assert a1["revenue_per_hour"] == pytest.approx(8000 / 24)
    assert a1["cost_per_bbl_avoided"] == pytest.approx(8000 / 1200)
    assert facs["Z9_FAC_A"]["revenue_per_hour"] == pytest.approx((8000 + 3750) / 24)
    assert {"from": "Z9_WELL_A1", "to": "Z9_TANK_A", "kind": "water"} in body["links"]
    assert {"from": "Z9_PIPE_B", "to": "Z9_DISPOSAL", "kind": "pipeline"} in body["links"]
    assert body["trucking"] == {"truck_capacity_bbl": 120, "cost_per_trip": 800}
    assert body["prices"] == {"oil_per_bbl": 70, "gas_per_mcf": 2.5} and body["repair_hours"] == 5


def test_network_has_no_blueprint_layout(fakes):
    """Dataset mode reads the sponsor files only: no eog.blueprint, so the network has no
    `layout` (and no `warnings`, which only ever reported a layout failure)."""
    body = ok(client.get("/api/eog/network"))
    assert set(body) == {"dataset", "at", "at_ts", "now", "now_ts", "limits", "prices", "repair_hours",
                         "trucking", "systems", "facilities", "wells", "links"}
    for mod in (service, api):
        assert "blueprint" not in Path(mod.__file__).read_text(encoding="utf-8"), mod.__name__


def test_state_statuses_follow_readings_and_events(fakes):
    def state(row):
        return ok(client.get("/api/eog/state", params={"at": ts(row)}))["assets"]

    a = state(5)
    assert all(v["status"] == "OK" for v in a.values()), {k: v["status"] for k, v in a.items() if v["status"] != "OK"}
    assert a["Z9_TANK_A"]["readings"] == {"level_pct": 42.0}
    assert a["Z9_PUMP_A"]["readings"] == {"discharge_psi": 1050.0, "flow_bpd": 2500.0, "status": 1.0}
    assert a["Z9_DISPOSAL"] == {"kind": "disposal", "system": "Z9", "facility": None, "status": "OK",
                                "readings": {}, "events": [], "reasons": []}

    a = state(15)                              # the worn pump on B is ongoing
    assert a["Z9_PUMP_B"]["status"] == "INSPECT" and a["Z9_PUMP_B"]["events"] == ["EV-0001"]
    assert a["Z9_FAC_B"]["status"] == "INSPECT" and a["Z9_TANK_B"]["status"] == "OK"

    a = state(31)                              # B's tank above warning, its pump healthy (off)
    assert a["Z9_TANK_B"]["status"] == "WARNING" and "80% warning" in a["Z9_TANK_B"]["reasons"][0]
    assert a["Z9_PUMP_A"]["status"] == "ALARM" and a["Z9_TANK_A"]["status"] == "ALARM"
    assert set(a["Z9_TANK_A"]["events"]) == {"EV-0002"} and "EV-0002" in a["Z9_WELL_A1"]["events"]

    a = state(34)                              # B's tank at shutdown, wells shut in
    assert a["Z9_TANK_B"]["status"] == "ALARM" and "90% shutdown" in a["Z9_TANK_B"]["reasons"][0]
    assert a["Z9_WELL_B1"]["status"] == "ALARM" and a["Z9_WELL_B1"]["readings"]["status"] == 0
    assert a["Z9_FAC_B"]["status"] == "ALARM"

    body = ok(client.get("/api/eog/state"))
    a = body["assets"]
    assert a["Z9_PIPE_B"]["status"] == "INSPECT" and a["Z9_PIPE_B"]["events"] == ["EV-0004"]
    assert a["Z9_FAC_B"]["status"] == "INSPECT" and a["Z9_FAC_A"]["status"] == "ALARM"
    assert body["systems"] == {"Z9": "ALARM"}
    assert set(body["events"]) == {"EV-0002", "EV-0004"}
    assert body["counts"]["ALARM"] == 3 and sum(body["counts"].values()) == len(a) == 2 * 6 + 1
    assert body["limits"] == {"warning_pct": 80, "shutdown_pct": 90}


def test_missing_readings_are_never_ok(tmp_path, monkeypatch):
    """CONTRACT: never report an asset safe because there is no data."""
    base = tmp_path / "gap"
    folder = write_dataset(base / "gappy", rows=26)        # pump A faults from row 24
    lines = (folder / "telemetry.csv").read_text(encoding="utf-8").splitlines()
    last = str(ts(25))
    drop = {("Z9_TANK_A", "level_pct"), ("Z9_TANK_B", "level_pct"), ("Z9_PUMP_B", "status"),
            ("Z9_PIPE_B", "receive_flow_bpd"), ("Z9_PIPE_B", "pressure_psi"), ("Z9_WELL_B2", "status")}
    keep = [ln for ln in lines if not any(f",{last},{a},{m}," in ln for a, m in drop)]
    assert len(lines) - len(keep) == len(drop)
    (folder / "telemetry.csv").write_text("\n".join(keep) + "\n", encoding="utf-8")
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    Fakes([]).install(monkeypatch)
    body = ok(client.get("/api/eog/state"))
    a = body["assets"]
    assert body["warnings"] == []
    assert a["Z9_TANK_A"]["readings"]["level_pct"] is None and a["Z9_TANK_A"]["status"] == "ALARM"   # its pump failed
    assert a["Z9_TANK_A"]["reasons"] == ["Its pump has failed, so the tank is filling. There is no level reading at this time."]
    assert a["Z9_TANK_B"]["status"] == "WARNING" and "can't be called safe" in a["Z9_TANK_B"]["reasons"][0]
    assert a["Z9_PUMP_B"]["status"] == "WARNING" and a["Z9_PIPE_B"]["status"] == "WARNING"
    assert a["Z9_WELL_B2"]["status"] == "WARNING" and a["Z9_FAC_B"]["status"] == "WARNING"
    assert a["Z9_PIPE_A"]["status"] == "OK" and a["Z9_WELL_B1"]["status"] == "OK"
    healthy = ok(client.get("/api/eog/state", params={"at": ts(24)}))["assets"]   # the row before has every reading
    assert healthy["Z9_PUMP_B"]["status"] == "OK" and healthy["Z9_PIPE_B"]["status"] == "OK"
    # A replay needs a level to start from, and a made-up one would be worse than none.
    r = client.post("/api/eog/whatif", json={"facility_id": "Z9_FAC_A", "actions": []})
    assert r.status_code == 422 and r.json()["detail"] == (
        f"Z9_TANK_A has no level reading at {iso(ts(25))}, so there is no level to replay from.")


# ---------- alerts, events, plans ----------

def test_alerts_order_countdown_and_actions(fakes):
    body = ok(client.get("/api/eog/alerts"))
    assert body["now"] == ts(ROWS - 1) and body["count"] == 2
    pf, leak = body["alerts"]
    assert (pf["order"], pf["event_id"], pf["category"], pf["asset"]) == (1, "EV-0002", "pump_failure", "Z9_PUMP_A")
    assert pf["confidence"] == 0.95 and pf["evidence"] and pf["urgent"] is True
    cd = pf["countdown"]
    assert cd["tank_id"] == "Z9_TANK_A" and cd["level_pct"] == 83.75
    assert cd["hours_to_limit"]["p50"] == pytest.approx((90 - 83.75) / (1800 / 24 / 500 * 100), abs=0.01)
    assert cd["repair_end"] == ts(24) + 5 * 3600 and cd["trips_at_ts"] == iso(cd["trips_at"])
    act = pf["action"]
    assert act["type"] == "plan" and act["summary"] == "1 truck, 1 trip (~$800)" and act["cost"] == 800
    assert act["actions"] == [{"type": "dispatch_truck", "facility_id": "Z9_FAC_A", "trucks": 1, "trips_per_truck": 1}]
    assert act["expires_at"] == ts(ROWS - 1) + 1800 and act["expires_at_ts"] == iso(ts(ROWS - 1) + 1800)
    assert "Z9_TANK_A reaches shutdown at" in pf["title"]
    assert (leak["order"], leak["category"], leak["urgent"]) == (2, "pipeline_leak", True)
    assert leak["action"] == {"type": "field_inspection", "asset_id": "Z9_PIPE_B", "summary": "Send an inspector to Z9_PIPE_B."}
    assert leak["countdown"] is None and leak["approval"] is None
    assert body["counts"] == {"pump_failure": 1, "pipeline_leak": 1, "pump_degradation": 0, "other": 0}
    assert ok(client.get("/api/eog/alerts", params={"at": ts(15)}))["alerts"][0]["title"] == "Z9_PUMP_B is wearing out: send an inspector"


def test_events_json_shape_and_download(fakes):
    body = ok(client.get("/api/eog/events"))
    events = body["events"]
    assert body["count"] == 4 and [e["event_id"] for e in events] == ["EV-0001", "EV-0002", "EV-0003", "EV-0004"]
    keys = {"event_id", "system_id", "facility_id", "asset_id", "category", "status", "onset_ts", "onset_unix",
            "end_ts", "end_unix", "confidence", "evidence", "level_pct", "hours_to_limit", "recommended_actions"}
    for e in events:
        assert keys <= set(e), e
        assert e["status"] in ("ongoing", "resolved") and (e["end_ts"] is None) == (e["status"] == "ongoing")
    worn, pf, pf_resolved, leak = events
    assert worn["recommended_actions"] == [{"type": "field_inspection", "asset_id": "Z9_PUMP_B"}] and worn["hours_to_limit"] is None
    assert pf["recommended_actions"][0]["type"] == "dispatch_truck" and pf["est_cost"] == 800
    assert pf["hours_to_limit"]["p50"] > 0
    assert pf_resolved["status"] == "resolved" and pf_resolved["end_unix"] == ts(29) and pf_resolved["hours_to_limit"] is None
    assert pf_resolved["est_cost"] == 800 and "est_cost" not in leak
    # Ongoing failures get the plan from now, resolved ones the plan made at their onset.
    modes = dict((m, ids) for m, ids in fakes.modes)
    assert modes["now"] == ["EV-0002"] and modes["reference"] == ["EV-0002", "EV-0003"]

    r = client.get("/api/eog/events", params={"download": "1"})
    assert r.status_code == 200 and r.headers["content-disposition"] == 'attachment; filename="events.json"'
    assert r.headers["content-type"].startswith("application/json")
    assert strict(r.text) == events
    r = client.get("/api/eog/events", params={"download": "maybe"})
    assert r.status_code == 422
    assert ok(client.get("/api/eog/events", params={"download": "0"}))["count"] == 4
    assert [e["event_id"] for e in ok(client.get("/api/eog/events", params={"at": ts(12)}))["events"]] == ["EV-0001"]


def test_plans_now_and_reference(fakes):
    body = ok(client.get("/api/eog/plans"))
    assert body["mode"] == "now"
    (item,) = body["plans"]
    assert item["event"]["id"] == "EV-0002" and item["plan"]["start"] == ts(ROWS - 1)
    assert item["trucks_free"] == 3 and item["alternatives"][0]["summary"] == "do nothing (~$5,000)"
    # wait_curve rows go out exactly as plan.wait_curve returns them (start_ts is a ts_unix number)
    assert item["wait_curve"] == fakes.wait_curve(service.SERVICE.dataset(), [], "EV-0002")
    assert item["wait_curve"][0] == {"delay_min": 0, "start_ts": ts(ROWS - 1), "cost": 800.0,
                                     "summary": "1 truck, 1 trip (~$800)", "trucks_free": 3}
    assert body["systems"] == [{"system": "Z9", "fleet_size": 3, "events": ["EV-0002"], "trucks_used": 1, "cost": 800.0}]

    # Two failures at once in one system share its fleet: each sees what the other leaves.
    body = ok(client.get("/api/eog/plans", params={"at": ts(27)}))
    assert [p["event"]["id"] for p in body["plans"]] == ["EV-0002", "EV-0003"]
    assert [p["trucks_free"] for p in body["plans"]] == [2, 2]
    assert body["systems"][0]["trucks_used"] == 2

    ref = ok(client.get("/api/eog/plans", params={"mode": "reference"}))
    assert ref["mode"] == "reference"
    assert [(p["event"]["id"], p["plan"]["start"]) for p in ref["plans"]] == [("EV-0002", ts(24)), ("EV-0003", ts(26))]
    assert ref["systems"][0]["trucks_used"] is None
    r = client.get("/api/eog/plans", params={"mode": "later"})
    assert r.status_code == 422
    sentence(r.json()["detail"])


def test_whatif_replays_the_ongoing_failure(fakes):
    acts = [{"type": "dispatch_truck", "trucks": 2, "trips_per_truck": 1},
            {"type": "shut_in_wells", "wells": ["Z9_WELL_A2"], "start_after_min": 30}]
    body = ok(client.post("/api/eog/whatif", json={"facility_id": "Z9_FAC_A", "actions": acts}))
    assert body["event"]["id"] == "EV-0002" and body["start"] == ts(ROWS - 1) and body["level_pct"] == 83.75
    assert body["repair_end"] == ts(24) + 5 * 3600
    assert body["actions"][0] == {"type": "dispatch_truck", "facility_id": "Z9_FAC_A", "trucks": 2, "trips_per_truck": 1}
    assert body["result"]["cost"] == 1600 and body["baseline"]["cost"] == 5000 and body["savings"] == 3400
    first, second = fakes.simulated[-2:]
    assert first["actions"] == body["actions"] and second["actions"] == []
    assert first["start_ts"] == ts(ROWS - 1) and first["level_pct"] == 83.75

    body = ok(client.post("/api/eog/whatif", json={"facility_id": "Z9_FAC_A", "actions": [], "at": iso(ts(30))}))
    assert body["now"] == ts(30) and body["level_pct"] == 55 + 6 * 1.25

    r = client.post("/api/eog/whatif", json={"facility_id": "Z9_FAC_B", "actions": []})
    assert r.status_code == 422 and "no ongoing pump failure" in r.json()["detail"]
    sentence(r.json()["detail"])
    r = client.post("/api/eog/whatif", json={"facility_id": "Z9_FAC_Q", "actions": []})
    assert r.status_code == 404
    bad_actions = [
        {"type": "teleport"}, {"trucks": 1}, "truck", {"type": "dispatch_truck", "trucks": 4},
        {"type": "dispatch_truck", "trucks": 0}, {"type": "dispatch_truck", "trucks": 1.5},
        {"type": "dispatch_truck", "facility_id": "Z9_FAC_B"}, {"type": "dispatch_truck", "trips": 2},
        {"type": "shut_in_wells", "wells": ["Z9_WELL_B1"]}, {"type": "shut_in_wells", "wells": []},
        {"type": "shut_in_wells", "wells": ["Z9_WELL_A1"], "start_after_min": -5},
        {"type": "field_inspection"},
    ]
    for a in bad_actions:
        r = client.post("/api/eog/whatif", json={"facility_id": "Z9_FAC_A", "actions": [a]})
        assert r.status_code == 422, (a, r.text)
        sentence(r.json()["detail"])
    for body in ({"facility_id": "Z9_FAC_A", "actions": {"type": "none"}}, {"actions": []},
                 {"facility_id": "Z9_FAC_A", "actions": [{"type": "dispatch_truck", "trucks": 2}] * 2}):
        r = client.post("/api/eog/whatif", json=body)
        assert r.status_code == 422, body
        sentence(r.json()["detail"])


def test_whatif_starts_from_the_events_countdown_level(fakes):
    """A replay starts from the event's level, which detect takes from plan.countdown (the level
    the plans start from), so replaying the recommended actions costs what the recommended plan
    says. No second countdown is asked for (the stand-in plan has none), and a tank already at
    shutdown starts at shutdown."""
    assert not hasattr(service.plan, "countdown")
    fakes.mutate = lambda evs: [replace(e, level_pct=81.2) if e.category == "pump_failure" else e for e in evs]
    body = ok(client.post("/api/eog/whatif", json={"facility_id": "Z9_FAC_A", "actions": []}))
    assert body["level_pct"] == 81.2 and fakes.simulated[-1]["level_pct"] == 81.2
    fakes.mutate = lambda evs: [replace(e, tripped=True) if e.category == "pump_failure" else e for e in evs]
    service.SERVICE.cache.clear()
    assert ok(client.post("/api/eog/whatif", json={"facility_id": "Z9_FAC_A"}))["level_pct"] == 90.0


def test_series_downsamples_keeping_spikes(fakes):
    body = ok(client.get("/api/eog/series", params={"asset_id": "Z9_PUMP_A", "metric": "discharge_psi"}))
    assert body["raw_points"] == ROWS and len(body["points"]) == ROWS
    assert body["points"][0] == [ts(0), 1050.0]
    small = ok(client.get("/api/eog/series", params={"asset_id": "Z9_PUMP_A", "metric": "discharge_psi", "max_points": 6}))
    pts = small["points"]
    assert len(pts) <= 6 and [ts(7), 5000.0] in pts and min(v for _, v in pts) == 900.0
    assert pts == sorted(pts)
    both = ok(client.get("/api/eog/series", params={"asset_id": "Z9_PUMP_A", "max_points": 10}))
    assert set(both["metrics"]) == {"discharge_psi", "flow_bpd", "status"} and "points" not in both
    assert all(len(p) <= 10 for p in both["metrics"].values())
    assert both["raw_points"] == {"discharge_psi": ROWS, "flow_bpd": ROWS, "status": ROWS}
    win = ok(client.get("/api/eog/series", params={"asset_id": "Z9_TANK_B", "metric": "level_pct",
                                                    "from": iso(ts(29)), "to": ts(34)}))
    assert [p[0] for p in win["points"]] == [ts(r) for r in range(29, 35)]
    assert win["from"] == ts(29) and win["to"] == ts(34)
    travel = ok(client.get("/api/eog/series", params={"asset_id": "Z9_TANK_B", "metric": "level_pct", "at": ts(10)}))
    assert travel["points"][-1][0] == ts(10) and travel["raw_points"] == 11     # nothing after "at"
    future = ok(client.get("/api/eog/series", params={"asset_id": "Z9_TANK_B", "metric": "level_pct", "to": ts(999)}))
    assert future["to"] == ts(ROWS - 1)
    for params, code in (({}, 422), ({"asset_id": "Z9_NOPE"}, 404), ({"asset_id": "Z9_TANK_A", "metric": "flow_bpd"}, 404),
                         ({"asset_id": "Z9_TANK_A", "from": ts(9), "to": ts(3)}, 422),
                         ({"asset_id": "Z9_TANK_A", "max_points": "lots"}, 422), ({"asset_id": "Z9_TANK_A", "max_points": 1}, 422),
                         ({"asset_id": "Z9_TANK_A", "from": "dawn"}, 422)):
        r = client.get("/api/eog/series", params=params)
        assert r.status_code == code, (params, r.text)
        sentence(r.json()["detail"])
    assert "level_pct" in client.get("/api/eog/series", params={"asset_id": "Z9_TANK_A", "metric": "flow_bpd"}).json()["detail"]


def test_downsample_unit():
    pts = [(i, float(i % 7)) for i in range(1000)] + [(1000, 99.0)] + [(i, 3.0) for i in range(1001, 2000)]
    out = service.downsample(pts, 50)
    assert len(out) <= 50 and (1000, 99.0) in out and out == sorted(out)
    assert service.downsample(pts[:10], 50) == pts[:10]
    assert service.downsample(pts, 2) == [(0, 0.0), (1000, 99.0)]


def test_handoff_reads_in_thirty_seconds(fakes):
    body = ok(client.get("/api/eog/handoff"))
    # T0 is Tue 2023-11-14 22:10 UTC, so the last row (47) is Wed 02:05.
    assert body["lines"] == [
        "Handoff Wed 2023-11-15 02:05 UTC: 1 pump down, 1 inspection open.",
        "1. Z9_PUMP_A down since 00:10: Z9_TANK_A 84%, shuts in at 02:30, 25 min left. "
        "Do: 1 truck, 1 trip (~$800), start by 02:35.",
        "Inspect: leak on Z9_PIPE_B since 01:30.",
        "Resolved this shift: Z9_PUMP_B failure (ended 00:35), Z9_PUMP_B worn pump (ended Tue 23:50).",
        "Plans total $800.",
    ]
    assert body["text"] == "\n".join(body["lines"]) and body["words"] == len(body["text"].split())
    assert body["read_s"] == math.ceil(body["words"] / 4) <= 30          # 240 words a minute
    assert body["counts"] == {"failing": 1, "inspect": 1, "resolved": 2} and body["total_cost"] == 800
    assert body["shift_start"] == ts(0) and body["shift_hours"] == 12
    calm = ok(client.get("/api/eog/handoff", params={"at": ts(5)}))
    assert calm["lines"] == ["Handoff Tue 2023-11-14 22:35 UTC: all clear, no pump down and no inspection open.",
                             "Nothing resolved this shift."]
    short = ok(client.get("/api/eog/handoff", params={"shift_hours": 1}))
    assert "worn pump" not in short["text"] and short["shift_start"] == ts(ROWS - 1) - 3600
    assert short["lines"][-2:] == ["Nothing resolved this shift.", "Plans total $800."]
    for bad in (0, "long", 200):
        r = client.get("/api/eog/handoff", params={"shift_hours": bad})
        assert r.status_code == 422
        sentence(r.json()["detail"])


def test_handoff_stays_short_on_a_busy_night(fakes):
    """More than three pumps down, five inspections and five resolved events: every failed pump
    is still named, the lists end in "and N more", and it still reads in about 30 seconds."""
    def busy(events):
        extra = []
        for k, (fac, pump) in enumerate([("Z9_FAC_B", "Z9_PUMP_B")] * 3):
            extra.append(Event(id="", category="pump_failure", system="Z9", facility=fac, asset=pump,
                               onset=ts(41 + k), end=None, status="ongoing", confidence=0.9,
                               level_pct=50.0 + k, hours_to_limit={"p50": 1.0 + k},
                               trips_at=ts(ROWS - 1) + (1 + k) * 3600, repair_end=ts(41 + k) + 5 * 3600,
                               lost_revenue_per_hour=100.0))
        for k in range(4):
            extra.append(Event(id="", category="pump_degradation", system="Z9", facility="Z9_FAC_A",
                               asset="Z9_PUMP_A", onset=ts(30 + k), end=None, status="ongoing", confidence=0.6))
            extra.append(Event(id="", category="pipeline_leak", system="Z9", facility="Z9_FAC_A",
                               asset="Z9_PIPE_A", onset=ts(1 + k), end=ts(5 + k), status="resolved", confidence=0.8))
        out = sorted(events + extra, key=lambda e: e.onset)
        for n, e in enumerate(out, 1):
            e.id = f"EV-{n:04d}"
        return out
    fakes.mutate = busy
    body = ok(client.get("/api/eog/handoff"))
    lines = body["lines"]
    assert body["counts"] == {"failing": 4, "inspect": 5, "resolved": 6}
    assert lines[0].endswith("4 pumps down, 5 inspections open.")
    assert [ln[:3] for ln in lines[1:4]] == ["1. ", "2. ", "3. "]
    assert lines[4].startswith("Also down: Z9_PUMP_B (shuts in at ") and lines[4].endswith(").")
    assert lines[5].startswith("Inspect: leak on Z9_PIPE_B since 01:30, worn Z9_PUMP_A since 00:40")  # leaks first
    assert lines[6].startswith("Resolved this shift: Z9_PUMP_B failure (ended 00:35)")   # latest first
    for line, total in ((lines[5], 5), (lines[6], 6)):
        named = line.count(", ") + 1
        assert named < total and line.endswith(f" and {total - named} more."), line
    assert lines[7] == f"Plans total ${4 * 800:,}." and len(lines) == 8
    assert body["words"] <= service.HANDOFF_WORDS and body["read_s"] <= 30, (body["words"], body["text"])


# ---------- approvals ----------

def test_approve_gives_steps_radio_script_and_records_who(fakes):
    before = ok(client.get("/api/eog/events"))["events"]
    body = ok(client.post("/api/eog/plans/EV-0002/approve", json={"by": "Dana"}))
    now = ts(ROWS - 1)
    steps = body["steps"]
    assert [s["n"] for s in steps] == list(range(1, len(steps) + 1))
    assert steps[0]["text"].startswith("Call 1 truck from the Z9 truck yard to Z9_FAC_A now")
    assert steps[1]["at"] == now + 40 * 60 and "First load: 1 truck at Z9_FAC_A" in steps[1]["text"]
    assert "hauling 120 bbl" in steps[1]["text"]
    assert steps[-1]["at"] == ts(24) + 5 * 3600 and "repair is due" in steps[-1]["text"]
    assert body["radio_script"].startswith("Field, this is control. Z9_PUMP_A at Z9_FAC_A has been down since")
    assert "Control out." in body["radio_script"] and "Plan: 1 truck, 1 trip (~$800)." in body["radio_script"]
    ap = body["approval"]
    assert (ap["by"], ap["at"], ap["event_id"], ap["cost"]) == ("Dana", now, "EV-0002", 800)
    alert = ok(client.get("/api/eog/alerts"))["alerts"][0]
    assert alert["approval"]["by"] == "Dana" and alert["approval"]["seq"] == ap["seq"]
    assert ok(client.get("/api/eog/plans"))["plans"][0]["approval"]["by"] == "Dana"
    assert "Do: 1 truck, 1 trip (~$800), approved by Dana." in ok(client.get("/api/eog/handoff"))["text"]
    assert ok(client.get("/api/eog/events"))["events"] == before        # the overlay never changes the data

    body = ok(client.post("/api/eog/plans/EV-0004/approve"))               # the leak: an inspector
    assert body["approval"]["by"] == "operator" and body["actions"] == [{"type": "field_inspection", "asset_id": "Z9_PIPE_B"}]
    assert body["steps"][0]["text"].startswith("Send an inspector to Z9_PIPE_B")
    r = client.post("/api/eog/plans/EV-0001/approve")
    assert r.status_code == 422 and "resolved" in r.json()["detail"]
    r = client.post("/api/eog/plans/EV-0099/approve")
    assert r.status_code == 404
    sentence(r.json()["detail"])
    assert client.post("/api/eog/plans/EV-0002/approve", json={"by": 5}).status_code == 422
    assert client.post("/api/eog/plans/EV-0002/approve", json={"who": "x"}).status_code == 422


def test_approve_steps_for_shut_ins_and_accepted_trips(fakes):
    fakes.plan_actions = lambda ev: [{"type": "shut_in_wells", "wells": ["Z9_WELL_A2"], "start_after_min": 30},
                                     {"type": "none"}]
    body = ok(client.post("/api/eog/plans/EV-0002/approve", json={"at": ts(30)}))
    texts = [s["text"] for s in body["steps"]]
    shut = next(s for s in body["steps"] if s["asset_id"] == "Z9_WELL_A2")
    assert shut["at"] == ts(30) + 1800 and shut["text"].startswith("Shut in Z9_WELL_A2 at ")
    assert "30 min from now" in shut["text"]
    assert any(t.startswith("No action needed: Z9_TANK_A stays under shutdown") for t in texts)
    assert body["now"] == ts(30) and body["approval"]["at"] == ts(30)


# ---------- cache ----------

def test_detection_and_plans_are_cached_per_view(fakes):
    for _ in range(3):
        ok(client.get("/api/eog/alerts"))
        ok(client.get("/api/eog/state"))
    assert fakes.calls["detect"] == 1 and fakes.calls["plan_failures"] == 1 and fakes.calls["alerts_order"] == 1
    ok(client.get("/api/eog/alerts", params={"at": ts(30)}))
    ok(client.get("/api/eog/alerts", params={"at": ts(30) + 120}))   # same 5-minute row: same view
    assert fakes.calls["detect"] == 2


def test_lru_evicts_and_single_flights():
    lru = service.LRU(maxsize=2)
    calls = []
    for k in ("a", "b", "a", "c", "b"):
        lru.get((k,), lambda k=k: calls.append(k) or k)
    assert calls == ["a", "b", "c", "b"] and len(lru) == 2

    slow = service.LRU()
    count = {"n": 0}

    def compute():
        count["n"] += 1
        time.sleep(0.2)
        return "done"
    threads = [threading.Thread(target=slow.get, args=(("k",), compute)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert count["n"] == 1 and slow.get(("k",), compute) == "done"

    def boom():
        raise ValueError("bad")
    with pytest.raises(ValueError):
        slow.get(("x",), boom)
    assert slow.get(("x",), lambda: 3) == 3           # a failure is not cached


# ---------- WebSocket ----------

def test_websocket_pushes_ticks_on_changes(fakes, monkeypatch):
    clock = {"t": 50.0}
    monkeypatch.setattr(service.SERVICE, "real", lambda: clock["t"])
    with client.websocket_connect("/api/eog/ws") as ws:
        first = recv(ws)
        assert first["type"] == "tick" and first["dataset"] == "alpha"
        assert first["clock"]["at"] == ts(ROWS - 1) and [a["event_id"] for a in first["alerts"]] == ["EV-0002", "EV-0004"]

        ok(client.post("/api/eog/clock", json={"at": ts(15)}))
        msg = recv(ws)
        assert msg["clock"]["at"] == ts(15) and [a["event_id"] for a in msg["alerts"]] == ["EV-0001"]

        ok(client.post("/api/eog/clock", json={"play": True, "speed": 5}))
        assert recv(ws)["clock"]["playing"] is True
        clock["t"] += 1.0                                   # one data row later
        msg = recv(ws)
        assert msg["clock"]["now"] == ts(16) and msg["clock"]["playing"] is True
        ok(client.post("/api/eog/clock", json={"play": False}))
        assert recv(ws)["clock"]["playing"] is False

        ok(client.post("/api/eog/plans/EV-0001/approve"))
        assert recv(ws)["alerts"][0]["approval"]["event_id"] == "EV-0001"

        ok(client.post("/api/eog/datasets/activate", json={"id": "beta"}))
        msg = recv(ws)
        assert msg["dataset"] == "beta" and msg["clock"]["last"] == ts(29)


def test_websocket_keeps_the_clock_going_through_errors(fakes, root, monkeypatch):
    def offline(ds):
        raise RuntimeError("detector offline")

    with client.websocket_connect("/api/eog/ws") as ws:
        assert recv(ws)["type"] == "tick"
        monkeypatch.setattr(service.detect, "detect", offline)    # detection breaks
        ok(client.post("/api/eog/clock", json={"at": ts(3)}))
        msg = recv(ws)
        assert msg["type"] == "tick" and msg["clock"]["at"] == ts(3) and msg["alerts"] is None
        assert msg["error"] == {"status": 500, "detail": "Internal error: RuntimeError: detector offline."}

        fakes.install(monkeypatch)                                 # back, but alerts_order has a bug
        monkeypatch.setattr(service.plan, "alerts_order", lambda events, plans: 1 / 0)
        ok(client.post("/api/eog/clock", json={"at": ts(4)}))      # nothing ongoing: no ordering needed
        msg = recv(ws)
        assert msg["clock"]["at"] == ts(4) and msg["alerts"] == [] and "error" not in msg
        ok(client.post("/api/eog/clock", json={"at": ts(15)}))     # the worn pump is ongoing
        msg = recv(ws)
        assert msg["clock"]["at"] == ts(15) and msg["alerts"] is None
        assert msg["error"] == {"status": 500, "detail": "Internal error: ZeroDivisionError: division by zero."}

        monkeypatch.setattr(service.plan, "alerts_order", fakes.alerts_order)
        ok(client.post("/api/eog/clock", json={"at": ts(16)}))
        msg = recv(ws)
        assert [a["event_id"] for a in msg["alerts"]] == ["EV-0001"] and "error" not in msg

        # The dataset itself breaks: one error message, then ticks again once it is fixed.
        (root / "alpha" / "telemetry.csv").write_text("when,what\n1,2\n", encoding="utf-8")
        msg = recv(ws)
        assert msg["type"] == "error" and msg["status"] == 503 and "telemetry.csv columns" in msg["detail"]
        sentence(msg["detail"])
        write_dataset(root / "alpha")
        msg = recv(ws)
        assert msg["type"] == "tick" and msg["dataset"] == "alpha" and msg["alerts"] is not None


# ---------- robustness ----------

def test_payloads_are_strict_json_even_with_nan_from_a_module(fakes):
    def poison(events):
        out = [replace(e, confidence=float("nan"), level_pct=float("inf"),
                       lost_revenue_per_hour=float("nan")) for e in events]
        for e in out:
            if e.category == "pump_failure" and e.status == "ongoing":
                e.hours_to_limit = {"p50": float("nan")}
        return out
    fakes.mutate = poison
    for path in ("/api/eog/state", "/api/eog/alerts", "/api/eog/events", "/api/eog/plans", "/api/eog/handoff",
                 "/api/eog/network", "/api/eog/clock", "/api/eog/datasets"):
        ok(client.get(path))                       # strict() fails on NaN or Infinity
    events = strict(client.get("/api/eog/events", params={"download": "true"}).text)
    assert events[0]["confidence"] is None and events[0]["level_pct"] is None
    assert events[1]["hours_to_limit"] == {"p50": None}
    pf = ok(client.get("/api/eog/alerts"))["alerts"][0]
    assert pf["countdown"]["level_pct"] is None and "reaches shutdown at" in pf["title"]
    assert "inf" not in ok(client.get("/api/eog/handoff"))["text"]
    assert "inf" not in ok(client.post("/api/eog/plans/EV-0002/approve"))["radio_script"]
    assert service.jsonable({"a": (1.0, float("-inf")), "b": {float("nan")}}) == {"a": [1.0, None], "b": [None]}


def test_service_calls_the_real_detect_and_plan():
    """App code holds no stand-ins and no lazy imports: service calls detect.py and plan.py, and
    asks for no countdown of its own (detect fills each Event's block from plan.countdown)."""
    assert service.detect is real_detect and service.plan is real_plan
    src = Path(service.__file__).read_text(encoding="utf-8")
    for gone in ("importlib", "_module(", "blueprint", "countdown("):
        assert gone not in src, gone


def test_real_modules_behind_the_api(root):
    """No stand-ins: detect.py and plan.py behind every route, on the synthetic week. Pump A faults
    at row 24 with its tank at 55 % and fills at 15 %/h (1800 bpd into 500 bbl), so at row 47
    plan.countdown gives 83.8 % (83.75 to 0.1 %), shutdown (90 %) at row 24 + 140 min, 25 min
    from now. Every countdown the API shows is that one, and the wait curve is plan.wait_curve's."""
    jsonschema = pytest.importorskip("jsonschema")
    view = load(root / "alpha")
    events = real_detect.detect(view)
    pa = next(e for e in events if e.asset == "Z9_PUMP_A" and e.status == "ongoing")
    cd = real_plan.countdown(view, pa)
    assert (cd["level_at_onset_pct"], cd["level_pct"], cd["trips_at"], cd["hours_to_limit"]["p50"], cd["tripped"]) == (
        55.0, 83.8, ts(24) + 140 * 60, 0.42, False)
    block = lambda d: (d["level_pct"], d["hours_to_limit"], d["trips_at"], d["tripped"])   # noqa: E731

    alerts = ok(client.get("/api/eog/alerts"))["alerts"]
    a = next(x for x in alerts if x["asset"] == "Z9_PUMP_A")
    assert block(a["countdown"]) == block(cd) and a["order"] == 1
    assert a["countdown"]["lost_revenue_per_hour"] == pytest.approx(cd["lost_revenue_per_hour"])
    exported = strict(client.get("/api/eog/events", params={"download": 1}).text)
    assert not sorted(jsonschema.Draft202012Validator(EVENTS_SCHEMA).iter_errors(exported), key=str)
    ej = next(e for e in exported if e["asset_id"] == "Z9_PUMP_A" and e["status"] == "ongoing")
    assert (ej["level_pct"], ej["hours_to_limit"]) == (cd["level_pct"], cd["hours_to_limit"])
    assert ej["recommended_actions"] and ej["recommended_actions"][0]["type"] != "field_inspection"
    plans = ok(client.get("/api/eog/plans"))["plans"]
    item = next(p for p in plans if p["event"]["asset"] == "Z9_PUMP_A")
    assert block(item["event"]) == block(cd) and item["event"]["level_at_onset_pct"] == 55.0
    ongoing = [e for e in events if e.category == "pump_failure" and e.status == "ongoing"]
    curve = real_plan.wait_curve(view, ongoing, pa.id)
    assert item["wait_curve"] == service.jsonable(curve) and curve
    assert set(item["wait_curve"][0]) == {"delay_min", "start_ts", "cost", "summary", "trucks_free"}
    assert item["wait_curve"][0]["start_ts"] == view.now and item["wait_curve"][0]["cost"] == item["plan"]["cost"]
    acts = [x for x in item["plan"]["actions"] if x["type"] != "none"]
    w = ok(client.post("/api/eog/whatif", json={"facility_id": "Z9_FAC_A", "actions": acts}))
    assert w["level_pct"] == cd["level_pct"] and w["result"]["cost"] == pytest.approx(item["plan"]["cost"], abs=0.01)
    when = service.clock_text(cd["trips_at"], view.now)
    assert f"reaches shutdown at {when} (in 25 min)" in a["title"]
    assert f"shuts in at {when}, 25 min left." in ok(client.get("/api/eog/handoff"))["text"]
    said = ok(client.post(f"/api/eog/plans/{a['event_id']}/approve"))["radio_script"]
    assert f"With no action it shuts in at {when}." in said
    for path in ("/api/eog/state", "/api/eog/network", "/api/eog/clock", "/api/eog/datasets"):
        ok(client.get(path))
    ok(client.get("/api/eog/plans", params={"mode": "reference"}))


def test_state_survives_a_detection_bug(fakes, monkeypatch, caplog):
    def broken(ds):
        raise ZeroDivisionError("division by zero")
    monkeypatch.setattr(service.detect, "detect", broken)
    for _ in range(3):
        body = ok(client.get("/api/eog/state"))
    assert body["warnings"] == ["Detection failed (ZeroDivisionError: division by zero), " + service.NO_DETECTION + "."]
    assert body["assets"]["Z9_TANK_A"]["readings"]["level_pct"] == 83.75
    assert sum("detection failed" in r.getMessage() for r in caplog.records) == 1     # logged once, not per poll
    loud = TestClient(app, raise_server_exceptions=False)  # the views that need events still fail loudly
    r = loud.get("/api/eog/alerts")
    assert r.status_code == 500 and r.json()["detail"] == "Internal error: ZeroDivisionError: division by zero"


def test_post_routes_use_the_operator_token_when_auth_exists(root, monkeypatch):
    """auth.py is not on this branch; with a stand-in that has require_role, POSTs need the token
    (after the 503 check), GETs stay open, and the approval records auth's device."""
    name = "backend.app.auth"
    saved = sys.modules.get(name)
    fake = types.ModuleType(name)

    def require_role(*roles):
        def dependency(x_freeboard_token: Optional[str] = Header(None)):
            if x_freeboard_token != "letmein":
                raise HTTPException(401, "Enter the operator PIN to send commands.")
            return SimpleNamespace(role="operator", decided_by=lambda by="": "console")
        return dependency
    fake.require_role = require_role
    sys.modules[name] = fake
    try:
        mod = importlib.reload(api)
        guarded = FastAPI()
        guarded.include_router(mod.router)
        c = TestClient(guarded)
        Fakes(SPECS).install(monkeypatch)
        assert c.post("/api/eog/clock", json={"at": ts(3)}).status_code == 401
        assert c.post("/api/eog/clock", json={"at": ts(3)}, headers={"X-Freeboard-Token": "letmein"}).status_code == 200
        assert c.get("/api/eog/alerts").status_code == 200
        r = c.post("/api/eog/plans/EV-0002/approve", json={"by": "Mallory", "at": ts(ROWS - 1)},
                   headers={"X-Freeboard-Token": "letmein"})
        assert r.status_code == 200 and r.json()["approval"]["by"] == "console"
        monkeypatch.delenv("FREEBOARD_EOG_DIR")
        r = c.post("/api/eog/clock", json={})
        assert r.status_code == 503 and r.json()["detail"] == service.OFF   # 503 before the token check
        monkeypatch.setenv("FREEBOARD_EOG_DIR", str(root))

        def closed(problem: str) -> None:
            """An auth.py that can't be used closes the commands (never opens them) and the rest
            of dataset mode keeps working; importing api.py does not raise."""
            mod = importlib.reload(api)
            assert mod.AUTH_PROBLEM == f"Dataset-mode commands are refused because backend/app/auth.py can't be used: {problem}."
            app2 = FastAPI()
            app2.include_router(mod.router)
            c2 = TestClient(app2)
            for path in ("/api/eog/clock", "/api/eog/datasets/activate", "/api/eog/whatif", "/api/eog/plans/EV-0002/approve"):
                r = c2.post(path, json={}, headers={"X-Freeboard-Token": "letmein"})
                assert r.status_code == 503 and r.json()["detail"] == mod.AUTH_PROBLEM, (path, r.text)
            assert c2.get("/api/eog/alerts").status_code == 200

        del fake.require_role
        closed("it has no require_role")

        class BrokenAuth(importlib.abc.MetaPathFinder, importlib.abc.Loader):
            def find_spec(self, fullname, path, target=None):
                return importlib.util.spec_from_loader(fullname, self) if fullname == name else None

            def create_module(self, spec):
                return None

            def exec_module(self, module):
                raise RuntimeError("bad config")
        sys.modules.pop(name, None)
        finder = BrokenAuth()
        sys.meta_path.insert(0, finder)
        try:
            closed("it fails to import (RuntimeError: bad config)")
        finally:
            sys.meta_path.remove(finder)
    finally:
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved
        importlib.reload(api)
    assert importlib.reload(api).AUTH_PROBLEM is None     # no auth.py on this branch: open, no problem


def test_dataset_mode_code_never_reads_the_system_clock_or_answer_keys():
    for mod in (service, api):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for banned in ("time.time(", "datetime.now(", "utcnow(", "date.today(", "answer_key"):
            assert banned not in src, (mod.__name__, banned)


# ---------- the sponsor data (skipped unless FREEBOARD_EOG_DIR points at it) ----------

@pytest.fixture
def real(monkeypatch) -> Path:
    base = Path(REAL_ROOT) if REAL_ROOT else None
    if base is None or not (base / "history" / "full_week" / "telemetry.csv").is_file():
        pytest.skip("FREEBOARD_EOG_DIR does not point at the sponsor repo")
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    return base


def _last_ts(folder: Path) -> int:
    with open(folder / "telemetry.csv", "rb") as f:
        f.seek(-400, os.SEEK_END)
        return int(f.read().decode().strip().splitlines()[-1].split(",")[1])


def test_real_datasets_clock_and_cuts(real):
    body = ok(client.get("/api/eog/datasets"))
    ids = [d["id"] for d in body["datasets"]]
    assert "history/full_week" in ids and sum(i.startswith("history/snapshot_") for i in ids) == 3
    assert body["active"] == ("test" if "test" in ids else "history/full_week")
    for ds_id in ids:
        c = ok(client.post("/api/eog/datasets/activate", json={"id": ds_id}))["clock"]
        last = _last_ts(real / ds_id)
        assert c["last"] == c["now"] == c["at"] == last, ds_id
        assert c["start"] == ok(client.get("/api/eog/series", params={
            "asset_id": ok(client.get("/api/eog/network"))["facilities"][0]["tank"]["id"],
            "metric": "level_pct", "max_points": 20000}))["points"][0][0]
        cut = ds_id.rsplit("_", 1)[-1]
        if "T" in cut and ds_id != "history/full_week":   # snapshot_2026-10-08T1200: "now" is the cut
            assert c["last_ts"] == f"{cut[:13]}:{cut[13:15]}:00Z", ds_id


def test_real_network_matches_the_files(real):
    net = json.loads((real / "history/full_week/network.json").read_text(encoding="utf-8"))
    ops = json.loads((real / "history/full_week/ops.json").read_text(encoding="utf-8"))
    ok(client.post("/api/eog/datasets/activate", json={"id": "history/full_week"}))
    body = ok(client.get("/api/eog/network"))
    facs = {f["id"]: f for f in body["facilities"]}
    wells = {w["id"]: w for w in body["wells"]}
    assert [s["id"] for s in body["systems"]] == [s["id"] for s in net["systems"]]
    for s, sb in zip(net["systems"], body["systems"]):
        assert sb["fleet_size"] == ops["systems"][s["id"]]["emergency_fleet_size"]
        assert sb["disposal"]["pressure_psi"] == s["disposal_site"]["pressure_psi"]
        for f in s["facilities"]:
            fb, fo = facs[f["id"]], ops["systems"][s["id"]]["facilities"][f["id"]]
            assert fb["tank"]["capacity_bbl"] == f["tank"]["capacity_bbl"]
            assert fb["pump"]["max_flow_bpd"] == f["pump"]["max_flow_bpd"]
            assert (fb["response_min"], fb["cycle_min"]) == (fo["response_min"], fo["cycle_min"])
            assert fb["tank"]["shutdown_pct"] == net["defaults"]["tank_limits_pct"]["shutdown"]
            for w in f["wells"]:
                day = w["oil_bpd"] * ops["prices"]["oil_per_bbl"] + w["gas_mcfd"] * ops["prices"]["gas_per_mcf"]
                assert wells[w["id"]]["revenue_per_day"] == pytest.approx(day)
                assert wells[w["id"]]["cost_per_bbl_avoided"] == pytest.approx(day / w["baseline_water_bpd"])
    assert len(facs) == sum(len(s["facilities"]) for s in net["systems"])


def test_real_time_travel_matches_the_snapshots(real, monkeypatch):
    """A snapshot is the full week cut short, so full_week viewed at a snapshot's "now" must
    read exactly like the snapshot itself."""
    Fakes([]).install(monkeypatch)
    snaps = [d["id"] for d in ok(client.get("/api/eog/datasets"))["datasets"] if "snapshot_" in d["id"] or "mock_test_" in d["id"]]
    assert len(snaps) == 4
    for snap in snaps:
        ok(client.post("/api/eog/datasets/activate", json={"id": snap}))
        cut = ok(client.get("/api/eog/clock"))["last"]
        mine = ok(client.get("/api/eog/state"))
        tank = next(a for a, v in mine["assets"].items() if v["kind"] == "tank")
        s1 = ok(client.get("/api/eog/series", params={"asset_id": tank, "max_points": 300}))
        ok(client.post("/api/eog/datasets/activate", json={"id": "history/full_week"}))
        week = ok(client.get("/api/eog/state", params={"at": cut}))
        s2 = ok(client.get("/api/eog/series", params={"asset_id": tank, "max_points": 300, "at": cut}))
        assert week["now"] == cut and mine["now"] == cut
        assert week["assets"] == mine["assets"], snap
        assert s1["metrics"] == s2["metrics"], snap


def test_real_fault_spikes_survive_downsampling(real):
    ok(client.post("/api/eog/datasets/activate", json={"id": "history/full_week"}))
    pumps = [f["pump"]["id"] for f in ok(client.get("/api/eog/network"))["facilities"]]
    faulted = 0
    for pump in pumps:
        full = ok(client.get("/api/eog/series", params={"asset_id": pump, "metric": "status", "max_points": 5000}))
        small = ok(client.get("/api/eog/series", params={"asset_id": pump, "metric": "status", "max_points": 20}))
        assert len(full["points"]) == full["raw_points"] == 2016 and len(small["points"]) <= 20
        assert max(v for _, v in small["points"]) == max(v for _, v in full["points"])
        assert min(v for _, v in small["points"]) == min(v for _, v in full["points"])
        faulted += max(v for _, v in full["points"]) == 2
    assert faulted >= 1


def test_real_routes_answer_strict_json(real, monkeypatch):
    Fakes([]).install(monkeypatch)
    for ds_id in [d["id"] for d in ok(client.get("/api/eog/datasets"))["datasets"]]:
        ok(client.post("/api/eog/datasets/activate", json={"id": ds_id}))
        for path in ("/api/eog/state", "/api/eog/alerts", "/api/eog/events", "/api/eog/plans",
                     "/api/eog/plans?mode=reference", "/api/eog/handoff", "/api/eog/network"):
            ok(client.get(path))
        events = strict(client.get("/api/eog/events?download=1").text)
        assert all(e["category"] in ("pump_failure", "pump_degradation", "pipeline_leak", "other") for e in events)
        for alert in ok(client.get("/api/eog/alerts"))["alerts"]:
            ok(client.post(f"/api/eog/plans/{alert['event_id']}/approve"))
            if alert["category"] == "pump_failure":
                ok(client.post("/api/eog/whatif", json={"facility_id": alert["facility"],
                                                        "actions": [{"type": "dispatch_truck", "trucks": 1}]}))


# ---------- the sponsor data with the labelled events (answer keys are test fixtures only) ----------

@pytest.fixture
def labelled(real) -> list[dict]:
    path = real / "history" / "full_week" / "answer_key.json"
    if not path.is_file():
        pytest.skip("the full week's answer key is not in the sponsor repo")
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_key(real: Path, ds_id: str) -> Optional[list]:
    path = real / ds_id / "answer_key.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def labelled_events(key: list[dict], ds) -> list[Event]:
    """The labelled events with onset at or before the view's now (a snapshot is the week cut
    short, so one key serves every folder), numbered in onset order, with no countdown."""
    out = []
    for e in key:
        if e["onset_unix"] > ds.now:
            continue
        end = e["end_unix"] if e["end_unix"] is not None and e["end_unix"] <= ds.now else None
        out.append(Event(id="", category=e["category"], system=e["system"], facility=e["facility"], asset=e["asset"],
                         onset=e["onset_unix"], end=end, status="resolved" if end else "ongoing", confidence=0.9,
                         evidence=[f"labelled {e['id']}"], detected_at=e["onset_unix"]))
    for n, ev in enumerate(out, 1):
        ev.id = f"EV-{n:04d}"
    return out


class Oracle(Fakes):
    """A perfect detector: the labelled events, each pump failure with the countdown block the
    real detect.countdown fills from plan.countdown, as detect.detect does. Plans are the plain
    stand-ins."""

    def __init__(self, key: list[dict]):
        super().__init__([])
        self.key = key

    def detect(self, ds):
        self.calls["detect"] += 1
        out = labelled_events(self.key, ds)
        for ev in out:
            real_detect.countdown(ds, ev)
        return out


_ACTION = {"oneOf": [
    {"type": "object", "required": ["type", "facility_id", "trucks", "trips_per_truck"], "additionalProperties": False,
     "properties": {"type": {"const": "dispatch_truck"}, "facility_id": {"type": "string"},
                    "trucks": {"type": "integer", "minimum": 1}, "trips_per_truck": {"type": "integer", "minimum": 1}}},
    {"type": "object", "required": ["type", "wells"], "additionalProperties": False,
     "properties": {"type": {"const": "shut_in_wells"}, "wells": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    "start_after_min": {"type": "number", "minimum": 0}}},
    {"type": "object", "required": ["type", "asset_id"], "additionalProperties": False,
     "properties": {"type": {"const": "field_inspection"}, "asset_id": {"type": "string"}}},
    {"type": "object", "required": ["type"], "additionalProperties": False, "properties": {"type": {"const": "none"}}},
]}
_TS = {"type": "string", "pattern": r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"}
EVENTS_SCHEMA = {   # SPEC.md "events.json (optional)"
    "type": "array",
    "items": {
        "type": "object",
        "required": ["event_id", "system_id", "facility_id", "asset_id", "category", "status", "onset_ts", "onset_unix",
                     "end_ts", "end_unix", "confidence", "evidence", "level_pct", "hours_to_limit", "recommended_actions"],
        "properties": {
            "event_id": {"type": "string", "pattern": r"^EV-\d{4}$"},
            "system_id": {"type": "string"}, "facility_id": {"type": "string"}, "asset_id": {"type": "string"},
            "category": {"enum": ["pump_degradation", "pump_failure", "pipeline_leak", "other"]},
            "status": {"enum": ["resolved", "ongoing"]},
            "onset_ts": _TS, "onset_unix": {"type": "integer"},
            "end_ts": {"oneOf": [_TS, {"type": "null"}]}, "end_unix": {"type": ["integer", "null"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "level_pct": {"type": ["number", "null"]},
            "hours_to_limit": {"oneOf": [{"type": "null"}, {
                "type": "object", "required": ["p50"],
                "properties": {k: {"type": "number"} for k in ("p50", "p10", "p90")}}]},
            "recommended_actions": {"type": "array", "minItems": 1, "items": _ACTION},
            "est_cost": {"type": "number"},
        },
        "allOf": [
            {"if": {"properties": {"status": {"const": "resolved"}}},
             "then": {"properties": {"end_ts": _TS, "end_unix": {"type": "integer"}}},
             "else": {"properties": {"end_ts": {"type": "null"}, "end_unix": {"type": "null"}}}},
            {"if": {"properties": {"category": {"const": "pump_failure"}, "status": {"const": "ongoing"}}},
             "else": {"properties": {"hours_to_limit": {"type": "null"}}}},
        ],
    },
}


def test_real_views_with_the_labelled_events(real, labelled, monkeypatch):
    """Every folder, with the events a perfect detector would report: the alerts list exactly
    the ongoing events, pump failures first in the snapshot keys' `order`, with the keys'
    countdown, level and lost revenue; events.json passes SPEC's schema; every alert can be
    approved and every ongoing failure replayed; and the full week viewed at a snapshot's now
    gives exactly the snapshot's events."""
    jsonschema = pytest.importorskip("jsonschema")
    Oracle(labelled).install(monkeypatch)
    check = jsonschema.Draft202012Validator(EVENTS_SCHEMA)
    ids = [d["id"] for d in ok(client.get("/api/eog/datasets"))["datasets"]]
    exports, checked_order = {}, 0
    for ds_id in ids:
        ok(client.post("/api/eog/datasets/activate", json={"id": ds_id}))
        now = ok(client.get("/api/eog/clock"))["now"]
        ongoing = [e for e in labelled if e["onset_unix"] <= now and (e["end_unix"] is None or e["end_unix"] > now)]
        alerts = ok(client.get("/api/eog/alerts"))["alerts"]
        assert sorted(a["asset"] for a in alerts) == sorted(e["asset"] for e in ongoing), ds_id
        kinds = [a["category"] == "pump_failure" for a in alerts]
        assert kinds == sorted(kinds, reverse=True), ds_id
        assert [a["order"] for a in alerts] == list(range(1, len(alerts) + 1))
        key = snapshot_key(real, ds_id)
        if key and ds_id != "history/full_week":
            want = sorted((e for e in key if e.get("order")), key=lambda e: e["order"])
            got = [a for a in alerts if a["category"] == "pump_failure"]
            assert [a["asset"] for a in got] == [e["asset"] for e in want], ds_id
            for a, e in zip(got, want):
                cd = a["countdown"]                       # plan.countdown's, exactly the key's
                assert cd["level_pct"] == e["level_final_pct"], (ds_id, a["asset"])
                assert cd["hours_to_limit"]["p50"] == e["hours_to_limit_final"], (ds_id, a["asset"])
                assert cd["lost_revenue_per_hour"] == pytest.approx(e["lost_revenue_per_hour"])
                assert cd["trips_at"] == e["trips_at_unix"] and a["action"]["type"] == "plan"
                checked_order += 1
            assert [e["id"] for e in key] == [e["id"] for e in labelled if e["onset_unix"] <= now]
        events = strict(client.get("/api/eog/events", params={"download": 1}).text)
        errors = sorted(check.iter_errors(events), key=str)
        assert not errors, (ds_id, [e.message for e in errors[:3]])
        assert [e["asset_id"] for e in events] == [e["asset"] for e in labelled if e["onset_unix"] <= now]
        exports[ds_id] = events
        plans = ok(client.get("/api/eog/plans"))
        assert sorted(p["event"]["asset"] for p in plans["plans"]) == sorted(
            e["asset"] for e in ongoing if e["category"] == "pump_failure")
        ref = ok(client.get("/api/eog/plans", params={"mode": "reference"}))
        assert len(ref["plans"]) == sum(e["category"] == "pump_failure" and e["onset_unix"] <= now for e in labelled)
        for a in alerts:
            body = ok(client.post(f"/api/eog/plans/{a['event_id']}/approve", json={"by": "night shift", "at": now}))
            assert [s["n"] for s in body["steps"]] == list(range(1, len(body["steps"]) + 1))
            assert body["radio_script"].startswith("Field, this is control.") and body["approval"]["by"] == "night shift"
            if a["category"] == "pump_failure":
                w = ok(client.post("/api/eog/whatif", json={"facility_id": a["facility"], "at": now,
                                                            "actions": [{"type": "dispatch_truck", "trucks": 1}]}))
                assert w["event"]["asset"] == a["asset"] and w["result"] and w["baseline"]
        state = ok(client.get("/api/eog/state"))
        for e in ongoing:
            assert any(state["assets"][e["asset"]]["events"]), (ds_id, e["asset"])
            assert state["assets"][e["asset"]]["status"] in ("ALARM", "INSPECT")
    assert checked_order == 7          # 3 + 1 + 3 ongoing pump failures in the three snapshots
    ok(client.post("/api/eog/datasets/activate", json={"id": "history/full_week"}))
    for ds_id, events in exports.items():
        if ds_id != "history/full_week":
            week = strict(client.get("/api/eog/events", params={"download": 1, "at": _last_ts(real / ds_id)}).text)
            assert week == events and events, ds_id


def test_real_handoff_reads_in_thirty_seconds_all_week(real, labelled, monkeypatch):
    """Every half hour of the week (the service directly: TestClient costs ~15 ms a call)."""
    Oracle(labelled).install(monkeypatch)
    ok(client.post("/api/eog/datasets/activate", json={"id": "history/full_week"}))
    c = ok(client.get("/api/eog/clock"))
    worst, n = (0, None), 0
    for at in range(c["start"], c["last"] + 1, 1800):
        body = strict(service.dumps(service.SERVICE.handoff(at)))
        n += 1
        assert body["read_s"] <= 30, (iso(at), body["words"], body["text"])
        for e in labelled:      # every failed pump is named
            if e["category"] == "pump_failure" and e["onset_unix"] <= body["now"] < e["end_unix"]:
                assert e["asset"] in body["text"], (iso(at), e["asset"])
        worst = max(worst, (body["words"], iso(at)))
    assert n == 336 and worst[0] <= service.HANDOFF_WORDS, worst
    assert ok(client.get("/api/eog/handoff", params={"at": worst[1]}))["words"] == worst[0]


def test_real_state_alarms_match_the_labelled_pump_failures(real, labelled, monkeypatch):
    """With detection failing (statuses from the readings alone), a pump is ALARM exactly while
    a labelled pump failure is ongoing on it, and a well is ALARM only inside one."""
    def offline(ds):
        raise RuntimeError("detector offline")
    monkeypatch.setattr(service, "detect", SimpleNamespace(detect=offline))
    windows: dict = {}
    for e in labelled:
        if e["category"] == "pump_failure":
            windows.setdefault(e["facility"], []).append((e["onset_unix"], e["end_unix"]))
    ok(client.post("/api/eog/datasets/activate", json={"id": "history/full_week"}))
    ds = service.SERVICE.dataset()
    hits = misses = false = wells_outside = 0
    for i in range(0, ds.end, 3):
        t = int(ds.times[i])
        body = service.SERVICE.state(t)
        assert body["warnings"] and body["events"] == {}
        assets = body["assets"]
        for f in ds.facilities.values():
            inside = any(a <= t < b for a, b in windows.get(f.id, []))
            alarm = assets[f.pump_id]["status"] == "ALARM"
            hits += inside and alarm
            misses += inside and not alarm
            false += alarm and not inside
            wells_outside += sum(assets[w]["status"] == "ALARM" for w in f.well_ids) if not inside else 0
    assert (misses, false, wells_outside) == (0, 0, 0) and hits == 13 * 24


def test_real_network_revenue_matches_the_answer_keys(real):
    ok(client.post("/api/eog/datasets/activate", json={"id": "history/full_week"}))
    body = ok(client.get("/api/eog/network"))
    facs = {f["id"]: f for f in body["facilities"]}
    wells = {w["id"]: w for w in body["wells"]}
    n = 0
    for ds_id in ("history/snapshot_2026-10-08T1200", "history/snapshot_2026-10-09T0330",
                  "history/snapshot_2026-10-10T0900"):
        for e in snapshot_key(real, ds_id) or []:
            if e.get("lost_revenue_per_hour") is not None:
                assert facs[e["facility"]]["revenue_per_hour"] == pytest.approx(e["lost_revenue_per_hour"])
                n += 1
    assert n == 7
    # README "Opening Scene": S1_WELL_C2 sells $12,000 a day, $500 an hour.
    assert (wells["S1_WELL_C2"]["revenue_per_day"], wells["S1_WELL_C2"]["revenue_per_hour"]) == (12000, 500)


def test_real_planner_behind_the_api_with_the_labelled_events(real, labelled, monkeypatch):
    """The real plan.py with a detector that reports the labelled events with no countdown, so
    plan_failures fills it: the snapshot keys' alert order and countdown come out of the API,
    every pump failure gets a reference plan, and replaying each recommended plan through
    /whatif costs what the plan says."""
    monkeypatch.setattr(service, "detect", SimpleNamespace(detect=lambda ds: labelled_events(labelled, ds)))
    checked = replayed = 0
    for ds_id in [d["id"] for d in ok(client.get("/api/eog/datasets"))["datasets"]]:
        ok(client.post("/api/eog/datasets/activate", json={"id": ds_id}))
        now = ok(client.get("/api/eog/clock"))["now"]
        ref = ok(client.get("/api/eog/plans", params={"mode": "reference"}))["plans"]
        assert len(ref) == sum(e["category"] == "pump_failure" and e["onset_unix"] <= now for e in labelled)
        assert all(it["plan"] and it["plan"]["summary"] for it in ref), ds_id
        alerts = ok(client.get("/api/eog/alerts"))["alerts"]
        key = snapshot_key(real, ds_id)
        if key and ds_id != "history/full_week":
            want = sorted((e for e in key if e.get("order")), key=lambda e: e["order"])
            got = [a for a in alerts if a["category"] == "pump_failure"]
            assert [a["asset"] for a in got] == [e["asset"] for e in want], ds_id
            for a, e in zip(got, want):
                cd = a["countdown"]
                assert cd["level_pct"] == e["level_final_pct"], (ds_id, a["asset"])
                assert cd["hours_to_limit"]["p50"] == e["hours_to_limit_final"], (ds_id, a["asset"])
                assert cd["trips_at"] == e["trips_at_unix"], (ds_id, a["asset"])
                checked += 1
        for item in ok(client.get("/api/eog/plans"))["plans"]:
            p = item["plan"]
            acts = [a for a in p["actions"] if a["type"] != "none"]
            w = ok(client.post("/api/eog/whatif", json={"facility_id": item["event"]["facility"], "actions": acts}))
            assert w["result"]["cost"] == pytest.approx(p["cost"], abs=0.01), (ds_id, item["event"]["id"])
            replayed += 1
    assert checked == 7 and replayed >= checked


def test_real_detect_and_plan_behind_the_api_match_the_snapshot_keys(real):
    """No stand-ins, every history dataset: every route answers strict JSON and the alerts list
    pump failures first, soonest shutdown first. Every countdown the alerts, events.json and the
    now plans show is plan.countdown's for that event, and in each snapshot it is the key's
    (order, level_final_pct, hours_to_limit_final, trips_at, lost revenue). Wait-curve rows are
    plan.wait_curve's, and replaying each recommended plan through /whatif costs what it says."""
    jsonschema = pytest.importorskip("jsonschema")
    check = jsonschema.Draft202012Validator(EVENTS_SCHEMA)
    block = lambda d: (d["level_pct"], d["hours_to_limit"], d["trips_at"], d["tripped"])   # noqa: E731
    checked = compared = 0
    for ds_id in [d["id"] for d in ok(client.get("/api/eog/datasets"))["datasets"]]:
        ok(client.post("/api/eog/datasets/activate", json={"id": ds_id}))
        for path in ("/api/eog/state", "/api/eog/handoff", "/api/eog/network"):
            ok(client.get(path))
        ok(client.get("/api/eog/plans", params={"mode": "reference"}))
        alerts = ok(client.get("/api/eog/alerts"))["alerts"]
        kinds = [a["category"] == "pump_failure" for a in alerts]
        assert kinds == sorted(kinds, reverse=True), ds_id
        hours = [(a["countdown"]["hours_to_limit"] or {}).get("p50") for a in alerts if a["category"] == "pump_failure"]
        known = [h for h in hours if h is not None]
        assert known == sorted(known), (ds_id, hours)
        exported = {e["event_id"]: e for e in strict(client.get("/api/eog/events", params={"download": 1}).text)}
        assert not sorted(check.iter_errors(list(exported.values())), key=str), ds_id
        plans = {it["event"]["id"]: it for it in ok(client.get("/api/eog/plans"))["plans"]}
        _, view, _ = service.SERVICE.view()
        events = {e.id: e for e in service.SERVICE.events(view)}
        ongoing = [e for e in events.values() if e.category == "pump_failure" and e.status == "ongoing"]
        for a in alerts:
            if a["category"] != "pump_failure":
                continue
            cd = real_plan.countdown(view, events[a["event_id"]])
            assert block(a["countdown"]) == block(cd), (ds_id, a["event_id"])
            assert block(plans[a["event_id"]]["event"]) == block(cd), (ds_id, a["event_id"])
            ej = exported[a["event_id"]]
            assert (ej["level_pct"], ej["hours_to_limit"]) == (cd["level_pct"], cd["hours_to_limit"]), ds_id
            curve = plans[a["event_id"]]["wait_curve"]
            assert curve == service.jsonable(real_plan.wait_curve(view, ongoing, a["event_id"])), ds_id
            assert all(set(r) == {"delay_min", "start_ts", "cost", "summary", "trucks_free"} for r in curve)
            compared += 1
        key = snapshot_key(real, ds_id)
        if key and "snapshot_" in ds_id:
            want = sorted((e for e in key if e.get("order")), key=lambda e: e["order"])
            got = [a for a in alerts if a["category"] == "pump_failure"]
            assert [a["asset"] for a in got] == [e["asset"] for e in want], ds_id
            for a, e in zip(got, want):
                c = a["countdown"]
                assert (c["level_pct"], c["hours_to_limit"]["p50"], c["trips_at"]) == (
                    e["level_final_pct"], e["hours_to_limit_final"], e["trips_at_unix"]), (ds_id, e["id"])
                assert c["lost_revenue_per_hour"] == pytest.approx(e["lost_revenue_per_hour"])
                assert exported[a["event_id"]]["hours_to_limit"]["p50"] == e["hours_to_limit_final"]
                checked += 1
        for item in plans.values():
            p = item["plan"]
            if p is None:
                continue
            acts = [a for a in p["actions"] if a["type"] != "none"]
            w = ok(client.post("/api/eog/whatif", json={"facility_id": item["event"]["facility"], "actions": acts}))
            assert w["result"]["cost"] == pytest.approx(p["cost"], abs=0.01), (ds_id, item["event"]["id"])
    assert checked == 7 and compared >= checked
