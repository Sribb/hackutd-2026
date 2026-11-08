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
or the choke fraction (`0.5`). `ok` means the broker acknowledged it (PUBACK); whether
the device moved shows up in its readings (VALVE_MISMATCH catches one that didn't).
Anything short of a PUBACK on the connection it went out on is `uncertain`
(UNCERTAIN, never FAILED): no PUBACK within timeout_s, or the link dropped while
waiting. The command may have reached the device.

At most once, in every ordering: paho re-sends an unacknowledged QoS 1 message after
a reconnect, so the connector empties paho's outgoing queue in `on_pre_connect`,
which paho calls inside reconnect() before it opens the new socket (and again in
on_connect, which paho calls before its re-send loop). A message can therefore go
out on one connection only. A message taken back that way is marked lost, so the
waiting send() reports UNCERTAIN at once instead of waiting out the timeout. send()
holds paho's queue lock while it publishes, so a reconnect can't slip in between a
publish that found no connection and taking that message back (that one is FAILED:
paho never wrote it). If this paho version lacks those internals, commands are
refused rather than sent without the guarantee.

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
MQTT_ERR_CONN_LOST = 7  # paho.mqtt.client.MQTT_ERR_CONN_LOST, what paho sets on lost QoS 0 packets


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
        self.unsafe = ""
        self.lost = 0                                 # commands taken back from paho on a reconnect
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
            client.on_pre_connect = self._on_pre_connect
            client.on_message = self._on_message
            client.connect_async(self.host, self.port, keepalive=int(self.config.get("keepalive_s", 30)))
            client.loop_start()
        except Exception as e:  # noqa: BLE001 - a bad config must not stop the backend
            return f"can't start the MQTT client: {e}"
        self.client = client
        if self._queue(client) is None:
            self.unsafe = ("this paho-mqtt version doesn't expose its outgoing queue, so a command "
                           "could be re-sent after a reconnect; commands are refused")
        return ""

    def _on_pre_connect(self, client, _userdata) -> None:
        """paho is about to open a new socket: nothing from the old one may go out again."""
        self._drop_outgoing(client)

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        # paho re-sends its queue right after this callback returns: empty it first (a
        # second guard; on_pre_connect already did).
        self._drop_outgoing(client)
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
        queue = self._queue(client)
        if queue is None:
            return {"ok": False, "detail": f"{self.id} (mqtt) can't send commands: {self.unsafe}. Nothing was sent."}
        mutex, out = queue
        payload = command_payload(command)
        with mutex:  # no reconnect (so no re-send and no take-back) while we publish
            try:
                info = client.publish(topic, payload, qos=1)
            except Exception as e:  # noqa: BLE001 - paho refused it before queueing: nothing went out
                self.last_error = str(e)
                return {"ok": False, "detail": f"Couldn't publish to {self.id}: {e}. Nothing was sent."}
            rc = int(getattr(info, "rc", 0) or 0)
            if rc > 0:
                # Never written to a socket (no connection, or the queue was full). paho would
                # send it after the next reconnect, so take it back while we still hold the lock.
                self._take_back(out, info)
                return {"ok": False, "detail": f"Couldn't publish to {self.id}: {rc}. Nothing was sent."}
        try:
            info.wait_for_publish(timeout=self.timeout)
            why = f"no PUBACK from the broker within {self.timeout:g} s"
        except Exception as e:  # noqa: BLE001 - the link dropped after the message went out
            why = f"the link dropped while waiting for the PUBACK: {e}"
        with mutex:
            if self._acked(info):
                return {"ok": True, "detail": f"Published {payload} to {topic} on {self.host}:{self.port}."}
            # Not acknowledged: take it back so no reconnect ever sends it, and say UNCERTAIN.
            self._take_back(out, info)
        return {"ok": False, "uncertain": True,
                "detail": f"{self.id}: {payload} to {topic} {UNCERTAIN_DETAIL} ({why})."}

    # ---- paho's outgoing queue (paho 2.x has no public way to cancel a publish) ----
    @staticmethod
    def _queue(client):
        """(lock, {mid: message}) of paho's outgoing QoS>0 messages, or None if this paho
        version doesn't have them."""
        mutex = getattr(client, "_out_message_mutex", None)
        out = getattr(client, "_out_messages", None)
        if mutex is None or not hasattr(mutex, "acquire") or not isinstance(out, dict):
            return None
        return mutex, out

    @staticmethod
    def _acked(info) -> bool:
        """True only when the broker's PUBACK came (paho marks it published, rc 0)."""
        try:
            return int(getattr(info, "rc", 0) or 0) == 0 and bool(info.is_published())
        except Exception:  # noqa: BLE001 - is_published raises when rc > 0
            return False

    @staticmethod
    def _take_back(out: dict, info) -> bool:
        """Remove this publish from paho's queue (the caller holds its lock). True if it was there."""
        mid = getattr(info, "mid", None)
        msg = out.get(mid)
        if msg is None or getattr(msg, "info", None) is not info:
            return False  # already acknowledged, already dropped, or not ours
        del out[mid]
        return True

    def _drop_outgoing(self, client) -> None:
        """Empty paho's outgoing queue before a (re)connect, so nothing is re-sent. Each
        waiting send() is woken as lost: it reports UNCERTAIN (the message may have gone
        out on the old connection), and no later PUBACK can make it look acknowledged."""
        queue = self._queue(client)
        if queue is None:
            return
        mutex, out = queue
        with mutex:
            dropped = list(out.values())
            out.clear()
            client._inflight_messages = 0
        for msg in dropped:
            info = getattr(msg, "info", None)
            if info is None:
                continue
            info.rc = MQTT_ERR_CONN_LOST
            try:
                info._set_as_published()  # wakes wait_for_publish, which then raises
            except Exception:  # noqa: BLE001
                pass
        if dropped:
            with self._lock:
                self.lost += len(dropped)

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
            if self.lost:
                detail += f", {self.lost} unacknowledged commands dropped on reconnect (never re-sent)"
            if self.unsafe:
                detail += f"; {self.unsafe}"
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
