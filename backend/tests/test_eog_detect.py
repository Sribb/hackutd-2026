"""Tests for backend/app/eog/detect.py: every problem in the readings, for any dataset view.

The synthetic tests always run: they write small dataset folders with tests/eog_fixture.py, using
ids, sizes, prices, pressure offsets and noise that are not the sponsor's. The sponsor-data tests
read FREEBOARD_EOG_DIR (a checkout of the sponsor repo) and skip when it is unset or missing. The
answer keys are opened only here, as fixtures; app code never opens them (tested below).
"""
from __future__ import annotations

import builtins
import io
import json
import os
import re
import time
from pathlib import Path

import pytest

from backend.app.eog import detect as detect_module
from backend.app.eog.dataset import load
from backend.app.eog.detect import countdown, detect
from backend.app.eog.model import CATEGORIES
from backend.tests.eog_fixture import DEFAULT_SYSTEMS, facility_spec, write_dataset

INF = float("inf")


# ---------- shared helpers ----------
def overlaps(a0, a1, b0, b1) -> bool:
    return a0 < (INF if b1 is None else b1) and b0 < (INF if a1 is None else a1)


def match(events, labels):
    """One to one: each label to the closest unused event with the same category and asset whose
    time span overlaps it. Returns (pairs, missed labels, extra events)."""
    used, pairs, missed = set(), [], []
    for k in sorted(labels, key=lambda k: k["onset"]):
        cands = [e for e in events if id(e) not in used and e.category == k["category"] and e.asset == k["asset"]
                 and overlaps(e.onset, e.end, k["onset"], k["end"])]
        if not cands:
            missed.append(k)
            continue
        e = min(cands, key=lambda e: abs(e.onset - k["onset"]))
        used.add(id(e))
        pairs.append((k, e))
    return pairs, missed, [e for e in events if id(e) not in used]


def running(ds, fac, i: int) -> bool:
    st, fl = ds.values(fac.pump_id, "status")[i], ds.values(fac.pump_id, "flow_bpd")[i]
    return st == 1 and fl > 0


def first_running_at_or_after(ds, fac, ts: int):
    for i in range(ds.index_at(ts), ds.end):
        if int(ds.times[i]) >= ts and running(ds, fac, i):
            return int(ds.times[i])
    return None


def gap_around(ds, fac, ts: int):
    """None if the pump runs at ts. Otherwise the pump-off stretch around ts as (first row without
    the pump, first row with it again or None): a leak or a worn pump cannot be seen inside it."""
    i = ds.index_at(ts)
    if running(ds, fac, i):
        return None
    j = i
    while j > 0 and not running(ds, fac, j - 1):
        j -= 1
    return int(ds.times[j]), first_running_at_or_after(ds, fac, ts)


def end_unobservable(ds, fac, key_end) -> bool:
    """The key says the problem ended by now, but the pump has not run since: no reading can show it."""
    if key_end is None or key_end > ds.now:
        return False
    return first_running_at_or_after(ds, fac, key_end) is None


def check_events_basic(ds, events) -> None:
    """Ids, order, status fields and value ranges every detect() result must satisfy."""
    assert [e.id for e in events] == [f"EV-{n:04d}" for n in range(1, len(events) + 1)]
    assert [e.onset for e in events] == sorted(e.onset for e in events)
    for e in events:
        assert e.category in CATEGORIES and e.category != "other"
        assert ds.start <= e.onset <= ds.now
        assert e.detected_at is not None and e.onset <= e.detected_at <= ds.now
        assert 0.0 <= e.confidence <= 1.0
        assert e.evidence and all(isinstance(s, str) and s for s in e.evidence)
        fac = ds.facilities[e.facility]
        assert e.system == fac.system
        assert e.asset == (fac.pipeline_id if e.category == "pipeline_leak" else fac.pump_id)
        if e.status == "ongoing":
            assert e.end is None
        else:
            assert e.status == "resolved" and e.end is not None and e.onset < e.end <= ds.now
        if e.category == "pump_failure":
            assert e.repair_end == e.onset + int(round(ds.repair_hours * 3600))
            assert e.lost_revenue_per_hour == pytest.approx(ds.facility_revenue_per_hour(e.facility))
            if e.status == "ongoing" and not e.tripped:
                h = e.hours_to_limit
                assert h is not None and 0 <= h["p10"] <= h["p50"] <= h["p90"]
        else:
            assert e.hours_to_limit is None and e.trips_at is None


EVENTS_JSON_SCHEMA = {
    "type": "object",
    "required": ["event_id", "system_id", "facility_id", "asset_id", "category", "status", "onset_ts",
                 "onset_unix", "end_ts", "end_unix", "confidence", "evidence", "level_pct", "hours_to_limit",
                 "recommended_actions"],
    "properties": {
        "event_id": {"type": "string", "pattern": "^EV-[0-9]{4}$"},
        "category": {"enum": ["pump_degradation", "pump_failure", "pipeline_leak", "other"]},
        "status": {"enum": ["resolved", "ongoing"]},
        "onset_ts": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"},
        "onset_unix": {"type": "integer"},
        "end_unix": {"type": ["integer", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "hours_to_limit": {"anyOf": [{"type": "null"}, {"type": "object", "required": ["p50"]}]},
        "recommended_actions": {"type": "array", "minItems": 1},
    },
}


def check_events_json(events) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    for e in events:
        out = e.to_events_json()
        jsonschema.validate(out, EVENTS_JSON_SCHEMA)
        json.dumps(out)
        if out["status"] == "resolved":
            assert out["end_ts"] is not None
        if out["hours_to_limit"] is not None:
            assert out["category"] == "pump_failure" and out["status"] == "ongoing"


def runs_of(status: list[int]) -> list[tuple[int, int]]:
    """Running stretches [i, j) of a pump's simulated status column."""
    out, i = [], 0
    while i < len(status):
        if status[i] == 1:
            j = i
            while j < len(status) and status[j] == 1:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def hours_of_row(fx, i: int) -> float:
    return i * fx.step / 3600.0


# ---------- synthetic datasets: always run ----------
def test_healthy_synthetic_networks_raise_nothing(tmp_path):
    for seed in (11, 12, 13):
        fx = write_dataset(tmp_path / f"h{seed}", seed=seed, hours=96)
        ds = load(fx.folder)
        assert detect(ds) == [], f"seed {seed}"
        for t in range(ds.start, ds.now + 1, 6 * 3600):           # and no view of it either
            assert detect(ds.at(t)) == []


def test_pump_failures_exact_to_the_row_with_level_and_trip(tmp_path):
    probs = [
        {"type": "pump_failure", "facility": "Q7_FACILITY_B", "onset_h": 20.0, "level_pct": 60},   # trips
        {"type": "pump_failure", "facility": "Q8_FACILITY_C", "onset_h": 30.0, "level_pct": 30},   # repair first
    ]
    fx = write_dataset(tmp_path / "fail", seed=3, hours=48, problems=probs, repair_hours=5)
    ds = load(fx.folder)
    events = detect(ds)
    check_events_basic(ds, events)
    pairs, missed, extra = match(events, fx.labels)
    assert not missed and not extra
    for lab, e in pairs:
        assert e.onset == lab["onset"] and e.end == lab["end"] and e.detected_at == lab["onset"]
        assert e.status == "resolved" and e.confidence >= 0.9
        assert e.level_at_onset_pct == pytest.approx(lab["level_at_onset_pct"], abs=0.3)
        fac = ds.facilities[e.facility]
        assert e.level_pct == ds.value_at(fac.tank_id, "level_pct", e.end)
        assert e.tripped == (lab["trips_at"] is not None)
        if lab["trips_at"] is None:
            assert e.trips_at is None
        else:
            assert abs(e.trips_at - lab["trips_at"]) <= 180
            assert e.onset < e.trips_at <= e.end
    assert {e.facility: e.tripped for e in events} == {"Q7_FACILITY_B": True, "Q8_FACILITY_C": False}
    check_events_json(events)


def test_countdown_for_an_ongoing_failure(tmp_path):
    probs = [
        {"type": "pump_failure", "facility": "Q7_FACILITY_B", "onset_h": 20.0, "level_pct": 60},
        {"type": "pump_failure", "facility": "Q8_FACILITY_C", "onset_h": 30.0, "level_pct": 30},
    ]
    fx = write_dataset(tmp_path / "cd", seed=4, hours=48, problems=probs, repair_hours=5)
    ds = load(fx.folder)
    trip_lab = next(lab for lab in fx.labels if lab["facility"] == "Q7_FACILITY_B")
    fac = ds.facilities["Q7_FACILITY_B"]

    # two hours in: still filling, trips before the repair
    view = ds.at(trip_lab["onset"] + 2 * 3600)
    e = next(e for e in detect(view) if e.facility == fac.id)
    assert e.status == "ongoing" and e.end is None and not e.tripped
    truth_h = (trip_lab["trips_at"] - view.now) / 3600.0
    assert e.hours_to_limit["p50"] == pytest.approx(truth_h, abs=0.1)
    assert e.hours_to_limit["p10"] <= e.hours_to_limit["p50"] <= e.hours_to_limit["p90"]
    assert abs(e.trips_at - (view.now + e.hours_to_limit["p50"] * 3600)) <= 20      # p50 is rounded to 0.01 h
    assert abs(e.trips_at - trip_lab["trips_at"]) <= 6 * 60
    true_level = fx.level[fac.tank_id][view.end - 1]
    assert e.level_pct == pytest.approx(true_level, abs=0.3)
    # the formula: (shutdown - level) / (producing wells' baseline / 24 / capacity * 100)
    rate = sum(w.baseline_water_bpd for w in fac.wells) / 24.0 / fac.capacity_bbl * 100.0
    assert e.hours_to_limit["p50"] == pytest.approx((view.shutdown_pct - e.level_pct) / rate, abs=0.006)
    assert "shutdown in about" in e.evidence[-1]

    # countdown() on its own fills the same fields, and leaves other events alone
    blank = type(e)(id="EV-0099", category="pump_failure", system=e.system, facility=e.facility, asset=e.asset,
                    onset=e.onset, end=None, status="ongoing", confidence=0.99)
    countdown(view, blank)
    assert blank.hours_to_limit == e.hours_to_limit and blank.trips_at == e.trips_at
    assert blank.repair_end == e.repair_end and blank.level_pct == pytest.approx(e.level_pct)
    resolved = type(e)(id="EV-0100", category="pump_failure", system=e.system, facility=e.facility,
                       asset=e.asset, onset=e.onset, end=e.onset + 3600, status="resolved", confidence=0.99)
    assert countdown(view, resolved).hours_to_limit is None

    # after the tank reached shutdown: tripped, nothing left to count, no future trips_at
    view = ds.at(trip_lab["trips_at"] + 30 * 60)
    e = next(e for e in detect(view) if e.facility == fac.id)
    assert e.status == "ongoing" and e.tripped and e.trips_at is None
    assert e.hours_to_limit == {"p50": 0.0, "p10": 0.0, "p90": 0.0}
    assert e.level_pct == pytest.approx(view.shutdown_pct)

    # the repair comes first: a countdown, but no trip time
    slow = next(lab for lab in fx.labels if lab["facility"] == "Q8_FACILITY_C")
    view = ds.at(slow["onset"] + 3600)
    e = next(e for e in detect(view) if e.facility == slow["facility"])
    assert e.status == "ongoing" and not e.tripped and e.trips_at is None
    assert view.now + e.hours_to_limit["p50"] * 3600 > e.repair_end
    assert "repair is due first" in e.evidence[-1]


def _leak_problem_times(tmp_path, seed: int):
    """A visible leak (onset and end while its pump runs) and a hidden one (onset in a pump-off gap)."""
    base = write_dataset(tmp_path / f"base{seed}", seed=seed, hours=72)
    ra = next(r for r in runs_of(base.status["Q8_PUMP_A"]) if hours_of_row(base, r[0]) > 8 and r[1] - r[0] > 40)
    on_row = ra[0] + 10
    end_run = next(r for r in runs_of(base.status["Q8_PUMP_A"]) if r[0] > on_row + 15 * 12)
    end_row = end_run[0] + 6
    rb = runs_of(base.status["Q7_PUMP_B"])
    gap = next((rb[k][1], rb[k + 1][0]) for k in range(len(rb) - 1) if hours_of_row(base, rb[k][1]) > 20)
    hid_row = gap[0] + int((gap[1] - gap[0]) * 0.6)
    return hours_of_row(base, on_row), hours_of_row(base, end_row), hours_of_row(base, hid_row)


def test_leaks_visible_onset_is_exact_and_hidden_onset_is_estimated(tmp_path):
    vis_on, vis_end, hid_on = _leak_problem_times(tmp_path, seed=5)
    probs = [
        {"type": "pipeline_leak", "facility": "Q8_FACILITY_A", "onset_h": vis_on, "end_h": vis_end},
        {"type": "pipeline_leak", "facility": "Q7_FACILITY_B", "onset_h": hid_on, "end_h": hid_on + 25},
    ]
    fx = write_dataset(tmp_path / "leaks", seed=5, hours=72, problems=probs)
    ds = load(fx.folder)
    events = detect(ds)
    check_events_basic(ds, events)
    pairs, missed, extra = match(events, fx.labels)
    assert not missed and not extra
    vis = next(e for lab, e in pairs if lab["asset"] == "Q8_PIPELINE_A")
    lab = next(lab for lab in fx.labels if lab["asset"] == "Q8_PIPELINE_A")
    assert vis.onset == lab["onset"] and vis.end == lab["end"] and vis.status == "resolved"
    assert vis.detected_at - vis.onset <= 2 * fx.step
    assert any("disposal end" in s for s in vis.evidence) and any("escaping" in s for s in vis.evidence)

    hid = next(e for lab, e in pairs if lab["asset"] == "Q7_PIPELINE_B")
    lab = next(lab for lab in fx.labels if lab["asset"] == "Q7_PIPELINE_B")
    fac = ds.facilities["Q7_FACILITY_B"]
    g0, g1 = gap_around(ds, fac, lab["onset"])
    assert g0 <= hid.onset <= g1                                   # inside the pump-off stretch
    assert abs(hid.onset - lab["onset"]) <= 30 * 60
    assert hid.detected_at <= g1 + 2 * fx.step
    assert any("did not run" in s for s in hid.evidence)


def _wear_then_fail(tmp_path, seed: int, systems=None, **kw):
    probs = [
        {"type": "pump_degradation", "facility": "Q7_FACILITY_A", "onset_h": 8.0},
        {"type": "pump_failure", "facility": "Q7_FACILITY_A", "onset_h": 40.0, "level_pct": 50},
        {"type": "pipeline_leak", "facility": "Q8_FACILITY_B", "onset_h": 16.0, "end_h": 44.0},
    ]
    return write_dataset(tmp_path / f"wf{seed}", seed=seed, hours=60, problems=probs, systems=systems, **kw)


def test_worn_pump_that_later_fails_is_two_events(tmp_path):
    fx = _wear_then_fail(tmp_path, seed=6)
    ds = load(fx.folder)
    events = detect(ds)
    check_events_basic(ds, events)
    pairs, missed, extra = match(events, fx.labels)
    assert not missed and not extra
    wear = next(e for e in events if e.category == "pump_degradation")
    fail = next(e for e in events if e.category == "pump_failure")
    lab = next(lab for lab in fx.labels if lab["category"] == "pump_degradation")
    assert wear.asset == fail.asset == "Q7_PUMP_A"
    assert abs(wear.onset - lab["onset"]) <= 30 * 60                # gradual: back-extrapolated trend
    assert wear.end == fail.onset == lab["end"] and wear.status == "resolved"
    assert wear.detected_at - lab["onset"] <= 5 * 3600
    assert any("rating" in s for s in wear.evidence) and any("failed outright" in s for s in wear.evidence)
    check_events_json(events)


def test_worn_pump_repaired_while_running_ends_on_the_row(tmp_path):
    # the worn pump drains slower, so take the schedule from the worn run itself (it is the same
    # up to the repair whatever happens after)
    probe = write_dataset(tmp_path / "probe", seed=7, hours=60,
                          problems=[{"type": "pump_degradation", "facility": "Q8_FACILITY_B", "onset_h": 6.0}])
    run = next(r for r in runs_of(probe.status["Q8_PUMP_B"]) if hours_of_row(probe, r[0]) > 30 and r[1] - r[0] > 24)
    end_h = hours_of_row(probe, run[0] + 12)
    fx = write_dataset(tmp_path / "rep", seed=7, hours=60,
                       problems=[{"type": "pump_degradation", "facility": "Q8_FACILITY_B", "onset_h": 6.0,
                                  "end_h": end_h}])
    ds = load(fx.folder)
    events = detect(ds)
    (lab,) = fx.labels
    pairs, missed, extra = match(events, fx.labels)
    assert not missed and not extra
    (_, e) = pairs[0]
    assert e.end == lab["end"] and e.status == "resolved"
    assert abs(e.onset - lab["onset"]) <= 30 * 60


def test_no_lookahead_and_no_flapping_across_views(tmp_path):
    later = [
        {"type": "pipeline_leak", "facility": "Q8_FACILITY_A", "onset_h": 31.0, "end_h": 60.0},
        {"type": "pump_failure", "facility": "Q7_FACILITY_C", "onset_h": 40.0, "level_pct": 55},
        {"type": "pump_degradation", "facility": "Q8_FACILITY_C", "onset_h": 34.0},
    ]
    a = load(write_dataset(tmp_path / "a", seed=8, hours=72).folder)
    b = load(write_dataset(tmp_path / "b", seed=8, hours=72, problems=later).folder)
    for h in (1, 3, 6, 12, 18, 24, 30):            # before anything differs, the views agree exactly
        t = a.start + h * 3600
        assert [e.to_dict() for e in detect(a.at(t))] == [e.to_dict() for e in detect(b.at(t))] == []
    seen: dict[tuple, int] = {}
    for t in range(b.start + 30 * 3600, b.now + 1, 1800):
        view = b.at(t)
        events = detect(view)
        check_events_basic(view, events)
        now_keys = {}
        for e in events:
            k = (e.category, e.asset, e.detected_at)
            now_keys[(e.category, e.asset)] = e.detected_at
            seen.setdefault((e.category, e.asset), e.detected_at)
            assert seen[(e.category, e.asset)] == e.detected_at, f"{k} moved"
        for k in seen:                              # once raised, raised at every later view
            assert k in now_keys, f"{k} disappeared at {t}"
    assert set(seen) == {("pipeline_leak", "Q8_PIPELINE_A"), ("pump_failure", "Q7_PUMP_C"),
                         ("pump_degradation", "Q8_PUMP_C")}


def test_unfamiliar_network_offsets_noise_and_setpoints(tmp_path):
    """Nothing tied to the sponsor's network: other ids, setpoints, disposal pressures, pressure
    offsets (the SPEC calls +150 / +50 placeholders) and 1.5 times the noise."""
    systems = {
        "ZR3": {"pressure": 1240.0, "facilities": [
            facility_spec("A", [900, 700, 500], 1000, 3200, start=80, stop=20, level0=40),
            facility_spec("B", [600, 450, 350], 650, 2200, start=75, stop=25, level0=62),
            facility_spec("C", [800, 500], 800, 2000, level0=35)]},
        "Q7": {"pressure": 870.0, "facilities": [
            facility_spec("A", [700, 650, 400], 900, 2600, start=78, stop=35, level0=55),
            facility_spec("B", [500, 500, 300], 700, 2000, level0=45),
            facility_spec("C", [650, 600], 750, 1800, level0=66)]},
    }
    probs = [
        {"type": "pump_degradation", "facility": "ZR3_FACILITY_A", "onset_h": 8.0},
        {"type": "pump_failure", "facility": "ZR3_FACILITY_A", "onset_h": 40.0, "level_pct": 50},
        {"type": "pipeline_leak", "facility": "Q7_FACILITY_B", "onset_h": 16.0, "end_h": 44.0},
    ]
    fx = write_dataset(tmp_path / "odd", seed=9, hours=60, problems=probs, systems=systems,
                       offsets=(120.0, 35.0), noise=1.5, repair_hours=4)
    ds = load(fx.folder)
    events = detect(ds)
    check_events_basic(ds, events)
    pairs, missed, extra = match(events, fx.labels)
    assert not missed and not extra
    for lab, e in pairs:
        fac = ds.facilities[lab["facility"]]
        if lab["category"] == "pump_degradation":
            assert abs(e.onset - lab["onset"]) <= 45 * 60
            assert e.end == lab["end"]                           # ended by its pump's failure
        elif lab["category"] == "pump_failure":
            assert e.onset == lab["onset"] and e.end == lab["end"]
        else:
            gap = gap_around(ds, fac, lab["onset"])
            assert e.onset == lab["onset"] if gap is None else gap[0] <= e.onset <= gap[1]
            gap = gap_around(ds, fac, lab["end"])
            assert e.end == lab["end"] if gap is None else gap[0] <= e.end <= gap[1]
    leak = next(e for e in events if e.category == "pipeline_leak")
    assert any("+ 35" in s for s in leak.evidence)     # the healthy offset was learned, not assumed


def test_missing_pipeline_sensors_still_find_a_worn_pump(tmp_path):
    fx = _wear_then_fail(tmp_path, seed=10, drop_metrics=("receive_flow_bpd", "pressure_psi"))
    ds = load(fx.folder)
    events = detect(ds)
    check_events_basic(ds, events)
    assert {(e.category, e.asset) for e in events} == {("pump_degradation", "Q7_PUMP_A"),
                                                       ("pump_failure", "Q7_PUMP_A")}


def test_tiny_and_early_views(tmp_path):
    fx = write_dataset(tmp_path / "tiny", seed=2, hours=12,
                       problems=[{"type": "pump_failure", "facility": "Q7_FACILITY_A", "onset_h": 0.0}])
    ds = load(fx.folder)
    first = ds.at(ds.start)
    assert first.end == 1
    (e,) = detect(first)                       # a fault in the very first row is already a failure
    assert e.category == "pump_failure" and e.onset == ds.start and e.status == "ongoing"
    assert all(ev.category == "pump_failure" for ev in detect(ds.at(ds.start + 1800)))


def test_answer_key_is_never_opened(tmp_path, monkeypatch):
    fx = _wear_then_fail(tmp_path, seed=6, answer_key={"events": "the app must never read this"})
    assert (fx.folder / "answer_key.json").is_file()
    real_open = builtins.open

    def guarded(file, *a, **k):
        if Path(str(file)).name == "answer_key.json":
            raise AssertionError("app code opened answer_key.json")
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(io, "open", guarded)
    ds = load(fx.folder)
    events = detect(ds)
    assert {e.category for e in events} == {"pump_degradation", "pump_failure", "pipeline_leak"}


def test_detector_source_has_no_clock_key_or_network_specifics():
    src = Path(detect_module.__file__).read_text(encoding="utf-8")
    for banned in ("answer_key", "time.time", "datetime.now", "date.today", "utcnow", "perf_counter"):
        assert banned not in src, banned
    assert not re.search(r"\bS\d+_", src), "an asset id from a particular network"
    assert not re.search(r"""["']S\d+["']""", src), "a system id from a particular network"


# ---------- sponsor data: skipped unless FREEBOARD_EOG_DIR points at the sponsor repo ----------
def _history() -> Path:
    raw = os.environ.get("FREEBOARD_EOG_DIR", "").strip()
    if not raw:
        pytest.skip("FREEBOARD_EOG_DIR is not set")
    hist = Path(raw) / "history"
    if not (hist / "full_week" / "telemetry.csv").is_file():
        pytest.skip(f"no sponsor history under {raw}")
    return hist


def _key(folder: Path) -> list[dict]:
    """Test fixture only: the answer key as labels."""
    raw = json.loads((folder / "answer_key.json").read_text(encoding="utf-8"))
    raw = raw["events"] if isinstance(raw, dict) else raw
    return [dict(k, onset=k["onset_unix"], end=k.get("end_unix")) for k in raw]


SNAPSHOTS = ("snapshot_2026-10-08T1200", "snapshot_2026-10-09T0330", "snapshot_2026-10-10T0900")


@pytest.fixture(scope="module")
def full_week():
    hist = _history()
    ds = load(hist / "full_week")
    return ds, _key(hist / "full_week")


def check_against_key(ds, events, key) -> dict:
    """Shared accuracy rules for a dataset view and its key (key ends after now count as ongoing).
    Returns the per-event errors in minutes, and the end states nobody could see, for reporting."""
    labels = [dict(k, end=k["end"] if (k["end"] is not None and k["end"] <= ds.now) else None)
              for k in key if k["onset"] <= ds.now]
    pairs, missed, extra = match(events, labels)
    assert not missed, f"missed {[(k['id'], k['category'], k['asset']) for k in missed]}"
    assert not extra, f"false positives {[(e.category, e.asset, e.onset) for e in extra]}"
    report = {"onset_err_min": {}, "end_err_min": {}, "unseen_end": []}
    for k, e in pairs:
        fac = ds.facilities[k["facility"]]
        assert e.facility == k["facility"] and e.system == k["system"]
        report["onset_err_min"][k["id"]] = (e.onset - k["onset"]) / 60
        assert e.detected_at >= k["onset"]
        if k["category"] == "pump_failure":
            assert e.onset == k["onset"] and e.detected_at == k["onset"]
            assert e.level_at_onset_pct == pytest.approx(k["level_pct"], abs=0.5)
        elif k["category"] == "pipeline_leak":
            gap = gap_around(ds, fac, k["onset"])
            if gap is None:
                assert e.onset == k["onset"], k["id"]          # abrupt and in view: exact to the row
            else:                                              # abrupt but unseen: inside the gap
                assert gap[0] <= e.onset <= gap[1] and abs(e.onset - k["onset"]) <= 30 * 60, k["id"]
        else:
            assert abs(e.onset - k["onset"]) <= 20 * 60, k["id"]      # gradual
        # status and end
        if k["end"] is None:
            assert e.status == "ongoing" and e.end is None, k["id"]
        elif e.status == "ongoing":
            assert end_unobservable(ds, fac, k["end"]), f"{k['id']} should be resolved by now"
            report["unseen_end"].append(k["id"])
        else:
            report["end_err_min"][k["id"]] = (e.end - k["end"]) / 60
            failed_then = any(x["category"] == "pump_failure" and x["asset"] == k["asset"] and x["onset"] == k["end"]
                              for x in key)
            gap = None if (k["category"] == "pump_failure" or failed_then) else gap_around(ds, fac, k["end"])
            if gap is None:
                assert e.end == k["end"], k["id"]                 # seen, or the pump failed outright
            else:
                assert gap[0] <= e.end <= gap[1], k["id"]         # repaired while the pump was off
        if k["category"] == "pump_failure":
            kt = k.get("trips_at_unix")
            if e.status == "ongoing" and e.tripped:               # already at shutdown: nothing to count
                assert kt is not None and kt <= ds.now and e.trips_at is None, k["id"]
            elif kt is None:
                assert e.trips_at is None and not e.tripped, k["id"]
            else:
                assert e.trips_at is not None and abs(e.trips_at - kt) <= 5 * 60, k["id"]
                assert e.tripped == (e.status == "resolved"), k["id"]
    return report


def test_full_week_finds_exactly_the_labelled_events(full_week):
    ds, key = full_week
    assert len(key) == 23                                          # fixture sanity
    events = detect(ds)
    check_events_basic(ds, events)
    assert len(events) == 23
    report = check_against_key(ds, events, key)
    assert report["unseen_end"] == []                              # by the week's end every end is seen
    wear_errs = [abs(v) for k, v in report["onset_err_min"].items()
                 if next(x for x in key if x["id"] == k)["category"] == "pump_degradation"]
    assert sum(wear_errs) / len(wear_errs) <= 10
    check_events_json(events)


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_snapshot_matches_its_key_and_countdowns(full_week, name):
    hist = _history()
    ds = load(hist / name)
    key = _key(hist / name)
    events = detect(ds)
    check_events_basic(ds, events)
    assert len(events) == len(key)
    check_against_key(ds, events, key)
    ongoing = [k for k in key if k["category"] == "pump_failure" and k["status"] == "ongoing"]
    assert ongoing
    found = {e.asset: e for e in events if e.category == "pump_failure" and e.status == "ongoing"}
    for k in ongoing:
        e = found[k["asset"]]
        assert e.hours_to_limit["p50"] == pytest.approx(k["hours_to_limit_final"], abs=0.05), k["id"]
        assert e.level_pct == pytest.approx(k["level_final_pct"], abs=0.3), k["id"]
        assert abs(e.trips_at - k["trips_at_unix"]) <= 5 * 60, k["id"]
        assert e.lost_revenue_per_hour == pytest.approx(k["lost_revenue_per_hour"], abs=0.01)
    # the alerts view order (soonest shutdown first, then lost revenue) comes out as in the key
    order = sorted(found.values(), key=lambda e: (e.hours_to_limit["p50"], -e.lost_revenue_per_hour))
    assert [e.asset for e in order] == [k["asset"] for k in sorted(ongoing, key=lambda k: k["order"])]
    # the snapshot is the full week cut short: the same answer as time travel to its "now"
    fw, _ = full_week
    assert [e.to_dict() for e in events] == [e.to_dict() for e in detect(fw.at(ds.now))]


def test_snapshot_end_that_cannot_be_seen_yet(full_week):
    """At 10-10 09:00 the key calls the S2_PIPELINE_C leak resolved at 06:00, but its pump stopped
    at 05:20 and only restarts at 09:30: no reading before 09:30 can show the repair. Keep it
    ongoing (the operator still needs the inspection) and close it once the pump runs again."""
    ds, key = full_week
    k = next(k for k in key if k["asset"] == "S2_PIPELINE_C" and k["category"] == "pipeline_leak")
    fac = ds.facilities[k["facility"]]
    cut = ds.at(1791622800)                                       # 2026-10-10T09:00:00Z
    assert end_unobservable(cut, fac, k["end"])
    e = next(e for e in detect(cut) if e.asset == k["asset"])
    assert e.status == "ongoing"
    back = first_running_at_or_after(ds, fac, k["end"])
    e = next(e for e in detect(ds.at(back)) if e.asset == k["asset"])
    assert e.status == "resolved" and e.end is not None and k["end"] - 3 * 3600 <= e.end <= back


def test_full_week_hourly_cuts(full_week):
    ds, key = full_week
    first_seen: dict[str, int] = {}
    det_at: dict[str, int] = {}
    unseen = []
    for t in range(ds.start + 3600, ds.last + 1, 3600):
        view = ds.at(t)
        events = detect(view)
        check_events_basic(view, events)
        labels = [dict(k, end=k["end"] if k["end"] <= view.now else None) for k in key if k["onset"] <= view.now]
        pairs, missed, extra = match(events, labels)
        assert not extra, f"false positive at {t}: {[(e.category, e.asset, e.onset) for e in extra]}"
        here = set()
        for k, e in pairs:
            here.add(k["id"])
            first_seen.setdefault(k["id"], view.now)
            det_at.setdefault(k["id"], e.detected_at)
            assert e.detected_at == det_at[k["id"]], f"{k['id']} detected_at moved at {t}"
            assert e.detected_at >= k["onset"]
            if k["end"] is None:
                assert e.status == "ongoing", f"{k['id']} resolved too early at {t}"
            elif e.status == "ongoing":
                assert end_unobservable(view, ds.facilities[k["facility"]], k["end"]), f"{k['id']} at {t}"
                unseen.append((t, k["id"]))
        for kid in first_seen:
            assert kid in here, f"{kid} flapped off at {t}"
    assert set(first_seen) == {k["id"] for k in key}
    delays = {}
    for k in key:
        d = (det_at[k["id"]] - k["onset"]) / 60
        delays.setdefault(k["category"], []).append(d)
        if k["category"] == "pump_failure":
            assert d == 0
        elif k["category"] == "pipeline_leak":            # a leak shows only once the pump runs
            visible = first_running_at_or_after(ds, ds.facilities[k["facility"]], k["onset"])
            assert det_at[k["id"]] - visible <= 3 * 300
        else:
            assert d <= 4 * 60
    assert max(delays["pipeline_leak"]) <= 70 and sorted(delays["pump_degradation"])[2] <= 180
    assert len(unseen) <= 8


def test_mock_test_matches_the_full_week_window(full_week):
    hist = _history()
    folders = sorted(p for p in hist.iterdir() if p.name.startswith("mock_test_") and (p / "telemetry.csv").is_file())
    if not folders:
        pytest.skip("no mock_test folder")
    fw, key = full_week
    for folder in folders:
        assert not (folder / "answer_key.json").exists()
        ds = load(folder)
        events = detect(ds)
        check_events_basic(ds, events)
        check_against_key(ds, events, key)                # the full week's key, cut at this "now"
        assert [e.to_dict() for e in events] == [e.to_dict() for e in detect(fw.at(ds.now))]


def test_full_week_detect_is_fast(full_week):
    ds, _ = full_week
    best = INF
    for _ in range(3):
        t0 = time.perf_counter()
        detect(ds)
        best = min(best, time.perf_counter() - t0)
    assert best < 1.0, f"detect took {best:.2f} s"
