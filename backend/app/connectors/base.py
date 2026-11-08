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

import importlib
import os
from types import ModuleType
from typing import Callable, Optional

from ..fieldmap import FieldMap

Reading = dict  # {"asset_id": str, "signal": str, "value": float | bool | str, "t": float}

ANALOG = ("level_pct", "flow_bbl_hr", "pressure_psi", "motor_amps")


def import_optional(module: str, package: str) -> tuple[Optional[ModuleType], Optional[str]]:
    """(module, None), or (None, why) when the library is missing or broken."""
    try:
        return importlib.import_module(module), None
    except Exception as e:  # noqa: BLE001 - ImportError, or a broken install
        return None, f"{package} is not installed (pip install {package}): {e}"


def _truthy(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    v = str(value).strip().upper()
    if v in {"OPEN", "OPENED", "ON", "TRUE", "1", "RUNNING", "RUN"}:
        return True
    if v in {"CLOSED", "CLOSE", "OFF", "FALSE", "0", "STOPPED", "STOP", "TRIPPED"}:
        return False
    try:
        return float(v) != 0
    except ValueError:
        return None


def coerce(signal: str, value):
    """A raw device value as the live layer's type for `signal`, or None if it makes no sense.

    Analog signals are floats, `running` is a bool, `position` is "OPEN" or "CLOSED".
    """
    if signal in ANALOG:
        if isinstance(value, bool):
            return None
        try:
            x = float(value)
        except (TypeError, ValueError):
            return None
        return x if x == x and abs(x) != float("inf") else None
    on = _truthy(value)
    if on is None:
        return None
    if signal == "position":
        return "OPEN" if on else "CLOSED"
    return on


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
    def missing_env(self) -> Optional[str]:
        """The first `*_env` variable the config names that isn't set, else None."""
        for key, var in self.config.items():
            if key.endswith("_env") and var and os.environ.get(var) is None:
                return str(var)
        return None

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
