"""CF-14 readings history: GET /api/history on the live clock, null for stale periods,
never the last value carried forward. Drives the live clock by hand (live.step)."""
from collections import deque
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import history, main, now
from backend.app.main import app
from backend.app.scenario import load_scenario

client = TestClient(app)


@pytest.fixture(autouse=True)
def live(monkeypatch):
    for var in ("FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FREEBOARD_ONE_CLOCK", "1")
    lv = app.state.live
    app.state.live_reload(load_scenario())
    history.recorder().reset()
    yield lv
    app.state.live_reload(load_scenario())
    history.recorder().reset()
    main._plans.clear()
    now.set_static(main.SCENARIO)


def advance(lv, minutes: float, dt: float = 1.0) -> None:
    for _ in range(int(round(minutes / dt))):
        lv.step(dt)
        history.recorder().sync(lv)   # what the lifespan task does every 0.5 s


def get(**params) -> dict:
    r = client.get("/api/history", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def series(out: dict, aid: str, sig: str = "level_pct") -> list:
    return next(s["points"] for s in out["series"] if s["asset_id"] == aid and s["signal"] == sig)


def test_history_is_on_the_live_clock_and_ends_now(live):
    advance(live, 10)
    out = get(minutes=10, step_min=1)
    st = client.get("/api/state").json()
    assert out["clock_source"] == "live" and out["clock"] == st["clock"]
    assert [p["t_min"] for p in out["timeline"]] == list(range(-10, 1))
    tanks = {t["id"] for t in load_scenario()["field"]["tanks"]}
    assert all(set(p["tanks"]) == tanks for p in out["timeline"])
    for tid in tanks:   # the newest point is the reading the console shows now
        assert out["timeline"][-1]["tanks"][tid] == pytest.approx(st["assets"][tid]["level_pct"], abs=0.06)
    assert out["timeline"][-1]["clock"] == st["clock"]
    assert all(v is not None for p in out["timeline"] for v in p["tanks"].values())
    assert out["gaps"] == []


def test_a_stale_period_is_null_and_never_the_last_value_carried_forward(live):
    advance(live, 3)
    live.registry.sim.inject({"asset_id": "TANK_5", "type": "SENSOR_STALE"})
    advance(live, 15)
    out = get(minutes=18, step_min=1)
    pts = series(out, "TANK_5")
    last_reading = max(t for t, _ in history.recorder().readings(("TANK_5", "level_pct")))
    died = last_reading - float(live.t)                      # minutes ago, <= 0
    stale_after = out["stale_after_min"]
    for tm, v in pts:
        if tm > died + stale_after + 1e-6:
            assert v is None, (tm, v)
    assert pts[-1][1] is None
    # Exactly the live layer's freshness rule: held no longer than stale_after_min.
    held = [v for tm, v in pts if died < tm <= died + stale_after + 1e-6]
    assert len(held) <= stale_after + 1
    gap = next(g for g in out["gaps"] if g["asset_id"] == "TANK_5")
    assert gap["to_t_min"] == 0 and gap["from_t_min"] <= died + stale_after + 1
    st = client.get("/api/state").json()["assets"]["TANK_5"]
    assert st["level_pct"] is None   # history and state agree: unknown now
    # Other tanks keep reporting.
    assert all(v is not None for _, v in series(out, "TANK_1"))


def test_before_the_first_reading_is_null_not_a_guess(live):
    advance(live, 5)
    out = get(minutes=30, step_min=5)
    assert out["since_t_min"] == pytest.approx(-5, abs=1)
    first = out["timeline"][0]
    assert first["t_min"] == -30 and all(v is None for v in first["tanks"].values())
    assert out["gaps"] == [], "time before recording started is not a stale gap"


def test_activating_a_blueprint_starts_the_history_over(live):
    advance(live, 6)
    assert any(v is not None for _, v in series(get(minutes=6), "TANK_3")[:-2])
    assert client.post("/api/blueprints/default/activate").status_code == 200
    advance(live, 2)
    pts = series(get(minutes=6), "TANK_3")
    assert all(v is None for tm, v in pts if tm < -2.5), pts
    assert pts[-1][1] is not None


def test_filters_and_any_signal(live):
    advance(live, 4)
    out = get(minutes=4, asset_id="tank_3,PUMP_3", signal="level_pct,running")
    keys = {(s["asset_id"], s["signal"]) for s in out["series"]}
    assert keys == {("TANK_3", "level_pct"), ("PUMP_3", "running")}
    assert set(out["timeline"][0]["tanks"]) == {"TANK_3"}
    running = [v for _, v in series(out, "PUMP_3", "running")]
    assert running[-1] in (False, 0, "OFF", "STOPPED"), "Pump 3 tripped at the start of the demo"
    no_levels = get(minutes=4, asset_id="PUMP_3", signal="flow_bbl_hr")
    assert no_levels["timeline"] == [] and len(no_levels["series"]) == 1
    every = get(minutes=2, signal="all")
    assert {"level_pct", "running", "position"} <= {s["signal"] for s in every["series"]}


def test_bad_requests_say_what_to_fix(live):
    assert client.get("/api/history", params={"minutes": 0}).status_code == 422
    assert client.get("/api/history", params={"step_min": 0.01}).status_code == 422
    r = client.get("/api/history", params={"minutes": 1440, "step_min": 0.1})
    assert r.status_code == 422 and "at most" in r.json()["detail"]
    r = client.get("/api/history", params={"asset_id": "TANK_99"})
    assert r.status_code == 404 and "TANK_99" in r.json()["detail"]


def test_no_live_layer_means_no_readings(monkeypatch):
    monkeypatch.setenv("FREEBOARD_ONE_CLOCK", "0")
    now.set_static(load_scenario())
    out = get()
    assert out["clock_source"] == "static" and out["timeline"] == [] and out["series"] == []
    assert "No live layer" in out["note"]


def test_history_route_changes_nothing(live):
    advance(live, 2)
    before = client.get("/api/state").json()
    get(minutes=2, signal="all")
    assert client.get("/api/state").json()["assets"] == before["assets"]
    assert client.post("/api/history").status_code == 405


# ---------- the recorder on its own ----------

def _fake_live(maxlen=3):
    return SimpleNamespace(t=0.0, registry=object(), history={("TANK_1", "level_pct"): deque(maxlen=maxlen)})


def test_recorder_keeps_more_than_the_live_buffer_and_drops_duplicates():
    rec = history.Recorder(retention_min=100)
    lv = _fake_live(maxlen=3)
    buf = lv.history[("TANK_1", "level_pct")]
    for k in range(10):
        lv.t = float(k)
        buf.append((float(k), 50.0 + k))
        rec.sync(lv)
        rec.sync(lv)            # a second pass with nothing new adds nothing
    got = rec.readings(("TANK_1", "level_pct"))
    assert [t for t, _ in got] == [float(k) for k in range(10)]


def test_recorder_prunes_past_retention_and_restarts_on_a_new_registry():
    rec = history.Recorder(retention_min=5)
    lv = _fake_live(maxlen=100)
    buf = lv.history[("TANK_1", "level_pct")]
    for k in range(20):
        lv.t = float(k)
        buf.append((float(k), 1.0))
        rec.sync(lv)
    assert min(t for t, _ in rec.readings(("TANK_1", "level_pct"))) >= 19 - 5
    lv.registry = object()      # what reload() does
    lv.history = {("TANK_9", "level_pct"): deque([(20.0, 3.0)])}
    lv.t = 20.0
    rec.sync(lv)
    assert rec.keys() == {("TANK_9", "level_pct")} and rec.started_t == 20.0


def test_resample_holds_a_reading_only_while_fresh():
    readings = [(0.0, 10.0), (1.0, 11.0), (2.0, 12.0)]
    times = [0.0, 1.0, 2.0, 3.0, 6.0, 7.0, 8.0]
    assert history.resample(readings, times, stale_after=5.0) == [10.0, 11.0, 12.0, 12.0, 12.0, 12.0, None]
    assert history.resample([], [0.0, 1.0], 5.0) == [None, None]
    assert history.resample([(5.0, float("nan"))], [5.0], 5.0) == [None]
