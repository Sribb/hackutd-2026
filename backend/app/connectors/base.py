"""The one connector interface.

A connector is where live signals come from and where confirmed commands go.

    read()        -> [{"asset_id", "signal", "value", "t"}, ...]   new readings since the last call
    send(command) -> {"ok": bool, "detail": str}                   command = {type, asset_id, value, target_id?}
    status()      -> {"id", "kind", "available", "detail"}

`t` is live minutes since the start clock, stamped by the server. A connector must
never raise out of read(), send(), or status(): a broken device shows up as stale
readings and an "unavailable" status, never as a crashed console.

A send() result may also carry `"uncertain": True`: the command left for the
device but its acknowledgement never came back (an MQTT PUBACK or a Modbus reply
timed out). The live layer records that as UNCERTAIN, never FAILED, and nothing
sends it again: a person checks the field first.

Secrets never live in the blueprint. A config key ending in `_env` names an
environment variable, and `secret()` reads it. The name must start with
FREEBOARD_CONN_ (ENV_PREFIX), so a blueprint can't point a connector at an
unrelated secret such as ANTHROPIC_API_KEY and send it to its host.

close() must return at once: it is called on the event loop (shutdown, reload).
Joining threads or waiting on sockets goes on a daemon thread (`close_later`).
"""
from __future__ import annotations

import importlib
import os
import re
import threading
from types import ModuleType
from typing import Callable, Optional

from ..fieldmap import FieldMap

Reading = dict  # {"asset_id": str, "signal": str, "value": float | bool | str, "t": float}

ANALOG = ("level_pct", "flow_bbl_hr", "pressure_psi", "motor_amps")
ENV_PREFIX = "FREEBOARD_CONN_"
ENV_NAME = re.compile(r"^FREEBOARD_CONN_[A-Za-z0-9_]+$")  # docs/blueprint.schema.json says the same
UNCERTAIN_DETAIL = "sent; delivery not confirmed — check the field before retrying"


def check_env_names(config: dict) -> None:
    """Every `*_env` value names a FREEBOARD_CONN_ variable. Raises ValueError otherwise."""
    for key, var in config.items():
        if str(key).endswith("_env") and var not in (None, "") and \
                not (isinstance(var, str) and ENV_NAME.fullmatch(var)):
            raise ValueError(f"config {key} must name an environment variable starting with {ENV_PREFIX} "
                             f"(for example {ENV_PREFIX}{str(key)[:-4].upper()}), got {var!r}")


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
        self.check_config()

    def check_config(self) -> None:
        """Raise ValueError for a config that must not load. The registry then shows the
        connector as unavailable with the reason."""
        check_env_names(self.config)

    # ---- the interface ----
    def read(self) -> list[Reading]:
        return []

    def send(self, command: dict) -> dict:
        return {"ok": False, "detail": f"{self.kind} connector {self.id} can't send commands."}

    def status(self) -> dict:
        return {"id": self.id, "kind": self.kind, "available": True, "detail": "ok"}

    def advance(self, dt_min: float) -> None:
        """Called once per live tick. Only the simulator does anything here."""

    def close(self) -> Optional[threading.Thread]:
        """Release sockets and threads. Returns at once; returns the thread still
        finishing the cleanup, if any, so shutdown can join it off the event loop."""
        return None

    # ---- helpers ----
    def missing_env(self) -> Optional[str]:
        """The first `*_env` variable the config names that isn't set, else None."""
        for key, var in self.config.items():
            if key.endswith("_env") and var and self.secret(key[:-4]) is None:
                return str(var)
        return None

    def secret(self, key: str) -> Optional[str]:
        """config[key + '_env'] names an environment variable; return its value, or None.
        Only FREEBOARD_CONN_ variables are ever read."""
        var = self.config.get(f"{key}_env")
        if not (isinstance(var, str) and ENV_NAME.fullmatch(var)):
            return None
        return os.environ.get(var)

    @staticmethod
    def close_later(fn: Callable[[], None], name: str) -> threading.Thread:
        """Run blocking cleanup on a daemon thread, so close() never blocks the event loop."""
        def run() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 - cleanup of a dead device must never raise
                pass
        t = threading.Thread(target=run, name=name, daemon=True)
        t.start()
        return t

    def reading(self, asset_id: str, signal: str, value) -> Reading:
        return {"asset_id": asset_id, "signal": signal, "value": value, "t": round(self.now(), 3)}


class UnavailableConnector(Connector):
    """Stands in for a connector whose kind we don't know, so the rest still runs."""

    kind = "unknown"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float], why: str) -> None:
        super().__init__(spec, fmap, now)
        self.kind = str(spec.get("kind", "unknown"))
        self.why = why

    def check_config(self) -> None:
        """It already stands for a config that didn't load."""

    def send(self, command: dict) -> dict:
        return {"ok": False, "detail": self.why}

    def status(self) -> dict:
        return {"id": self.id, "kind": self.kind, "available": False, "detail": self.why}
