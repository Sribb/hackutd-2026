"""Fake PLC over Modbus TCP: a pymodbus server whose registers follow the simulator.

    python -m backend.tools.fake_plc_modbus                     # 127.0.0.1:5020, unit 1
    python -m backend.tools.fake_plc_modbus --port 5021 --blueprint scenario/samples/sample1_pecos_mqtt_modbus.json

The register map comes from the blueprint's modbus connector, so it always matches
what the backend polls. Every `--interval` seconds the simulator advances and each
declared signal is written to its register as `round(value / scale)`; a sensor the
simulator reports as dead reads 65535 (bad quality). A write to a declared command
register (SET_VALVE, SET_PUMP 1/0, CHOKE_WELL percent) is applied to the simulator
and printed. Type `trip PUMP_5` and Enter to trip a pump on the PLC side.

For one coherent field over both protocols, run backend/tools/fake_field.py instead.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.connectors.modbus import parse_address  # noqa: E402
from backend.tools.fake_common import (DEFAULT_BLUEPRINT, FieldSim, LoopThread, default_speed,  # noqa: E402
                                       find_connector, run_field, start_console, wait_for_port)

try:  # pymodbus 3.8 names; newer releases renamed slave -> device
    from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext
    try:
        from pymodbus.datastore import ModbusSlaveContext as _DeviceContext
    except ImportError:  # pragma: no cover
        from pymodbus.datastore import ModbusDeviceContext as _DeviceContext
    from pymodbus.server import ModbusTcpServer
except ImportError as e:  # pragma: no cover
    raise SystemExit(f"The fake PLC needs pymodbus: pip install -r backend/requirements.txt ({e})")

FC = {"coil": 1, "di": 2, "hr": 3, "ir": 4}
TABLE_OF = {"c": "coil", "d": "di", "h": "hr", "i": "ir"}
BAD = 65535


def _block() -> ModbusSequentialDataBlock:
    return ModbusSequentialDataBlock(0, [0] * 65537)


class PlcContext(_DeviceContext):
    """Calls `on_write(table, address, values)` for every write a Modbus client makes."""

    def __init__(self, on_write) -> None:
        super().__init__(di=_block(), co=_block(), hr=_block(), ir=_block())
        self.on_write = on_write

    async def async_setValues(self, fc_as_hex, address, values):  # noqa: N802 - pymodbus name
        await super().async_setValues(fc_as_hex, address, values)
        self.on_write(TABLE_OF[self.decode(fc_as_hex)], address, list(values))


class FakePlc:
    """The field side of the modbus connector."""

    def __init__(self, field: FieldSim, spec: dict, host: str = "127.0.0.1", port: int = 5020) -> None:
        self.field = field
        self.host, self.port = host, port
        self.points: dict[tuple, tuple[str, int, float]] = {}       # (asset, signal) -> (table, address, scale)
        for s in spec.get("signals") or []:
            table, n = parse_address(s.get("address"))
            self.points[(s["asset_id"], s["signal"])] = (table, n, float(s.get("scale") or 1.0))
        self.cmd_regs: dict[tuple, tuple[str, str]] = {}           # (table, address) -> (asset, command)
        for c in spec.get("commands") or []:
            self.cmd_regs[parse_address(c.get("address"))] = (c["asset_id"], c["command"])
        self.device = PlcContext(self._on_write)
        self.context = ModbusServerContext(self.device, single=True)
        self.server: Optional[ModbusTcpServer] = None
        self.writes: list[dict] = []        # every client write: {table, address, value, asset_id?, command?, result?}
        self._bg: Optional[LoopThread] = None

    # ---- lifecycle ----
    async def start(self) -> None:
        self.publish(self.field.readings())
        self.server = ModbusTcpServer(self.context, address=(self.host, self.port))
        await self.server.serve_forever(background=True)

    async def stop(self) -> None:
        if self.server is not None:
            await self.server.shutdown()
            self.server = None

    def start_in_thread(self) -> "FakePlc":
        self._bg = LoopThread()
        self._bg.run(self.start())
        wait_for_port(self.host, self.port)
        return self

    def stop_thread(self) -> None:
        if self._bg is not None:
            self._bg.run(self.stop())
            self._bg.stop()
            self._bg = None

    def publish_now(self, readings: Optional[list] = None) -> None:
        self.publish(self.field.readings() if readings is None else readings)

    # ---- registers ----
    def register(self, address) -> int | bool:
        """Read one register as a client would see it, e.g. register('hr:0')."""
        table, n = parse_address(address)
        return self.device.getValues(FC[table], n, 1)[0]

    def publish(self, readings: list[dict]) -> None:
        got = {(r["asset_id"], r["signal"]): r["value"] for r in readings}
        for key, (table, n, scale) in self.points.items():
            if key not in got:
                if table in ("hr", "ir"):
                    self.device.setValues(FC[table], n, [BAD])      # dead sensor: bad quality
                continue
            v = got[key]
            if isinstance(v, (bool, str)):                         # running, position
                on = v is True or str(v).upper() in ("OPEN", "ON")
                raw = on if table in ("coil", "di") else int(on)
            elif table in ("coil", "di"):
                raw = bool(v)
            else:
                raw = max(0, min(BAD - 1, int(round(float(v) / scale))))
            self.device.setValues(FC[table], n, [raw])

    def _on_write(self, table: str, address: int, values: list) -> None:
        for i, raw in enumerate(values):
            entry = {"table": table, "address": address + i, "value": int(raw)}
            target = self.cmd_regs.get((table, address + i))
            if target is not None:
                aid, cmd = target
                if cmd == "SET_VALVE":
                    value = "OPEN" if raw else "CLOSED"
                elif cmd == "SET_PUMP":
                    value = "ON" if raw else "OFF"
                else:
                    value = max(0.0, min(1.0, int(raw) / 100.0))
                result = self.field.command({"type": cmd, "asset_id": aid, "value": value})
                entry.update(asset_id=aid, command=cmd, result=result)
                print(f"[{self.field.clock()}] Modbus write {table}:{address + i} = {int(raw)} "
                      f"-> {cmd} {aid} {value}: {result['detail']}", flush=True)
            self.writes.append(entry)
        self.publish(self.field.readings())


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--blueprint", default=str(DEFAULT_BLUEPRINT))
    ap.add_argument("--connector", help="connector id (default: the first modbus connector)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None, help="default: the connector's port, else 5020")
    ap.add_argument("--speed", type=float, default=default_speed(), help="live minutes per real second")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between register updates")
    a = ap.parse_args(argv)
    field = FieldSim(a.blueprint)
    spec = find_connector(field.raw, "modbus", a.connector)
    port = a.port or int((spec.get("config") or {}).get("port") or 5020)
    plc = FakePlc(field, spec, a.host, port)

    async def run() -> None:
        await plc.start()
        print(f"Fake PLC: {len(plc.points)} signal registers, {len(plc.cmd_regs)} command registers on "
              f"{a.host}:{port} ({spec['id']}, {Path(a.blueprint).name})", flush=True)
        start_console(field)
        await run_field(field, [plc], a.speed, a.interval)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
