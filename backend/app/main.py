"""Freeboard backend. Run from the repo root:

    uvicorn backend.app.main:app --host 127.0.0.1 --port 8000

One clock (#42, now.py): /api/state, /api/forecast, /api/plans, approve, Last Call, and
the SpacetimeDB mirror all start from the same moment. With the live layer mounted that
is the live field (its clock, readings, and what people confirmed); otherwise it's the
active scenario plus the plans approved on it.

uvicorn's own request log never shows a token: log_redact.py masks ?token= (and any
other secret-looking query parameter) on the uvicorn loggers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import history, log_redact, now, origin_guard, spacetime_sync
from .connectors import extra as _extra_connectors  # noqa: F401 - registers OPC UA, Sparkplug B (#126)

try:   # SpacetimeDB v2 mirror (#126): alerts, proposals, connectors, one apply_batch per tick
    from . import spacetime_mirror
except ImportError:  # a checkout without it keeps the asset-only mirror below
    spacetime_mirror = None
from .auth import Caller, OPERATOR, require_role
from .forecast import forecast
from .planner import approve, make_plans
from .scenario import field_payload, load_scenario, state_payload

log = logging.getLogger("freeboard")
log_redact.install()   # uvicorn logs /ws?token=...; the filter masks the token (#126 round 2)
SCENARIO = load_scenario()
_plans: dict[str, dict] = {}   # plan id -> plan, from the latest /api/plans calls
now.set_static(SCENARIO)
MIRROR_S = float(os.environ.get("SPACETIME_MIRROR_S", "1.0"))   # SpacetimeDB follows the live field this often


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Mirror the starting state into SpacetimeDB without blocking startup.
    threading.Thread(target=spacetime_sync.push_state,
                     args=(now.state(_app),), daemon=True).start()
    live = getattr(_app.state, "live", None)  # live layer (#38), mounted at the bottom
    if live:
        live.start()
    mirror = asyncio.get_running_loop().create_task(
        spacetime_mirror.run(_app, MIRROR_S) if spacetime_mirror is not None else _mirror(_app))
    recording = asyncio.get_running_loop().create_task(history.run(_app))   # CF-14
    yield
    runs = getattr(_app.state, "runs", None)  # plan runs (#63): stop background re-plans first
    if runs:
        await runs.close()
    mirror.cancel()
    recording.cancel()
    if live:
        await live.stop()


def _mirror_key(a: dict) -> tuple:
    lvl = a.get("level_pct")
    return (a.get("status"), None if lvl is None else round(float(lvl), 1), a.get("position"), a.get("reason"))


async def _mirror(_app: FastAPI) -> None:
    """Push every change on the live field into SpacetimeDB within MIRROR_S (C4): only the
    assets whose status, level (0.1 %), position, or reason changed, plus the clock. Used
    only when spacetime_mirror.py (#126) is missing; that one also mirrors alerts,
    proposals, and connectors."""
    sent: dict[str, tuple] = {}
    field_id = None
    while True:
        await asyncio.sleep(MIRROR_S)
        if not spacetime_sync.enabled():
            continue
        try:
            st = now.state(_app)          # on the loop, so it never reads half a tick
            if field_id != id(SCENARIO):  # a new field: send everything again
                sent, field_id = {}, id(SCENARIO)
            changed = {aid: a for aid, a in st["assets"].items() if sent.get(aid) != _mirror_key(a)}
            ok = await asyncio.to_thread(spacetime_sync.push_state, {**st, "assets": changed})
            if ok:
                sent.update({aid: _mirror_key(a) for aid, a in changed.items()})
        except Exception:  # noqa: BLE001 - the mirror must never take the server down
            log.exception("SpacetimeDB mirror failed")


app = FastAPI(title="Freeboard backend", lifespan=lifespan)
app.include_router(history.router)   # GET /api/history (CF-14)
from . import phone_alerts  # noqa: E402
app.include_router(phone_alerts.router)   # /api/phone: text and voice alerts (#160), off without TWILIO_*


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
    """Any bug comes back as JSON the console can show, never a bare crash page."""
    log.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": f"Internal error: {type(exc).__name__}: {exc}"})

# Cross-site writes are always refused (origin_guard.py). Installed before CORS so CORS
# stays outermost and the browser can read the refusal. Tokens are checked per route
# with auth.require_role (#103, CF-15).
origin_guard.install(app)

# Security events (#126): records refused requests (401, 403, 429, 413/415, /ws 1008) and
# serves GET /api/security/events. Added after origin_guard, so it sits outside it and
# sees its 403s. It never changes a response.
try:
    from . import security_log

    security_log.install(app)
except ImportError as e:
    log.warning("Security log not mounted: %s", e)

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
async def get_state() -> dict:
    """Now (#42): the live clock and readings when the live layer runs. `clock_source` says
    which. Runs on the event loop, so it never reads the live field halfway through a tick."""
    return now.state(app)


@app.post("/api/forecast")
async def post_forecast(req: ForecastRequest = ForecastRequest()) -> dict:
    """The future from now (#42). Whatever is already under way (trucks on the road, pumps
    that read off) runs first; the request's actions are what-ifs on top, timed from now."""
    n = now.get(app)  # snapshot on the loop; the simulation runs in a worker thread
    actions = list(n.actions) + [a.model_dump(exclude_none=True) for a in req.actions]
    out = await run_in_threadpool(forecast, n.scenario, actions, req.horizon_minutes, req.step_minutes,
                                  plant=n.plant)
    out["clock"], out["clock_source"] = n.plant.start_clock, n.source
    return out


@app.post("/api/plans")
async def post_plans(req: PlansRequest = PlansRequest()) -> dict:
    """Cheapest plans from now (#42). A plan never repeats something already under way."""
    n = now.get(app)
    out = await run_in_threadpool(make_plans, n.scenario, req.horizon_minutes, req.top_n, list(n.actions), n.plant)
    for p in out["plans"]:
        _plans[p["id"]] = p
    runs = getattr(app.state, "runs", None)
    if runs is not None and n.source == "live":  # executable by id, planned at this live minute (#63)
        runs.remember(out["plans"], t=n.t)
    out["clock"], out["clock_source"] = n.plant.start_clock, n.source
    out.setdefault("warnings", [w[:1].upper() + w[1:] for w in dict.fromkeys(n.plant.warnings)])
    return out


@app.post("/api/plans/{plan_id}/approve")
async def post_approve(plan_id: str, caller: Caller = Depends(require_role(OPERATOR))) -> dict:
    """Numbered steps and a radio script, then (#42):
    - live layer: one PENDING proposal per step (`proposals`). Nothing moves until a person
      confirms each one (POST /api/actions/{id}/confirm, phone or console), exactly like a
      Jarvis proposal. Approving the same plan again reuses proposals still pending.
    - no live layer: the plan's actions are recorded, so /api/state shows them and every
      later forecast and plan includes them."""
    n = now.get(app)
    plan = _plans.get(plan_id)
    if plan is None:  # e.g. the server restarted; plan ids are deterministic
        found = await run_in_threadpool(make_plans, n.scenario, 360, 3, list(n.actions), n.plant)
        for p in found["plans"]:
            _plans[p["id"]] = p
        plan = _plans.get(plan_id)
    if plan is None:
        raise HTTPException(404, f"No plan {plan_id}. Call POST /api/plans first.")
    out = await run_in_threadpool(approve, n.scenario, plan, list(n.actions), n.plant)
    live = now.live_of(app)
    if live is not None:
        out["proposals"], errors = await _propose_steps(live, plan, out)
        if errors:
            out["proposal_errors"] = errors
    else:
        now.record(plan["actions"])
        out["recorded_actions"] = len(plan["actions"])
    audit = getattr(app.state, "audit", None)
    if audit is not None:
        audit.append("approved", source=caller.source(), role=caller.role, plan_id=plan["id"],
                     proposals=out.get("proposals", []))
    threading.Thread(target=_push_plan, args=(out,), daemon=True).start()
    return out


def _same_action(a: dict, b: dict) -> bool:
    keep = ("type", "asset_id", "value", "target_id")
    return {k: a.get(k) for k in keep if a.get(k) is not None} == {k: b.get(k) for k in keep if b.get(k) is not None}


async def _propose_steps(live, plan: dict, out: dict) -> tuple[list, list]:
    """One live proposal per plan step, in step order. Reuses an identical PENDING one."""
    step_of = {s["asset_id"]: s for s in out["steps"]}
    ordered = sorted(plan["actions"], key=lambda a: step_of.get(a["asset_id"], {}).get("n", 99))
    ids, errors = [], []
    for a in ordered:
        action = {k: a[k] for k in ("type", "asset_id", "value", "target_id") if a.get(k) is not None}
        same = next((p for p in live.proposals.values()
                     if p["status"] == "PENDING" and _same_action(p["action"], action)), None)
        if same is not None:
            ids.append(same["id"])
            continue
        step = step_of.get(a["asset_id"], {})
        reason = f"Step {step.get('n', '?')} of approved plan {plan['id']}: {step.get('text', '')}".strip()
        try:
            p = await live.propose(action, reason=reason, source="plan")
            ids.append(p["id"])
        except Exception as e:  # noqa: BLE001 - ActionError and friends: report, keep going
            errors.append(f"{a['type']} {a['asset_id']}: {getattr(e, 'detail', e)}")
    return ids, errors


def _push_plan(out: dict) -> None:
    if not spacetime_sync.enabled():
        return
    try:
        spacetime_sync.call_reducer("set_plan", [out["plan_id"], json.dumps(out["steps"])])
    except Exception as e:  # noqa: BLE001 - SpacetimeDB must never break the API
        spacetime_sync.status.update(last_push_ok=False, last_error=str(e))
        log.warning("SpacetimeDB set_plan failed: %s", e)


# Bumped when a route's shape changes; clients can check it in /api/health.
API_VERSION = "1.1"


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "api_version": API_VERSION, "scenario": SCENARIO.get("_source"), "spacetime": spacetime_sync.status,
            "clock_source": "live" if now.live_of(app) is not None else "static",
            "lastcall": LASTCALL, "ingest": INGEST}


@app.post("/api/spacetime/sync")
async def spacetime_sync_now(caller: Caller = Depends(require_role(OPERATOR))) -> dict:
    """Re-push the current state into SpacetimeDB, e.g. after republishing the module."""
    st = now.state(app)
    ok = await run_in_threadpool(spacetime_sync.push_state, st)
    return {"ok": ok, "spacetime": spacetime_sync.status}


# ---------- Last Call (lastcall/, owned by @ronakkasaram) ----------

_lastcall = threading.local()   # the moment one Last Call request plans from


def _lastcall_now():
    """Last Call's route runs in a worker thread. Take the snapshot on the event loop (so it
    never reads half a tick) and pin it for the request's many forecasts."""
    try:
        import anyio.from_thread
        n = anyio.from_thread.run_sync(now.get, app)
    except RuntimeError:  # not inside a worker thread (a test or a script): read directly
        n = now.get(app)
    _lastcall.now, _lastcall.at = n, time.monotonic()
    return n


def _lastcall_forecast(actions, inflow_scale):
    n = getattr(_lastcall, "now", None)
    if n is None or time.monotonic() - getattr(_lastcall, "at", 0) > 10:
        n = now.get(app)
    return forecast(n.scenario, list(n.actions) + list(actions), inflow_scale=inflow_scale, with_range=False,
                    plant=n.plant)


def lastcall_scenario() -> dict:
    """The flat field shape lastcall/core.py reads: field data plus current readings,
    from the same moment as /api/forecast (#42)."""
    n = _lastcall_now()
    f, st = n.scenario["field"], state_payload(n.scenario)
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

    set_source(get_scenario=lastcall_scenario, forecast_fn=_lastcall_forecast)
    app.include_router(lastcall_router)
    LASTCALL = "mounted"
except ImportError:
    LASTCALL = "not installed (lastcall/ is not on this branch yet)"


# ---------- Dataset mode (backend/app/eog): the EOG sponsor data under /api/eog ----------
# Every /api/eog route answers 503 until FREEBOARD_EOG_DIR points at a folder of datasets.

try:
    from .eog.api import router as eog_router

    app.include_router(eog_router)
except ImportError as e:
    log.warning("Dataset mode not mounted: %s", e)

# Dataset mode on the legacy routes (#153): with FREEBOARD_EOG_DIR set, /api/field, /api/state,
# /api/forecast, /api/plans, approve and /api/lastcall serve the active dataset (eog/legacy_api.py
# mounts them in front of the routes here, which answer as before when dataset mode is off).
try:
    from .eog import legacy_api as _eog_legacy

    _eog_legacy.install(app)
except ImportError as e:
    log.warning("Dataset mode legacy routes not mounted: %s", e)

# /api/dataset/* (the /dataset page, its 3D) and GET /api/eog/blueprint (#152).
try:
    from .eog.dataset_api import router as dataset_router

    app.include_router(dataset_router)
except ImportError as e:
    log.warning("Dataset page API not mounted: %s", e)


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

    LIVE = install_live(app, SCENARIO, plans=_plans)
except ImportError as e:
    LIVE = None
    log.warning("Live layer not mounted: %s", e)


# ---------- Blueprints (#41): upload, validate, build, serve, activate ----------
try:
    from . import blueprints

    def _use_scenario(scenario: dict) -> None:
        """Swaps the field every endpoint reads (POST /api/blueprints/{id}/activate), and
        points the live layer at it, so one clock stays one field."""
        global SCENARIO
        SCENARIO = scenario
        _plans.clear()
        now.set_static(scenario)
        reload = getattr(app.state, "live_reload", None)
        if reload is not None:
            reload(scenario)
        threading.Thread(target=spacetime_sync.push_state, args=(now.state(app),), daemon=True).start()

    blueprints.setup(default=SCENARIO, use=_use_scenario)
    app.include_router(blueprints.router)
except ImportError as e:  # e.g. a checkout without backend/app/blueprints.py
    log.warning("Blueprints API not mounted: %s", e)
