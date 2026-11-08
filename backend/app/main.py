"""Freeboard backend. Run from the repo root:

    uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Optional, Union

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import spacetime_sync
from .forecast import build_plant, forecast
from .planner import approve, make_plans
from .scenario import field_payload, load_scenario, state_payload

log = logging.getLogger("freeboard")
SCENARIO = load_scenario()
_plans: dict[str, dict] = {}   # plan id -> plan, from the latest /api/plans calls


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Mirror the starting state into SpacetimeDB without blocking startup.
    threading.Thread(target=spacetime_sync.push_state,
                     args=(state_payload(SCENARIO),), daemon=True).start()
    live = getattr(_app.state, "live", None)  # live layer (#38), mounted at the bottom
    if live:
        live.start()
    yield
    if live:
        await live.stop()


app = FastAPI(title="Freeboard backend", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
    """Any bug comes back as JSON the console can show, never a bare crash page."""
    log.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": f"Internal error: {type(exc).__name__}: {exc}"})

# The web console runs on a local dev server (Vite defaults to 5173).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/api/field")
def get_field() -> dict:
    return field_payload(SCENARIO)


@app.get("/api/state")
def get_state() -> dict:
    return state_payload(SCENARIO)


@app.post("/api/forecast")
def post_forecast(req: ForecastRequest = ForecastRequest()) -> dict:
    actions = [a.model_dump(exclude_none=True) for a in req.actions]
    return forecast(SCENARIO, actions, req.horizon_minutes, req.step_minutes)


@app.post("/api/plans")
def post_plans(req: PlansRequest = PlansRequest()) -> dict:
    out = make_plans(SCENARIO, req.horizon_minutes, req.top_n)
    for p in out["plans"]:
        _plans[p["id"]] = p
    return out


@app.post("/api/plans/{plan_id}/approve")
def post_approve(plan_id: str) -> dict:
    plan = _plans.get(plan_id)
    if plan is None:  # e.g. the server restarted; plan ids are deterministic
        for p in make_plans(SCENARIO)["plans"]:
            _plans[p["id"]] = p
        plan = _plans.get(plan_id)
    if plan is None:
        raise HTTPException(404, f"No plan {plan_id}. Call POST /api/plans first.")
    out = approve(SCENARIO, plan)
    threading.Thread(target=_push_plan, args=(out,), daemon=True).start()
    return out


def _push_plan(out: dict) -> None:
    if not spacetime_sync.enabled():
        return
    try:
        spacetime_sync.call_reducer("set_plan", [out["plan_id"], json.dumps(out["steps"])])
    except Exception as e:  # noqa: BLE001 - SpacetimeDB must never break the API
        spacetime_sync.status.update(last_push_ok=False, last_error=str(e))
        log.warning("SpacetimeDB set_plan failed: %s", e)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "scenario": SCENARIO.get("_source"), "spacetime": spacetime_sync.status,
            "lastcall": LASTCALL, "ingest": INGEST}


@app.post("/api/spacetime/sync")
def spacetime_sync_now() -> dict:
    """Re-push the current state into SpacetimeDB, e.g. after republishing the module."""
    ok = spacetime_sync.push_state(state_payload(SCENARIO))
    return {"ok": ok, "spacetime": spacetime_sync.status}


# ---------- Last Call (lastcall/, owned by @ronakkasaram) ----------

def lastcall_scenario() -> dict:
    """The flat field shape lastcall/core.py reads: field data plus current readings."""
    f, st = SCENARIO["field"], state_payload(SCENARIO)
    assets = st["assets"]
    return {
        "clock": st["clock"],
        "tanks": [{**t, "level_pct": assets[t["id"]].get("level_pct")} for t in f["tanks"]],
        "pumps": [{**p, "status": assets[p["id"]]["status"],
                   **({"reason": assets[p["id"]]["reason"]} if "reason" in assets[p["id"]] else {})}
                  for p in f["pumps"]],
        "valves": [{**v, "position": assets[v["id"]].get("position", "CLOSED")} for v in f["valves"]],
        "wells": f["wells"],
        "disposal_wells": f["disposal_wells"],
        "trucks": f["trucks"],
        "lead_min": f.get("lead_min", {}),
        "costs": f.get("costs", {}),
    }


try:
    from lastcall.router import router as lastcall_router, set_source

    _PLANT = build_plant(SCENARIO)  # the scenario is fixed, so build it once for Last Call's many runs
    set_source(get_scenario=lastcall_scenario,
               forecast_fn=lambda actions, inflow_scale: forecast(
                   SCENARIO, actions, inflow_scale=inflow_scale, with_range=False, plant=_PLANT))
    app.include_router(lastcall_router)
    LASTCALL = "mounted"
except ImportError:
    LASTCALL = "not installed (lastcall/ is not on this branch yet)"


# ---------- Ingest (ingest/, owned by @teameuphoriainv-dot) ----------
# POST /api/ingest turns an uploaded site plan into blueprint.json. Images and PDFs
# need ANTHROPIC_API_KEY in the server environment. Never put it in the repo or the browser.

try:
    from ingest.router import router as ingest_router

    app.include_router(ingest_router)
    INGEST = "mounted" if os.environ.get("ANTHROPIC_API_KEY") else \
        "mounted (JSON and CSV only: ANTHROPIC_API_KEY is not set, so images and PDFs will fail)"
except ImportError as e:
    INGEST = f"not installed ({e})"
except RuntimeError as e:  # FastAPI raises this when python-multipart is missing
    INGEST = f"not mounted ({e})"

# ---------- Live layer: connectors, alerts, actions, Jarvis, /ws (#38, @vinnycodes67) ----------

try:
    from .live_api import install as install_live

    LIVE = install_live(app, SCENARIO)
except ImportError as e:
    LIVE = None
    log.warning("Live layer not mounted: %s", e)
