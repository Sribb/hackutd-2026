"""The command bus: the only way anything, AI or human, changes a machine.

    PROPOSED -> APPROVED -> SENT -> ACKED
         \\-> REJECTED     \\-> FAILED

Rules (backend/ROADMAP.md section 2):
- Agents can only propose. Approval needs a human operator name.
- Every proposal is safety-checked: the action must be valid for that asset, and
  the forecast with the action must not add a shutdown or bring one earlier.
  The check runs again at approval, against whatever has been applied since.
- Execution goes to simulated actuators only (backend/app/actuators.py).
- A halt freezes the queue. Nothing is sent until resume.
- Every step is in the audit log.
"""
from __future__ import annotations

import itertools
import threading
import time

from . import fields
from .forecast import ACTION_TYPES, clean_actions, nice, simulate

EXTRA_TYPES = ("RESET_PUMP",)      # simulated only: repair a tripped pump
HORIZON = 360
EARLIER_TOLERANCE_MIN = 1.0

_lock = threading.RLock()
_ids = itertools.count(1)
_commands: dict[str, dict] = {}
_audit: list[dict] = []
_halted = False
_queue: list[str] = []
_queue_event = threading.Event()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _log(event: str, cmd: dict | None = None, **extra) -> None:
    _audit.append({"ts_ms": _now_ms(), "event": event,
                   **({"command_id": cmd["id"], "asset_id": cmd["action"]["asset_id"]} if cmd else {}),
                   **extra})


# ---------- safety check ----------

def check(action: dict, snap=None) -> dict:
    """Is this action safe to carry out now? Never raises."""
    snap = snap or fields.snapshot()
    problems: list[str] = []
    typ, aid = action.get("type"), action.get("asset_id")
    plant = snap.plant

    if typ in EXTRA_TYPES:
        if aid not in plant.pumps:
            problems.append(f"RESET_PUMP needs a pump ID, got {aid!r}.")
        elif aid not in plant.tripped:
            problems.append(f"{nice(aid)} isn't tripped, so there's nothing to reset.")
        return {"ok": not problems, "problems": problems, "forecast_before": None, "forecast_after": None}

    if typ not in ACTION_TYPES:
        return {"ok": False, "problems": [f"Unknown action type {typ!r}."],
                "forecast_before": None, "forecast_after": None}

    base = list(snap.applied)
    candidate = {**action, "at_min": 0.0}
    cleaned, warns = clean_actions(plant, [candidate])
    problems += [w.replace("Ignored ", "Not allowed: ", 1) for w in warns]
    if problems:
        return {"ok": False, "problems": problems, "forecast_before": None, "forecast_after": None}

    before = simulate(plant, clean_actions(plant, base)[0], HORIZON)
    after = simulate(plant, clean_actions(plant, base + cleaned)[0], HORIZON)
    for tid, t_after in sorted(after.shutdowns.items()):
        t_before = before.shutdowns.get(tid)
        if t_before is None:
            problems.append(f"It would make {nice(tid)} reach shutdown at minute {t_after:g}.")
        elif t_after < t_before - EARLIER_TOLERANCE_MIN:
            problems.append(f"It would bring {nice(tid)}'s shutdown earlier, from minute "
                            f"{t_before:g} to {t_after:g}.")

    def brief(run):
        return {"shutdowns": run.shutdowns, "cost": run.cost}

    return {"ok": not problems, "problems": problems,
            "forecast_before": brief(before), "forecast_after": brief(after)}


# ---------- lifecycle ----------

def propose(action: dict, proposed_by: str, reason: str = "") -> dict:
    snap = fields.snapshot()
    with _lock:
        cid = f"cmd_{next(_ids)}"
        cmd = {"id": cid, "action": dict(action), "proposed_by": proposed_by, "reason": reason,
               "status": "PROPOSED", "check": check(action, snap), "field_version": snap.version,
               "proposed_ms": _now_ms(), "approved_by": None, "sent_ms": None, "ack_ms": None,
               "result": None}
        _commands[cid] = cmd
        _log("PROPOSED", cmd, by=proposed_by, ok=cmd["check"]["ok"], problems=cmd["check"]["problems"])
        return dict(cmd)


class CommandError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def _get(cid: str) -> dict:
    cmd = _commands.get(cid)
    if cmd is None:
        raise CommandError(404, f"No command {cid}.")
    return cmd


def approve(cid: str, operator: str) -> dict:
    if not operator or operator.lower().startswith("agent"):
        raise CommandError(403, "Only a human operator can approve. Send the X-Operator header.")
    with _lock:
        cmd = _get(cid)
        if cmd["status"] != "PROPOSED":
            raise CommandError(409, f"{cid} is {cmd['status']}, not PROPOSED.")
        snap = fields.snapshot()
        if snap.version != cmd["field_version"]:
            cmd["status"] = "REJECTED"
            cmd["result"] = "The active field changed after this was proposed."
            _log("REJECTED", cmd, by="system", reason=cmd["result"])
            raise CommandError(409, cmd["result"])
        cmd["check"] = check(cmd["action"], snap)
        if not cmd["check"]["ok"]:
            _log("APPROVAL_REFUSED", cmd, by=operator, problems=cmd["check"]["problems"])
            raise CommandError(422, "Safety check failed: " + " ".join(cmd["check"]["problems"]))
        cmd["status"], cmd["approved_by"] = "APPROVED", operator
        _log("APPROVED", cmd, by=operator)
        _queue.append(cid)
        _queue_event.set()
        return dict(cmd)


def reject(cid: str, operator: str, reason: str = "") -> dict:
    with _lock:
        cmd = _get(cid)
        if cmd["status"] not in ("PROPOSED", "APPROVED"):
            raise CommandError(409, f"{cid} is {cmd['status']} and can't be rejected.")
        if cid in _queue:
            _queue.remove(cid)
        cmd["status"], cmd["result"] = "REJECTED", reason or "Rejected by operator."
        _log("REJECTED", cmd, by=operator or "unknown", reason=cmd["result"])
        return dict(cmd)


def halt(operator: str) -> dict:
    global _halted
    with _lock:
        _halted = True
        _log("HALT", by=operator or "unknown")
        return status()


def resume(operator: str) -> dict:
    global _halted
    with _lock:
        _halted = False
        _log("RESUME", by=operator or "unknown")
        _queue_event.set()
        return status()


def status() -> dict:
    with _lock:
        return {"halted": _halted, "queued": list(_queue)}


def list_commands() -> list[dict]:
    with _lock:
        return [dict(c) for c in _commands.values()]


def audit() -> list[dict]:
    with _lock:
        return list(_audit)


def reset() -> None:
    """Clears everything (tests, and switching fields)."""
    global _halted
    with _lock:
        _commands.clear()
        _audit.clear()
        _queue.clear()
        _halted = False


# ---------- execution ----------

def run_once(actuate) -> bool:
    """Sends the next approved command if not halted. Returns True if one ran.
    `actuate(action) -> (ok, message)` is the actuator (simulated by default)."""
    with _lock:
        if _halted or not _queue:
            return False
        cid = _queue.pop(0)
        cmd = _commands[cid]
        cmd["status"], cmd["sent_ms"] = "SENT", _now_ms()
        _log("SENT", cmd)
    ok, message = actuate(cmd["action"])
    with _lock:
        if ok:
            try:
                fields.apply(cmd["action"], expected_version=cmd["field_version"])
            except RuntimeError as e:
                ok, message = False, str(e)
        cmd["status"] = "ACKED" if ok else "FAILED"
        cmd["ack_ms"], cmd["result"] = _now_ms(), message
        _log(cmd["status"], cmd, result=message)
    return True


def start_worker(actuate, poll_s: float = 0.2) -> threading.Thread:
    def loop():
        while True:
            _queue_event.wait(poll_s)
            _queue_event.clear()
            while run_once(actuate):
                pass
    t = threading.Thread(target=loop, name="command-worker", daemon=True)
    t.start()
    return t
