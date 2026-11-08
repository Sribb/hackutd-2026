"""Connector registry: builds connectors from a blueprint's optional `connectors` list.

With no connectors, the simulator owns every asset, so the demo never depends on
hardware. With some, each declared (asset, signal) is read from its connector and
the simulator keeps the rest. A command goes to the connector that declares it in
`commands`. An asset that a real connector feeds (any signal or command declared
for it) never falls back to the simulator: a command no real connector declares for
it is refused ("no command path"), since the simulator moving would change nothing
in the field while the console said it had. Only assets no real connector touches
go to the simulator. SEND_TRUCK is a dispatch, so it always goes to the simulator
(which models the truck).
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from ..fieldmap import FieldMap
from .base import Connector, Reading, UnavailableConnector
from .modbus import ModbusConnector
from .mqtt import MqttConnector
from .sim import SimConnector
from .stubs import OpcuaConnector
from .webhook import WebhookConnector

KINDS = {"webhook": WebhookConnector, "mqtt": MqttConnector, "modbus": ModbusConnector,
         "opcua": OpcuaConnector}

__all__ = ["Connector", "Reading", "Registry", "SimConnector", "WebhookConnector",
           "MqttConnector", "ModbusConnector", "OpcuaConnector", "NoCommandPath", "KINDS"]


class NoCommandPath(Connector):
    """Where a command goes when a real connector feeds the asset but none declares that
    command. It sends nothing. `id` is the feeding connector, so the proposal names it."""

    kind = "none"

    def __init__(self, via: Connector, command: dict) -> None:
        self.id = via.id
        self.via = via
        self.config, self.signals, self.commands = {}, {}, {}
        self.detail = f"no command path to {command.get('asset_id')} on {via.id}"

    def send(self, command: dict) -> dict:
        return {"ok": False, "detail": f"{self.detail}: {via_hint(self.via, command)}"}

    def status(self) -> dict:
        return {"id": self.id, "kind": self.kind, "available": False, "detail": self.detail}


def via_hint(via: Connector, command: dict) -> str:
    return (f"the blueprint declares no {command.get('type')} command for it on {via.id} ({via.kind}), "
            f"so nothing was sent. Do it at the device, or add the command to the blueprint.")


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
            # Its assets stay its own even when it didn't load: they go UNKNOWN, never simulated.
            self.connectors.append(conn)
        claimed = {key for c in self.connectors for key in c.signals}
        self.sim = SimConnector(sim_spec, fmap, now, initial_pct=initial_pct, faults=faults,
                                sensors=sensors, skip=claimed)
        self.connectors.insert(0, self.sim)
        self.owner = {key: c for c in self.connectors[1:] for key in c.signals}
        self.feeders: dict[str, Connector] = {}       # asset id -> first real connector that touches it
        for c in self.connectors[1:]:
            for aid, _ in [*c.signals, *c.commands]:
                self.feeders.setdefault(aid, c)

    def get(self, connector_id: str) -> Optional[Connector]:
        return next((c for c in self.connectors if c.id == connector_id), None)

    def feeder(self, asset_id: str) -> Optional[Connector]:
        """The real connector this asset is read or commanded over, or None if only simulated."""
        return self.feeders.get(asset_id)

    def route(self, command: dict) -> Connector:
        """The connector a confirmed command goes to: the real one that declares it, a
        NoCommandPath (sends nothing) when a real connector feeds the asset but none
        declares it, else the simulator."""
        if command.get("type") == "SEND_TRUCK":
            return self.sim
        for c in self.connectors[1:]:
            if (command.get("asset_id"), command.get("type")) in c.commands:
                return c
        via = self.feeder(command.get("asset_id"))
        return NoCommandPath(via, command) if via is not None else self.sim

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

    def close(self) -> list[threading.Thread]:
        """Close every connector without blocking. Returns the cleanup threads still
        running, so shutdown can join them off the event loop."""
        pending = []
        for c in self.connectors:
            try:
                t = c.close()
            except Exception:  # noqa: BLE001
                t = None
            if isinstance(t, threading.Thread):
                pending.append(t)
        return pending
