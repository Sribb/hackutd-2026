"""Refuse writes that come from a page on another site (#105).

A plain HTML form on any website can POST text/plain or multipart to this server without
a CORS preflight, so CORS alone doesn't stop it. Browsers always send Origin on such a
POST, so a write whose Origin names a different host than the server's gets 403.

Allowed: no Origin (curl, devices, server-to-server webhooks), localhost, the server's own
host on any port (the console on :5173, the API on :8000, also on a LAN IP with --lan),
and hosts listed in FREEBOARD_ALLOWED_HOSTS=a,b (e.g. a tunnel).

Tokens are a separate check: backend/app/auth.py, per route.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REFUSED = "Refused: this request came from another site."


def _host(value: str) -> str:
    try:
        return (urlsplit(value if "//" in value else "//" + value).hostname or "").lower()
    except ValueError:
        return ""


def cross_site(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return False
    if origin == "null":  # sandboxed iframes and file:// pages
        return True
    o = _host(origin)
    allowed = {h.strip().lower() for h in os.environ.get("FREEBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return not (o in LOCAL_HOSTS or o == _host(request.headers.get("host", "")) or o in allowed)


def install(app: FastAPI) -> None:
    """Add before CORSMiddleware, so CORS stays the outer layer and the browser can read the 403."""
    @app.middleware("http")
    async def _origin_guard(request: Request, call_next):
        if request.method not in SAFE_METHODS and cross_site(request):
            return JSONResponse(status_code=403, content={"detail": REFUSED})
        return await call_next(request)
