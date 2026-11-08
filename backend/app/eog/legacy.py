"""The legacy console shapes (docs/CONTRACT.md "API") built from the active EOG dataset (#153).

With dataset mode on, GET /api/field, GET /api/state, POST /api/forecast, POST /api/plans,
POST /api/plans/{id}/approve and POST /api/lastcall answer from the sponsor's data instead of the
demo scenario, so the existing console shows the sponsor network with no URL changes. The routes
live in legacy_api.py; this module is the payloads.

One source of numbers. Every event, countdown, plan and Last Call row comes from the same Service
the /api/eog routes use (service.SERVICE: its cached detect.detect, plan.plan_failures,
plan.wait_curve and plan.alternatives), so the console, /api/eog and events.json never disagree.
Countdowns are the Event's (filled from plan.countdown); a forecast with actions replays the tank
with plan.simulate from the same start level the plans use.

Time is data time. "Now" is the view's last telemetry row at the Service's clock cursor, and
legacy minutes (t_min, at_min, expires_min) count from it. Clock text is HH:MM in the dataset's
time zone (meta.timezone, UTC for the sponsor's data).

The sponsor's world has trucks and well shut-ins, nothing else: SEND_TRUCK maps to a truck call
(arriving response_min later, then a trip every cycle_min; `value` is trips for that truck,
default 1) and CHOKE_WELL with value 0 to a shut-in until the repair. SET_VALVE, SET_PUMP and a
partial choke are refused with a sentence (422).
"""
from __future__ import annotations

import math
from bisect import bisect_right
from datetime import datetime, timezone
from typing import Any, Optional

from . import plan as planmod
from . import service
from .dataset import Dataset
from .model import Event, iso, money
from .service import BadInput, NotFound, clock_text

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8
    ZoneInfo = None

NOT_SPONSOR = ("{kind} is not in the sponsor's world: the EOG network has trucks and well shut-ins only "
               "(SEND_TRUCK, and CHOKE_WELL with value 0).")


# ---------------------------------------------------------------- small helpers
def _svc(svc: Optional[service.Service]) -> service.Service:
    return svc if svc is not None else service.SERVICE


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _tz(ds: Dataset):
    name = ((ds.network.get("meta") or {}).get("timezone") or "UTC") if isinstance(ds.network, dict) else "UTC"
    if ZoneInfo is not None and name.upper() != "UTC":
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - an unknown zone falls back to UTC, the SPEC default
            pass
    return timezone.utc


def hhmm(ds: Dataset, ts: Optional[float]) -> Optional[str]:
    """ts_unix -> "HH:MM" in the dataset's time zone. None stays None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=_tz(ds)).strftime("%H:%M")


def minutes(ts: Optional[float], now: int) -> Optional[float]:
    return None if ts is None else round((float(ts) - now) / 60.0, 1)


def truck_ids(system_id: str, fleet: int) -> list[str]:
    return [f"{system_id}_TRUCK_{n}" for n in range(1, int(fleet) + 1)]


def header(ds: Dataset, view: Dataset, at: int) -> dict:
    return {"mode": "dataset", "dataset": ds.id, "clock": hhmm(ds, view.now), "clock_ts": iso(view.now),
            "clock_source": "dataset", "at": at, "now": view.now}


def _ongoing(svc: service.Service, view: Dataset) -> tuple[list[Event], dict]:
    events = svc.events(view)
    return svc.ongoing_failures(events), svc.plans_now(view, events)


# ---------------------------------------------------------------- GET /api/field
def field_payload(ds: Dataset) -> dict:
    """Everything that doesn't change: the network as the legacy field shape, plus the systems
    (with their ids, so the web side can load each system's field.glb)."""
    tanks, pumps, wells, pipelines, disposal, trucks, systems = [], [], [], [], [], [], []
    for f in ds.facilities.values():
        tanks.append({"id": f.tank_id, "capacity_bbl": f.capacity_bbl, "shutdown_pct": ds.shutdown_pct,
                      "warning_pct": ds.warning_pct, "facility_id": f.id, "system_id": f.system})
        pumps.append({"id": f.pump_id, "tank_id": f.tank_id, "rate_bbl_hr": round(f.max_flow_bpd / 24.0, 4),
                      "facility_id": f.id, "system_id": f.system})
        for w in f.wells:
            wells.append({"id": w.id, "tank_id": f.tank_id, "water_bbl_hr": round(w.baseline_water_bpd / 24.0, 4),
                          "lost_usd_hr": round(ds.revenue_per_hour(w.id), 2), "oil_bpd": w.oil_bpd,
                          "gas_mcfd": w.gas_mcfd, "facility_id": f.id, "system_id": f.system})
        pipelines.append({"id": f.pipeline_id, "facility_id": f.id, "system_id": f.system,
                          "max_bpd": f.pipeline_max_bpd, "max_pressure_psi": f.pipeline_max_pressure_psi})
    for s in ds.systems.values():
        facs = [ds.facilities[i] for i in s.facility_ids]
        disposal.append({"id": s.disposal_id, "system_id": s.id, "pressure_psi": s.disposal_pressure_psi})
        ids = truck_ids(s.id, s.fleet_size)
        for tid in ids:
            trucks.append({"id": tid, "system_id": s.id, "capacity_bbl": ds.truck_capacity_bbl,
                           "cost_per_trip": ds.truck_cost,
                           "minutes_to": {f.tank_id: f.response_min for f in facs},
                           "cycle_min": {f.tank_id: f.cycle_min for f in facs}})
        systems.append({"id": s.id, "facility_ids": list(s.facility_ids), "tank_ids": [f.tank_id for f in facs],
                        "truck_ids": ids, "fleet_size": s.fleet_size, "disposal_id": s.disposal_id,
                        "glb": f"/api/dataset/field.glb?system={s.id}"})
    return {"mode": "dataset", "dataset": ds.id, "tanks": tanks, "pumps": pumps, "valves": [], "wells": wells,
            "disposal_wells": disposal, "pipelines": pipelines, "trucks": trucks, "systems": systems,
            "lead_min": {"SEND_TRUCK": 0, "CHOKE_WELL": 0}, "repair_hours": ds.repair_hours,
            "costs": {"truck_per_trip_usd": ds.truck_cost, "oil_per_bbl": ds.oil_price, "gas_per_mcf": ds.gas_price}}


# ---------------------------------------------------------------- GET /api/state
def _legacy_status(kind: str, eog: dict) -> str:
    """docs/CONTRACT.md "Status colors" from the dataset statuses (OK/INSPECT/WARNING/ALARM).
    A missing reading is UNKNOWN, never OK; a shut-in well is OFFLINE; INSPECT is WARNING."""
    st, r = eog["status"], eog.get("readings") or {}
    if kind == "well":
        if r.get("status") is None:
            return "UNKNOWN"
        return "OFFLINE" if st == "ALARM" else "OK"
    if st == "ALARM":
        return "ALARM"
    if st == "INSPECT":
        return "WARNING"
    if st == "WARNING":
        missing = ((kind == "tank" and r.get("level_pct") is None) or (kind == "pump" and r.get("status") is None)
                   or (kind == "pipeline" and all(v is None for v in r.values())))
        return "UNKNOWN" if missing else "WARNING"
    return "OK"


def state_payload(ds: Dataset, view: Dataset, at: int, svc: Optional[service.Service] = None) -> dict:
    """The readings at the view's now with legacy statuses, and the top alert as the incident."""
    svc = _svc(svc)
    st = svc.state(at)                     # the same statuses and reasons as GET /api/eog/state
    assets: dict[str, dict] = {}
    for aid, a in st["assets"].items():
        kind = a["kind"]
        if kind == "facility":
            continue
        out: dict = {"status": _legacy_status(kind, a), "kind": kind, "system_id": a["system"],
                     "facility_id": a["facility"]}
        if kind == "tank":
            lvl = a["readings"].get("level_pct")
            out["level_pct"] = None if lvl is None else round(float(lvl), 2)
        if kind == "pump":
            out["running"] = a["readings"].get("status") == 1
        if out["status"] != "OK":
            out["reason"] = " ".join(a["reasons"]) or "No reading at this time, so it can't be called safe."
        if a["events"]:
            out["events"] = list(a["events"])
        assets[aid] = out
    for s in ds.systems.values():
        for tid in truck_ids(s.id, s.fleet_size):
            assets[tid] = {"status": "OK", "kind": "truck", "system_id": s.id, "facility_id": None,
                           "location_text": f"At the {s.id} truck yard (the recording dispatches none)"}
    incident = None
    try:
        top = svc.alert_entries(ds, view)
    except Exception:  # noqa: BLE001 - the map stays up on the readings alone (warnings say so)
        top = []
    if top:
        a = top[0]
        incident = {"root_asset_id": a["asset"], "summary": a["title"] + ".", "event_id": a["event_id"],
                    "category": a["category"], "facility_id": a["facility"], "alerts": len(top)}
    return {**header(ds, view, at), "assets": assets, "incident": incident, "warnings": list(st["warnings"])}


# ---------------------------------------------------------------- legacy actions
def plan_actions(ds: Dataset, p: Any, trucks: list[str]) -> list[dict]:
    """A Plan's events.json actions as legacy actions: one SEND_TRUCK per truck (value = trips)
    and one CHOKE_WELL (value 0, shut in) per well, timed in minutes from the plan's start."""
    fac = ds.facilities[_get(p, "facility_id")]
    out, pool = [], list(trucks)
    for a in _get(p, "actions", []) or []:
        if a.get("type") == "dispatch_truck":
            for _ in range(int(a.get("trucks", 1) or 1)):
                tid = pool.pop(0) if pool else f"{fac.system}_TRUCK_?"
                out.append({"type": "SEND_TRUCK", "asset_id": tid, "target_id": fac.tank_id,
                            "value": int(a.get("trips_per_truck", 1) or 1), "at_min": 0})
        elif a.get("type") == "shut_in_wells":
            for w in a.get("wells", []):
                out.append({"type": "CHOKE_WELL", "asset_id": w, "value": 0.0,
                            "at_min": float(a.get("start_after_min") or 0)})
    return out


def assign_trucks(ds: Dataset, ongoing: list[Event], plans: dict) -> dict[str, list[str]]:
    """Truck ids for each event's plan: the joint plan splits a system's fleet, so hand out the
    system's trucks in onset order (the planner's own order), never the same truck twice."""
    nxt: dict[str, int] = {}
    out: dict[str, list[str]] = {}
    for ev in sorted(ongoing, key=lambda e: (e.onset, e.id)):
        n = sum(int(a.get("trucks", 0) or 0) for a in _get(plans.get(ev.id), "actions", []) or []
                if a.get("type") == "dispatch_truck")
        ids = truck_ids(ev.system, ds.systems[ev.system].fleet_size)
        i = nxt.get(ev.system, 0)
        out[ev.id] = ids[i:i + n]
        nxt[ev.system] = i + n
    return out


def _to_eog_actions(ds: Dataset, failing: dict[str, Event], actions: list[dict]) -> tuple[dict, list[str]]:
    """Legacy actions -> {facility_id: events.json actions with start_after_min}, plus warnings.
    Anything the sponsor's world can't do is a BadInput sentence (422)."""
    by_fac: dict[str, list] = {}
    trucks: dict[tuple, int] = {}
    wells: dict[tuple, list] = {}
    used, warnings = set(), []
    truck_system = {t: s.id for s in ds.systems.values() for t in truck_ids(s.id, s.fleet_size)}
    tank_fac = {f.tank_id: f for f in ds.facilities.values()}
    for i, a in enumerate(actions):
        kind, aid, at_min = a.get("type"), a.get("asset_id"), float(a.get("at_min") or 0)
        where = f"actions[{i}] ({kind} {aid})"
        if kind in ("SET_VALVE", "SET_PUMP"):
            raise BadInput(NOT_SPONSOR.format(kind=kind))
        if kind == "SEND_TRUCK":
            if aid not in truck_system:
                raise BadInput(f"{where}: no truck by that id; the trucks are {', '.join(sorted(truck_system))}.")
            if aid in used:
                raise BadInput(f"{where}: {aid} is sent twice; each truck can be sent once.")
            f = tank_fac.get(a.get("target_id"))
            if f is None:
                raise BadInput(f"{where}: target_id must be a tank id, like {next(iter(tank_fac))}.")
            if truck_system[aid] != f.system:
                raise BadInput(f"{where}: {aid} belongs to {truck_system[aid]}, but {f.tank_id} is in {f.system}.")
            v = a.get("value")
            try:
                k = 1 if v is None else int(float(v))
            except (TypeError, ValueError):
                raise BadInput(f"{where}: value is the number of trips for that truck, a whole number of at least 1.") from None
            if k < 1 or (v is not None and float(v) != k) or k > 100:
                raise BadInput(f"{where}: value is the number of trips for that truck, a whole number from 1 to 100.")
            used.add(aid)
            if f.id not in failing:
                warnings.append(f"{aid} to {f.tank_id} changes nothing in this forecast: {f.pump_id} is running.")
                continue
            trucks[(f.id, at_min, k)] = trucks.get((f.id, at_min, k), 0) + 1
        elif kind == "CHOKE_WELL":
            w = ds.wells.get(aid)
            if w is None:
                raise BadInput(f"{where}: no well by that id.")
            v = a.get("value")
            if v is None or not isinstance(v, (int, float)) or float(v) not in (0.0, 1.0):
                raise BadInput(f"{where}: a partial choke is not in the sponsor's world, where a well is on or "
                               f"shut in, so value must be 0 (shut in) or 1 (leave it on).")
            if float(v) == 1.0:
                continue
            if w.facility not in failing:
                warnings.append(f"Shutting in {aid} changes nothing in this forecast: its facility's pump is running.")
                continue
            wells.setdefault((w.facility, at_min), []).append(aid)
        else:
            raise BadInput(f"{where}: unknown action type; use SEND_TRUCK or CHOKE_WELL.")
    for (fid, at_min, k), n in trucks.items():
        by_fac.setdefault(fid, []).append({"type": "dispatch_truck", "facility_id": fid, "trucks": n,
                                           "trips_per_truck": k, "start_after_min": at_min})
    for (fid, at_min), ids in wells.items():
        by_fac.setdefault(fid, []).append({"type": "shut_in_wells", "wells": ids, "start_after_min": at_min})
    return by_fac, warnings


# ---------------------------------------------------------------- POST /api/forecast
def _level_at(points: list, ts: float) -> Optional[float]:
    """The replay's level at ts: the last point at ts (after a truck round), else linear between."""
    if not points:
        return None
    times = [p[0] for p in points]
    i = bisect_right(times, ts)            # the first point after ts
    if i == 0:
        return points[0][1]
    t0, v0 = points[i - 1]
    if t0 == ts or i == len(points):
        return v0
    t1, v1 = points[i]
    return v0 + (v1 - v0) * (ts - t0) / (t1 - t0)


def forecast(ds: Dataset, view: Dataset, at: int, actions: list[dict], horizon_minutes: int = 360,
             step_minutes: int = 5, svc: Optional[service.Service] = None) -> dict:
    """Every tank with an ongoing pump failure replayed to its repair end under `actions`."""
    svc = _svc(svc)
    ongoing, _ = _ongoing(svc, view)
    failing = {e.facility: e for e in ongoing}
    by_fac, warnings = _to_eog_actions(ds, failing, actions)
    now = view.now
    runs, events, by_tank = {}, [], {}
    cost = {"total_usd": 0.0, "trucking_usd": 0.0, "lost_production_usd": 0.0, "disposal_usd": 0.0}
    for fid, ev in failing.items():
        f = ds.facilities[fid]
        level = svc.start_level(ds, view, ev)
        if level is None:
            warnings.append(f"{f.tank_id} has no level reading, so it can't be forecast. Treat it as unknown, not safe.")
            continue
        repair_end = svc.repair_end_of(ds, ev)
        mine = by_fac.get(fid, [])
        res = planmod.simulate(view, fid, now, level, repair_end, mine)
        warnings += [f"{f.tank_id}: {e}" for e in res["errors"]]
        runs[fid] = (res, repair_end, mine)
        for k in ("truck_cost", "shut_in_cost", "auto_shut_in_cost"):
            cost["trucking_usd" if k == "truck_cost" else "lost_production_usd"] += res[k]
        cost["total_usd"] += res["cost"]
        by_tank[f.tank_id] = {"event_id": ev.id, "total_usd": res["cost"], "trucking_usd": res["truck_cost"],
                              "lost_production_usd": round(res["shut_in_cost"] + res["auto_shut_in_cost"], 2),
                              "trip_at": res["trip_at"], "peak_level_pct": res["peak_level_pct"]}
        # The countdown (plan.countdown via the Event) when nothing changes; the replay otherwise.
        # The band is the countdown's p10/p90 spread around whichever trip time applies.
        h = ev.hours_to_limit or {}
        lo = (h.get("p50", 0) - h.get("p10", 0)) * 60 if h else 0.0
        hi = (h.get("p90", 0) - h.get("p50", 0)) * 60 if h else 0.0
        if ev.tripped:
            trip, lo, hi = now, 0.0, 0.0
        else:
            trip = res["trip_at"] if mine else ev.trips_at
        t = minutes(trip, now)
        if t is None or t > horizon_minutes:
            continue
        shut = {w for a in mine if a["type"] == "shut_in_wells" for w in a["wells"]}
        lost = [w for w in f.well_ids if w not in shut and view.value_at(w, "status") != 0]
        text = (f"{f.tank_id} is at shutdown now: its wells stay shut in until the repair at {hhmm(ds, repair_end)}"
                if ev.tripped else f"{f.tank_id} reaches shutdown at {hhmm(ds, trip)} if nothing more is done")
        events.append({"type": "SHUTDOWN_LEVEL", "asset_id": f.tank_id, "t_min": t,
                       "t_min_low": round(max(0.0, t - lo), 1), "t_min_high": round(t + hi, 1),
                       "clock": hhmm(ds, trip), "at": int(trip), "at_ts": iso(trip), "text": text,
                       "wells_lost": lost, "event_id": ev.id, "facility_id": fid})
    events.sort(key=lambda e: (e["t_min"], e["asset_id"]))
    producing = [w for w in ds.wells if view.value_at(w, "status") not in (0, None)]
    timeline = []
    for m in range(0, int(horizon_minutes) + 1, int(step_minutes)):
        ts = now + 60 * m
        tanks, off = {}, set()
        for fid, (res, repair_end, mine) in runs.items():
            f = ds.facilities[fid]
            if ts > repair_end:
                continue
            lvl = _level_at(res["points"], ts)
            tanks[f.tank_id] = None if lvl is None else round(lvl, 2)
            for a in mine:
                if a["type"] == "shut_in_wells" and ts >= now + 60 * a["start_after_min"]:
                    off.update(a["wells"])
            if res["trip_at"] is not None and ts >= res["trip_at"]:
                off.update(f.well_ids)
        timeline.append({"t_min": m, "clock": hhmm(ds, ts), "tanks": tanks,
                         "wells_online": sum(1 for w in producing if w not in off)})
    for fid, (res, repair_end, _) in runs.items():
        if repair_end < now + 60 * horizon_minutes:
            warnings.append(f"{ds.facilities[fid].tank_id} leaves the timeline at its repair ({hhmm(ds, repair_end)}): "
                            f"after that its pump runs again and the forecast does not follow it.")
    return {**header(ds, view, at), "timeline": timeline, "events": events,
            "cost": {k: round(v, 2) for k, v in cost.items()}, "cost_by_tank": by_tank, "warnings": warnings,
            "note": None if failing else f"No pump is down in {ds.id} at {hhmm(ds, now)}: every pump is keeping "
                                         f"its tank in band, so no tank reaches shutdown."}


# ---------------------------------------------------------------- POST /api/plans
def legacy_plan(ds: Dataset, view: Dataset, ev: Event, p: Any, trucks: list[str]) -> dict:
    f = ds.facilities[ev.facility]
    now, exp = view.now, _get(p, "expires_at")
    trips = bool(_get(p, "trips"))
    return {"id": ev.id, "event_id": ev.id, "facility_id": ev.facility, "system_id": ev.system,
            "asset_id": ev.asset, "tank_id": f.tank_id, "summary": _get(p, "summary"),
            "actions": plan_actions(ds, p, trucks),
            "cost": {"total_usd": _get(p, "cost"), "trucking_usd": _get(p, "truck_cost"),
                     "lost_production_usd": round((_get(p, "shut_in_cost") or 0) + (_get(p, "auto_shut_in_cost") or 0), 2),
                     "disposal_usd": 0.0},
            "keeps_running": not trips, "cannot_save": [f.tank_id] if trips else [],
            "trip_at": _get(p, "trip_at"), "trip_clock": hhmm(ds, _get(p, "trip_at")),
            "expires_at": exp, "expires_at_ts": iso(exp), "expires_clock": hhmm(ds, exp),
            "expires_min": minutes(exp, now), "eog_actions": list(_get(p, "actions", []) or []),
            "notes": list(_get(p, "notes", []) or []), "shares_with": list(_get(p, "shares_with", []) or [])}


def plans_payload(ds: Dataset, view: Dataset, at: int, svc: Optional[service.Service] = None) -> dict:
    """The exact planner's plans (mode now, joint per system), one per ongoing pump failure, cheapest first."""
    svc = _svc(svc)
    ongoing, plans = _ongoing(svc, view)
    trucks = assign_trucks(ds, ongoing, plans)
    out = [legacy_plan(ds, view, ev, plans[ev.id], trucks[ev.id]) for ev in ongoing if ev.id in plans]
    out.sort(key=lambda p: (p["cost"]["total_usd"], p["id"]))
    body = {**header(ds, view, at), "plans": out, "warnings": []}
    if not out:
        body["note"] = f"No pump is down in {ds.id} at {hhmm(ds, view.now)}, so there is nothing to plan."
    return body


# ---------------------------------------------------------------- approve
def steps_for(ds: Dataset, view: Dataset, ev: Event, p: Any, trucks: list[str]) -> list[dict]:
    """Numbered steps with asset ids and clocks: each truck's call and arrival, each shut-in, the repair."""
    f = ds.facilities[ev.facility]
    now = view.now
    repair = service.Service.repair_end_of(ds, ev)
    steps = []
    for a in plan_actions(ds, p, trucks):
        if a["type"] == "SEND_TRUCK":
            arrive = now + int(round(f.response_min * 60))
            k = int(a["value"])
            more = f", then every {f.cycle_min:g} min for {k} trips" if k > 1 else ""
            steps.append({"at": now, "asset_id": a["asset_id"], "do_by_clock": hhmm(ds, now),
                          "arrives_clock": hhmm(ds, arrive),
                          "text": f"Send {a['asset_id']} to {f.tank_id} now ({hhmm(ds, now)}). It arrives at "
                                  f"{hhmm(ds, arrive)}{more}, hauling {ds.truck_capacity_bbl:g} bbl a trip."})
        else:
            ts = now + int(round(a["at_min"] * 60))
            steps.append({"at": ts, "asset_id": a["asset_id"], "do_by_clock": hhmm(ds, ts),
                          "text": f"Shut in {a['asset_id']} at {hhmm(ds, ts)} and keep it off until the repair at "
                                  f"{hhmm(ds, repair)}."})
    if not steps:
        trip = _get(p, "trip_at")
        steps.append({"at": now, "asset_id": f.tank_id, "do_by_clock": None,
                      "text": (f"{f.tank_id} is at shutdown: its wells stay shut in until the repair at {hhmm(ds, repair)}."
                               if ev.tripped else
                               f"Let {f.tank_id} reach shutdown at {hhmm(ds, trip)}: its wells shut in by themselves "
                               f"until the repair at {hhmm(ds, repair)}." if trip is not None else
                               f"No action needed: {f.tank_id} stays under shutdown until the repair at {hhmm(ds, repair)}.")})
    steps.append({"at": repair, "asset_id": ev.asset, "do_by_clock": None,
                  "text": f"The {ev.asset} repair is due at {hhmm(ds, repair)}."})
    steps.sort(key=lambda s: s["at"])
    return [{"n": n, **s, "at_ts": iso(s["at"])} for n, s in enumerate(steps, 1)]


def approve_payload(ds: Dataset, view: Dataset, at: int, plan_id: str, by: Optional[str] = None,
                    svc: Optional[service.Service] = None) -> dict:
    """Record the approval with the Service (GET /api/eog/alerts shows it) and return the legacy
    steps and the Service's radio script. Nothing is sent to equipment: the dataset is a recording."""
    svc = _svc(svc)
    events = svc.events(view)
    ev = next((e for e in events if e.id == plan_id), None)
    if ev is None:
        raise NotFound(f"No plan {plan_id[:40]!r} in {ds.id} at {hhmm(ds, view.now)}. Call POST /api/plans first.")
    out = svc.approve(ev.id, by=by, at_raw=at)
    if ev.category == "pump_failure":
        ongoing, plans = _ongoing(svc, view)
        steps = steps_for(ds, view, ev, plans[ev.id], assign_trucks(ds, ongoing, plans)[ev.id])
    else:
        steps = [{"n": s["n"], "at": s["at"], "at_ts": s["at_ts"], "asset_id": s["asset_id"],
                  "do_by_clock": hhmm(ds, s["at"]), "text": s["text"]} for s in out["steps"]]
    return {**header(ds, view, at), "plan_id": ev.id, "event_id": ev.id, "summary": out["summary"],
            "cost": {"total_usd": out["cost"]}, "steps": steps, "radio_script": out["radio_script"],
            "approval": out["approval"],
            "note": "The dataset is a recording: approving records the plan for the shift (GET /api/eog/alerts "
                    "shows who approved it). Nothing is sent to equipment."}
