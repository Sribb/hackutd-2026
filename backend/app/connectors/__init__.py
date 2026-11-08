"""Connector registry: builds connectors from a blueprint's optional `connectors` list.

With no connectors, the simulator owns every asset, so the demo never depends on
hardware. With some, each declared (asset, signal) is read from its connector and
the simulator keeps the rest. A command goes to the connector that declares it in
`commands`, else to the simulator. SEND_TRUCK is a dispatch, so it always goes to
the simulator (which models the truck).
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from ..fieldmap import FieldMap
from .base import Connector, Reading, UnavailableConnector
from .sim import SimConnector
from .stubs import ModbusConnector, MqttConnector, OpcuaConnector
from .webhook import WebhookConnector

KINDS = {"webhook": WebhookConnector, "mqtt": MqttConnector, "modbus": ModbusConnector,
         "opcua": OpcuaConnector}

__all__ = ["Connector", "Reading", "Registry", "SimConnector", "WebhookConnector",
           "MqttConnector", "ModbusConnector", "OpcuaConnector", "KINDS"]


class Registry:
    def __init__(self, specs: Optional[list], fmap: FieldMap, now: Callable[[], float], *,
                 initial_pct: Optional[dict] = None, faults: Optional[list] = None,
                 sensors: Optional[dict] = None) -> None:
        self.fmap = fmap
        self.connectors: list[Connector] = []
        sim_spec: dict = {}
        for spec in specs or []:
            kind = str(spec.get("kind", "")).lower()
            if kind == "sim":
                sim_spec = spec
                continue
            cls = KINDS.get(kind)
            try:
                conn = cls(spec, fmap, now) if cls else UnavailableConnector(
                    spec, fmap, now, f"unknown connector kind {kind!r}")
            except Exception as e:  # noqa: BLE001 - a bad spec must not stop the backend
                conn = UnavailableConnector(spec, fmap, now, f"bad config: {e}")
            self.connectors.append(conn)
        claimed = {key for c in self.connectors for key in c.signals}
        self.sim = SimConnector(sim_spec, fmap, now, initial_pct=initial_pct, faults=faults,
                                sensors=sensors, skip=claimed)
        self.connectors.insert(0, self.sim)
        self.owner = {key: c for c in self.connectors[1:] for key in c.signals}

    def get(self, connector_id: str) -> Optional[Connector]:
        return next((c for c in self.connectors if c.id == connector_id), None)

    def route(self, command: dict) -> Connector:
        """The connector a confirmed command goes to."""
        if command.get("type") != "SEND_TRUCK":
            for c in self.connectors[1:]:
                if (command.get("asset_id"), command.get("type")) in c.commands:
                    return c
        return self.sim

    def path(self, command: dict) -> tuple:
        """What a command would go out over: the connector's id and kind, its config, and the
        command's address. Equal paths send the same command to the same place. The simulator
        is one path whatever its config: it moves no equipment."""
        c = self.route(command)
        if c is self.sim:
            return (c.id, c.kind)
        return (c.id, c.kind, json.dumps(c.config, sort_keys=True, default=str),
                json.dumps(c.commands.get((command.get("asset_id"), command.get("type"))), default=str))

    def advance(self, dt_min: float) -> None:
        for c in self.connectors:
            try:
                c.advance(dt_min)
            except Exception:  # noqa: BLE001
                pass

    def read(self) -> list[Reading]:
        out: list[Reading] = []
        for c in self.connectors:
            try:
                for r in c.read():
                    out.append({**r, "source": c.id})
            except Exception:  # noqa: BLE001 - its readings just go stale
                pass
        return out

    def status(self) -> list[dict]:
        """[{id, kind, status: "connected" | "unavailable", detail}], the simulator first."""
        out = []
        for c in self.connectors:
            try:
                s = c.status()
            except Exception as e:  # noqa: BLE001
                s = {"available": False, "detail": str(e)}
            out.append({"id": c.id, "kind": c.kind,
                        "status": "connected" if s.get("available") else "unavailable",
                        "detail": s.get("detail", "")})
        return out

    def close(self) -> None:
        for c in self.connectors:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
