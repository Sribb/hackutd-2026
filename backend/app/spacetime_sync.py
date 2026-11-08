"""Pushes backend state into the SpacetimeDB module in backend/spacetime.

Off unless SPACETIME_DB is set. SpacetimeDB being down never breaks the REST API:
every failure is recorded in `status` and logged, and the call returns.

  SPACETIME_URL   default http://127.0.0.1:3000
  SPACETIME_DB    database name used with `spacetime publish`, e.g. freeboard
  SPACETIME_TOKEN optional bearer token (never commit it, never logged)

Two ways in:
  push_state(state)    one reducer call per asset (upsert_asset, set_clock). Works with
                       every version of the module.
  push_batch(payload)  one apply_batch call (module v2, #126): the clock, assets, alerts,
                       proposals, and connectors from one moment in one transaction.
                       backend/app/spacetime_mirror.py builds the payload.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request

log = logging.getLogger("freeboard.spacetime")

status: dict = {"enabled": False, "last_push_ok": None, "last_error": None}

NO_LEVEL = -1.0  # the module stores -1 for "not a tank" or "no reading"
BATCH_TIMEOUT_S = 5.0

# Counts finished push_state() calls. Those write asset rows on their own threads, from a
# snapshot that may be older than the mirror's, so the mirror sends a full batch after one.
state_pushes = 0
_count_lock = threading.Lock()


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
    so claim it once per process before pushing. Already-taken is fine if we hold it.
    When SpacetimeDB doesn't answer at all (down, unreachable), the claim is tried again
    before the next push, so a database that comes up later still gets claimed."""
    global _claimed
    if _claimed:
        return
    try:
        call_reducer("claim_writer", [])
        _claimed = True
    except urllib.error.HTTPError as e:  # the module answered: claimed by us or someone else
        why = _why(e)
        if "already taken" not in why:
            log.info("SpacetimeDB claim_writer: %s", why)
        _claimed = True  # a push that then fails reports the real problem
    except (urllib.error.URLError, OSError, RuntimeError) as e:  # no answer: try again next push
        log.info("SpacetimeDB claim_writer: %s", e)


def push_state(state: dict) -> bool:
    """Mirrors a full /api/state payload into SpacetimeDB. Returns True on success."""
    global state_pushes
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
    finally:
        with _count_lock:
            state_pushes += 1
    status.update(last_push_ok=True, last_error=None)
    return True


def _why(e: Exception) -> str:
    """What went wrong, with the module's own message when it refused the call (an
    HTTP error's body, e.g. "unknown status 'FOO'"). Never includes the token."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - the body is only a nicer message
            body = ""
        return f"HTTP {e.code}: {body[:300] or e.reason}"
    return str(e)


def push_batch(payload: dict) -> bool:
    """Sends one apply_batch call (module v2) with `payload` as its JSON argument. Claims
    the writer role first, once per process, like push_state. Returns True on success;
    on failure records why in `status` and returns False, so the caller keeps its
    changes and sends them again later. Never raises for a network or module error."""
    if _config() is None:
        status.update(enabled=False)
        return False
    status.update(enabled=True)
    try:
        body = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as e:  # a bug in the caller: NaN or an object JSON can't hold
        status.update(last_push_ok=False, last_error=f"batch is not valid JSON: {e}")
        log.error("SpacetimeDB batch is not valid JSON: %s", e)
        return False
    try:
        _claim_writer()
        call_reducer("apply_batch", [body], timeout=BATCH_TIMEOUT_S)
    except (urllib.error.URLError, OSError, RuntimeError, ValueError) as e:  # ValueError: a bad SPACETIME_URL
        why = _why(e)
        status.update(last_push_ok=False, last_error=why)
        log.warning("SpacetimeDB apply_batch failed: %s", why)
        return False
    status.update(last_push_ok=True, last_error=None)
    return True
