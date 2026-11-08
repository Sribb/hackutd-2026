"""gaia on a dataset: jarvis.py's read tools answered from the active sponsor dataset.

With a dataset on screen, gaia's tools in backend/app/jarvis.py (list_alerts, explain_asset,
get_forecast, get_plans) would read the live demo field and answer about the wrong field. One
guarded line at the top of Jarvis.run_tool hands those tools (and propose_action) here while
active() is true. Each answer has the same top-level keys as jarvis.py's own, filled from the
active dataset as of the service's clock cursor (service.SERVICE.view): sponsor ids, the dataset's
clock in UTC, and a "source" naming the folder and the cut. Every number is the service's cached
detect and plan output, the same /api/eog and /api/dataset serve.

active() is true while dataset mode serves a dataset: once one has been activated in this process
by either route (POST /api/eog/datasets/activate or POST /api/dataset/load, which both set
Service.activated), or while the console's legacy routes serve one (eog/legacy_api.py, #153).
With no dataset ever activated and no legacy routes serving one, gaia reads the live field
exactly as before.

The dataset is a recording, so propose_action never makes an equipment action here: it answers
with a sentence saying a plan can be approved for its steps and a radio script instead.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Optional

from . import service
from .dataset import Dataset
from .model import Event

log = logging.getLogger("freeboard.eog")

TOOLS = ("list_alerts", "explain_asset", "get_forecast", "get_plans", "propose_action")
RECORDING = ("The dataset on screen is a recording, so gaia can't send equipment commands for it. "
             "A plan can be approved for its numbered steps and a radio script instead: "
             "POST /api/eog/plans/{event_id}/approve.")

def _legacy_on() -> bool:
    """The console's legacy routes serve the dataset (eog/legacy_api.py, #153, once it is mounted)."""
    mod = sys.modules.get(f"{__package__}.legacy_api")
    on = getattr(mod, "on", None)
    return bool(on()) if callable(on) else False


def active() -> bool:
    """True while dataset mode serves a dataset to someone: one was activated in this process,
    by any route (Service.activated: POST /api/eog/datasets/activate or POST /api/dataset/load),
    or the console's legacy routes serve one. False while dataset mode is off. Never raises."""
    try:
        svc = service.SERVICE
        if getattr(svc, "activated", None) is None and not _legacy_on():
            return False
        svc.require_enabled()
    except Exception:  # noqa: BLE001 - dataset mode off or broken: gaia answers from the live field
        return False
    return True


# ---------- shared pieces ----------

def _clock(ts: Optional[int]) -> Optional[str]:
    """'2026-10-10 09:00 UTC'."""
    if ts is None:
        return None
    return service._utc(ts).strftime("%Y-%m-%d %H:%M UTC")


def _hm(ts: Optional[int]) -> Optional[str]:
    return None if ts is None else service._utc(ts).strftime("%H:%M")


def _view() -> tuple[Dataset, Dataset, int]:
    return service.SERVICE.view(None)


def _head(ds: Dataset, view: Dataset, at: int) -> dict:
    return {"dataset": ds.id, "now": view.now, "clock": _hm(view.now), "as_of": _clock(view.now),
            "source": f"dataset {ds.id}, cut at {_clock(at)} (the recording's own clock, UTC)"}


def _kinds(ds: Dataset) -> dict[str, tuple[str, Any]]:
    out: dict[str, tuple[str, Any]] = {}
    for s in ds.systems.values():
        out[s.disposal_id] = ("disposal", s)
    for f in ds.facilities.values():
        out[f.id] = ("facility", f)
        out[f.tank_id] = ("tank", f)
        out[f.pump_id] = ("pump", f)
        out[f.pipeline_id] = ("pipeline", f)
        for w in f.wells:
            out[w.id] = ("well", (f, w))
    return out


def _blind_tanks(ds: Dataset, view: Dataset) -> list[str]:
    """Tanks with no level reading at the view's now: unknown, never safe."""
    return [f.tank_id for f in ds.facilities.values() if view.value_at(f.tank_id, "level_pct") is None]


def _priority(ev: Event) -> str:
    if ev.category == "pump_failure":
        h = service.p50(ev)
        return "P1" if ev.tripped or (h is not None and h <= 1.0) else "P2"
    return "P2" if ev.category == "pipeline_leak" else "P3"


def _ongoing(ds: Dataset, view: Dataset) -> tuple[list[Event], dict]:
    return service.SERVICE.ordered(view)


def _row(ds: Dataset, view: Dataset, ev: Event, plans: dict) -> dict:
    svc = service.SERVICE
    p = plans.get(ev.id)
    fac = ds.facilities.get(ev.facility)
    return {"id": ev.id, "asset_id": ev.asset, "name": ev.asset, "kind": ev.category.upper(),
            "severity": "ALARM" if ev.category == "pump_failure" else "WARNING", "priority": _priority(ev),
            "message": svc._title(ds, view, ev), "raised_at": _hm(ev.onset), "raised_at_utc": _clock(ev.onset),
            # the message already says when the tank trips; jarvis's forecast sentence would use the live clock
            "consequence": None, "consequence_clock": None, "minutes_to_consequence": None,
            "minutes_from_now": None if ev.trips_at is None else int(round((ev.trips_at - view.now) / 60)),
            "usd_at_risk": 0 if p is None else round(float(p.cost), 2), "forecast_at": _clock(view.now),
            "reading": None, "root_id": None, "acked": False, "shelved": False, "shelved_until": None,
            "shelve_reason": None,
            "facility_id": ev.facility, "system_id": ev.system, "tank_id": fac.tank_id if fac else None,
            "evidence": list(ev.evidence or []), "hours_to_limit": ev.hours_to_limit,
            "trips_at_utc": _clock(ev.trips_at), "plan": None if p is None else p.summary}


# ---------- the tools ----------

def list_alerts() -> dict:
    ds, view, at = _view()
    order, plans = _ongoing(ds, view)
    groups = [{"root": _row(ds, view, ev, plans), "related": 0, "related_count": 0, "related_ids": [],
               "shelved_root": None} for ev in order]
    return {**_head(ds, view, at), "open": len(order), "groups": groups, "unknown_tanks": _blind_tanks(ds, view)}


def explain_asset(asset_id: str) -> dict:
    ds, view, at = _view()
    aid = str(asset_id or "").upper()
    kinds = _kinds(ds)
    if aid not in kinds:
        return {"error": f"Unknown asset {aid}."}
    kind, ref = kinds[aid]
    order, plans = _ongoing(ds, view)
    mine = [ev for ev in order if ev.asset == aid or ev.facility == aid
            or (kind in ("tank", "well") and ev.facility == (ref[0].id if kind == "well" else ref.id))]
    status = "OK"
    if any(ev.category == "pump_failure" for ev in mine):
        status = "ALARM"
    elif mine:
        status = "WARNING"
    metrics = {m: view.value_at(aid, m) for m in ds.metrics(aid)}
    live: dict = {"status": status, **metrics}
    design: dict = {}
    if kind == "tank":
        f = ref
        design = {"capacity_bbl": f.capacity_bbl, "shutdown_pct": ds.shutdown_pct, "warning_pct": ds.warning_pct,
                  "facility_id": f.id, "pump_id": f.pump_id}
        lvl = metrics.get("level_pct")
        if lvl is None:
            live["status"] = "UNKNOWN"
        elif status == "OK" and lvl >= ds.warning_pct:
            live["status"] = "WARNING"
    elif kind == "pump":
        f = ref
        design = {"max_flow_bpd": f.max_flow_bpd, "start_level_pct": f.start_level_pct,
                  "stop_level_pct": f.stop_level_pct, "tank_id": f.tank_id, "facility_id": f.id}
        flow = metrics.get("flow_bpd")
        live["running"] = metrics.get("status") == 1
        live["flow_bbl_hr"] = None if flow is None else round(flow / 24.0, 1)
    elif kind == "well":
        f, w = ref
        design = {"baseline_water_bpd": w.baseline_water_bpd, "oil_bpd": w.oil_bpd, "gas_mcfd": w.gas_mcfd,
                  "facility_id": f.id, "lost_usd_per_hour_if_shut_in": round(ds.revenue_per_hour(w.id), 2)}
        water = metrics.get("water_bpd")
        live["flow_bbl_hr"] = None if water is None else round(water / 24.0, 1)
    elif kind == "pipeline":
        f = ref
        design = {"max_bpd": f.pipeline_max_bpd, "max_pressure_psi": f.pipeline_max_pressure_psi, "facility_id": f.id}
        flow = metrics.get("receive_flow_bpd")
        live["flow_bbl_hr"] = None if flow is None else round(flow / 24.0, 1)
    elif kind == "facility":
        f = ref
        design = {"tank_id": f.tank_id, "pump_id": f.pump_id, "pipeline_id": f.pipeline_id,
                  "wells": list(f.well_ids), "system_id": f.system}
    else:
        s = ref
        design = {"system_id": s.id, "pressure_psi": s.disposal_pressure_psi}
    if all(v is None for v in metrics.values()) and kind not in ("facility", "disposal"):
        live["status"] = "UNKNOWN"
    out = {**_head(ds, view, at), "asset_id": aid, "name": aid, "kind": kind, "live": live,
           "reading": {"as_of": _clock(view.now), "metrics": metrics} if metrics else None, "design": design,
           "alerts": [{"id": ev.id, "kind": ev.category.upper(), "severity": "ALARM" if ev.category == "pump_failure"
                       else "WARNING", "priority": _priority(ev), "root_id": None,
                       "message": service.SERVICE._title(ds, view, ev)} for ev in mine]}
    if live["status"] == "UNKNOWN":
        out["say"] = (f"{aid} has no reading at {_clock(view.now)} in this dataset, so gaia can't say it is safe. "
                      f"Treat it as at risk and check in person.")
    return out


def get_forecast(actions: Any) -> dict:
    """If nobody acts: each ongoing pump failure's tank countdown (plan.countdown) and the cost of
    the automatic shut-ins. A recording can't replay hypothetical commands, so actions only warn."""
    from .dataset_api import do_nothing_cost

    ds, view, at = _view()
    order, plans = _ongoing(ds, view)
    events = service.SERVICE.events(view)
    ongoing = service.SERVICE.ongoing_failures(events)
    out_events, lost = [], 0.0
    for ev in order:
        if ev.category != "pump_failure":
            continue
        fac = ds.facilities.get(ev.facility)
        tank = fac.tank_id if fac else ev.facility
        repair_end = service.SERVICE.repair_end_of(ds, ev)
        if ev.tripped:
            text = f"{tank} is at shutdown; {ev.facility}'s wells stay shut in until the repair ends at {_clock(repair_end)}."
            out_events.append({"type": "SHUTDOWN_LEVEL", "asset_id": tank, "t_min": 0, "text": text})
        elif ev.trips_at is not None:
            text = (f"{tank} reaches shutdown at {_clock(ev.trips_at)} and {ev.facility}'s wells shut in "
                    f"until the repair ends at {_clock(repair_end)}.")
            out_events.append({"type": "SHUTDOWN_LEVEL", "asset_id": tank,
                               "t_min": round((ev.trips_at - view.now) / 60.0, 1), "text": text})
        cost = do_nothing_cost(ds, view, ev, ongoing, plans)
        lost += cost or 0.0
    warnings = []
    if actions:
        warnings.append("This dataset is a recording, so the actions asked about were not replayed: the forecast "
                        "is the do-nothing case. GET /api/eog/plans has every plan and POST /api/eog/whatif "
                        "replays a tank with sponsor actions.")
    blind = _blind_tanks(ds, view)
    warnings += [f"{t} has no level reading at {_clock(view.now)}." for t in blind]
    return {**_head(ds, view, at), "events": out_events[:12],
            "cost": {"total_usd": round(lost, 2), "trucking_usd": 0.0, "lost_production_usd": round(lost, 2),
                     "disposal_usd": 0.0},
            "warnings": warnings, "unknown_tanks": blind}


def _with_asset(a: dict, ev: Event, tank: str) -> dict:
    """Sponsor action plus an asset_id, which jarvis.py's plan sentence focuses the camera on."""
    a = dict(a)
    if a.get("type") == "shut_in_wells" and a.get("wells"):
        a.setdefault("asset_id", a["wells"][0])
    else:
        a.setdefault("asset_id", a.get("facility_id") or tank)
    return a


def get_plans() -> dict:
    ds, view, at = _view()
    order, plans = _ongoing(ds, view)
    rows = []
    for ev in order:
        p = plans.get(ev.id)
        if ev.category != "pump_failure" or p is None:
            continue
        fac = ds.facilities.get(ev.facility)
        tank = fac.tank_id if fac else ev.facility
        acts = [_with_asset(a, ev, tank) for a in p.actions or [] if a.get("type") != "none"]
        rows.append({"id": ev.id, "event_id": ev.id, "facility_id": ev.facility,
                     "summary": f"{ev.asset} failure at {ev.facility}: {p.summary}.",
                     "actions": acts, "total_usd": round(float(p.cost), 2), "keeps_running": not p.trips,
                     "robust": None, "cannot_save": [tank] if p.trips else [],
                     "expires_at_utc": _clock(p.expires_at)})
    return {**_head(ds, view, at), "unknown_tanks": _blind_tanks(ds, view), "plans": rows}


async def run_tool(tool: str, args: dict, ctx: dict) -> Any:
    """jarvis.py's tool on the active dataset (TOOLS). Blocking work runs off the event loop."""
    args = args or {}
    try:
        if tool == "list_alerts":
            return await asyncio.to_thread(list_alerts)
        if tool == "explain_asset":
            return await asyncio.to_thread(explain_asset, args.get("asset_id", ""))
        if tool == "get_forecast":
            return await asyncio.to_thread(get_forecast, args.get("actions") or [])
        if tool == "get_plans":
            return await asyncio.to_thread(get_plans)
        if tool == "propose_action":
            return {"error": RECORDING}
    except service.ServiceError as e:
        return {"error": e.sentence}
    return {"error": f"Unknown tool {tool}."}
