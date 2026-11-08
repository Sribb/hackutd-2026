"""Dataset mode API under /api/eog (docs/CONTRACT.md, "Dataset mode (EOG sponsor data)").

A thin layer over service.py. Every GET takes an optional ?at= (ts_unix or ISO 8601 UTC,
clamped into the dataset's range) and otherwise reads the data as of the clock cursor. Every
error is one plain sentence in `detail`: 503 while dataset mode is off, 404 for an unknown id,
422 for bad input. POST routes need the operator token where backend/app/auth.py exists;
without it they stay open, because the server binds 127.0.0.1. An auth.py that exists but
can't be used refuses them (503) instead of opening them.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import service

log = logging.getLogger("freeboard.eog")

_AUTH = __name__.rsplit(".", 2)[0] + ".auth"


def _open() -> None:
    """No auth.py on this branch: POST routes stay open (the server binds 127.0.0.1)."""
    return None


def _refusing(sentence: str) -> Callable[[], None]:
    def refuse() -> None:
        raise HTTPException(503, sentence)
    return refuse


def _operator_dependency() -> tuple[Callable[..., Any], Optional[str]]:
    """require_role("operator") from backend/app/auth.py (#103) where it exists, with the
    sentence explaining any problem. Only the file's absence opens the POST routes. An auth.py
    that can't be used closes them (503 with the reason) rather than opening them, and it
    never stops the rest of the backend from starting."""
    try:
        mod = importlib.import_module(_AUTH)
    except ModuleNotFoundError as e:
        if e.name == _AUTH:
            return _open, None
        problem = f"it imports {e.name}, which is missing"
    except Exception as e:  # noqa: BLE001 - any import failure closes the commands
        problem = f"it fails to import ({type(e).__name__}: {e})"
    else:
        require = getattr(mod, "require_role", None)
        if not callable(require):
            problem = "it has no require_role"
        else:
            try:
                return require("operator"), None
            except Exception as e:  # noqa: BLE001
                problem = f"require_role('operator') fails ({type(e).__name__}: {e})"
    sentence = f"Dataset-mode commands are refused because backend/app/auth.py can't be used: {problem}."
    log.error(sentence)
    return _refusing(sentence), sentence


_operator, AUTH_PROBLEM = _operator_dependency()


def _svc() -> service.Service:
    return service.SERVICE


def _on() -> None:
    """First dependency of every HTTP route: 503 with one sentence while dataset mode is off.
    It runs before the token check, so a server without data says so instead of asking for a PIN."""
    try:
        _svc().require_enabled()
    except service.ServiceError as e:
        raise HTTPException(e.status, e.sentence) from None


ON = [Depends(_on)]
router = APIRouter(prefix="/api/eog", tags=["dataset mode"])


def _json(fn: Callable[..., Any], *args: Any) -> JSONResponse:
    try:
        payload = fn(*args)
    except service.Stale as e:   # 409: the sentence, plus the fresh plan to show instead
        return JSONResponse(service.jsonable({"detail": e.sentence, **e.payload}), status_code=e.status)
    except service.ServiceError as e:
        raise HTTPException(e.status, e.sentence) from None
    return JSONResponse(service.jsonable(payload))


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


async def _body(request: Request, allowed: tuple[str, ...]) -> dict:
    """The JSON object body (empty means {}), with a sentence for anything else."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw, parse_constant=_no_constant)
    except ValueError:
        raise HTTPException(422, "The request body must be a JSON object.") from None
    if not isinstance(data, dict):
        raise HTTPException(422, "The request body must be a JSON object.")
    unknown = sorted(str(k) for k in data if k not in allowed)
    if unknown:
        raise HTTPException(422, f"Unknown field {unknown[0][:40]!r}; this route takes {', '.join(allowed)}.")
    return data


def _who(caller: Any, by: Optional[str]) -> Optional[str]:
    """Who approved: with tokens on, auth's device for the token (never the body); else the body's `by`."""
    decided = getattr(caller, "decided_by", None)
    return decided(by or "") if callable(decided) else by


# ---------- datasets and clock ----------

def _checked(at: Optional[str], fn: Callable[[], dict]) -> dict:
    """Every GET takes ?at=; these two always describe the real cursor, so it is only checked."""
    service.parse_ts(at)
    return fn()


@router.get("/datasets", dependencies=ON)
def get_datasets(at: Optional[str] = None) -> JSONResponse:
    """{enabled, active, datasets: [{id}], clock, warnings}."""
    return _json(_checked, at, _svc().datasets_payload)


@router.post("/datasets/activate", dependencies=ON)
async def post_activate(request: Request, caller: Any = Depends(_operator)) -> JSONResponse:
    body = await _body(request, ("id",))
    return await run_in_threadpool(_json, _svc().activate, body.get("id"))


@router.get("/clock", dependencies=ON)
def get_clock(at: Optional[str] = None) -> JSONResponse:
    """{start, now, last, step_s, at, playing, speed, ...}."""
    return _json(_checked, at, _svc().clock_payload)


@router.post("/clock", dependencies=ON)
async def post_clock(request: Request, caller: Any = Depends(_operator)) -> JSONResponse:
    body = await _body(request, ("at", "play", "speed"))
    return await run_in_threadpool(_json, _svc().set_clock, body)


# ---------- views ----------

@router.get("/network", dependencies=ON)
def get_network(at: Optional[str] = None) -> JSONResponse:
    return _json(_svc().network, at)


@router.get("/state", dependencies=ON)
def get_state(at: Optional[str] = None) -> JSONResponse:
    return _json(_svc().state, at)


@router.get("/alerts", dependencies=ON)
def get_alerts(at: Optional[str] = None) -> JSONResponse:
    return _json(_svc().alerts, at)


@router.get("/events", dependencies=ON)
def get_events(at: Optional[str] = None, download: Optional[str] = None) -> Response:
    """Every event up to `at` in SPEC's events.json shape; ?download=1 sends the bare list as events.json."""
    try:
        dl = bool(download and download.strip()) and service.parse_bool(download, "download")
        head, events = _svc().events_export(at)
    except service.ServiceError as e:
        raise HTTPException(e.status, e.sentence) from None
    if dl:
        return Response(service.dumps(events, indent=2) + "\n", media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="events.json"'})
    return JSONResponse(service.jsonable({**head, "count": len(events), "events": events}))


@router.get("/plans", dependencies=ON)
def get_plans(at: Optional[str] = None, mode: Optional[str] = None) -> JSONResponse:
    return _json(_svc().plans, at, mode)


@router.post("/whatif", dependencies=ON)
async def post_whatif(request: Request, caller: Any = Depends(_operator)) -> JSONResponse:
    body = await _body(request, ("facility_id", "actions", "at"))
    return await run_in_threadpool(_json, _svc().whatif, body)


@router.get("/series", dependencies=ON)
def get_series(asset_id: Optional[str] = None, metric: Optional[str] = None,
               from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
               max_points: Optional[str] = None, at: Optional[str] = None) -> JSONResponse:
    return _json(_svc().series, asset_id, metric, from_, to, max_points, at)


@router.get("/handoff", dependencies=ON)
def get_handoff(at: Optional[str] = None, shift_hours: Optional[str] = None) -> JSONResponse:
    return _json(_svc().handoff, at, shift_hours)


@router.post("/plans/{event_id}/approve", dependencies=ON)
async def post_approve(event_id: str, request: Request, caller: Any = Depends(_operator)) -> JSONResponse:
    body = await _body(request, ("by", "at", "plan_at"))
    by = body.get("by")
    if by is not None and not isinstance(by, str):
        raise HTTPException(422, "by must be text: the name of who approved the plan.")
    return await run_in_threadpool(_json, _svc().approve, event_id, _who(caller, by), body.get("at"),
                                   body.get("plan_at"))


# ---------- WebSocket ----------

def _error(status: int, text: str) -> dict:
    return {"type": "error", "status": status, "detail": text}


async def _send(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_text(service.dumps(payload))
        return True
    except (WebSocketDisconnect, RuntimeError, OSError):
        return False


@router.websocket("/ws")
async def ws_ticks(ws: WebSocket) -> None:
    """Pushes {"type": "tick", dataset, clock, alerts} on connect and whenever the cursor moves
    (a new data row while playing, any move while paused), play/pause or speed change, the
    dataset changes, or a plan is approved. Read-only, so it needs no token. When the alerts
    can't be built the tick still carries the clock, with alerts null and an `error`. A problem
    with the dataset itself arrives as {"type": "error", status, detail}; with dataset mode off
    the socket then closes with 1013."""
    await ws.accept()
    svc = _svc()
    try:
        await run_in_threadpool(svc.require_enabled)
    except service.ServiceError as e:
        await _send(ws, _error(e.status, e.sentence))
        await ws.close(code=1013)   # try again later
        return
    recv = asyncio.ensure_future(ws.receive())
    last: Any = None
    said: Optional[str] = None      # the last error sent, so a lasting problem is sent once
    try:
        while True:
            payload, sig = None, None
            try:
                sig = await run_in_threadpool(svc.signature)
                if sig != last:
                    payload, last = await run_in_threadpool(svc.tick)
                    said = None
            except service.ServiceError as e:
                last = None
                if e.sentence != said:
                    payload, said = _error(e.status, e.sentence), e.sentence
            except Exception as e:  # noqa: BLE001 - one bad moment must not end the stream
                text = f"Internal error: {type(e).__name__}: {e}"
                last = sig          # retry only once something changes
                if text != said:
                    log.exception("dataset-mode tick failed")
                    payload, said = _error(500, text), text
            if payload is not None and not await _send(ws, payload):
                break
            done, _ = await asyncio.wait({recv}, timeout=svc.poll_s)
            if recv in done:
                if recv.result().get("type") == "websocket.disconnect":
                    break
                recv = asyncio.ensure_future(ws.receive())   # client messages are ignored
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        recv.cancel()
