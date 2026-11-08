"""Fake SCADA over MQTT: a local broker plus a field that publishes readings from the simulator.

    python -m backend.tools.fake_scada_mqtt                     # broker on 127.0.0.1:1883
    python -m backend.tools.fake_scada_mqtt --port 1884 --blueprint scenario/samples/sample1_pecos_mqtt_modbus.json
    python -m backend.tools.fake_scada_mqtt --broker 127.0.0.1:1883   # publish to mosquitto instead

The topics come from the blueprint's mqtt connector: every declared signal is
published to `topic_prefix + address` as a plain value every `--interval` seconds,
and every declared command topic is subscribed to and applied to the simulator
(`OPEN`/`CLOSED`, `ON`/`OFF`, or a choke fraction). Type `trip PUMP_1` and Enter to
trip a pump on the field side.

For one coherent field over both protocols, run backend/tools/fake_field.py instead.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.connectors.mqtt import parse_payload  # noqa: E402
from backend.tools.fake_common import (DEFAULT_BLUEPRINT, FieldSim, LoopThread, default_speed,  # noqa: E402
                                       find_connector, run_field, start_console, wait_for_port)
from backend.tools.mqtt_broker import MqttBroker  # noqa: E402

log = logging.getLogger("freeboard.fake_scada")


def payload_text(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


class FakeScadaMqtt:
    """The field side of the mqtt connector. Owns an embedded broker unless `broker` is given."""

    def __init__(self, field: FieldSim, spec: dict, host: str = "127.0.0.1", port: int = 1883,
                 broker: Optional[str] = None) -> None:
        self.field = field
        prefix = str((spec.get("config") or {}).get("topic_prefix") or "")
        self.topics = {(s["asset_id"], s["signal"]): prefix + (s.get("address") or f"{s['asset_id']}/{s['signal']}")
                       for s in spec.get("signals") or []}
        self.cmd_topics = {prefix + (c.get("address") or f"{c['asset_id']}/set"): (c["asset_id"], c["command"])
                           for c in spec.get("commands") or []}
        self.host, self.port = host, port
        self.external = broker
        self.broker: Optional[MqttBroker] = None
        self.client = None
        self.commands: list[dict] = []      # every command received: {topic, payload, asset_id, command, result}
        self.published = 0
        self._bg: Optional[LoopThread] = None

    # ---- lifecycle ----
    async def start(self) -> None:
        if self.external:
            self._start_external()
        else:
            self.broker = MqttBroker(self.host, self.port, on_message=self._on_command)
            await self.broker.start()
            self.port = self.broker.port

    async def stop(self) -> None:
        if self.broker is not None:
            await self.broker.stop()
        if self.client is not None:
            self.client.loop_stop()
            self.client.disconnect()

    def _start_external(self) -> None:
        import paho.mqtt.client as mqtt
        host, _, port = self.external.partition(":")
        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="freeboard-fake-scada",
                        protocol=mqtt.MQTTv311)
        c.on_connect = lambda cl, *_: cl.subscribe([(t, 1) for t in self.cmd_topics])
        c.on_message = lambda _c, _u, msg: self._on_command(msg.topic, msg.payload)
        c.connect(host, int(port or 1883))
        c.loop_start()
        self.client = c

    def start_in_thread(self) -> "FakeScadaMqtt":
        """Start the broker on a background loop (tests). Use publish_now() to send readings."""
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
        """Publish readings (default: the field's current ones) from the broker's loop."""
        readings = self.field.readings() if readings is None else readings
        if self._bg is not None:
            self._bg.call(self.publish, readings)
        else:
            self.publish(readings)

    # ---- the field side ----
    def publish(self, readings: list[dict]) -> None:
        for r in readings:
            topic = self.topics.get((r["asset_id"], r["signal"]))
            if topic is None:
                continue
            text = payload_text(r["value"])
            if self.broker is not None:
                self.broker.publish(topic, text)
            elif self.client is not None:
                self.client.publish(topic, text, qos=0)
            self.published += 1

    def _on_command(self, topic: str, payload: bytes) -> None:
        target = self.cmd_topics.get(topic)
        if target is None:
            return
        aid, cmd = target
        result = self.field.command({"type": cmd, "asset_id": aid, "value": parse_payload(payload)})
        entry = {"topic": topic, "payload": payload.decode("utf-8", "replace"), "asset_id": aid,
                 "command": cmd, "result": result}
        self.commands.append(entry)
        print(f"[{self.field.clock()}] MQTT {topic} <- {entry['payload']}: {result['detail']}", flush=True)
        self.publish(self.field.readings())


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--blueprint", default=str(DEFAULT_BLUEPRINT))
    ap.add_argument("--connector", help="connector id (default: the first mqtt connector)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None, help="broker port (default: the connector's port, else 1883)")
    ap.add_argument("--broker", help="host:port of an existing broker to publish to instead of the embedded one")
    ap.add_argument("--speed", type=float, default=default_speed(), help="live minutes per real second")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between publishes")
    a = ap.parse_args(argv)
    field = FieldSim(a.blueprint)
    spec = find_connector(field.raw, "mqtt", a.connector)
    port = a.port or int((spec.get("config") or {}).get("port") or 1883)
    dev = FakeScadaMqtt(field, spec, a.host, port, broker=a.broker)

    async def run() -> None:
        await dev.start()
        where = a.broker or f"{a.host}:{dev.port}"
        print(f"Fake SCADA: {len(dev.topics)} signal topics, {len(dev.cmd_topics)} command topics on {where} "
              f"({spec['id']}, {Path(a.blueprint).name})", flush=True)
        start_console(field)
        await run_field(field, [dev], a.speed, a.interval)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
