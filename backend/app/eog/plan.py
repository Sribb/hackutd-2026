"""The cheapest plan for each pump failure (SPEC.md "Plans", "Shared trucks", "Alerts view"), the
tank replay behind what-if, and Last Call (how long a plan stays open, and the price of waiting).

Public API
    simulate(ds, facility_id, start_ts, level_pct, repair_end, actions, inflow_bpd=None, wells_on=None) -> dict
    plan_failures(ds, events, start_ts=None, busy=None, mode="now") -> dict[event_id, Plan]
    alternatives(ds, event, start_ts=None, trucks_free=None) -> list[Plan]
    wait_curve(ds, events, event_id, step_min=5, until=None) -> list[dict]
    alerts_order(events, plans) -> list[Event]
    countdown(ds, event, at_ts=None) -> dict      de-noised level and countdown for one pump failure
    key_text(plan) -> str                         summary plus notes, written like the answer key

Everything comes from the dataset: capacities, wells, prices, trucks, limits and repair time. "Now"
is the view's last telemetry row, never the clock. answer_key.json is never opened here.

The search space (SPEC "Plans"): do nothing, or n trucks (n <= trucks free) each making k trips
(rounds arrive response_min + j * cycle_min after the start, every one before the repair ends),
optionally with a shut-in of any subset of the producing wells from one whole minute until the
repair ends. A plan must keep the tank below shutdown; doing nothing is the only plan allowed to
trip. The search is exact: for each (n, k, subset) the latest feasible shut-in minute is found
(feasibility is monotone in the start minute: shutting in earlier never raises the level), and
the result is checked against a minute-by-minute brute force in tests/test_eog_plan.py.

Rules SPEC leaves open, reverse-engineered from the practice answer keys (the keys are test
fixtures only; this module never reads them):

R1 De-noising. Readings carry noise (sd about 0.08 points), so the level is a least-squares fit,
   not one reading. While a pump is failed the tank rises in a straight line at the producing wells'
   baseline water (fitted slopes match baseline/24/capacity within 0.02 %/h on all 13 history
   failures). Before the onset the level follows the run of the same pump state with slope
   (inflow - pump flow), and it is continuous at the onset. The level at the onset is the common
   intercept of: the fill rows from the onset (known slope) and up to PRE_ROWS rows of the run
   before it, whose slope uses the pump's flow readings fitted as a line in time (a wearing pump's
   flow falls during the run). Rows whose fitted level is within CLAMP_MARGIN_PCT of shutdown are
   dropped (readings clamp at the limit). With this fit every history level_pct and trips_at and
   every snapshot level_final_pct and hours_to_limit_final is reproduced, for any pre-onset window
   from 12 to 60 rows. What fails: the reading itself (EVENT_21's onset reads 54.92 and puts the
   shut-in at +53 min, key +51); fill rows only (EVENT_20 trips at 298.47 min, i.e. 12:28, key 12:29;
   snapshot EVENT_15 at 69.5 %, key 69.6); a free pre-onset slope (snapshot EVENT_15 at 2.32 h,
   key 2.30).
R2 Plans start from the level rounded to 0.1 %, the key's precision: history plans from
   round(L_onset, 1), plans at "now" from round(round(L_onset, 1) + fill * elapsed, 1).
   EVENT_21 at onset: the key's "+51 min" needs exactly 55.0 % with R3; the unrounded 54.99 %
   gives +52. Snapshot 2026-10-10T0900 EVENT_20: "+108 min, ~100 min before the trip" needs
   59.2 %; the unrounded 59.18 % gives +109. That two-stage value equals level_final_pct on all
   seven ongoing snapshot failures, where rounding the fit at "now" directly misses EVENT_15.
R3 Reaching shutdown exactly is a trip (README: the wells shut in when the tank *reaches* it).
   EVENT_21 at onset (440 bbl, 75 bbl/h; S4_WELL_A2 cuts 30 bbl/h): starting at +52 min the tank
   ends at exactly 736.0 bbl = 92 %, and the key picks +51.
R4 The countdown comes from the unrounded fit, rounded to the minute (half up): trips_at is the
   minute the fitted line reaches shutdown, hours_to_limit is (trips_at - now) / 3600 to two
   decimals. EVENT_14: from 45.0 % the crossing is 304.56 min (01:05) but the key says 01:04, the
   fit (45.04 %) gives 304.3 min. Snapshot 2026-10-08T1200 EVENT_11: 1.42 h is 85 whole minutes to
   the minute-rounded trip (the unrounded 84.6 min would be 1.41).
R5 Shut-ins start on whole minutes after the plan start, and a plan takes the latest minute that
   still keeps the tank below shutdown. EVENT_13 +189 (bound 189.8), EVENT_18 +306, and README's
   S1_WELL_C2 at 02:25 (+125).
R6 Equal-cost plans: fewer trucks, then fewer trips per truck, then fewer wells, then the later
   shut-in; a plan that keeps the tank below shutdown beats doing nothing at the same cost.
   EVENT_15 at onset: "1 truck, 2 trips" and "2 trucks, 1 trip each" both cost $1,810 and both
   work; the key picks the single truck. At the 2026-10-09T0330 snapshot a single truck's second
   trip would arrive at 08:50, after the 08:00 repair end, so "2 trucks, 1 trip each" is the only
   $1,810 plan there: one rule explains both. Joint plans with equal totals prefer fewer trucks,
   then more trucks for the earlier onset (no key evidence either way; it is deterministic).
R7 "~N min before the trip" is round(trip - start minute), the trip being the plan's own
   no-action trip from its rounded level: snapshot EVENT_20 gives 208.4 - 108 = ~100, while the
   label's 12:29 would give 101.
R8 "~$N" is the cost to the nearest dollar with Python's round (model.money); costs keep cents.
   No key cost sits on a half dollar ($3,384.375, $2,414.06, $478.125, $975.31, $1,298.33 ...).
R9 Doing nothing costs the producing wells' revenue from the exact crossing to the repair end,
   not from the rounded minute: README's EVENT_9 is $3,300/h * 91.2 min = $5,016.
R10 History trucks stay busy until cycle_min after their last trip (start + response + k * cycle)
   and are free again once that time is at or before the next onset: the key's "(1 of 2 trucks
   free ...)" notes on EVENT_12, 20 and 23 and "(0 of 2 ...)" on EVENT_13 and 21.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from itertools import combinations
from typing import Iterable, Mapping, Optional, Sequence, Union

from . import physics
from .dataset import Dataset, DatasetError, Facility
from .model import Event, Plan, money

PRE_ROWS = 36               # rows (3 h) of the pump run before the onset used by the fit (R1)
CLAMP_MARGIN_PCT = 0.3      # drop rows fitted this close to shutdown: readings clamp there (R1)
MIN_TREND_ROWS = 3          # fewer pre-onset flow readings than this: use their mean, no trend
DEFAULT_SD_PCT = 0.08       # reading noise assumed when a fit has too few rows to measure it
Z90 = 1.2815515655446004    # p10 / p90 of a normal distribution
COST_TOL = 1e-6             # dollars: costs closer than this are equal for tie-breaks
FREE_NOTE = "{free} of {fleet} trucks free; the rest are on earlier events"
SHARE_NOTE = "shares {system}'s {trucks} with {others}"

Busy = Optional[Mapping[str, Union[int, Sequence[int]]]]


# ---------------------------------------------------------------- small helpers
def _half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def _minute_ts(ts: float) -> int:
    """A timestamp rounded to the nearest whole minute (half up), like the key's trips_at."""
    return _half_up(ts / 60.0) * 60


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _repair_s(ds: Dataset) -> int:
    return int(round(ds.repair_hours * 3600))


def _col(ds: Dataset, asset: str, metric: str):
    try:
        return ds.values(asset, metric)
    except DatasetError:
        return None


def key_text(plan: Plan) -> str:
    """The plan the way the answer key writes reference_action: summary, then each note in ()."""
    return plan.summary + "".join(f" ({n})" for n in plan.notes)


# ---------------------------------------------------------------- de-noising and countdown (R1, R2, R4)
@dataclass
class _Tank:
    """One failed pump's tank, fitted from the telemetry (R1)."""
    event: Event
    f: Facility
    onset: int
    repair_end: int
    shutdown_pct: float
    fit_pct: float               # unrounded level at the onset
    rate_pct_h: float            # fill rate while the pump is failed
    q_bpd: float                 # inflow of the producing wells
    wells_on: tuple              # wells producing at the onset
    se_pct: float
    rows_post: int
    rows_pre: int
    trip_row_ts: Optional[int]   # first visible row after the onset with every producing well off

    @property
    def onset_pct(self) -> float:
        return round(self.fit_pct, 1)

    def plan_level(self, at: int) -> float:
        """R2: the level a plan starting at `at` uses (0.1 % steps, capped at shutdown)."""
        lvl = round(self.onset_pct + self.rate_pct_h * (at - self.onset) / 3600.0, 1)
        return min(lvl, self.shutdown_pct)

    def est_level(self, at: int) -> float:
        return min(self.fit_pct + self.rate_pct_h * (at - self.onset) / 3600.0, self.shutdown_pct)

    def cross_ts(self) -> Optional[float]:
        """When the fitted line reaches shutdown (exact), or None with no inflow."""
        if self.rate_pct_h <= 0:
            return None if self.fit_pct < self.shutdown_pct else float(self.onset)
        t = self.onset + (self.shutdown_pct - self.fit_pct) / self.rate_pct_h * 3600.0
        if self.trip_row_ts is not None and self.trip_row_ts < t:
            t = float(self.trip_row_ts)      # the readings show the trip earlier than the fit
        return max(t, float(self.onset))

    def tripped_at(self, at: int) -> bool:
        if self.trip_row_ts is not None and self.trip_row_ts <= at:
            return True
        return self.plan_level(at) >= self.shutdown_pct - 1e-9


def _producing(ds: Dataset, f: Facility, i: int) -> tuple:
    out = []
    for w in f.wells:
        col = _col(ds, w.id, "status")
        v = col[i] if (col is not None and 0 <= i < len(col)) else float("nan")
        if math.isnan(v) or v != 0:
            out.append(w.id)
    return tuple(out)


def _pre_rows(ds: Dataset, f: Facility, i_on: int, onset: int, wells_on: tuple):
    """(x_hours, level, flow) for the run of one pump state just before the onset row."""
    if i_on <= 0:
        return []
    lv, ps, pf = ds.values(f.tank_id, "level_pct"), _col(ds, f.pump_id, "status"), _col(ds, f.pump_id, "flow_bpd")
    if ps is None:
        return []
    st = ps[i_on - 1]
    if math.isnan(st):
        return []
    rows, k, cut = [], i_on - 1, False
    while k >= 0 and len(rows) < PRE_ROWS:
        if ps[k] != st or _producing(ds, f, k) != wells_on or math.isnan(lv[k]):
            cut = True
            break
        flow = pf[k] if pf is not None and not math.isnan(pf[k]) else (0.0 if st != 1 else float("nan"))
        rows.append(((int(ds.times[k]) - onset) / 3600.0, lv[k], flow))
        k -= 1
    if cut and rows:
        rows.pop()        # the row where the state switched still carries the previous state's level
    return rows[::-1]


def _fit_tank(ds: Dataset, event: Event, data_until: int) -> _Tank:
    """R1 fit over the visible rows up to data_until (never later rows: no peeking in time travel)."""
    if event.facility not in ds.facilities:
        raise DatasetError(f"{event.facility} is not a facility in this dataset.")
    f = ds.facilities[event.facility]
    onset = int(event.onset)
    repair_end = onset + _repair_s(ds)
    S = ds.shutdown_pct
    lv = ds.values(f.tank_id, "level_pct")
    end = bisect_right(ds.times, int(data_until), 0, ds.end)
    i_on = bisect_left(ds.times, onset, 0, ds.end)
    wells_on = _producing(ds, f, min(i_on, ds.end - 1))
    q_bpd = sum(ds.wells[w].baseline_water_bpd for w in wells_on)
    k = 100.0 / 24.0 / f.capacity_bbl          # %/h per bpd
    r = q_bpd * k

    post, trip_row = [], None
    last = min(end, bisect_left(ds.times, repair_end, 0, ds.end))
    for i in range(i_on, last):
        y = lv[i]
        if i > i_on and _producing(ds, f, i) != wells_on:
            break                        # the inflow changed (a trip, or wells switched)
        if math.isnan(y):
            continue
        if y >= S - 1e-9:
            break                        # clamped at the limit: nothing more to learn here
        post.append(((int(ds.times[i]) - onset) / 3600.0, y))
    for i in range(i_on + 1, last):      # the automatic shut-in: every producing well goes off
        if not _producing(ds, f, i):
            trip_row = int(ds.times[i])
            break

    pre = _pre_rows(ds, f, min(i_on, end), onset, wells_on) if i_on <= end else []
    flows = [(x, p) for x, _, p in pre if not math.isnan(p)]
    if len(flows) >= MIN_TREND_ROWS:
        mx = sum(x for x, _ in flows) / len(flows)
        mp = sum(p for _, p in flows) / len(flows)
        sxx = sum((x - mx) ** 2 for x, _ in flows)
        g = sum((x - mx) * (p - mp) for x, p in flows) / sxx if sxx > 0 else 0.0
        p0 = mp - g * mx
    elif flows:
        g, p0 = 0.0, sum(p for _, p in flows) / len(flows)
    else:
        g, p0 = 0.0, 0.0

    def shape_pre(x: float) -> float:   # level change from the onset back to x (< 0), SPEC formula
        return (q_bpd - p0) * k * x - g * k * x * x / 2.0

    def solve(post_rows, pre_rows):
        terms = [y - r * x for x, y in post_rows] + [y - shape_pre(x) for x, y, _ in pre_rows]
        return sum(terms) / len(terms), terms

    pre_rows = [row for row in pre if not math.isnan(row[2])]
    if not post and not pre_rows:
        v = ds.value_at(f.tank_id, "level_pct", min(onset, int(ds.times[end - 1])) if end else onset)
        if v is None:
            raise DatasetError(f"No level readings for {f.tank_id} around {onset}.")
        return _Tank(event, f, onset, repair_end, S, v, r, q_bpd, wells_on, DEFAULT_SD_PCT, 0, 0, trip_row)
    a, _ = solve(post, pre_rows)
    kept = [(x, y) for x, y in post if a + r * x < S - CLAMP_MARGIN_PCT]
    if kept or pre_rows:
        post = kept
        a, terms = solve(post, pre_rows)
    else:
        _, terms = solve(post, pre_rows)
    n = len(terms)
    sd = math.sqrt(sum((t - a) ** 2 for t in terms) / (n - 1)) if n >= 3 else DEFAULT_SD_PCT
    return _Tank(event, f, onset, repair_end, S, a, r, q_bpd, wells_on, sd / math.sqrt(n), len(post),
                 len(pre_rows), trip_row)


def _countdown_dict(ds: Dataset, tank: _Tank, at: int) -> dict:
    cross = tank.cross_ts()
    tripped = tank.tripped_at(at)
    trips_at = None
    if cross is not None and cross < tank.repair_end:
        trips_at = _minute_ts(cross)
    if tripped:
        htl = {"p50": 0.0, "p10": 0.0, "p90": 0.0}
    elif cross is None:
        htl = None
    else:
        p50 = max(0.0, (_minute_ts(cross) - at) / 3600.0)
        sig = tank.se_pct / tank.rate_pct_h if tank.rate_pct_h > 0 else 0.0
        htl = {"p50": round(p50, 2), "p10": round(max(0.0, p50 - Z90 * sig), 2), "p90": round(p50 + Z90 * sig, 2)}
    return {
        "event_id": tank.event.id,
        "facility_id": tank.f.id,
        "at": at,
        "onset": tank.onset,
        "repair_end": tank.repair_end,
        "level_at_onset_pct": tank.onset_pct,
        "level_at_onset_fit_pct": round(tank.fit_pct, 4),
        "level_pct": tank.shutdown_pct if tripped else tank.plan_level(at),
        "level_fit_pct": round(tank.est_level(at), 4),
        "fill_pct_per_hour": round(tank.rate_pct_h, 4),
        "inflow_bpd": tank.q_bpd,
        "wells_on": list(tank.wells_on),
        "tripped": tripped,
        "trips_at": trips_at,
        "hours_to_limit": htl,
        "lost_revenue_per_hour": sum(ds.revenue_per_hour(w.id) for w in tank.f.wells),
        "rows_used": {"after_onset": tank.rows_post, "before_onset": tank.rows_pre},
        "level_se_pct": round(tank.se_pct, 4),
    }


def countdown(ds: Dataset, event: Event, at_ts: Optional[float] = None) -> dict:
    """De-noised level and countdown for one pump failure at `at_ts` (default: the view's now).

    Uses only rows at or before at_ts. level_pct is the level a plan uses (R2); level_fit_pct the
    unrounded fit; trips_at / hours_to_limit follow R4 (hours_to_limit ignores the repair end: it is
    the countdown "if nothing changes", while trips_at is None when the repair comes first).
    """
    at = ds.now if at_ts is None else int(at_ts)
    at = max(at, int(event.onset))
    return _countdown_dict(ds, _fit_tank(ds, event, at), at)


# ---------------------------------------------------------------- one tank's search
@dataclass
class _Cand:
    n: int
    k: int
    wells: tuple
    tau: Optional[int]
    run: physics.Run
    cost: float
    truck_cost: float
    shut_cost: float
    auto_cost: float

    @property
    def trips(self) -> bool:
        return self.run.trip_min is not None

    def key(self):
        return (round(self.cost / COST_TOL) * COST_TOL, 1 if self.trips else 0, self.n, self.k,
                len(self.wells), -(self.tau or 0), self.wells)


@dataclass
class _Ctx:
    ds: Dataset
    tank: Optional[_Tank]
    f: Facility
    event_id: str
    start: int
    repair_end: int
    level_pct: float
    wells_on: tuple
    horizon: float = 0.0
    v0: float = 0.0
    shut_bbl: float = 0.0
    q: float = 0.0
    arrivals: list = field(default_factory=list)
    rev_min: dict = field(default_factory=dict)
    already_tripped: bool = False
    base: Optional[physics.Run] = None

    def __post_init__(self):
        ds, f = self.ds, self.f
        self.horizon = max(0.0, (self.repair_end - self.start) / 60.0)
        self.shut_bbl = ds.shutdown_pct / 100.0 * f.capacity_bbl
        self.v0 = self.level_pct / 100.0 * f.capacity_bbl
        self.q = sum(ds.wells[w].baseline_water_bpd for w in self.wells_on) / 1440.0
        self.arrivals = []
        a = f.response_min
        while a < self.horizon and f.cycle_min > 0:
            self.arrivals.append(a)
            a += f.cycle_min
        if f.cycle_min <= 0 and f.response_min < self.horizon:
            self.arrivals = [f.response_min]
        self.rev_min = {w: ds.revenue_per_day(w) / 1440.0 for w in self.wells_on}
        self.already_tripped = self.v0 >= self.shut_bbl - physics.EPS_BBL
        self.base = self.replay((), ())

    def replay(self, rounds, shutins) -> physics.Run:
        return physics.run(self.v0, self.shut_bbl, self.q, self.horizon, rounds, shutins,
                           self.ds.truck_capacity_bbl)

    def cand(self, n: int, k: int, wells: tuple, tau: Optional[int], run: physics.Run) -> _Cand:
        trips = sum(t for _, t, _ in run.rounds)
        truck = trips * self.ds.truck_cost
        shut = sum(self.rev_min[w] for w in wells) * run.shut_min[0] if wells else 0.0
        auto = 0.0
        if run.trip_min is not None:
            auto = sum(self.rev_min.values()) * max(0.0, self.horizon - run.trip_min)
        return _Cand(n, k, wells, tau, run, truck + shut + auto, truck, shut, auto)

    def latest_start(self, rounds, wells: tuple) -> Optional[int]:
        """R5: the latest whole minute a shut-in of `wells` can start and keep the tank below shutdown."""
        t_max = math.ceil(self.horizon) - 1
        if t_max < 0:
            return None
        cut = sum(self.ds.wells[w].baseline_water_bpd for w in wells) / 1440.0
        if cut <= 0:
            return None

        def ok(tau: int) -> bool:
            return self.replay(rounds, [(tau, cut)]).trip_min is None

        # closed-form guess from the peaks (just before each round, and the horizon)
        guess, v, t = t_max, self.v0, 0.0
        peaks = []
        for m, n in rounds:
            v += self.q * (m - t)
            peaks.append((m, v))
            v, t = max(0.0, v - n * self.ds.truck_capacity_bbl), m
        peaks.append((self.horizon, v + self.q * (self.horizon - t)))
        for p, vp in peaks:
            if vp >= self.shut_bbl - physics.EPS_BBL:
                guess = min(guess, math.ceil(p - (vp - self.shut_bbl) / cut) - 1)
        guess = max(0, min(t_max, guess))
        if ok(guess):
            if guess == t_max or not ok(guess + 1):
                return guess
            lo, hi = guess + 1, t_max
        else:
            if guess == 0 or not ok(0):
                return None
            if ok(guess - 1):
                return guess - 1
            lo, hi = 0, guess - 1
        while lo < hi:                   # lo is feasible; find the last feasible minute
            mid = (lo + hi + 1) // 2
            if ok(mid):
                lo = mid
            else:
                hi = mid - 1
        return lo

    def candidates(self, max_trucks: int) -> list:
        out = [self.cand(0, 0, (), None, self.base)]
        if self.already_tripped or self.base.trip_min is None or self.horizon <= 0:
            return out
        subsets = [c for size in range(1, len(self.wells_on) + 1) for c in combinations(self.wells_on, size)]
        configs = [(0, 0)] + [(n, k) for n in range(1, max(0, max_trucks) + 1)
                              for k in range(1, len(self.arrivals) + 1)]
        for n, k in configs:
            rounds = [(m, n) for m in self.arrivals[:k]]
            if n:
                r0 = self.replay(rounds, ())
                if r0.trip_min is None:
                    out.append(self.cand(n, k, (), None, r0))
                    continue             # a shut-in on top would only add cost
            for wells in subsets:
                tau = self.latest_start(rounds, wells)
                if tau is not None:
                    cut = sum(self.ds.wells[w].baseline_water_bpd for w in wells) / 1440.0
                    out.append(self.cand(n, k, wells, tau, self.replay(rounds, [(tau, cut)])))
        return out


def _best(cands: Iterable[_Cand]) -> _Cand:
    return min(cands, key=lambda c: c.key())


def _by_trucks(cands: list, max_trucks: int) -> list:
    """opt[m] = the best candidate using at most m trucks, m = 0..max_trucks."""
    return [_best(c for c in cands if c.n <= m) for m in range(max(0, max_trucks) + 1)]


def _summary(ctx: _Ctx, c: _Cand) -> str:
    if c.n == 0 and not c.wells:
        if ctx.already_tripped:
            return f"none (already tripped; wells shut in until the repair, ~{money(c.cost)})"
        if c.trips:
            return f"none (trips at +{_half_up(c.run.trip_min)} min, ~{money(c.cost)})"
        return f"none (peaks ~{c.run.peak_bbl / ctx.f.capacity_bbl * 100:.1f}%, never trips)"
    parts = []
    if c.n:
        parts.append(f"{_plural(c.n, 'truck')}, {_plural(c.k, 'trip')}{' each' if c.n > 1 else ''}")
    if c.wells:
        text = f"shut in {', '.join(c.wells)} at +{c.tau} min"
        if not c.n and ctx.base.trip_min is not None:
            text += f", ~{_half_up(ctx.base.trip_min - c.tau)} min before the trip"
        parts.append(text)
    return " + ".join(parts) + f" (~{money(c.cost)})"


def _actions(ctx: _Ctx, c: _Cand) -> list:
    acts = []
    if c.n:
        acts.append({"type": "dispatch_truck", "facility_id": ctx.f.id, "trucks": c.n, "trips_per_truck": c.k})
    if c.wells:
        acts.append({"type": "shut_in_wells", "wells": list(c.wells), "start_after_min": c.tau})
    return acts or [{"type": "none"}]


def _expires(ctx: _Ctx, c: _Cand) -> Optional[int]:
    """Last Call: the latest start (whole minutes later) at which this same plan still works.

    Same plan = the same trucks and trips, called at the later start, and the shut-in at the same
    clock time as planned (a later start cannot move it earlier). Nothing to expire for a plan
    with no actions or one that trips anyway.
    """
    if c.trips or (c.n == 0 and not c.wells):
        return None
    offsets = [ctx.f.response_min + j * ctx.f.cycle_min for j in range(c.k)] if c.n else []
    cut = sum(ctx.ds.wells[w].baseline_water_bpd for w in c.wells) / 1440.0 if c.wells else 0.0
    last = 0
    for d in range(1, math.ceil(ctx.horizon)):
        v = ctx.v0 + ctx.q * d
        hz = ctx.horizon - d
        if v >= ctx.shut_bbl - physics.EPS_BBL or any(a >= hz for a in offsets):
            break
        if c.wells and d > c.tau:
            break
        shut = [(c.tau - d, cut)] if c.wells else []
        run = physics.run(v, ctx.shut_bbl, ctx.q, hz, [(a, c.n) for a in offsets], shut, ctx.ds.truck_capacity_bbl)
        if run.trip_min is not None:
            break
        last = d
    return ctx.start + 60 * last


def _plan(ctx: _Ctx, c: _Cand, trucks_free: int, fleet: int, shares: Sequence[str] = (),
          notes: Sequence[str] = (), expiry: bool = True) -> Plan:
    f = ctx.f
    arrivals = [ctx.start + _half_up(m * 60) for m, _, _ in c.run.rounds]
    busy_until = [ctx.start + _half_up((f.response_min + c.k * f.cycle_min) * 60)] * c.n if c.n else []
    if ctx.already_tripped and ctx.tank is not None:
        cross = ctx.tank.cross_ts()
        trip_at = _minute_ts(cross) if cross is not None else ctx.start
    else:
        trip_at = None if c.run.trip_min is None else _minute_ts(ctx.start + c.run.trip_min * 60)
    return Plan(
        event_id=ctx.event_id, facility_id=f.id, system=f.system, start=ctx.start, actions=_actions(ctx, c),
        cost=round(c.cost, 2), trips=c.trips, trip_at=trip_at,
        peak_level_pct=round(c.run.peak_bbl / f.capacity_bbl * 100.0, 3), summary=_summary(ctx, c),
        truck_cost=round(c.truck_cost, 2), shut_in_cost=round(c.shut_cost, 2),
        auto_shut_in_cost=round(c.auto_cost, 2), trucks_free=trucks_free, fleet_size=fleet,
        shares_with=list(shares), arrivals=arrivals, busy_until=busy_until,
        expires_at=_expires(ctx, c) if expiry else None, notes=list(notes),
    )


# ---------------------------------------------------------------- busy trucks and joint plans
def _busy_list(busy: Busy, system: str) -> list:
    if not busy or system not in busy:
        return []
    v = busy[system]
    if isinstance(v, (int, float)):
        return [float("inf")] * max(0, int(v))
    return [float(t) for t in v]


def _allocations(n_events: int, trucks: int):
    if n_events == 0:
        yield ()
        return
    for m in range(trucks + 1):
        for rest in _allocations(n_events - 1, trucks - m):
            yield (m,) + rest


def _joint(opts: list, trucks: int) -> list:
    """Split the system's free trucks between its tanks: lowest total cost, then fewest trucks,
    then more trucks for the earlier onset (opts are in onset order). R6."""
    best, best_key = None, None
    for alloc in _allocations(len(opts), trucks):
        pick = [opts[i][alloc[i]] for i in range(len(opts))]
        total = sum(c.cost for c in pick)
        key = (round(total / COST_TOL) * COST_TOL, sum(c.n for c in pick), tuple(-c.n for c in pick))
        if best_key is None or key < best_key:
            best, best_key = pick, key
    return best


def _ongoing_at(e: Event, at: int, repair_s: int) -> bool:
    if e.category != "pump_failure" or e.onset > at or at >= e.onset + repair_s:
        return False
    return e.end is None or e.end > at


def _fill_event(e: Event, cd: dict, history: bool) -> None:
    """Fill the countdown block of an Event where it is still empty (detect may have filled it)."""
    if e.level_at_onset_pct is None:
        e.level_at_onset_pct = cd["level_at_onset_pct"]
    if e.repair_end is None:
        e.repair_end = cd["repair_end"]
    if e.trips_at is None:
        e.trips_at = cd["trips_at"]
    if e.lost_revenue_per_hour is None:
        e.lost_revenue_per_hour = cd["lost_revenue_per_hour"]
    if not history:
        if e.level_pct is None:
            e.level_pct = cd["level_pct"]
        if e.hours_to_limit is None:
            e.hours_to_limit = cd["hours_to_limit"]
        if cd["tripped"]:
            e.tripped = True


def _plan_now(ds: Dataset, tanks: list, start: int, busy: Busy, expiry: bool = True,
              levels: Optional[Mapping[str, float]] = None) -> dict:
    """Joint plans at `start`. levels overrides the start level per event id (wait_curve)."""
    by_system: dict = {}
    for t in tanks:
        by_system.setdefault(t.f.system, []).append(t)
    out = {}
    for sid, group in by_system.items():
        group.sort(key=lambda t: (t.onset, t.event.id))
        fleet = ds.systems[sid].fleet_size
        free = max(0, fleet - sum(1 for b in _busy_list(busy, sid) if b > start))
        ctxs = []
        for t in group:
            if t.trip_row_ts is not None and t.trip_row_ts <= start:
                lvl = ds.shutdown_pct
            elif levels is not None and t.event.id in levels:
                lvl = min(levels[t.event.id], ds.shutdown_pct)
            else:
                lvl = ds.shutdown_pct if t.tripped_at(start) else t.plan_level(start)
            ctxs.append(_Ctx(ds, t, t.f, t.event.id, start, t.repair_end, lvl, t.wells_on))
        opts = [_by_trucks(c.candidates(free), free) for c in ctxs]
        pick = _joint(opts, free)
        ids = [t.event.id for t in group]
        for ctx, c in zip(ctxs, pick):
            others = [i for i in ids if i != ctx.event_id]
            notes = []
            if free < fleet:
                notes.append(FREE_NOTE.format(free=free, fleet=fleet))
            if others:
                trucks = _plural(fleet, "truck") if free == fleet else f"{_plural(free, 'free truck')} (of {fleet})"
                notes.append(SHARE_NOTE.format(system=sid, trucks=trucks, others=", ".join(others)))
            out[ctx.event_id] = _plan(ctx, c, free, fleet, others, notes, expiry)
    return out


def plan_failures(ds: Dataset, events: Sequence[Event], start_ts: Optional[float] = None,
                  busy: Busy = None, mode: str = "now") -> dict:
    """The cheapest plan for every pump failure, keyed by event id.

    mode "now": every pump failure still going at start_ts (default: the view's now) is planned
    from start_ts, jointly per system, sharing the trucks that are free then (SPEC "Shared trucks":
    nothing is dispatched in the recording, so the whole fleet unless `busy` says otherwise).
    Levels come from rows at or before start_ts (R1, R2); a start after the data extrapolates the
    fill. A tank that already tripped gets "none" with the shut-in cost still to come.

    mode "reference": history, as the answer key's reference_action. Each pump failure (onset at or
    before start_ts) is planned alone at its own onset, in onset order, from its onset level, with
    the trucks still out on earlier plans in its system unavailable (R10).

    busy: {system_id: [busy_until_ts, ...]} or {system_id: count} for trucks already out.
    The countdown block of each planned Event is filled where it is empty.
    """
    if mode not in ("now", "reference"):
        raise ValueError(f"mode must be 'now' or 'reference', got {mode!r}.")
    repair_s = _repair_s(ds)
    start = ds.now if start_ts is None else int(start_ts)
    if mode == "now":
        tanks = []
        for e in events:
            if _ongoing_at(e, start, repair_s):
                t = _fit_tank(ds, e, min(start, ds.now))
                tanks.append(t)
                _fill_event(e, _countdown_dict(ds, t, start), history=False)
        return _plan_now(ds, tanks, start, busy)

    pumps = sorted((e for e in events if e.category == "pump_failure" and e.onset <= start),
                   key=lambda e: (e.onset, e.id))
    out: dict = {}
    out_busy: dict = {}
    for e in pumps:
        t = _fit_tank(ds, e, ds.now)
        _fill_event(e, _countdown_dict(ds, t, t.onset), history=True)
        sid = t.f.system
        fleet = ds.systems[sid].fleet_size
        held = _busy_list(busy, sid) + out_busy.get(sid, [])
        free = max(0, fleet - sum(1 for b in held if b > t.onset))
        ctx = _Ctx(ds, t, t.f, e.id, t.onset, t.repair_end, t.plan_level(t.onset), t.wells_on)
        c = _by_trucks(ctx.candidates(free), free)[free]
        notes = [FREE_NOTE.format(free=free, fleet=fleet)] if free < fleet else []
        p = _plan(ctx, c, free, fleet, (), notes)
        out_busy.setdefault(sid, []).extend(p.busy_until)
        out[e.id] = p
    return out


# ---------------------------------------------------------------- alternatives, Last Call, alerts
def alternatives(ds: Dataset, event: Event, start_ts: Optional[float] = None,
                 trucks_free: Optional[int] = None) -> list:
    """Ranked options for one pump failure on its own, cheapest first (ties as R6): do nothing,
    the cheapest trucks-only plan for each truck count, each single well's latest shut-in, the
    cheapest shut-in of any set of wells, and the cheapest mix. trucks_free defaults to the
    system's whole fleet."""
    start = ds.now if start_ts is None else int(start_ts)
    t = _fit_tank(ds, event, min(start, ds.now))
    fleet = ds.systems[t.f.system].fleet_size
    free = fleet if trucks_free is None else max(0, min(fleet, int(trucks_free)))
    level = ds.shutdown_pct if t.tripped_at(start) else t.plan_level(start)
    ctx = _Ctx(ds, t, t.f, event.id, start, t.repair_end, level, t.wells_on)
    cands = ctx.candidates(free)
    picks = [cands[0]]                                   # do nothing
    works = [c for c in cands if not c.trips]
    for n in range(1, free + 1):
        trucks_only = [c for c in works if c.n == n and not c.wells]
        if trucks_only:
            picks.append(_best(trucks_only))
    for w in ctx.wells_on:
        single = [c for c in works if c.n == 0 and c.wells == (w,)]
        if single:
            picks.append(_best(single))
    shut_only = [c for c in works if c.n == 0 and c.wells]
    if shut_only:
        picks.append(_best(shut_only))
    mixed = [c for c in works if c.n and c.wells]
    if mixed:
        picks.append(_best(mixed))
    seen, uniq = set(), []
    for c in sorted(picks, key=lambda c: c.key()):
        sig = (c.n, c.k, c.wells, c.tau)
        if sig not in seen:
            seen.add(sig)
            uniq.append(c)
    return [_plan(ctx, c, free, fleet) for c in uniq]


def wait_curve(ds: Dataset, events: Sequence[Event], event_id: str, step_min: int = 5,
               until: Optional[float] = None) -> list:
    """Last Call: the price of waiting. For delays 0, step, 2*step ... minutes after the view's now,
    until the repair ends (or `until`), the cheapest cost if the operator does nothing until then
    and then follows the best plan, with the system's failures re-planned together at that moment
    (shared trucks as in mode "now"). Each tank starts from its plan level at now (as
    plan_failures) and fills untouched during the wait, so delay 0 is plan_failures' plan.
    cost = revenue already lost to a trip during the wait + the plan's cost from then on, so a tank
    that trips while you wait costs its whole shutdown."""
    if step_min <= 0:
        raise ValueError("step_min must be positive.")
    target = next((e for e in events if e.id == event_id), None)
    if target is None or target.category != "pump_failure":
        raise ValueError(f"{event_id} is not a pump failure in the given events.")
    repair_s = _repair_s(ds)
    now = ds.now
    if not _ongoing_at(target, now, repair_s):
        return []
    group = [e for e in events if _ongoing_at(e, now, repair_s) and e.system == target.system]
    tanks = [_fit_tank(ds, e, now) for e in group]
    level0 = {t.event.id: (ds.shutdown_pct if t.tripped_at(now) else t.plan_level(now)) for t in tanks}
    me = next(t for t in tanks if t.event.id == event_id)
    stop = me.repair_end if until is None else min(int(until), me.repair_end)
    rev_h = sum(ds.revenue_per_hour(w) for w in me.wells_on)
    trip0 = None                                   # when my tank trips if nobody acts
    if level0[event_id] >= ds.shutdown_pct - 1e-9:
        cross = me.cross_ts()
        trip0 = float(now) if cross is None else min(cross, float(now))
    elif me.rate_pct_h > 0:
        trip0 = now + (ds.shutdown_pct - level0[event_id]) / me.rate_pct_h * 3600.0
    out = []
    d = 0
    while now + 60 * d < stop:
        s = now + 60 * d
        live = [t for t in tanks if s < t.repair_end]
        levels = {t.event.id: level0[t.event.id] + t.rate_pct_h * (s - now) / 3600.0 for t in live}
        p = _plan_now(ds, live, s, None, expiry=False, levels=levels)[event_id]
        lost = 0.0
        if trip0 is not None and trip0 < s:
            lost = rev_h * (s - max(float(now), trip0)) / 3600.0
        out.append({"delay_min": d, "start_ts": s, "cost": round(p.cost + lost, 2), "summary": p.summary})
        d += step_min
    return out


def alerts_order(events: Sequence[Event], plans: Mapping[str, Plan]) -> list:
    """SPEC "Alerts view": ongoing pump failures first, soonest to shutdown (smallest
    hours_to_limit p50) first, ties to the higher lost revenue per hour; then the ongoing leaks and
    worn pumps (then anything else) in a stable order (onset, then id).

    hours_to_limit comes from the Event (plan_failures fills it). For an Event without one, a plan
    that still trips gives its trip time; otherwise the failure sorts after those with a countdown.
    """
    def htl(e: Event) -> float:
        p = (e.hours_to_limit or {}).get("p50")
        if p is None and e.tripped:
            p = 0.0
        if p is None:
            plan = plans.get(e.id) if plans else None
            if plan is not None and plan.trips and plan.trip_at is not None:
                p = max(0.0, (plan.trip_at - plan.start) / 3600.0)
        return math.inf if p is None else float(p)

    ongoing = [e for e in events if e.status == "ongoing"]
    pumps = sorted((e for e in ongoing if e.category == "pump_failure"),
                   key=lambda e: (htl(e), -(e.lost_revenue_per_hour or 0.0), e.onset, e.id))
    rest = sorted((e for e in ongoing if e.category != "pump_failure"),
                  key=lambda e: (0 if e.category in ("pipeline_leak", "pump_degradation") else 1, e.onset, e.id))
    return pumps + rest


# ---------------------------------------------------------------- what-if replay
def _as_int(v, name: str, errors: list) -> Optional[int]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        errors.append(f"{name} must be a whole number, got {v!r}.")
        return None
    if not f.is_integer() or f < 1:
        errors.append(f"{name} must be a whole number of at least 1, got {v!r}.")
        return None
    return int(f)


def _as_minutes(v, name: str, errors: list) -> Optional[float]:
    if v is None:
        return 0.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        errors.append(f"{name} must be a number of minutes, got {v!r}.")
        return None
    if math.isnan(f) or f < 0:
        errors.append(f"{name} must be 0 or more minutes, got {v!r}.")
        return None
    return f


def simulate(ds: Dataset, facility_id: str, start_ts: float, level_pct: float, repair_end: float,
             actions: Optional[Sequence[dict]], inflow_bpd: Optional[float] = None,
             wells_on: Optional[Sequence[str]] = None) -> dict:
    """Replay one tank with a failed pump from start_ts to repair_end under `actions`
    (events.json action shapes; dispatch_truck may also carry start_after_min).

    Inflow is the baseline water of the wells still producing (wells_on, default all; inflow_bpd
    overrides the total) and each shut-in removes its wells' water from its start. Trucks arrive
    response_min after their call, then every cycle_min; a round removes min(trucks * load, water in
    the tank); rounds at or after repair_end are dropped with an error. At shutdown every well
    shuts in automatically and the level holds until repair_end, costing the producing wells'
    revenue for that time. Returns points [[ts, level_pct], ...] every 5 min plus every event time
    (two points at a truck round: before and after), trip_at (minute, or None), peak_level_pct,
    the cost parts and total, arrivals (ts of each round), removed_bbl and errors (plain English).
    """
    if facility_id not in ds.facilities:
        raise DatasetError(f"{facility_id} is not a facility in this dataset.")
    f = ds.facilities[facility_id]
    fleet = ds.systems[f.system].fleet_size
    start, end = int(start_ts), int(repair_end)
    errors: list = []
    horizon = max(0.0, (end - start) / 60.0)
    if end <= start:
        errors.append("The repair ends at or before the start, so there is nothing to replay.")
    if wells_on is None:
        on = list(f.well_ids)
    else:
        on = []
        for w in wells_on:
            if w in f.well_ids:
                if w not in on:
                    on.append(w)
            else:
                errors.append(f"{w} is not a well of {facility_id}.")
    q_bpd = float(inflow_bpd) if inflow_bpd is not None else sum(ds.wells[w].baseline_water_bpd for w in on)
    rounds: list = []
    shutins: list = []
    shut_wells: list = []
    for act in actions or []:
        typ = act.get("type") if isinstance(act, dict) else None
        if typ == "dispatch_truck":
            if act.get("facility_id", facility_id) != facility_id:
                errors.append(f"A truck action for {act.get('facility_id')} does not apply to {facility_id}.")
                continue
            n = _as_int(act.get("trucks"), "trucks", errors)
            k = _as_int(act.get("trips_per_truck"), "trips_per_truck", errors)
            d0 = _as_minutes(act.get("start_after_min"), "start_after_min", errors)
            if n is None or k is None or d0 is None:
                continue
            if n > fleet:
                errors.append(f"{n} trucks asked for, but {f.system} has {fleet}.")
            for j in range(k):
                m = d0 + f.response_min + j * f.cycle_min
                if m >= horizon:
                    errors.append(f"Trip {j + 1} of {k} would arrive after the repair ends, so it is left out.")
                    continue
                rounds.append((m, n))
        elif typ == "shut_in_wells":
            tau = _as_minutes(act.get("start_after_min"), "start_after_min", errors)
            if tau is None:
                continue
            ids = []
            for w in act.get("wells") or []:
                if w not in f.well_ids:
                    errors.append(f"{w} is not a well of {facility_id}.")
                elif w not in on:
                    errors.append(f"{w} is not producing, so shutting it in changes nothing.")
                elif w in shut_wells:
                    errors.append(f"{w} is shut in twice; the first one counts.")
                else:
                    ids.append(w)
                    shut_wells.append(w)
            if ids:
                shutins.append((tau, sum(ds.wells[w].baseline_water_bpd for w in ids) / 1440.0, ids))
        elif typ in ("none", "field_inspection"):
            continue
        else:
            errors.append(f"Unknown action type {typ!r}.")
    cap = f.capacity_bbl
    res = physics.run(float(level_pct) / 100.0 * cap, ds.shutdown_pct / 100.0 * cap, q_bpd / 1440.0, horizon,
                      rounds, [(m, r) for m, r, _ in shutins], ds.truck_capacity_bbl, want_knots=True)
    rev_min = {w: ds.revenue_per_day(w) / 1440.0 for w in on}
    truck_cost = float(sum(n for _, n, _ in res.rounds) * ds.truck_cost)
    shut_cost = float(sum(sum(rev_min[w] for w in ids) * res.shut_min[i] for i, (_, _, ids) in enumerate(shutins)))
    auto_cost = sum(rev_min.values()) * max(0.0, horizon - res.trip_min) if res.trip_min is not None else 0.0
    knots = res.knots
    grid = [5.0 * i for i in range(int(horizon // 5) + 1)]
    if horizon not in grid:
        grid.append(horizon)
    merged = sorted(list(knots) + [(m, physics.level_at(knots, m)) for m in grid], key=lambda p: p[0])
    points: list = []
    for m, v in merged:
        pt = [start + _half_up(m * 60), round(v / cap * 100.0, 4)]
        if not points or points[-1] != pt:
            points.append(pt)
    return {
        "points": points,
        "trip_at": None if res.trip_min is None else _minute_ts(start + res.trip_min * 60),
        "peak_level_pct": round(res.peak_bbl / cap * 100.0, 4),
        "truck_cost": round(truck_cost, 2),
        "shut_in_cost": round(shut_cost, 2),
        "auto_shut_in_cost": round(auto_cost, 2),
        "cost": round(truck_cost + shut_cost + auto_cost, 2),
        "arrivals": [start + _half_up(m * 60) for m, _, _ in res.rounds],
        "removed_bbl": round(res.removed_bbl, 4),
        "errors": errors,
    }
