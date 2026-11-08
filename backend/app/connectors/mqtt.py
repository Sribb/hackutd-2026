"""MQTT connector (paho-mqtt, MQTT 3.1.1).

Readings: subscribes to `topic_prefix + address` for every signal in the blueprint
(address defaults to `<ASSET_ID>/<signal>`). A payload is a plain value (`72.4`,
`1`, `true`, `ON`, `OPEN`) or JSON (`72.4`, `{"value": 72.4}`). Only declared
signals are read, so a broker can't override an asset this connector doesn't own.
An address is one exact topic: `+` and `#` (MQTT wildcards) are rejected when the
config loads, since one wildcard would let any publisher feed a tank's level.

A retained message (the broker's last stored value, delivered on subscribe) is not
fresh: it can be hours old. It is counted but not read, so the asset stays UNKNOWN
until the device publishes a live value.

Commands: a confirmed command is published at QoS 1 to `topic_prefix + address`
(default `<ASSET_ID>/set`) with the value as plain text: `OPEN`/`CLOSED`, `ON`/`OFF`,
or the choke fraction (`0.5`). `ok` means the broker acknowledged it; whether the
device moved shows up in its readings (VALVE_MISMATCH catches one that didn't). If
the PUBACK doesn't come within timeout_s the result is `uncertain` (UNCERTAIN, never
FAILED): the command may have reached the device. It is also taken out of paho's
queue, because paho re-sends unacknowledged QoS 1 messages after a reconnect, and a
confirmed command must go out at most once.

Config: host (required), port (1883), topic_prefix (""), client_id, username,
password_env (the name of an environment variable holding the password; it must
start with FREEBOARD_CONN_), tls (false), keepalive_s (30), timeout_s (5).

Connecting happens on paho's own thread and retries forever, so an unreachable
broker never blocks the console: the connector reports `unavailable` with the
reason, returns no readings, refuses commands, and its assets go stale (UNKNOWN).
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Callable

from ..fieldmap import FieldMap
from .base import UNCERTAIN_DETAIL, Connector, Reading, coerce, import_optional

WILDCARDS = ("+", "#")
WAIT_FOR_PUBACK = 2  # paho.mqtt.client.mqtt_ms_wait_for_puback


def check_topic(topic: str, what: str) -> str:
    """An exact topic, or ValueError. Wildcards would read (or write) more than one point."""
    if any(w in topic for w in WILDCARDS):
        raise ValueError(f"{what} topic {topic!r} has an MQTT wildcard (+ or #); "
                         f"each signal and command needs one exact topic")
    if not topic:
        raise ValueError(f"{what} topic is empty")
    return topic


def parse_payload(payload: bytes):
    """A message payload as a Python value: JSON when it parses, else the stripped text."""
    text = payload.decode("utf-8", "replace").strip()
    try:
        v = json.loads(text)
    except ValueError:
        return text
    if isinstance(v, dict):
        return v.get("value")
    return v


def command_payload(command: dict) -> str:
    v = command.get("value")
    if isinstance(v, float):
        return f"{v:g}"
    return "" if v is None else str(v)


class MqttConnector(Connector):
    kind = "mqtt"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float]) -> None:
        super().__init__(spec, fmap, now)
        cfg = self.config
        self.host = str(cfg.get("host") or "")
        self.port = int(cfg.get("port") or 1883)
        self.prefix = str(cfg.get("topic_prefix") or "")
        self.timeout = float(cfg.get("timeout_s", 5))
        self.topics = {check_topic(self.prefix + (addr or f"{aid}/{sig}"), f"{aid}.{sig}"): (aid, sig)
                       for (aid, sig), addr in self.signals.items()}
        self.cmd_topics = {key: check_topic(self.prefix + (addr or f"{key[0]}/set"), f"{key[0]} {key[1]}")
                           for key, addr in self.commands.items()}
        self._lock = threading.Lock()
        self._latest: dict[tuple, object] = {}       # (asset, signal) -> value not yet read
        self.connected = False
        self.last_error = ""
        self.bad_payloads = 0
        self.received = 0
        self.retained = 0                             # retained (old) messages not counted as readings
        self.client = None
        self.why = self._start()

    # ---- connection (runs on paho's thread) ----
    def _start(self) -> str:
        mqtt, err = import_optional("paho.mqtt.client", "paho-mqtt")
        if mqtt is None:
            return err or "paho-mqtt is not installed"
        if not self.host:
            return "not configured: config needs host"
        var = self.missing_env()
        if var:
            return f"environment variable {var} is not set"
        try:
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=str(self.config.get("client_id") or f"freeboard-{uuid.uuid4().hex[:8]}"),
                                 protocol=mqtt.MQTTv311)
            if self.config.get("username"):
                client.username_pw_set(str(self.config["username"]), self.secret("password"))
            if self.config.get("tls"):
                client.tls_set()
            client.connect_timeout = self.timeout
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_connect_fail = self._on_connect_fail
            client.on_message = self._on_message
            client.connect_async(self.host, self.port, keepalive=int(self.config.get("keepalive_s", 30)))
            client.loop_start()
        except Exception as e:  # noqa: BLE001 - a bad config must not stop the backend
            return f"can't start the MQTT client: {e}"
        self.client = client
        return ""

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        if getattr(reason_code, "is_failure", False):
            self.connected = False
            self.last_error = f"broker refused the connection: {reason_code}"
            return
        self.connected = True
        self.last_error = ""
        if self.topics:
            client.subscribe([(t, 1) for t in self.topics])

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props=None) -> None:
        self.connected = False
        if not self.last_error:
            self.last_error = f"disconnected: {reason_code}"

    def _on_connect_fail(self, _client, _userdata) -> None:
        self.connected = False
        self.last_error = f"can't reach {self.host}:{self.port}"

    def _on_message(self, _client, _userdata, msg) -> None:
        key = self.topics.get(msg.topic)
        if key is None:
            return
        if getattr(msg, "retain", False):
            # The broker's stored value from whenever it was last published: not fresh.
            with self._lock:
                self.retained += 1
            return
        value = coerce(key[1], parse_payload(msg.payload))
        with self._lock:
            if value is None:
                self.bad_payloads += 1
                return
            self.received += 1
            self._latest[key] = value

    # ---- the interface ----
    def read(self) -> list[Reading]:
        with self._lock:
            latest, self._latest = self._latest, {}
        return [self.reading(aid, sig, v) for (aid, sig), v in latest.items()]

    def send(self, command: dict) -> dict:
        client = self.client
        if client is None or not self.connected:
            return {"ok": False, "detail": f"{self.id} (mqtt) is not available: {self._problem()}."}
        topic = self.cmd_topics.get((command.get("asset_id"), command.get("type")))
        if topic is None:
            return {"ok": False, "detail": f"{self.id} has no topic for {command.get('type')} on {command.get('asset_id')}."}
        payload = command_payload(command)
        done = {"ok": True, "detail": f"Published {payload} to {topic} on {self.host}:{self.port}."}
        try:
            info = client.publish(topic, payload, qos=1)
        except Exception as e:  # noqa: BLE001 - paho refused it before queueing: nothing went out
            self.last_error = str(e)
            return {"ok": False, "detail": f"Couldn't publish to {self.id}: {e}. Nothing was sent."}
        if int(getattr(info, "rc", 0) or 0) > 0:
            # Never written to the socket (no connection, queue full). paho would send it on
            # the next reconnect, so take it back: nothing reached the broker.
            self._forget(client, info)
            return {"ok": False, "detail": f"Couldn't publish to {self.id}: {info.rc}. Nothing was sent."}
        try:
            info.wait_for_publish(timeout=self.timeout)
            why = f"no PUBACK from the broker within {self.timeout:g} s"
        except Exception as e:  # noqa: BLE001 - the link died after the message went out
            self.last_error = str(e)
            why = f"the link failed while waiting for the PUBACK: {e}"
        if self._forget(client, info):
            return {"ok": False, "uncertain": True,
                    "detail": f"{self.id}: {payload} to {topic} {UNCERTAIN_DETAIL} ({why})."}
        return done  # paho only drops the message from its queue when the PUBACK arrives

    @staticmethod
    def _forget(client, info) -> bool:
        """Take a QoS 1 message out of paho's outgoing queue, so a reconnect never re-sends
        it (a command goes out at most once). True if it was still waiting, False if the
        PUBACK already came. A PUBACK that arrives later is ignored: paho no longer knows the mid.

        Uses paho's queue directly: paho 2.x has no public way to cancel a publish."""
        mutex = getattr(client, "_out_message_mutex", None)
        out = getattr(client, "_out_messages", None)
        mid = getattr(info, "mid", None)
        if mutex is None or out is None or mid is None:
            return not info.is_published()
        with mutex:
            msg = out.pop(mid, None)
            if msg is None:
                return False
            if getattr(msg, "state", None) == WAIT_FOR_PUBACK:
                client._inflight_messages = max(0, client._inflight_messages - 1)
            return True

    def _problem(self) -> str:
        if self.why:
            return self.why
        return self.last_error or f"connecting to {self.host}:{self.port}"

    def status(self) -> dict:
        if self.client is not None and self.connected:
            detail = f"connected to {self.host}:{self.port}, {len(self.topics)} topics, {self.received} messages"
            if self.bad_payloads:
                detail += f", {self.bad_payloads} unreadable"
            if self.retained:
                detail += f", {self.retained} retained (old) values ignored until a live update"
            return {"id": self.id, "kind": self.kind, "available": True, "detail": detail}
        return {"id": self.id, "kind": self.kind, "available": False, "detail": self._problem()}

    def close(self):
        """Returns at once: paho's disconnect and thread join run on a daemon thread."""
        client, self.client = self.client, None
        self.connected = False
        if client is None:
            return None

        def stop() -> None:
            try:
                client.disconnect()
            finally:
                client.loop_stop()
        return self.close_later(stop, f"mqtt-close-{self.id}")
