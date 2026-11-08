"""Security events (#126): wrong tokens, refused writes, and rejected uploads, kept for the
console and the audit log. Alerts stay on the server's own screens and in its audit log:
no SMS, no outside service.

A pure ASGI middleware watches what the server answers and never changes it: every
message goes on exactly as it came. On /api/* it notes

    401                               auth_failed      missing or wrong token
    403 with origin_guard's refusal   cross_site       a write from a page on another site
    any other 403                     forbidden_role   a token without the role, e.g. the phone
                                                       token on an operator route
    429                               lockout          too many requests: the PIN lockout (auth.py's,
                                                       #57; this only records it), or /api/ingest's
                                                       Claude rate limit
    413 or 415                        upload_rejected  an upload too large, or of a type the server
                                                       doesn't take

and on /ws, a close with code 1008 (ws_auth_failed: a wrong ?token=).

An event:

    {"id": 3, "kind": "auth_failed", "method": "POST", "path": "/api/actions/act_1/confirm",
     "client": "192.168.1.x", "first_t": "2026-11-07T07:12:03+00:00",
     "last_t": "2026-11-07T07:12:41+00:00", "count": 4, "detail": "401: missing or wrong token."}

It holds only what is safe to show on an open read. `path` never has a query string, so
a ?token= never reaches the log. `client` has the last IPv4 octet masked, or an IPv6
address cut to its /48. `detail` is written here from the status code. Headers, bodies,
tokens, and PINs are never recorded: the start of a 403 body is searched for
origin_guard's refusal and then dropped.

No repeat alerts: the same (kind, path, client) within FREEBOARD_SECURITY_DEDUPE_S (60)
of an event's first_t adds to its count and last_t instead of adding an event. Only that
first occurrence is an alert: an audit entry (app.state.audit.append("security", ...))
and a {"type": "security", "event": {...}} message to every screen on /ws. At most
ALERTS_PER_MIN alerts go out in any minute, so a flood of distinct paths can't fill the
audit log or bury the screens; those events are still kept here. The log keeps the
last CAP (500) events, in memory.

    GET /api/security/events?since=<id>   {"events": [...]}, oldest first. Open, like every read.

main.py calls install(app) once, after origin_guard.install(app). Middleware added later
wraps what was added before, so this layer then sees origin_guard's 403s.
"""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import itertools
import logging
import math
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from fastapi import APIRouter, FastAPI, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import origin_guard
from .detect import now_iso

log = logging.getLogger("freeboard.security")

CAP = 500              # events kept
DEDUPE_S = 60.0        # FREEBOARD_SECURITY_DEDUPE_S when unset or not a number
ALERTS_PER_MIN = 30    # audit entries and /ws messages, at most, in any 60 s
PATH_MAX = 200         # characters of a path kept
SNIFF_BYTES = 1024     # of a 403 body, searched for origin_guard's refusal, then dropped
EVENTS_PATH = "/api/security/events"

REFUSED = origin_guard.REFUSED.encode()
HTTP_KINDS = {401: "auth_failed", 413: "upload_rejected", 415: "upload_rejected", 429: "lockout"}
DETAIL = {
    ("auth_failed", 401): "401: missing or wrong token.",
    ("cross_site", 403): "403: a write from a page on another site was refused.",
    ("forbidden_role", 403): "403: this token isn't allowed to do that.",
    ("lockout", 429): "429: too many requests, refused for now.",
    ("upload_rejected", 413): "413: the upload is too large.",
    ("upload_rejected", 415): "415: the upload's type isn't accepted.",
    ("ws_auth_failed", 1008): "1008: /ws refused the connection: wrong token.",
}


def dedupe_window() -> float:
    """FREEBOARD_SECURITY_DEDUPE_S, read each time so a test or a restart sees the current
    value. Anything that isn't a number of seconds, 0 or more, means the default."""
    raw = os.environ.get("FREEBOARD_SECURITY_DEDUPE_S", "").strip()
    if not raw:
        return DEDUPE_S
    try:
        v = float(raw)
    except ValueError:
        return DEDUPE_S
    return v if math.isfinite(v) and v >= 0 else DEDUPE_S


def clean_path(path: Any) -> str:
    """The path as recorded: no query string or fragment ever (the server decodes %3F, so a
    "?" can still turn up here), no control characters, at most PATH_MAX characters."""
    p = str(path or "").split("?", 1)[0].split("#", 1)[0]
    p = "".join(ch if ch.isprintable() else "_" for ch in p)
    return (p[:PATH_MAX] + "...") if len(p) > PATH_MAX else (p or "/")


def mask_client(client: Any) -> str:
    """The ASGI client, (host, port) or a host, as recorded: "192.168.1.x" for IPv4 (also
    when mapped into IPv6), the /48 for IPv6 ("2001:db8:85a3::/48"), else "unknown"."""
    host = client[0] if isinstance(client, (tuple, list)) and client else client
    try:
        ip = ipaddress.ip_address(str(host or "").split("%", 1)[0])
    except ValueError:
        return "unknown"
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 4:
        return str(ip).rsplit(".", 1)[0] + ".x"
    return str(ipaddress.ip_network(f"{ip}/48", strict=False))


class SecurityLog:
    """The last `cap` security events, oldest first, with repeats folded in. Thread safe:
    the middleware records on the event loop while reads may come from worker threads."""

    def __init__(self, cap: int = CAP, dedupe_s: Optional[float] = None,
                 alerts_per_min: int = ALERTS_PER_MIN, clock: Callable[[], float] = time.monotonic) -> None:
        """dedupe_s None reads FREEBOARD_SECURITY_DEDUPE_S on every event. `clock` counts
        seconds and never jumps (time.monotonic); tests pass a fake one."""
        self.cap = max(1, int(cap))
        self.dedupe_s = dedupe_s
        self.alerts_per_min = max(0, int(alerts_per_min))
        self.clock = clock
        self._events: deque[dict] = deque()
        self._open: dict[tuple, tuple[dict, float]] = {}   # (kind, path, client) -> (newest event, its start)
        self._alerts: deque[float] = deque()               # when each alert of the last minute went out
        self._paused = False
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def window_s(self) -> float:
        return dedupe_window() if self.dedupe_s is None else max(0.0, float(self.dedupe_s))

    def record(self, kind: str, method: str, path: Any, client: Any, detail: str) -> tuple[dict, bool]:
        """Note one occurrence. `path` and `client` are cleaned here (clean_path, mask_client).

        Returns (a copy of its event, alert). alert is True only for the first occurrence of
        (kind, path, client) in its window, and only while fewer than alerts_per_min alerts
        went out in the last minute."""
        path, client = clean_path(path), mask_client(client)
        window = self.window_s()
        key = (kind, path, client)
        paused_now = False
        with self._lock:
            now, stamp = self.clock(), now_iso()
            hit = self._open.get(key)
            if hit is not None and now - hit[1] < window:
                ev = hit[0]
                ev["count"] += 1
                ev["last_t"] = stamp
                return dict(ev), False
            ev = {"id": next(self._ids), "kind": kind, "method": method, "path": path, "client": client,
                  "first_t": stamp, "last_t": stamp, "count": 1, "detail": detail}
            self._events.append(ev)
            self._open[key] = (ev, now)
            while len(self._events) > self.cap:
                old = self._events.popleft()
                old_key = (old["kind"], old["path"], old["client"])
                if self._open.get(old_key, (None, 0.0))[0] is old:
                    del self._open[old_key]
            while self._alerts and now - self._alerts[0] >= 60.0:
                self._alerts.popleft()
            alert = len(self._alerts) < self.alerts_per_min
            if alert:
                self._alerts.append(now)
                self._paused = False
            elif not self._paused:
                self._paused = paused_now = True
            out = dict(ev)
        if paused_now:
            log.warning("Security alerts paused: %d went out in the last minute. New events are still "
                        "kept at %s.", self.alerts_per_min, EVENTS_PATH)
        return out, alert

    def events(self, since: int = 0) -> list[dict]:
        """Copies, oldest first; `since` skips events with id <= since. count and last_t of
        an event keep changing during its window, so read without `since` to see them."""
        with self._lock:
            return [dict(e) for e in self._events if e["id"] > since]

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


class SecurityLogMiddleware:
    """Pure ASGI. Passes every message through untouched and notes the ones above.

    It reads app.state.audit and app.state.live when an alert goes out, not at install, so
    the live layer may be mounted before or after it."""

    def __init__(self, app: ASGIApp, seclog: SecurityLog, state: Any) -> None:
        self.app = app
        self.seclog = seclog
        self.state = state
        self._broadcasts: set = set()   # tasks in flight, so none is garbage collected early

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        typ, path = scope.get("type"), scope.get("path") or ""
        if typ == "http" and path.startswith("/api/"):
            await self._http(scope, receive, send)
        elif typ == "websocket" and (path == "/ws" or path.startswith("/ws/")):
            await self._websocket(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        head = bytearray()
        pending = False   # a 403 whose body hasn't yet said whether origin_guard sent it

        def settle_403() -> None:
            nonlocal pending
            pending = False
            self._note(scope, "cross_site" if REFUSED in head else "forbidden_role", 403)

        async def watch(message: Message) -> None:
            nonlocal pending
            try:
                typ = message.get("type")
                if typ == "http.response.start":
                    status = int(message.get("status") or 0)
                    if status == 403:
                        pending = True
                    elif status in HTTP_KINDS:
                        self._note(scope, HTTP_KINDS[status], status)
                elif typ == "http.response.body" and pending:
                    room = SNIFF_BYTES - len(head)
                    if room > 0:
                        head.extend((message.get("body") or b"")[:room])
                    if not message.get("more_body", False) or len(head) >= SNIFF_BYTES:
                        settle_403()
            except Exception:  # noqa: BLE001 - the log must never break a response
                log.exception("Security log could not note a response")
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            if pending:   # the body never finished (the client left, or the app raised)
                try:
                    settle_403()
                except Exception:  # noqa: BLE001
                    log.exception("Security log could not note a response")

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def watch(message: Message) -> None:
            try:
                if message.get("type") == "websocket.close" and message.get("code") == 1008:
                    self._note(scope, "ws_auth_failed", 1008)
            except Exception:  # noqa: BLE001
                log.exception("Security log could not note a websocket close")
            await send(message)

        await self.app(scope, receive, watch)

    def _note(self, scope: Scope, kind: str, code: int) -> None:
        method = "WS" if scope.get("type") == "websocket" else str(scope.get("method") or "")[:16]
        event, alert = self.seclog.record(kind, method, scope.get("path"), scope.get("client"),
                                          DETAIL.get((kind, code), str(code)))
        if alert:
            self._alert(event)

    def _alert(self, event: dict) -> None:
        """The audit entry and the /ws message for a new event. Never raises, never waits:
        the hub's broadcast is async, so it is scheduled on this loop and left to run."""
        log.info("Security event %s: %s %s from %s", event["kind"], event["method"], event["path"],
                 event["client"])
        audit = getattr(self.state, "audit", None)
        if audit is not None:
            try:
                audit.append("security", source="security", kind=event["kind"], path=event["path"],
                             client=event["client"])
            except Exception:  # noqa: BLE001 - the audit log must never break a response
                log.warning("Security event not written to the audit log", exc_info=True)
        hub = getattr(getattr(self.state, "live", None), "hub", None)
        if hub is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # not on an event loop: nothing could carry the message
            return
        try:
            pending = hub.broadcast("security", {"event": event})
            if inspect.isawaitable(pending):
                task = asyncio.ensure_future(pending, loop=loop)
                self._broadcasts.add(task)
                task.add_done_callback(self._broadcast_done)
        except Exception:  # noqa: BLE001 - nor may the hub
            log.warning("Security event not broadcast", exc_info=True)

    def _broadcast_done(self, task: asyncio.Future) -> None:
        self._broadcasts.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("Security event broadcast failed: %s", task.exception())


router = APIRouter()


@router.get(EVENTS_PATH)
async def get_security_events(request: Request, since: int = 0) -> dict:
    """Security events, oldest first; `since` skips those with id <= since. Open, like
    every read: no event holds a header, body, token, PIN, query string, or full address."""
    seclog = getattr(request.app.state, "security_log", None)
    return {"events": seclog.events(since) if seclog is not None else []}


def _wraps_origin_guard(app: FastAPI) -> bool:
    """Whether origin_guard's middleware is already on the app, so ours goes outside it."""
    try:
        return any(getattr(m.kwargs.get("dispatch"), "__module__", "") == origin_guard.__name__
                   for m in app.user_middleware)
    except Exception:  # noqa: BLE001 - only a hint for the log
        return True


def install(app: FastAPI, seclog: Optional[SecurityLog] = None) -> SecurityLog:
    """Watch /api/* and /ws, and serve GET /api/security/events. Returns the log, which is
    also app.state.security_log. Call it once, after origin_guard.install(app); a second
    call returns the log already installed and adds nothing."""
    current = getattr(app.state, "security_log", None)
    if current is not None:
        return current
    if not _wraps_origin_guard(app):
        log.warning("security_log.install ran before origin_guard.install, so cross-site refusals "
                    "won't be seen. Install it after.")
    seclog = seclog if seclog is not None else SecurityLog()
    app.state.security_log = seclog
    app.add_middleware(SecurityLogMiddleware, seclog=seclog, state=app.state)
    if not any(getattr(r, "path", None) == EVENTS_PATH for r in app.router.routes):
        app.include_router(router)
    return seclog
