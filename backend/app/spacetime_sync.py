"""Pushes backend state into the SpacetimeDB module in backend/spacetime.

Off unless SPACETIME_DB is set. SpacetimeDB being down never breaks the REST API:
every failure is recorded in `status` and logged, and the call returns.

  SPACETIME_URL   default http://127.0.0.1:3000
  SPACETIME_DB    database name used with `spacetime publish`, e.g. freeboard
  SPACETIME_TOKEN optional bearer token (never commit it)
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("freeboard.spacetime")

status: dict = {"enabled": False, "last_push_ok": None, "last_error": None}

NO_LEVEL = -1.0  # the module stores -1 for "not a tank" or "no reading"


def _config() -> tuple[str, str, str | None] | None:
    db = os.environ.get("SPACETIME_DB")
    if not db:
        return None
    url = os.environ.get("SPACETIME_URL", "http://127.0.0.1:3000").rstrip("/")
    return url, db, os.environ.get("SPACETIME_TOKEN")


def enabled() -> bool:
    return _config() is not None


def call_reducer(reducer: str, args: list, timeout: float = 3.0) -> None:
    """POST /v1/database/{db}/call/{reducer} with positional JSON args."""
    cfg = _config()
    if cfg is None:
        raise RuntimeError("SPACETIME_DB is not set")
    url, db, token = cfg
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{url}/v1/database/{db}/call/{reducer}",
        data=json.dumps(args).encode(), method="POST", headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def state_to_calls(state: dict) -> list[tuple[str, list]]:
    """Turns a /api/state payload into reducer calls, in order."""
    incident = state.get("incident") or {}
    calls: list[tuple[str, list]] = [(
        "set_clock",
        [state.get("clock", ""), incident.get("root_asset_id", ""), incident.get("summary", "")],
    )]
    for asset_id, a in sorted(state.get("assets", {}).items()):
        level = a.get("level_pct")
        calls.append((
            "upsert_asset",
            [asset_id, a["status"], NO_LEVEL if level is None else float(level),
             a.get("position") or "", a.get("reason") or ""],
        ))
    return calls


_claimed = False


def _claim_writer() -> None:
    """The module only lets its writer identity change live state (D2). On a database
    published before that rule, the writer list starts empty and the first claim wins,
    so claim it once per process before pushing. Already-taken is fine if we hold it."""
    global _claimed
    if _claimed:
        return
    try:
        call_reducer("claim_writer", [])
        _claimed = True
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        if "already taken" not in str(e):
            log.info("SpacetimeDB claim_writer: %s", e)
        _claimed = True  # a push that then fails reports the real problem


def push_state(state: dict) -> bool:
    """Mirrors a full /api/state payload into SpacetimeDB. Returns True on success."""
    if _config() is None:
        status.update(enabled=False)
        return False
    status.update(enabled=True)
    _claim_writer()
    try:
        for reducer, args in state_to_calls(state):
            call_reducer(reducer, args)
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        status.update(last_push_ok=False, last_error=str(e))
        log.warning("SpacetimeDB push failed: %s", e)
        return False
    status.update(last_push_ok=True, last_error=None)
    return True
