"""LoRaWAN uplink connector (kind "lorawan"): battery sensors through a network server's MQTT.

LoRaWAN sensors (tank level, flow, pressure, pump run contacts) send a small radio
uplink every few minutes. A network server decodes it with the device's payload codec
and publishes it over MQTT. This connector reads two of those integrations:

- ChirpStack v4 (network "chirpstack"): topic application/<application_id>/device/<dev_eui>/event/up,
  JSON with `object` (what the codec decoded), `time`, and deviceInfo.devEui.
- The Things Stack v3 (network "ttn"): topic v3/<application_id>@<tenant>/devices/<device_id>/up,
  JSON with uplink_message.decoded_payload and received_at. The MQTT username is
  usually <application_id>@<tenant> and the password an API key.

It is read-only. LoRaWAN downlinks wait for the device's next uplink window, so a
person could never know when a command acts; send() always refuses, and a spec that
declares commands is a config error.

Addresses are <device>/<dotted path into the decoded object>:

    a84041000181c2ee/level_pct          ChirpStack: the dev_eui (16 hex digits, any case)
    tank-3-level/level_pct              TTN: the device_id (a dev_eui works too)
    a84041000181c2ee/sensors.0.flow     a number in a path step indexes a list

Each declared signal's `scale` and its rule in config.signal_map are applied
(signalmap.SignalMap). Without a rule range, level_pct must be 0 to 100 and flow and
motor amps at least 0.

Freshness. Uplinks are infrequent, so a value may be held: with config
uplink_interval_min above 0, a signal's quality stays good until uplink_interval_min
* 1.5 after its uplink time, and never past that; then it turns stale. read() hands
each uplink's value on once, when it arrives, and never again with a new time: a
repeated reading would put flat points in the live layer's level history between
uplinks, so detect.py's observed fill rate would read near 0 while a tank fills. The
live layer keeps the value until sensors.stale_after_min; set that to cover the
uplink interval to show the value between uplinks (a shorter one shows UNKNOWN
between them, which is never falsely safe). The default uplink_interval_min is 0: no
hold, and an uplink must be at most config max_age_s (60) old when it arrives. An
uplink is not fresh, and gives no values, when it is retained, has no readable time,
is older than that window (or that far in the future), or is not newer than the last
uplink taken from the same device (a replay or a duplicate). A missing field, a codec
error, or a value out of range gives no value for that signal, and its quality says
why at once.

The window is measured on the wall clock, against the uplink's own time from the
network server, not in live minutes: the device uplinks in real time whatever speed
the live clock runs at.

A value that isn't good becomes no reading (see signalmap.py); `quality` says why at
once, for every declared signal: {(asset_id, signal): {"quality", "detail", "t"}}.
Losing the broker, or the broker refusing the subscription, turns every declared
signal comm_lost and makes status() unavailable.

Config: network ("chirpstack" or "ttn"), host, port (1883, or 8883 with tls),
application_id, tenant (ttn, default "ttn"), username, password_env (a FREEBOARD_CONN_
variable), tls (false), client_id, keepalive_s (30), uplink_interval_min (0),
max_age_s (60), signal_map.

paho-mqtt runs on its own network thread. It reconnects forever, each wait from
signalmap.Backoff (1 s doubling to 30 s, with jitter). read(), status() and close()
never wait on the network.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..fieldmap import FieldMap
from .base import Connector, Reading
from .signalmap import (BAD, COMM_LOST, NULL, OUT_OF_RANGE, STALE, Backoff, Inbox, Mapped,
                        SignalMap, check_env_names, close_later, env_secret, import_optional,
                        missing_env)

NETWORKS = ("chirpstack", "ttn")
DEV_EUI = re.compile(r"[0-9A-Fa-f]{16}")
TTN_ID = re.compile(r"[a-z0-9](?:-?[a-z0-9]){1,35}")
TOPIC_CHARS = "+#/\x00"
# Without a rule range, these are the only physical values.
DEFAULT_RANGE = {"level_pct": (0.0, 100.0), "flow_bbl_hr": (0.0, None), "motor_amps": (0.0, None)}
_RFC3339 = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)$")


def parse_time(text: Any) -> Optional[float]:
    """Seconds since the epoch for an RFC 3339 time (any number of fraction digits), or None."""
    if not isinstance(text, str):
        return None
    m = _RFC3339.fullmatch(text.strip().upper())
    if m is None:
        return None
    frac = (m.group(2) or "")[:7]          # "." plus at most 6 digits
    zone = "+00:00" if m.group(3) == "Z" else m.group(3)
    try:
        return datetime.fromisoformat(m.group(1) + frac + zone).timestamp()
    except ValueError:
        return None


def parse_address(address: Any, network: str, what: str) -> tuple[str, tuple]:
    """(device, path) for an address; device is lower case. Raises ValueError saying what is wrong."""
    example = "a84041000181c2ee/level_pct" if network == "chirpstack" else "tank-3-level/level_pct"
    if not isinstance(address, str) or "/" not in address:
        raise ValueError(f"{what} needs an address like {example} (device, then a path into the decoded uplink)")
    if "+" in address or "#" in address or "\x00" in address:
        raise ValueError(f"{what}: address {address!r} has an MQTT wildcard; addresses are exact")
    device, _, dotted = address.partition("/")
    device = device.strip().lower()
    if network == "chirpstack" and not DEV_EUI.fullmatch(device):
        raise ValueError(f"{what}: {device!r} is not a dev_eui (16 hex digits), as in {example}")
    if network == "ttn" and not (DEV_EUI.fullmatch(device) or TTN_ID.fullmatch(device)):
        raise ValueError(f"{what}: {device!r} is not a TTN device_id or dev_eui, as in {example}")
    path = tuple(dotted.split("."))
    if not dotted or any(not p for p in path):
        raise ValueError(f"{what}: address {address!r} needs a dotted path into the decoded uplink, as in {example}")
    return device, path


def lookup(obj: Any, path: tuple) -> tuple[bool, Any]:
    """(found, value) for a dotted path into a decoded uplink."""
    for step in path:
        if isinstance(obj, dict):
            if step not in obj:
                return False, None
            obj = obj[step]
        elif isinstance(obj, list) and step.isdigit() and int(step) < len(obj):
            obj = obj[int(step)]
        else:
            return False, None
    return True, obj


def codec_error(obj: Any) -> Optional[str]:
    """Why a decoded object is a codec error and not data, or None. Codecs written to the
    LoRaWAN payload codec API return {"data", "errors", "warnings"}; a non-empty `errors`
    (or an `error`) means the payload didn't decode."""
    if not isinstance(obj, dict):
        return "the decoded payload is not an object"
    for key in ("errors", "error"):
        err = obj.get(key)
        if err:
            first = err[0] if isinstance(err, list) else err
            return f"the payload codec reported an error: {str(first)[:120]}"
    return None


def _positive(config: dict, key: str, default: float, allow_zero: bool = False) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"config {key} must be a number, got {value!r}")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"config {key} must be {'at least 0' if allow_zero else 'above 0'}, got {value!r}")
    return float(value)


def _name(config: dict, key: str, what: str, default: Optional[str] = None) -> str:
    value = config.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"config {key} is required ({what})")
    if any(c in value for c in TOPIC_CHARS):
        raise ValueError(f"config {key} {value!r} can't contain /, + or #")
    return value.strip()


class LorawanConnector(Connector):
    kind = "lorawan"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float],
                 clock: Callable[[], float] = time.time) -> None:
        super().__init__(spec, fmap, now)
        cfg = self.config
        check_env_names(cfg)
        self.smap = SignalMap(spec)
        self.clock = clock                       # wall clock, for uplink times
        network = cfg.get("network")
        if network not in NETWORKS:
            raise ValueError(f"config network must be one of {', '.join(NETWORKS)}, got {network!r}")
        self.network = network
        self.host = cfg.get("host")
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("config host is required (the network server's MQTT broker)")
        tls = cfg.get("tls", False)
        if not isinstance(tls, bool):
            raise ValueError("config tls must be true or false")
        self.tls = tls
        port = cfg.get("port", 8883 if tls else 1883)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError(f"config port must be a TCP port number, got {port!r}")
        self.port = port
        self.application_id = _name(cfg, "application_id", "the network server application to read")
        self.tenant = _name(cfg, "tenant", "the TTN tenant", "ttn") if network == "ttn" else None
        username = cfg.get("username")
        if username is not None and not isinstance(username, str):
            raise ValueError("config username must be text")
        self.username = username
        self.client_id = cfg.get("client_id") or f"freeboard-lorawan-{self.id}"
        if not isinstance(self.client_id, str):
            raise ValueError("config client_id must be text")
        self.keepalive_s = max(1, int(round(_positive(cfg, "keepalive_s", 30))))
        self.interval_min = _positive(cfg, "uplink_interval_min", 0, allow_zero=True)
        # How long an uplink counts, from its own time.
        self.window_s = self.interval_min * 60 * 1.5 if self.interval_min > 0 else self.smap.max_age_s
        if self.commands:
            raise ValueError("commands: LoRaWAN uplink sensors are read-only; remove the commands")

        # device -> [(path, asset_id, signal)]
        self.targets: dict[str, list[tuple]] = {}
        for (aid, sig), addr in self.signals.items():
            device, path = parse_address(addr, network, f"signals: {aid}.{sig}")
            self.targets.setdefault(device, []).append((path, aid, sig))
        if network == "chirpstack":
            self.topic = f"application/{self.application_id}/device/+/event/up"
        else:
            self.topic = f"v3/{self.application_id}@{self.tenant}/devices/+/up"

        self._lock = threading.RLock()
        self._inbox = Inbox()
        self._held: dict[tuple, float] = {}         # (asset_id, signal) -> good until, epoch s
        self._last_up: dict[str, float] = {}         # device -> uplink time of the last uplink taken
        self._backoff = Backoff(initial_s=1.0, max_s=30.0)
        self._connected = False
        self._subscribed = False                     # the broker granted the uplink topic
        self._closed = False
        self._why = f"connecting to {self.host}:{self.port}"
        self._refused = ""
        self.uplinks = 0                             # uplinks taken, for status and tests
        self.ignored: dict[str, int] = {}            # why -> count
        t = self._t()
        self.quality = {key: {"quality": COMM_LOST, "detail": "not connected yet", "t": t}
                        for key in self.signals}
        self._client = None
        missing = missing_env(cfg)
        if missing:
            self._why = f"environment variable {missing} is not set (the broker password)"
            return
        mqtt, err = import_optional("paho.mqtt.client", "paho-mqtt")
        if mqtt is None:
            self._why = err or "paho-mqtt is not installed"
            return
        self._start(mqtt)

    # ---------- connection (callbacks run on paho's thread) ----------
    def _start(self, mqtt) -> None:
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=self.client_id, clean_session=True, protocol=mqtt.MQTTv311)
        if self.username is not None:
            client.username_pw_set(self.username, env_secret(self.config, "password"))
        if self.tls:
            client.tls_set()
        client.connect_timeout = 5.0
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        client.on_message = self._on_message
        client.on_subscribe = self._on_subscribe
        self._client = client
        client.connect_async(self.host, self.port, keepalive=self.keepalive_s)
        client.loop_start()

    def _next_wait(self, client) -> float:
        delay = self._backoff.next_delay()
        client.reconnect_delay_set(min_delay=delay, max_delay=delay)   # paho waits exactly this long
        return delay

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        if self._closed:
            return
        if getattr(reason_code, "is_failure", False):
            self._refused = f"the broker at {self.host}:{self.port} refused the connection ({reason_code})"
            self._why = self._refused
            return
        with self._lock:
            self._connected = True
            self._subscribed = False
            self._why = f"connected to {self.host}:{self.port}; waiting for the broker to grant {self.topic}"
            self._refused = ""
        self._backoff.reset()
        client.subscribe(self.topic, qos=0)

    def _on_subscribe(self, _client, _userdata, _mid, reason_codes, _props=None) -> None:
        if self._closed:
            return
        codes = list(reason_codes or [])
        if not codes or any(getattr(rc, "is_failure", rc == 0x80) for rc in codes):
            why = (f"the broker refused the subscription to {self.topic} "
                   "(check application_id, tenant, and the account's read rights)")
            with self._lock:
                self._why = why
            self._lost(why, link_up=True)
            return
        with self._lock:
            self._subscribed = True
            self._why = ""

    def _lost(self, why: str, link_up: bool = False) -> None:
        with self._lock:
            self._connected = link_up
            self._subscribed = False
            self._held = {}
            self._inbox.clear()
        self._mark_all(COMM_LOST, why)

    def _on_disconnect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        if self._closed:
            with self._lock:
                self._connected = False
            return
        lost = self._refused or f"lost the connection to the broker at {self.host}:{self.port} ({reason_code})"
        self._why = f"{lost}; retrying in {self._next_wait(client):.1f} s"
        self._lost("no connection to the network server's MQTT broker")

    def _on_connect_fail(self, client, _userdata) -> None:
        if self._closed:
            return
        self._why = f"can't reach the broker at {self.host}:{self.port}; retrying in {self._next_wait(client):.1f} s"
        self._lost("no connection to the network server's MQTT broker")

    def _on_message(self, _client, _userdata, msg) -> None:
        if self._closed:
            return
        try:
            self._handle(str(msg.topic), bytes(msg.payload), bool(msg.retain))
        except Exception:  # noqa: BLE001 - one bad message must not stop the network thread
            self._ignore("a message the connector couldn't handle")

    # ---------- uplinks ----------
    def _parse(self, topic: str, doc: dict) -> Optional[tuple]:
        """(topic device, payload device ids, uplink time, decoded object or None, why) or None."""
        parts = topic.split("/")
        if self.network == "chirpstack":
            if len(parts) != 6 or parts[0] != "application" or parts[4:] != ["event", "up"]:
                return None
            info = doc.get("deviceInfo") if isinstance(doc.get("deviceInfo"), dict) else {}
            app = info.get("applicationId")
            if app is not None and str(app) != self.application_id:
                self._ignore("an uplink for another application")
                return None
            ids = {str(info.get("devEui", "")).lower()}
            when = parse_time(doc.get("time"))
            if when is None:
                rx = doc.get("rxInfo") if isinstance(doc.get("rxInfo"), list) else []
                times = [parse_time(r.get("nsTime")) for r in rx if isinstance(r, dict)]
                times = [x for x in times if x is not None]
                when = max(times) if times else None
            has_obj = "object" in doc
            obj = doc.get("object")
            missing = "the uplink has no decoded object (the codec failed or isn't set)"
        else:
            if len(parts) != 5 or parts[0] != "v3" or parts[2] != "devices" or parts[4] != "up":
                return None
            ends = doc.get("end_device_ids") if isinstance(doc.get("end_device_ids"), dict) else {}
            apps = ends.get("application_ids") if isinstance(ends.get("application_ids"), dict) else {}
            app = apps.get("application_id")
            if app is not None and str(app) != self.application_id:
                self._ignore("an uplink for another application")
                return None
            ids = {str(ends.get("device_id", "")).lower(), str(ends.get("dev_eui", "")).lower()}
            up = doc.get("uplink_message") if isinstance(doc.get("uplink_message"), dict) else {}
            when = parse_time(doc.get("received_at"))
            if when is None:
                when = parse_time(up.get("received_at"))
            has_obj = "decoded_payload" in up
            obj = up.get("decoded_payload")
            missing = "the uplink has no decoded_payload (the payload formatter failed or isn't set)"
        device = parts[3].lower()
        if device not in ids:
            self._ignore("an uplink whose topic and payload name different devices")
            return None
        ids.discard("")
        why = None if has_obj and obj is not None else missing
        if why is None:
            why = codec_error(obj)
        return device, ids, when, obj, why

    def _handle(self, topic: str, payload: bytes, retained: bool) -> None:
        try:
            doc = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._ignore("a message that isn't JSON")
            return
        if not isinstance(doc, dict):
            self._ignore("a message that isn't a JSON object")
            return
        parsed = self._parse(topic, doc)
        if parsed is None:
            return
        _device, ids, when, obj, why = parsed
        targets = [t for dev in ids for t in self.targets.get(dev, [])]
        if not targets:
            return
        if retained:
            self._ignore("a retained uplink")
            self._mark(targets, STALE, "a retained uplink from the broker, not a new one")
            return
        if when is None:
            self._ignore("an uplink with no readable time")
            self._mark(targets, STALE, "the uplink has no readable time, so its freshness is unknown")
            return
        age = self.clock() - when
        future_limit = max(self.smap.max_age_s, 5.0)
        if age > self.window_s or age < -future_limit:
            self._ignore("an uplink outside its freshness window")
            text = (f"the uplink is {age:.0f} s old (limit {self.window_s:g} s)" if age > 0
                    else f"the uplink time is {-age:.0f} s in the future")
            self._mark(targets, STALE, text)
            return
        with self._lock:
            last = max((self._last_up.get(d, float("-inf")) for d in ids), default=float("-inf"))
            if when <= last:
                self._ignore("a replayed or duplicate uplink")
                return
            for d in ids:
                self._last_up[d] = when
            self.uplinks += 1
        expires = when + self.window_s
        for path, aid, sig in targets:
            if why is not None:
                mapped = Mapped(None, BAD, why)
            else:
                found, raw = lookup(obj, path)
                if not found:
                    mapped = Mapped(None, NULL, f"the decoded uplink has no field {'.'.join(path)}")
                else:
                    mapped = self._range(aid, sig, self.smap.apply(aid, sig, raw))
            self._put(aid, sig, mapped, expires)

    def _range(self, aid: str, sig: str, mapped: Mapped) -> Mapped:
        """The default physical range, for a signal whose rule doesn't set one."""
        if not mapped.good or sig not in DEFAULT_RANGE:
            return mapped
        rule = self.smap.rules.get((aid, sig))
        lo, hi = DEFAULT_RANGE[sig]
        v = mapped.value
        if (rule is None or rule.range is None) and (v < lo or (hi is not None and v > hi)):
            top = f"{hi:g}" if hi is not None else "up"
            return Mapped(None, OUT_OF_RANGE, f"{v:g} is outside the physical range for {sig}, {lo:g} to {top}")
        return mapped

    # ---------- quality, the inbox, and held values ----------
    def _t(self) -> float:
        try:
            return round(float(self.now()), 3)
        except Exception:  # noqa: BLE001
            return 0.0

    def _put(self, aid: str, sig: str, mapped: Mapped, expires: Optional[float] = None) -> None:
        key = (aid, sig)
        with self._lock:
            if self._closed and mapped.good:
                return                               # a message that raced close()
            self._inbox.put(aid, sig, mapped)
            if mapped.good and expires is not None:
                self._held[key] = expires
            else:
                self._held.pop(key, None)
            detail = mapped.detail if not mapped.good else ""
            self.quality[key] = {"quality": mapped.quality, "detail": detail, "t": self._t()}

    def _mark(self, targets: list, quality: str, detail: str) -> None:
        for _path, aid, sig in targets:
            self._put(aid, sig, Mapped(None, quality, detail))

    def _mark_all(self, quality: str, detail: str) -> None:
        for aid, sig in self.signals:
            self._put(aid, sig, Mapped(None, quality, detail))

    def _ignore(self, why: str) -> None:
        self.ignored[why] = self.ignored.get(why, 0) + 1

    # ---------- the interface ----------
    def read(self) -> list[Reading]:
        now = self.clock()
        with self._lock:
            if self._closed:
                self._inbox.clear()
                return []
            out = {(aid, sig): value for aid, sig, value in self._inbox.drain()}
            for key, expires in list(self._held.items()):
                if now >= expires:
                    del self._held[key]
                    out.pop(key, None)
                    if self.interval_min > 0:
                        text = (f"no uplink within {self.window_s / 60:g} min "
                                f"(1.5 times uplink_interval_min {self.interval_min:g})")
                    else:
                        text = f"no uplink within max_age_s {self.window_s:g} s"
                    self.quality[key] = {"quality": STALE, "detail": text, "t": self._t()}
        return [self.reading(aid, sig, value) for (aid, sig), value in out.items()]

    def status(self) -> dict:
        base = {"id": self.id, "kind": self.kind}
        if self._client is None or self._closed or not self._connected or not self._subscribed:
            why = "closed" if self._closed else self._why or f"connecting to {self.host}:{self.port}"
            return {**base, "available": False, "detail": why}
        with self._lock:
            last = max(self._last_up.values(), default=None)
        where = (f"ChirpStack application {self.application_id}" if self.network == "chirpstack"
                 else f"The Things Stack application {self.application_id}@{self.tenant}")
        heard = "no uplink yet" if last is None else f"last uplink {max(0.0, self.clock() - last):.0f} s ago"
        return {**base, "available": True,
                "detail": f"connected to {self.host}:{self.port}, {where}; {heard}; read-only"}

    def send(self, command: dict) -> dict:
        return {"ok": False, "detail": f"{self.id} reads LoRaWAN uplink sensors and never writes; nothing was sent"}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self._held = {}
            self._inbox.clear()
        self._mark_all(COMM_LOST, "the connector is closed")
        client = self._client
        if client is None:
            return

        def shut() -> None:
            client.disconnect()
            client.loop_stop()
        close_later(shut, name=f"freeboard-close-{self.id}")
