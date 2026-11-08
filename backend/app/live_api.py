"""HTTP and WebSocket routes for the live layer (#38). Mounted by main.py with install().

    GET  /api/live/state                  live readings and statuses (UNKNOWN when stale)
    GET  /api/live/connectors             connector status
    POST /api/live/readings/{connector}   push readings into a webhook connector
    POST /api/live/sim                    demo controls: speed, pause, advance
    POST /api/live/faults                 demo: inject a fault into the simulator
    GET  /api/alerts                      every alert, newest last
    POST /api/alerts/{id}/ack             acknowledge (the ntfy button calls this)
    GET  /api/actions                     every proposal
    POST /api/actions                     propose a command (PENDING, changes nothing)
    POST /api/actions/{id}/confirm        send it through the asset's connector
    POST /api/actions/{id}/reject
    POST /api/jarvis                      ask Jarvis
    POST /api/plans/{id}/execute          run a plan: one confirmed proposal per step (#63)
    GET  /api/runs, GET /api/runs/{id}
    POST /api/runs/{id}/pause | resume | cancel
    WS   /ws                              state, alert, proposal, ui, run
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Union

from fastapi import APIRouter, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .connectors.webhook import WebhookConnector
from .forecast import forecast
from .jarvis import Jarvis, JarvisUnavailable
from .live import ActionError, LiveField
from .planner import make_plans
from .runs import RunManager

log = logging.getLogger("freeboard.live_api")


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


def _raw(scenario: dict) -> dict:
    try:
        return json.loads(Path(scenario.get("_source", "")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_router(live: LiveField, jarvis: Jarvis, runs: Optional[RunManager] = None) -> APIRouter:
    r = APIRouter()
    runs = runs or RunManager(live, jarvis)

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
    async def sim_controls(body: SimIn) -> dict:
        if body.speed is not None:
            live.speed = body.speed
        if body.paused is not None:
            live.paused = body.paused
        if body.advance_min:
            await live.tick(body.advance_min)
        return {"clock": live.fmap.clock(live.t), "t": round(live.t, 2), "speed": live.speed, "paused": live.paused}

    @r.post("/api/live/faults")
    async def inject_fault(body: FaultIn) -> dict:
        why = live.registry.sim.inject(body.model_dump(exclude_none=True))
        if why:
            raise HTTPException(422, why)
        return {"ok": True}

    @r.get("/api/alerts")
    def get_alerts() -> dict:
        return {"alerts": live.alerts()}

    @r.post("/api/alerts/{alert_id}/ack")
    def ack_alert(alert_id: str) -> dict:
        for a in live.detector.alerts:
            if a["id"] == alert_id:
                a["acked"] = True
                return a
        raise HTTPException(404, f"No alert {alert_id}.")

    @r.get("/api/actions")
    def get_actions() -> dict:
        return {"proposals": list(live.proposals.values())}

    @r.post("/api/actions")
    async def post_action(body: ActionIn) -> dict:
        try:
            return await live.propose(body.model_dump(), reason=body.reason, source="console")
        except ActionError as e:
            fail(e)

    @r.post("/api/actions/{action_id}/confirm")
    async def confirm_action(action_id: str, body: Optional[DecideIn] = None) -> dict:
        try:
            return await live.confirm(action_id, by=body.by if body else "")
        except ActionError as e:
            fail(e)

    @r.post("/api/actions/{action_id}/reject")
    async def reject_action(action_id: str, body: Optional[DecideIn] = None) -> dict:
        try:
            return await live.reject(action_id, by=body.by if body else "")
        except ActionError as e:
            fail(e)

    @r.post("/api/jarvis")
    async def ask_jarvis(body: JarvisIn) -> dict:
        try:
            return await jarvis.handle(body.message, body.selected_asset_ids, body.image_png_base64)
        except JarvisUnavailable as e:
            raise HTTPException(503, e.detail)

    # ---------- plan runs (#63) ----------
    @r.post("/api/plans/{plan_id}/execute")
    async def execute_plan(plan_id: str, body: Optional[DecideIn] = None) -> dict:
        try:
            return await runs.execute_id(plan_id, source="console", by=body.by if body else "")
        except ActionError as e:
            fail(e)

    @r.get("/api/runs")
    def get_runs() -> dict:
        return {"runs": runs.list()}

    @r.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            return runs.get(run_id)
        except ActionError as e:
            fail(e)

    @r.post("/api/runs/{run_id}/{verb}")
    async def run_control(run_id: str, verb: str, body: Optional[DecideIn] = None) -> dict:
        fn = {"pause": runs.pause, "resume": runs.resume, "cancel": runs.cancel}.get(verb)
        if fn is None:
            raise HTTPException(404, f"Unknown run action {verb!r}. Use pause, resume, or cancel.")
        try:
            return await fn(run_id, by=body.by if body else "")
        except ActionError as e:
            fail(e)

    @r.websocket("/ws")
    async def ws(websocket: WebSocket, role: str = "web", name: str = "") -> None:
        client = await live.hub.connect(websocket, role, name)
        try:
            await live.hub.send(client, "state", {"state": live.state()})
            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "ping":
                    await live.hub.send(client, "pong", {})
                elif msg.get("type") == "ui" and live.fmap.kind_of(str(msg.get("asset_id", ""))):
                    # A phone can point every console at an asset.
                    await live.focus(msg["asset_id"], str(msg.get("reason", "")), source=client.role)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            live.hub.disconnect(client)

    return r


def install(app: FastAPI, scenario: dict, plans: Optional[dict] = None) -> LiveField:
    """Mount the live layer. main.py's lifespan starts and stops app.state.live.
    `plans` is main.py's cache of POST /api/plans results, so those ids can be executed."""
    live = LiveField(scenario, _raw(scenario))
    jarvis = Jarvis(live, forecast_fn=lambda sc, actions: forecast(sc, actions),
                    plans_fn=lambda sc: make_plans(sc))
    runs = RunManager(live, jarvis, plans=plans)
    jarvis.runs = runs
    app.include_router(build_router(live, jarvis, runs))
    extra = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
    if extra:  # e.g. a phone or tunnel origin; localhost is already allowed by main.py
        app.add_middleware(CORSMiddleware, allow_origins=extra,
                           allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
                           allow_methods=["*"], allow_headers=["*"])
    app.state.live = live
    app.state.jarvis = jarvis
    app.state.runs = runs
    return live
