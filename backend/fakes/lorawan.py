"""A fake LoRaWAN network server's MQTT integration, for the tests and the demo.

It connects to an MQTT broker on 127.0.0.1 (the fake broker in mqtt_broker.py, or one
it starts itself) and publishes uplinks the way the real integrations do:

- ChirpStack v4 (network "chirpstack"): application/<app>/device/<dev_eui>/event/up with
  deduplicationId, time, deviceInfo, devAddr, fCnt, fPort, base64 data, the decoded
  `object`, rxInfo and txInfo. A codec error leaves out `object` and publishes a
  .../event/log message with code UPLINK_CODEC, as ChirpStack does.
- The Things Stack v3 (network "ttn"): v3/<app>@<tenant>/devices/<device_id>/up with
  end_device_ids, received_at, and uplink_message (f_port, f_cnt, frm_payload,
  decoded_payload, rx_metadata, settings). A codec error leaves out decoded_payload.

Each device uplinks every period_s, and at once after set(). The fake also subscribes to
the downlink topics (ChirpStack .../command/down, TTN .../down/push and .../down/replace)
and records anything that arrives in .writes: the Freeboard connector is read-only, so
that list must stay empty.

Test hooks: set() changes a decoded field, codec_error() makes uplinks fail to decode,
pause() and resume() stop and restart uplinks, uplink() sends one now with an optional
time shift, and publish_retained() leaves a retained uplink on the broker.

    python -m backend.fakes.lorawan                         # its own broker on 127.0.0.1:1887
    python -m backend.fakes.lorawan --network ttn --broker-port 1885

In-process, start(...) returns a FakeNetworkServer; .port is the broker port and stop()
stops it (and the broker, when it started one).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import struct
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from backend.fakes import mqtt_broker
from backend.fakes.mqtt_broker import HOST, LoopThread, MqttClient

# The demo sensors: what the demo blueprint maps (see demo_spec in the docs).
DEMO_DEVICES = {
    "tank-3-level": {"dev_eui": "a84041000181c2ee", "profile": "Dragino LDDS75",
                     "decoded": {"level_pct": 62.0, "battery_v": 3.61, "distance_mm": 1140}},
    "pump-3-flow": {"dev_eui": "a84041000181c2ef", "profile": "Milesight EM300-DI",
                    "decoded": {"flow_bbl_hr": 118.0, "running": True, "battery_pct": 87}},
}
CHIRPSTACK_APP = "4f1d3c0e-5b7a-4c4e-9d0a-2b6f8e1c7a90"
TTN_APP = "freeboard-swd"
TTN_TENANT = "ttn"


def iso(epoch: float, digits: int = 6) -> str:
    """RFC 3339 in UTC with `digits` fraction digits (TTN uses 9, ChirpStack 6)."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    frac = f"{epoch % 1:.9f}"[2:2 + digits]
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + (f".{frac}" if digits else "") + "Z"


def _frm(decoded: dict) -> str:
    """A plausible raw payload: the first two numbers as big-endian 16-bit tenths."""
    nums = [v for v in decoded.values() if isinstance(v, (int, float)) and not isinstance(v, bool)][:2]
    raw = b"".join(struct.pack(">H", int(abs(n) * 10) & 0xFFFF) for n in nums if n == n and abs(n) < 1e6)
    return base64.b64encode(raw or b"\x00").decode("ascii")


class FakeNetworkServer:
    """One application's devices on a broker. Every public method is safe from any thread."""

    def __init__(self, broker_port: int, network: str = "chirpstack", application_id: Optional[str] = None,
                 tenant: str = TTN_TENANT, devices: Optional[dict] = None, period_s: float = 0.5,
                 host: str = HOST) -> None:
        if host not in mqtt_broker.LOOPBACK:
            raise ValueError("the fake network server only talks to a broker on 127.0.0.1")
        if network not in ("chirpstack", "ttn"):
            raise ValueError("network must be chirpstack or ttn")
        self.host, self.port = host, broker_port
        self.network = network
        self.application_id = application_id or (CHIRPSTACK_APP if network == "chirpstack" else TTN_APP)
        self.tenant = tenant
        self.period_s = period_s
        self.devices = copy.deepcopy(DEMO_DEVICES if devices is None else devices)
        self.fcnt = {d: 0 for d in self.devices}
        self.errors: dict[str, str] = {}          # device_id -> codec error text
        self.paused = False
        self.stopped = False
        self.writes: list[dict] = []              # downlinks received: must stay empty
        self.sent: list[dict] = []                # {"device", "topic", "retain", "time"}
        self.broker = None                        # set by start() when it started one
        self.lt = LoopThread(f"fake-lorawan-{network}")
        self.client: Optional[MqttClient] = None
        try:
            self.lt.run(self._connect())
        except Exception:
            self.lt.stop()
            raise
        self._ticker = asyncio.run_coroutine_threadsafe(self._tick_loop(), self.lt.loop)

    # ---------- topics and payloads ----------
    @property
    def endpoint(self) -> str:
        return f"mqtt://{self.host}:{self.port}"

    def topic(self, device_id: str, event: str = "up") -> str:
        if self.network == "chirpstack":
            return f"application/{self.application_id}/device/{self.devices[device_id]['dev_eui']}/event/{event}"
        return f"v3/{self.application_id}@{self.tenant}/devices/{device_id}/{event}"

    def _down_filters(self) -> list:
        if self.network == "chirpstack":
            return [(f"application/{self.application_id}/device/+/command/down", 0)]
        base = f"v3/{self.application_id}@{self.tenant}/devices/+"
        return [(f"{base}/down/push", 0), (f"{base}/down/replace", 0)]

    def payload(self, device_id: str, when: float) -> dict:
        dev = self.devices[device_id]
        self.fcnt[device_id] += 1
        fcnt = self.fcnt[device_id]
        decoded = copy.deepcopy(dev["decoded"])
        err = self.errors.get(device_id)
        eui = dev["dev_eui"]
        if self.network == "chirpstack":
            doc = {
                "deduplicationId": str(uuid.uuid4()),
                "time": iso(when, 6),
                "deviceInfo": {"tenantId": "52f14cd4-c6f1-4fbd-8f87-4025e1d49242", "tenantName": "Freeboard",
                               "applicationId": self.application_id, "applicationName": "saltwater-yard",
                               "deviceProfileId": "0a7e7d3c-1f0e-4b2a-9a51-6c3c4b8f0d11",
                               "deviceProfileName": dev.get("profile", "sensor"), "deviceName": device_id,
                               "devEui": eui, "deviceClassEnabled": "CLASS_A", "tags": {}},
                "devAddr": "01fc5e3a", "adr": True, "dr": 3, "fCnt": fcnt, "fPort": 2, "confirmed": False,
                "data": _frm(decoded),
                "rxInfo": [{"gatewayId": "0016c001ff1e3a7b", "uplinkId": 40213 + fcnt, "nsTime": iso(when, 6),
                            "rssi": -97, "snr": 7.5, "channel": 2, "rfChain": 1,
                            "location": {"latitude": 31.9973, "longitude": -102.0779}, "context": "EFwMtA==",
                            "metadata": {"region_config_id": "us915_1", "region_common_name": "US915"},
                            "crcStatus": "CRC_OK"}],
                "txInfo": {"frequency": 904700000,
                           "modulation": {"lora": {"bandwidth": 125000, "spreadingFactor": 9, "codeRate": "CR_4_5"}}},
            }
            if err is None:
                doc["object"] = decoded
            return doc
        up = {
            "session_key_id": "AYv8XqX3k1n7mQ==", "f_port": 2, "f_cnt": fcnt, "frm_payload": _frm(decoded),
            "rx_metadata": [{"gateway_ids": {"gateway_id": "freeboard-gw-1", "eui": "0016C001FF1E3A7B"},
                             "time": iso(when, 9), "timestamp": 2831405644, "rssi": -97, "channel_rssi": -97,
                             "snr": 7.5, "uplink_token": "ChwKGgoOZnJlZWJvYXJkLWd3LTE=",
                             "received_at": iso(when, 9)}],
            "settings": {"data_rate": {"lora": {"bandwidth": 125000, "spreading_factor": 9, "coding_rate": "4/5"}},
                         "frequency": "904700000", "timestamp": 2831405644},
            "received_at": iso(when, 9), "consumed_airtime": "0.205824s",
            "network_ids": {"net_id": "000013", "tenant_id": self.tenant, "cluster_id": "nam1",
                            "cluster_address": "nam1.cloud.thethings.network"},
        }
        if err is None:
            up["decoded_payload"] = decoded
        else:
            up["decoded_payload_warnings"] = []
        return {
            "end_device_ids": {"device_id": device_id,
                               "application_ids": {"application_id": self.application_id},
                               "dev_eui": eui.upper(), "join_eui": "70B3D57ED0000000", "dev_addr": "260C5E3A"},
            "correlation_ids": [f"as:up:{uuid.uuid4().hex.upper()}"],
            "received_at": iso(when, 9),
            "uplink_message": up,
        }

    # ---------- session (runs on the fake's loop) ----------
    async def _connect(self) -> None:
        self.client = MqttClient(self.host, self.port, f"fake-lorawan-{self.network}-{uuid.uuid4().hex[:8]}",
                                 keepalive=5, on_message=self._on_message)
        await self.client.connect()
        await self.client.subscribe(self._down_filters())

    def _on_message(self, topic: str, payload: bytes, retain: bool) -> None:
        self.writes.append({"topic": topic, "payload": payload, "retain": retain})

    def _publish(self, device_id: str, shift_s: float = 0.0, retain: bool = False) -> bool:
        if self.client is None or not self.client.connected:
            return False
        when = time.time() + shift_s
        doc = self.payload(device_id, when)
        topic = self.topic(device_id)
        ok = self.client.publish(topic, json.dumps(doc).encode("utf-8"), retain=retain)
        if ok:
            self.sent.append({"device": device_id, "topic": topic, "retain": retain, "time": when})
            err = self.errors.get(device_id)
            if err is not None and self.network == "chirpstack":
                log = {"time": iso(when), "deviceInfo": doc["deviceInfo"], "level": "ERROR",
                       "code": "UPLINK_CODEC", "description": err, "context": {"deduplication_id":
                                                                              doc["deduplicationId"]}}
                self.client.publish(self.topic(device_id, "log"), json.dumps(log).encode("utf-8"))
        return ok

    async def _tick_loop(self) -> None:
        while True:
            if self.client is None or not self.client.connected:
                try:
                    await self._connect()
                except (OSError, ConnectionError, asyncio.TimeoutError):
                    await asyncio.sleep(0.3)
                    continue
            if not self.paused:
                for dev in list(self.devices):
                    self._publish(dev)
            await asyncio.sleep(self.period_s)

    # ---------- test hooks ----------
    def _do(self, fn, *args) -> Any:
        return self.lt.call(fn, *args)

    def set(self, device_id: str, field: str, value: Any, send: bool = True) -> None:
        """Change a decoded field (None removes it) and, by default, uplink at once."""
        def do():
            decoded = self.devices[device_id]["decoded"]
            if value is None:
                decoded.pop(field, None)
            else:
                decoded[field] = value
            if send and not self.paused:
                self._publish(device_id)
        self._do(do)

    def codec_error(self, device_id: str, text: Optional[str] = "Invalid payload length") -> None:
        """From now on the device's uplinks fail to decode (None: they decode again). Uplinks at once."""
        def do():
            if text is None:
                self.errors.pop(device_id, None)
            else:
                self.errors[device_id] = text
            if not self.paused:
                self._publish(device_id)
        self._do(do)

    def uplink(self, device_id: str, shift_s: float = 0.0) -> bool:
        """Publish one uplink now, its time moved by shift_s (negative: an old uplink)."""
        return self._do(self._publish, device_id, shift_s)

    def publish_retained(self, device_id: str, shift_s: float = 0.0) -> bool:
        """Leave a retained uplink on the broker, which a new subscriber gets with the retain flag."""
        return self._do(self._publish, device_id, shift_s, True)

    def pause(self) -> None:
        """Stop uplinks, as a sensor with a flat battery would."""
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def stop(self) -> None:
        """Disconnect and stop (and stop the broker if start() made one)."""
        if self.stopped:
            return
        self.stopped = True

        async def shut():
            self._ticker.cancel()
            if self.client is not None:
                await self.client.disconnect()
        try:
            self.lt.run(shut(), timeout=5)
        except Exception:  # noqa: BLE001 - stopping must not raise
            pass
        self.lt.stop()
        if self.broker is not None:
            self.broker.stop()


def start(broker_port: Optional[int] = None, network: str = "chirpstack", application_id: Optional[str] = None,
          tenant: str = TTN_TENANT, devices: Optional[dict] = None, period_s: float = 0.5,
          broker_listen_port: int = 0) -> FakeNetworkServer:
    """Start a fake network server. With broker_port None it starts its own broker on
    127.0.0.1 (broker_listen_port, 0 picks a free one) and stop() stops that too.
    devices is {device_id: {"dev_eui", "decoded": {...}}}; DEMO_DEVICES by default."""
    broker = None
    if broker_port is None:
        broker = mqtt_broker.start(port=broker_listen_port)
        broker_port = broker.port
    try:
        fake = FakeNetworkServer(broker_port, network, application_id, tenant, devices, period_s)
    except Exception:
        if broker is not None:
            broker.stop()
        raise
    fake.broker = broker
    return fake


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description="A fake LoRaWAN network server MQTT integration on 127.0.0.1.")
    ap.add_argument("--network", choices=("chirpstack", "ttn"), default="chirpstack")
    ap.add_argument("--port", type=int, default=1887, help="port for its own broker (0 picks a free one)")
    ap.add_argument("--broker-port", type=int, default=None, help="use a broker already on 127.0.0.1 at this port")
    ap.add_argument("--period", type=float, default=5.0, help="seconds between uplinks")
    a = ap.parse_args(argv)
    fake = start(a.broker_port, a.network, period_s=a.period, broker_listen_port=a.port)
    print(f"LoRaWAN fake {a.network} integration, application {fake.application_id}, on broker {fake.endpoint}. "
          f"Devices: {', '.join(f'{d} ({v['dev_eui']})' for d, v in fake.devices.items())}. "
          f"tank-3-level rises slowly. Ctrl+C stops it.", flush=True)
    try:
        while True:
            time.sleep(1.0)
            level = fake.devices["tank-3-level"]["decoded"].get("level_pct")
            if isinstance(level, float) and level < 99.5:
                fake.set("tank-3-level", "level_pct", round(level + 0.05, 3), send=False)
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()


if __name__ == "__main__":
    main()
