"""Freeboard backend. Run from the repo root:

    uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import spacetime_sync
from .scenario import field_payload, load_scenario, state_payload

SCENARIO = load_scenario()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Mirror the starting state into SpacetimeDB without blocking startup.
    threading.Thread(target=spacetime_sync.push_state,
                     args=(state_payload(SCENARIO),), daemon=True).start()
    yield


app = FastAPI(title="Freeboard backend", lifespan=lifespan)

# The web console runs on a local dev server (Vite defaults to 5173).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/field")
def get_field() -> dict:
    return field_payload(SCENARIO)


@app.get("/api/state")
def get_state() -> dict:
    return state_payload(SCENARIO)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "spacetime": spacetime_sync.status}


@app.post("/api/spacetime/sync")
def spacetime_sync_now() -> dict:
    """Re-push the current state into SpacetimeDB, e.g. after republishing the module."""
    ok = spacetime_sync.push_state(state_payload(SCENARIO))
    return {"ok": ok, "spacetime": spacetime_sync.status}
