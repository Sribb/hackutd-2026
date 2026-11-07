"""Freeboard backend. Run from the repo root:

    uvicorn backend.app.main:app --port 8000            # laptop only
    uvicorn backend.app.main:app --host 0.0.0.0 --port 8000   # phones on the same Wi-Fi

REST endpoints follow docs/CONTRACT.md. Everything also streams over /ws.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field as PField

from .bus import Hub
from .field import load_field
from .jarvis import Jarvis
from .live import LiveField
from .sim import forecast as sim_forecast

try:  # Ronak's lane (PR #10). Optional until it merges.
    from lastcall import core as lastcall_core
except ImportError:
    lastcall_core = None


def _load_dotenv() -> None:
    """Tiny .env loader so keys never live in code. Existing env vars win."""
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.split(" #", 1)[0].strip().strip('"').strip("'")  # allow trailing comments
            if v:
                os.environ.setdefault(k.strip(), v)


_load_dotenv()
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

FIELD = load_field()
HUB = Hub()
LIVE = LiveField(FIELD, HUB)
JARVIS = Jarvis(LIVE)
PHONE_PAGE = Path(__file__).with_name("phone.html")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.environ.get("FREEBOARD_AUTOSTART", "1") != "0":
        LIVE.start()
    await LIVE.refresh_forecast()
    yield
    await LIVE.stop()


app = FastAPI(title="Freeboard", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- request models
class Action(BaseModel):
    type: Literal["SET_VALVE", "SET_PUMP", "CHOKE_WELL", "SEND_TRUCK"]
    asset_id: str
    value: Any = None
    target_id: str | None = None
    at_min: float = 0


class ForecastReq(BaseModel):
    horizon_minutes: int = PField(360, ge=5, le=1440)
    step_minutes: int = PField(5, ge=1, le=60)
    actions: list[Action] = []


class PlansReq(BaseModel):
    horizon_minutes: int = PField(360, ge=30, le=1440)


class ApproveReq(BaseModel):
    by: str = ""
    execute: bool = True


class CommandReq(BaseModel):
    type: Literal["SET_VALVE", "SET_PUMP", "CHOKE_WELL", "SEND_TRUCK"]
    asset_id: str
    value: Any = None
    target_id: str | None = None
    source: str = "web"
    by: str = ""
    confirm: bool = False


class ConfirmReq(BaseModel):
    by: str = ""
    approve: bool = True


class JarvisReq(BaseModel):
    text: str = PField(..., min_length=1, max_length=2000)
    session: str = "default"
    source: str = "web"
    by: str = ""


class Reading(BaseModel):
    asset_id: str
    metric: str
    value: Any
    hold_min: float = 10


class TelemetryReq(BaseModel):
    source: str = "device"
    readings: list[Reading]


class UiReq(BaseModel):
    action: str
    asset_id: str | None = None
    asset_ids: list[str] | None = None
    t_min: float | None = None
    panel: str | None = None
    source: str = "web"
    by: str = ""


class SimReq(BaseModel):
    speed: float | None = None
    paused: bool | None = None


class AdvanceReq(BaseModel):
    minutes: float = PField(5, ge=0, le=240)


class FaultReq(BaseModel):
    asset_id: str
    type: Literal["TRIP", "REPAIR", "SENSOR_DROPOUT", "SENSOR_STUCK", "CLEAR_SENSOR"] = "TRIP"
    reason: str | None = None


# ---------------------------------------------------------------- contract endpoints
@app.get("/health")
async def health():
    return {"ok": True, "clock": FIELD.clock_at(LIVE.plant.t), "clients": len(HUB.clients),
            "jarvis": JARVIS.status(), "push_enabled": LIVE.alerts.push_enabled}


@app.get("/api/field")
async def get_field():
    return FIELD.public()


@app.get("/api/state")
async def get_state():
    return LIVE.state()


@app.post("/api/forecast")
async def post_forecast(req: ForecastReq):
    return await LIVE.forecast([a.model_dump() for a in req.actions], req.horizon_minutes, req.step_minutes)


@app.post("/api/plans")
async def post_plans(req: PlansReq | None = None):
    return await LIVE.make_plans((req or PlansReq()).horizon_minutes)


@app.get("/api/plans")
async def get_plans():
    return LIVE.plans_result or {"plans": []}


@app.post("/api/plans/{plan_id}/approve")
async def approve_plan(plan_id: str, req: ApproveReq | None = None):
    req = req or ApproveReq()
    res = await LIVE.approve(plan_id, by=req.by, execute=req.execute)
    if res is None:
        raise HTTPException(404, f"No plan {plan_id}. POST /api/plans first; plans reset each time.")
    return res


class LastCallReq(BaseModel):
    horizon_minutes: int = PField(360, ge=30, le=1440)
    step_minutes: int = PField(5, ge=1, le=60)
    top_n: int = PField(3, ge=1, le=10)


@app.post("/api/lastcall")
async def post_lastcall(req: LastCallReq | None = None):
    """Last Call (lastcall/core.py) run on this backend's live field and forecast.

    Snapshot the sensed plant under the lock once, then let Last Call bisect on that
    snapshot in a worker thread, so the live clock can't change the plant mid-search.
    """
    if lastcall_core is None:
        raise HTTPException(503, "lastcall/ is not installed yet (PR #10).")
    req = req or LastCallReq()
    # ~1,000 forecasts per call (several seconds), so reuse it until the field changes.
    key = (int(LIVE.plant.t // 2), len(LIVE.command_log), tuple(sorted(LIVE.failed)),
           tuple(sorted(LIVE.data_issues)), req.horizon_minutes, req.step_minutes, req.top_n)
    if _lastcall_cache.get("key") == key:
        return _lastcall_cache["res"]
    async with LIVE.lock:
        sp = LIVE.sensed_plant()
        warns = LIVE.data_warnings()
        scenario = {**FIELD.raw, "clock": FIELD.clock_at(sp.t),
                    "valves": [{**v, "position": "OPEN" if sp.valve_open[v["id"]] else "CLOSED"} for v in FIELD.raw["valves"]]}

    def fc(actions, inflow_scale=1.0):
        return sim_forecast(sp, actions, req.horizon_minutes, req.step_minutes, warns,
                            with_range=False, record=False, inflow_scale=inflow_scale)

    res = await asyncio.to_thread(lastcall_core.last_call, scenario, fc, req.horizon_minutes, req.step_minutes, req.top_n)
    res.setdefault("warnings", warns)
    _lastcall_cache.update(key=key, res=res)
    return res


_lastcall_cache: dict = {}


# ---------------------------------------------------------------- live control
@app.post("/api/commands")
async def post_command(req: CommandReq):
    res = await LIVE.command(req.model_dump(include={"type", "asset_id", "value", "target_id"}),
                             source=req.source, by=req.by, confirm=req.confirm)
    if res.get("status") == "REJECTED" and "error" in res:
        raise HTTPException(422, res["error"])
    return res


@app.get("/api/commands")
async def get_commands():
    return {"commands": list(LIVE.command_log), "pending": list(LIVE.pending.values())}


@app.post("/api/commands/{pending_id}/confirm")
async def confirm_command(pending_id: str, req: ConfirmReq | None = None):
    req = req or ConfirmReq()
    res = await LIVE.confirm(pending_id, by=req.by, approve=req.approve)
    if res is None:
        raise HTTPException(404, f"No pending command {pending_id}")
    return res


@app.post("/api/jarvis")
async def post_jarvis(req: JarvisReq):
    return await JARVIS.handle(req.text, req.session, req.source, req.by)


@app.post("/api/ui")
async def post_ui(req: UiReq):
    """Any screen (usually a phone) can drive every other screen."""
    d = req.model_dump(exclude_none=True)
    await LIVE.broadcast("ui", d)
    return {"ok": True}


@app.post("/api/telemetry")
async def post_telemetry(req: TelemetryReq):
    res = LIVE.ingest([r.model_dump() for r in req.readings], req.source)
    await LIVE.broadcast("telemetry", LIVE.telemetry())
    return res


@app.get("/api/telemetry")
async def get_telemetry():
    return LIVE.telemetry()


@app.get("/api/alerts")
async def get_alerts():
    return {"alerts": LIVE.alerts.items, "push_enabled": LIVE.alerts.push_enabled}


@app.post("/api/alerts/{alert_id}/ack")
async def ack_alert(alert_id: str, by: str = "phone"):
    a = await LIVE.alerts.ack(alert_id, by)
    if a is None:
        raise HTTPException(404, f"No alert {alert_id}")
    return a


@app.get("/api/snapshot")
async def snapshot():
    return {**LIVE.snapshot(), "jarvis": JARVIS.status()}


# ---------------------------------------------------------------- demo controls
@app.get("/api/sim")
async def get_sim():
    return LIVE.sim_status()


@app.post("/api/sim")
async def post_sim(req: SimReq):
    return await LIVE.set_sim(req.speed, req.paused)


@app.post("/api/sim/advance")
async def post_advance(req: AdvanceReq):
    return await LIVE.advance(req.minutes)


@app.post("/api/demo/reset")
async def demo_reset():
    return await LIVE.reset()


@app.post("/api/demo/fault")
async def demo_fault(req: FaultReq):
    return await LIVE.inject(req.model_dump(exclude_none=True))


# ---------------------------------------------------------------- realtime
@app.websocket("/ws")
async def ws(websocket: WebSocket, role: str = "web", name: str = ""):
    client = await HUB.connect(websocket, role, name)
    await HUB.send(client, "hello", {"you": client.public(), "field": FIELD.public(),
                                      **LIVE.snapshot(), "jarvis": JARVIS.status()})
    await HUB.broadcast("presence", HUB.presence())
    try:
        while True:
            msg = await websocket.receive_json()
            await _handle_ws(client, msg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # malformed client message: tell them, keep the hub alive
        await HUB.send(client, "error", {"error": str(exc)})
    finally:
        await HUB.disconnect(client)


async def _handle_ws(client, msg: dict) -> None:
    typ = msg.get("type")
    by = msg.get("by") or client.name
    rid = msg.get("rid")  # request id echoed back so clients can match replies

    async def reply(data: dict) -> None:
        await HUB.send(client, "reply", {"rid": rid, "for": typ, **data})

    if typ == "ping":
        await reply({"pong": True})
    elif typ == "jarvis":
        res = await JARVIS.handle(msg.get("text", ""), msg.get("session", client.id), client.role, by)
        await reply(res)
    elif typ == "command":
        res = await LIVE.command(msg.get("action", {}), source=client.role, by=by, confirm=bool(msg.get("confirm")))
        await reply(res)
    elif typ == "confirm":
        res = await LIVE.confirm(msg.get("id", ""), by=by, approve=msg.get("approve", True))
        await reply(res or {"error": "No such pending command"})
    elif typ == "forecast":
        res = await LIVE.forecast(msg.get("actions", []), int(msg.get("horizon_minutes", 360)), int(msg.get("step_minutes", 5)))
        await reply(res)
    elif typ == "plans":
        await reply(await LIVE.make_plans())
    elif typ == "approve":
        res = await LIVE.approve(msg.get("plan_id", ""), by=by, execute=msg.get("execute", True))
        await reply(res or {"error": "No such plan"})
    elif typ == "ui":
        await LIVE.broadcast("ui", {**msg.get("data", {}), "source": client.role, "by": by})
    elif typ == "ack_alert":
        await reply(await LIVE.alerts.ack(msg.get("id", ""), by) or {"error": "No such alert"})
    elif typ == "telemetry":
        await reply(LIVE.ingest(msg.get("readings", []), client.name or client.role))
    else:
        await reply({"error": f"Unknown message type {typ!r}"})


# ---------------------------------------------------------------- phone remote
@app.get("/phone", response_class=HTMLResponse)
async def phone():
    """Minimal phone remote: alerts, countdown, Jarvis, confirm buttons. The real UI can replace it."""
    if PHONE_PAGE.exists():
        return FileResponse(PHONE_PAGE)
    return HTMLResponse("<p>phone.html missing</p>", status_code=500)
