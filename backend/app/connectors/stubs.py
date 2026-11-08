"""Stubs for industrial protocols: MQTT, Modbus TCP, OPC UA.

Each imports its library only when asked for its status, so the backend runs without
any of them installed. Until a driver is written, each reports why it isn't
available (library missing, config missing, or driver not written yet), returns no
readings, and refuses commands. Its assets then go stale and show as UNKNOWN,
never as OK, which is the safe failure.
"""
from __future__ import annotations

import importlib
from typing import Optional

from .base import Connector, Reading


class LibraryStub(Connector):
    library = ""        # module to import
    package = ""        # what to pip install
    required = ()       # config keys that must be present

    _import_error: Optional[str] = None
    _checked = False

    def _library_ok(self) -> bool:
        if not self._checked:
            self._checked = True
            try:
                importlib.import_module(self.library)
            except Exception as e:  # noqa: BLE001 - ImportError, or a broken install
                self._import_error = f"{self.package} is not installed (pip install {self.package}): {e}"
        return self._import_error is None

    def why_unavailable(self) -> str:
        if not self._library_ok():
            return self._import_error or f"{self.package} is not installed"
        missing = [k for k in self.required if not self.config.get(k)]
        if missing:
            return f"not configured: config needs {', '.join(missing)}"
        for key, var in self.config.items():
            if key.endswith("_env") and var and self.secret(key[:-4]) is None:
                return f"environment variable {var} is not set"
        return f"the {self.kind} driver is a stub in this build; nothing is read or sent"

    def read(self) -> list[Reading]:
        return []

    def send(self, command: dict) -> dict:
        return {"ok": False, "detail": f"{self.id} ({self.kind}) is not available: {self.why_unavailable()}."}

    def status(self) -> dict:
        return {"id": self.id, "kind": self.kind, "available": False, "detail": self.why_unavailable()}


class MqttConnector(LibraryStub):
    kind = "mqtt"
    library = "paho.mqtt.client"
    package = "paho-mqtt"
    required = ("host",)


class ModbusConnector(LibraryStub):
    kind = "modbus"
    library = "pymodbus.client"
    package = "pymodbus"
    required = ("host",)


class OpcuaConnector(LibraryStub):
    kind = "opcua"
    library = "asyncua"
    package = "asyncua"
    required = ("endpoint",)
