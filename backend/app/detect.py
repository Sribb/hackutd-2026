"""Detection: flags deficiencies from the reading time series alone.

- PUMP_TRIP (ALARM): the pump reports `running` false, or its flow meter reads under
  10% of its rate while it should be running, or its tank rises faster than its wells
  explain with the pump running. Confirmed over CONFIRM_READINGS evaluations, so one
  noisy sample never alerts, and a trip alerts within 3 readings.
- SENSOR_STALE (WARNING): no reading of a signal for `sensors.stale_after_min`. A
  stale tank is UNKNOWN, never OK.
- VALVE_MISMATCH (WARNING): a valve's reported position differs from the position a
  person confirmed, for CONFIRM_READINGS evaluations after a short grace period.

Alerts: {id, asset_id, kind, severity, message, t, clock, active}. `t` is ISO wall
time. One active alert per (asset, kind); it resolves (active false) when the
condition clears.
"""
from __future__ import annotations

import itertools
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from .fieldmap import FieldMap, name

CONFIRM_READINGS = 2
MISMATCH_GRACE_MIN = 1.0
RATE_WINDOW_MIN = 10.0

Key = tuple  # (asset_id, signal)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slope_bbl_hr(points: list[tuple[float, float]], capacity_bbl: float) -> Optional[float]:
    """Least-squares slope of (t_min, level_pct) points, as bbl/hr."""
    if len(points) < 4 or points[-1][0] - points[0][0] < 2.0:
        return None
    n = len(points)
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    den = sum((t - mt) ** 2 for t, _ in points)
    if den == 0:
        return None
    pct_per_min = sum((t - mt) * (v - mv) for t, v in points) / den
    return pct_per_min / 100.0 * capacity_bbl * 60.0


class Detector:
    def __init__(self, fmap: FieldMap, stale_after_min: float = 5.0) -> None:
        self.f = fmap
        self.stale_after = float(stale_after_min)
        self._ids = itertools.count(1)
        self.alerts: list[dict] = []                 # every alert, oldest first
        self.active: dict[tuple, dict] = {}          # (asset_id, kind) -> alert
        self.bad: dict[tuple, int] = {}              # (asset_id, kind) -> consecutive bad evaluations
        self.good: dict[tuple, int] = {}
        self.stale_keys: set[Key] = set()
        self.rates: dict[str, dict] = {}             # tank id -> observed / expected bbl/hr
        self.flow_state: dict[str, tuple] = {}       # tank id -> its pumps' and valves' last known state
        self.flow_changed_t: dict[str, float] = {}   # tank id -> live time that state last changed
        self.pump_cap: dict[str, float] = {}         # pump id -> bbl/hr it can move (shared disposal well)
        self.expected_keys: set[Key] = {(tid, "level_pct") for tid in fmap.tanks}

    # ---------- queries ----------
    def is_stale(self, asset_id: str, signal: Optional[str] = None) -> bool:
        return any(a == asset_id and (signal is None or s == signal) for a, s in self.stale_keys)

    def active_for(self, asset_id: str) -> list[dict]:
        return [a for (aid, _), a in self.active.items() if aid == asset_id]

    # ---------- one evaluation ----------
    def evaluate(self, t: float, latest: dict, history: dict, commanded: dict, clock: str = "") -> list[dict]:
        """Run every check at live time t. Returns alerts raised or resolved."""
        changed: list[dict] = []
        self.expected_keys |= set(latest)

        def fresh(key: Key) -> bool:
            r = latest.get(key)
            return r is not None and t - r["t"] <= self.stale_after

        # SENSOR_STALE
        stale_now = {k for k in self.expected_keys if not fresh(k)}
        self.stale_keys = stale_now
        for aid in {a for a, _ in self.expected_keys}:
            sigs = sorted(s for a, s in stale_now if a == aid)
            if sigs:
                ages = [t - latest[(aid, s)]["t"] for s in sigs if (aid, s) in latest]
                age = f"{max(ages):.0f} min" if ages else "since startup"
                msg = (f"No {', '.join(sigs)} reading from {name(aid)} for {age}. "
                       f"Treat it as unknown, not safe.")
                changed += self._set(aid, "SENSOR_STALE", "WARNING", msg, True, t, clock, confirm=1)
            else:
                changed += self._set(aid, "SENSOR_STALE", "WARNING", "", False, t, clock, confirm=1)

        # Observed fill rate for every tank, every tick, so it never goes stale after a trip.
        # Only readings since the tank's pumps or valves last changed state count, so right
        # after a trip the rate doesn't still average in the draining before it.
        # expected_bbl_hr is what the wells and the pumps' run states explain (the pump check
        # below refines it when a pump should be running).
        self._track_flow_state(t, latest, fresh)
        self._share_disposal(latest, fresh)
        for tid, tank in self.f.tanks.items():
            obs = None
            if fresh((tid, "level_pct")):
                obs = slope_bbl_hr(self._rate_points(tid, t, history), float(tank["capacity_bbl"]))
            self.rates[tid] = {"observed_bbl_hr": None if obs is None else round(obs, 1),
                               "expected_bbl_hr": self._expected_bbl_hr(tid, latest, fresh)}

        # PUMP_TRIP
        for pid, pump in self.f.pumps.items():
            if not (fresh((pid, "flow_bbl_hr")) or fresh((pid, "running"))):
                continue  # no evidence either way; SENSOR_STALE covers it
            bad, msg = self._pump_check(t, pid, pump, latest, history, commanded, fresh)
            changed += self._set(pid, "PUMP_TRIP", "ALARM", msg, bad, t, clock)

        # VALVE_MISMATCH
        for vid in self.f.valves:
            cmd = commanded.get(vid)
            key = (vid, "position")
            if not cmd or not fresh(key) or t - cmd["t"] < MISMATCH_GRACE_MIN:
                continue
            reported = str(latest[key]["value"]).upper()
            bad = reported != str(cmd["value"]).upper()
            msg = (f"{name(vid)} reports {reported} but was commanded {str(cmd['value']).upper()}. "
                   f"Check it in the field.")
            changed += self._set(vid, "VALVE_MISMATCH", "WARNING", msg, bad, t, clock)
        return changed

    def _rate_points(self, tid: str, t: float, history: dict) -> list[tuple[float, float]]:
        """Level readings in the rate window, starting no earlier than the last state change."""
        since = max(t - RATE_WINDOW_MIN, self.flow_changed_t.get(tid, float("-inf")))
        return [(tt, v) for tt, v in history.get((tid, "level_pct"), ()) if tt >= since - 1e-9]

    def _track_flow_state(self, t: float, latest: dict, fresh) -> None:
        """Note when anything that moves water out of a tank changes state: one of its pumps
        starting or stopping (run signal, else flow over 10% of its rate), or its discharge
        valve or a crossover on it opening or closing."""
        f = self.f
        for tid in f.tanks:
            pumps = sorted(f.pumps_by_tank.get(tid, []))
            valves = sorted({f.pump_valve[p] for p in pumps if p in f.pump_valve} |
                            {xo["id"] for xo in f.crossovers if tid in (xo.get("connects") or [])})
            sig = (tuple((p, self._pump_on(p, latest, fresh)) for p in pumps) +
                   tuple((v, self._position(v, latest, fresh)) for v in valves))
            prev = self.flow_state.get(tid)
            if prev is not None and sig != prev:
                self.flow_changed_t[tid] = t
            self.flow_state[tid] = sig

    def _share_disposal(self, latest: dict, fresh) -> None:
        """What each pump can move when the pumps on one disposal well ask for more than it
        takes: they share its max_bbl_hr, as the field does. Without this, a busy disposal
        well looks like pumps that aren't moving water."""
        f = self.f
        want: dict[str, float] = {}
        for pid, p in f.pumps.items():
            tid = p["tank_id"]
            r = latest.get((tid, "level_pct"))
            low = r is not None and fresh((tid, "level_pct")) and float(r["value"]) <= f.low_pct(tid) + 0.5
            want[pid] = 0.0 if self._pump_on(pid, latest, fresh) is False or low else float(p["rate_bbl_hr"])
        self.pump_cap = {pid: float(p["rate_bbl_hr"]) for pid, p in f.pumps.items()}
        for sid, s in f.swds.items():
            feeders = [pid for pid, s_id in f.pump_swd.items() if s_id == sid and pid in want]
            total, mx = sum(want[p] for p in feeders), float(s.get("max_bbl_hr") or 0)
            if total > mx > 0:
                for pid in feeders:
                    self.pump_cap[pid] = float(f.pumps[pid]["rate_bbl_hr"]) * mx / total

    def _pump_on(self, pid: str, latest: dict, fresh) -> Optional[bool]:
        """Run state from the run signal, else from flow over 10% of its rate; None if unknown."""
        if fresh((pid, "running")):
            return latest[(pid, "running")]["value"] not in (False, 0, "false", "OFF", "STOPPED")
        if fresh((pid, "flow_bbl_hr")):
            return float(latest[(pid, "flow_bbl_hr")]["value"]) >= 0.1 * float(self.f.pumps[pid]["rate_bbl_hr"])
        return None

    @staticmethod
    def _position(vid: str, latest: dict, fresh) -> Optional[str]:
        return str(latest[(vid, "position")]["value"]).upper() if fresh((vid, "position")) else None

    def _expected_bbl_hr(self, tid: str, latest: dict, fresh) -> Optional[float]:
        """Wells in minus the pumps that are running, or None when it can't be explained
        that simply (no fresh level, an unknown pump, a crossover not known to be closed)."""
        f = self.f
        r = latest.get((tid, "level_pct"))
        if not r or not fresh((tid, "level_pct")):
            return None
        if any(tid in (xo.get("connects") or []) and self._position(xo["id"], latest, fresh) != "CLOSED"
               for xo in f.crossovers):
            return None
        out = 0.0
        for pid in f.pumps_by_tank.get(tid, []):
            on = self._pump_on(pid, latest, fresh)
            if on is None:
                return None
            vid = f.pump_valve.get(pid)
            if on and not (vid and self._position(vid, latest, fresh) == "CLOSED"):
                out += self.pump_cap.get(pid, float(f.pumps[pid]["rate_bbl_hr"]))
        inflow = 0.0
        for wid in f.wells_by_tank.get(tid, []):
            w = latest.get((wid, "flow_bbl_hr"))
            inflow += float(w["value"]) if w and fresh((wid, "flow_bbl_hr")) else float(f.wells[wid]["water_bbl_hr"])
        if out and float(r["value"]) <= f.low_pct(tid) + 0.5:
            return 0.0  # the pump is holding the tank at its low level
        return round(inflow - out, 1)

    def _pump_check(self, t, pid, pump, latest, history, commanded, fresh) -> tuple[Optional[bool], str]:
        f, tid = self.f, pump["tank_id"]
        rate = float(pump["rate_bbl_hr"])
        level_r = latest.get((tid, "level_pct"))
        level = float(level_r["value"]) if level_r and fresh((tid, "level_pct")) else None
        vid = f.pump_valve.get(pid)
        valve_open = not vid or not fresh((vid, "position")) or str(latest[(vid, "position")]["value"]).upper() == "OPEN"
        commanded_on = str(commanded.get(pid, {}).get("value", "ON")).upper() not in ("OFF", "FALSE")
        should_run = commanded_on and valve_open and (level is None or level > f.low_pct(tid) + 0.5)

        if fresh((pid, "running")) and latest[(pid, "running")]["value"] in (False, 0, "false", "OFF", "STOPPED") \
                and commanded_on:
            return True, f"{name(pid)} tripped: it reports stopped. {name(tid)} is no longer being emptied."
        if should_run and fresh((pid, "flow_bbl_hr")):
            flow = float(latest[(pid, "flow_bbl_hr")]["value"])
            if flow < 0.1 * rate:
                return True, (f"{name(pid)} tripped: flow reads {flow:.0f} bbl/hr, "
                              f"it should be moving {rate:.0f}. {name(tid)} is filling.")

        # Tank rising faster than its wells explain with the pump running.
        if level is not None and should_run and not any(
                tid in (xo.get("connects") or []) and str(latest.get((xo["id"], "position"), {}).get("value", "")).upper() == "OPEN"
                for xo in f.crossovers):
            obs = slope_bbl_hr(self._rate_points(tid, t, history), float(f.tanks[tid]["capacity_bbl"]))
            inflow = 0.0
            for wid in f.wells_by_tank.get(tid, []):
                r = latest.get((wid, "flow_bbl_hr"))
                inflow += float(r["value"]) if r and fresh((wid, "flow_bbl_hr")) else float(f.wells[wid]["water_bbl_hr"])
            out = sum(self.pump_cap.get(p, float(f.pumps[p]["rate_bbl_hr"])) for p in f.pumps_by_tank.get(tid, []))
            expected = inflow - out if level > f.low_pct(tid) + 0.5 else 0.0
            self.rates[tid] = {"observed_bbl_hr": None if obs is None else round(obs, 1),
                               "expected_bbl_hr": round(expected, 1)}
            if obs is not None and obs - expected > max(30.0, 0.25 * rate):
                return True, (f"{name(tid)} is rising {obs:+.0f} bbl/hr; with {name(pid)} running it should be "
                              f"{expected:+.0f}. {name(pid)} is likely not moving water.")
        return False, ""

    def _set(self, aid: str, kind: str, severity: str, msg: str, bad: Optional[bool], t: float,
             clock: str, confirm: int = CONFIRM_READINGS) -> list[dict]:
        key = (aid, kind)
        if bad is None:
            return []
        if bad:
            self.good[key] = 0
            self.bad[key] = self.bad.get(key, 0) + 1
            if key not in self.active and self.bad[key] >= confirm:
                alert = {"id": f"alert_{next(self._ids)}", "asset_id": aid, "kind": kind, "severity": severity,
                         "message": msg, "t": now_iso(), "clock": clock, "active": True}
                self.active[key] = alert
                self.alerts.append(alert)
                return [alert]
            if key in self.active:
                self.active[key]["message"] = msg  # keep the evidence current, no new alert
            return []
        self.bad[key] = 0
        self.good[key] = self.good.get(key, 0) + 1
        if key in self.active and self.good[key] >= confirm:
            alert = self.active.pop(key)
            alert.update(active=False, resolved_t=now_iso())
            return [alert]
        return []


def new_history() -> deque:
    return deque(maxlen=240)
