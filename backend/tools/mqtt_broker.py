"""A tiny MQTT 3.1.1 broker for the local demo and tests. Standard library only.

Enough of the protocol for the fake SCADA field and the backend's paho client:
CONNECT, PUBLISH (QoS 0, 1, 2 in; QoS 0 out), SUBSCRIBE with `+` and `#`,
UNSUBSCRIBE, PINGREQ, DISCONNECT, and retained messages. No auth, no TLS, no
persistence: it is a lab bench, not production. For a real broker use mosquitto
and point the connector's host/port at it.

In-process hooks: `publish(topic, payload)` sends to every subscriber, and
`on_message(topic, payload)` is called for every PUBLISH a client sends.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Callable, Optional

log = logging.getLogger("freeboard.mqtt_broker")

CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 10, 11, 12, 13, 14


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


def _remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _packet(first: int, body: bytes) -> bytes:
    return bytes([first]) + _remaining_length(len(body)) + body


def _str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def _read_str(buf: bytes, i: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("!H", buf, i)
    return buf[i + 2:i + 2 + n].decode("utf-8", "replace"), i + 2 + n


class _Session:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.subs: set[str] = set()
        self.client_id = ""

    def send(self, data: bytes) -> None:
        if not self.writer.is_closing():
            self.writer.write(data)


class MqttBroker:
    def __init__(self, host: str = "127.0.0.1", port: int = 1883,
                 on_message: Optional[Callable[[str, bytes], None]] = None) -> None:
        self.host, self.port = host, port
        self.on_message = on_message
        self.sessions: set[_Session] = set()
        self.retained: dict[str, bytes] = {}
        self.drop_pubacks = False  # tests: deliver QoS 1 publishes but never acknowledge them
        self.server: Optional[asyncio.base_events.Server] = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]  # the real port when 0 was asked for

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            for s in list(self.sessions):
                s.writer.close()
            await self.server.wait_closed()
            self.server = None

    def drop_clients(self) -> None:
        """Close every client connection (tests: a link that drops). Call from the broker's loop."""
        for s in list(self.sessions):
            s.writer.close()

    @property
    def clients(self) -> int:
        return len(self.sessions)

    def publish(self, topic: str, payload: bytes | str, retain: bool = False) -> None:
        """Send to every matching subscriber (QoS 0). Call from the broker's event loop."""
        data = payload.encode() if isinstance(payload, str) else bytes(payload)
        if retain:
            if data:
                self.retained[topic] = data
            else:
                self.retained.pop(topic, None)
        pkt = _packet(PUBLISH << 4, _str(topic) + data)
        for s in list(self.sessions):
            if any(topic_matches(p, topic) for p in s.subs):
                s.send(pkt)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        s = _Session(writer)
        try:
            while True:
                first = (await reader.readexactly(1))[0]
                n, mult = 0, 1
                while True:
                    b = (await reader.readexactly(1))[0]
                    n += (b & 0x7F) * mult
                    mult *= 128
                    if not b & 0x80:
                        break
                    if mult > 128 ** 4:
                        raise ValueError("bad remaining length")
                body = await reader.readexactly(n) if n else b""
                if not await self._handle(s, first >> 4, first & 0x0F, body):
                    break
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ValueError, struct.error):
            pass
        finally:
            self.sessions.discard(s)
            writer.close()

    async def _handle(self, s: _Session, typ: int, flags: int, body: bytes) -> bool:
        if typ == CONNECT:
            _proto, i = _read_str(body, 0)
            level = body[i]
            if level not in (3, 4):
                s.send(_packet(CONNACK << 4, b"\x00\x01"))  # unacceptable protocol version
                return False
            s.client_id, _ = _read_str(body, i + 4)
            self.sessions.add(s)
            s.send(_packet(CONNACK << 4, b"\x00\x00"))
        elif typ == PUBLISH:
            qos, retain = (flags >> 1) & 3, bool(flags & 1)
            topic, i = _read_str(body, 0)
            pid = None
            if qos:
                (pid,) = struct.unpack_from("!H", body, i)
                i += 2
            payload = body[i:]
            if qos == 1 and not self.drop_pubacks:
                s.send(_packet(PUBACK << 4, struct.pack("!H", pid)))
            elif qos == 2:
                s.send(_packet(PUBREC << 4, struct.pack("!H", pid)))
            self.publish(topic, payload, retain)
            if self.on_message is not None:
                try:
                    self.on_message(topic, payload)
                except Exception:  # noqa: BLE001 - a bad hook must not drop the client
                    log.exception("on_message hook failed")
        elif typ == PUBREL:
            s.send(_packet(PUBCOMP << 4, body[:2]))
        elif typ == SUBSCRIBE:
            (pid,) = struct.unpack_from("!H", body, 0)
            i, granted, new = 2, bytearray(), []
            while i < len(body):
                pattern, i = _read_str(body, i)
                i += 1  # requested QoS; everything goes out at QoS 0
                s.subs.add(pattern)
                new.append(pattern)
                granted.append(0)
            s.send(_packet(SUBACK << 4, struct.pack("!H", pid) + bytes(granted)))
            for topic, data in self.retained.items():
                if any(topic_matches(p, topic) for p in new):
                    s.send(_packet((PUBLISH << 4) | 1, _str(topic) + data))
        elif typ == UNSUBSCRIBE:
            (pid,) = struct.unpack_from("!H", body, 0)
            i = 2
            while i < len(body):
                pattern, i = _read_str(body, i)
                s.subs.discard(pattern)
            s.send(_packet(UNSUBACK << 4, struct.pack("!H", pid)))
        elif typ == PINGREQ:
            s.send(_packet(PINGRESP << 4, b""))
        elif typ == DISCONNECT:
            return False
        return True
