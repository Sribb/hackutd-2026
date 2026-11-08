"""Shared by the fake field devices: load a blueprint and run the simulator as "the field".

The fakes are driven by the same blueprint the backend loads, so their topics and
registers always match the connector config, and by the backend's own simulator
(backend/app/connectors/sim.py), so the physics is the one the demo already uses.
The blueprint's scripted `faults` fire on the fake's clock, exactly as they would
in the backend's simulator.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # so `python backend/tools/x.py` works as well as `python -m backend.tools.x`
    sys.path.insert(0, str(ROOT))

from backend.app.connectors.sim import SimConnector  # noqa: E402
from backend.app.fieldmap import FieldMap  # noqa: E402
from backend.app.live import start_conditions  # noqa: E402
from backend.app.scenario import load_scenario  # noqa: E402

DEFAULT_BLUEPRINT = ROOT / "scenario" / "samples" / "sample1_pecos_mqtt_modbus.json"
FAULT_WORDS = {"trip": "TRIP", "stuck": "STUCK_VALVE", "stale": "SENSOR_STALE", "leak": "LEAK",
               "repair": "REPAIR"}


def find_connector(raw: dict, kind: str, connector_id: Optional[str] = None) -> dict:
    """The blueprint's connector spec of this kind (or with this id)."""
    for spec in raw.get("connectors") or []:
        if (connector_id and spec.get("id") == connector_id) or \
                (not connector_id and str(spec.get("kind", "")).lower() == kind):
            return spec
    raise SystemExit(f"No {kind} connector{' ' + connector_id if connector_id else ''} in the blueprint.")


class FieldSim:
    """The simulated field, thread-safe. `t` is live minutes since clock_start."""

    def __init__(self, blueprint: Path | str = DEFAULT_BLUEPRINT, seed: int = 11) -> None:
        self.path = Path(blueprint)
        self.raw = json.loads(self.path.read_text(encoding="utf-8"))
        scenario = load_scenario(self.path)
        start, initial, faults, sensors, _specs = start_conditions(scenario, self.raw)
        self.fmap = FieldMap.from_scenario(scenario, start)
        self.t = 0.0
        self.lock = threading.RLock()
        self.sim = SimConnector({"config": {"seed": seed}}, self.fmap, lambda: self.t,
                                initial_pct=initial, faults=faults, sensors=sensors)

    def advance(self, dt_min: float) -> list[dict]:
        """Move the field forward and return fresh readings."""
        with self.lock:
            self.sim.advance(dt_min)
            self.t += dt_min
            return self.sim.read()

    def readings(self) -> list[dict]:
        with self.lock:
            return self.sim.read()

    def command(self, command: dict) -> dict:
        with self.lock:
            res = self.sim.send(command)
            self.sim._step(0.0)  # flows follow the change now, not at the next tick
            return res

    def inject(self, fault: dict) -> str:
        with self.lock:
            why = self.sim.inject(fault)
            self.sim._step(0.0)
            return why

    def clock(self) -> str:
        return self.fmap.clock(self.t)


def parse_console_line(line: str) -> Optional[dict]:
    """'trip PUMP_5' -> {"type": "TRIP", "asset_id": "PUMP_5"}; None if it isn't a fault."""
    parts = line.strip().split()
    if len(parts) < 2 or parts[0].lower() not in FAULT_WORDS:
        return None
    return {"type": FAULT_WORDS[parts[0].lower()], "asset_id": parts[1].upper(),
            "reason": " ".join(parts[2:]) or f"{parts[0].lower()} injected on the fake field"}


CONSOLE_HELP = "Type a fault and Enter: trip PUMP_5 | stuck VALVE_D_5 | stale TANK_6 | leak TANK_7 | repair PUMP_5"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.02)
    raise TimeoutError(f"nothing listening on {host}:{port}")


class LoopThread:
    """An asyncio loop on a daemon thread, so tests can run a fake device in-process."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="fake-device", daemon=True)
        self.thread.start()

    def run(self, coro, timeout: float = 10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call(self, fn, *args, timeout: float = 10.0):
        async def wrap():
            return fn(*args)
        return self.run(wrap(), timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


async def run_field(field: FieldSim, devices: list, speed: float, interval: float) -> None:
    """Advance the field every `interval` real seconds and hand the readings to each device."""
    for d in devices:
        d.publish(field.readings())
    while True:
        await asyncio.sleep(interval)
        readings = field.advance(speed * interval)
        for d in devices:
            d.publish(readings)


def start_console(field: FieldSim) -> None:
    """Read fault commands from stdin on a thread ('trip PUMP_5')."""
    def loop() -> None:
        for line in sys.stdin:
            fault = parse_console_line(line)
            if fault is None:
                if line.strip():
                    print(CONSOLE_HELP, flush=True)
                continue
            why = field.inject(fault)
            print(f"[{field.clock()}] {why or 'injected ' + fault['type'] + ' on ' + fault['asset_id']}", flush=True)
    if sys.stdin and sys.stdin.isatty():
        print(CONSOLE_HELP, flush=True)
    threading.Thread(target=loop, name="fault-console", daemon=True).start()


def default_speed() -> float:
    return float(os.environ.get("FREEBOARD_SPEED", "0.25"))
