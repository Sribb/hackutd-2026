"""One clock (#42): the moment every backend endpoint plans from.

With the live layer mounted (live.py, #38), "now" is the live field: its clock, its
readings, and what people have confirmed since. GET /api/state, POST /api/forecast,
POST /api/plans, approve, Last Call, and the SpacetimeDB mirror all read it here, so the
console, the phone, Jarvis, and every open screen tell one story. Without the live
layer, or with FREEBOARD_ONE_CLOCK=0, "now" is the active scenario plus the plans
approved since it was activated.

"Already done" comes from the live readings, because with real connectors the readings
are the truth, not the simulator:
- a well's water rate is its live flow reading, so a confirmed choke or shut-in counts.
  A missing or stale reading keeps the design rate: never assume less water than we know.
- a pump that reads not running counts as off from its first reading, before its trip
  alarm is confirmed, so the forecast never shows a tank as safe in that gap. Its run
  reading comes first, then its flow (over 10% of its rate means running).
- a pump with neither a fresh run nor a fresh flow reading counts as off, with a warning:
  never assume it is still draining its tank.
- a valve's position is its live reading. A valve with no fresh OPEN or CLOSED reading
  counts as closed, with a warning. For a discharge valve that is the worst case for its
  tank. For a crossover, closed gives the earliest shutdown of the two tanks it joins
  (an open crossover only moves water from the higher tank to the lower), and the warning
  says the lower tank could fill faster than shown.
- a tank with a stale level has no level, so the forecast warns instead of calling it safe.
- these warnings ride in the scenario as `live_assumptions`, so every forecast, plan, and
  Last Call from this moment carries them (build_plant adds them to its warnings).
- trucks already on the road (the simulator's jobs) arrive, load, and haul on their real
  timetable with the barrels they can still take, and a plan never sends them twice.
"""
from __future__ import annotations

import copy
import os
import threading
from dataclasses import dataclass
from typing import Optional

from .forecast import Plant, build_plant, clock_at, nice
from .scenario import state_payload

_lock = threading.RLock()
_static: dict = {"scenario": None, "plant": None, "applied": []}
_local = threading.local()   # Last Call's request-scoped "now" (see pinned())


@dataclass(frozen=True)
class Now:
    scenario: dict      # the field as it is now: clock, levels, trips, valve positions
    plant: Plant        # build_plant(scenario)
    actions: tuple      # already under way; run them before any what-if (at_min from now)
    source: str         # "live" or "static"
    t: float            # live minutes since the live layer started (0.0 when static)


# ---------- the static field (no live layer) ----------

def set_static(scenario: dict) -> None:
    """The active scenario, at startup and on every blueprint activation. Clears what was
    approved on the previous field, since its IDs may not exist on this one."""
    with _lock:
        _static.update(scenario=scenario, plant=build_plant(scenario), applied=[])


def record(actions: list[dict]) -> None:
    """Approving a plan with no live layer records its actions (with their lead-time
    at_min), so /api/state shows them and every later forecast includes them."""
    with _lock:
        _static["applied"].extend(copy.deepcopy(a) for a in actions)


def _static_now() -> Now:
    with _lock:
        if _static["scenario"] is None:
            raise RuntimeError("now.set_static() was never called.")
        return Now(_static["scenario"], _static["plant"], tuple(_static["applied"]), "static", 0.0)


# ---------- the live field ----------

def one_clock() -> bool:
    return os.environ.get("FREEBOARD_ONE_CLOCK", "1").strip() != "0"


def live_of(app) -> Optional[object]:
    """The live layer, when it's mounted and one clock is on."""
    live = getattr(getattr(app, "state", None), "live", None)
    return live if live is not None and one_clock() else None


def _fresh(live, asset_id: str, signal: str):
    """The latest reading, or None when there is none or it's stale."""
    if live.detector.is_stale(asset_id, signal):
        return None
    return live.value(asset_id, signal)


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().upper() in ("1", "TRUE", "ON", "RUNNING", "YES")
    return bool(v)


_OPEN = {"OPEN", "OPENED", "1", "TRUE", "ON"}
_CLOSED = {"CLOSED", "CLOSE", "SHUT", "0", "FALSE", "OFF"}


def _position(v) -> Optional[str]:
    """OPEN, CLOSED, or None for anything else (missing, TRAVEL, FAULT, ...)."""
    if isinstance(v, bool):
        return "OPEN" if v else "CLOSED"
    if v is None:
        return None
    s = str(v).strip().upper()
    return "OPEN" if s in _OPEN else "CLOSED" if s in _CLOSED else None


def pump_running(live, pump: dict) -> Optional[bool]:
    """From a fresh run reading, else a fresh flow reading (over 10% of its rate means
    running). None when neither is fresh: unknown, never assumed to be running."""
    running = _fresh(live, pump["id"], "running")
    if running is not None:
        return _truthy(running)
    flow = _fresh(live, pump["id"], "flow_bbl_hr")
    if flow is None:
        return None
    try:
        return float(flow) > 0.1 * float(pump.get("rate_bbl_hr") or 0.0)
    except (TypeError, ValueError):
        return None


def under_way(live, scenario: dict) -> list[dict]:
    """Actions already in motion on the live field, timed from now. Pure: reads only.
    A pump that reads off, or has no fresh reading at all, runs OFF from now."""
    actions: list[dict] = []
    tripped = {f["asset_id"] for f in scenario.get("failures", [])}
    for p in scenario["field"]["pumps"]:
        if p["id"] not in tripped and not pump_running(live, p):
            actions.append({"type": "SET_PUMP", "asset_id": p["id"], "value": "OFF", "at_min": 0.0})

    sim = getattr(getattr(live, "registry", None), "sim", None)
    if sim is None:
        return actions
    trucks = {t["id"] for t in scenario["field"]["trucks"]}
    now = float(sim.t)
    for j in sorted(getattr(sim, "truck_jobs", []), key=lambda j: j["depart"]):
        tid, target = j["truck_id"], j["target_id"]
        if now >= j["done"] or tid not in trucks:
            continue
        base = {"type": "SEND_TRUCK", "asset_id": tid, "target_id": target}
        if j["depart"] > now:                                   # queued behind its current trip
            actions.append({**base, "at_min": j["depart"] - now})
        elif now < j["load_end"] and j["remaining"] > 0:         # on the way, or loading
            actions.append({**base, "at_min": 0.0,
                            "arrive_in": max(0.0, j["arrive"] - now),
                            "load_min": max(0.0, j["load_end"] - max(now, j["arrive"])),
                            "capacity_bbl": float(j["remaining"]),
                            "haul_min": max(0.0, j["done"] - j["load_end"])})
        else:                                                    # full, hauling to disposal
            actions.append({**base, "at_min": 0.0, "busy_min": j["done"] - now})
    return actions


def live_scenario(live) -> dict:
    """live.live_scenario() plus wells at their live flow (so a confirmed choke counts), and
    the worst case for anything with no fresh reading, each with a warning in
    `live_assumptions`."""
    sc = live.live_scenario()
    notes: list[str] = []
    for w in sc["field"]["wells"]:
        flow = _fresh(live, w["id"], "flow_bbl_hr")
        if flow is None:
            continue
        try:
            w["water_bbl_hr"] = max(0.0, float(flow))
        except (TypeError, ValueError):
            pass

    tanks = {t["id"] for t in sc["field"]["tanks"]}
    pumps = {p["id"] for p in sc["field"]["pumps"]}
    for v in sc["field"]["valves"]:
        pos = _position(_fresh(live, v["id"], "position"))
        if pos is not None:
            v["position"] = pos
            continue
        v["position"] = "CLOSED"
        # By what it connects: not every field file has `kind` (backend/data/scenario.json doesn't).
        ends = [a for a in (v.get("connects") or []) if isinstance(a, str)]
        pump = v.get("pump_id") if v.get("pump_id") in pumps else next((a for a in ends if a in pumps), None)
        joined = [a for a in ends if a in tanks]
        if pump:
            notes.append(f"{nice(v['id'])} has no fresh position reading, so the forecast assumes it is closed "
                         f"and {nice(pump)} can't drain its tank. Check it in the field.")
        elif len(joined) == 2:
            notes.append(f"{nice(v['id'])} has no fresh position reading, so the forecast assumes it is closed. "
                         f"If it is open, the lower of {nice(joined[0])} and {nice(joined[1])} fills faster "
                         f"than shown.")
        else:
            notes.append(f"{nice(v['id'])} has no fresh position reading, so the forecast assumes it is closed.")

    tripped = {f["asset_id"] for f in sc.get("failures", [])}
    for p in sc["field"]["pumps"]:
        if p["id"] not in tripped and pump_running(live, p) is None:
            tank = f" and {nice(p['tank_id'])} is filling" if p.get("tank_id") else ""
            notes.append(f"{nice(p['id'])} has no fresh run or flow reading, so the forecast assumes it is off"
                         f"{tank}. Check it in the field.")
    sc["live_assumptions"] = notes
    return sc


def _live_now(live) -> Now:
    sc = live_scenario(live)
    return Now(sc, build_plant(sc), tuple(under_way(live, sc)), "live", float(live.t))


# ---------- what endpoints call ----------

def get(app) -> Now:
    """The current moment. Cheap enough to call per request (a copy and a plant build)."""
    pinned_now = getattr(_local, "now", None)
    if pinned_now is not None:
        return pinned_now
    live = live_of(app)
    return _live_now(live) if live is not None else _static_now()


class pinned:
    """`with now.pinned(app):` makes every get() in this thread return one moment, so a
    request that forecasts many times (Last Call) plans from a single instant even while
    the live loop ticks."""

    def __init__(self, app) -> None:
        self.app = app

    def __enter__(self) -> Now:
        _local.now = None
        _local.now = get(self.app)
        return _local.now

    def __exit__(self, *exc) -> None:
        _local.now = None


def state(app) -> dict:
    """GET /api/state in the contract's shape, from the same moment as the forecast.

    Live: statuses come from the live layer (UNKNOWN for a stale reading, ALARM for a
    confirmed trip), so /api/state and /api/live/state always agree. Static: the scenario,
    with approved actions shown on the assets they change.
    """
    live = live_of(app)
    if live is None:
        n = _static_now()
        st = state_payload(n.scenario)
        _overlay(st, n)
        st["clock_source"] = "static"
        return st
    sc = live_scenario(live)
    st = state_payload(sc)
    lv = live.state()
    st["clock"] = lv["clock"]
    for aid, la in lv["assets"].items():
        entry = st["assets"].setdefault(aid, {})
        entry["status"] = la.get("status") or entry.get("status", "UNKNOWN")
        reason = la.get("reason") or (entry.get("reason") if entry["status"] != "OK" else None)
        if reason:
            entry["reason"] = reason
        else:
            entry.pop("reason", None)
        for k in ("position", "stale", "age_min", "flow_bbl_hr", "running", "phase", "target_id"):
            if k in la:
                entry[k] = la[k]
        if "level_pct" in la:
            # A stale level is not a level: show it as last_level_pct, never as the reading.
            if la.get("stale"):
                entry["level_pct"] = None
                entry["last_level_pct"] = la["level_pct"]
            else:
                entry["level_pct"] = la["level_pct"]
    st["clock_source"] = "live"
    st["t"] = lv.get("t")
    return st


def _overlay(st: dict, n: Now) -> None:
    """Static mode: show approved actions on the assets they change."""
    assets = st["assets"]
    for a in n.actions:
        aid, typ = a["asset_id"], a["type"]
        if aid not in assets:
            continue
        when = clock_at(n.plant.start_clock, a.get("at_min", 0))
        later = f" Takes effect around {when}." if a.get("at_min", 0) > 0 else ""
        if typ == "SET_VALVE":
            assets[aid]["position"] = a["value"]
            assets[aid]["reason"] = f"{'Opened' if a['value'] == 'OPEN' else 'Closed'} by an approved plan.{later}"
        elif typ == "SET_PUMP" and a["value"] == "OFF":
            assets[aid] = {"status": "OFFLINE", "reason": f"Turned off by an approved plan.{later}"}
        elif typ == "CHOKE_WELL":
            kept = float(a["value"])
            assets[aid] = ({"status": "OFFLINE", "reason": f"Shut in by an approved plan.{later}"} if kept == 0 else
                           {"status": "WARNING", "reason": f"Choked to {round(kept * 100)}% by an approved plan.{later}"})
        elif typ == "SEND_TRUCK":
            assets[aid]["reason"] = f"Sent to {a.get('target_id')} by an approved plan.{later}"
    st["applied_actions"] = [dict(a) for a in n.actions]
