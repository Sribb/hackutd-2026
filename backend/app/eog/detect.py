"""Find every problem in the readings of one dataset view: pump failures, worn pumps and leaks.

`detect(ds)` returns every event with onset <= ds.now, in onset order, numbered EV-0001, EV-0002...
It reads only rows visible in the view (index < ds.end), and the decision to raise or close a
problem at a row uses only that row and earlier ones. A snapshot, a time-travel view (`ds.at(t)`)
and the full week therefore agree: an event raised at one "now" is raised, with the same
`detected_at`, at every later "now". `countdown(ds, event)` fills a pump failure's countdown block
(level, hours to the shutdown limit, trip time) from plan.countdown, the one countdown in the app.

How each problem shows up was worked out from history/full_week and its answer key. No rule names
a network, an asset, a size, a price or a time: every threshold is in units of a reading's own
noise, measured from the data, or comes from the asset's parameters in network.json.

pump_failure      The pump reports status 2 (fault). Flow and receive flow drop to 0, both pressures
                  fall to the disposal pressure and the tank fills at its inflow. Onset is the first
                  status-2 row and the end the first row without it, so both are exact to the row.
pipeline_leak     Visible only while the pump runs: pressure at the disposal end drops below its
                  healthy running level and less water arrives than the pump sends, while pump flow
                  and discharge stay normal. The pressure drop is a step at onset (about 4.5 times
                  its noise in the history week) that keeps growing, so the onset is the row where
                  the pressure stepped down.
pump_degradation  Visible only while the pump runs: flow falls below max_flow_bpd and discharge
                  pressure below its healthy running level, from zero at onset and a little more
                  every hour, while the pipeline still receives what the pump sends. The onset is
                  where the trend, run back in time, leaves normal.

With the pump off, a leak or a worn pump cannot be seen at all (the line reads the disposal pressure
and nothing flows). A worn pump's onset in such a gap still comes from its trend; a leak's onset in
a gap comes from how much bigger than its opening step the leak was when the pump restarted (the
step is measured on leaks whose start was in view, if any); an end in a gap is the gap's middle.

Healthy running levels and noise are learned from the readings, pooled over every pump in the
network an hour at a time (the median of hourly medians; hourly MADs for the noise). Each hour is
judged only against what was learned before it, and a sick asset's readings stay out of the pool.
Each pair of channels is learned on its own, so a network without pipeline sensors still gets its
worn pumps found, and one without discharge sensors its leaks.
"""
from __future__ import annotations

import math
from bisect import bisect_left, insort
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from . import plan
from .dataset import STEP_S, Dataset, DatasetError, Facility
from .model import Event

# ---- statistics, in units of each reading's own noise; nothing here is specific to a network ----
LEAK_K, LEAK_H = 2.0, 4.0     # CUSUM for a leak: a ~4.5-sigma step raises it within 2-3 running rows
WEAR_K, WEAR_H = 0.75, 11.0   # CUSUM for a worn pump: a slow ramp from zero
END_LLR = 10.0                # nats of "healthy rather than sick" evidence that close a problem
RECENT_H = 1.0                # hours of recent rows that set a problem's expected size and its evidence
CP_BACK_H = 2.0               # hours of running rows before the first suspicious one a leak onset may move to
MIN_POOL = 20                 # readings an hour needs before it counts towards the healthy levels
MIN_HOURS, MIN_READINGS = 3, 200   # what a channel's healthy level must rest on before it is trusted
INV_SQRT2 = 1.0 / math.sqrt(2.0)
WEAR_CH, LEAK_CH = ("f", "d"), ("p", "r")


# ---------- small helpers ----------
def _median_sorted(v: list[float]) -> float:
    n = len(v)
    h = n // 2
    return v[h] if n % 2 else 0.5 * (v[h - 1] + v[h])


def _median(v) -> float:
    return _median_sorted(sorted(v))


def _fit(ts: list[float], ys: list[float]) -> Optional[tuple[float, float, float]]:
    """Least-squares line y = a + b*t. Returns (a, b, residual sd), or None if t does not vary."""
    n = len(ts)
    if n < 2:
        return None
    mt = sum(ts) / n
    my = sum(ys) / n
    stt = sum((t - mt) ** 2 for t in ts)
    if stt <= 0:
        return None
    b = sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / stt
    a = my - b * mt
    res = sum((y - a - b * t) ** 2 for t, y in zip(ts, ys))
    return a, b, (math.sqrt(res / (n - 2)) if n > 2 else 0.0)


def _hm(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _n(x: float, nd: int = 0) -> str:
    return f"{x:,.{nd}f}"


_ok = math.isfinite              # a real reading: not missing (NaN) and not infinite


def _grid_step(ds: Dataset) -> int:
    """Spacing of the telemetry grid, from the data itself (SPEC: every 5 minutes)."""
    n = min(ds.end, 64)
    diffs = sorted(int(ds.times[i + 1]) - int(ds.times[i]) for i in range(n - 1))
    mid = diffs[len(diffs) // 2] if diffs else 0
    return mid if mid > 0 else STEP_S


def _hinge(ts: list[float], chans: list[list[float]], lo: float, hi: float):
    """Fit every channel c as x_c = b_c * max(0, t - tau): healthy, then a ramp from zero that
    starts at the same tau for all channels. Times in hours. Returns (tau, [b_c]) or None.

    This runs the trend back to where it leaves normal. For tau between two rows the later rows
    are fixed, and the best tau there is a root of a quadratic (or an end of the interval)."""
    n = len(ts)
    if n < 3:
        return None
    m = len(chans)
    s0 = [0.0] * (n + 1)
    s1 = [0.0] * (n + 1)
    s2 = [0.0] * (n + 1)
    sx = [[0.0] * (n + 1) for _ in range(m)]
    stx = [[0.0] * (n + 1) for _ in range(m)]
    for j in range(n - 1, -1, -1):
        t = ts[j]
        s0[j] = s0[j + 1] + 1.0
        s1[j] = s1[j + 1] + t
        s2[j] = s2[j + 1] + t * t
        for c in range(m):
            x = chans[c][j]
            sx[c][j] = sx[c][j + 1] + x
            stx[c][j] = stx[c][j + 1] + t * x

    def gain(tau: float, j: int):
        d = s2[j] - 2.0 * tau * s1[j] + tau * tau * s0[j]
        if d <= 1e-12:
            return None
        nums = [stx[c][j] - tau * sx[c][j] for c in range(m)]
        return sum(max(0.0, v) ** 2 for v in nums) / d, [max(0.0, v) / d for v in nums]

    best = None
    for j in range(n):
        if s0[j] < 2:
            break
        a = max(lo, ts[j - 1] if j > 0 else lo)
        b = min(hi, ts[j])
        if a > b:
            continue
        cands = [a, b]
        # d/dtau of sum(N_c^2)/D = 0  ->  (Q S1 - P S0) tau^2 + (R S0 - Q S2) tau + (P S2 - R S1) = 0
        A = [stx[c][j] for c in range(m)]
        B = [sx[c][j] for c in range(m)]
        P = sum(x * y for x, y in zip(A, B))
        Q = sum(y * y for y in B)
        R = sum(x * x for x in A)
        qa, qb, qc = Q * s1[j] - P * s0[j], R * s0[j] - Q * s2[j], P * s2[j] - R * s1[j]
        if abs(qa) > 1e-12:
            disc = qb * qb - 4 * qa * qc
            if disc >= 0:
                r = math.sqrt(disc)
                cands += [(-qb + r) / (2 * qa), (-qb - r) / (2 * qa)]
        elif abs(qb) > 1e-12:
            cands.append(-qc / qb)
        for tau in cands:
            if a <= tau <= b:
                g = gain(tau, j)
                if g and (best is None or g[0] > best[0]):
                    best = (g[0], tau, g[1])
    return None if best is None else (best[1], best[2])


# ---------- healthy levels, learned causally ----------
class _Healthy:
    """Healthy running readings and their noise, pooled over every pump in the network.

    Channels: f = pump flow / max_flow_bpd, d = discharge - disposal pressure, p = pipeline
    pressure - disposal pressure, r = receive flow / pump flow. Readings are pooled an hour at a
    time; the level is the median of the hourly medians and the noise the median of the hourly MADs
    (scaled to a standard deviation), so a few sick assets cannot move either. A channel enters
    `ref` once it rests on MIN_HOURS hourly estimates and MIN_READINGS readings: a noise level
    taken from a few dozen readings can be a third too small, and every alarm is in its units."""
    CH = WEAR_CH + LEAK_CH

    def __init__(self):
        self.meds = {c: [] for c in self.CH}
        self.mads = {c: [] for c in self.CH}
        self.count = {c: 0 for c in self.CH}
        self.carry = {c: [] for c in self.CH}      # a small network's hour waits for more readings
        self.ref: dict[str, tuple[float, float]] = {}

    def has(self, chans: tuple[str, ...]) -> bool:
        return all(c in self.ref for c in chans)

    def add(self, pool: dict[str, list[float]]) -> None:
        for c in self.CH:
            v = self.carry[c] + pool[c]
            if len(v) < MIN_POOL:
                self.carry[c] = v
                continue
            self.carry[c] = []
            v.sort()
            m = _median_sorted(v)
            insort(self.meds[c], m)
            insort(self.mads[c], _median([abs(x - m) for x in v]))
            self.count[c] += len(v)
            if len(self.meds[c]) < MIN_HOURS or self.count[c] < MIN_READINGS:
                continue
            level = _median_sorted(self.meds[c])
            sd = 1.4826 * _median_sorted(self.mads[c])
            # a floor for noise-free inputs, well under any real sensor's noise
            floor = 0.002 if c in ("f", "r") else max(0.5, 0.01 * abs(level))
            self.ref[c] = (level, max(sd, floor))


# ---------- one CUSUM watch on one asset ----------
class _Record:
    """A problem a watch raised. Positions index the watch's segment rows."""
    __slots__ = ("idx", "us", "vs", "xs", "cp", "alarm", "end_pos", "end_row", "how", "recent", "R",
                 "rstart")

    def __init__(self, w: "_Watch", cp: int, alarm: int):
        self.idx, self.us, self.vs, self.xs = w.idx, w.us, w.vs, w.xs   # shared while open
        self.cp = cp                         # position where the evidence started building
        self.alarm = alarm                   # row index where the test fired (detected_at)
        self.end_pos: Optional[int] = None   # position of the first healthy row, closed by readings
        self.end_row: Optional[int] = None   # row index of the end, closed by a pump failure
        self.how: Optional[str] = None       # "normal" or "failure"
        self.recent: deque = deque(self.xs[cp:], maxlen=w.recent)
        self.R = 0.0
        self.rstart: Optional[int] = None

    def stop(self) -> int:
        return self.end_pos if self.end_pos is not None else len(self.idx)

    def size(self, floor: float) -> float:
        return max(floor, _median(self.recent)) if self.recent else floor


class _Watch:
    """CUSUM on a standardized deficit x (about 0 when healthy, positive when sick).

    Raises when S = max(0, S + x - k) reaches h. While raised, each running row adds m * (m/2 - x)
    to a closing statistic R, the log-likelihood that the row is healthy rather than sick (m is the
    problem's recent size). The problem ends where R starts the climb that reaches END_LLR.
    Each row also keeps its two raw readings (u, v) for the onset estimate made afterwards."""

    def __init__(self, k: float, h: float, recent: int):
        self.k, self.h, self.recent = k, h, recent
        self.records: list[_Record] = []
        self._segment([], [], [], [])

    def _segment(self, idx, us, vs, xs) -> None:
        self.idx, self.us, self.vs, self.xs = idx, us, vs, xs
        self.S = 0.0
        self.exc: Optional[int] = None
        self.open: Optional[_Record] = None

    def step(self, i: int, u: float, v: float, x: Optional[float]) -> None:
        """One running row. x is None until the healthy levels are learned: the row is kept for
        the onset estimates made afterwards, but cannot raise anything yet."""
        self.idx.append(i)
        self.us.append(u)
        self.vs.append(v)
        self.xs.append(0.0 if x is None else x)
        if x is None:
            return
        rec = self.open
        if rec is None:
            s = self.S + x - self.k
            if s <= 0.0:
                self.S, self.exc = 0.0, None
                return
            if self.exc is None:
                self.exc = len(self.idx) - 1
            self.S = s
            if s >= self.h:
                rec = _Record(self, self.exc, i)
                self.open = rec
                self.records.append(rec)
            return
        m = rec.size(self.k)
        r = rec.R + m * (0.5 * m - x)
        if r <= 0.0:
            rec.R, rec.rstart = 0.0, None
            rec.recent.append(x)
            return
        if rec.rstart is None:
            rec.rstart = len(self.idx) - 1
        rec.R = r
        if r >= END_LLR:
            pos = rec.rstart
            rec.end_pos, rec.how = pos, "normal"
            keep = (self.idx, self.us, self.vs, self.xs)
            # the healthy run that closed it starts the next segment
            self._segment(*(lst[pos:] for lst in keep))
            rec.idx, rec.us, rec.vs, rec.xs = (lst[:pos + 1] for lst in keep)

    def cut(self, i: int) -> None:
        """The pump failed (or came back from a failure) at row i. An open problem ends at the
        failure, and watching starts afresh."""
        rec = self.open
        if rec is not None:
            rec.end_row, rec.how = i, "failure"
            rec.idx, rec.us, rec.vs, rec.xs = list(rec.idx), list(rec.us), list(rec.vs), list(rec.xs)
        self._segment([], [], [], [])


# ---------- one facility ----------
class _Facility:
    def __init__(self, ds: Dataset, fac: Facility, recent: int = 12):
        self.fac = fac
        self.P = ds.systems[fac.system].disposal_pressure_psi

        def get(asset: str, metric: str):
            return ds.values(asset, metric) if ds.has(asset, metric) else None

        self.st = get(fac.pump_id, "status")
        self.fl = get(fac.pump_id, "flow_bpd")
        self.di = get(fac.pump_id, "discharge_psi")
        self.rc = get(fac.pipeline_id, "receive_flow_bpd")
        self.pp = get(fac.pipeline_id, "pressure_psi")
        self.lv = get(fac.tank_id, "level_pct")
        self.wells = [c for c in (get(w.id, "status") for w in fac.wells) if c is not None]
        self.maxf = fac.max_flow_bpd if fac.max_flow_bpd and fac.max_flow_bpd > 0 else None
        self.can_wear = bool(self.fl is not None and self.di is not None and self.maxf)
        self.can_leak = self.fl is not None and self.rc is not None and self.pp is not None
        self.fail_on: Optional[int] = None
        self.failures: list[tuple[int, Optional[int]]] = []
        self.leak = _Watch(LEAK_K, LEAK_H, recent)
        self.wear = _Watch(WEAR_K, WEAR_H, recent)

    def run(self, b0: int, b1: int, ref: dict, pool: dict[str, list[float]]) -> None:
        st = self.st
        if st is None:
            return
        fl, di, rc, pp, P, maxf = self.fl, self.di, self.rc, self.pp, self.P, self.maxf
        leak, wear = self.leak, self.wear
        wear_on = self.can_wear and all(c in ref for c in WEAR_CH)
        leak_on = self.can_leak and all(c in ref for c in LEAK_CH)
        if wear_on:
            (lf, sf), (ld, sd) = ref["f"], ref["d"]
        if leak_on:
            (lp, sp), (lr, sr) = ref["p"], ref["r"]
        for i in range(b0, b1):
            s = st[i]
            if not _ok(s):                   # missing reading
                continue
            if s > 1.5:                      # 2: fault
                if self.fail_on is None:
                    self.fail_on = i
                    wear.cut(i)
                continue
            if self.fail_on is not None:     # the fault cleared: repaired
                self.failures.append((self.fail_on, i))
                self.fail_on = None
                wear.cut(i)
            if s < 0.5 or fl is None:        # 0: stopped by its setpoints
                continue
            q = fl[i]
            if not (_ok(q) and q > 0):
                continue
            if self.can_wear:
                f = q / maxf
                d = di[i] - P
                if _ok(d):
                    wear.step(i, f, d, -((f - lf) / sf + (d - ld) / sd) * INV_SQRT2 if wear_on else None)
                    if wear.open is None:
                        pool["f"].append(f)
                        pool["d"].append(d)
            if self.can_leak:
                p = pp[i] - P
                r = rc[i] / q
                if _ok(p) and _ok(r):
                    leak.step(i, p, r, -((p - lp) / sp + (r - lr) / sr) * INV_SQRT2 if leak_on else None)
                    if leak.open is None:
                        pool["p"].append(p)
                        pool["r"].append(r)


# ---------- the scan ----------
class _Scan:
    """One pass over the visible rows: every facility's watches, hour block by hour block."""

    def __init__(self, ds: Dataset):
        self.ds = ds
        self.step = _grid_step(ds)
        self.per_hour = max(1, round(3600 / self.step))
        self.recent = max(2, round(RECENT_H * self.per_hour))
        self.cp_back = max(2, round(CP_BACK_H * self.per_hour))
        self.facs = [_Facility(ds, f, self.recent) for f in ds.facilities.values()]
        self.healthy = _Healthy()
        n = ds.end
        for b0 in range(0, n, self.per_hour):
            b1 = min(n, b0 + self.per_hour)
            ref = dict(self.healthy.ref)            # what was learned before this hour
            pool = {c: [] for c in _Healthy.CH}
            for fs in self.facs:
                fs.run(b0, b1, ref, pool)
            self.healthy.add(pool)

    def t(self, i: int) -> int:
        return int(self.ds.times[i])

    def snap(self, ts: float) -> int:
        """The visible row timestamp nearest to ts."""
        times, n = self.ds.times, self.ds.end
        k = bisect_left(times, ts, 0, n)
        if k >= n:
            return int(times[n - 1])
        if k > 0 and ts - times[k - 1] <= times[k] - ts:
            return int(times[k - 1])
        return int(times[k])

    def hours(self, rows: list[int], t0: int) -> list[float]:
        return [(int(self.ds.times[i]) - t0) / 3600.0 for i in rows]


# ---------- shared pieces of an event ----------
def _end_of(sc: _Scan, rec: _Record) -> tuple[Optional[int], Optional[tuple[int, int]]]:
    """End of a closed problem: the failure row, the first healthy row, or the middle of the
    pump-off gap before it when the repair happened unseen (then also returns the gap as
    (first row with the pump off, first healthy running row))."""
    if rec.how == "failure":
        return sc.t(rec.end_row), None
    if rec.end_pos is None:
        return None, None
    pos = rec.end_pos
    first = sc.t(rec.idx[pos])
    prev = sc.t(rec.idx[pos - 1]) if pos > 0 else None
    if prev is None or first - prev <= sc.step:
        return first, None
    return sc.snap(0.5 * (prev + sc.step + first)), (prev + sc.step, first)


def _confidence(rec: _Record) -> float:
    """How sure: the recent average deficit in noise units, times the square root of how many
    readings it rests on, mapped to 0.5..0.99. A slow problem that is just starting scores lower."""
    xs = list(rec.recent)
    if not xs:
        return 0.5
    z = (sum(xs) / len(xs)) * math.sqrt(len(xs))
    return round(min(0.99, max(0.5, 0.5 + 0.49 * math.tanh(max(0.0, z) / 8.0))), 2)


def _recent_means(sc: _Scan, fs: _Facility, rows: list[int]) -> dict[str, float]:
    tail = rows[-sc.recent:]
    out = {}
    for name, col in (("flow", fs.fl), ("dis", fs.di), ("rcv", fs.rc), ("pp", fs.pp)):
        if col is not None:
            vals = [col[i] for i in tail if _ok(col[i])]
            if vals:
                out[name] = sum(vals) / len(vals)
    return out


# ---------- leaks ----------
def _leak_onset(sc: _Scan, rec: _Record, ref) -> tuple[int, Optional[tuple[float, float]]]:
    """Position of the first leaking row: the maximum-likelihood step in pipeline pressure.

    Each candidate row is scored by whether its pressure deficit sits nearer the healthy level (0)
    or the leak's level (its fitted line, and never less than the opening step: the step does not
    shrink going back in time); the onset is where the running sum of those scores starts the
    climb that never comes back. Returns (position, line of the pressure deficit
    a + b*(hours after the onset row)) for the hidden-onset estimate."""
    lp, sp = ref["p"]
    stop = rec.stop()
    lo = max(0, rec.cp - sc.cp_back)
    dp = [-(rec.us[k] - lp) / sp for k in range(lo, stop)]
    pos = rec.cp
    t_cp = sc.t(rec.idx[pos])
    rows = list(range(rec.cp, stop))
    if len(rows) >= 4:
        fit = _fit(sc.hours([rec.idx[k] for k in rows], t_cp), [dp[k - lo] for k in rows])
        if fit:
            a, b, _ = fit
            hi = min(stop, rec.cp + sc.cp_back)
            best, acc = None, 0.0
            for k in range(hi - 1, lo - 1, -1):      # suffix sums of the per-row scores
                mu = max(0.0, a + b * max(0.0, sc.t(rec.idx[k]) - t_cp) / 3600.0)
                z = dp[k - lo]
                acc += 0.5 * (z * z - (z - mu) ** 2)
                if best is None or acc > best[0]:
                    best = (acc, k)
            if best is not None:
                pos = best[1]
    t0 = sc.t(rec.idx[pos])
    ks = list(range(pos, stop))
    line = None
    if len(ks) >= 6:
        fit = _fit(sc.hours([rec.idx[k] for k in ks], t0), [dp[k - lo] for k in ks])
        if fit:
            line = (fit[0], fit[1])
    return pos, line


def _leak_events(sc: _Scan) -> list[Event]:
    ref = sc.healthy.ref
    found = []
    for fs in sc.facs:
        for n_rec, rec in enumerate(fs.leak.records):
            pos, line = _leak_onset(sc, rec, ref)
            t_on = sc.t(rec.idx[pos])
            if pos > 0:                          # the last running row before the first leaking one
                prev = sc.t(rec.idx[pos - 1])
            elif n_rec == 0 and rec.idx[0] > 0:  # the pump had not run since the first row
                prev = sc.ds.start - sc.step
            else:
                prev = None
            hidden = prev is not None and t_on - prev > sc.step
            found.append((fs, rec, pos, t_on, prev, hidden, line))
    # the opening step, from leaks whose first reading came with the pump already running
    steps = [line[0] for (*_, hidden, line) in found if not hidden and line and line[1] > 0]
    step0 = _median(steps) if steps else None
    out = []
    for fs, rec, pos, t_on, prev, hidden, line in found:
        onset, note = t_on, "the first reading with the pressure stepped down"
        if hidden:
            off = f"the pump did not run between {_hm(prev + sc.step)} and {_hm(t_on)}"
            note = f"{off}, so the first running reading"
            if step0 is not None and line and line[1] > 0 and line[0] > step0:
                tau = t_on - (line[0] - step0) / line[1] * 3600.0
                onset = sc.snap(max(prev + sc.step, min(t_on, tau)))
                note = f"{off}; estimated from how far the leak had grown past its opening step"
        out.append(_leak_event(sc, fs, rec, pos, onset, note, line))
    return out


def _leak_event(sc: _Scan, fs: _Facility, rec: _Record, pos: int, onset: int, note: str, line) -> Event:
    fac, P = fs.fac, fs.P
    lp, sp = sc.healthy.ref["p"]
    end, gap = _end_of(sc, rec)
    rows = rec.idx[pos:rec.stop()]
    m = _recent_means(sc, fs, rows)
    healthy = P + lp
    ev = []
    if "pp" in m:
        ev.append(f"While {fac.pump_id} ran, {fac.pipeline_id} read {_n(m['pp'])} psi at the disposal end, "
                  f"{_n(healthy - m['pp'])} psi below its healthy running {_n(healthy)} psi "
                  f"(disposal {_n(P)} psi + {_n(lp)}; noise about {_n(sp, 1)} psi).")
    if "flow" in m and "rcv" in m and m["flow"] > 0:
        ev.append(f"Only {m['rcv'] / m['flow'] * 100:.1f}% of the pumped water reached the disposal site "
                  f"({_n(m['rcv'])} of {_n(m['flow'])} bpd), about {_n(m['flow'] - m['rcv'])} bpd escaping.")
    if "flow" in m and "dis" in m:
        ev.append(f"Pump flow ({m['flow'] / fac.max_flow_bpd * 100:.1f}% of its {_n(fac.max_flow_bpd)} bpd rating) "
                  f"and discharge ({_n(m['dis'])} psi) look normal, so the water is lost in the line.")
    if line and line[1] > 0 and len(rows) > 1:
        ev.append(f"The leak kept growing: the pressure deficit rose about {line[1] * sp:.1f} psi an hour "
                  f"while the pump ran.")
    ev.append(f"Started {_hm(onset)} ({note}); first flagged {_hm(sc.t(rec.alarm))}.")
    if end is not None:
        ev.append(f"Back to normal at {_hm(end)}." if gap is None else
                  f"Repaired while the pump was off between {_hm(gap[0])} and {_hm(gap[1])}; "
                  f"the end is taken as the middle, {_hm(end)}.")
    return Event(id="", category="pipeline_leak", system=fac.system, facility=fac.id, asset=fac.pipeline_id,
                 onset=onset, end=end, status="ongoing" if end is None else "resolved",
                 confidence=_confidence(rec), evidence=ev, detected_at=sc.t(rec.alarm))


# ---------- worn pumps ----------
def _wear_events(sc: _Scan) -> list[Event]:
    ref = sc.healthy.ref
    (lf, sf), (ld, sd) = ref["f"], ref["d"]
    out = []
    for fs in sc.facs:
        for rec in fs.wear.records:
            stop = rec.stop()
            t0 = sc.t(rec.idx[0])
            ts = sc.hours(rec.idx[:stop], t0)
            zf = [-(u - lf) / sf for u in rec.us[:stop]]
            zd = [-(v - ld) / sd for v in rec.vs[:stop]]
            fit = _hinge(ts, [zf, zd], 0.0, (sc.t(rec.alarm) - t0) / 3600.0)
            if fit is not None:
                onset = sc.snap(t0 + fit[0] * 3600.0)
                rates = (fit[1][0] * sf * 100.0, fit[1][1] * sd)     # % of rating an hour, psi an hour
            else:
                onset, rates = sc.t(rec.idx[rec.cp]), None
            out.append(_wear_event(sc, fs, rec, onset, rates))
    return out


def _wear_event(sc: _Scan, fs: _Facility, rec: _Record, onset: int, rates) -> Event:
    fac, P = fs.fac, fs.P
    ld = sc.healthy.ref["d"][0]
    end, gap = _end_of(sc, rec)
    rows = rec.idx[rec.cp:rec.stop()]
    m = _recent_means(sc, fs, rows)
    ev = []
    if "flow" in m:
        ev.append(f"While running, {fac.pump_id} moved {_n(m['flow'])} bpd, {m['flow'] / fac.max_flow_bpd * 100:.1f}% "
                  f"of its {_n(fac.max_flow_bpd)} bpd rating"
                  + (f", losing about {rates[0]:.2f} points an hour." if rates else "."))
    if "dis" in m:
        healthy = P + ld
        ev.append(f"Discharge pressure {_n(m['dis'])} psi, {_n(healthy - m['dis'])} psi below its healthy running "
                  f"{_n(healthy)} psi (disposal {_n(P)} psi + {_n(ld)})"
                  + (f", falling about {rates[1]:.1f} psi an hour." if rates else "."))
    if "rcv" in m and "flow" in m:
        ev.append(f"The pipeline still receives what the pump sends ({_n(m['rcv'])} of {_n(m['flow'])} bpd), "
                  f"so no water is lost in the line.")
    ev.append(f"Wearing since about {_hm(onset)} (its trend run back to where it leaves normal); "
              f"first flagged {_hm(sc.t(rec.alarm))}.")
    if end is not None:
        if rec.how == "failure":
            ev.append(f"Ended {_hm(end)} when the pump failed outright (a separate event).")
        elif gap is None:
            ev.append(f"Flow and discharge back to normal at {_hm(end)}.")
        else:
            ev.append(f"Repaired while the pump was off between {_hm(gap[0])} and {_hm(gap[1])}; "
                      f"the end is taken as the middle, {_hm(end)}.")
    return Event(id="", category="pump_degradation", system=fac.system, facility=fac.id, asset=fac.pump_id,
                 onset=onset, end=end, status="ongoing" if end is None else "resolved",
                 confidence=_confidence(rec), evidence=ev, detected_at=sc.t(rec.alarm))


# ---------- pump failures ----------
def _shut_in(wells, i: int) -> bool:
    return bool(wells) and all(_ok(c[i]) and c[i] < 0.5 for c in wells)


def _trip_row(fs: _Facility, shutdown: float, i0: int, i1: int) -> Optional[int]:
    """First row in [i0, i1) where the tank is at shutdown or its wells were shut in."""
    lv = fs.lv
    for i in range(i0, i1):
        if (lv is not None and _ok(lv[i]) and lv[i] >= shutdown) or _shut_in(fs.wells, i):
            return i
    return None


def _failure_event(sc: _Scan, fs: _Facility, on: int, off: Optional[int]) -> Event:
    ds, fac, lv, P = sc.ds, fs.fac, fs.lv, fs.P
    end = sc.t(off) if off is not None else None
    at = off if off is not None else ds.end - 1          # the restart row, or "now"
    first = f"{fac.pump_id} reports a fault (status 2) from {_hm(sc.t(on))}"
    if fs.fl is not None and _ok(fs.fl[on]):
        first += f": flow {_n(fs.fl[on])} bpd"
    if fs.di is not None and _ok(fs.di[on]):
        first += f", discharge {_n(fs.di[on])} psi (the disposal pressure is {_n(P)} psi)"
    ev = [first + "."]
    lv0 = lv[on] if (lv is not None and _ok(lv[on])) else None
    lv1 = lv[at] if (lv is not None and _ok(lv[at])) else None
    if lv0 is not None and lv1 is not None:
        ev.append(f"{fac.tank_id} went from {lv0:.1f}% at onset to {lv1:.1f}% "
                  f"{'when the pump restarted' if off is not None else 'in the latest reading'} "
                  f"({(sc.t(at) - sc.t(on)) / 3600.0:.1f} h later); shutdown is {ds.shutdown_pct:g}%.")
    e = Event(id="", category="pump_failure", system=fac.system, facility=fac.id, asset=fac.pump_id,
              onset=sc.t(on), end=end, status="ongoing" if end is None else "resolved",
              confidence=0.99, evidence=ev, detected_at=sc.t(on),
              level_pct=lv1 if end is not None else None,
              repair_end=sc.t(on) + int(round(ds.repair_hours * 3600)),
              tripped=_trip_row(fs, ds.shutdown_pct, on, off if off is not None else ds.end) is not None,
              lost_revenue_per_hour=ds.facility_revenue_per_hour(fac.id))
    countdown(ds, e)                                     # the countdown block, from plan.countdown
    if e.tripped:
        when = "" if e.trips_at is None else f" at about {_hm(e.trips_at)}"
        ev.append(f"The tank reached shutdown{when} and its wells shut in.")
    if end is not None:
        ev.append(f"Pump running again at {_hm(end)}, {(end - sc.t(on)) / 3600.0:.1f} h after the fault.")
    return e


# ---------- public ----------
def detect(ds: Dataset) -> list[Event]:
    """Every problem with onset <= ds.now in the view's readings, in onset order (EV-0001, ...).

    Reads only rows with index < ds.end. Pump failures carry their countdown block (countdown())."""
    if ds.end < 1:
        return []
    sc = _Scan(ds)
    events: list[Event] = []
    for fs in sc.facs:
        spans = list(fs.failures) + ([(fs.fail_on, None)] if fs.fail_on is not None else [])
        events += [_failure_event(sc, fs, on, off) for on, off in spans]
    if sc.healthy.has(LEAK_CH):
        events += _leak_events(sc)
    if sc.healthy.has(WEAR_CH):
        events += _wear_events(sc)
    events = [e for e in events if e.onset <= ds.now]
    events.sort(key=lambda e: (e.onset, e.detected_at if e.detected_at is not None else e.onset, e.asset, e.category))
    for n, e in enumerate(events, start=1):
        e.id = f"EV-{n:04d}"
        if e.category == "pump_failure" and e.status == "ongoing":
            e.evidence.append(_countdown_words(e))
    return events


def _dur(hours: float) -> str:
    m = int(round(hours * 60))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _countdown_words(e: Event) -> str:
    h = e.hours_to_limit
    if e.tripped:
        return f"The tank is at shutdown and its wells are shut in until the repair, due {_hm(e.repair_end)}."
    if e.level_pct is None:
        return "No tank level readings for this failure, so there is no countdown; treat the tank as filling."
    if not h:
        return "The tank is not filling (no producing wells), so there is no countdown."
    when = (f"at about {_hm(e.trips_at)}, before the repair due {_hm(e.repair_end)}" if e.trips_at is not None
            else f"but the repair is due first, at {_hm(e.repair_end)}")
    band = _dur(h["p50"])
    if _dur(h["p10"]) != _dur(h["p90"]):
        band += f" ({_dur(h['p10'])} to {_dur(h['p90'])})"
    return (f"Level now about {e.level_pct:.1f}% (the readings since the onset, fitted at the producing wells' "
            f"inflow): shutdown in about {band}, {when}.")


def countdown(ds: Dataset, event: Event) -> Event:
    """Fill a pump failure's countdown block from plan.countdown, the one countdown in the app (R1:
    a line at the producing wells' inflow fitted to the readings since the onset; levels in 0.1 %
    steps; trip times to the minute; see plan.py).

    Ongoing, at the view's now: level_at_onset_pct, level_pct, hours_to_limit {"p50", "p10", "p90"}
    (all 0 once tripped), trips_at (when the tank reached or will reach shutdown if nobody acts, None
    if the repair comes first), tripped, repair_end and lost_revenue_per_hour. Resolved, at its end:
    the same, except that hours_to_limit stays None and level_pct is left as it is (detect sets the
    reading at the restart). With no level readings at all the block is left as it is. Any other
    event comes back unchanged. Returns the same event."""
    if event.category != "pump_failure":
        return event
    ongoing = event.status == "ongoing" or event.end is None
    at = ds.now if ongoing else min(int(event.end), ds.now)
    try:
        cd = plan.countdown(ds, event, at_ts=at)
    except DatasetError:                                 # this tank has no level readings at all
        if event.repair_end is None:
            event.repair_end = int(event.onset) + int(round(ds.repair_hours * 3600))
        return event
    event.level_at_onset_pct = cd["level_at_onset_pct"]
    event.trips_at = cd["trips_at"]
    event.tripped = cd["tripped"]
    event.repair_end = cd["repair_end"]
    event.lost_revenue_per_hour = cd["lost_revenue_per_hour"]
    if ongoing:
        event.level_pct = cd["level_pct"]
        event.hours_to_limit = cd["hours_to_limit"]
    else:
        event.hours_to_limit = None
    return event
