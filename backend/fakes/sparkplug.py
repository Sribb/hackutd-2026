"""A fake Sparkplug B edge node with one or more devices, for the tests and the demo.

It connects to an MQTT broker on 127.0.0.1 (the fake broker in mqtt_broker.py, or one
it starts itself) and behaves like a Sparkplug 3.0 edge node:

- Each MQTT session gets the next bdSeq. The will is an NDEATH carrying it, and the
  NBIRTH carries it too, with "Node Control/Rebirth" and the node metrics. A DBIRTH
  per device follows. Births name every metric with its datatype and an alias.
- Every period_s it publishes NDATA and DDATA with every metric by alias only, the way
  real edge nodes save bytes, so a host has to use the birth's alias map. A change from
  set() goes out at once. Every message after NBIRTH carries the next seq (mod 256).
- NCMD "Node Control/Rebirth" = true makes it publish its births again. Any other NCMD
  metric, and every DCMD metric, is a write: it is recorded in .writes, the metric
  takes the new value, and the change goes out in the next NDATA or DDATA at once.
- die() cuts the socket without DISCONNECT, so the broker publishes the NDEATH will;
  revive() starts a new session. If the broker goes away it reconnects by itself.
- skip_seq() makes the next message skip a sequence number, null() makes a metric
  report is_null, publish_historical() sends an is_historical value, and
  publish_retained_stale() leaves a retained DDATA with old values on the broker.

    python -m backend.fakes.sparkplug                 # its own broker on 127.0.0.1:1886
    python -m backend.fakes.sparkplug --broker-port 1885   # use a broker already running

In-process, start(...) returns a FakeEdgeNode; .port is the broker port and stop()
stops the node (and the broker, when it started one).
"""
from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any, Optional

from backend.app.connectors import sparkplug_codec as codec
from backend.fakes import mqtt_broker
from backend.fakes.mqtt_broker import HOST, LoopThread, MqttClient

NS = "spBv1.0"
REBIRTH = "Node Control/Rebirth"

# The demo device: what the Freeboard demo blueprint maps (see demo_spec in the docs).
DEMO_DEVICES = {
    "PLC_1": {
        "TANK_3/Level": ("Float", 62.0),
        "PUMP_3/Running": ("Boolean", True),
        "PUMP_3/Flow": ("Float", 118.0),
        "VALVE_D_6/Open": ("Boolean", True),
    },
}


def _ms() -> int:
    return int(time.time() * 1000)


class FakeEdgeNode:
    """One edge node on a broker. Every public method is safe from any thread."""

    def __init__(self, broker_port: int, group: str = "FREEBOARD", node: str = "EDGE_1",
                 devices: Optional[dict] = None, node_metrics: Optional[dict] = None,
                 period_s: float = 0.5, keepalive: int = 5, host: str = HOST) -> None:
        if host not in mqtt_broker.LOOPBACK:
            raise ValueError("the fake edge node only talks to a broker on 127.0.0.1")
        self.host, self.port = host, broker_port
        self.group, self.node = group, node
        self.period_s = period_s
        self.keepalive = keepalive
        # scope ("" for the node, else a device) -> metric -> [datatype, value]
        self.metrics: dict[str, dict[str, list]] = {"": {}}
        for name, (dt, value) in (node_metrics or {}).items():
            self.metrics[""][name] = [codec.DATATYPES.get(dt, dt), value]
        for dev, metrics in (DEMO_DEVICES if devices is None else devices).items():
            self.metrics[dev] = {name: [codec.DATATYPES.get(dt, dt), value] for name, (dt, value) in metrics.items()}
        self.aliases: dict[tuple, int] = {}
        n = 1
        for scope, metrics in self.metrics.items():
            for name in metrics:
                self.aliases[(scope, name)] = n
                n += 1
        self.by_alias = {a: key for key, a in self.aliases.items()}
        self.nulls: set = set()
        self.bdseq = -1
        self.seq = 0
        self._skip = 0
        self.alive = False            # connected and born
        self.dead = False             # died on purpose: don't reconnect by itself
        self.stopped = False
        self.writes: list[dict] = []          # DCMD and NCMD writes (not rebirth requests)
        self.rebirth_requests = 0
        self.births = 0                       # NBIRTHs published
        self.sent: list[dict] = []            # {"type", "device", "seq", "retain"} per publish
        self.client: Optional[MqttClient] = None
        self.lt = LoopThread(f"fake-sparkplug-{node}")
        self.broker = None                    # set by start() when it started one
        try:
            self.lt.run(self._connect())
        except Exception:
            self.lt.stop()
            raise
        self._ticker = asyncio.run_coroutine_threadsafe(self._tick_loop(), self.lt.loop)

    # ---------- topics and payloads ----------
    def _topic(self, mtype: str, device: str = "") -> str:
        base = f"{NS}/{self.group}/{mtype}/{self.node}"
        return f"{base}/{device}" if device else base

    def _next_seq(self) -> int:
        self.seq = (self.seq + 1 + self._skip) % 256
        self._skip = 0
        return self.seq

    def _metric(self, scope: str, name: str, birth: bool, ts: int) -> dict:
        dt, value = self.metrics[scope][name]
        m = {"alias": self.aliases[(scope, name)], "timestamp": ts, "datatype": dt}
        if birth:
            m["name"] = name
        if (scope, name) in self.nulls:
            m["is_null"] = True
        else:
            m["value"] = value
        return m

    def _publish(self, mtype: str, device: str, metrics: list, seq: Optional[int], retain: bool = False,
                 datatypes: bool = True) -> None:
        if self.client is None:
            return
        payload = codec.encode_payload({"timestamp": _ms(), "metrics": metrics, "seq": seq}, datatypes=datatypes)
        if self.client.publish(self._topic(mtype, device), payload, retain=retain):
            self.sent.append({"type": mtype, "device": device, "seq": seq, "retain": retain})

    def _ndeath_payload(self) -> bytes:
        return codec.encode_payload({"timestamp": _ms(), "metrics": [
            {"name": "bdSeq", "datatype": codec.INT64, "value": self.bdseq}]})

    # ---------- session (runs on the fake's loop) ----------
    async def _connect(self) -> None:
        self.bdseq = (self.bdseq + 1) % 256
        self.client = MqttClient(self.host, self.port, f"fake-sparkplug-{self.group}-{self.node}",
                                 keepalive=self.keepalive,
                                 will=(self._topic("NDEATH"), self._ndeath_payload(), 1, False),
                                 on_message=self._on_message, on_lost=self._on_lost)
        await self.client.connect()
        await self.client.subscribe([(self._topic("NCMD"), 0), (self._topic("DCMD", "+"), 0)])
        self._birth()

    def _birth(self) -> None:
        ts = _ms()
        self.seq = 0
        node = [{"name": "bdSeq", "datatype": codec.INT64, "value": self.bdseq, "timestamp": ts},
                {"name": REBIRTH, "datatype": codec.BOOLEAN, "value": False, "timestamp": ts}]
        node += [self._metric("", name, True, ts) for name in self.metrics[""]]
        self._publish("NBIRTH", "", node, 0)
        self.births += 1
        for dev in self.metrics:
            if dev:
                self._publish("DBIRTH", dev, [self._metric(dev, n, True, ts) for n in self.metrics[dev]],
                              self._next_seq())
        self.alive = True

    def _report(self, scope: Optional[str] = None, names: Optional[list] = None) -> None:
        """NDATA/DDATA for `names` in `scope` (or everything), by alias only."""
        if not self.alive:
            return
        ts = _ms()
        for dev, metrics in self.metrics.items():
            if scope is not None and dev != scope:
                continue
            chosen = [n for n in metrics if names is None or n in names]
            if not chosen:
                continue
            self._publish("DDATA" if dev else "NDATA", dev,
                          [self._metric(dev, n, False, ts) for n in chosen], self._next_seq(), datatypes=False)

    async def _tick_loop(self) -> None:
        while not self.stopped:
            await asyncio.sleep(self.period_s)
            self._report()

    def _on_message(self, topic: str, payload: bytes, _retain: bool) -> None:
        parts = topic.split("/")
        if len(parts) < 4 or parts[3] != self.node:
            return
        mtype = parts[2]
        scope = parts[4] if mtype == "DCMD" and len(parts) == 5 else ""
        if mtype not in ("NCMD", "DCMD") or scope not in self.metrics:
            return
        try:
            p = codec.decode_payload(payload)
        except codec.CodecError:
            return
        changed = []
        for m in p["metrics"]:
            name = m["name"]
            if name is None and m["alias"] is not None:
                key = self.by_alias.get(m["alias"])
                name = key[1] if key and key[0] == scope else None
            if mtype == "NCMD" and name == REBIRTH:
                if m["value"] is True:
                    self.rebirth_requests += 1
                    self._birth()
                continue
            if name not in self.metrics[scope]:
                continue
            self.writes.append({"type": mtype, "device": scope, "metric": name, "value": m["value"],
                                "datatype": m["datatype"], "t": time.time()})
            self.metrics[scope][name][1] = m["value"]
            self.nulls.discard((scope, name))
            changed.append(name)
        if changed:
            self._report(scope, changed)

    def _on_lost(self) -> None:
        self.alive = False
        if not (self.dead or self.stopped):
            self.lt.loop.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        while not (self.dead or self.stopped or self.alive):
            try:
                await self._connect()
                return
            except (OSError, ConnectionError, asyncio.TimeoutError):
                await asyncio.sleep(0.3)

    # ---------- the handle ----------
    def set(self, device: str, metric: str, value: Any) -> None:
        """Give a metric a new value (device "" for a node metric) and report it at once."""
        def do():
            self.metrics[device][metric][1] = value
            self.nulls.discard((device, metric))
            self._report(device, [metric])
        self.lt.call(do)

    def get(self, device: str, metric: str) -> Any:
        return self.lt.call(lambda: self.metrics[device][metric][1])

    def null(self, device: str, metric: str) -> None:
        """Report the metric as is_null (no value) until set() gives it one."""
        def do():
            self.nulls.add((device, metric))
            self._report(device, [metric])
        self.lt.call(do)

    def skip_seq(self, n: int = 1) -> None:
        """The next message skips n sequence numbers."""
        def do():
            self._skip += n
        self.lt.call(do)

    def publish_historical(self, device: str, metric: str, value: Any, age_s: float = 600.0) -> None:
        """A DDATA (NDATA for device "") with this value marked is_historical."""
        def do():
            if not self.alive:
                return
            dt = self.metrics[device][metric][0]
            m = {"alias": self.aliases[(device, metric)], "timestamp": _ms() - int(age_s * 1000),
                 "datatype": dt, "value": value, "is_historical": True}
            self._publish("DDATA" if device else "NDATA", device, [m], self._next_seq())
        self.lt.call(do)

    def publish_retained_stale(self, device: str, metric: str, value: Any, age_s: float = 3600.0) -> None:
        """Leave a retained DDATA with this value and an old timestamp on the broker. It
        uses the current seq without moving it on, like a replay would."""
        def do():
            dt = self.metrics[device][metric][0]
            m = {"name": metric, "alias": self.aliases[(device, metric)], "datatype": dt, "value": value,
                 "timestamp": _ms() - int(age_s * 1000)}
            self._publish("DDATA" if device else "NDATA", device, [m], self.seq, retain=True)
        self.lt.call(do)

    def clear_retained(self, device: str) -> None:
        def do():
            if self.client is not None:
                self.client.publish(self._topic("DDATA" if device else "NDATA", device), b"", retain=True)
        self.lt.call(do)

    def die(self) -> None:
        """Cut the connection without DISCONNECT: the broker publishes the NDEATH will."""
        def do():
            self.dead, self.alive = True, False
            if self.client is not None:
                self.client.drop()
        self.lt.call(do)

    def revive(self) -> None:
        """A new session: the next bdSeq, NBIRTH and DBIRTH."""
        def go():
            self.dead = False
            return self._connect()
        self.lt.run(go())

    def stop(self) -> None:
        """Publish NDEATH, disconnect, and stop (and stop the broker if start() made one)."""
        if self.stopped:
            return
        self.stopped = True

        async def shut():
            self._ticker.cancel()
            if self.client is not None:
                if self.alive:
                    self.client.publish(self._topic("NDEATH"), self._ndeath_payload())
                await self.client.disconnect()
            self.alive = False
        try:
            self.lt.run(shut(), timeout=5)
        except Exception:  # noqa: BLE001 - stopping must not raise
            pass
        self.lt.stop()
        if self.broker is not None:
            self.broker.stop()


def start(broker_port: Optional[int] = None, group: str = "FREEBOARD", node: str = "EDGE_1",
          devices: Optional[dict] = None, node_metrics: Optional[dict] = None,
          period_s: float = 0.5, broker_listen_port: int = 0) -> FakeEdgeNode:
    """Start a fake edge node. With broker_port None it starts its own broker on
    127.0.0.1 (broker_listen_port, 0 picks a free one) and stop() stops that too.
    devices is {device: {metric: (datatype name, value)}}; DEMO_DEVICES by default."""
    broker = None
    if broker_port is None:
        broker = mqtt_broker.start(port=broker_listen_port)
        broker_port = broker.port
    try:
        fake = FakeEdgeNode(broker_port, group, node, devices, node_metrics, period_s)
    except Exception:
        if broker is not None:
            broker.stop()
        raise
    fake.broker = broker
    return fake


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description="A fake Sparkplug B edge node on 127.0.0.1, for tests and the demo.")
    ap.add_argument("--port", type=int, default=1886, help="port for its own broker (0 picks a free one)")
    ap.add_argument("--broker-port", type=int, default=None, help="use a broker already on 127.0.0.1 at this port")
    ap.add_argument("--group", default="FREEBOARD")
    ap.add_argument("--node", default="EDGE_1")
    ap.add_argument("--period", type=float, default=1.0, help="seconds between data reports")
    a = ap.parse_args(argv)
    fake = start(a.broker_port, a.group, a.node, period_s=a.period, broker_listen_port=a.port)
    print(f"Sparkplug B fake edge node {a.group}/{a.node} (device PLC_1) on broker mqtt://{HOST}:{fake.port}. "
          f"TANK_3/Level rises slowly. Ctrl+C stops it.", flush=True)
    try:
        while True:
            time.sleep(1.0)
            level = fake.get("PLC_1", "TANK_3/Level")
            if isinstance(level, float) and level < 99.5:
                fake.set("PLC_1", "TANK_3/Level", round(level + 0.05, 3))
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()


if __name__ == "__main__":
    main()
