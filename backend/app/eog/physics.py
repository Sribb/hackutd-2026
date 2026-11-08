"""Tank replay for a failed pump (SPEC.md "Plans"): deterministic, event-driven, exact in time.

A failed pump moves nothing, so between events the tank fills in a straight line at the inflow of
the wells still producing. Two kinds of event change that line:

- a truck round: `trucks` trucks arrive together and each takes `load_bbl` or whatever is left,
  so the round removes min(trucks * load_bbl, water in the tank);
- a shut-in: from that minute the inflow drops by the shut wells' water.

The tank trips the moment its volume reaches the shutdown volume. SPEC and README say the wells
shut in when the tank *reaches* shutdown, so touching it exactly counts as a trip (plan.py has the
answer-key evidence, EVENT_21). After a trip every well is off and the level holds until the repair
ends; a truck that arrives after the trip removes nothing (it was still called, so it is still paid
for). Events at or after the horizon never happen: a trip must arrive before the repair ends.

Times are minutes from the plan start, volumes bbl, rates bbl/min. No clock is read anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

EPS_BBL = 1e-9   # a volume within this of the shutdown volume counts as reaching it


@dataclass
class Run:
    trip_min: Optional[float]          # when the tank reaches shutdown; None if not before the horizon
    peak_bbl: float                    # highest volume over [0, horizon]
    end_bbl: float                     # volume at the horizon
    removed_bbl: float                 # water the trucks actually took
    rounds: list = field(default_factory=list)    # (minute, trucks, bbl removed) per round before the horizon
    shut_min: list = field(default_factory=list)  # per shut-in group: minutes its wells were off by the plan
    knots: list = field(default_factory=list)     # (minute, bbl) trajectory, two knots at a jump


def run(v0: float, shutdown_bbl: float, inflow: float, horizon: float,
        rounds: Sequence[tuple[float, int]] = (), shutins: Sequence[tuple[float, float]] = (),
        load_bbl: float = 0.0, want_knots: bool = False) -> Run:
    """Replay one tank from minute 0 to `horizon`.

    rounds:  [(minute, trucks)] truck arrivals.   shutins: [(minute, bbl_per_min)] inflow cuts.
    The volume is piecewise linear and only rises between events, so its maximum over any stretch
    is at the stretch's end: checking just before every event and at the horizon is exact.
    """
    events = sorted([(max(0.0, float(m)), 0, i, float(d)) for i, (m, d) in enumerate(shutins)] +
                    [(max(0.0, float(m)), 1, i, int(n)) for i, (m, n) in enumerate(rounds)])
    t, v, q = 0.0, float(v0), max(0.0, float(inflow))
    peak, removed = v, 0.0
    knots = [(0.0, v)] if want_knots else []
    done: list = []
    trip: Optional[float] = None
    handled = 0
    if v >= shutdown_bbl - EPS_BBL:
        trip = 0.0
    else:
        for m, kind, _, val in events:
            if m >= horizon:
                break
            v_m = v + q * (m - t)
            if v_m >= shutdown_bbl - EPS_BBL:
                trip = t + (shutdown_bbl - v) / q        # q > 0 here: v was below, v_m is not
                break
            t, v = m, v_m
            peak = max(peak, v)
            if want_knots:
                knots.append((t, v))
            if kind == 1:
                take = min(val * load_bbl, v)
                v -= take
                removed += take
                done.append((m, val, take))
                if want_knots:
                    knots.append((t, v))
            else:
                q = max(0.0, q - val)
            handled += 1
        if trip is None:
            v_h = v + q * (horizon - t)
            if v_h >= shutdown_bbl - EPS_BBL:
                trip = t + (shutdown_bbl - v) / q if q > 0 else t
            else:
                t, v = float(horizon), v_h
                peak = max(peak, v)
                if want_knots:
                    knots.append((t, v))
    if trip is not None:
        peak = max(peak, shutdown_bbl)
        v = shutdown_bbl
        if want_knots:
            knots.append((trip, shutdown_bbl))
            if horizon > trip:
                knots.append((float(horizon), shutdown_bbl))
        # rounds still due after the trip arrive, take nothing, and are still paid for
        for m, kind, _, val in events[handled:]:
            if kind == 1 and m < horizon:
                done.append((m, val, 0.0))
    stop = horizon if trip is None else min(trip, horizon)
    shut = [max(0.0, stop - max(0.0, m)) if m < horizon else 0.0 for m, _ in shutins]
    return Run(trip_min=trip, peak_bbl=peak, end_bbl=v, removed_bbl=removed, rounds=done,
               shut_min=shut, knots=knots)


def level_at(knots: Sequence[tuple[float, float]], minute: float) -> float:
    """The volume at `minute` from a knot list; at a jump it is the value after the jump."""
    for i in range(len(knots) - 1, -1, -1):
        t_i, v_i = knots[i]
        if t_i <= minute:
            if i + 1 < len(knots):
                t_n, v_n = knots[i + 1]
                if t_n > t_i:
                    return v_i + (v_n - v_i) * (minute - t_i) / (t_n - t_i)
            return v_i
    return knots[0][1]
