"""Control tokens for every write (#103, CF-15). One mechanism for the QR token and the PIN.

    FREEBOARD_TOKEN        operator token (the console's #k=... link)
    OPERATOR_PIN           also operator: a short PIN a person can type (CF-5's prompt)
    FREEBOARD_PHONE_TOKEN  phone token (the QR's #k=...): confirm, reject, and ack only

With none of them set, nothing changes: every route is open, as before. With any of them
set, a write needs the header `X-Freeboard-Token`:
    missing or wrong          -> 401 {"detail": "Enter the operator PIN to send commands."}
    phone token, operator route -> 403
Reads (GET) stay open. Tokens are compared with hmac.compare_digest and never logged.

Use it on any route, in any lane:

    from backend.app.auth import Caller, require_role
    @app.post("/api/blueprints/{id}/activate")
    def activate(id: str, caller: Caller = Depends(require_role("operator"))): ...

`caller.role` is "operator", "phone", or None (no token configured). `caller.device` is
"console" or "phone" (None when open) and is what `decided_by` and the audit log record.
"""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Header, HTTPException

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


def check(token: Optional[str], *allowed: str) -> Caller:
    """The caller for this token, or HTTPException 401/403. Operator may do everything."""
    if not enabled():
        return Caller(None)
    role = role_for(token)
    if role is None:
        raise HTTPException(401, DENIED)
    if role != OPERATOR and role not in allowed:
        raise HTTPException(403, f"The {role} token can't do this. Enter the operator PIN.")
    return Caller(role)


def require_role(*allowed: str) -> Callable[..., Caller]:
    """FastAPI dependency. require_role("operator") for operator only; require_role("phone")
    for phone or operator (operator can always do what a phone can)."""
    for r in allowed:
        if r not in DEVICE:
            raise ValueError(f"Unknown role {r!r}. Use 'operator' or 'phone'.")

    def dependency(x_freeboard_token: Optional[str] = Header(None)) -> Caller:
        return check(x_freeboard_token, *allowed)

    return dependency


def ws_caller(token: Optional[str]) -> tuple[bool, Caller]:
    """For /ws?token=. (ok, caller): with tokens on, a wrong token is not ok (close 1008),
    and no token connects read-only (role None, so its `ui` messages are ignored)."""
    if not enabled():
        return True, Caller(None)
    if not (token or "").strip():
        return True, Caller(None)
    role = role_for(token)
    return role is not None, Caller(role)


def may_steer(caller: Caller) -> bool:
    """Whether a /ws client's `ui` messages move every screen."""
    return not enabled() or caller.role in DEVICE
