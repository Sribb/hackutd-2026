"""HTTP and WebSocket routes for the live layer (#38). Mounted by main.py with install().

    GET  /api/live/state                  live readings and statuses (UNKNOWN when stale)
    GET  /api/live/connectors             connector status
    POST /api/live/readings/{connector}   push readings into a webhook connector
    POST /api/live/sim                    demo controls: speed, pause, advance
    POST /api/live/faults                 demo: inject a fault into the simulator
    GET  /api/alerts                      every alert, newest last
    POST /api/alerts/{id}/ack             acknowledge (the ntfy button calls this)
    POST /api/alerts/{id}/shelve          {minutes 15..240, reason}: hide it for a while (CF-7)
    POST /api/alerts/{id}/unshelve        bring it back before the timer ends
    GET  /api/actions                     every proposal
    POST /api/actions                     propose a command (PENDING, changes nothing)
    POST /api/actions/{id}/confirm        send it through the asset's connector
    POST /api/actions/{id}/reject
    POST /api/jarvis                      ask Jarvis
    GET  /api/jarvis/brief                countdown, plans, Last Call for the console's agents
    POST /api/jarvis/preview              {action}: what a fix card's action would change (compare_whatif)
    GET  /api/audit                       who proposed, confirmed, rejected, acked what (CF-10)
    WS   /ws                              state, alert, proposal, ui, brief

Control tokens (#103, auth.py): with FREEBOARD_TOKEN, OPERATOR_PIN, or FREEBOARD_PHONE_TOKEN
set, every POST here needs X-Freeboard-Token (the webhook readings route keeps its own
connector token). The phone token may only confirm, reject, ack, shelve, and unshelve. decided_by comes from
the token's role, never from the body's "by".

/ws takes its token, best first: the subprotocol "freeboard.<token>" (offer
["freeboard", "freeboard.<token>"]; the server picks "freeboard"), or a first message
{"type": "auth", "token": ...} within WS_AUTH_S seconds of connecting (answered with
{"type": "auth", "ok": true, "role": ...}), or ?token= (kept as a fallback; install()
redacts it, and X-Freeboard-Token values, from uvicorn's logs).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Callable, List, Optional, Union

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

from . import auth, jarvis_tools
from .auth import Caller, require_role
from .connectors.webhook import WebhookConnector
from .forecast import forecast
from .jarvis import Jarvis, JarvisUnavailable
from .live import SHELVE_MAX, SHELVE_MIN, ActionError, LiveField
from .planner import make_plans

log = logging.getLogger("freeboard.live_api")

WS_AUTH_S = 5.0            # a /ws client may send {"type": "auth"} first, within this long
WS_PROTOCOL = "freeboard"  # subprotocols: "freeboard" (picked) and "freeboard.<token>"
RELOAD_TIMEOUT_S = 30.0


# ---------- keep tokens out of the logs ----------

REDACTED = "[redacted]"
_TOKEN_PARAM = re.compile(r"(?i)(token=)[^&\s\"'\\]+")
_TOKEN_HEADER = re.compile(r"(?i)(x-freeboard-token['\"]?\s*[:=,]\s*b?['\"]?)[^'\"\s,;)}\]\\]+")
_PROTO_HEADER = re.compile(r"(?i)(sec-websocket-protocol['\"]?\s*[:=,]\s*b?['\"]?)([^'\"\r\n]*)")
_PROTO_TOKEN = re.compile(r"(?i)(freeboard\.)[^\s,'\"]+")


def redact(text: str) -> str:
    """text with every ?token=, X-Freeboard-Token value, and freeboard.<token> subprotocol hidden."""
    text = _TOKEN_PARAM.sub(lambda m: m.group(1) + REDACTED, text)
    text = _TOKEN_HEADER.sub(lambda m: m.group(1) + REDACTED, text)
    return _PROTO_HEADER.sub(lambda m: m.group(1) + _PROTO_TOKEN.sub(lambda n: n.group(1) + REDACTED, m.group(2)),
                             text)


class RedactTokens(logging.Filter):
    """On uvicorn.access and uvicorn.error: the access line "GET /ws?token=1234" becomes
    "GET /ws?token=[redacted]". Rewrites only the message and its string arguments (an
    argument that is not a string is replaced by its redacted text only when it held a
    token), so uvicorn's own formatters still get the argument shapes they expect."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(self._arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: self._arg(v) for k, v in record.args.items()}
        except Exception:  # noqa: BLE001 - never lose a log line over redaction
            pass
        return True

    @staticmethod
    def _arg(a):
        if isinstance(a, str):
            return redact(a)
        if isinstance(a, (int, float, bool)) or a is None:
            return a
        text = str(a)
        clean = redact(text)
        return a if clean == text else clean


def install_log_redaction() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokens) for f in logger.filters):
            logger.addFilter(RedactTokens())


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


class PreviewIn(BaseModel):
    action: dict


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
    brief = jarvis_tools.BriefFeed.attach(live, jarvis)

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
    async def sim_controls(body: SimIn, caller: Caller = OPERATOR) -> dict:
        live.audit.append("sim", source=caller.source(), role=caller.role, **body.model_dump(exclude_none=True))
        if body.speed is not None:
            live.speed = body.speed
        if body.paused is not None:
            live.paused = body.paused
        if body.advance_min:
            await live.tick(body.advance_min)
        return {"clock": live.fmap.clock(live.t), "t": round(live.t, 2), "speed": live.speed, "paused": live.paused}

    @r.post("/api/live/faults")
    async def inject_fault(body: FaultIn, caller: Caller = OPERATOR) -> dict:
        fault = body.model_dump(exclude_none=True)
        why = live.registry.sim.inject(dict(fault))
        live.audit.append("fault", source=caller.source(), role=caller.role, asset_id=fault.pop("asset_id"),
                          fault=fault.pop("type"), ok=not why, **fault, **({"result": why} if why else {}))
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

    @r.get("/api/jarvis/brief")
    async def jarvis_brief() -> dict:
        return await brief.get()

    @r.post("/api/jarvis/preview")
    async def jarvis_preview(body: PreviewIn, _caller: Caller = PHONE) -> dict:
        out = await jarvis_tools.compare_whatif(jarvis, [body.action])
        if "error" in out:
            raise HTTPException(422, out["error"])
        return out

    @r.get("/api/audit")
    def get_audit(since: int = 0) -> dict:
        return {"entries": live.audit.entries(since)}

    @r.websocket("/ws")
    async def ws(websocket: WebSocket, role: str = "web", name: str = "", token: str = "") -> None:
        offered = [s.strip() for s in websocket.headers.get("sec-websocket-protocol", "").split(",") if s.strip()]
        carried = next((s for s in offered if s.startswith(WS_PROTOCOL + ".")), None)
        if carried:
            token = carried[len(WS_PROTOCOL) + 1:]
        # A browser drops the socket unless the server picks one of the offered subprotocols.
        picked = WS_PROTOCOL if WS_PROTOCOL in offered else carried
        ip = auth.client_of(websocket)

        async def refuse(reason: str) -> None:
            await websocket.accept(subprotocol=picked)
            await websocket.close(code=1008, reason=reason)

        def verdict(tok: str) -> tuple[Optional[str], Caller]:
            """(why it is refused or None, caller)."""
            wait = auth.ws_wait_s(ip) if (tok or "").strip() else 0
            if wait:
                return f"Too many wrong PINs. Wait {wait} s.", Caller(None)
            ok, who = auth.ws_caller(tok, client=ip, audit=live.audit)
            return (None if ok else auth.DENIED), who

        why, caller = verdict(token)
        if why:
            await refuse(why)
            return
        steer = auth.may_steer(caller)
        client = await live.hub.connect(websocket, role, name, subprotocol=picked)
        # Without a token yet, the first message may carry it, within WS_AUTH_S seconds.
        auth_until = asyncio.get_running_loop().time() + WS_AUTH_S if not token.strip() else None
        try:
            await live.hub.send(client, "state", {"state": live.state()})
            if brief.latest is not None:
                await live.hub.send(client, "brief", {"brief": brief.latest})
            while True:
                msg = await websocket.receive_json()
                if not isinstance(msg, dict):
                    auth_until = None
                    continue
                if msg.get("type") == "auth":
                    in_time = auth_until is not None and asyncio.get_running_loop().time() <= auth_until
                    auth_until = None
                    if not in_time:
                        await live.hub.send(client, "auth", {"ok": False, "role": caller.role, "detail": (
                            f"Send auth as the first message, within {WS_AUTH_S:g} s of connecting.")})
                        continue
                    why, caller = verdict(str(msg.get("token") or ""))
                    if why:
                        await websocket.close(code=1008, reason=why)
                        return
                    steer = auth.may_steer(caller)
                    await live.hub.send(client, "auth", {"ok": True, "role": caller.role})
                    continue
                auth_until = None
                if msg.get("type") == "ping":
                    await live.hub.send(client, "pong", {})
                elif steer and msg.get("type") == "ui":
                    # A tokened phone or console can steer every screen, through the same whitelist
                    # as Jarvis (jarvis_tools.check_directive). An untokened client never can.
                    d, _why = jarvis_tools.check_directive(live, {"action": "focus", **msg})
                    if d:
                        cid = str(msg.get("id") or "")
                        d.update(id=cid if jarvis_tools.CLIENT_ID.match(cid) else jarvis_tools.new_id(),
                                 source=client.role, reason=str(msg.get("reason", ""))[:200])
                        await live.ui(d)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            live.hub.disconnect(client)

    return r


class ReloadTimeout(TimeoutError):
    """The live loop didn't get to the reload in time. It was called off: nothing changed."""


def make_reload(live: LiveField, jarvis: Jarvis, timeout_s: float = RELOAD_TIMEOUT_S) -> Callable[[dict], dict]:
    """A plain function that points the live layer at a newly activated scenario.

    Safe to call from anywhere: from a sync endpoint's worker thread (#64's
    _use_scenario) it runs the reload on the live loop and waits, so it never interleaves
    with a tick or a confirm; on the loop itself it reloads at once and schedules the
    broadcast; with no loop running (tests, scripts) it just reloads.

    From a worker thread it waits up to timeout_s for the loop to start the reload. If the
    loop hasn't started it by then, the reload is called off (it never applies later) and
    ReloadTimeout is raised; once it has started, the call returns its changes."""
    pending: set = set()

    def on_loop(scenario: dict, raw: dict, loop: asyncio.AbstractEventLoop) -> dict:
        gate = threading.Lock()
        job: dict = {"off": False}

        async def run() -> Optional[dict]:
            with gate:   # held only while reload() runs: it is synchronous
                if job["off"]:
                    return None
                job["changes"] = live.reload(scenario, raw)
            await live.publish_reload(job["changes"])
            return job["changes"]

        fut = asyncio.run_coroutine_threadsafe(run(), loop)
        try:
            return fut.result(timeout=timeout_s)
        except TimeoutError:
            with gate:
                if "changes" not in job:
                    job["off"] = True
                    fut.cancel()
                    raise ReloadTimeout(f"The live layer was busy for {timeout_s:g} s, so the field was not "
                                        f"reloaded. Activate the blueprint again.") from None
            return job["changes"]   # applied; its broadcast is still going out

    def reload(scenario: dict) -> dict:
        raw = _raw(scenario)
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        loop = live.loop
        if loop is not None and loop.is_running() and here is not loop:
            changes = on_loop(scenario, raw, loop)
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
    install_log_redaction()   # /ws?token= and X-Freeboard-Token never reach uvicorn's logs
    auth.warn_short_pin()
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
