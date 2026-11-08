"""Plans for pump failures: backend/app/eog/plan.py and physics.py.

Sponsor-data tests read FREEBOARD_EOG_DIR (a checkout of the sponsor repo) and skip without it.
The answer keys are test fixtures only; test_app_never_opens_answer_keys proves the app code never
opens one. Every key case is run twice: with the key's onsets, and with onsets read from the
telemetry (the first row a pump reports a fault), so nothing depends on the key. Every plan is
also checked against an independent, exact brute force over the whole plan space.

Synthetic tests build a tiny dataset folder in tmp_path and always run.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import random
import time
from fractions import Fraction
from itertools import combinations
from pathlib import Path

import pytest

from backend.app.eog import physics
from backend.app.eog import plan as P
from backend.app.eog.dataset import DatasetError, load
from backend.app.eog.model import Event, iso


# ---------------------------------------------------------------- sponsor data
def _root():
    raw = os.environ.get("FREEBOARD_EOG_DIR", "").strip()
    p = Path(raw) if raw else None
    return p if p is not None and (p / "history" / "full_week").is_dir() else None


ROOT = _root()
needs_data = pytest.mark.skipif(ROOT is None, reason="FREEBOARD_EOG_DIR is not set or has no history/full_week")
SNAPSHOTS = sorted(p.name for p in (ROOT / "history").iterdir()
                   if (p / "answer_key.json").is_file() and p.name != "full_week") if ROOT else []
ONSETS = ["key", "telemetry"]


def _folder(name: str) -> Path:
    return ROOT / "history" / name


def _key(name: str) -> list:
    return json.loads((_folder(name) / "answer_key.json").read_text(encoding="utf-8"))


def _key_events(key: list) -> list:
    return [Event(id=k["id"], category=k["category"], system=k["system"], facility=k["facility"],
                  asset=k["asset"], onset=k["onset_unix"], end=k.get("end_unix"), status=k["status"],
                  confidence=1.0) for k in key]


def _telemetry_events(ds) -> list:
    """Pump failures straight from the readings: each run of pump status 2 (fault, SPEC)."""
    out = []
    for fid in sorted(ds.facilities):
        f = ds.facilities[fid]
        st = ds.values(f.pump_id, "status")
        i = 0
        while i < ds.end:
            if st[i] != 2:
                i += 1
                continue
            j = i
            while j < ds.end and st[j] == 2:
                j += 1
            ongoing = j >= ds.end
            out.append(Event(id=f"TEL-{len(out) + 1}", category="pump_failure", system=f.system, facility=fid,
                             asset=f.pump_id, onset=int(ds.times[i]), end=None if ongoing else int(ds.times[j]),
                             status="ongoing" if ongoing else "resolved", confidence=1.0))
            i = j
    return out


def _events(ds, key: list, source: str):
    """(events, {event id -> key entry}) with onsets from the key or from the telemetry."""
    pumps = {(k["facility"], k["onset_unix"]): k for k in key if k["category"] == "pump_failure"}
    if source == "key":
        evs = _key_events(key)
        return evs, {e.id: pumps[(e.facility, e.onset)] for e in evs if e.category == "pump_failure"}
    evs = _telemetry_events(ds)
    match = {e.id: pumps.get((e.facility, e.onset)) for e in evs}
    assert all(match.values()), f"telemetry fault runs without a key event: {[i for i, k in match.items() if not k]}"
    assert len(evs) == len(pumps), f"{len(pumps) - len(evs)} key pump failures have no fault run in the telemetry"
    return evs, match


def _as_key(text: str, match: dict) -> str:
    """Our text with telemetry event ids replaced by the key's (only the shares note names ids)."""
    for tid in sorted(match, key=len, reverse=True):
        text = text.replace(tid, match[tid]["id"])
    return text


# ---------------------------------------------------------------- independent exact brute force
class Exact:
    """An independent model of one tank: integer volumes in 1/1,440,000 bbl (levels are whole tenths
    of a percent, capacities and rates whole numbers), whole minutes, Fractions for trip times."""
    UNIT = 1_440_000

    def __init__(self, ds, facility_id, start, level_pct, repair_end, wells_on=None):
        f = ds.facilities[facility_id]
        tenths = round(level_pct * 10)
        assert abs(tenths - level_pct * 10) < 1e-6, f"level {level_pct} is not whole tenths of a percent"
        assert float(f.capacity_bbl).is_integer() and (repair_end - start) % 60 == 0
        cap = int(f.capacity_bbl)
        self.v0 = tenths * cap * 1440
        self.S = round(ds.shutdown_pct * 10) * cap * 1440
        self.wells = list(wells_on if wells_on is not None else f.well_ids)
        for w in self.wells:
            assert float(ds.wells[w].baseline_water_bpd).is_integer()
        self.rate = {w: int(ds.wells[w].baseline_water_bpd) * 1000 for w in self.wells}
        self.q = sum(self.rate.values())
        self.H = (repair_end - start) // 60
        self.load = int(ds.truck_capacity_bbl) * self.UNIT
        self.resp, self.cycle = int(f.response_min), int(f.cycle_min)
        self.K = sum(1 for j in range(10_000) if self.resp + j * self.cycle < self.H)
        self.price = ds.truck_cost
        self.rev_day = {w: ds.revenue_per_day(w) for w in self.wells}

    def trip(self, n, k, wells, tau):
        """The trip minute (Fraction), or None if the tank stays below shutdown to the repair end."""
        ev = [(self.resp + j * self.cycle, 1, n) for j in range(k)]
        if wells:
            ev.append((tau, 0, sum(self.rate[w] for w in wells)))
        ev = sorted(e for e in ev if e[0] < self.H)
        t, v, q = 0, self.v0, self.q
        if v >= self.S:
            return Fraction(0)
        for m, kind, val in ev:
            vm = v + q * (m - t)
            if vm >= self.S:
                return t + Fraction(self.S - v, q)
            t, v = m, vm
            if kind:
                v -= min(val * self.load, v)
            else:
                q -= val
        if v + q * (self.H - t) >= self.S:
            return t + Fraction(self.S - v, q)
        return None

    def best(self, max_trucks):
        """Cheapest cost over every plan: do nothing, or n <= max_trucks trucks x k trips, any well
        subset shut in from any whole minute before the repair ends (every minute is tried)."""
        nothing = self.trip(0, 0, (), None)
        if nothing is None:
            return 0.0
        best = sum(self.rev_day.values()) / 1440.0 * float(self.H - nothing)
        subsets = [()] + [c for r in range(1, len(self.wells) + 1) for c in combinations(self.wells, r)]
        for n in range(max_trucks + 1):
            for k in (range(1, self.K + 1) if n else [0]):
                for wells in subsets:
                    if not n and not wells:
                        continue
                    for tau in (range(self.H) if wells else [None]):
                        if self.trip(n, k, wells, tau) is None:
                            cost = n * k * self.price
                            if wells:
                                cost += sum(self.rev_day[w] for w in wells) / 1440.0 * (self.H - tau)
                            best = min(best, cost)
        return best


def _allocations(n_events, trucks):
    if n_events == 0:
        yield ()
        return
    for m in range(trucks + 1):
        for rest in _allocations(n_events - 1, trucks - m):
            yield (m,) + rest


def _trucks_used(plan) -> int:
    return sum(a["trucks"] for a in plan.actions if a["type"] == "dispatch_truck")


def _wells_on(ds, e) -> list:
    """The facility's wells producing at the onset row, read straight from the telemetry."""
    i = ds.index_at(e.onset)
    return [w for w in ds.facilities[e.facility].well_ids if ds.values(w, "status")[i] != 0]


def _check_joint_optimal(ds, events, plans, start):
    """Each system's plans together cost the brute-force minimum over every split of its trucks,
    and each plan is the brute-force best with the trucks it uses."""
    by_sys = {}
    for e in events:
        if e.id in plans:
            by_sys.setdefault(e.system, []).append(e)
    for sid, group in by_sys.items():
        fleet = ds.systems[sid].fleet_size
        free = plans[group[0].id].trucks_free
        tables = []
        for e in group:
            p = plans[e.id]
            assert p.trucks_free == free and p.fleet_size == fleet
            ex = Exact(ds, e.facility, start, e.level_pct, e.onset + int(ds.repair_hours * 3600), _wells_on(ds, e))
            table = [ex.best(m) for m in range(free + 1)]
            tables.append(table)
            assert p.cost == pytest.approx(table[_trucks_used(p)], abs=0.006), (e.id, p.summary, table)
        joint = min(sum(t[m] for t, m in zip(tables, alloc)) for alloc in _allocations(len(group), free))
        ours = sum(plans[e.id].cost for e in group)
        assert ours == pytest.approx(joint, abs=0.02), (sid, ours, joint)


# ---------------------------------------------------------------- the keys
@needs_data
@pytest.mark.parametrize("source", ONSETS)
def test_full_week_reference_matches_key(source):
    """Every pump failure's reference_action (trucks, trips, wells, shut-in minute, ~$ cost, notes)
    and trips_at to the minute, with every plan equal to the brute-force minimum."""
    ds = load(_folder("full_week"))
    key = _key("full_week")
    events, match = _events(ds, key, source)
    plans = P.plan_failures(ds, events, mode="reference")
    pumps = [e for e in events if e.category == "pump_failure"]
    assert len(pumps) == 13 and set(plans) == {e.id for e in pumps}
    for e in pumps:
        k, p = match[e.id], plans[e.id]
        assert _as_key(P.key_text(p), match) == k["reference_action"], e.id
        assert e.trips_at == k["trips_at_unix"], (e.id, iso(e.trips_at), k["trips_at"])
        assert e.level_at_onset_pct == pytest.approx(k["level_pct"], abs=1e-9), e.id
        assert p.start == e.onset and p.fleet_size == ds.systems[e.system].fleet_size
        note = [n for n in p.notes if "trucks free" in n]
        if p.trucks_free < p.fleet_size:
            assert note == [f"{p.trucks_free} of {p.fleet_size} trucks free; the rest are on earlier events"]
        else:
            assert note == []
        ex = Exact(ds, e.facility, p.start, e.level_at_onset_pct, e.repair_end, _wells_on(ds, e))
        assert p.cost == pytest.approx(ex.best(p.trucks_free), abs=0.006), (e.id, p.summary)


@needs_data
@pytest.mark.parametrize("source", ONSETS)
@pytest.mark.parametrize("snap", SNAPSHOTS)
def test_snapshot_now_matches_key(snap, source):
    """Each ongoing pump failure at the snapshot's now: reference_action, level_final_pct,
    hours_to_limit_final, order and lost_revenue_per_hour; joint plans equal the brute force."""
    ds = load(_folder(snap))
    key = _key(snap)
    events, match = _events(ds, key, source)
    plans = P.plan_failures(ds, events, mode="now")
    ongoing = [e for e in events if e.id in match and match[e.id]["status"] == "ongoing"]
    assert ongoing and set(plans) == {e.id for e in ongoing}
    order = [e.id for e in P.alerts_order(events, plans) if e.category == "pump_failure"]
    for e in ongoing:
        k, p = match[e.id], plans[e.id]
        assert _as_key(P.key_text(p), match) == k["reference_action"], e.id
        assert e.level_pct == pytest.approx(k["level_final_pct"], abs=1e-9), e.id
        assert e.hours_to_limit["p50"] == pytest.approx(k["hours_to_limit_final"], abs=1e-9), e.id
        assert order.index(e.id) + 1 == k["order"], e.id
        assert e.lost_revenue_per_hour == pytest.approx(k["lost_revenue_per_hour"], abs=1e-9), e.id
        assert p.start == ds.now and e.status == "ongoing"
        others = sorted((o for o in ongoing if o.system == e.system and o.id != e.id), key=lambda o: o.onset)
        assert p.shares_with == [o.id for o in others]
    _check_joint_optimal(ds, ongoing, plans, ds.now)


@needs_data
@pytest.mark.parametrize("snap", SNAPSHOTS)
def test_snapshot_history_matches_key(snap):
    """In a cut, history mode reproduces the key's reference_action of every resolved failure."""
    ds = load(_folder(snap))
    key = _key(snap)
    events = _key_events(key)
    plans = P.plan_failures(ds, events, mode="reference")
    resolved = [k for k in key if k["category"] == "pump_failure" and k["status"] == "resolved"]
    assert resolved
    for k in resolved:
        assert P.key_text(plans[k["id"]]) == k["reference_action"], k["id"]


@needs_data
@pytest.mark.parametrize("window", [12, 24, 48, 60])
def test_fit_window_does_not_matter(window, monkeypatch):
    """R1's claim: any pre-onset window from 12 to 60 rows (1 to 5 h) reproduces every key target."""
    monkeypatch.setattr(P, "PRE_ROWS", window)
    ds = load(_folder("full_week"))
    key = _key("full_week")
    events, match = _events(ds, key, "key")
    plans = P.plan_failures(ds, events, mode="reference")
    for eid, p in plans.items():
        e = next(x for x in events if x.id == eid)
        assert P.key_text(p) == match[eid]["reference_action"] and e.trips_at == match[eid]["trips_at_unix"], eid
    for snap in SNAPSHOTS:
        ds = load(_folder(snap))
        key = _key(snap)
        events, match = _events(ds, key, "key")
        plans = P.plan_failures(ds, events, mode="now")
        for eid, p in plans.items():
            e, k = next(x for x in events if x.id == eid), match[eid]
            assert P.key_text(p) == k["reference_action"], (snap, eid)
            assert (e.level_pct, e.hours_to_limit["p50"]) == (k["level_final_pct"], k["hours_to_limit_final"]), eid


@needs_data
def test_rules_from_the_key_evidence():
    """R3 strict shutdown and R6 tie-break, shown on the cases that pin them down."""
    ds = load(_folder("full_week"))
    e21 = next(e for e in _key_events(_key("full_week")) if e.id == "EVENT_21")
    start, end = e21.onset, e21.onset + 6 * 3600
    exact = P.simulate(ds, e21.facility, start, 55.0, end,
                       [{"type": "shut_in_wells", "wells": ["S4_WELL_A2"], "start_after_min": 52}])
    assert exact["trip_at"] == end and exact["peak_level_pct"] == 92.0      # touches 92 % at the repair end
    ok = P.simulate(ds, e21.facility, start, 55.0, end,
                    [{"type": "shut_in_wells", "wells": ["S4_WELL_A2"], "start_after_min": 51}])
    assert ok["trip_at"] is None and ok["cost"] == pytest.approx(2414.06, abs=0.01)
    # EVENT_15 at onset: both truck plans cost $1,810 and both work; the key's choice has 1 truck
    e15 = next(e for e in _key_events(_key("full_week")) if e.id == "EVENT_15")
    alts = P.alternatives(ds, e15, start_ts=e15.onset)
    two_ways = [p.summary for p in alts if p.cost == 1810.0]
    assert two_ways == ["1 truck, 2 trips (~$1,810)", "2 trucks, 1 trip each (~$1,810)"]


@needs_data
def test_readme_opening_scene_alternatives():
    """README's EVENT_9 table: truck $905, S1_WELL_C2 at 02:25 for $1,958, do nothing $5,016 at 04:49."""
    ds = load(_folder("full_week"))
    e9 = next(e for e in _key_events(_key("full_week")) if e.id == "EVENT_9")
    alts = P.alternatives(ds, e9, start_ts=e9.onset)
    by = {p.summary: p for p in alts}
    assert alts[0].summary == "1 truck, 1 trip (~$905)" and alts[0].arrivals == [e9.onset + 75 * 60]
    c2 = by["shut in S1_WELL_C2 at +125 min, ~144 min before the trip (~$1,958)"]
    assert iso(c2.start + 125 * 60) == "2026-10-08T02:25:00Z"
    nothing = alts[-1]
    assert nothing.actions == [{"type": "none"}] and nothing.trips
    assert round(nothing.cost) == 5016 and iso(nothing.trip_at) == "2026-10-08T04:49:00Z"
    assert [p.cost for p in alts] == sorted(p.cost for p in alts)


@needs_data
def test_expires_at_is_the_last_minute_the_plan_works():
    """Last Call: started at expires_at the same plan still holds; one minute later it does not."""
    ds = load(_folder("snapshot_2026-10-10T0900"))
    events = _key_events(_key("snapshot_2026-10-10T0900"))
    plans = P.plan_failures(ds, events, mode="now")
    checked = 0
    for e in events:
        p = plans.get(e.id)
        if p is None or p.expires_at is None:
            continue
        rate = P.countdown(ds, e)["fill_pct_per_hour"]

        def works(delay_min):
            s = p.start + 60 * delay_min
            acts = []
            for a in p.actions:
                if a["type"] == "shut_in_wells":
                    if a["start_after_min"] < delay_min:
                        return False
                    a = dict(a, start_after_min=a["start_after_min"] - delay_min)
                acts.append(a)
            r = P.simulate(ds, p.facility_id, s, e.level_pct + rate * delay_min / 60.0, e.repair_end, acts)
            return r["trip_at"] is None and not r["errors"]

        d = (p.expires_at - p.start) // 60
        assert works(0) and works(d) and not works(d + 1), (e.id, p.summary, d)
        checked += 1
    assert checked == 3


@needs_data
@pytest.mark.parametrize("snap", SNAPSHOTS)
def test_wait_curve_on_snapshots(snap):
    """Delay 0 is the plan; the price of waiting never goes down; points stop before the repair."""
    ds = load(_folder(snap))
    events = _key_events(_key(snap))
    plans = P.plan_failures(ds, events, mode="now")
    for eid, p in plans.items():
        curve = P.wait_curve(ds, events, eid)
        e = next(x for x in events if x.id == eid)
        assert set(curve[0]) == {"delay_min", "start_ts", "cost", "summary", "trucks_free"}
        assert curve[0]["delay_min"] == 0 and curve[0]["cost"] == pytest.approx(p.cost, abs=0.01)
        assert curve[0]["summary"] == p.summary and curve[0]["start_ts"] == ds.now
        assert all(0 <= c["trucks_free"] <= p.fleet_size for c in curve)
        assert [c["delay_min"] for c in curve] == list(range(0, 5 * len(curve), 5))
        assert all(c["start_ts"] < e.repair_end for c in curve)
        assert e.repair_end - curve[-1]["start_ts"] <= 300
        costs = [c["cost"] for c in curve]
        assert all(b >= a - 0.01 for a, b in zip(costs, costs[1:])), (eid, costs)


@needs_data
def test_mock_test_plans_and_speed():
    """The dress rehearsal (no key): telemetry onsets, joint S5 plan is brute-force optimal."""
    ds = load(_folder("mock_test_2026-10-10T1530"))
    events = _telemetry_events(ds)
    t0 = time.perf_counter()
    plans = P.plan_failures(ds, events, mode="now")
    assert time.perf_counter() - t0 < 0.5
    ongoing = [e for e in events if e.status == "ongoing"]
    assert len(ongoing) == 2 and set(plans) == {e.id for e in ongoing}
    _check_joint_optimal(ds, ongoing, plans, ds.now)


@needs_data
@pytest.mark.parametrize("snap", SNAPSHOTS)
def test_plan_failures_under_half_a_second(snap):
    ds = load(_folder(snap))
    for source in ONSETS:
        events, _ = _events(ds, _key(snap), source)
        t0 = time.perf_counter()
        P.plan_failures(ds, events, mode="now")
        assert time.perf_counter() - t0 < 0.5


@needs_data
def test_app_never_opens_answer_keys(monkeypatch):
    real = builtins.open

    def guard(file, *a, **kw):
        if "answer_key" in str(file):
            raise AssertionError(f"app code opened {file}")
        return real(file, *a, **kw)

    snap = "snapshot_2026-10-08T1200"
    events = _key_events(_key(snap))          # the fixture is read before the guard goes up
    monkeypatch.setattr(builtins, "open", guard)
    monkeypatch.setattr(io, "open", guard)
    with pytest.raises(AssertionError):       # the guard does catch both ways of opening it
        _key(snap)
    with pytest.raises(AssertionError):
        open(_folder(snap) / "answer_key.json", encoding="utf-8")
    ds = load(_folder(snap))
    plans = P.plan_failures(ds, events, mode="now")
    P.plan_failures(ds, events, mode="reference")
    pump = next(e for e in events if e.id in plans)
    P.alternatives(ds, pump)
    P.wait_curve(ds, events, pump.id)
    P.countdown(ds, pump)
    P.simulate(ds, pump.facility, ds.now, 50.0, ds.now + 3600, [])
    assert len(plans) == 3


def test_no_clock_in_data_logic():
    for name in ("plan.py", "physics.py"):
        src = (Path(P.__file__).parent / name).read_text(encoding="utf-8")
        for banned in ("time.time(", "datetime.now(", "date.today(", "utcnow(", "time.monotonic("):
            assert banned not in src, f"{name} calls {banned}"


# ---------------------------------------------------------------- synthetic dataset (always runs)
T0 = 1_800_000_000          # any 5-minute boundary; unrelated to the sponsor week


def _network():
    def fac(fid, cap, wells, lat):
        return {"id": fid, "lat": lat, "lon": -100.0,
                "wells": [{"id": w, "baseline_water_bpd": b, "oil_bpd": o, "gas_mcfd": g} for w, b, o, g in wells],
                "tank": {"id": fid.replace("FAC", "TANK"), "capacity_bbl": cap},
                "pump": {"id": fid.replace("FAC", "PUMP"), "max_flow_bpd": 3000, "start_level_pct": 70,
                         "stop_level_pct": 30},
                "pipeline": {"id": fid.replace("FAC", "PIPE"), "max_bpd": 4000, "max_pressure_psi": 1600}}
    return {"defaults": {"tank_limits_pct": {"warning": 85, "shutdown": 92}},
            "systems": [{"id": "SX", "center": [30.0, -100.0], "truck_yard": [30.1, -100.1],
                         "disposal_site": {"id": "SX_DISP", "lat": 30.0, "lon": -100.0, "pressure_psi": 1000},
                         "facilities": [
                             fac("SX_FAC_A", 1000, [("SX_W_A1", 600, 900, 3000), ("SX_W_A2", 400, 600, 2000),
                                                    ("SX_W_A3", 200, 500, 2000)], 30.0),
                             fac("SX_FAC_B", 800, [("SX_W_B1", 500, 50, 200), ("SX_W_B2", 300, 300, 1000),
                                                   ("SX_W_B3", 200, 400, 1500)], 30.1)]}]}


def _ops(fleet):
    return {"prices": {"oil_per_bbl": 50, "gas_per_mcf": 2.5}, "expected_repair_hours": {"pump_failure": 6},
            "emergency_trucking": {"truck_capacity_bbl": 120, "cost_per_trip": 800},
            "systems": {"SX": {"emergency_fleet_size": fleet,
                               "facilities": {"SX_FAC_A": {"response_min": 60, "cycle_min": 200},
                                              "SX_FAC_B": {"response_min": 50, "cycle_min": 180}}}}}


def _write(folder: Path, rows: int, failures: dict, fleet: int = 2, noise: float = 0.0, seed: int = 7) -> Path:
    """failures: {facility_id: (onset_row, level_at_onset_pct)}. Before its onset a pump matches the
    inflow (flat level); from the onset it faults, the tank fills at the wells' baseline water,
    trips at 92 % (wells off, level held), and the pump is back 72 rows (6 h) later."""
    folder.mkdir(parents=True, exist_ok=True)
    net, rng = _network(), random.Random(seed)
    (folder / "network.json").write_text(json.dumps(net))
    (folder / "ops.json").write_text(json.dumps(_ops(fleet)))
    lines = ["timestamp,ts_unix,asset_id,metric,value"]
    for i in range(rows):
        ts = T0 + 300 * i
        stamp = iso(ts)
        for f in net["systems"][0]["facilities"]:
            fid, cap = f["id"], f["tank"]["capacity_bbl"]
            q = sum(w["baseline_water_bpd"] for w in f["wells"])
            onset_row, lvl0 = failures.get(fid, (None, 50.0))
            state, level, wells_on = 1, lvl0, True
            if onset_row is not None and onset_row <= i < onset_row + 72:
                state = 2
                hours = (i - onset_row) * 5 / 60.0
                level = lvl0 + q / 24.0 / cap * 100.0 * hours
                cross_h = (92.0 - lvl0) / (q / 24.0 / cap * 100.0)
                if hours >= cross_h:
                    level, wells_on = 92.0, False
            elif onset_row is not None and i >= onset_row + 72:
                level = 60.0
            reading = level if not wells_on else min(92.0, level + rng.gauss(0.0, noise))
            for w in f["wells"]:
                lines.append(f"{stamp},{ts},{w['id']},water_bpd,{w['baseline_water_bpd'] if wells_on else 0}")
                lines.append(f"{stamp},{ts},{w['id']},status,{1 if wells_on else 0}")
            lines.append(f"{stamp},{ts},{fid},inflow_bpd,{q if wells_on else 0}")
            lines.append(f"{stamp},{ts},{f['tank']['id']},level_pct,{round(reading, 2)}")
            lines.append(f"{stamp},{ts},{f['pump']['id']},status,{state}")
            lines.append(f"{stamp},{ts},{f['pump']['id']},flow_bpd,{q if state == 1 else 0}")
    (folder / "telemetry.csv").write_text("\n".join(lines) + "\n")
    return folder


def _ev(eid, fid, onset_row, end_row=None):
    return Event(id=eid, category="pump_failure", system="SX", facility=fid, asset=fid.replace("FAC", "PUMP"),
                 onset=T0 + 300 * onset_row, end=None if end_row is None else T0 + 300 * end_row,
                 status="ongoing" if end_row is None else "resolved", confidence=1.0)


def test_simulate_truck_round_takes_load_or_what_is_left(tmp_path):
    ds = load(_write(tmp_path / "d", 20, {}))
    end = T0 + 6 * 3600
    full = P.simulate(ds, "SX_FAC_A", T0, 75.0, end,
                      [{"type": "dispatch_truck", "facility_id": "SX_FAC_A", "trucks": 2, "trips_per_truck": 1}])
    assert full["arrivals"] == [T0 + 3600] and full["removed_bbl"] == 240.0 and full["truck_cost"] == 1600.0
    assert full["trip_at"] is None and full["peak_level_pct"] == pytest.approx(81.0)
    low = P.simulate(ds, "SX_FAC_A", T0, 10.0, end,
                     [{"type": "dispatch_truck", "facility_id": "SX_FAC_A", "trucks": 2, "trips_per_truck": 1}])
    assert low["removed_bbl"] == pytest.approx(150.0)          # 100 bbl at start + 50 bbl/h for 1 h
    assert [p for p in low["points"] if p[0] == T0 + 3600] == [[T0 + 3600, 15.0], [T0 + 3600, 0.0]]


def test_simulate_points_trip_and_costs(tmp_path):
    ds = load(_write(tmp_path / "d", 20, {}))
    end = T0 + 6 * 3600
    r = P.simulate(ds, "SX_FAC_A", T0, 75.0, end, [])
    # 5 %/h from 75 % reaches 92 % after 3.4 h = 204 min; the wells' revenue is lost to the repair end
    assert r["trip_at"] == T0 + 204 * 60 and r["peak_level_pct"] == 92.0
    assert r["auto_shut_in_cost"] == pytest.approx((52500 + 35000 + 30000) / 24 * (360 - 204) / 60, abs=0.01)
    ts = [p[0] for p in r["points"]]
    assert ts == sorted(ts) and ts[0] == T0 and ts[-1] == end
    assert all(t in ts for t in range(T0, end + 1, 300)) and T0 + 204 * 60 in ts
    assert all(p[1] == 92.0 for p in r["points"] if p[0] >= T0 + 204 * 60)       # the level holds
    late = P.simulate(ds, "SX_FAC_A", T0, 75.0, end,
                      [{"type": "dispatch_truck", "facility_id": "SX_FAC_A", "trucks": 1, "trips_per_truck": 2}])
    # the first round (60 min) keeps it below; the second (260 min) still arrives in time
    assert late["trip_at"] is None and late["removed_bbl"] == 240.0
    wasted = P.simulate(ds, "SX_FAC_A", T0, 91.0, end,
                        [{"type": "dispatch_truck", "facility_id": "SX_FAC_A", "trucks": 1, "trips_per_truck": 1}])
    assert wasted["trip_at"] == T0 + 12 * 60 and wasted["removed_bbl"] == 0.0 and wasted["truck_cost"] == 800.0


def test_simulate_reaching_shutdown_exactly_is_a_trip(tmp_path):
    ds = load(_write(tmp_path / "d", 20, {}))
    # 62 % + 5 %/h * 6 h = 92 % exactly at the repair end
    r = P.simulate(ds, "SX_FAC_A", T0, 62.0, T0 + 6 * 3600, [])
    assert r["trip_at"] == T0 + 6 * 3600 and r["auto_shut_in_cost"] == 0.0
    assert P.simulate(ds, "SX_FAC_A", T0, 61.9, T0 + 6 * 3600, [])["trip_at"] is None


def test_simulate_reports_bad_actions(tmp_path):
    ds = load(_write(tmp_path / "d", 20, {}, fleet=1))
    end = T0 + 3 * 3600
    r = P.simulate(ds, "SX_FAC_A", T0, 50.0, end, [
        {"type": "dispatch_truck", "facility_id": "SX_FAC_A", "trucks": 2, "trips_per_truck": 2},
        {"type": "dispatch_truck", "facility_id": "SX_FAC_B", "trucks": 1, "trips_per_truck": 1},
        {"type": "dispatch_truck", "facility_id": "SX_FAC_A", "trucks": 0, "trips_per_truck": 1},
        {"type": "shut_in_wells", "wells": ["SX_W_B1", "SX_W_A1", "SX_W_A1"], "start_after_min": 10},
        {"type": "shut_in_wells", "wells": ["SX_W_A2"], "start_after_min": -5},
        {"type": "teleport"},
        {"type": "none"},
    ])
    text = " | ".join(r["errors"])
    for part in ("2 trucks asked for, but SX has 1", "Trip 2 of 2 would arrive after the repair ends",
                 "does not apply to SX_FAC_A", "trucks must be a whole number of at least 1",
                 "SX_W_B1 is not a well of SX_FAC_A", "SX_W_A1 is shut in twice",
                 "start_after_min must be 0 or more", "Unknown action type 'teleport'"):
        assert part in text, part
    assert r["arrivals"] == [T0 + 3600] and r["truck_cost"] == 1600.0
    assert r["shut_in_cost"] == pytest.approx(52500 / 1440 * (180 - 10), abs=0.01)
    assert sum("Unknown action type" in x for x in r["errors"]) == 1      # "none" is not an error
    assert P.simulate(ds, "SX_FAC_A", T0, 50.0, T0, [])["errors"]
    off = P.simulate(ds, "SX_FAC_A", T0, 50.0, end, [{"type": "shut_in_wells", "wells": ["SX_W_A3"]}],
                     wells_on=["SX_W_A1", "SX_W_A2"])
    assert off["errors"] == ["SX_W_A3 is not producing, so shutting it in changes nothing."]
    # inflow_bpd overrides the wells' total: no inflow, a flat line and nothing lost
    flat = P.simulate(ds, "SX_FAC_A", T0, 50.0, end, [], inflow_bpd=0)
    assert {p[1] for p in flat["points"]} == {50.0} and flat["cost"] == 0.0 and flat["trip_at"] is None
    with pytest.raises(DatasetError):
        P.simulate(ds, "NO_SUCH_FACILITY", T0, 50.0, end, [])


def test_countdown_recovers_exact_level_and_no_peeking(tmp_path):
    # readings are written to 2 decimals, so the fit is exact only to about 1e-4
    ds = load(_write(tmp_path / "d", 61, {"SX_FAC_A": (20, 65.3)}))
    e = _ev("E1", "SX_FAC_A", 20)
    cd = P.countdown(ds, e)
    assert ds.now == T0 + 300 * 60 and cd["at"] == ds.now and not cd["tripped"]
    assert cd["level_at_onset_fit_pct"] == pytest.approx(65.3, abs=1e-3) and cd["level_at_onset_pct"] == 65.3
    assert cd["fill_pct_per_hour"] == pytest.approx(5.0)
    assert cd["level_pct"] == 82.0                       # R2: round(65.3 + 5 %/h * 200 min, 1)
    assert cd["level_fit_pct"] == pytest.approx(65.3 + 5.0 * 200 / 60, abs=1e-3)
    # 26.7 points at 5 %/h is 320.4 min: the minute-rounded trip, and 2.00 h from now (R4)
    assert cd["trips_at"] == T0 + 300 * 20 + 320 * 60
    assert cd["hours_to_limit"]["p50"] == 2.0
    assert cd["rows_used"] == {"after_onset": 41, "before_onset": 20}
    assert cd["lost_revenue_per_hour"] == pytest.approx((52500 + 35000 + 30000) / 24)
    # a view that ends earlier never sees the later rows: writing them differently changes nothing
    early = P.countdown(ds, e, at_ts=T0 + 300 * 25)
    assert early["rows_used"] == {"after_onset": 6, "before_onset": 20}
    ds2 = load(_write(tmp_path / "e", 61, {"SX_FAC_A": (20, 65.3)}))
    lv = ds2.values("SX_TANK_A", "level_pct")
    for i in range(26, 61):
        lv[i] = 10.0
    assert P.countdown(ds2, e, at_ts=T0 + 300 * 25) == early
    assert P.countdown(ds2, e) != cd


def test_countdown_denoises_noisy_readings(tmp_path):
    ds = load(_write(tmp_path / "d", 60, {"SX_FAC_A": (30, 55.0)}, noise=0.08, seed=3))
    cd = P.countdown(ds, _ev("E1", "SX_FAC_A", 30))
    assert cd["level_at_onset_pct"] == 55.0
    assert abs(cd["level_at_onset_fit_pct"] - 55.0) < 0.05
    assert cd["rows_used"] == {"after_onset": 30, "before_onset": 30}


def test_now_mode_tie_break_prefers_fewer_trucks(tmp_path):
    # 75 % -> 105 % by the repair end: needs two loads; 1 truck x 2 trips and 2 trucks x 1 trip
    # both cost $1,600, every shut-in mix costs more (R6)
    ds = load(_write(tmp_path / "d", 21, {"SX_FAC_A": (20, 75.0)}))
    e = _ev("E1", "SX_FAC_A", 20)
    plan = P.plan_failures(ds, [e])["E1"]
    assert plan.summary == "1 truck, 2 trips (~$1,600)" and plan.busy_until == [plan.start + (60 + 2 * 200) * 60]
    alts = [p.summary for p in P.alternatives(ds, e)]
    assert alts[:2] == ["1 truck, 2 trips (~$1,600)", "2 trucks, 1 trip each (~$1,600)"]


def test_now_mode_trucks_shared_and_busy(tmp_path):
    ds = load(_write(tmp_path / "d", 23, {"SX_FAC_A": (20, 75.0), "SX_FAC_B": (22, 70.0)}, fleet=1))
    evs = [_ev("EA", "SX_FAC_A", 20), _ev("EB", "SX_FAC_B", 22)]
    plans = P.plan_failures(ds, evs)
    assert sum(_trucks_used(p) for p in plans.values()) <= 1
    assert plans["EA"].notes == ["shares SX's 1 truck with EB"] and plans["EB"].shares_with == ["EA"]
    _check_joint_optimal(ds, evs, plans, ds.now)
    # a truck already out until after now leaves none
    busy = P.plan_failures(ds, [_ev("EA", "SX_FAC_A", 20), _ev("EB", "SX_FAC_B", 22)],
                           busy={"SX": [ds.now + 3600]})
    assert all(_trucks_used(p) == 0 and p.trucks_free == 0 for p in busy.values())
    assert busy["EA"].notes[0] == "0 of 1 trucks free; the rest are on earlier events"
    back = P.plan_failures(ds, [_ev("EA", "SX_FAC_A", 20), _ev("EB", "SX_FAC_B", 22)], busy={"SX": [ds.now]})
    assert all(p.trucks_free == 1 for p in back.values())


def test_reference_mode_trucks_busy_from_earlier_plans(tmp_path):
    ds = load(_write(tmp_path / "d", 200, {"SX_FAC_A": (20, 75.0), "SX_FAC_B": (30, 72.0)}, fleet=2))
    evs = [_ev("EA", "SX_FAC_A", 20, 92), _ev("EB", "SX_FAC_B", 30, 102)]
    plans = P.plan_failures(ds, evs, mode="reference")
    a, b = plans["EA"], plans["EB"]
    assert a.summary == "1 truck, 2 trips (~$1,600)" and a.trucks_free == 2 and a.notes == []
    assert b.trucks_free == 1 and b.notes == ["1 of 2 trucks free; the rest are on earlier events"]
    assert evs[0].trips_at == T0 + 300 * 20 + 204 * 60 and evs[0].level_at_onset_pct == 75.0
    for e in evs:
        assert plans[e.id].cost == pytest.approx(
            Exact(ds, e.facility, e.onset, e.level_at_onset_pct, e.repair_end).best(plans[e.id].trucks_free), abs=0.006)
    # the cut-off: only failures that started by start_ts are planned, from rows up to start_ts only
    cut = P.plan_failures(ds, [_ev("EA", "SX_FAC_A", 20, 92), _ev("EB", "SX_FAC_B", 30, 102)],
                          start_ts=T0 + 300 * 25, mode="reference")
    assert set(cut) == {"EA"}
    ds2 = load(_write(tmp_path / "e", 200, {"SX_FAC_A": (20, 75.0), "SX_FAC_B": (30, 72.0)}, fleet=2))
    lv = ds2.values("SX_TANK_A", "level_pct")
    for i in range(26, 200):
        lv[i] = 10.0
    cut2 = P.plan_failures(ds2, [_ev("EA", "SX_FAC_A", 20, 92)], start_ts=T0 + 300 * 25, mode="reference")
    assert (cut2["EA"].summary, cut2["EA"].cost) == (cut["EA"].summary, cut["EA"].cost)


def test_already_tripped_and_never_trips(tmp_path):
    ds = load(_write(tmp_path / "d", 71, {"SX_FAC_A": (20, 75.0), "SX_FAC_B": (20, 40.0)}))
    evs = [_ev("EA", "SX_FAC_A", 20), _ev("EB", "SX_FAC_B", 20)]
    plans = P.plan_failures(ds, evs)
    a, b = plans["EA"], plans["EB"]
    # A tripped 204 min after its onset (row 20 + 40.8 rows); now is row 70, 110 min before the repair end
    assert evs[0].tripped and evs[0].level_pct == 92.0 and evs[0].hours_to_limit["p50"] == 0.0
    assert a.actions == [{"type": "none"}] and a.trips and a.trip_at == T0 + 300 * 20 + 204 * 60
    assert a.cost == pytest.approx(117500 / 24 * 110 / 60, abs=0.01) and a.expires_at is None
    assert a.summary == "none (already tripped; wells shut in until the repair, ~$8,976)"
    assert b.summary.startswith("none (peaks ~") and b.summary.endswith("%, never trips)") and b.cost == 0.0
    assert not b.trips and b.expires_at is None
    with pytest.raises(ValueError):
        P.plan_failures(ds, evs, mode="later")


def test_alerts_order_rules():
    def ev(eid, cat, htl=None, rev=None, onset=0, status="ongoing", tripped=False):
        e = Event(id=eid, category=cat, system="SX", facility="F", asset="A", onset=onset, end=None,
                  status=status, confidence=1.0)
        e.hours_to_limit = None if htl is None else {"p50": htl}
        e.lost_revenue_per_hour, e.tripped = rev, tripped
        return e

    events = [ev("leak", "pipeline_leak", onset=5), ev("p_slow", "pump_failure", 3.0, 100.0),
              ev("p_tie_low", "pump_failure", 2.0, 100.0), ev("worn", "pump_degradation", onset=1),
              ev("p_tie_high", "pump_failure", 2.0, 500.0), ev("p_done", "pump_failure", 0.5, 1.0, status="resolved"),
              ev("p_tripped", "pump_failure", None, 1.0, tripped=True), ev("other", "other", onset=0)]
    order = [e.id for e in P.alerts_order(events, {})]
    assert order == ["p_tripped", "p_tie_high", "p_tie_low", "p_slow", "worn", "leak", "other"]


def test_wait_curve_trip_while_waiting_costs_the_whole_shutdown(tmp_path):
    ds = load(_write(tmp_path / "d", 41, {"SX_FAC_A": (20, 75.0)}))
    e = _ev("E1", "SX_FAC_A", 20)
    plan = P.plan_failures(ds, [e])["E1"]
    curve = P.wait_curve(ds, [e], "E1", step_min=10)
    assert curve[0]["cost"] == pytest.approx(plan.cost, abs=0.01)
    costs = [c["cost"] for c in curve]
    assert all(b >= a - 0.01 for a, b in zip(costs, costs[1:]))
    # left alone from its plan level at now (R2: 83.3 %), A trips (92 - 83.3) / 5 h = 104.4 min
    # after now; waiting past that costs the wells' revenue from then to the repair end (260 min)
    assert e.level_pct == 83.3
    whole = 117500 / 24 * (260 - 104.4) / 60
    assert curve[-1]["cost"] == pytest.approx(whole, abs=0.01)
    assert curve[-1]["summary"].startswith("none (already tripped")
    assert P.wait_curve(ds, [e], "E1", until=ds.now + 1800)[-1]["delay_min"] == 25
    with pytest.raises(ValueError):
        P.wait_curve(ds, [e], "missing")


def test_wait_curve_other_failures_keep_their_trucks(tmp_path):
    """Waiting on one failure: the others go ahead with their plans from now, so their trucks are
    out until busy_until; a truck that comes back counts again."""
    ds = load(_write(tmp_path / "d", 41, {"SX_FAC_A": (20, 70.0), "SX_FAC_B": (40, 65.0)}, fleet=1))
    evs = [_ev("EA", "SX_FAC_A", 20), _ev("EB", "SX_FAC_B", 40)]
    plans = P.plan_failures(ds, evs)
    assert plans["EA"].summary == "1 truck, 1 trip (~$800)" and plans["EA"].busy_until == [ds.now + 260 * 60]
    assert plans["EB"].summary == "shut in SX_W_B1 at +262 min, ~49 min before the trip (~$204)"
    eb = P.wait_curve(ds, evs, "EB", step_min=20)
    assert eb[0]["cost"] == plans["EB"].cost and eb[0]["summary"] == plans["EB"].summary
    assert [r["trucks_free"] for r in eb] == [0 if r["delay_min"] < 260 else 1 for r in eb]
    assert eb[-1]["delay_min"] == 340 and eb[-1]["cost"] == pytest.approx(1504.5, abs=0.01)
    costs = [r["cost"] for r in eb]
    assert all(b >= a for a, b in zip(costs, costs[1:]))
    ea = P.wait_curve(ds, evs, "EA", step_min=20)
    assert {r["trucks_free"] for r in ea} == {1} and ea[0]["summary"] == plans["EA"].summary


def test_search_equals_brute_force_on_random_tanks(tmp_path):
    """Property: the closed-form search equals the every-minute brute force on random tanks."""
    ds = load(_write(tmp_path / "d", 3, {}))
    rng = random.Random(20261108)
    for case in range(40):
        f = ds.facilities[rng.choice(sorted(ds.facilities))]
        wells = tuple(w for w in f.well_ids if rng.random() < 0.85) or (f.well_ids[0],)
        level = rng.randint(300, 919) / 10.0
        horizon = 5 * rng.randint(1, 72)
        trucks = rng.randint(0, 3)
        ctx = P._Ctx(ds, None, f, "R", T0, T0 + 60 * horizon, level, wells)
        ours = P._by_trucks(ctx.candidates(trucks), trucks)[trucks]
        brute = Exact(ds, f.id, T0, level, T0 + 60 * horizon, wells).best(trucks)
        assert ours.cost == pytest.approx(brute, abs=1e-6), (case, f.id, wells, level, horizon, trucks)
        # and the replay agrees with the chosen plan
        r = P.simulate(ds, f.id, T0, level, T0 + 60 * horizon, P._actions(ctx, ours), wells_on=list(wells))
        assert r["cost"] == pytest.approx(ours.cost, abs=0.006) and (r["trip_at"] is None) == (not ours.trips)


def test_physics_run_edges():
    # starts at the limit: trips at once and holds
    r = physics.run(100.0, 100.0, 1.0, 60.0)
    assert r.trip_min == 0.0 and r.peak_bbl == 100.0
    # no inflow never trips; a round at the horizon never happens
    r = physics.run(10.0, 100.0, 0.0, 60.0, rounds=[(60.0, 1)], load_bbl=5.0)
    assert r.trip_min is None and r.rounds == [] and r.end_bbl == 10.0
    # a shut-in that cuts more than the inflow leaves the level flat
    r = physics.run(10.0, 100.0, 1.0, 60.0, shutins=[(10.0, 5.0)], want_knots=True)
    assert r.end_bbl == pytest.approx(20.0) and r.shut_min == [50.0]
    assert physics.level_at(r.knots, 5.0) == pytest.approx(15.0)
    assert physics.level_at(r.knots, 30.0) == pytest.approx(20.0)
