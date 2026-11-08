"""HTTP and WebSocket routes for the live layer (#38). Mounted by main.py with install().

    GET  /api/live/state                  live readings and statuses (UNKNOWN when stale)
    GET  /api/live/connectors             connector status
    POST /api/live/readings/{connector}   push readings into a webhook connector
    POST /api/live/sim                    demo controls: speed, pause, advance
    POST /api/live/faults                 demo: inject a fault into the simulator (409 for an asset on a real connector)
    GET  /api/alerts                      every alert, newest last
    POST /api/alerts/{id}/ack             acknowledge (the ntfy button calls this)
    POST /api/alerts/{id}/shelve          {minutes 15..240, reason}: hide it for a while (CF-7)
    POST /api/alerts/{id}/unshelve        bring it back before the timer ends
    GET  /api/actions                     every proposal
    POST /api/actions                     propose a command (PENDING, changes nothing)
    POST /api/actions/{id}/confirm        send it through the asset's connector
    POST /api/actions/{id}/reject
    POST /api/jarvis                      ask Jarvis
    GET  /api/audit                       who proposed, confirmed, rejected, acked what (CF-10)
    WS   /ws                              state, alert, proposal, ui

Control tokens (#103, auth.py): with FREEBOARD_TOKEN, OPERATOR_PIN, or FREEBOARD_PHONE_TOKEN
set, every POST here needs X-Freeboard-Token (the webhook readings route keeps its own
connector token). The phone token may only confirm, reject, ack, shelve, and unshelve. decided_by comes from
the token's role, never from the body's "by". /ws takes ?token=.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable, List, Optional, Union

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from . import auth
from .auth import Caller, require_role
from .connectors.webhook import WebhookConnector
from .forecast import forecast
from .jarvis import Jarvis, JarvisUnavailable
from .live import SHELVE_MAX, SHELVE_MIN, ActionError, LiveField
from .planner import make_plans

log = logging.getLogger("freeboard.live_api")

# Fault type -> the word backend.tools.fake_field's console takes ('trip PUMP_2').
FAULT_WORDS = {"TRIP": "trip", "STUCK_VALVE": "stuck", "SENSOR_STALE": "stale", "LEAK": "leak", "REPAIR": "repair"}


class ActionIn(BaseModel):
    type: str
    asset_id: str
    value: Optional[Union[float, str]] = None
    target_id: Optional[str] = None
    reason: str = ""


class JarvisIn(BaseModel):
    message: str = Field("", max_length=4000)
    selected_asset_ids: List[str] = []
    image_png_base64: Optional[str] = Field(None, max_length=15_000_000)


class ReadingsIn(BaseModel):
    readings: List[dict]


class SimIn(BaseModel):
    speed: Optional[float] = Field(None, ge=0, le=30)
    paused: Optional[bool] = None
    advance_min: Optional[float] = Field(None, ge=0, le=240)


class FaultIn(BaseModel):
    asset_id: str
    type: str = "TRIP"
    reason: Optional[str] = None
    rate_bbl_hr: Optional[float] = None


class DecideIn(BaseModel):
    by: str = ""


class ShelveIn(BaseModel):
    minutes: float = Field(..., ge=SHELVE_MIN, le=SHELVE_MAX)
    reason: str = Field(..., max_length=500)
    by: str = ""

    @field_validator("reason")
    @classmethod
    def _reason_required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("A reason is required to shelve an alert.")
        return v.strip()


def _raw(scenario: dict) -> dict:
    try:
        return json.loads(Path(scenario.get("_source", "")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


OPERATOR = Depends(require_role(auth.OPERATOR))
PHONE = Depends(require_role(auth.PHONE))   # phone or operator


def build_router(live: LiveField, jarvis: Jarvis) -> APIRouter:
    r = APIRouter()

    def fail(e: ActionError):
        raise HTTPException(e.status, e.detail)

    @r.get("/api/live/state")
    def live_state() -> dict:
        return live.state()

    @r.get("/api/live/connectors")
    def live_connectors() -> dict:
        return {"connectors": live.registry.status(), "jarvis": jarvis.status(),
                "push": {"enabled": live.notifier.enabled, "last_error": live.notifier.last_error}}

    @r.post("/api/live/readings/{connector_id}")
    def push_readings(connector_id: str, body: ReadingsIn,
                      x_freeboard_token: Optional[str] = Header(None)) -> dict:
        conn = live.registry.get(connector_id)
        if not isinstance(conn, WebhookConnector):
            raise HTTPException(404, f"No webhook connector {connector_id}.")
        if not conn.authorized(x_freeboard_token):
            raise HTTPException(401, "Missing or wrong X-Freeboard-Token.")
        return conn.push(body.readings)

    @r.post("/api/live/sim")
    async def sim_controls(body: SimIn, _caller: Caller = OPERATOR) -> dict:
        if body.speed is not None:
            live.speed = body.speed
        if body.paused is not None:
            live.paused = body.paused
        if body.advance_min:
            await live.tick(body.advance_min)
        return {"clock": live.fmap.clock(live.t), "t": round(live.t, 2), "speed": live.speed, "paused": live.paused}

    @r.post("/api/live/faults")
    async def inject_fault(body: FaultIn, _caller: Caller = OPERATOR) -> dict:
        fault = body.model_dump(exclude_none=True)
        fault["asset_id"] = str(fault.get("asset_id", "")).upper()
        via = live.registry.feeder(fault["asset_id"])
        if via is not None:
            # The simulator doesn't own this asset: injecting there would answer ok and change
            # nothing on screen. The fake devices have no control endpoint to call instead.
            word = FAULT_WORDS.get(str(fault.get("type", "TRIP")).upper(), "trip")
            raise HTTPException(409, f"{fault['asset_id']} is read over {via.id} ({via.kind}): fault injection "
                                     f"only works on simulated assets; trip it at the device (e.g. type "
                                     f"'{word} {fault['asset_id']}' in backend.tools.fake_field)")
        why = live.registry.sim.inject(fault)
        if why:
            raise HTTPException(422, why)
        return {"ok": True}

    @r.get("/api/alerts")
    def get_alerts() -> dict:
        return {"alerts": live.alerts()}

    @r.post("/api/alerts/{alert_id}/ack")
    def ack_alert(alert_id: str, body: Optional[DecideIn] = None, caller: Caller = PHONE) -> dict:
        by = body.by if body else ""
        a = live.ack(alert_id, by=caller.decided_by(by) or "", role=caller.role, source=caller.source(by))
        if a is None:
            raise HTTPException(404, f"No alert {alert_id}.")
        return a

    @r.post("/api/alerts/{alert_id}/shelve")
    async def shelve_alert(alert_id: str, body: ShelveIn, caller: Caller = PHONE) -> dict:
        try:
            return await live.shelve(alert_id, body.minutes, body.reason, by=caller.decided_by(body.by) or "",
                                     role=caller.role, source=caller.source(body.by))
        except ActionError as e:
            fail(e)

    @r.post("/api/alerts/{alert_id}/unshelve")
    async def unshelve_alert(alert_id: str, body: Optional[DecideIn] = None, caller: Caller = PHONE) -> dict:
        by = body.by if body else ""
        try:
            return await live.unshelve(alert_id, by=caller.decided_by(by) or "", role=caller.role,
                                       source=caller.source(by))
        except ActionError as e:
            fail(e)

    @r.get("/api/actions")
    def get_actions() -> dict:
        return {"proposals": list(live.proposals.values())}

    @r.post("/api/actions")
    async def post_action(body: ActionIn, caller: Caller = OPERATOR) -> dict:
        try:
            return await live.propose(body.model_dump(), reason=body.reason, source="console", role=caller.role)
        except ActionError as e:
            fail(e)

    @r.post("/api/actions/{action_id}/confirm")
    async def confirm_action(action_id: str, body: Optional[DecideIn] = None, caller: Caller = PHONE) -> dict:
        by = body.by if body else ""
        try:
            return await live.confirm(action_id, by=caller.decided_by(by) or "", role=caller.role,
                                      source=caller.source(by))
        except ActionError as e:
            fail(e)

    @r.post("/api/actions/{action_id}/reject")
    async def reject_action(action_id: str, body: Optional[DecideIn] = None, caller: Caller = PHONE) -> dict:
        by = body.by if body else ""
        try:
            return await live.reject(action_id, by=caller.decided_by(by) or "", role=caller.role,
                                     source=caller.source(by))
        except ActionError as e:
            fail(e)

    @r.post("/api/jarvis")
    async def ask_jarvis(body: JarvisIn, caller: Caller = OPERATOR) -> dict:
        try:
            return await jarvis.handle(body.message, body.selected_asset_ids, body.image_png_base64,
                                       role=caller.role)
        except JarvisUnavailable as e:
            raise HTTPException(503, e.detail)

    @r.get("/api/audit")
    def get_audit(since: int = 0) -> dict:
        return {"entries": live.audit.entries(since)}

    @r.websocket("/ws")
    async def ws(websocket: WebSocket, role: str = "web", name: str = "", token: str = "") -> None:
        ok, caller = auth.ws_caller(token)
        if not ok:
            await websocket.accept()
            await websocket.close(code=1008, reason=auth.DENIED)
            return
        steer = auth.may_steer(caller)
        client = await live.hub.connect(websocket, role, name)
        try:
            await live.hub.send(client, "state", {"state": live.state()})
            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "ping":
                    await live.hub.send(client, "pong", {})
                elif steer and msg.get("type") == "ui" and live.fmap.kind_of(str(msg.get("asset_id", ""))):
                    # A phone can point every console at an asset (with tokens on, only with a valid one).
                    await live.focus(msg["asset_id"], str(msg.get("reason", "")), source=client.role)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            live.hub.disconnect(client)

    return r


def make_reload(live: LiveField, jarvis: Jarvis) -> Callable[[dict], dict]:
    """A plain function that points the live layer at a newly activated scenario.

    Safe to call from anywhere: from a sync endpoint's worker thread (#64's
    _use_scenario) it runs the reload on the live loop and waits, so it never interleaves
    with a tick or a confirm; on the loop itself it reloads at once and schedules the
    broadcast; with no loop running (tests, scripts) it just reloads."""
    pending: set = set()

    def reload(scenario: dict) -> dict:
        raw = _raw(scenario)
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        loop = live.loop
        if loop is not None and loop.is_running() and here is not loop:
            changes = asyncio.run_coroutine_threadsafe(live.reload_and_publish(scenario, raw), loop).result(timeout=30)
        else:
            changes = live.reload(scenario, raw)
            if here is not None:
                task = here.create_task(live.publish_reload(changes))
                pending.add(task)
                task.add_done_callback(pending.discard)
        jarvis.refresh()
        log.info("live layer reloaded: %d assets, %d alerts resolved, %d proposals closed", changes["assets"],
                 len(changes["resolved_alerts"]), len(changes["closed_proposals"]))
        return changes

    return reload


def install(app: FastAPI, scenario: dict) -> LiveField:
    """Mount the live layer. main.py's lifespan starts and stops app.state.live.

    app.state.live_reload(scenario) makes the live layer follow a newly activated
    blueprint (POST /api/blueprints/{id}/activate calls it once #64 wires it in)."""
    live = LiveField(scenario, _raw(scenario))
    jarvis = Jarvis(live, forecast_fn=lambda sc, actions: forecast(sc, actions),
                    plans_fn=lambda sc: make_plans(sc))
    app.state.live_reload = make_reload(live, jarvis)
    app.include_router(build_router(live, jarvis))
    extra = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
    if extra:  # e.g. a phone or tunnel origin; localhost is already allowed by main.py
        app.add_middleware(CORSMiddleware, allow_origins=extra,
                           allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
                           allow_methods=["*"], allow_headers=["*"])
    app.state.live = live
    app.state.jarvis = jarvis
    app.state.audit = live.audit   # app.state.audit.append("approved", source=..., role=...) from any lane
    return live
