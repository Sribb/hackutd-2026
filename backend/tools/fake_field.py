"""One fake field, two protocols: the MQTT SCADA and the Modbus PLC share one simulator.

    python -m backend.tools.fake_field          # broker 127.0.0.1:1883 + PLC 127.0.0.1:5020

Then start the backend on the same blueprint:

    FREEBOARD_SCENARIO=scenario/samples/sample1_pecos_mqtt_modbus.json \\
        python -m uvicorn backend.app.main:app --port 8000

Because both fakes read one simulated field, a command sent over Modbus (say, a
crossover between Tank 3 and Tank 4) moves the levels the MQTT side reports. Type
`trip PUMP_5` and Enter to trip a pump in the field.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.tools.fake_common import (DEFAULT_BLUEPRINT, FieldSim, default_speed, find_connector,  # noqa: E402
                                       run_field, start_console)
from backend.tools.fake_plc_modbus import FakePlc  # noqa: E402
from backend.tools.fake_scada_mqtt import FakeScadaMqtt  # noqa: E402


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--blueprint", default=str(DEFAULT_BLUEPRINT))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=None, help="default: the mqtt connector's port, else 1883")
    ap.add_argument("--modbus-port", type=int, default=None, help="default: the modbus connector's port, else 5020")
    ap.add_argument("--speed", type=float, default=default_speed(), help="live minutes per real second")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between updates")
    a = ap.parse_args(argv)
    field = FieldSim(a.blueprint)
    mq = find_connector(field.raw, "mqtt")
    mb = find_connector(field.raw, "modbus")
    scada = FakeScadaMqtt(field, mq, a.host, a.mqtt_port or int((mq.get("config") or {}).get("port") or 1883))
    plc = FakePlc(field, mb, a.host, a.modbus_port or int((mb.get("config") or {}).get("port") or 5020))

    async def run() -> None:
        await scada.start()
        await plc.start()
        print(f"Fake field ({Path(a.blueprint).name}):\n"
              f"  MQTT   {mq['id']} on {a.host}:{scada.port}: {len(scada.topics)} signals, {len(scada.cmd_topics)} commands\n"
              f"  Modbus {mb['id']} on {a.host}:{plc.port}: {len(plc.points)} signals, {len(plc.cmd_regs)} commands",
              flush=True)
        start_console(field)
        await run_field(field, [scada, plc], a.speed, a.interval)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
