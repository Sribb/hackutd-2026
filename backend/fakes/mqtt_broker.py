"""A small MQTT 3.1.1 broker for the tests and the local demo, on 127.0.0.1 only.

Standard library only (asyncio). It speaks enough of the protocol for paho-mqtt and the
fake devices:

- CONNECT and CONNACK, with a client id (one is assigned when it is empty), keepalive,
  and a will message. A username and password are ignored unless the broker was started
  with username= and password=; then a CONNECT without that exact pair gets CONNACK code 4
  (bad user name or password). The broker never logs or keeps what a client sent.
- Clean sessions only: nothing is kept for a client between connections, so CONNACK
  always says no session is present.
- SUBSCRIBE and SUBACK with + and # wildcards; UNSUBSCRIBE and UNSUBACK.
- PUBLISH at QoS 0 and 1 in both directions. A QoS 1 publish gets its PUBACK, and a
  message goes out at the lower of its QoS and the subscription's. A QoS 2 publish closes
  the connection, since this broker doesn't do QoS 2.
- Retained messages, one per topic (an empty retained payload deletes it), sent with the
  retain flag set to a new subscription. A message forwarded to a subscription that
  already existed has the retain flag cleared, as MQTT 3.1.1 says.
- The will, published when a client goes away without DISCONNECT: its socket drops, it
  says nothing for 1.5 times its keepalive, it breaks the protocol, or a new connection
  takes over its client id.
- PINGREQ and PINGRESP, and DISCONNECT (which drops the will).

No persistence, TLS, or QoS 2: it is a lab bench. Point a connector at
mosquitto for anything real.

    python -m backend.fakes.mqtt_broker --port 1885

In-process, start(host="127.0.0.1", port=0) runs it on its own event loop thread and returns a handle with
.port, .publish(), .retained(), .log(), .clients(), and .stop(). MqttClient at the bottom
is the minimal client the fake devices use.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import itertools
import struct
import threading
import time
import uuid
from typing import Any, Callable, Optional

HOST = "127.0.0.1"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 10, 11, 12, 13, 14
MAX_PACKET = 1 << 20           # bytes; a bigger packet closes the connection
CONNECT_TIMEOUT_S = 10.0       # a socket that doesn't send CONNECT by then is closed
LOG_SIZE = 10_000              # publishes kept in .log for tests


class ProtocolError(Exception):
    """The peer broke MQTT 3.1.1 (or used what this broker doesn't support)."""


# ---------- packets ----------

def remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def packet(first: int, body: bytes = b"") -> bytes:
    return bytes([first]) + remaining_length(len(body)) + body


def mqtt_string(text: str) -> bytes:
    data = text.encode("utf-8")
    if len(data) > 0xFFFF:
        raise ValueError("an MQTT string is at most 65535 bytes")
    return struct.pack("!H", len(data)) + data


def read_string(buf: bytes, i: int) -> tuple[str, int]:
    if i + 2 > len(buf):
        raise ProtocolError("the packet ends inside a string length")
    (n,) = struct.unpack_from("!H", buf, i)
    if i + 2 + n > len(buf):
        raise ProtocolError("the packet ends inside a string")
    try:
        return bytes(buf[i + 2:i + 2 + n]).decode("utf-8"), i + 2 + n
    except UnicodeDecodeError as e:
        raise ProtocolError("a string isn't UTF-8") from e


def read_binary(buf: bytes, i: int) -> tuple[bytes, int]:
    if i + 2 > len(buf):
        raise ProtocolError("the packet ends inside a length")
    (n,) = struct.unpack_from("!H", buf, i)
    if i + 2 + n > len(buf):
        raise ProtocolError("the packet ends inside binary data")
    return bytes(buf[i + 2:i + 2 + n]), i + 2 + n


async def read_packet(reader: asyncio.StreamReader, max_size: int = MAX_PACKET) -> tuple[int, bytes]:
    """(first byte, body) of the next packet. Raises IncompleteReadError at end of stream."""
    head = await reader.readexactly(1)
    n, mult = 0, 1
    for _ in range(4):
        byte = (await reader.readexactly(1))[0]
        n += (byte & 0x7F) * mult
        if not byte & 0x80:
            break
        mult *= 128
    else:
        raise ProtocolError("the remaining length runs past 4 bytes")
    if n > max_size:
        raise ProtocolError(f"a {n} byte packet is over the {max_size} byte limit")
    return head[0], (await reader.readexactly(n) if n else b"")


def publish_packet(topic: str, payload: bytes, qos: int = 0, retain: bool = False,
                   pid: Optional[int] = None, dup: bool = False) -> bytes:
    first = PUBLISH << 4 | (0x08 if dup else 0) | qos << 1 | (0x01 if retain else 0)
    body = mqtt_string(topic) + (struct.pack("!H", pid) if qos else b"") + bytes(payload)
    return packet(first, body)


def valid_topic(topic: str) -> bool:
    """A topic a message can be published to: not empty, no wildcards, no NUL."""
    return bool(topic) and not any(c in topic for c in "+#\x00") and len(topic.encode("utf-8")) <= 0xFFFF


def valid_filter(filt: str) -> bool:
    """A subscription filter: # only as the whole last level, + only as a whole level."""
    if not filt or "\x00" in filt or len(filt.encode("utf-8")) > 0xFFFF:
        return False
    levels = filt.split("/")
    for i, level in enumerate(levels):
        if "#" in level and (level != "#" or i != len(levels) - 1):
            return False
        if "+" in level and level != "+":
            return False
    return True


def topic_matches(filt: str, topic: str) -> bool:
    """MQTT 3.1.1 matching. A wildcard at the first level never matches a $ topic."""
    if topic.startswith("$") and filt[:1] in ("+", "#"):
        return False
    f, t = filt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


# ---------- the broker ----------

class _Client:
    def __init__(self, writer: asyncio.StreamWriter, client_id: str, keepalive: int,
                 will: Optional[tuple]) -> None:
        self.writer = writer
        self.client_id = client_id
        self.keepalive = keepalive
        self.will = will                     # (topic, payload, qos, retain) or None
        self.subs: dict[str, int] = {}       # filter -> granted QoS
        self.pids = itertools.cycle(range(1, 0x10000))
        self.gone = False                    # its will is settled: published or dropped

    def send(self, data: bytes) -> None:
        if not self.writer.is_closing():
            self.writer.write(data)

    def deliver(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        self.send(publish_packet(topic, payload, qos, retain, next(self.pids) if qos else None))


class Broker:
    """The broker itself. Every method runs on its event loop."""

    def __init__(self, host: str = HOST, port: int = 0, max_packet: int = MAX_PACKET,
                 username: Optional[str] = None, password: Optional[str] = None) -> None:
        if host not in LOOPBACK:
            raise ValueError(f"the fake broker listens on 127.0.0.1 only, not {host!r}")
        self.host, self.port, self.max_packet = host, int(port), max_packet
        # (username, password bytes) a client must send, or None to let anyone in.
        self._login = None if username is None else (username, (password or "").encode("utf-8"))
        self.clients: dict[str, _Client] = {}
        self.retained: dict[str, tuple[bytes, int]] = {}      # topic -> (payload, qos)
        self.log: collections.deque = collections.deque(maxlen=LOG_SIZE)
        self.on_publish: Optional[Callable[[dict], None]] = None
        self.server: Optional[asyncio.base_events.Server] = None
        self.stopping = False
        self._writers: set = set()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._serve, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        """Close the listener and every connection at once. No will is published: with
        the broker gone there is nobody to tell."""
        self.stopping = True
        if self.server is not None:
            self.server.close()
        for writer in list(self._writers):
            writer.transport.abort()
        if self.server is not None:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 5)
            except (asyncio.TimeoutError, OSError):
                pass
        self.clients.clear()

    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        """Publish as the broker itself (tests and fakes)."""
        data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        self._route(topic, data, min(int(qos), 1), retain, sender=None)

    # ---- one connection ----
    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        client: Optional[_Client] = None
        clean = False
        try:
            first, body = await asyncio.wait_for(read_packet(reader, self.max_packet), CONNECT_TIMEOUT_S)
            if first >> 4 != CONNECT:
                return
            client = self._connect(body, writer)
            while client is not None:
                timeout = client.keepalive * 1.5 if client.keepalive else None
                first, body = await asyncio.wait_for(read_packet(reader, self.max_packet), timeout)
                if first >> 4 == DISCONNECT:
                    clean = True
                    return
                self._handle(client, first, body)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError, ProtocolError):
            pass   # an unclean end: the will goes out below
        finally:
            self._writers.discard(writer)
            if client is not None:
                self._gone(client, clean)
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - already gone
                pass

    def _connect(self, body: bytes, writer: asyncio.StreamWriter) -> Optional[_Client]:
        name, i = read_string(body, 0)
        if i + 4 > len(body):
            raise ProtocolError("CONNECT is too short")
        level, flags = body[i], body[i + 1]
        (keepalive,) = struct.unpack_from("!H", body, i + 2)
        i += 4
        if (name, level) not in (("MQTT", 4), ("MQIsdp", 3)):
            writer.write(packet(CONNACK << 4, b"\x00\x01"))     # unacceptable protocol version
            return None
        if flags & 0x01:
            raise ProtocolError("the reserved CONNECT flag is set")
        client_id, i = read_string(body, i)
        will = None
        if flags & 0x04:
            topic, i = read_string(body, i)
            payload, i = read_binary(body, i)
            qos = (flags >> 3) & 0x03
            if qos > 2 or not valid_topic(topic):
                raise ProtocolError("a bad will")
            will = (topic, payload, min(qos, 1), bool(flags & 0x20))
        elif flags & 0x38:
            raise ProtocolError("will QoS or retain without a will")
        user = password = None
        if flags & 0x80:
            user, i = read_string(body, i)
        if flags & 0x40:
            password, i = read_binary(body, i)
        if self._login is not None and (user, password) != self._login:
            writer.write(packet(CONNACK << 4, b"\x00\x04"))      # bad user name or password
            return None
        if not client_id:
            if not flags & 0x02:
                writer.write(packet(CONNACK << 4, b"\x00\x02"))  # identifier rejected
                return None
            client_id = f"auto-{uuid.uuid4().hex[:12]}"
        old = self.clients.get(client_id)
        if old is not None:          # takeover: the old connection ends as if it dropped
            self._gone(old, clean=False)
            old.writer.transport.abort()
        client = _Client(writer, client_id, keepalive, will)
        self.clients[client_id] = client
        client.send(packet(CONNACK << 4, b"\x00\x00"))
        return client

    def _gone(self, client: _Client, clean: bool) -> None:
        if self.clients.get(client.client_id) is client:
            del self.clients[client.client_id]
        if client.gone:
            return
        client.gone = True
        if not clean and not self.stopping and client.will is not None:
            topic, payload, qos, retain = client.will
            self._route(topic, payload, qos, retain, sender=client.client_id, will=True)

    def _handle(self, client: _Client, first: int, body: bytes) -> None:
        kind = first >> 4
        if kind == PUBLISH:
            self._publish_in(client, first, body)
        elif kind == PUBACK:
            pass
        elif kind == SUBSCRIBE and first & 0x0F == 0x02:
            self._subscribe(client, body)
        elif kind == UNSUBSCRIBE and first & 0x0F == 0x02:
            self._unsubscribe(client, body)
        elif kind == PINGREQ:
            client.send(packet(PINGRESP << 4))
        else:
            raise ProtocolError(f"packet type {kind} (flags {first & 0x0F}) isn't expected here")

    def _publish_in(self, client: _Client, first: int, body: bytes) -> None:
        qos, retain = (first >> 1) & 0x03, bool(first & 0x01)
        if qos > 1:
            raise ProtocolError("QoS 2 isn't supported by this test broker")
        topic, i = read_string(body, 0)
        if not valid_topic(topic):
            raise ProtocolError(f"can't publish to {topic!r}")
        pid = None
        if qos:
            if i + 2 > len(body):
                raise ProtocolError("PUBLISH is missing its packet id")
            (pid,) = struct.unpack_from("!H", body, i)
            i += 2
            if pid == 0:
                raise ProtocolError("packet id 0")
        self._route(topic, bytes(body[i:]), qos, retain, sender=client.client_id)
        if qos:
            client.send(packet(PUBACK << 4, struct.pack("!H", pid)))

    def _route(self, topic: str, payload: bytes, qos: int, retain: bool, sender: Optional[str],
               will: bool = False) -> None:
        entry = {"t": time.time(), "client_id": sender, "topic": topic, "payload": payload,
                 "qos": qos, "retain": retain, "will": will}
        self.log.append(entry)
        if retain:
            if payload:
                self.retained[topic] = (payload, qos)
            else:
                self.retained.pop(topic, None)
        for c in list(self.clients.values()):
            granted = max((q for f, q in c.subs.items() if topic_matches(f, topic)), default=None)
            if granted is not None:
                c.deliver(topic, payload, min(qos, granted), retain=False)
        if self.on_publish is not None:
            try:
                self.on_publish(entry)
            except Exception:  # noqa: BLE001 - a test hook must not break the broker
                pass

    def _subscribe(self, client: _Client, body: bytes) -> None:
        if len(body) < 2:
            raise ProtocolError("SUBSCRIBE is missing its packet id")
        (pid,) = struct.unpack_from("!H", body, 0)
        i, pairs = 2, []
        while i < len(body):
            filt, i = read_string(body, i)
            if i >= len(body) or body[i] & 0xFC:
                raise ProtocolError("a bad requested QoS")
            pairs.append((filt, body[i]))
            i += 1
        if not pairs:
            raise ProtocolError("SUBSCRIBE with no filters")
        codes, granted = [], {}
        for filt, qos in pairs:
            if not valid_filter(filt):
                codes.append(0x80)
                continue
            client.subs[filt] = granted[filt] = min(qos, 1)
            codes.append(granted[filt])
        client.send(packet(SUBACK << 4, struct.pack("!H", pid) + bytes(codes)))
        for topic, (payload, rqos) in list(self.retained.items()):
            best = max((q for f, q in granted.items() if topic_matches(f, topic)), default=None)
            if best is not None:
                client.deliver(topic, payload, min(rqos, best), retain=True)

    def _unsubscribe(self, client: _Client, body: bytes) -> None:
        if len(body) < 2:
            raise ProtocolError("UNSUBSCRIBE is missing its packet id")
        (pid,) = struct.unpack_from("!H", body, 0)
        i = 2
        while i < len(body):
            filt, i = read_string(body, i)
            client.subs.pop(filt, None)
        client.send(packet(UNSUBACK << 4, struct.pack("!H", pid)))


# ---------- running it on a thread ----------

class LoopThread:
    """An asyncio loop on a daemon thread, so a test can run fakes in-process."""

    def __init__(self, name: str = "fake-device") -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name=name, daemon=True)
        self.thread.start()

    def run(self, coro, timeout: float = 10.0) -> Any:
        """Run a coroutine on the loop and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def call(self, fn: Callable, *args, timeout: float = 10.0) -> Any:
        """Call fn(*args) on the loop and wait for its result."""
        async def wrap():
            return fn(*args)
        return self.run(wrap(), timeout)

    def stop(self) -> None:
        """Stop the loop, cancel what is left on it, and close it."""
        if self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            return    # stuck in a callback: closing a running loop would raise
        try:
            pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        finally:
            self.loop.close()


class BrokerThread:
    """A running broker on its own loop thread. Every method is safe from any thread."""

    def __init__(self, host: str = HOST, port: int = 0, loop: Optional[LoopThread] = None,
                 username: Optional[str] = None, password: Optional[str] = None) -> None:
        self._own_loop = loop is None
        self.lt = loop or LoopThread("fake-mqtt-broker")
        self.broker = Broker(host, port, username=username, password=password)
        try:
            self.lt.run(self.broker.start())
        except Exception:
            if self._own_loop:
                self.lt.stop()
            raise
        self.host, self.port = host, self.broker.port
        self.stopped = False

    def publish(self, topic: str, payload: bytes | str, qos: int = 0, retain: bool = False) -> None:
        self.lt.call(self.broker.publish, topic, payload, qos, retain)

    def retained(self) -> dict:
        """{topic: payload} the broker keeps."""
        return self.lt.call(lambda: {t: p for t, (p, _q) in self.broker.retained.items()})

    def log(self) -> list[dict]:
        """Every publish so far: {t, client_id, topic, payload, qos, retain, will}."""
        return self.lt.call(lambda: list(self.broker.log))

    def clients(self) -> dict:
        """{client_id: {"subs": {...}, "will": (topic, payload, qos, retain) or None, "keepalive"}}."""
        return self.lt.call(lambda: {cid: {"subs": dict(c.subs), "will": c.will, "keepalive": c.keepalive}
                                     for cid, c in self.broker.clients.items()})

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        try:
            self.lt.run(self.broker.stop())
        finally:
            if self._own_loop:
                self.lt.stop()


def start(host: str = HOST, port: int = 0, username: Optional[str] = None,
          password: Optional[str] = None) -> BrokerThread:
    """Start a broker on 127.0.0.1 (port 0 picks a free one) on its own thread and return
    its handle; .port is the bound port. With username and password set, a client must
    log in with exactly those. Call .stop() when done."""
    return BrokerThread(host, port, username=username, password=password)


# ---------- a minimal client, for the fake devices ----------

class MqttClient:
    """Just enough of an MQTT 3.1.1 client for a fake device, on an asyncio loop: CONNECT
    with a will, SUBSCRIBE, QoS 0 publishes, keepalive pings, received publishes passed to
    on_message(topic, payload, retain), and drop(), which cuts the socket without
    DISCONNECT so the broker publishes the will."""

    def __init__(self, host: str, port: int, client_id: str, keepalive: int = 10,
                 will: Optional[tuple] = None,
                 on_message: Optional[Callable[[str, bytes, bool], None]] = None,
                 on_lost: Optional[Callable[[], None]] = None) -> None:
        self.host, self.port, self.client_id = host, port, client_id
        self.keepalive = keepalive
        self.will = will                      # (topic, payload bytes, qos, retain)
        self.on_message = on_message
        self.on_lost = on_lost
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.connected = False
        self._pids = itertools.cycle(range(1, 0x10000))
        self._acks: dict[int, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []

    async def connect(self, timeout: float = 5.0) -> None:
        self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout)
        flags = 0x02
        payload = mqtt_string(self.client_id)
        if self.will is not None:
            topic, data, qos, retain = self.will
            flags |= 0x04 | (qos & 0x03) << 3 | (0x20 if retain else 0)
            payload += mqtt_string(topic) + struct.pack("!H", len(data)) + bytes(data)
        body = mqtt_string("MQTT") + bytes([4, flags]) + struct.pack("!H", self.keepalive) + payload
        self.writer.write(packet(CONNECT << 4, body))
        first, ack = await asyncio.wait_for(read_packet(self.reader), timeout)
        if first >> 4 != CONNACK or len(ack) != 2 or ack[1] != 0:
            self.writer.transport.abort()
            raise ConnectionError(f"the broker refused the connection ({ack!r})")
        self.connected = True
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._read_loop())]
        if self.keepalive:
            self._tasks.append(loop.create_task(self._ping_loop()))

    async def subscribe(self, filters: list, timeout: float = 5.0) -> bytes:
        """Subscribe to [(filter, qos)] and wait for the SUBACK; returns its return codes."""
        pid = next(self._pids)
        fut = asyncio.get_running_loop().create_future()
        self._acks[pid] = fut
        body = struct.pack("!H", pid) + b"".join(mqtt_string(f) + bytes([q]) for f, q in filters)
        self._write(packet(SUBSCRIBE << 4 | 0x02, body))
        return await asyncio.wait_for(fut, timeout)

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> bool:
        """Publish at QoS 0. False if not connected (nothing was written)."""
        return self._write(publish_packet(topic, payload, 0, retain))

    def drop(self) -> None:
        """Cut the connection without DISCONNECT, as a crash would. The broker publishes the will."""
        self.connected = False
        for t in self._tasks:
            t.cancel()
        if self.writer is not None:
            self.writer.transport.abort()

    async def disconnect(self) -> None:
        """Send DISCONNECT and close: the broker drops the will."""
        if self.connected:
            self._write(packet(DISCONNECT << 4))
            try:
                await asyncio.wait_for(self.writer.drain(), 2)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass
        self.connected = False
        for t in self._tasks:
            t.cancel()
        if self.writer is not None:
            self.writer.close()

    def _write(self, data: bytes) -> bool:
        if not self.connected or self.writer is None or self.writer.is_closing():
            return False
        self.writer.write(data)
        return True

    async def _ping_loop(self) -> None:
        while self.connected:
            await asyncio.sleep(max(0.5, self.keepalive / 2))
            self._write(packet(PINGREQ << 4))

    async def _read_loop(self) -> None:
        try:
            while True:
                first, body = await read_packet(self.reader)
                kind = first >> 4
                if kind == PUBLISH:
                    qos, retain = (first >> 1) & 0x03, bool(first & 0x01)
                    topic, i = read_string(body, 0)
                    if qos:
                        (pid,) = struct.unpack_from("!H", body, i)
                        i += 2
                        self._write(packet(PUBACK << 4, struct.pack("!H", pid)))
                    if self.on_message is not None:
                        try:
                            self.on_message(topic, bytes(body[i:]), retain)
                        except Exception:  # noqa: BLE001 - a bad handler must not kill the client
                            pass
                elif kind == SUBACK and len(body) >= 2:
                    (pid,) = struct.unpack_from("!H", body, 0)
                    fut = self._acks.pop(pid, None)
                    if fut is not None and not fut.done():
                        fut.set_result(bytes(body[2:]))
        except asyncio.CancelledError:
            return
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ProtocolError, struct.error):
            pass
        was = self.connected
        self.connected = False
        if was and self.on_lost is not None:
            try:
                self.on_lost()
            except Exception:  # noqa: BLE001
                pass


# ---------- command line ----------

def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description="A small MQTT 3.1.1 broker on 127.0.0.1, for tests and the demo.")
    ap.add_argument("--port", type=int, default=1885, help="TCP port (0 picks a free one)")
    a = ap.parse_args(argv)

    async def run() -> None:
        broker = Broker(HOST, a.port)
        port = await broker.start()
        print(f"MQTT broker on mqtt://{HOST}:{port} (MQTT 3.1.1, QoS 0 and 1, retained, wills). Ctrl+C stops it.",
              flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await broker.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
