"""Freeboard backend. Run from the repo root:

    uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

Everything reads the active field (backend/app/fields.py). At startup that's the
scenario file. POST /api/blueprints/{id}/activate switches it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Optional, Union

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import actuators, blueprints, commands, fields, spacetime_sync
from .forecast import forecast
from .planner import approve, make_plans
from .scenario import field_payload, load_scenario

log = logging.getLogger("freeboard")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_plans: dict[str, dict] = {}   # plan id -> plan, from the latest /api/plans calls

fields.activate(load_scenario())


def _push(snap) -> None:
    """Mirror every field change into SpacetimeDB, off the request thread."""
    if spacetime_sync.enabled():
        threading.Thread(target=spacetime_sync.push_state, args=(fields.live_state(snap),), daemon=True).start()


fields.on_change(_push)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _push(fields.snapshot())
    commands.start_worker(actuators.get())
    yield


app = FastAPI(title="Freeboard backend", lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
    """Any bug comes back as JSON the console can show, never a bare crash page."""
    log.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": f"Internal error: {type(exc).__name__}: {exc}"})


@app.exception_handler(commands.CommandError)
async def command_error(_request: Request, exc: commands.CommandError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


@app.exception_handler(blueprints.BlueprintError)
async def blueprint_error(_request: Request, exc: blueprints.BlueprintError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": exc.message, **exc.detail})


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


class CommandRequest(BaseModel):
    action: Action
    reason: str = ""


class RejectRequest(BaseModel):
    reason: str = ""


# ---------- field, state, forecast, plans ----------

@app.get("/api/field")
def get_field() -> dict:
    return field_payload(fields.snapshot().scenario)


@app.get("/api/state")
def get_state() -> dict:
    return fields.live_state()


@app.post("/api/forecast")
def post_forecast(req: ForecastRequest = ForecastRequest()) -> dict:
    """The future from now. Actions already carried out are always included."""
    snap = fields.snapshot()
    actions = list(snap.applied) + [a.model_dump(exclude_none=True) for a in req.actions]
    return forecast(snap.scenario, actions, req.horizon_minutes, req.step_minutes, plant=snap.plant)


@app.post("/api/plans")
def post_plans(req: PlansRequest = PlansRequest()) -> dict:
    snap = fields.snapshot()
    out = make_plans(snap.scenario, req.horizon_minutes, req.top_n, base_actions=list(snap.applied),
                     plant=snap.plant)
    for p in out["plans"]:
        _plans[p["id"]] = p
    return out


@app.post("/api/plans/{plan_id}/approve")
def post_approve(plan_id: str, x_operator: Optional[str] = Header(None)) -> dict:
    """Turns a plan into numbered steps, and proposes one command per step.
    Each command still needs its own human approval (POST /api/commands/{id}/approve)."""
    snap = fields.snapshot()
    plan = _plans.get(plan_id)
    if plan is None:  # e.g. the server restarted; plan ids are deterministic
        for p in make_plans(snap.scenario, base_actions=list(snap.applied), plant=snap.plant)["plans"]:
            _plans[p["id"]] = p
        plan = _plans.get(plan_id)
    if plan is None:
        raise HTTPException(404, f"No plan {plan_id}. Call POST /api/plans first.")
    out = approve(snap.scenario, plan)
    by = f"plan:{plan_id}" + (f" via {x_operator}" if x_operator else "")
    step_for = {s["asset_id"]: s["n"] for s in out["steps"]}
    ordered = sorted(plan["actions"], key=lambda a: step_for.get(a["asset_id"], 99))
    out["command_ids"] = [commands.propose({k: v for k, v in a.items() if k != "at_min"}, by,
                                           f"Step {step_for.get(a['asset_id'])} of {plan_id}")["id"]
                          for a in ordered]
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


# ---------- commands (the only path to a machine) ----------

@app.post("/api/commands")
def post_command(req: CommandRequest, x_operator: Optional[str] = Header(None)) -> dict:
    """Propose. Anyone (agent or human) can propose. Nothing runs until a human approves."""
    by = x_operator or "agent"
    return commands.propose(req.action.model_dump(exclude_none=True, exclude={"at_min"}), by, req.reason)


@app.get("/api/commands")
def get_commands() -> dict:
    return {"commands": commands.list_commands(), **commands.status()}


@app.post("/api/commands/halt")
def post_halt(x_operator: Optional[str] = Header(None)) -> dict:
    return commands.halt(x_operator or "unknown")


@app.post("/api/commands/resume")
def post_resume(x_operator: Optional[str] = Header(None)) -> dict:
    if not x_operator:
        raise HTTPException(403, "Only a named human operator can resume. Send the X-Operator header.")
    return commands.resume(x_operator)


@app.post("/api/commands/{cid}/approve")
def post_command_approve(cid: str, x_operator: Optional[str] = Header(None)) -> dict:
    return commands.approve(cid, x_operator or "")


@app.post("/api/commands/{cid}/reject")
def post_command_reject(cid: str, req: RejectRequest = RejectRequest(),
                        x_operator: Optional[str] = Header(None)) -> dict:
    return commands.reject(cid, x_operator or "unknown", req.reason)


@app.get("/api/audit")
def get_audit() -> dict:
    return {"audit": commands.audit()}


# ---------- blueprints ----------

@app.post("/api/blueprints")
async def post_blueprint(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Uploads must be under 20 MB.")
    from fastapi.concurrency import run_in_threadpool
    return await run_in_threadpool(blueprints.create, data, file.filename or "upload", file.content_type)


@app.get("/api/blueprints")
def get_blueprints() -> dict:
    return {"blueprints": blueprints.list_all(), "active": fields.snapshot().blueprint_id}


@app.get("/api/blueprints/{bid}")
def get_blueprint(bid: str) -> dict:
    return blueprints.get(bid)


@app.put("/api/blueprints/{bid}")
def put_blueprint(bid: str, blueprint: dict) -> dict:
    return blueprints.update(bid, blueprint)


@app.post("/api/blueprints/{bid}/build")
def post_build(bid: str) -> dict:
    return blueprints.build(bid)


@app.get("/api/blueprints/{bid}/field.glb")
def get_glb(bid: str) -> FileResponse:
    return FileResponse(blueprints.glb_path(bid), media_type="model/gltf-binary", filename="field.glb")


@app.post("/api/blueprints/{bid}/activate")
def post_activate(bid: str) -> dict:
    out = blueprints.activate(bid)
    _plans.clear()
    return out


# ---------- health ----------

@app.get("/api/health")
def health() -> dict:
    snap = fields.snapshot()
    return {"ok": True, "scenario": snap.scenario.get("_source"), "blueprint_id": snap.blueprint_id,
            "field_version": snap.version, "applied_actions": len(snap.applied),
            "commands": commands.status(), "actuator": os.environ.get("ACTUATOR", "sim"),
            "spacetime": spacetime_sync.status, "lastcall": LASTCALL, "ingest": INGEST}


@app.post("/api/spacetime/sync")
def spacetime_sync_now() -> dict:
    """Re-push the current state into SpacetimeDB, e.g. after republishing the module."""
    ok = spacetime_sync.push_state(fields.live_state())
    return {"ok": ok, "spacetime": spacetime_sync.status}


# ---------- Last Call (lastcall/, owned by @ronakkasaram) ----------

def lastcall_scenario() -> dict:
    """The flat field shape lastcall/core.py reads: field data plus current readings."""
    snap = fields.snapshot()
    f, st = snap.scenario["field"], fields.live_state(snap)
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


def _lastcall_forecast(actions, inflow_scale):
    snap = fields.snapshot()
    return forecast(snap.scenario, list(snap.applied) + list(actions), inflow_scale=inflow_scale,
                    with_range=False, plant=snap.plant)


try:
    from lastcall.router import router as lastcall_router, set_source

    set_source(get_scenario=lastcall_scenario, forecast_fn=_lastcall_forecast)
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
