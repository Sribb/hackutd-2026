"""Control tokens for every write (#103, CF-15). One mechanism for the QR token and the PIN.

    FREEBOARD_TOKEN        operator token (the console's #k=... link)
    OPERATOR_PIN           also operator: a short PIN a person can type (CF-5's prompt)
    FREEBOARD_PHONE_TOKEN  phone token (the QR's #k=...): confirm, reject, ack, shelve, unshelve only

With none of them set, nothing changes: every route is open, as before. With any of them
set, a write needs the header `X-Freeboard-Token`:
    missing or wrong          -> 401 {"detail": "Enter the operator PIN to send commands."}
    phone token, operator route -> 403
Reads (GET) stay open. Tokens are compared with hmac.compare_digest and never logged.

Wrong tokens are throttled (THROTTLE), so a short OPERATOR_PIN can't be guessed by brute
force: 5 wrong tokens in 60 s from one client IP lock that IP out for 60 s, then 120 s,
240 s, ... up to 15 min: 429 {"detail": "Too many wrong PINs. Wait N s."} with Retry-After.
30 wrong tokens in 60 s from all IPs together lock out, the same way, every IP that hasn't
used a correct token in the last day. A correct token from another IP keeps working. Each
counted failure goes to the audit log as `auth_failed` (client and path, never the value
tried). A missing token is the PIN prompt, not a guess, so it is not counted. Use a PIN of
6 or more digits.

Use it on any route, in any lane:

    from backend.app.auth import Caller, require_role
    @app.post("/api/blueprints/{id}/activate")
    def activate(id: str, caller: Caller = Depends(require_role("operator"))): ...

`caller.role` is "operator", "phone", or None (no token configured). `caller.device` is
"console" or "phone" (None when open) and is what `decided_by` and the audit log record.
The dependency also sets `request.state.role`.
"""
from __future__ import annotations

import hmac
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import Header, HTTPException, Request

log = logging.getLogger("freeboard.auth")

DENIED = "Enter the operator PIN to send commands."
OPERATOR, PHONE = "operator", "phone"
ENV = ((OPERATOR, "FREEBOARD_TOKEN"), (OPERATOR, "OPERATOR_PIN"), (PHONE, "FREEBOARD_PHONE_TOKEN"))
DEVICE = {OPERATOR: "console", PHONE: "phone"}


@dataclass(frozen=True)
class Caller:
    role: Optional[str] = None   # None: no token is configured, so the route is open

    @property
    def device(self) -> Optional[str]:
        return DEVICE.get(self.role or "")

    def decided_by(self, by: str = "") -> Optional[str]:
        """Who decided, as stored on a proposal. With tokens on it comes from the role and
        never from the request body; with tokens off it is the body's `by`, as before."""
        return self.device if self.role else (by or None)

    def source(self, by: str = "") -> str:
        """console or phone, for the audit log."""
        if self.role:
            return DEVICE[self.role]
        return "phone" if (by or "").strip().lower() == "phone" else "console"


def _secrets() -> list[tuple[str, bytes]]:
    """Read at call time, so a test (or a restart with new env) sees the current values."""
    out = []
    for role, var in ENV:
        v = os.environ.get(var, "").strip()
        if v:
            out.append((role, v.encode()))
    return out


def enabled() -> bool:
    return bool(_secrets())


def role_for(token: Optional[str]) -> Optional[str]:
    """The role a token grants, or None. Checks every secret so the time doesn't say which matched."""
    got = (token or "").strip().encode()
    found = None
    for role, secret in _secrets():
        if hmac.compare_digest(got, secret) and found is None:
            found = role
    return found if got else None


# ---------- throttling wrong tokens ----------

class Throttle:
    """Counts wrong tokens per client IP and overall, and says how long a client must wait.

    `limit` wrong tokens within `window_s` from one client lock it for `base_s`, doubling on
    each lock up to `max_s` (the doubling starts over once a lock has been over for `max_s`).
    `global_limit` wrong tokens within `window_s` from everyone together lock, the same way,
    every client that hasn't used a correct token in the last `good_s`. A waiting client is
    refused without its token being checked, so it learns nothing while it waits.
    Thread-safe: sync routes run in worker threads."""

    MAX_CLIENTS = 4096   # prune forgotten clients past this many

    def __init__(self, limit: int = 5, window_s: float = 60.0, base_s: float = 60.0, max_s: float = 900.0,
                 global_limit: int = 30, good_s: float = 86400.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window_s, self.base_s, self.max_s = limit, window_s, base_s, max_s
        self.global_limit, self.good_s, self.clock = global_limit, good_s, clock
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._fails: dict[str, list[float]] = {}   # client -> times of recent wrong tokens
            self._until: dict[str, float] = {}         # client -> end of its lock
            self._level: dict[str, int] = {}           # client -> locks so far (the doubling)
            self._good: dict[str, float] = {}          # client -> last correct token
            self._gfails: list[float] = []
            self._guntil = 0.0
            self._glevel = 0

    def _lock_for(self, level: int) -> float:
        return min(self.base_s * (2 ** level), self.max_s)

    def wait_s(self, client: str) -> int:
        """Whole seconds this client must wait before its next token is checked (0: go ahead)."""
        now = self.clock()
        with self._lock:
            left = self._until.get(client, 0.0) - now
            good = now - self._good.get(client, -math.inf) < self.good_s
            if left <= 0 and not good:
                left = self._guntil - now
        return math.ceil(left) if left > 0 else 0

    def failed(self, client: str) -> int:
        """Count one wrong token. Returns the seconds of the lock it started, else 0."""
        now = self.clock()
        with self._lock:
            if len(self._fails) > self.MAX_CLIENTS:
                self._prune(now)
            if now - self._until.get(client, -math.inf) > self.max_s:
                self._level[client] = 0
            fails = [t for t in self._fails.get(client, []) if now - t < self.window_s] + [now]
            locked = 0.0
            if len(fails) >= self.limit:
                level = self._level.get(client, 0)
                locked = self._lock_for(level)
                self._until[client] = now + locked
                self._level[client] = level + 1
                fails = []
            self._fails[client] = fails
            if now - self._guntil > self.max_s:
                self._glevel = 0
            g = [t for t in self._gfails if now - t < self.window_s] + [now]
            if len(g) >= self.global_limit:
                self._guntil = now + self._lock_for(self._glevel)
                self._glevel += 1
                g = []
            self._gfails = g
        return math.ceil(locked)

    def succeeded(self, client: str) -> None:
        """A correct token. It never clears the client's count (a phone token must not reset
        it while that phone guesses the PIN); it only lets this client through a global lock."""
        with self._lock:
            self._good[client] = self.clock()

    def _prune(self, now: float) -> None:
        stale = [c for c, f in self._fails.items()
                 if not any(now - t < self.window_s for t in f)
                 and now - self._until.get(c, -math.inf) > self.max_s]
        for c in stale:
            self._fails.pop(c, None)
            self._until.pop(c, None)
            self._level.pop(c, None)


THROTTLE = Throttle()


def too_many(seconds: int) -> HTTPException:
    return HTTPException(429, f"Too many wrong PINs. Wait {seconds} s.", headers={"Retry-After": str(seconds)})


def _count_failure(client: str, audit: Any, path: str) -> None:
    """Count a wrong token and log it. Never the value that was tried."""
    locked = THROTTLE.failed(client)
    log.warning("wrong control token from %s on %s%s", client, path, f", locked for {locked} s" if locked else "")
    if audit is None:
        return
    try:
        audit.append("auth_failed", source=client, role=None, path=path, **({"locked_s": locked} if locked else {}))
    except Exception:  # noqa: BLE001 - the audit log must never change the answer
        log.exception("audit append failed")


def check(token: Optional[str], *allowed: str, client: Optional[str] = None, audit: Any = None,
          path: str = "") -> Caller:
    """The caller for this token, or HTTPException 401/403/429. Operator may do everything.
    With `client` (the caller's IP) wrong tokens are throttled, and each counted one goes to
    `audit` (an AuditLog) as auth_failed."""
    if not enabled():
        return Caller(None)
    if not (token or "").strip():
        raise HTTPException(401, DENIED)   # the PIN prompt, not a guess: not counted
    if client is not None:
        wait = THROTTLE.wait_s(client)
        if wait:
            raise too_many(wait)
    role = role_for(token)
    if role is None:
        if client is not None:
            _count_failure(client, audit, path)
        raise HTTPException(401, DENIED)
    if client is not None:
        THROTTLE.succeeded(client)
    if role != OPERATOR and role not in allowed:
        raise HTTPException(403, f"The {role} token can't do this. Enter the operator PIN.")
    return Caller(role)


def client_of(conn: Any) -> str:
    """The client IP of a Request or WebSocket. Behind a proxy every client shares its IP."""
    c = getattr(conn, "client", None)
    return (c.host if c else None) or "unknown"


def require_role(*allowed: str) -> Callable[..., Caller]:
    """FastAPI dependency. require_role("operator") for operator only; require_role("phone")
    for phone or operator (operator can always do what a phone can). Sets request.state.role."""
    for r in allowed:
        if r not in DEVICE:
            raise ValueError(f"Unknown role {r!r}. Use 'operator' or 'phone'.")

    def dependency(request: Request, x_freeboard_token: Optional[str] = Header(None)) -> Caller:
        caller = check(x_freeboard_token, *allowed, client=client_of(request),
                       audit=getattr(request.app.state, "audit", None), path=request.url.path)
        request.state.role = caller.role
        return caller

    return dependency


def ws_caller(token: Optional[str], client: Optional[str] = None, audit: Any = None,
              path: str = "/ws") -> tuple[bool, Caller]:
    """For /ws. (ok, caller): with tokens on, a wrong token is not ok (close 1008), and no
    token connects read-only (role None, so its `ui` messages are ignored). With `client`,
    wrong tokens are throttled like HTTP ones: check ws_wait_s(client) first."""
    if not enabled():
        return True, Caller(None)
    if not (token or "").strip():
        return True, Caller(None)
    if client is not None and THROTTLE.wait_s(client):
        return False, Caller(None)
    role = role_for(token)
    if client is not None:
        if role is None:
            _count_failure(client, audit, path)
        else:
            THROTTLE.succeeded(client)
    return role is not None, Caller(role)


def ws_wait_s(client: str) -> int:
    """Seconds a /ws client must wait before its token is checked (0 when tokens are off)."""
    return THROTTLE.wait_s(client) if enabled() else 0


def may_steer(caller: Caller) -> bool:
    """Whether a /ws client's `ui` messages move every screen."""
    return not enabled() or caller.role in DEVICE


def warn_short_pin() -> None:
    """live_api.install() calls this: a PIN under 6 characters is easy to guess."""
    pin = os.environ.get("OPERATOR_PIN", "").strip()
    if pin and len(pin) < 6:
        log.warning("OPERATOR_PIN is shorter than 6 characters. Use 6 or more digits.")
