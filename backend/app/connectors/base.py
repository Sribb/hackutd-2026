"""The one connector interface.

A connector is where live signals come from and where confirmed commands go.

    read()        -> [{"asset_id", "signal", "value", "t"}, ...]   new readings since the last call
    send(command) -> {"ok": bool, "detail": str}                   command = {type, asset_id, value, target_id?}
    status()      -> {"id", "kind", "available", "detail"}

`t` is live minutes since the start clock, stamped by the server. A connector must
never raise out of read(), send(), or status(): a broken device shows up as stale
readings and an "unavailable" status, never as a crashed console.

Secrets never live in the blueprint. A config key ending in `_env` names an
environment variable, and `secret()` reads it.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

from ..fieldmap import FieldMap

Reading = dict  # {"asset_id": str, "signal": str, "value": float | bool | str, "t": float}


class Connector:
    kind = "base"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float]) -> None:
        self.spec = spec
        self.id = spec.get("id") or f"CONN_{self.kind.upper()}"
        self.config = dict(spec.get("config") or {})
        self.fmap = fmap
        self.now = now
        # (asset_id, signal) -> address, and (asset_id, command) -> address
        self.signals = {(s["asset_id"], s["signal"]): s.get("address")
                        for s in spec.get("signals") or [] if s.get("asset_id") and s.get("signal")}
        self.commands = {(c["asset_id"], c["command"]): c.get("address")
                         for c in spec.get("commands") or [] if c.get("asset_id") and c.get("command")}

    # ---- the interface ----
    def read(self) -> list[Reading]:
        return []

    def send(self, command: dict) -> dict:
        return {"ok": False, "detail": f"{self.kind} connector {self.id} can't send commands."}

    def status(self) -> dict:
        return {"id": self.id, "kind": self.kind, "available": True, "detail": "ok"}

    def advance(self, dt_min: float) -> None:
        """Called once per live tick. Only the simulator does anything here."""

    def close(self) -> None:
        """Release sockets and threads on shutdown."""

    # ---- helpers ----
    def secret(self, key: str) -> Optional[str]:
        """config[key + '_env'] names an environment variable; return its value, or None."""
        var = self.config.get(f"{key}_env")
        return os.environ.get(var) if var else None

    def reading(self, asset_id: str, signal: str, value) -> Reading:
        return {"asset_id": asset_id, "signal": signal, "value": value, "t": round(self.now(), 3)}


class UnavailableConnector(Connector):
    """Stands in for a connector whose kind we don't know, so the rest still runs."""

    kind = "unknown"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float], why: str) -> None:
        super().__init__(spec, fmap, now)
        self.kind = str(spec.get("kind", "unknown"))
        self.why = why

    def send(self, command: dict) -> dict:
        return {"ok": False, "detail": self.why}

    def status(self) -> dict:
        return {"id": self.id, "kind": self.kind, "available": False, "detail": self.why}
