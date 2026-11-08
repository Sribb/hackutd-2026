"""Readings history (CF-14): GET /api/history, the past on the same clock as the forecast.

The live layer (live.py, #38) keeps only its last 240 readings per signal. A Recorder
copies every new reading into a longer buffer (FREEBOARD_HISTORY_MIN live minutes, 720
by default), and the endpoint resamples it onto a regular grid that ends at "now".

A point is what the console showed at that moment: the latest reading at or before it,
while that reading was fresh. Fresh uses the live layer's own rule (stale after
`stale_after_min`, 5 by default), so a null point is exactly a SENSOR_STALE period, or a
time before the first reading. A stale period is always null: the last value is never
carried forward into it, and a number never stands in for a reading nobody has.

`timeline` has the same shape as POST /api/forecast's timeline, with t_min <= 0, so the
console draws the past and the future as one trend (CF-3): history.timeline, then the
forecast's. Activating a blueprint starts the history over, since its assets are new.
Without the live layer (or with FREEBOARD_ONE_CLOCK=0) there are no readings, so every
list is empty and `note` says why.

Reads only: no route here changes anything.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
from collections import deque
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from . import now

log = logging.getLogger("freeboard.history")

RETENTION_MIN = float(os.environ.get("FREEBOARD_HISTORY_MIN", "720"))
POLL_S = 0.5            # the live loop ticks every 1 s and keeps 240 readings, so nothing is missed
MAX_POINTS = 2000
EPS = 1e-6

router = APIRouter()


class Recorder:
    """Every reading the live layer took, for the last `retention_min` live minutes."""

    def __init__(self, retention_min: float = RETENTION_MIN) -> None:
        self.retention = float(retention_min)
        self._lock = threading.Lock()
        self._series: dict[tuple, deque] = {}
        self._last_t: dict[tuple, float] = {}
        self._registry = None
        self.started_t: Optional[float] = None

    def reset(self) -> None:
        with self._lock:
            self._series.clear()
            self._last_t.clear()
            self._registry = None
            self.started_t = None

    def sync(self, live) -> int:
        """Copy the readings the live layer took since the last call. Call it on the event
        loop the live layer ticks on, so a tick is never read halfway. A new connector
        registry means a blueprint was activated: the history starts over. Returns how
        many readings were added."""
        added = 0
        with self._lock:
            registry = getattr(live, "registry", None)
            if registry is not self._registry or self.started_t is None:
                self._series.clear()
                self._last_t.clear()
                self._registry = registry
                self.started_t = float(live.t)
            cutoff = float(live.t) - self.retention
            for key, hist in list(live.history.items()):
                last = self._last_t.get(key, -math.inf)
                fresh = []
                for t, v in reversed(hist):   # newest first; stop at what we already have
                    if t <= last:
                        break
                    fresh.append((float(t), v))
                buf = self._series.get(key)
                if fresh:
                    if buf is None:
                        buf = self._series[key] = deque()
                    buf.extend(reversed(fresh))
                    self._last_t[key] = buf[-1][0]
                    added += len(fresh)
                while buf and buf[0][0] < cutoff:
                    buf.popleft()
        return added

    def backfill(self, key: tuple, readings) -> int:
        """Put older (t, value) pairs, such as a historian's archive, in front of what was
        recorded for `key`. Returns how many were added.

        Only pairs older than the first recorded reading, and older than the moment
        recording started (started_t), are taken. So a recorded live reading is never
        overwritten, nothing is added twice (calling it again with the same pairs adds
        nothing), a live reading synced later always lands after the backfill, and a
        period since recording started with no live reading stays null, as it was on the
        console. Pairs with the same t keep the first one. A None,
        NaN, or inf value, or a t that isn't a finite number, is dropped, so a gap stays
        a gap. Pairs older than `retention_min` before the newest reading are dropped.

        Call it after sync(live): sync() starts the history over when it sees a new
        connector registry (or on its first call), and that would drop the backfill too.
        Before the first sync it adds nothing and returns 0."""
        pts: dict[float, object] = {}
        for item in readings or ():
            try:
                t, v = item
                t = float(t)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(t) or v is None:
                continue
            if isinstance(v, float) and not math.isfinite(v):
                continue
            pts.setdefault(t, v)
        with self._lock:
            if self.started_t is None:
                return 0
            buf = self._series.get(key)
            first = min(buf[0][0] if buf else math.inf, self.started_t)
            older = sorted((t, v) for t, v in pts.items() if t < first - EPS)
            if not older:
                return 0
            newest = buf[-1][0] if buf else older[-1][0]
            older = [p for p in older if p[0] >= newest - self.retention]
            if not older:
                return 0
            if buf is None:
                buf = self._series[key] = deque()
            buf.extendleft(reversed(older))
            return len(older)

    def readings(self, key: tuple) -> list:
        with self._lock:
            return list(self._series.get(key, ()))

    def keys(self) -> set:
        with self._lock:
            return set(self._series)


_recorder = Recorder()


def recorder() -> Recorder:
    return _recorder


async def run(app) -> None:
    """Background task (main.py's lifespan): keep the recorder up to date."""
    while True:
        await asyncio.sleep(POLL_S)
        live = getattr(getattr(app, "state", None), "live", None)
        if live is None:
            continue
        try:
            _recorder.sync(live)
        except Exception:  # noqa: BLE001 - keep recording through one bad pass
            log.exception("history sync failed")


# ---------- resampling ----------

def _clean(v):
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return round(f, 2) if math.isfinite(f) else None


def resample(readings: list, times: list[float], stale_after: float) -> list:
    """The value at each time: the latest reading at or before it, if it was fresh then
    (no older than stale_after). Otherwise None. Readings are (t, value) in time order."""
    out, i, n = [], -1, len(readings)
    for tk in times:
        while i + 1 < n and readings[i + 1][0] <= tk + EPS:
            i += 1
        if i >= 0 and tk - readings[i][0] <= stale_after + EPS:
            out.append(_clean(readings[i][1]))
        else:
            out.append(None)
    return out


def _gaps(aid: str, sig: str, t_mins: list[float], values: list, since_t_min: float) -> list[dict]:
    """Runs of null after recording started: periods with no fresh reading."""
    gaps, start = [], None
    for tm, v in zip(t_mins, values):
        if v is None and tm >= since_t_min - EPS:
            start = tm if start is None else start
        elif start is not None:
            gaps.append({"asset_id": aid, "signal": sig, "from_t_min": start, "to_t_min": prev})
            start = None
        prev = tm
    if start is not None:
        gaps.append({"asset_id": aid, "signal": sig, "from_t_min": start, "to_t_min": t_mins[-1]})
    return gaps


def _split(text: Optional[str]) -> list[str]:
    return [p.strip() for p in (text or "").split(",") if p.strip()]


def _round_t(x: float) -> float:
    r = round(x, 2)
    return int(r) if r == int(r) else r


@router.get("/api/history")
async def get_history(request: Request,
                      minutes: float = Query(60, gt=0, le=1440),
                      step_min: float = Query(1, ge=0.1, le=60),
                      asset_id: Optional[str] = None,
                      signal: str = "level_pct") -> dict:
    """Readings on a regular grid ending now. `asset_id` and `signal` take comma lists;
    `signal=all` is every signal recorded. Tanks' level_pct is also `timeline`."""
    points = int(math.floor(minutes / step_min + EPS))
    if points + 1 > MAX_POINTS:
        raise HTTPException(422, f"That's {points + 1} points. Ask for at most {MAX_POINTS}: "
                                 f"fewer minutes or a bigger step_min.")
    live = now.live_of(request.app)
    if live is None:
        n = now.get(request.app)
        return {"clock": n.plant.start_clock, "clock_source": n.source, "t": None,
                "minutes": minutes, "step_min": step_min, "stale_after_min": None, "since_t_min": None,
                "timeline": [], "series": [], "gaps": [],
                "note": "No live layer, so there are no readings to show. The forecast starts from the scenario."}

    rec = _recorder
    rec.sync(live)                                 # include the readings of the last tick
    now_t = float(live.t)
    field_ids = set(live.fmap.all_ids())
    want_assets = [a.upper() for a in _split(asset_id)]
    unknown = [a for a in want_assets if a not in field_ids]
    if unknown:
        raise HTTPException(404, f"No asset {unknown[0]} in the active field.")
    assets = set(want_assets) or None
    sigs = set(_split(signal)) or {"level_pct"}
    recorded = rec.keys()
    if "all" in sigs:
        sigs = {s for _, s in recorded} | {"level_pct"}

    tanks = [tid for tid in live.fmap.tanks if assets is None or tid in assets]
    keys = {k for k in recorded if k[1] in sigs and k[0] in field_ids and (assets is None or k[0] in assets)}
    if "level_pct" in sigs:
        keys |= {(tid, "level_pct") for tid in tanks}   # a blind tank is still listed, all null

    stale_after = float(getattr(live.detector, "stale_after", 5.0))
    t_mins = [_round_t(-minutes + k * step_min) for k in range(points + 1)]
    if t_mins[-1] != 0:                       # the grid always ends exactly at now
        t_mins.append(0)
    times = [now_t + tm for tm in t_mins]
    since = max(-minutes, (rec.started_t if rec.started_t is not None else now_t) - now_t)

    series, gaps, by_key = [], [], {}
    for aid, sig in sorted(keys):
        values = resample(rec.readings((aid, sig)), times, stale_after)
        by_key[(aid, sig)] = values
        series.append({"asset_id": aid, "signal": sig, "points": [[tm, v] for tm, v in zip(t_mins, values)]})
        gaps += _gaps(aid, sig, t_mins, values, since)

    timeline = []
    if "level_pct" in sigs:
        timeline = [{"t_min": tm, "clock": live.fmap.clock(tk),
                     "tanks": {tid: by_key[(tid, "level_pct")][i] for tid in tanks}}
                    for i, (tm, tk) in enumerate(zip(t_mins, times))]

    return {"clock": live.fmap.clock(now_t), "clock_source": "live", "t": round(now_t, 2),
            "minutes": minutes, "step_min": step_min, "stale_after_min": stale_after,
            "since_t_min": _round_t(since), "timeline": timeline, "series": series, "gaps": gaps}
