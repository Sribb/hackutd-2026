"""Start every fake the connectors demo needs, on 127.0.0.1 only. No real equipment.

    python -m backend.fakes [--opcua-port 4840] [--sparkplug-port 1886] [--workdir DIR]

It starts the fake OPC UA server (backend/fakes/opcua.py) and a fake Sparkplug B edge
node with its own MQTT broker (backend/fakes/sparkplug.py), the two connectors that
backend/data/samples/connectors_demo.json declares. Then it prints the environment
variables to export in the backend's shell before starting it. The ports in the demo
blueprint are 4840 and 1886; with other ports, change the blueprint to match.

The OPC UA login password comes from FREEBOARD_CONN_OPCUA_PASSWORD when it is set, and
is then not printed. When it is not set, the fake makes up a throwaway password for this
run and prints it in the export line, since it opens nothing but this local fake.
Certificates go to --workdir, or to a temp dir that is deleted when this stops.
Ctrl+C stops everything.
"""
from __future__ import annotations

import argparse
import copy
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Optional

from backend.fakes import opcua, sparkplug

# The demo edge node: the stock demo device plus VALVE_D_3, so Sparkplug B owns the TANK_3,
# PUMP_3, VALVE_D_3 line and OPC UA owns TANK_6, PUMP_6, VALVE_D_6 (no signal has two owners).
DEMO_DEVICES = copy.deepcopy(sparkplug.DEMO_DEVICES)
DEMO_DEVICES["PLC_1"].pop("VALVE_D_6/Open", None)
DEMO_DEVICES["PLC_1"]["VALVE_D_3/Open"] = ("Boolean", True)


class DemoFakes:
    """The running fakes. stop() stops them all and removes a temp certificate dir."""

    def __init__(self, opcua_port: int = 4840, sparkplug_port: int = 1886, workdir: Optional[str] = None,
                 password: Optional[str] = None, period_s: float = 1.0) -> None:
        self.password_generated = not password
        self.temp_workdir = workdir is None
        self.opcua = opcua.start(port=opcua_port or None, workdir=workdir,
                                 password=password or secrets.token_urlsafe(16))
        try:
            self.sparkplug = sparkplug.start(None, "FREEBOARD", "EDGE_1", devices=copy.deepcopy(DEMO_DEVICES),
                                             period_s=period_s, broker_listen_port=sparkplug_port)
        except Exception:
            self._stop_opcua()
            raise

    def env(self) -> dict[str, str]:
        """What the backend's connectors read. The password only when it was made up here."""
        out = {"FREEBOARD_CONN_OPCUA_CERT": str(Path(self.opcua.client_cert).resolve()),
               "FREEBOARD_CONN_OPCUA_KEY": str(Path(self.opcua.client_key).resolve()),
               "FREEBOARD_CONN_OPCUA_SERVER_CERT": str(Path(self.opcua.server_cert).resolve())}
        if self.password_generated:
            out[opcua.PASSWORD_ENV] = self.opcua.password
        return out

    def step(self) -> None:
        """Let TANK_3 rise slowly on the edge node, as the single fake does."""
        level = self.sparkplug.get("PLC_1", "TANK_3/Level")
        if isinstance(level, float) and level < 99.5:
            self.sparkplug.set("PLC_1", "TANK_3/Level", round(level + 0.05, 3))

    def _stop_opcua(self) -> None:
        try:
            self.opcua.stop()
        finally:
            if self.temp_workdir:
                shutil.rmtree(self.opcua.workdir, ignore_errors=True)

    def stop(self) -> None:
        try:
            self.sparkplug.stop()
        finally:
            self._stop_opcua()


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description="Start the fakes for the Freeboard connectors demo (127.0.0.1 only).")
    ap.add_argument("--opcua-port", type=int, default=4840)
    ap.add_argument("--sparkplug-port", type=int, default=1886, help="port of the fake MQTT broker")
    ap.add_argument("--workdir", default=None, help="where to keep the OPC UA certificates (default: a temp dir)")
    ap.add_argument("--period", type=float, default=1.0, help="seconds between Sparkplug data reports")
    a = ap.parse_args(argv)
    fakes = DemoFakes(a.opcua_port, a.sparkplug_port, a.workdir, os.environ.get(opcua.PASSWORD_ENV), a.period)
    print(f"fake OPC UA server   {fakes.opcua.endpoint} (SignAndEncrypt, username {fakes.opcua.username})")
    print(f"fake Sparkplug B     edge node FREEBOARD/EDGE_1, device PLC_1, broker mqtt://127.0.0.1:{fakes.sparkplug.port}")
    print("Export these in the backend's shell, then start it and activate "
          "backend/data/samples/connectors_demo.json:")
    for k, v in fakes.env().items():
        print(f"  export {k}='{v}'")
    if not fakes.password_generated:
        print(f"  ({opcua.PASSWORD_ENV} is already set; it is not printed)")
    print("Ctrl+C stops the fakes.", flush=True)
    try:
        while True:
            time.sleep(1.0)
            fakes.step()
    except KeyboardInterrupt:
        pass
    finally:
        fakes.stop()


if __name__ == "__main__":
    main()
