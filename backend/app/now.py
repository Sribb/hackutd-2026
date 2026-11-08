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
  alarm is confirmed, so the forecast never shows a tank as safe in that gap.
- a valve's position is its live reading (live.live_scenario does that).
- a tank with a stale level has no level, so the forecast warns instead of calling it safe.
- trucks already on the road (the simulator's jobs) arrive, load, and haul on their real
  timetable with the barrels they can still take, and a plan never sends them twice.
"""
from __future__ import annotations

import copy
import os
import threading
from dataclasses import dataclass
from typing import Optional

from .forecast import Plant, build_plant, clock_at
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


def under_way(live, scenario: dict) -> list[dict]:
    """Actions already in motion on the live field, timed from now. Pure: reads only."""
    actions: list[dict] = []
    tripped = {f["asset_id"] for f in scenario.get("failures", [])}
    for p in scenario["field"]["pumps"]:
        running = _fresh(live, p["id"], "running")
        if running is not None and not _truthy(running) and p["id"] not in tripped:
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
    """live.live_scenario() plus wells at their live flow, so a confirmed choke counts."""
    sc = live.live_scenario()
    for w in sc["field"]["wells"]:
        flow = _fresh(live, w["id"], "flow_bbl_hr")
        if flow is None:
            continue
        try:
            w["water_bbl_hr"] = max(0.0, float(flow))
        except (TypeError, ValueError):
            pass
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
