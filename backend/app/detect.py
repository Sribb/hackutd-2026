"""Detection: flags deficiencies from the reading time series alone.

- PUMP_TRIP (ALARM): the pump reports `running` false, or its flow meter reads under
  10% of its rate while it should be running, or its tank rises faster than its wells
  explain with the pump running. Confirmed over CONFIRM_READINGS evaluations, so one
  noisy sample never alerts, and a trip alerts within 3 readings.
- SENSOR_STALE (WARNING): no reading of a signal for `sensors.stale_after_min`. A
  stale tank is UNKNOWN, never OK.
- VALVE_MISMATCH (WARNING): a valve's reported position differs from the position a
  person confirmed, for CONFIRM_READINGS evaluations after a short grace period.
- TANK_HIGH (WARNING): a fresh tank level at or over its warning_pct.
- TANK_HIGH_HIGH (ALARM): a fresh tank level at or over high_high_pct (shutdown_pct - 5).
  Both clear LEVEL_DEADBAND below the threshold, so a level riding the line can't chatter.

Alerts: {id, asset_id, kind, severity, message, t, clock, active, root_id, priority, ...}.
`t` is ISO wall time. One active alert per (asset, kind); it resolves (active false) when
the condition clears.

CF-7: `root_id` points a consequential alarm at its cause (a tank's high level at the
pump trip on that tank, see root_for). `priority` and the consequence fields come from
the baseline forecast (consequence_of, called by live.py); the shelve fields are set by
live.py's shelve().
"""
from __future__ import annotations

import itertools
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from .fieldmap import FieldMap, name
from .forecast import clock_at

CONFIRM_READINGS = 2
MISMATCH_GRACE_MIN = 1.0
RATE_WINDOW_MIN = 10.0
LEVEL_DEADBAND = 1.0      # a high-level alert clears this many % below its threshold
P1_MIN, P2_MIN = 60, 180  # CF-7: a shutdown within this many minutes is P1, then P2, else P3
TANK_KINDS = ("TANK_HIGH", "TANK_HIGH_HIGH")

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


def priority_for(minutes: Optional[int]) -> str:
    """P1 if the consequence is within P1_MIN minutes, P2 within P2_MIN, else P3."""
    if minutes is None:
        return "P3"
    return "P1" if minutes <= P1_MIN else "P2" if minutes <= P2_MIN else "P3"


def tanks_hit_by(fmap: FieldMap, alert: dict) -> list[str]:
    """The tanks whose shutdown this alarm leads to if it is left alone.

    A pump trip, a high level, or a valve that isn't where a person put it all end in a
    tank shutting in. A stale sensor is a loss of sight, not a cause (consequence_of
    ranks a stale tank level P2, never safe, and every other stale signal P3)."""
    aid, kind = alert["asset_id"], alert["kind"]
    if kind == "PUMP_TRIP" and aid in fmap.pumps:
        return [fmap.pumps[aid]["tank_id"]]
    if kind in TANK_KINDS and aid in fmap.tanks:
        return [aid]
    if kind == "VALVE_MISMATCH" and aid in fmap.valves:
        ends = [e for e in (fmap.valves[aid].get("connects") or [])[:2] if e in fmap.tanks]
        if ends:
            return ends  # a crossover: either of its tanks
        return [fmap.pumps[p]["tank_id"] for p, v in fmap.pump_valve.items() if v == aid and p in fmap.pumps]
    return []


def consequence_of(fmap: FieldMap, alert: dict, shutdowns: dict, horizon_min: float,
                   restart_usd: float, now_t: float = 0.0, start_clock: str = "00:00",
                   unknown_tanks: Optional[set] = None) -> dict:
    """Priority and consequence fields for one alert, from the baseline forecast's
    SHUTDOWN_LEVEL events (`shutdowns`: tank id -> event, its t_min counted from now,
    which is live minute `now_t` and clock `start_clock`, the forecast's start).

    usd_at_risk is what the forecast charges for that shutdown: the shut-in wells' lost
    production to the end of the horizon, plus a restart for each well.

    Missing data never lowers a priority: a cause alarm (a trip, a high level, a valve
    mismatch) on a tank in `unknown_tanks` (no fresh level, so the forecast has no
    shutdown for it) is P1, "Tank 3 level unknown; can't rule out shutdown.", and keeps
    the dollars it last had at risk."""
    blank = {"minutes_to_consequence": None, "minutes_to_consequence_low": None, "consequence": None,
             "consequence_asset_id": None, "consequence_clock": None, "consequence_t": None, "usd_at_risk": 0}
    if alert["kind"] == "SENSOR_STALE":
        if alert["asset_id"] in fmap.tanks:
            return {**blank, "priority": "P2",
                    "consequence": f"{name(alert['asset_id'])} has no fresh level, so when it shuts in "
                                   f"is unknown. Treat it as at risk."}
        return {**blank, "priority": "P3"}
    hit = tanks_hit_by(fmap, alert)
    blind = [tid for tid in hit if tid in (unknown_tanks or ()) and tid not in shutdowns]
    out = _forecast_consequence(fmap, [shutdowns[tid] for tid in hit if tid in shutdowns],
                                horizon_min, restart_usd, now_t, start_clock)
    if blind and (out is None or out["priority"] != "P1"):
        return {**blank, "priority": "P1", "consequence_asset_id": blind[0],
                "consequence": f"{name(blind[0])} level unknown; can't rule out shutdown.",
                "usd_at_risk": max(int(alert.get("usd_at_risk") or 0), out["usd_at_risk"] if out else 0)}
    return out or {**blank, "priority": "P3"}


def _forecast_consequence(fmap: FieldMap, events: list, horizon_min: float, restart_usd: float,
                          now_t: float, start_clock: str) -> Optional[dict]:
    """The soonest of `events` as consequence fields, or None when there is none."""
    if not events:
        return None
    ev = min(events, key=lambda e: e["t_min"])
    minutes = int(ev["t_min"] + 0.5)   # whole minutes, as the console shows them
    # Ranked on the early end of the forecast's give-or-take (inflow 15% high), so a tank
    # forecast at 61 minutes, give or take 9, is P1: it can shut in within the hour.
    low = ev.get("t_min_low")
    low = minutes if low is None else min(int(low + 0.5), minutes)
    lost = [w for w in ev.get("wells_lost", []) if w in fmap.wells]
    lost_hr = sum(float(fmap.wells[w].get("lost_usd_hr", 0)) for w in lost)
    usd = lost_hr * max(horizon_min - ev["t_min"], 0.0) / 60.0 + restart_usd * len(lost)
    return {"priority": priority_for(low), "minutes_to_consequence": minutes,
            "minutes_to_consequence_low": low,
            "consequence": ev.get("text"), "consequence_asset_id": ev["asset_id"],
            "consequence_clock": clock_at(start_clock, ev["t_min"]),
            "consequence_t": round(now_t + ev["t_min"], 2), "usd_at_risk": int(round(usd))}


class Detector:
    def __init__(self, fmap: FieldMap, stale_after_min: float = 5.0, declared=()) -> None:
        """declared: (asset, signal) keys a real connector promises. One that never reports goes
        SENSOR_STALE after stale_after_min from the first evaluation, so missing data is never silent."""
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
        self.declared: set[Key] = set(declared)
        self.expected_keys |= self.declared
        self.t0: Optional[float] = None              # live time of the first evaluation

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
        if self.t0 is None:
            self.t0 = t

        def fresh(key: Key) -> bool:
            r = latest.get(key)
            return r is not None and t - r["t"] <= self.stale_after

        def in_grace(key: Key) -> bool:
            # A declared signal that hasn't reported yet is not stale until stale_after has passed
            # since the first evaluation (connectors may still be connecting); it is never 'fresh'.
            return key not in latest and key in self.declared and t - self.t0 <= self.stale_after

        # SENSOR_STALE
        stale_now = {k for k in self.expected_keys if not fresh(k) and not in_grace(k)}
        self.stale_keys = stale_now
        for aid in {a for a, _ in self.expected_keys}:
            sigs = sorted(s for a, s in stale_now if a == aid)
            if sigs:
                ages = [t - latest[(aid, s)]["t"] for s in sigs if (aid, s) in latest]
                age = f"for {max(ages):.0f} min" if ages else "since startup"
                msg = (f"No {', '.join(sigs)} reading from {name(aid)} {age}. "
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

        # TANK_HIGH, TANK_HIGH_HIGH (fresh levels only: a stale tank is SENSOR_STALE, never OK)
        for tid, tank in self.f.tanks.items():
            if not fresh((tid, "level_pct")):
                continue
            lvl = float(latest[(tid, "level_pct")]["value"])
            for kind, sev, at, what in (("TANK_HIGH", "WARNING", self.f.warning_pct(tid), "high"),
                                        ("TANK_HIGH_HIGH", "ALARM", self.f.high_high_pct(tid), "high-high")):
                bad = True if lvl >= at else False if lvl < at - LEVEL_DEADBAND else None
                msg = (f"{name(tid)} is at {lvl:.0f}%, over its {what} level of {at:g}%. "
                       f"It shuts in at {float(tank['shutdown_pct']):g}%.")
                changed += self._set(tid, kind, sev, msg, bad, t, clock)

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

    # ---------- root cause (CF-7) ----------
    def root_for(self, asset_id: str, kind: str) -> Optional[dict]:
        """The active alarm this one is a consequence of, or None when it is a root itself.

        A tank's high or high-high level points at a trip of one of its pumps, else at a
        mismatch on one of its pumps' discharge valves. A high-high with neither points at
        the same tank's high alert, so the pair still folds into one row."""
        f = self.f
        if kind not in TANK_KINDS or asset_id not in f.tanks:
            return None
        pumps = sorted(f.pumps_by_tank.get(asset_id, []))
        keys = [(p, "PUMP_TRIP") for p in pumps] + \
               [(f.pump_valve[p], "VALVE_MISMATCH") for p in pumps if p in f.pump_valve]
        for key in keys:
            if key in self.active:
                return self.active[key]
        if kind == "TANK_HIGH_HIGH":
            return self.active.get((asset_id, "TANK_HIGH"))
        return None

    def link_roots(self) -> list[dict]:
        """Give an active alert with no root the root that appeared since (a trip confirmed
        after its tank already read high). A root, once set, is kept even after it resolves:
        it is still why this alarm happened. Returns the alerts it changed."""
        out = []
        for (aid, kind), a in self.active.items():
            if a.get("root_id") is None:
                root = self.root_for(aid, kind)
                if root is not None and root["id"] != a["id"]:
                    a["root_id"] = root["id"]
                    out.append(a)
        return out

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

    # ---------- alerts from outside the detector (plan runs) ----------
    def raise_external(self, aid: str, kind: str, severity: str, msg: str, clock: str = "") -> dict:
        """Raise one alert that evaluate() doesn't own, or refresh its message if it's active."""
        key = (aid, kind)
        if key in self.active:
            self.active[key].update(message=msg, severity=severity)
            return self.active[key]
        alert = {"id": f"alert_{next(self._ids)}", "asset_id": aid, "kind": kind, "severity": severity,
                 "message": msg, "t": now_iso(), "clock": clock, "active": True}
        self.active[key] = alert
        self.alerts.append(alert)
        return alert

    def resolve_external(self, aid: str, kind: str) -> Optional[dict]:
        alert = self.active.pop((aid, kind), None)
        if alert:
            alert.update(active=False, resolved_t=now_iso())
        return alert

    def _set(self, aid: str, kind: str, severity: str, msg: str, bad: Optional[bool], t: float,
             clock: str, confirm: int = CONFIRM_READINGS) -> list[dict]:
        key = (aid, kind)
        if bad is None:
            return []
        if bad:
            self.good[key] = 0
            self.bad[key] = self.bad.get(key, 0) + 1
            if key not in self.active and self.bad[key] >= confirm:
                root = self.root_for(aid, kind)
                alert = {"id": f"alert_{next(self._ids)}", "asset_id": aid, "kind": kind, "severity": severity,
                         "message": msg, "t": now_iso(), "clock": clock, "active": True,
                         "root_id": root["id"] if root else None, "priority": "P3",
                         "minutes_to_consequence": None, "minutes_to_consequence_low": None,
                         "consequence": None, "consequence_asset_id": None,
                         "consequence_clock": None, "consequence_t": None, "usd_at_risk": 0,
                         "shelved_until": None, "shelved_until_t": None, "shelve_reason": None,
                         "shelved_by": None, "shelved_t": None}
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
