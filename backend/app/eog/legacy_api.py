"""The legacy routes in dataset mode (#153), Last Call on the dataset, and the SpacetimeDB push.

install(app) puts dataset versions of GET /api/field, GET /api/state, POST /api/forecast,
POST /api/plans, POST /api/plans/{id}/approve and POST /api/lastcall in front of main.py's own.
Each is a DatasetRoute: it matches only while dataset mode is on (FREEBOARD_EOG_DIR is set and
names a folder with at least one dataset), and otherwise lets the request fall through to the
legacy route, which then behaves exactly as before. It also adds GET and POST /api/eog/lastcall.

SpacetimeDB: with SPACETIME_DB set, a daemon thread pushes legacy.state_payload through
spacetime_sync.push_state when a dataset is activated or the clock cursor moves, at most once
every PUSH_S seconds. It starts after startup and never blocks it; a push that fails is logged
(never the token) and retried on the next change.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional, Union

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.routing import Match

from .. import spacetime_sync
from ..auth import OPERATOR, Caller, require_role
from . import dataset as dsmod
from . import service
from .legacy import (assign_trucks, field_payload, forecast, header, hhmm, legacy_plan, minutes,
                     plans_payload, approve_payload, state_payload, steps_for)
from .model import iso, money
from .service import ServiceError, jsonable

log = logging.getLogger("freeboard.eog.legacy")
PUSH_S = 2.0


def on(svc: Optional[service.Service] = None) -> bool:
    """Dataset mode is on: FREEBOARD_EOG_DIR is set and the Service has a dataset to show (it
    picks the default the first time). Cheap after that first call; parses nothing."""
    if not os.environ.get(dsmod.ENV_ROOT, "").strip():
        return False
    try:
        (svc or service.SERVICE).require_enabled()
        return True
    except ServiceError:
        return False


class DatasetRoute(APIRoute):
    """Matches only in dataset mode, so the legacy route behind it answers otherwise."""

    def matches(self, scope):
        match, child = super().matches(scope)
        if match == Match.NONE or not on():
            return Match.NONE, {}
        return match, child


def _run(fn, *args, **kwargs) -> Any:
    try:
        return jsonable(fn(*args, **kwargs))
    except ServiceError as e:
        raise HTTPException(e.status, e.sentence) from None


def _view() -> tuple:
    svc = service.SERVICE
    try:
        ds, view, at = svc.view()
    except ServiceError as e:
        raise HTTPException(e.status, e.sentence) from None
    return svc, ds, view, at


# ---------------------------------------------------------------- Last Call
def lastcall_payload(ds, view, at: int, top_n: int = 3, svc: Optional[service.Service] = None) -> dict:
    """POST /api/lastcall's shape from the exact planner: each failing pump's plan and when it
    expires (Plan.expires_at), most urgent first (the alerts order), the price of waiting on the
    most urgent one (plan.wait_curve rows as plan.py returns them, plus the legacy names), and
    the point after which no plan keeps that tank below shutdown."""
    svc = svc or service.SERVICE
    now = view.now
    order, plans = svc.ordered(view)
    failing = [e for e in order if e.category == "pump_failure"]
    out = {**header(ds, view, at), "status": "CALM", "headline": "", "shutdown": None, "point_of_no_return": None,
           "do_nothing_usd": 0.0, "options": [], "ladder": [], "cliffs": [], "curve": [], "curves": {}, "assets": {}}
    if not failing:
        out["headline"] = f"No pump is down at {hhmm(ds, now)}, so no tank is heading for shutdown."
        return out
    ongoing = svc.ongoing_failures(svc.events(view))
    trucks = assign_trucks(ds, ongoing, plans)
    out["do_nothing_usd"] = forecast(ds, view, at, [], svc=svc)["cost"]["total_usd"]
    for ev in failing[:max(1, int(top_n))]:
        p = plans.get(ev.id)
        if p is None:
            continue
        lp = legacy_plan(ds, view, ev, p, trucks[ev.id])
        steps = steps_for(ds, view, ev, p, trucks[ev.id])
        for s in steps:
            if s["asset_id"] in trucks[ev.id] and lp["expires_clock"]:
                s["do_by_clock"] = lp["expires_clock"]
        out["options"].append({
            "plan_id": ev.id, "event_id": ev.id, "asset_id": ev.asset, "tank_id": lp["tank_id"],
            "summary": lp["summary"], "cost": {"total_usd": lp["cost"]["total_usd"]},
            "expires_min": lp["expires_min"], "expires_clock": lp["expires_clock"], "expires_at": lp["expires_at"],
            "expires_at_ts": lp["expires_at_ts"], "expires_min_hot": None, "robust": None,
            "keeps_running": lp["keeps_running"], "actions": lp["actions"],
            "steps": [{k: s[k] for k in ("n", "asset_id", "do_by_clock", "text")} for s in steps]})
        ids = sorted({a["asset_id"] for a in lp["actions"]})
        out["ladder"].append({"plan_id": ev.id, "from_min": 0, "expires_min": lp["expires_min"],
                              "usd_at_start": lp["cost"]["total_usd"], "asset_ids": ids})
        for aid in ids:
            out["assets"].setdefault(aid, {"useful_until_min": lp["expires_min"],
                                           "useful_until_clock": lp["expires_clock"], "plan_id": ev.id})
        rows = []
        for r in svc.wait_curve(view, ongoing, ev):
            keeps = not str(r["summary"]).startswith("none (")
            rows.append({**r, "start_min": r["delay_min"], "total_usd": r["cost"], "clock": hhmm(ds, r["start_ts"]),
                         "plan_id": ev.id, "keeps_running": keeps})
        out["curves"][ev.id] = rows
    tripping = [e for e in failing if e.tripped or e.trips_at is not None]
    if not tripping:
        out["headline"] = (f"{len(failing)} pump{'s' if len(failing) > 1 else ''} down, but every tank stays under "
                           f"shutdown until its repair. Nothing to race.")
        return out
    out["status"] = "INCIDENT"
    focus = tripping[0]
    fac = ds.facilities[focus.facility]
    trip = now if focus.tripped else focus.trips_at
    out["shutdown"] = {"asset_id": fac.tank_id, "t_min": minutes(trip, now), "clock": hhmm(ds, trip),
                       "at": trip, "at_ts": iso(trip), "event_id": focus.id}
    curve = out["curves"].get(focus.id, [])
    out["curve"] = curve
    for a, b in zip(curve, curve[1:]):
        if b["cost"] - a["cost"] > 1.0 and len(out["cliffs"]) < 5:
            out["cliffs"].append({"t_min": b["delay_min"], "clock": b["clock"], "from_usd": a["cost"],
                                  "to_usd": b["cost"], "event_id": focus.id,
                                  "text": f"Start after {a['clock']} and {focus.id} costs {money(b['cost'])} "
                                          f"instead of {money(a['cost'])}."})
    free = svc.trucks_free(ds, focus, ongoing, plans)
    last = max((p.expires_at for p in svc.alternatives(view, focus, free)
                if not p.trips and p.expires_at is not None), default=None)
    if last is not None:
        out["point_of_no_return"] = {"t_min": minutes(last, now), "clock": hhmm(ds, last), "at": last,
                                     "at_ts": iso(last), "event_id": focus.id,
                                     "text": f"After {hhmm(ds, last)} no plan keeps {fac.tank_id} below shutdown."}
    opt = next((o for o in out["options"] if o["event_id"] == focus.id), None)
    head = (f"{focus.asset} is down: {fac.tank_id} is at shutdown now." if focus.tripped else
            f"{focus.asset} is down: {fac.tank_id} reaches shutdown at {hhmm(ds, trip)} if nobody acts.")
    if opt and opt["expires_at"] is not None:
        later = next((r for r in curve if r["start_ts"] > opt["expires_at"]), None)
        head += f" Its plan ({opt['summary']}) only works if started by {opt['expires_clock']}."
        if later is not None and later["cost"] > opt["cost"]["total_usd"] + 1.0:
            head += (f" Wait past it and the bill goes from {money(opt['cost']['total_usd'])} "
                     f"to {money(later['cost'])}.")
    if len(failing) > 1:
        head += f" {len(failing) - 1} more pump{'s' if len(failing) > 2 else ''} down."
    out["headline"] = head
    return out


# ---------------------------------------------------------------- request bodies (as main.py's)
class Action(BaseModel):
    type: str
    asset_id: str
    value: Optional[Union[float, str]] = None
    target_id: Optional[str] = None
    at_min: float = Field(0, ge=0)


class ForecastRequest(BaseModel):
    horizon_minutes: int = Field(360, ge=5, le=1440)
    step_minutes: int = Field(5, ge=1, le=60)
    actions: list[Action] = []


class PlansRequest(BaseModel):
    horizon_minutes: int = Field(360, ge=5, le=1440)
    top_n: int = Field(3, ge=1, le=10)


class LastCallRequest(BaseModel):
    horizon_minutes: int = 360
    step_minutes: int = 5
    top_n: int = Field(3, ge=1, le=10)


# ---------------------------------------------------------------- the routes
router = APIRouter(route_class=DatasetRoute)
eog_router = APIRouter(prefix="/api/eog")


@router.get("/api/field")
def get_field() -> dict:
    svc, ds, view, at = _view()
    return svc.cache.get(("legacy_field", ds.id, str(ds.folder), id(ds.times)),
                         lambda: _run(field_payload, ds), keep=ds.times)


@router.get("/api/state")
def get_state() -> dict:
    svc, ds, view, at = _view()
    return _run(state_payload, ds, view, at, svc)


@router.post("/api/forecast")
def post_forecast(req: ForecastRequest = ForecastRequest()) -> dict:
    svc, ds, view, at = _view()
    actions = [a.model_dump() for a in req.actions]
    return _run(forecast, ds, view, at, actions, req.horizon_minutes, req.step_minutes, svc)


@router.post("/api/plans")
def post_plans(req: PlansRequest = PlansRequest()) -> dict:
    svc, ds, view, at = _view()
    return _run(plans_payload, ds, view, at, svc)


@router.post("/api/plans/{plan_id}/approve")
def post_approve(plan_id: str, request: Request, caller: Caller = Depends(require_role(OPERATOR))) -> dict:
    svc, ds, view, at = _view()
    out = _run(approve_payload, ds, view, at, plan_id, caller.decided_by("console") or "console", svc)
    audit = getattr(request.app.state, "audit", None)
    if audit is not None:
        audit.append("approved", source=caller.source(), role=caller.role, plan_id=out["plan_id"],
                     dataset=ds.id, proposals=[])
    threading.Thread(target=_push_plan, args=(out,), daemon=True).start()
    return out


@router.post("/api/lastcall")
def post_lastcall(req: LastCallRequest = LastCallRequest()) -> dict:
    svc, ds, view, at = _view()
    return _run(lastcall_payload, ds, view, at, req.top_n, svc)


def _eog_lastcall(at: Optional[str], top_n: int) -> dict:
    svc = service.SERVICE
    try:
        ds, view, cut = svc.view(at)
    except ServiceError as e:
        raise HTTPException(e.status, e.sentence) from None
    return _run(lastcall_payload, ds, view, cut, top_n, svc)


@eog_router.get("/lastcall")
def get_eog_lastcall(at: Optional[str] = None, top_n: int = 3) -> dict:
    return _eog_lastcall(at, max(1, min(10, top_n)))


@eog_router.post("/lastcall")
def post_eog_lastcall(req: LastCallRequest = LastCallRequest(), at: Optional[str] = None) -> dict:
    return _eog_lastcall(at, req.top_n)


def _push_plan(out: dict) -> None:
    if not spacetime_sync.enabled():
        return
    try:
        spacetime_sync.call_reducer("set_plan", [out["plan_id"], json.dumps(out["steps"])])
    except Exception as e:  # noqa: BLE001 - SpacetimeDB must never break the API
        spacetime_sync.status.update(last_push_ok=False, last_error=str(e))
        log.warning("SpacetimeDB set_plan failed: %s", type(e).__name__)


# ---------------------------------------------------------------- SpacetimeDB push
class Pusher:
    """Pushes the dataset's legacy state into SpacetimeDB when it changes, at most every PUSH_S."""

    def __init__(self, period_s: float = PUSH_S):
        self.period_s = period_s
        self._last: Optional[tuple] = None
        self._thread: Optional[threading.Thread] = None

    def push_once(self) -> bool:
        """One check: push if dataset mode is on and the dataset, cursor or approvals moved."""
        if not on() or not spacetime_sync.enabled():
            return False
        svc = service.SERVICE
        sig = svc.signature()
        if sig == self._last:
            return False
        ds, view, at = svc.view()
        if spacetime_sync.push_state(jsonable(state_payload(ds, view, at, svc))):
            self._last = sig
            return True
        return False

    def _loop(self) -> None:
        while True:
            try:
                self.push_once()
            except Exception as e:  # noqa: BLE001 - the push must never take the server down
                log.warning("Dataset mode: SpacetimeDB push failed: %s", type(e).__name__)
            time.sleep(self.period_s)

    def start(self) -> None:
        if self._thread is None and spacetime_sync.enabled():
            self._thread = threading.Thread(target=self._loop, name="eog-spacetime-push", daemon=True)
            self._thread.start()


PUSHER = Pusher()


def install(app: FastAPI) -> None:
    """Mount the dataset routes in front of the legacy ones, add /api/eog/lastcall, and start
    the SpacetimeDB push (only with SPACETIME_DB set)."""
    n = len(app.router.routes)
    app.include_router(router)
    mine = app.router.routes[n:]
    del app.router.routes[n:]
    app.router.routes[0:0] = mine
    app.include_router(eog_router)
    PUSHER.start()
