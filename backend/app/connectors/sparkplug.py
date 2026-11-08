"""Sparkplug B connector (kind "sparkplug"): Freeboard as a Sparkplug 3.0 host application.

Sparkplug B is MQTT with rules on top. Edge nodes publish a birth certificate (NBIRTH,
then a DBIRTH per device) that names every metric, gives its datatype, and usually an
alias; after that they send only changes (NDATA, DDATA), often by alias alone. Every
message from an edge node after NBIRTH carries a sequence number (0 to 255, then 0
again), so a host can tell when it missed one. When an edge node's MQTT session ends,
the broker publishes its will, an NDEATH carrying the same bdSeq as its NBIRTH.

This connector is the host:

- On every connect it subscribes to spBv1.0/STATE/<host_id> and spBv1.0/<group_id>/#,
  then publishes spBv1.0/STATE/<host_id> {"online": true, "timestamp": ms}, retained at
  QoS 1. Its will is {"online": false} with the same timestamp, retained. If it sees
  its own STATE say offline (an old will), it publishes online again.
- It tracks each edge node it maps: bdSeq from NBIRTH, alias to metric name maps and
  datatypes from NBIRTH and DBIRTH, and the last seq.
- Data from an edge node or device with no birth since this connection is ignored, and
  the connector asks for a rebirth: an NCMD with "Node Control/Rebirth" = true. That is
  a protocol control message and moves no equipment. Rebirth requests are rate-limited
  to one per edge node per rebirth_min_interval_s (default 10 s).
- A seq gap (mod 256) means a message was lost. That message is ignored, the node's
  signals turn uncertain, everything from the node is ignored until its next NBIRTH,
  and a rebirth is requested. A payload that doesn't decode is treated the same way.
- NDEATH with the node's bdSeq (or any NDEATH when the birth had none), or DDEATH,
  turns every mapped signal of that node or device to comm_lost at once. Nothing from
  it counts again until the next birth. An NDEATH with an older bdSeq is a late will
  from a session that already ended, and is ignored.
- A retained message delivered on subscribe is not fresh: it is ignored and a rebirth
  is requested. A metric marked is_historical is never read as live. An is_null metric
  has no value. A metric timestamp older than max_age_s (default 60 s) is stale.
- A metric's "Quality" property (Sparkplug 3.0: 0 bad, 192 good, 500 stale) is
  honored: anything but 192 gives no value. A metric that wasn't in the birth
  certificate gives no value either.
- A new NBIRTH makes every device signal of that node uncertain until the device's
  DBIRTH comes.

A value that isn't good becomes no reading (see signalmap.py), so the asset goes stale
and shows UNKNOWN; `quality` says why at once, for every declared signal:
{(asset_id, signal): {"quality", "detail", "t"}}. Losing the broker turns every
declared signal comm_lost.

Addresses are exact Sparkplug metric paths, never MQTT filters (+ and # are refused):

    group/edge_node/device/metric     a device metric, e.g. FREEBOARD/EDGE_1/PLC_1/TANK_3/Level
    group/edge_node//metric           an edge node metric, e.g. FREEBOARD/EDGE_1//Battery

The group must be config group_id. The metric name is everything after the third slash,
so it may contain slashes itself. Each declared signal's `scale` and its rule in
config.signal_map are applied (signalmap.SignalMap).

Writes happen only in send(), which the live layer calls only after a person confirms.
A command becomes a DCMD (an NCMD for a node metric) with the metric by name and the
datatype from its birth certificate, at QoS 0 as Sparkplug says, never retained.
SET_VALVE OPEN/CLOSED and SET_PUMP ON/OFF write true/false to a Boolean metric, 1/0 to an
integer or float metric, or the word itself to a String metric; CHOKE_WELL writes the
fraction (0 to 1) to a Float or Double metric. The device must be online (born, not
dead) with that metric in its birth, or nothing is sent. ok means the command was
published; the change itself shows in the next DDATA. A QoS 0 publish is never queued
across a reconnect (paho drops it), so a command goes out at most once. If it isn't
written within timeout_s, send() says uncertain and nobody retries it blindly.

A good value's quality turns good when read() hands the value on, not when it arrives,
so an asset never shows the value from before a bad period as fresh while the new one
waits in the inbox. status() is available only while at least one edge node this
connector maps is born; with every one of them dead or not yet born it says unavailable
and why, since none of its signals can be read.

The broker host must be one an operator allows (FREEBOARD_CONN_ALLOWED_HOSTS, see
hosts.py; only this machine when it is not set). Any other host is a ValueError when
the connector is built, so nothing connects to it and the registry shows why.

Config: host, port (1883), group_id, host_id, client_id (freeboard-<host_id>-<id>),
username, password_env (a FREEBOARD_CONN_ variable), tls (false), keepalive_s (30),
timeout_s (5), rebirth_min_interval_s (10), max_age_s (60), signal_map.

paho-mqtt runs on its own network thread. It reconnects forever, each wait from
signalmap.Backoff (1 s doubling to 30 s, with jitter). read(), status() and close()
never wait on the network.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Optional

from ..fieldmap import FieldMap
from .base import Connector, Reading
from . import sparkplug_codec as codec
from .hosts import check_host
from .signalmap import (BAD, COMM_LOST, GOOD, NULL, STALE, UNCERTAIN, Backoff, Inbox, Mapped, SignalMap,
                        check_env_names, close_later, env_secret, import_optional, normalize,
                        uncertain_result)

NAMESPACE = "spBv1.0"
REBIRTH = "Node Control/Rebirth"
BDSEQ = "bdSeq"
NODE_TYPES = ("NBIRTH", "NDEATH", "NDATA")
DEVICE_TYPES = ("DBIRTH", "DDEATH", "DDATA")
TOPIC_CHARS = "+#/\x00"
INTEGERS = frozenset(codec.SIGNED_BITS) | (frozenset(codec.UNSIGNED_BITS) - {codec.DATETIME})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _name(config: dict, key: str, what: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"config {key} is required ({what})")
    if any(c in value for c in TOPIC_CHARS):
        raise ValueError(f"config {key} {value!r} can't contain /, + or #")
    return value


def _positive(config: dict, key: str, default: float, allow_zero: bool = False) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"config {key} must be a number, got {value!r}")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"config {key} must be {'at least 0' if allow_zero else 'above 0'}, got {value!r}")
    return float(value)


def parse_address(address: Any, group_id: str, what: str) -> tuple[str, str, str]:
    """(edge_node, device, metric) for an address; device is "" for a node metric.
    Raises ValueError saying what is wrong."""
    if not isinstance(address, str) or not address:
        raise ValueError(f"{what} needs an address like {group_id}/EDGE_NODE/DEVICE/Metric")
    if "+" in address or "#" in address or "\x00" in address:
        raise ValueError(f"{what}: address {address!r} has an MQTT wildcard; Sparkplug addresses are exact")
    parts = address.split("/", 3)
    if len(parts) != 4 or not parts[1] or not parts[3]:
        raise ValueError(f"{what}: address {address!r} must look like group/edge_node/device/metric "
                         f"(group/edge_node//metric for a node metric)")
    if parts[0] != group_id:
        raise ValueError(f"{what}: address {address!r} is in group {parts[0]!r}, not config group_id {group_id!r}")
    return parts[1], parts[2], parts[3]


def property_quality(props: dict) -> tuple[str, str]:
    """(quality, detail) from a metric's "Quality" property (Sparkplug 3.0: 0 bad, 192
    good, 500 stale). No such property is good. Only 192 is good: 64 to 127 (OPC
    uncertain) is uncertain, and any other code, or a value that isn't an integer, is bad."""
    key = next((k for k in props if isinstance(k, str) and k.lower() == "quality"), None)
    if key is None:
        return GOOD, ""
    code = props[key]
    if isinstance(code, bool) or not isinstance(code, int):
        return BAD, "the metric's Quality property is unreadable"
    if code == 192:
        return GOOD, ""
    if code == 500:
        return STALE, "the edge node marks the metric stale (Quality 500)"
    if 64 <= code < 128:
        return UNCERTAIN, f"the edge node marks the metric uncertain (Quality {code})"
    return BAD, f"the edge node marks the metric bad (Quality {code})"


class _Scope:
    """What a birth certificate said about an edge node or one of its devices."""

    def __init__(self) -> None:
        self.born = False
        self.aliases: dict[int, str] = {}
        self.types: dict[str, Optional[int]] = {}

    def reset(self) -> None:
        self.born = False
        self.aliases, self.types = {}, {}


class _Node(_Scope):
    def __init__(self) -> None:
        super().__init__()
        self.bdseq: Optional[int] = None
        self.seq: Optional[int] = None
        self.devices: dict[str, _Scope] = {}

    def die(self) -> None:
        self.reset()
        self.seq = None
        for dev in self.devices.values():
            dev.reset()


class SparkplugConnector(Connector):
    kind = "sparkplug"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float]) -> None:
        super().__init__(spec, fmap, now)
        cfg = self.config
        check_env_names(cfg)
        self.smap = SignalMap(spec)
        self.host = cfg.get("host")
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("config host is required (the MQTT broker's host name)")
        check_host(self.host.strip())
        port = cfg.get("port", 1883)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError(f"config port must be a TCP port number, got {port!r}")
        self.port = port
        self.group_id = _name(cfg, "group_id", "the Sparkplug group to read")
        self.host_id = _name(cfg, "host_id", "this host application's id for its STATE topic")
        self.client_id = cfg.get("client_id") or f"freeboard-{self.host_id}-{self.id}"
        if not isinstance(self.client_id, str):
            raise ValueError("config client_id must be text")
        username = cfg.get("username")
        if username is not None and not isinstance(username, str):
            raise ValueError("config username must be text")
        self.username = username
        tls = cfg.get("tls", False)
        if not isinstance(tls, bool):
            raise ValueError("config tls must be true or false")
        self.tls = tls
        self.keepalive_s = max(1, int(round(_positive(cfg, "keepalive_s", 30))))
        self.timeout_s = _positive(cfg, "timeout_s", 5)
        self.rebirth_min_s = _positive(cfg, "rebirth_min_interval_s", 10, allow_zero=True)

        # address -> mapped signals, and commands -> where they go
        self.targets: dict[tuple, list[tuple]] = {}
        self.where: dict[tuple, tuple] = {}            # (asset_id, signal) -> (edge, device)
        for (aid, sig), addr in self.signals.items():
            edge, device, metric = parse_address(addr, self.group_id, f"signals: {aid}.{sig}")
            self.targets.setdefault((edge, device, metric), []).append((aid, sig))
            self.where[(aid, sig)] = (edge, device)
        self.cmd_addr: dict[tuple, tuple] = {}
        for (aid, typ), addr in self.commands.items():
            if typ not in ("SET_VALVE", "SET_PUMP", "CHOKE_WELL"):
                raise ValueError(f"commands: {aid}.{typ} isn't a command this connector sends "
                                 f"(SET_VALVE, SET_PUMP, CHOKE_WELL)")
            self.cmd_addr[(aid, typ)] = parse_address(addr, self.group_id, f"commands: {aid}.{typ}")
        self.edges = {e for e, _d, _m in self.targets} | {e for e, _d, _m in self.cmd_addr.values()}
        self.state_topic = f"{NAMESPACE}/STATE/{self.host_id}"

        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._inbox = Inbox()
        self._qlock = threading.Lock()      # a value and its quality change together
        self._nodes: dict[str, _Node] = {}
        self._rebirth_at: dict[str, float] = {}
        self._backoff = Backoff(initial_s=1.0, max_s=30.0)
        self._connected = False
        self._closed = False
        self._state_ts = _now_ms()
        self._why = f"connecting to {self.host}:{self.port}"
        self._refused = ""
        self.rebirths_requested = 0
        self.ignored: dict[str, int] = {}               # why -> count, for status and tests
        t = self._t()
        # Every declared signal has an entry from the start, so the dict never changes size.
        self.quality = {key: {"quality": COMM_LOST, "detail": "not connected yet", "t": t}
                        for key in self.signals}
        self._client = None
        mqtt, err = import_optional("paho.mqtt.client", "paho-mqtt")
        if mqtt is None:
            self._why = err or "paho-mqtt is not installed"
            return
        self._mqtt = mqtt
        self._start(mqtt)

    # ---------- connection (callbacks run on paho's thread) ----------
    def _start(self, mqtt) -> None:
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=self.client_id, clean_session=True, protocol=mqtt.MQTTv311)
        if self.username is not None:
            client.username_pw_set(self.username, env_secret(self.config, "password"))
        if self.tls:
            client.tls_set()
        client.connect_timeout = self.timeout_s
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_pre_connect = self._on_pre_connect
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        client.on_message = self._on_message
        self._client = client
        client.connect_async(self.host, self.port, keepalive=self.keepalive_s)
        client.loop_start()

    def _state_payload(self, online: bool) -> bytes:
        return json.dumps({"online": online, "timestamp": self._state_ts}).encode("utf-8")

    def _on_pre_connect(self, client, _userdata) -> None:
        # A new session: a new STATE timestamp, and the will carries the same one.
        self._state_ts = _now_ms()
        client.will_set(self.state_topic, self._state_payload(False), qos=1, retain=True)

    def _next_wait(self, client) -> float:
        delay = self._backoff.next_delay()
        client.reconnect_delay_set(min_delay=delay, max_delay=delay)   # paho waits exactly this long
        return delay

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        if self._closed:
            return
        if getattr(reason_code, "is_failure", False):
            # paho calls on_disconnect next, which schedules the retry and keeps this reason.
            self._refused = f"the broker at {self.host}:{self.port} refused the connection ({reason_code})"
            self._why = self._refused
            return
        with self._lock:
            self._connected = True
            self._nodes = {}                # anything could have happened while we were away
            self._rebirth_at = {}
            self._why = ""
            self._refused = ""
        self._backoff.reset()
        client.subscribe([(self.state_topic, 1), (f"{NAMESPACE}/{self.group_id}/#", 0)])
        client.publish(self.state_topic, self._state_payload(True), qos=1, retain=True)

    def _on_disconnect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        with self._lock:
            self._connected = False
            self._nodes = {}
        if self._closed:
            return
        lost = self._refused or f"lost the connection to the broker at {self.host}:{self.port} ({reason_code})"
        self._why = f"{lost}; retrying in {self._next_wait(client):.1f} s"
        self._mark_all(COMM_LOST, "no connection to the MQTT broker")

    def _on_connect_fail(self, client, _userdata) -> None:
        if self._closed:
            return
        self._why = (f"can't reach the broker at {self.host}:{self.port}; "
                     f"retrying in {self._next_wait(client):.1f} s")
        self._mark_all(COMM_LOST, "no connection to the MQTT broker")

    def _on_message(self, _client, _userdata, msg) -> None:
        if self._closed:
            return
        try:
            self._handle(str(msg.topic), bytes(msg.payload), bool(msg.retain))
        except Exception:  # noqa: BLE001 - one bad message must not stop the network thread
            self._ignore("a message the connector couldn't handle")

    # ---------- Sparkplug ----------
    def _handle(self, topic: str, payload: bytes, retained: bool) -> None:
        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != NAMESPACE:
            return
        if parts[1] == "STATE":
            if topic == self.state_topic:
                self._own_state(payload)
            return
        if parts[1] != self.group_id or len(parts) not in (4, 5):
            return
        mtype, edge = parts[2], parts[3]
        device = parts[4] if len(parts) == 5 else ""
        if edge not in self.edges:
            return                                      # not a node this connector maps
        if not ((mtype in NODE_TYPES and not device) or (mtype in DEVICE_TYPES and device)):
            return                                      # NCMD, DCMD, or a malformed topic
        with self._lock:
            node = self._nodes.setdefault(edge, _Node())
            if retained:
                self._ignore(f"retained {mtype} (not fresh)")
                self._request_rebirth(edge)
                return
            try:
                p = codec.decode_payload(payload)
            except codec.CodecError:
                self._lost_sync(edge, node, f"an unreadable {mtype} payload from edge node {edge}")
                return
            if mtype == "NDEATH":
                self._ndeath(edge, node, p)
            elif mtype == "NBIRTH":
                self._nbirth(edge, node, p)
            elif not node.born:
                self._ignore(f"{mtype} before a birth")
                self._request_rebirth(edge)
            elif p["seq"] is None or node.seq is None or p["seq"] != (node.seq + 1) % 256:
                self._lost_sync(edge, node, f"seq gap from edge node {edge} "
                                            f"(expected {(node.seq or 0) + 1 & 255}, got {p['seq']})")
            else:
                node.seq = p["seq"]
                if mtype == "NDATA":
                    self._apply(edge, "", node, p)
                elif mtype == "DBIRTH":
                    dev = node.devices.setdefault(device, _Scope())
                    dev.reset()
                    dev.born = True
                    self._apply(edge, device, dev, p, birth=True)
                else:
                    dev = node.devices.get(device)
                    if dev is None or not dev.born:
                        self._ignore(f"{mtype} before a device birth")
                        self._request_rebirth(edge)
                    elif mtype == "DDEATH":
                        dev.reset()
                        self._mark(edge, device, COMM_LOST, f"device {edge}/{device} went offline (DDEATH)")
                    else:
                        self._apply(edge, device, dev, p)

    def _own_state(self, payload: bytes) -> None:
        try:
            online = json.loads(payload.decode("utf-8")).get("online")
        except Exception:  # noqa: BLE001
            online = None
        if online is False and self._connected and not self._closed and self._client is not None:
            # An old will says this host is offline while it is not: say online again.
            self._client.publish(self.state_topic, self._state_payload(True), qos=1, retain=True)

    def _nbirth(self, edge: str, node: _Node, p: dict) -> None:
        node.die()
        node.born = True
        node.seq = p["seq"] if p["seq"] is not None else 0
        node.bdseq = None
        for m in p["metrics"]:
            if m["name"] == BDSEQ and isinstance(m["value"], int):
                node.bdseq = m["value"]
        # A new NBIRTH voids every device's birth: until its DBIRTH, a device's values
        # from before are not current, even if no DDEATH came.
        for key, (e, d) in self.where.items():
            if e == edge and d:
                self._put(key[0], key[1], Mapped(None, UNCERTAIN,
                                                 f"edge node {edge} was born again; waiting for {d}'s DBIRTH"))
        self._apply(edge, "", node, p, birth=True)

    def _ndeath(self, edge: str, node: _Node, p: dict) -> None:
        bdseq = next((m["value"] for m in p["metrics"] if m["name"] == BDSEQ), None)
        if node.born and node.bdseq is not None and bdseq is not None and bdseq != node.bdseq:
            self._ignore("NDEATH from an older session (bdSeq mismatch)")
            return
        node.die()
        self._mark(edge, None, COMM_LOST, f"edge node {edge} died (NDEATH)")

    def _lost_sync(self, edge: str, node: _Node, why: str) -> None:
        self._ignore(why.split(" from ")[0])
        node.die()
        self._mark(edge, None, UNCERTAIN, f"{why}; ignoring the node until it is born again")
        self._request_rebirth(edge)

    def _apply(self, edge: str, device: str, scope: _Scope, p: dict, birth: bool = False) -> None:
        now_ms = _now_ms()
        seen = set()
        for m in p["metrics"]:
            name = m["name"]
            if birth and name is not None:
                scope.types[name] = m["datatype"]
                if m["alias"] is not None:
                    scope.aliases[m["alias"]] = name
            if name is None and m["alias"] is not None:
                name = scope.aliases.get(m["alias"])
            if name is None:
                continue
            seen.add(name)
            targets = self.targets.get((edge, device, name))
            if not targets:
                continue
            if m["is_historical"]:
                self._ignore("historical metric")
                continue
            stamp = m["timestamp"] if m["timestamp"] is not None else p["timestamp"]
            age_s = None if stamp is None else (now_ms - stamp) / 1000.0
            pq, pq_detail = property_quality(m.get("properties") or {})
            if not birth and name not in scope.types:
                value, quality, detail = None, BAD, f"metric {name!r} is not in the birth certificate"
            elif pq != GOOD:
                value, quality, detail = None, pq, pq_detail
            elif m["is_null"]:
                value, quality, detail = None, NULL, "the metric is null"
            else:
                datatype = m["datatype"] if m["datatype"] is not None else scope.types.get(name)
                value, detail = codec.typed_value(datatype, m["value_type"], m["raw"])
                quality = GOOD if value is not None else BAD
            for aid, sig in targets:
                mapped = self.smap.apply(aid, sig, value, quality=quality, age_s=age_s)
                if quality != GOOD:
                    mapped = Mapped(None, quality, detail)
                self._put(aid, sig, mapped)
        if birth:
            for (e, d, metric), targets in self.targets.items():
                if e == edge and d == device and metric not in seen:
                    for aid, sig in targets:
                        self._put(aid, sig, Mapped(None, BAD, f"metric {metric!r} is not in the birth certificate"))

    def _request_rebirth(self, edge: str) -> None:
        """NCMD Node Control/Rebirth = true: a protocol request, never an equipment write."""
        now = time.monotonic()
        last = self._rebirth_at.get(edge)
        if last is not None and now - last < self.rebirth_min_s:
            return
        if self._client is None or not self._connected:
            return
        self._rebirth_at[edge] = now
        ms = _now_ms()
        payload = codec.encode_payload({"timestamp": ms, "metrics": [
            {"name": REBIRTH, "datatype": codec.BOOLEAN, "value": True, "timestamp": ms}]})
        self._client.publish(f"{NAMESPACE}/{self.group_id}/NCMD/{edge}", payload, qos=0, retain=False)
        self.rebirths_requested += 1

    # ---------- quality and the inbox ----------
    def _t(self) -> float:
        try:
            return round(float(self.now()), 3)
        except Exception:  # noqa: BLE001
            return 0.0

    def _put(self, aid: str, sig: str, mapped: Mapped) -> None:
        with self._qlock:
            self._inbox.put(aid, sig, mapped)
            if mapped.good:
                return       # turns good in read(), when the value is handed on
            self.quality[(aid, sig)] = {"quality": mapped.quality, "detail": mapped.detail, "t": self._t()}

    def _mark(self, edge: str, device: Optional[str], quality: str, detail: str) -> None:
        """Every mapped signal of an edge node (device None) or one device gets `quality`."""
        for key, (e, d) in self.where.items():
            if e == edge and (device is None or d == device):
                self._put(key[0], key[1], Mapped(None, quality, detail))

    def _mark_all(self, quality: str, detail: str) -> None:
        for aid, sig in self.where:
            self._put(aid, sig, Mapped(None, quality, detail))

    def _ignore(self, why: str) -> None:
        self.ignored[why] = self.ignored.get(why, 0) + 1

    # ---------- the interface ----------
    def read(self) -> list[Reading]:
        with self._qlock:
            items = self._inbox.drain()
            t = self._t()
            for aid, sig, _value in items:
                self.quality[(aid, sig)] = {"quality": GOOD, "detail": "", "t": t}
        return [self.reading(aid, sig, value) for aid, sig, value in items]

    def status(self) -> dict:
        base = {"id": self.id, "kind": self.kind}
        if self._client is None or self._closed or not self._connected:
            why = "closed" if self._closed else self._why or f"connecting to {self.host}:{self.port}"
            return {**base, "available": False, "detail": why}
        with self._lock:
            born = sorted(e for e, n in self._nodes.items() if n.born)
        waiting = sorted(self.edges - set(born))
        detail = (f"connected to {self.host}:{self.port} as Sparkplug host {self.host_id}, group {self.group_id}; "
                  f"edge nodes online: {', '.join(born) or 'none'}")
        if waiting:
            detail += f"; waiting for a birth from: {', '.join(waiting)}"
        # Connected to the broker but no mapped edge node alive: none of its signals can be read.
        return {**base, "available": bool(set(born) & self.edges) or not self.edges, "detail": detail}

    def send(self, command: dict) -> dict:
        try:
            typ, aid, value = normalize(command)
        except ValueError as e:
            return {"ok": False, "detail": str(e)}
        target = self.cmd_addr.get((aid, typ))
        if target is None:
            return {"ok": False, "detail": f"{self.id} doesn't declare {typ} for {aid}; nothing was sent"}
        edge, device, metric = target
        where = f"{edge}/{device}" if device else f"edge node {edge}"
        with self._lock:
            if self._client is None or self._closed or not self._connected:
                return {"ok": False, "detail": f"{self.id} is not connected to the broker; nothing was sent"}
            node = self._nodes.get(edge)
            scope = None if node is None or not node.born else (node.devices.get(device) if device else node)
            if scope is None or not scope.born:
                return {"ok": False, "detail": f"{where} is offline or hasn't sent a birth certificate; "
                                               f"nothing was sent"}
            if metric not in scope.types:
                return {"ok": False, "detail": f"{where} has no metric {metric!r} in its birth certificate; "
                                               f"nothing was sent"}
            datatype = scope.types[metric]
        try:
            wire = self._wire_value(typ, value, datatype)
            ms = _now_ms()
            payload = codec.encode_payload({"timestamp": ms, "metrics": [
                {"name": metric, "datatype": datatype, "value": wire, "timestamp": ms}]})
        except ValueError as e:
            return {"ok": False, "detail": f"{e}; nothing was sent"}
        topic = (f"{NAMESPACE}/{self.group_id}/DCMD/{edge}/{device}" if device
                 else f"{NAMESPACE}/{self.group_id}/NCMD/{edge}")
        with self._send_lock:
            try:
                info = self._client.publish(topic, payload, qos=0, retain=False)
            except Exception as e:  # noqa: BLE001 - refused before anything was queued
                return {"ok": False, "detail": f"not sent: {type(e).__name__}"}
            if info.rc != 0:
                return {"ok": False, "detail": f"{self.id} lost the broker connection; nothing was sent"}
            try:
                info.wait_for_publish(self.timeout_s)
                sent = info.is_published() and info.rc == 0
            except Exception:  # noqa: BLE001 - the link dropped while it was waiting to go out
                sent = False
            if not sent:
                return uncertain_result()
        kind = "DCMD" if device else "NCMD"
        return {"ok": True, "detail": f"published {kind} {metric} = {wire!r} to {where}; "
                                      f"the next data message confirms the change"}

    @staticmethod
    def _wire_value(typ: str, value: Any, datatype: Optional[int]) -> Any:
        name = codec.type_name(datatype)
        if typ == "CHOKE_WELL":
            if datatype in (codec.FLOAT, codec.DOUBLE):
                return float(value)
            raise ValueError(f"CHOKE_WELL needs a Float or Double metric for the fraction, and this one is {name}")
        if datatype == codec.BOOLEAN:
            return bool(value)
        if datatype in INTEGERS:
            return 1 if value else 0
        if datatype in (codec.FLOAT, codec.DOUBLE):
            return 1.0 if value else 0.0
        if datatype in codec.TEXTS:
            words = {"SET_VALVE": ("OPEN", "CLOSED"), "SET_PUMP": ("ON", "OFF")}[typ]
            return words[0] if value else words[1]
        raise ValueError(f"{typ} can't be written to a {name} metric")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._mark_all(COMM_LOST, "the connector is closed")
        client = self._client
        if client is None:
            return
        connected = self._connected

        def shut() -> None:
            if connected:
                try:
                    client.publish(self.state_topic, self._state_payload(False), qos=1, retain=True) \
                        .wait_for_publish(1.0)
                except Exception:  # noqa: BLE001
                    pass
            client.disconnect()
            client.loop_stop()
        close_later(shut, name=f"freeboard-close-{self.id}")
