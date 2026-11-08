"""FastAPI route for Last Call. Mount it in the backend with:

    from lastcall.router import router as lastcall_router
    app.include_router(lastcall_router)

Until the backend has its own scenario loader and forecast, this serves the
placeholder field in lastcall/scenario.json with the reference forecast in
lastcall/sim.py. To use the real forecast, call set_source() at startup.
"""

from __future__ import annotations

import os
from typing import Callable, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from . import core

router = APIRouter()

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIELD = os.path.join(os.path.dirname(_HERE), "scenario", "field.json")
_get_scenario: Callable[[], dict] = lambda: core.load_scenario(
    _FIELD if os.path.exists(_FIELD) else os.path.join(_HERE, "scenario.json"))
_forecast: Optional[Callable[[List[dict], float], dict]] = None


def set_source(get_scenario: Callable[[], dict],
               forecast_fn: Optional[Callable[[List[dict], float], dict]] = None) -> None:
    """Point Last Call at the live scenario and, optionally, the backend's own forecast."""
    global _get_scenario, _forecast
    _get_scenario, _forecast = get_scenario, forecast_fn


class LastCallRequest(BaseModel):
    horizon_minutes: int = 360
    step_minutes: int = 5
    top_n: int = 3


@router.post("/api/lastcall")
def lastcall(req: LastCallRequest = LastCallRequest()) -> dict:
    return core.last_call(_get_scenario(), _forecast, req.horizon_minutes, req.step_minutes, req.top_n)
