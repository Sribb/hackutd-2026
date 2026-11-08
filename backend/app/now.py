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
  So does a flow that can't be right (NaN, infinite, not a number, or more than
  FLOW_NOISE_BBL_HR below zero), with a warning; it is never read as a shut-in well.
- the simulator's readings never count for an asset a field connector declares. The
  registry gives the simulator every signal no field connector declares, so a pump whose
  field connector reports only `running` still has a simulated flow; when the real run
  signal goes bad, that pump is unknown (off in the forecast, with a warning), not running.
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
- a field connector's own quality counts at once (#126). When a connector reports a
  signal as anything but good (bad, uncertain, fault, out_of_range, comm_lost, stale,
  null), that signal has no fresh reading here, even if its last good value is still
  inside stale_after_min: a tank has no level, a valve counts as closed and a pump as off
  (with the warnings above), and a well keeps its design rate. /api/state shows the asset
  UNKNOWN, with a reason naming the connector and the quality.
- a pump with neither a fresh run nor a fresh flow reading shows UNKNOWN in /api/state,
  never OK.
- a level that is not a usable percentage (not a number, NaN, infinite, or more than
  LEVEL_SLACK_PCT past 0 or 100) is no level: the forecast warns about the tank, and
  /api/state shows it UNKNOWN with the reading as last_level_pct. The SpacetimeDB mirror
  uses the same rule (usable_level).
- /api/state never shows a value nobody vouches for as current. When an asset is stale,
  or one of its signals is stale or not good, its level_pct, running, flow_bbl_hr and
  position are null, and the last values ride along as last_level_pct, last_running,
  last_flow_bbl_hr and last_position.
- these warnings ride in the scenario as `live_assumptions`, so every forecast, plan, and
  Last Call from this moment carries them (build_plant adds them to its warnings).
- trucks already on the road (the simulator's jobs) arrive, load, and haul on their real
  timetable with the barrels they can still take, and a plan never sends them twice.
"""
from __future__ import annotations

import copy
import math
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


GOOD = "good"
LEVEL_SLACK_PCT = 0.5   # sensor noise past 0 or 100 that is clamped instead of dropped
SHOWN = ("level_pct", "running", "flow_bbl_hr", "position")   # values /api/state shows as readings
FLOW_NOISE_BBL_HR = 1.0  # a well flow this far below zero is meter noise (read as 0); further is a broken meter


def usable_level(v) -> Optional[float]:
    """A tank level as a percentage, clamped to 0..100, or None when it isn't usable:
    not a number, NaN, infinite, or more than LEVEL_SLACK_PCT past 0 or 100."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x < -LEVEL_SLACK_PCT or x > 100.0 + LEVEL_SLACK_PCT:
        return None
    return min(100.0, max(0.0, x))


def bad_quality(live) -> dict[tuple, dict]:
    """{(asset_id, signal): {"connector", "quality", "detail", "t"}} for every signal a field
    connector reports as not good right now (its `quality` dict, #126). `t` is the live
    minute the connector set that quality, or None. Connectors without a quality dict (the
    simulator) report nothing here."""
    out: dict[tuple, dict] = {}
    for c in getattr(getattr(live, "registry", None), "connectors", None) or []:
        q = getattr(c, "quality", None)
        if not isinstance(q, dict) or not q:
            continue
        items = None
        for _ in range(3):   # the connector's I/O thread may be adding a key right now
            try:
                items = list(q.items())
                break
            except RuntimeError:
                continue
        for key, entry in items or []:
            quality = entry.get("quality") if isinstance(entry, dict) else entry
            if quality == GOOD:
                continue
            detail = str(entry.get("detail") or "") if isinstance(entry, dict) else ""
            t = entry.get("t") if isinstance(entry, dict) else None
            out[tuple(key)] = {"connector": str(getattr(c, "id", "?")), "quality": str(quality or "unknown"),
                               "detail": detail,
                               "t": float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None}
    return out


def field_assets(live) -> set:
    """Every asset a field connector (anything but the simulator) declares a signal of."""
    registry = getattr(live, "registry", None)
    sim = getattr(registry, "sim", None)
    out = set()
    for c in getattr(registry, "connectors", None) or []:
        if c is sim:
            continue
        out |= {aid for aid, _ in (getattr(c, "signals", None) or {})}
    return out


def _simulated(live, asset_id: str, signal: str) -> bool:
    """True when the latest reading came from the simulator for an asset a field connector
    declares. The registry gives the simulator every signal no field connector declares, so
    a field pump that reports only `running` still gets a simulated flow. That flow says
    nothing about the real pump, so it never stands in for a run reading that went bad."""
    registry = getattr(live, "registry", None)
    sim = getattr(registry, "sim", None)
    if sim is None:
        return False
    r = (getattr(live, "latest", None) or {}).get((asset_id, signal))
    return bool(r) and r.get("source") == getattr(sim, "id", None) and asset_id in field_assets(live)


def _fresh(live, asset_id: str, signal: str, bad: Optional[dict] = None):
    """The latest reading, or None when there is none, it's stale, its connector says its
    quality isn't good (`bad`: bad_quality(live), worked out here when not given), or the
    simulator made it up for an asset a field connector declares."""
    if live.detector.is_stale(asset_id, signal):
        return None
    if (asset_id, signal) in (bad_quality(live) if bad is None else bad):
        return None
    if _simulated(live, asset_id, signal):
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
    levels = sc.get("initial_levels_pct")
    if isinstance(levels, dict):
        for (aid, sig) in bad_quality(live):
            if sig == "level_pct" and aid in levels:
                levels[aid] = None   # a level its connector doesn't vouch for is no level
        for aid, lvl in list(levels.items()):
            if lvl is not None and usable_level(lvl) is None:
                levels[aid] = None   # NaN, negative, or far past 100: never a level to plan from
                notes.append(f"{nice(aid)} reads a level that is not a usable percentage ({_shown(lvl)}), "
                             f"so the forecast has no level for it. Check it in the field.")
            elif lvl is not None:
                levels[aid] = usable_level(lvl)
    for w in sc["field"]["wells"]:
        flow = _fresh(live, w["id"], "flow_bbl_hr")
        if flow is None:
            continue
        x = None
        if not isinstance(flow, bool):
            try:
                x = float(flow)
            except (TypeError, ValueError):
                x = None
        if x is None or not math.isfinite(x) or x < -FLOW_NOISE_BBL_HR:
            # A broken meter is not a shut-in well: keep the design rate, the worst case.
            notes.append(f"{nice(w['id'])} reads a flow that can't be right ({_shown(flow)}), so the forecast "
                         f"keeps its design rate of {nice_rate(w.get('water_bbl_hr'))}. Check it in the field.")
            continue
        w["water_bbl_hr"] = max(0.0, x)

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


def nice_rate(v) -> str:
    try:
        return f"{float(v):g} bbl/hr"
    except (TypeError, ValueError):
        return "unknown"


def _shown(v) -> str:
    """A bad reading for a warning: short, and never more than a few characters of text."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:g}" if math.isfinite(v) else str(v)
    return repr(v)[:24]


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
            entry["level_pct"] = la["level_pct"]
    _unusable_levels(st)
    _unknown_pumps(live, sc, st)
    _mark_quality(live, st)
    _blank_unvouched(live, st)
    st["clock_source"] = "live"
    st["t"] = lv.get("t")
    return st


def _unusable_levels(st: dict) -> None:
    """A level that is not a usable percentage shows UNKNOWN (ALARM stays ALARM), with the
    reading as last_level_pct and no level_pct. A usable one is clamped to 0..100."""
    for aid, entry in st["assets"].items():
        lvl = entry.get("level_pct")
        if lvl is None:
            continue
        good = usable_level(lvl)
        if good is not None:
            entry["level_pct"] = good
            continue
        entry["last_level_pct"] = lvl if isinstance(lvl, (int, float)) and not isinstance(lvl, bool) else None
        entry["level_pct"] = None
        entry["stale"] = True
        if entry.get("status") != "ALARM":
            entry["status"] = "UNKNOWN"
        note = f"The level reads {_shown(lvl)}, which is not a usable percentage. Treat it as unknown, not safe."
        old = entry.get("reason")
        entry["reason"] = f"{old} {note}" if old and note not in old else note


def _blank_unvouched(live, st: dict) -> None:
    """No value nobody vouches for is shown as current. For an asset that is stale, or has
    a signal that is stale or not good, level_pct, running, flow_bbl_hr and position are
    null, and the old values move to last_<name>."""
    bad = bad_quality(live)
    for aid, entry in st["assets"].items():
        blank_all = bool(entry.get("stale")) or any(a == aid for a, _ in bad)
        for k in SHOWN:
            if k not in entry or entry[k] is None:
                continue
            if blank_all or live.detector.is_stale(aid, k):
                entry.setdefault(f"last_{k}", entry[k])
                entry[k] = None


def _unknown_pumps(live, sc: dict, st: dict) -> None:
    """A pump with neither a fresh run nor a fresh flow reading is UNKNOWN, never OK
    (live.py's own pump status can say OK when only another signal is fresh). A pump in
    ALARM stays in ALARM."""
    bad = bad_quality(live)
    for p in sc["field"]["pumps"]:
        entry = st["assets"].get(p["id"])
        if entry is None or entry.get("status") == "ALARM":
            continue
        if _fresh(live, p["id"], "running", bad) is None and _fresh(live, p["id"], "flow_bbl_hr", bad) is None:
            entry["status"] = "UNKNOWN"
            entry["stale"] = True
            if not entry.get("reason"):
                entry["reason"] = "No fresh run or flow reading. Treat as unknown, not safe."


def _mark_quality(live, st: dict) -> None:
    """Every asset with a signal its connector reports as not good shows UNKNOWN at once
    (ALARM stays ALARM), with a reason naming the connector and the quality. A level or
    position it doesn't vouch for is not shown as the reading."""
    for (aid, sig), q in sorted(bad_quality(live).items()):
        entry = st["assets"].get(aid)
        if entry is None:
            continue
        if entry.get("status") != "ALARM":
            entry["status"] = "UNKNOWN"
        entry["stale"] = True
        entry.setdefault("quality", {})[sig] = q["quality"]
        detail = f" ({q['detail']})" if q["detail"] else ""
        note = f"{q['connector']} reports {sig} as {q['quality']}{detail}. Treat as unknown, not safe."
        old = entry.get("reason")
        entry["reason"] = f"{old} {note}" if old and note not in old else note


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
