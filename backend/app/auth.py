"""Who may change things. Reads stay open; writes need a token once one is set.

Environment (never commit these values):
    FREEBOARD_TOKEN         operator token (npm run demo makes one per run)
    OPERATOR_PIN            a PIN the operator types (CF-15); also the operator role
    FREEBOARD_PHONE_TOKEN   phone token: confirm, reject, and ack only

With none of them set, writes stay open (today's behavior, so the demo can't break).
With any set, every non-GET request needs the header X-Freeboard-Token. A missing or
wrong token gets 401 {"detail": "Enter the operator PIN to send commands."}. The phone
token on any other write gets 403.

Separately, and always on: a write whose Origin header names a different host than the
server's (a page on another site posting to us) is refused with 403. A plain HTML form
can send text/plain or multipart across sites without a CORS preflight, so CORS alone
doesn't stop it.

request.state.role is "operator", "phone", or "open" (no tokens set), so handlers can
record who decided from the token, never from the body.
"""
from __future__ import annotations

import hmac
import os
import re
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

HEADER = "x-freeboard-token"
NEED_TOKEN = "Enter the operator PIN to send commands."
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# The only writes the phone may make.
PHONE_PATHS = re.compile(r"^/api/(actions/[^/]+/(confirm|reject)|alerts/[^/]+/ack)/?$")


def _secrets() -> list[tuple[str, str]]:
    out = []
    for env, role in (("FREEBOARD_TOKEN", "operator"), ("OPERATOR_PIN", "operator"),
                      ("FREEBOARD_PHONE_TOKEN", "phone")):
        v = os.environ.get(env, "").strip()
        if v:
            out.append((v, role))
    return out


def enabled() -> bool:
    return bool(_secrets())


def role_for(token: Optional[str]) -> Optional[str]:
    """'operator', 'phone', 'open' when no tokens are set, or None for a wrong token.
    The live layer's /ws uses this with its ?token= value."""
    secrets = _secrets()
    if not secrets:
        return "open"
    if not token:
        return None
    got = token.strip().encode()
    role = None
    for value, r in secrets:  # compare against every secret so timing doesn't leak which matched
        if hmac.compare_digest(got, value.encode()) and role != "operator":
            role = r
    return role


def _host(value: str) -> str:
    return (urlsplit(value if "//" in value else "//" + value).hostname or "").lower()


def cross_site(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin == "null"  # sandboxed iframes and file:// pages send "null"
    o = _host(origin)
    allowed = {h.strip().lower() for h in os.environ.get("FREEBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return not (o in LOCAL_HOSTS or o == _host(request.headers.get("host", "")) or o in allowed)


def check(request: Request) -> Optional[JSONResponse]:
    """None if the request may go ahead, else the refusal. Sets request.state.role."""
    if request.method in SAFE_METHODS:
        request.state.role = role_for(request.headers.get(HEADER)) or "anonymous"
        return None
    if cross_site(request):
        return JSONResponse(status_code=403, content={"detail": "Refused: this request came from another site."})
    role = role_for(request.headers.get(HEADER))
    if role is None:
        return JSONResponse(status_code=401, content={"detail": NEED_TOKEN})
    if role == "phone" and not PHONE_PATHS.match(request.url.path):
        return JSONResponse(status_code=403,
                            content={"detail": "The phone can only confirm, reject, and acknowledge. "
                                               "This needs the operator token."})
    request.state.role = role
    return None


def install(app: FastAPI) -> None:
    """Add before CORSMiddleware, so CORS stays the outer layer and the browser can read a 401."""
    @app.middleware("http")
    async def _auth(request: Request, call_next):
        refused = check(request)
        return refused if refused is not None else await call_next(request)
