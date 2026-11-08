"""/api/dataset/* (backend/app/eog/dataset_api.py): the /dataset page's backend and its 3D.

Synthetic datasets always; the sponsor's when FREEBOARD_EOG_DIR points at a checkout of it.
The answer keys are read here as fixtures only, never by the app.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.eog import dataset_api, detect, plan, service
from backend.app.eog.dataset import load
from backend.app.main import app
from backend.tests.eog_fixture import facility_spec, write_dataset

REAL_ROOT = os.environ.get("FREEBOARD_EOG_DIR", "")   # read before any test changes the env
SECRET = "answer-key-sentinel-9f3a"
ALERT_KEYS = ("event_id", "category", "status", "system_id", "facility_id", "asset_id", "onset_unix", "end_unix",
              "confidence", "evidence", "hours_to_limit", "trips_at_unix", "repair_end_unix", "tripped",
              "lost_revenue_per_hour", "do_nothing_cost", "recommended_actions", "est_cost", "plan")
PLAN_KEYS = ("summary", "actions", "cost", "trip_at", "expires_at")
SPEC_KEYS = ("event_id", "system_id", "facility_id", "asset_id", "category", "status", "onset_ts", "onset_unix",
             "end_ts", "end_unix", "confidence", "evidence", "level_pct", "hours_to_limit", "recommended_actions")
client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    for var in ("FREEBOARD_EOG_DATASET", "FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(dataset_api.ENV_ENGINE, "kit")          # Blender takes ~10 s a system
    monkeypatch.setattr(dataset_api, "CACHE_DIR", tmp_path / "glb")
    monkeypatch.setattr(dataset_api, "PREBUILD", False)
    dataset_api._glb.clear()
    dataset_api._prebuilt.clear()
    service.SERVICE.reset()
    yield
    service.SERVICE.reset()


SYSTEMS_6_11 = {f"S{n}": {"pressure": 900.0 + 40 * n, "facilities": [
    facility_spec("A", [700, 400], 800, 2400, level0=40 + n),
    facility_spec("B", [500, 300], 650, 2000, level0=50)]} for n in range(6, 12)}


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """alpha: Q7/Q8, a pump failure still going at the end; beta: S6-S11, a short calm day."""
    base = tmp_path / "eog"
    write_dataset(base / "alpha", seed=3, hours=24, repair_hours=5,
                  problems=[{"type": "pump_failure", "facility": "Q7_FACILITY_A", "onset_h": 21.5, "level_pct": 60}])
    write_dataset(base / "nested" / "beta", systems=SYSTEMS_6_11, seed=5, hours=4)
    (base / "alpha" / "answer_key.json").write_text(json.dumps({"secret": SECRET}))
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    return base


def ok(r, status: int = 200):
    assert r.status_code == status, r.text
    return r.json()


# ---------- folders, load, files ----------

def test_off_is_503_with_a_sentence(monkeypatch):
    monkeypatch.delenv("FREEBOARD_EOG_DIR", raising=False)
    for url in ("/api/dataset", "/api/dataset/alerts", "/api/dataset/file/network.json",
                "/api/dataset/field.glb?system=S1", "/api/dataset/events.json"):
        r = client.get(url)
        assert r.status_code == 503 and "FREEBOARD_EOG_DIR" in r.json()["detail"], url
    assert client.post("/api/dataset/load", json={"folder": "x"}).status_code == 503


def test_folders_and_load(root):
    d = ok(client.get("/api/dataset"))
    assert sorted(d["folders"]) == ["alpha", "nested/beta"] and d["folder"] in d["folders"]
    d = ok(client.post("/api/dataset/load", json={"folder": "nested/beta"}))
    assert d["folder"] == "nested/beta"
    assert ok(client.get("/api/eog/datasets"))["active"] == "nested/beta"     # the same active dataset
    assert ok(client.get("/api/dataset"))["folder"] == "nested/beta"
    assert client.post("/api/dataset/load", json={"folder": "nope"}).status_code == 404
    assert client.post("/api/dataset/load", json={"folder": ""}).status_code == 422
    assert client.post("/api/dataset/load", content=b"not json").status_code == 422


def test_load_is_same_site_only_but_needs_no_token(root, monkeypatch):
    """The page posts load with plain fetch, so a token can't be required; a cross-site page can't post."""
    monkeypatch.setenv("FREEBOARD_TOKEN", "t0ken-for-test")
    r = client.post("/api/dataset/load", json={"folder": "alpha"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    ok(client.post("/api/dataset/load", json={"folder": "alpha"}, headers={"Origin": "http://localhost:5173"}))
    # the /api/eog command still needs the token
    assert client.post("/api/eog/datasets/activate", json={"id": "alpha"}).status_code == 401


def test_files_byte_for_byte_and_never_the_answer_key(root):
    ok(client.post("/api/dataset/load", json={"folder": "alpha"}))
    for name, ctype in (("network.json", "application/json"), ("ops.json", "application/json"),
                        ("telemetry.csv", "text/csv")):
        r = client.get(f"/api/dataset/file/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith(ctype)
        assert r.content == (root / "alpha" / name).read_bytes()
        assert int(r.headers["content-length"]) == len(r.content)
    for bad in ("answer_key.json", "..%2Fanswer_key.json", "README.md", "blueprint.json"):
        r = client.get(f"/api/dataset/file/{bad}")
        assert r.status_code == 404 and SECRET not in r.text
    for url in ("/api/dataset/alerts", "/api/dataset/events.json", "/api/dataset", "/api/dataset/field_ids?system=Q7"):
        assert SECRET not in client.get(url).text


# ---------- alerts ----------

def test_alerts_shape_order_and_backend_plans(root):
    ok(client.post("/api/dataset/load", json={"folder": "alpha"}))
    d = ok(client.get("/api/dataset/alerts", params={"folder": "alpha"}))
    ds = load(root / "alpha", "alpha")
    assert d["folder"] == "alpha" and d["at"] == ds.last and d["now"] == ds.now
    live = [a for a in d["alerts"] if a["status"] == "ongoing"]
    pumps = [a for a in live if a["category"] == "pump_failure"]
    assert pumps, "the synthetic failure is still going at the end"
    for a in d["alerts"]:
        assert all(k in a for k in ALERT_KEYS), sorted(set(ALERT_KEYS) - set(a))
    assert [a["status"] for a in d["alerts"]] == sorted((a["status"] for a in d["alerts"]),
                                                        key=lambda s: s != "ongoing")
    # The same numbers /api/eog serves: plan, cost, countdown (plan.countdown) and order.
    e = ok(client.get("/api/eog/alerts"))
    assert [a["event_id"] for a in live] == [a["event_id"] for a in e["alerts"]]
    view = ds.at(ds.last)
    events = {ev.id: ev for ev in detect.detect(view)}
    for a in pumps:
        mine = next(x for x in e["alerts"] if x["event_id"] == a["event_id"])
        assert a["plan"]["summary"] == mine["action"]["summary"] and a["plan"]["cost"] == mine["action"]["cost"]
        assert a["est_cost"] == round(mine["action"]["cost"], 2)
        assert a["hours_to_limit"] == mine["countdown"]["hours_to_limit"]
        assert a["hours_to_limit"] == plan.countdown(view, events[a["event_id"]])["hours_to_limit"]
        assert a["trips_at_unix"] == mine["countdown"]["trips_at"]
        assert a["repair_end_unix"] == mine["countdown"]["repair_end"]
        assert all(k in a["plan"] for k in PLAN_KEYS)
        assert a["recommended_actions"] == a["plan"]["actions"]
        assert a["do_nothing_cost"] is not None and a["do_nothing_cost"] >= a["est_cost"] - 1e-6


def test_folder_is_per_request_and_echoed(root):
    ok(client.post("/api/dataset/load", json={"folder": "alpha"}))
    d = ok(client.get("/api/dataset/alerts", params={"folder": "nested/beta"}))
    assert d["folder"] == "nested/beta"
    assert d["now"] == load(root / "nested" / "beta").last
    assert ok(client.get("/api/dataset"))["folder"] == "alpha"           # the active one did not change
    assert client.get("/api/dataset/alerts", params={"folder": "gamma"}).status_code == 404


def test_at_cuts_equal_dataset_at(root):
    ds = load(root / "alpha", "alpha")
    for at in (ds.start + 3600, ds.last - 3 * 3600, ds.last - 1800 + 17, ds.last + 99999):
        d = ok(client.get("/api/dataset/alerts", params={"folder": "alpha", "at": at}))
        cut = service.clamp_ts(at, ds.start, ds.last)
        view = ds.at(cut)
        assert d["at"] == cut and d["now"] == view.now
        want = {(ev.id, ev.category, ev.onset, ev.status) for ev in detect.detect(view)}
        got = {(a["event_id"], a["category"], a["onset_unix"], a["status"]) for a in d["alerts"]}
        assert got == want
    iso = ok(client.get("/api/dataset/alerts", params={"folder": "alpha", "at": "2027-12-28T01:00:00Z"}))
    assert iso["at"] == service.clamp_ts(service.parse_ts("2027-12-28T01:00:00Z"), ds.start, ds.last)
    assert client.get("/api/dataset/alerts", params={"folder": "alpha", "at": "soon"}).status_code == 422


def test_events_json_is_the_spec_document(root):
    ok(client.post("/api/dataset/load", json={"folder": "alpha"}))
    ds = load(root / "alpha", "alpha")
    at = ds.last - 600
    r = client.get("/api/dataset/events.json", params={"at": at})
    assert r.status_code == 200 and r.headers["content-disposition"] == 'attachment; filename="events.json"'
    doc = r.json()
    assert isinstance(doc, list) and doc
    for ev in doc:
        assert all(k in ev for k in SPEC_KEYS)
        assert (ev["hours_to_limit"] is None) == (ev["category"] != "pump_failure" or ev["status"] != "ongoing")
    eog = client.get("/api/eog/events", params={"at": at, "download": "1"}).json()
    assert doc == eog                                     # one events.json, whichever route


# ---------- 3D ----------

def _systems(folder: Path) -> list[str]:
    return [s["id"] for s in json.loads((folder / "network.json").read_text())["systems"]]


def test_field_glb_builds_and_caches_for_an_unseen_s6_s11_network(root, monkeypatch):
    ok(client.post("/api/dataset/load", json={"folder": "nested/beta"}))
    from ingest import eog_field
    calls = []
    real = eog_field.field_glb
    monkeypatch.setattr(eog_field, "field_glb", lambda *a, **k: calls.append(a[1]) or real(*a, **k))
    systems = _systems(root / "nested" / "beta")
    assert systems == [f"S{n}" for n in range(6, 12)]
    for s in systems:
        r = client.get("/api/dataset/field.glb", params={"system": s})
        assert r.status_code == 200 and r.headers["content-type"] == "model/gltf-binary" and r.content[:4] == b"glTF"
        ids = ok(client.get("/api/dataset/field_ids", params={"system": s}))
        assert ids["system"] == s and f"{s}_TANK_A" in ids["assets"]
    assert calls == systems                               # each built once; field_ids and repeats hit the cache
    assert client.get("/api/dataset/field.glb", params={"system": "S1"}).status_code == 404
    assert client.get("/api/dataset/field.glb").status_code == 422
    assert all((dataset_api.CACHE_DIR).rglob("field.glb"))


def test_activation_prebuilds_every_system(root, monkeypatch):
    monkeypatch.setattr(dataset_api, "PREBUILD", True)
    started = []
    real = dataset_api.prebuild
    monkeypatch.setattr(dataset_api, "prebuild", lambda p: started.append(real(p)) or started[-1])
    ok(client.post("/api/dataset/load", json={"folder": "nested/beta"}))
    threads = [t for t in started if t is not None]
    assert len(threads) == 1
    threads[0].join(120)
    built = sorted(k[1] for k in dataset_api._glb if k[0] == str((root / "nested" / "beta").resolve()))
    assert built == sorted(_systems(root / "nested" / "beta"))
    ok(client.get("/api/dataset"))
    assert [t for t in started if t is not None] == threads     # once per folder


def test_activation_through_api_eog_prebuilds_too(root, monkeypatch):
    """The /dataset page of #157 activates through /api/eog: the Service hook starts the 3D build."""
    monkeypatch.setattr(dataset_api, "PREBUILD", True)
    started = []
    real = dataset_api.prebuild
    monkeypatch.setattr(dataset_api, "prebuild", lambda p: started.append(real(p)) or started[-1])
    ok(client.post("/api/eog/datasets/activate", json={"id": "nested/beta"}))
    threads = [t for t in started if t is not None]
    assert len(threads) == 1
    threads[0].join(120)
    ids = ok(client.get("/api/dataset/field_ids", params={"system": "S11"}))
    assert ids["system"] == "S11"
    built = sorted(k[1] for k in dataset_api._glb if k[0] == str((root / "nested" / "beta").resolve()))
    assert built == sorted(_systems(root / "nested" / "beta"))


def test_blender_failure_falls_back_to_the_kit(root, monkeypatch):
    monkeypatch.setenv(dataset_api.ENV_ENGINE, "blender")
    from ingest import eog_field
    from ingest.eog_network import DatasetError
    real = eog_field.field_glb

    def fake(folder, system, cache_dir=None, engine="kit"):
        if engine == "blender":
            raise DatasetError("Blender not found")
        return real(folder, system, cache_dir=cache_dir, engine=engine)
    monkeypatch.setattr(eog_field, "field_glb", fake)
    ok(client.post("/api/dataset/load", json={"folder": "nested/beta"}))
    r = client.get("/api/dataset/field.glb", params={"system": "S7"})
    assert r.status_code == 200 and r.content[:4] == b"glTF"


def test_blueprint_route(root):
    ok(client.post("/api/dataset/load", json={"folder": "nested/beta"}))
    allbp = ok(client.get("/api/eog/blueprint"))
    assert allbp["dataset"] == "nested/beta" and sorted(allbp["systems"]) == sorted(_systems(root / "nested" / "beta"))
    one = ok(client.get("/api/eog/blueprint", params={"system": "S9"}))
    assert one == allbp["systems"]["S9"]
    assert client.get("/api/eog/blueprint", params={"system": "S1"}).status_code == 404


# ---------- the sponsor's data ----------

@pytest.fixture
def real(monkeypatch) -> Path:
    base = Path(REAL_ROOT) if REAL_ROOT else None
    if base is None or not (base / "history" / "full_week" / "telemetry.csv").is_file():
        pytest.skip("FREEBOARD_EOG_DIR does not point at the sponsor repo")
    monkeypatch.setenv("FREEBOARD_EOG_DIR", str(base))
    return base


def test_sponsor_every_folder_alerts_have_every_key(real):
    for folder in ok(client.get("/api/dataset"))["folders"]:
        d = ok(client.get("/api/dataset/alerts", params={"folder": folder}))
        assert d["folder"] == folder
        for a in d["alerts"]:
            assert all(k in a for k in ALERT_KEYS)
            if a["category"] == "pump_failure" and a["status"] == "ongoing":
                assert a["plan"] and a["plan"]["summary"] and a["est_cost"] is not None
                assert a["hours_to_limit"] is None or set(a["hours_to_limit"]) == {"p50", "p10", "p90"}


def test_sponsor_10_10_snapshot_s4_plans_match_eog(real):
    folder = "history/snapshot_2026-10-10T0900"
    ok(client.post("/api/dataset/load", json={"folder": folder}))
    d = ok(client.get("/api/dataset/alerts", params={"folder": folder}))
    s4 = [a for a in d["alerts"] if a["category"] == "pump_failure" and a["status"] == "ongoing" and a["system_id"] == "S4"]
    assert sorted(a["est_cost"] for a in s4) == [905.0, 975.31, 1215.0]
    e = {a["event_id"]: a for a in ok(client.get("/api/eog/alerts"))["alerts"]}
    for a in s4:
        assert a["plan"]["summary"] == e[a["event_id"]]["action"]["summary"]
        assert a["hours_to_limit"] == e[a["event_id"]]["countdown"]["hours_to_limit"]


def test_sponsor_full_week_field_glb_for_every_system(real):
    ok(client.post("/api/dataset/load", json={"folder": "history/full_week"}))
    for s in _systems(real / "history" / "full_week"):
        r = client.get("/api/dataset/field.glb", params={"system": s})
        assert r.status_code == 200 and r.content[:4] == b"glTF", s
