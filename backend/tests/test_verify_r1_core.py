"""Verification round 1 (#126 core): /api/state never shows a stale or unvouched value as
current, an impossible level is no level, and /api/history turns null the moment a
connector stops vouching for a signal."""
import copy
import math
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app import history, main, now, spacetime_mirror
from backend.app.connectors import KINDS
from backend.app.connectors.base import Connector
from backend.app.forecast import build_plant
from backend.app.live import LiveField
from backend.app.main import app
from backend.app.planner import make_plans
from backend.app.scenario import load_scenario

client = TestClient(app)


class Scripted(Connector):
    """A field connector the test drives: it reads `value` for every declared signal while
    `good`, and keeps a quality dict like the #126 connectors."""

    kind = "scripted"

    def __init__(self, spec, fmap, now_fn):
        super().__init__(spec, fmap, now_fn)
        self.values = {key: 51.7 for key in self.signals}
        self.good = True
        self.quality = {key: {"quality": "good", "detail": "", "t": 0.0} for key in self.signals}

    def read(self):
        if not self.good:
            return []
        out = []
        for (aid, sig), v in self.values.items():
            self.quality[(aid, sig)] = {"quality": "good", "detail": "", "t": round(self.now(), 3)}
            out.append(self.reading(aid, sig, v))
        return out

    def drop(self):
        self.good = False
        for key in self.quality:
            self.quality[key] = {"quality": "comm_lost", "detail": "link down", "t": round(self.now(), 3)}


SPEC = {"id": "CONN_SCRIPT", "kind": "scripted",
        "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "a"},
                    {"asset_id": "PUMP_6", "signal": "running", "address": "b"},
                    {"asset_id": "PUMP_6", "signal": "flow_bbl_hr", "address": "c"},
                    {"asset_id": "WELL_21", "signal": "flow_bbl_hr", "address": "d"}]}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for var in ("FREEBOARD_TOKEN", "OPERATOR_PIN", "FREEBOARD_PHONE_TOKEN", "JARVIS_CONFIRM", "NTFY_TOPIC"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FREEBOARD_ONE_CLOCK", "1")
    monkeypatch.setitem(KINDS, "scripted", Scripted)


def live_with(spec=None):
    sc = copy.deepcopy(load_scenario())
    if spec:
        sc["connectors"] = [copy.deepcopy(spec)]
    lf = LiveField(sc)
    lf.step(1.0)
    return lf, SimpleNamespace(state=SimpleNamespace(live=lf))


def conn(lf, cid):
    return next(c for c in lf.registry.connectors if c.id == cid)


# ---------- /api/state: nothing stale or unvouched shows as current ----------

def test_api_state_shows_no_stale_value_as_current():
    lf, app_ = live_with()
    lf.step(1.0)
    stale = [("PUMP_2", "running"), ("PUMP_2", "flow_bbl_hr"), ("WELL_5", "flow_bbl_hr"), ("VALVE_D_2", "position")]
    lf.detector.stale_keys.update(stale)          # what the simulator's SENSOR_STALE fault leads to
    st = now.state(app_)["assets"]
    shown = {f"{a}.{s}": st[a][s] for a, s in stale if st[a].get(s) is not None}
    assert not shown, f"/api/state shows these stale readings as current values: {shown}"
    for a, s in stale:
        assert st[a]["status"] == "UNKNOWN" and st[a]["stale"] is True
        assert st[a][f"last_{s}"] is not None
    assert st["PUMP_1"]["running"] is not None          # fresh ones are untouched


def test_api_state_shows_no_comm_lost_value_as_current():
    lf, app_ = live_with(SPEC)
    c = conn(lf, "CONN_SCRIPT")
    c.values.update({("PUMP_6", "running"): True, ("PUMP_6", "flow_bbl_hr"): 215.0, ("WELL_21", "flow_bbl_hr"): 39.0})
    lf.step(0.5)
    assert now.state(app_)["assets"]["PUMP_6"]["running"] is True
    c.drop()
    lf.step(0.5)
    st = now.state(app_)["assets"]
    got = {"PUMP_6.running": st["PUMP_6"]["running"], "PUMP_6.flow_bbl_hr": st["PUMP_6"]["flow_bbl_hr"],
           "WELL_21.flow_bbl_hr": st["WELL_21"]["flow_bbl_hr"], "TANK_6.level_pct": st["TANK_6"]["level_pct"]}
    assert all(v is None for v in got.values()), f"/api/state, quality comm_lost, still shows: {got}"
    assert st["PUMP_6"]["last_running"] is True and st["PUMP_6"]["last_flow_bbl_hr"] == 215.0
    assert st["WELL_21"]["last_flow_bbl_hr"] == 39.0 and st["TANK_6"]["last_level_pct"] == 51.7
    assert all(st[a]["status"] == "UNKNOWN" for a in ("PUMP_6", "WELL_21", "TANK_6"))


# ---------- impossible levels ----------

@pytest.mark.parametrize("raw", [-20.0, float("nan"), float("inf"), 130.0])
def test_one_clock_takes_no_impossible_level_as_real(raw):
    hook = {"id": "CONN_HOOK", "kind": "webhook", "config": {},
            "signals": [{"asset_id": "TANK_5", "signal": "level_pct"}]}
    lf, app_ = live_with(hook)
    conn(lf, "CONN_HOOK").push([{"asset_id": "TANK_5", "signal": "level_pct", "value": raw}])
    lf.step(0.5)
    a = now.state(app_)["assets"]["TANK_5"]
    # Never OK. A reading past the shutdown level already put it in ALARM, which stays ALARM
    # (the mirror's rule too).
    assert a["status"] == ("ALARM" if raw > 90 else "UNKNOWN") and a["level_pct"] is None, a
    assert a["last_level_pct"] == raw or (math.isnan(raw) and math.isnan(a["last_level_pct"]))
    assert "not a usable percentage" in a["reason"]
    n = now.get(app_)
    assert n.scenario["initial_levels_pct"]["TANK_5"] is None
    assert any("Tank 5" in w for w in n.scenario["live_assumptions"])
    assert any("Tank 5" in w for w in build_plant(n.scenario).warnings)
    plans = make_plans(n.scenario, base_actions=list(n.actions), plant=n.plant)["plans"]
    assert all("TANK_5" in p.get("unknown_tanks", []) for p in plans)
    assert spacetime_mirror.level(raw) is None      # the mirror and /api/state agree


def test_a_level_within_the_slack_is_clamped_not_dropped():
    assert now.usable_level(100.3) == 100.0 and now.usable_level(-0.4) == 0.0
    assert now.usable_level(100.6) is None and now.usable_level(True) is None and now.usable_level("x") is None
    assert spacetime_mirror.level(55.55) == 55.5 or spacetime_mirror.level(55.55) == 55.6


# ---------- /api/history and connector quality ----------

@pytest.fixture()
def app_live(monkeypatch):
    sc = copy.deepcopy(load_scenario())
    sc["connectors"] = [copy.deepcopy(SPEC)]
    app.state.live_reload(sc)
    history.recorder().reset()
    yield app.state.live
    app.state.live_reload(load_scenario())
    history.recorder().reset()
    main._plans.clear()
    now.set_static(main.SCENARIO)


def advance(lv, minutes, dt=0.5):
    for _ in range(int(round(minutes / dt))):
        lv.step(dt)
        history.recorder().sync(lv)


def points(out, aid, sig="level_pct"):
    return next(s["points"] for s in out["series"] if s["asset_id"] == aid and s["signal"] == sig)


def test_history_shows_no_number_while_state_says_unknown(app_live):
    lv = app_live
    advance(lv, 3)
    c = conn(lv, "CONN_SCRIPT")
    c.drop()
    dropped_at = lv.t
    advance(lv, 2)                     # shorter than stale_after_min (5)
    st = client.get("/api/state").json()["assets"]["TANK_6"]
    assert st["level_pct"] is None and st["status"] == "UNKNOWN"
    out = client.get("/api/history", params={"minutes": 5, "step_min": 0.5, "asset_id": "TANK_6"}).json()
    series = points(out, "TANK_6")
    after = [v for t, v in series if lv.t + t >= dropped_at - 1e-6]
    before = [v for t, v in series if lv.t + t < dropped_at - 1e-6]
    assert after and all(v is None for v in after), f"history says {series} while /api/state says UNKNOWN"
    assert before and all(v == 51.7 for v in before)
    assert out["timeline"][-1]["tanks"]["TANK_6"] is None
    assert any(g["asset_id"] == "TANK_6" for g in out["gaps"])

    c.good = True                      # back: the next good reading ends the null
    advance(lv, 1)
    out = client.get("/api/history", params={"minutes": 6, "step_min": 0.5, "asset_id": "TANK_6"}).json()
    series = points(out, "TANK_6")
    assert series[-1][1] == 51.7
    gaps = [g for g in out["gaps"] if g["asset_id"] == "TANK_6"]
    assert len(gaps) == 1 and gaps[0]["to_t_min"] < 0


def test_recorder_marks_a_short_bad_window_null():
    q = {}
    hist = {("TANK_6", "level_pct"): [(1.0, 50.0), (2.0, 50.1)]}
    live = SimpleNamespace(t=2.0, history=hist,
                           registry=SimpleNamespace(connectors=[SimpleNamespace(id="C", quality=q)]))
    rec = history.Recorder()
    rec.sync(live)
    q[("TANK_6", "level_pct")] = {"quality": "out_of_range", "detail": "130", "t": 2.5}
    live.t = 3.0
    rec.sync(live)
    rec.sync(live)                     # only one marker per bad window
    assert rec.readings(("TANK_6", "level_pct")) == [(1.0, 50.0), (2.0, 50.1), (2.5, None)]
    q[("TANK_6", "level_pct")] = {"quality": "good", "detail": "", "t": 3.5}
    hist[("TANK_6", "level_pct")].append((3.5, 50.3))
    live.t = 4.0
    rec.sync(live)
    r = rec.readings(("TANK_6", "level_pct"))
    assert history.resample(r, [2.0, 2.5, 3.0, 3.5, 4.0], 5.0) == [50.1, None, None, 50.3, 50.3]
