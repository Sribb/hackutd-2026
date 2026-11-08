"""SpacetimeDB mirror (#126): the live field, its alerts, proposals, and connectors, one
batch per tick.

Every SPACETIME_MIRROR_S seconds (default 1) run() reads, on the event loop so it never
sees half a tick:
  - now.state(app): the clock, the incident, and every asset, from the same moment as
    /api/state and the forecast (#42);
  - the live layer's alerts, proposals, and connector status (app.state.live), when it
    is mounted. Without it those tables are empty.
It compares each row with what SpacetimeDB last accepted and sends only what changed, in
one apply_batch call (spacetime_sync.push_batch) on a worker thread, so a slow or dead
SpacetimeDB never blocks the loop. A failed push keeps every change for the next try,
which waits longer after each failure (up to BACKOFF_MAX_S). The mirror never raises.

A full batch lists every row, and the module deletes the rows it doesn't list. It goes:
  - first, so assets, alerts, and proposals left from an earlier run go away;
  - after spacetime_sync.push_state() has written asset rows from its own, possibly older,
    snapshot (startup, a blueprint activation, POST /api/spacetime/sync);
  - every SPACETIME_RESYNC_S seconds (default 60), in case anything else wrote to it.

Rows follow the module's rules (backend/spacetime/src/index.ts), so one odd value never
gets a whole batch refused. Never falsely safe:
  - a status the module doesn't know goes as UNKNOWN;
  - a stale asset (`stale: true`, no fresh reading) goes as UNKNOWN unless it is in ALARM,
    with no level (-1) and no position (""), because the module has no column for a last
    known value and a console would show it as the current one;
  - a tank level that isn't a number from 0 to 100 goes as -1 (no reading), and the tank
    as UNKNOWN unless it is already in ALARM. Noise up to LEVEL_SLACK_PCT past either end
    is clamped;
  - an alert severity the module doesn't know goes as ALARM, an alert with no `active`
    stays active, and a connector status the module doesn't know goes as unavailable.
A row whose ID the module would refuse is left out, and logged once.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from typing import Optional

from . import now, spacetime_sync

log = logging.getLogger("freeboard.spacetime")


def _env_s(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
    except ValueError:
        return default
    return v if math.isfinite(v) and v > 0 else default


MIRROR_S = _env_s("SPACETIME_MIRROR_S", 1.0)
RESYNC_S = _env_s("SPACETIME_RESYNC_S", 60.0)
BACKOFF_MAX_S = 15.0
LEVEL_SLACK_PCT = 0.5   # sensor noise past 0 or 100 that is clamped instead of dropped
MAX_TEXT = 1000         # longer messages, reasons, and results are cut to this length

# The module's rules (backend/spacetime/src/index.ts). test_spacetime_mirror.py checks they match.
STATUSES = ("OK", "WARNING", "ALARM", "OFFLINE", "UNKNOWN")
ACTION_TYPES = ("SET_VALVE", "SET_PUMP", "CHOKE_WELL", "SEND_TRUCK")
SEVERITIES = ("WARNING", "ALARM")
PROPOSAL_STATUSES = ("PENDING", "SENDING", "CONFIRMED", "FAILED", "REJECTED", "UNCERTAIN")
CONNECTOR_STATUSES = ("connected", "unavailable")
ID = re.compile(r"[A-Z][A-Z0-9_]*")        # asset and connector IDs, alert kinds
LOWER_ID = re.compile(r"[a-z][a-z0-9_]*")  # alert and proposal ids, connector kinds, proposal sources

TABLES = ("assets", "alerts", "proposals", "connectors")
KEY = {"assets": "asset_id", "alerts": "id", "proposals": "id", "connectors": "id"}
UNUSABLE_LEVEL = "The level reading is not a usable percentage. Treat it as unknown, not safe."
NO_FRESH_READING = "No fresh reading. Treat it as unknown, not safe."


# ---------- rows, as the module's apply_batch takes them ----------

def _is(rx: re.Pattern, v) -> bool:
    return isinstance(v, str) and rx.fullmatch(v) is not None


def _text(v, limit: int = MAX_TEXT) -> str:
    """Text as the module stores it: "" for None, cut to `limit`, and valid UTF-8 (a lone
    surrogate from a client's JSON would get the whole batch refused)."""
    if v is None:
        return ""
    s = v if isinstance(v, str) else str(v)
    if len(s) > limit:
        s = s[:limit - 3] + "..."
    return s.encode("utf-8", "replace").decode("utf-8")


def level(v) -> Optional[float]:
    """A tank level as the module takes it, to 0.1 %, or None when it isn't a usable
    percentage: not a number, NaN, infinite, or more than LEVEL_SLACK_PCT past 0 or 100."""
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x) or x < -LEVEL_SLACK_PCT or x > 100.0 + LEVEL_SLACK_PCT:
        return None
    return round(min(100.0, max(0.0, x)), 1)


def _position(v) -> str:
    if isinstance(v, bool):
        return "OPEN" if v else "CLOSED"
    return _text(v, 64)


def _value(v) -> str:
    """A command value as text: OPEN, ON, 0.5, or "" for none."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:g}" if math.isfinite(v) else ""
    return _text(v, 64)


def _kind(v) -> str:
    """An alert kind as an ID-style word (PUMP_TRIP). One that can't be made into one is OTHER."""
    k = re.sub(r"[^A-Z0-9_]", "_", str(v or "").upper())
    return k if _is(ID, k) else "OTHER"


def clock_row(state: dict) -> dict:
    """The field_clock row: the live clock and the incident, if any."""
    incident = state.get("incident") or {}
    root = incident.get("root_asset_id")
    return {"clock": _text(state.get("clock"), 32),
            "incident_root": root if _is(ID, root) else "",
            "incident_summary": _text(incident.get("summary"))}


def asset_row(asset_id, a: dict) -> Optional[dict]:
    """The asset_state row for one /api/state asset, or None when the module would refuse its ID."""
    if not _is(ID, asset_id):
        return None
    status = a.get("status")
    status = status if status in STATUSES else "UNKNOWN"
    reason = _text(a.get("reason"))
    lvl = spacetime_sync.NO_LEVEL
    if a.get("stale") is True:
        # The last level or position is not the current one: send neither.
        return {"asset_id": asset_id, "status": status if status == "ALARM" else "UNKNOWN",
                "level_pct": lvl, "position": "", "reason": reason or NO_FRESH_READING}
    if a.get("level_pct") is not None:
        usable = level(a["level_pct"])
        if usable is not None:
            lvl = usable
        elif status != "ALARM":
            status = "UNKNOWN"
            reason = f"{UNUSABLE_LEVEL} {reason}".strip()
    return {"asset_id": asset_id, "status": status, "level_pct": lvl,
            "position": _position(a.get("position")), "reason": reason}


def alert_row(a: dict, resolved_clock: str = "") -> Optional[dict]:
    """The alert row for one live alert, or None when the module would refuse its id or
    asset. Anything but an explicit `active: False` counts as still active."""
    if not (_is(LOWER_ID, a.get("id")) and _is(ID, a.get("asset_id"))):
        return None
    severity = str(a.get("severity") or "").upper()
    active = a.get("active") is not False
    return {"id": a["id"], "asset_id": a["asset_id"], "kind": _kind(a.get("kind")),
            "severity": severity if severity in SEVERITIES else "ALARM",
            "message": _text(a.get("message")), "active": active,
            "raised_clock": _text(a.get("clock"), 32),
            "resolved_clock": "" if active else _text(resolved_clock, 32)}


def proposal_row(p: dict) -> Optional[dict]:
    """The proposal row for one live proposal, or None when the module would refuse it
    (an id, type, asset, target, or status it doesn't take)."""
    a = p.get("action")
    if not isinstance(a, dict):
        return None
    target = a.get("target_id") or ""
    if not (_is(LOWER_ID, p.get("id")) and a.get("type") in ACTION_TYPES and _is(ID, a.get("asset_id"))
            and (target == "" or _is(ID, target)) and p.get("status") in PROPOSAL_STATUSES):
        return None
    source = str(p.get("source") or "").lower()
    return {"id": p["id"], "type": a["type"], "asset_id": a["asset_id"], "value": _value(a.get("value")),
            "target_id": target, "status": p["status"], "source": source if _is(LOWER_ID, source) else "",
            "reason": _text(p.get("reason")), "decided_by": _text(p.get("decided_by"), 200),
            "result": _text(p.get("result"))}


def connector_row(c: dict, updated_clock: str = "") -> Optional[dict]:
    """The connector_status row for one entry of registry.status(), or None when the module
    would refuse its ID."""
    if not _is(ID, c.get("id")):
        return None
    kind = str(c.get("kind") or "").lower()
    status = c.get("status")
    return {"id": c["id"], "kind": kind if _is(LOWER_ID, kind) else "unknown",
            "status": status if status in CONNECTOR_STATUSES else "unavailable",
            "detail": _text(c.get("detail")), "updated_clock": _text(updated_clock, 32)}


def _alerts(live) -> list:
    """live.alerts(), else the detector's list: every alert, oldest first."""
    fn = getattr(live, "alerts", None)
    found = fn() if callable(fn) else getattr(getattr(live, "detector", None), "alerts", None)
    return [a for a in (found or []) if isinstance(a, dict)]


def _proposals(live) -> list:
    return [p for p in list((getattr(live, "proposals", None) or {}).values()) if isinstance(p, dict)]


def _connectors(live) -> list:
    registry = getattr(live, "registry", None)
    found = registry.status() if registry is not None else []
    return [c for c in (found or []) if isinstance(c, dict)]


# ---------- the mirror ----------

class Mirror:
    """What SpacetimeDB holds, as far as the mirror knows, and the next batch to send it."""

    def __init__(self, period_s: float = MIRROR_S, resync_s: float = RESYNC_S) -> None:
        self.period_s = period_s
        self.resync_s = resync_s
        self.sent: Optional[dict] = None     # the last snapshot SpacetimeDB accepted; None: not known
        self.last_full = 0.0                 # time.monotonic() of the last full batch it accepted
        self.state_pushes = spacetime_sync.state_pushes
        self.failures = 0
        self.retry_at = 0.0
        self.resolved_clock: dict[str, str] = {}    # alert id -> live clock it was first seen resolved
        self.connector_seen: dict[str, tuple] = {}  # connector id -> ((kind, status, detail), clock since)
        self._left_out: set = set()

    def snapshot(self, app) -> dict:
        """Every row as SpacetimeDB should hold it now: {"clock": row, "assets": {id: row},
        "alerts": ..., "proposals": ..., "connectors": ...}. Synchronous: call it on the loop."""
        st = now.state(app)
        clock = clock_row(st)
        snap: dict = {"clock": clock, **{t: {} for t in TABLES}}
        for aid, a in (st.get("assets") or {}).items():
            self._add(snap, "assets", aid, asset_row(aid, a if isinstance(a, dict) else {}))
        live = getattr(getattr(app, "state", None), "live", None)
        alert_ids, connector_ids = set(), set()
        if live is not None:
            for a in _alerts(live):
                aid = a.get("id")
                if not isinstance(aid, str):
                    self._add(snap, "alerts", aid, None)
                    continue
                alert_ids.add(aid)
                if a.get("active") is not False:
                    # Active again, or a new alert that reuses the id (a new live field
                    # counts from alert_1 again): its old resolved clock no longer applies.
                    self.resolved_clock.pop(aid, None)
                elif aid not in self.resolved_clock:
                    self.resolved_clock[aid] = str(a.get("resolved_clock") or clock["clock"])
                self._add(snap, "alerts", aid, alert_row(a, self.resolved_clock.get(aid, "")))
            for p in _proposals(live):
                self._add(snap, "proposals", p.get("id"), proposal_row(p))
            for c in _connectors(live):
                cid = c.get("id")
                if not isinstance(cid, str):
                    self._add(snap, "connectors", cid, None)
                    continue
                connector_ids.add(cid)
                key = (c.get("kind"), c.get("status"), c.get("detail"))
                if cid not in self.connector_seen or self.connector_seen[cid][0] != key:
                    self.connector_seen[cid] = (key, clock["clock"])
                self._add(snap, "connectors", cid, connector_row(c, self.connector_seen[cid][1]))
        self.resolved_clock = {k: v for k, v in self.resolved_clock.items() if k in alert_ids}
        self.connector_seen = {k: v for k, v in self.connector_seen.items() if k in connector_ids}
        return snap

    def _add(self, snap: dict, table: str, key, row: Optional[dict]) -> None:
        if row is not None:
            snap[table][row[KEY[table]]] = row
        elif (table, repr(key)) not in self._left_out:
            self._left_out.add((table, repr(key)))
            log.warning("SpacetimeDB mirror leaves out %s %r: the module would refuse it.", table, key)

    def batch(self, snap: dict, full: bool = False) -> Optional[dict]:
        """The apply_batch payload that takes SpacetimeDB from what it holds to `snap`: the rows
        that changed and the ids that are gone, or None when nothing changed. A full batch (and
        the first one) lists every row instead."""
        if full or self.sent is None:
            return {"full": True, "clock": snap["clock"], **{t: list(snap[t].values()) for t in TABLES}}
        out: dict = {}
        if snap["clock"] != self.sent["clock"]:
            out["clock"] = snap["clock"]
        for t in TABLES:
            old, new = self.sent[t], snap[t]
            changed = [row for k, row in new.items() if old.get(k) != row]
            gone = sorted(k for k in old if k not in new)
            if changed:
                out[t] = changed
            if gone:
                out[f"removed_{t}"] = gone
        return out or None

    async def tick(self, app) -> Optional[bool]:
        """One pass. True: SpacetimeDB is up to date (pushed, or nothing changed). False: the
        push failed and its changes wait for the next try. None: skipped, because SpacetimeDB
        is off or the mirror is waiting out a failure. Never raises."""
        if not spacetime_sync.enabled() or time.monotonic() < self.retry_at - 0.05:
            return None
        pushes = spacetime_sync.state_pushes
        try:
            snap = self.snapshot(app)
            full = (self.sent is None or pushes != self.state_pushes
                    or time.monotonic() - self.last_full >= self.resync_s)
            payload = self.batch(snap, full)
            ok = payload is None or await asyncio.to_thread(spacetime_sync.push_batch, payload)
        except Exception:  # noqa: BLE001 - the mirror must never take the server down
            log.exception("SpacetimeDB mirror failed")
            ok = False
        if not ok:
            self.failures += 1
            wait = min(BACKOFF_MAX_S, self.period_s * 2 ** min(self.failures - 1, 8))
            self.retry_at = time.monotonic() + wait
            return False
        self.sent, self.failures, self.retry_at = snap, 0, 0.0
        if full:
            self.last_full, self.state_pushes = time.monotonic(), pushes
        return True


async def run(app, period_s: Optional[float] = None) -> None:
    """The mirror loop. main.py's lifespan runs it as a task and cancels it on shutdown.
    One tick every `period_s` seconds (default SPACETIME_MIRROR_S, 1). Never raises."""
    period = MIRROR_S if period_s is None else float(period_s)
    if not (math.isfinite(period) and period > 0):
        period = MIRROR_S
    mirror = Mirror(period_s=period)
    while True:
        await asyncio.sleep(period)
        await mirror.tick(app)
