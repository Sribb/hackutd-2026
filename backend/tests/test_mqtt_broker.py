"""The fake MQTT 3.1.1 broker (backend/fakes/mqtt_broker.py), driven by real paho-mqtt
2.1.0 clients on 127.0.0.1. Every test starts its own broker on a free port and stops it."""
import queue
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

paho = pytest.importorskip("paho.mqtt.client")

from backend.fakes import mqtt_broker as mb  # noqa: E402

TIMEOUT = 5.0


@pytest.fixture
def broker():
    b = mb.start(port=0)
    try:
        yield b
    finally:
        b.stop()


class Client:
    """A paho client that queues what it receives, for one test."""

    def __init__(self, port, client_id, keepalive=30, will=None, username=None, password=None,
                 clean_session=True):
        self.messages = queue.Queue()
        self.connack = queue.Queue()
        self.subacks = queue.Queue()
        self.c = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=client_id,
                             protocol=paho.MQTTv311, clean_session=clean_session)
        if will is not None:
            topic, payload, qos, retain = will
            self.c.will_set(topic, payload, qos=qos, retain=retain)
        if username is not None:
            self.c.username_pw_set(username, password)
        self.c.on_connect = lambda c, u, flags, rc, props: self.connack.put(rc)
        self.c.on_message = lambda c, u, msg: self.messages.put(msg)
        self.c.on_subscribe = lambda c, u, mid, codes, props: self.subacks.put(codes)
        self.c.connect("127.0.0.1", port, keepalive=keepalive)
        self.c.loop_start()
        self.rc = self.connack.get(timeout=TIMEOUT)

    def subscribe(self, filt, qos=0):
        self.c.subscribe(filt, qos)
        return self.subacks.get(timeout=TIMEOUT)

    def next(self, timeout=TIMEOUT):
        return self.messages.get(timeout=timeout)

    def drain_until(self, topic, timeout=TIMEOUT):
        """Every message up to and including the first one on topic."""
        got = []
        deadline = time.monotonic() + timeout
        while True:
            msg = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            got.append(msg)
            if msg.topic == topic:
                return got

    def close(self):
        try:
            self.c.disconnect()
        finally:
            self.c.loop_stop()

    def kill(self):
        """Stop the network thread and close the socket without DISCONNECT, as a crash would."""
        self.c.loop_stop()
        sock = self.c.socket()
        if sock is not None:
            sock.close()


def ok(client):
    assert not client.rc.is_failure, client.rc
    return client


def wait_for(fn, timeout=TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


# ---------- topic rules ----------

def test_topic_matching_and_filters():
    m = mb.topic_matches
    assert m("spBv1.0/+/NDATA/+", "spBv1.0/field/NDATA/edge1")
    assert not m("spBv1.0/+/NDATA/+", "spBv1.0/field/NDATA/edge1/dev")
    assert m("a/#", "a") and m("a/#", "a/b/c") and m("#", "a/b")
    assert m("a/+/c", "a//c") and not m("a/+", "a/b/c")
    assert not m("#", "$SYS/x") and not m("+/x", "$SYS/x") and m("$SYS/#", "$SYS/x")
    assert mb.valid_filter("a/+/#") and not mb.valid_filter("a/#/b") and not mb.valid_filter("a/b+")
    assert not mb.valid_topic("a/+") and not mb.valid_topic("") and mb.valid_topic("a/b")


def test_listens_on_loopback_only():
    with pytest.raises(ValueError):
        mb.start(host="0.0.0.0")


# ---------- paho clients ----------

def test_pub_sub_and_wildcards(broker):
    assert broker.port > 0
    sub = ok(Client(broker.port, "sub"))
    pub = ok(Client(broker.port, "pub"))
    try:
        assert [c.value for c in sub.subscribe("spBv1.0/field/+/edge1")] == [0]
        sub.subscribe("plant/#")
        pub.c.publish("spBv1.0/field/NDATA/edge1", b"n1")
        pub.c.publish("spBv1.0/field/NDATA/edge2", b"skip")
        pub.c.publish("plant/tank/3/level", b"t3")
        pub.c.publish("plant", b"root")
        pub.c.publish("done", b"")          # not subscribed: must not arrive
        pub.c.publish("plant/end", b"end")
        got = [(m.topic, m.payload) for m in sub.drain_until("plant/end")]
        assert got == [("spBv1.0/field/NDATA/edge1", b"n1"), ("plant/tank/3/level", b"t3"),
                       ("plant", b"root"), ("plant/end", b"end")]
        # Unsubscribe: plant/# stops, the other filter keeps working.
        done = queue.Queue()
        sub.c.on_unsubscribe = lambda c, u, mid, codes, props: done.put(mid)
        sub.c.unsubscribe("plant/#")
        done.get(timeout=TIMEOUT)
        pub.c.publish("plant/tank/3/level", b"gone")
        pub.c.publish("spBv1.0/field/NDATA/edge1", b"n2")
        assert sub.next().payload == b"n2"
        assert sub.messages.empty()
    finally:
        sub.close()
        pub.close()


def test_qos1_puback_and_downgrade(broker):
    s0 = ok(Client(broker.port, "s0"))
    s1 = ok(Client(broker.port, "s1"))
    pub = ok(Client(broker.port, "pub"))
    try:
        assert s0.subscribe("cmd/#", qos=0)[0].value == 0
        assert s1.subscribe("cmd/#", qos=1)[0].value == 1
        info = pub.c.publish("cmd/valve", b"CLOSED", qos=1)
        info.wait_for_publish(timeout=TIMEOUT)
        assert info.is_published()            # the broker sent PUBACK
        assert s0.next().qos == 0             # min(1, 0)
        assert s1.next().qos == 1             # min(1, 1)
        pub.c.publish("cmd/valve", b"x", qos=0)
        assert s1.next().qos == 0             # min(0, 1)
        entry = [e for e in broker.log() if e["payload"] == b"CLOSED"][0]
        assert entry["client_id"] == "pub" and entry["qos"] == 1
    finally:
        for c in (s0, s1, pub):
            c.close()


def test_retained_delivery_and_clearing(broker):
    pub = ok(Client(broker.port, "pub"))
    early = ok(Client(broker.port, "early"))
    try:
        early.subscribe("state/#")
        info = pub.c.publish("state/edge1", b"ONLINE", qos=1, retain=True)
        info.wait_for_publish(timeout=TIMEOUT)
        # A subscription that already existed gets it with retain cleared.
        msg = early.next()
        assert msg.payload == b"ONLINE" and not msg.retain
        assert broker.retained() == {"state/edge1": b"ONLINE"}

        late = ok(Client(broker.port, "late"))
        late.subscribe("state/+", qos=1)
        msg = late.next()
        assert (msg.topic, msg.payload, msg.retain, msg.qos) == ("state/edge1", b"ONLINE", True, 1)
        late.close()

        # An empty retained payload clears it.
        pub.c.publish("state/edge1", b"", qos=1, retain=True).wait_for_publish(timeout=TIMEOUT)
        assert wait_for(lambda: broker.retained() == {})
        after = ok(Client(broker.port, "after"))
        after.subscribe("state/#")
        after.subscribe("marker")
        pub.c.publish("marker", b"m")
        assert [m.topic for m in after.drain_until("marker")] == ["marker"]
        after.close()
    finally:
        pub.close()
        early.close()


def test_will_on_abrupt_socket_close(broker):
    watcher = ok(Client(broker.port, "watcher"))
    edge = ok(Client(broker.port, "edge1", will=("state/edge1", b"OFFLINE", 1, True)))
    try:
        watcher.subscribe("state/#", qos=1)
        edge.kill()
        msg = watcher.next()
        assert (msg.topic, msg.payload, msg.qos) == ("state/edge1", b"OFFLINE", 1)
        assert broker.retained() == {"state/edge1": b"OFFLINE"}   # will retain honoured
        assert any(e["will"] and e["client_id"] == "edge1" for e in broker.log())
    finally:
        watcher.close()


def test_clean_disconnect_drops_the_will(broker):
    watcher = ok(Client(broker.port, "watcher"))
    edge = ok(Client(broker.port, "edge1", will=("state/edge1", b"OFFLINE", 0, False)))
    try:
        watcher.subscribe("state/#")
        watcher.subscribe("marker")
        edge.close()
        assert wait_for(lambda: "edge1" not in broker.clients())
        broker.publish("marker", "m")
        assert [m.topic for m in watcher.drain_until("marker")] == ["marker"]
    finally:
        watcher.close()


def test_will_on_keepalive_timeout(broker):
    watcher = ok(Client(broker.port, "watcher"))
    edge = ok(Client(broker.port, "edge1", keepalive=1, will=("state/edge1", b"DEAD", 0, False)))
    try:
        watcher.subscribe("state/#")
        edge.c.loop_stop()                   # socket stays open, but no more PINGREQs
        t0 = time.monotonic()
        msg = watcher.next(timeout=TIMEOUT)
        assert msg.payload == b"DEAD"
        assert 1.0 <= time.monotonic() - t0 + 0.2 < TIMEOUT   # about 1.5 x keepalive
        assert "edge1" not in broker.clients()
    finally:
        edge.kill()
        watcher.close()


def test_pingreq_keeps_a_quiet_client_alive(broker):
    watcher = ok(Client(broker.port, "watcher"))
    edge = ok(Client(broker.port, "edge1", keepalive=1, will=("state/edge1", b"DEAD", 0, False)))
    try:
        watcher.subscribe("state/#")
        time.sleep(2.5)                      # well past 1.5 s: paho's pings keep it up
        assert "edge1" in broker.clients()
        assert watcher.messages.empty()
    finally:
        edge.close()
        watcher.close()


def test_username_password_check():
    b = mb.start(port=0, username="freeboard", password="pw-for-test")
    try:
        good = Client(b.port, "good", username="freeboard", password="pw-for-test")
        assert not good.rc.is_failure
        good.close()
        bad = Client(b.port, "bad", username="freeboard", password="wrong")
        assert bad.rc.is_failure and bad.rc.value in (4, 134, 135)
        bad.c.loop_stop()
        none = Client(b.port, "none")
        assert none.rc.is_failure
        none.c.loop_stop()
    finally:
        b.stop()


def test_client_id_takeover_ends_the_old_connection(broker):
    watcher = ok(Client(broker.port, "watcher"))
    first = ok(Client(broker.port, "edge1", will=("state/edge1", b"OLD", 0, False)))
    first.c.reconnect_delay_set(60, 60)      # don't let paho fight back during the test
    try:
        watcher.subscribe("state/#")
        second = ok(Client(broker.port, "edge1"))
        assert watcher.next().payload == b"OLD"
        assert list(broker.clients()).count("edge1") == 1
        second.close()
    finally:
        first.kill()
        watcher.close()


def test_stop_returns_and_frees_the_port():
    b = mb.start(port=0)
    c = ok(Client(b.port, "c"))
    port = b.port
    t0 = time.monotonic()
    b.stop()
    b.stop()                                  # twice is fine
    assert time.monotonic() - t0 < 2
    c.c.loop_stop()
    with socket.socket() as s:
        # SO_REUSEADDR: the client's closed connection can sit in TIME_WAIT (macOS refuses a
        # plain bind then); this still fails if anything is listening on the port.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))          # the listener is gone


# ---------- raw protocol edges ----------

def test_raw_connect_without_client_id_and_bad_packets(broker):
    def connect(flags, client_id=b""):
        s = socket.create_connection(("127.0.0.1", broker.port), timeout=TIMEOUT)
        body = mb.mqtt_string("MQTT") + bytes([4, flags]) + struct.pack("!H", 0) + struct.pack("!H", len(client_id)) + client_id
        s.sendall(mb.packet(mb.CONNECT << 4, body))
        return s, s.recv(4)

    s, ack = connect(0x02)                    # empty id with clean session: one is assigned
    assert ack == b"\x20\x02\x00\x00"
    s.close()
    s, ack = connect(0x00)                    # empty id without clean session: refused
    assert ack == b"\x20\x02\x00\x02"
    s.close()
    s, ack = connect(0x02, b"q2")
    assert ack[3] == 0
    s.sendall(mb.publish_packet("a", b"x", qos=2, pid=1))         # QoS 2: connection closed
    s.settimeout(TIMEOUT)
    assert s.recv(10) == b""
    s.close()


# ---------- the client the fake devices use ----------

def test_minimal_client_against_paho(broker):
    watcher = ok(Client(broker.port, "watcher"))
    got = queue.Queue()
    loop = mb.LoopThread("test-client")
    try:
        watcher.subscribe("dev/#")
        dev = mb.MqttClient("127.0.0.1", broker.port, "dev1", keepalive=5,
                            will=("dev/state", b"LOST", 0, False),
                            on_message=lambda t, p, r: got.put((t, p, r)))
        loop.run(dev.connect())
        assert loop.run(dev.subscribe([("cmd/dev1", 1)])) == b"\x01"
        watcher.c.publish("cmd/dev1", b"ON", qos=1).wait_for_publish(timeout=TIMEOUT)
        assert got.get(timeout=TIMEOUT) == ("cmd/dev1", b"ON", False)
        loop.call(dev.publish, "dev/flow", b"12.5")
        assert watcher.next().payload == b"12.5"
        loop.call(dev.drop)
        assert watcher.next().payload == b"LOST"
    finally:
        loop.stop()
        watcher.close()


def test_command_line_prints_the_port():
    root = Path(__file__).resolve().parents[2]
    p = subprocess.Popen([sys.executable, "-m", "backend.fakes.mqtt_broker", "--port", "0"], cwd=root,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = p.stdout.readline()
        assert "mqtt://127.0.0.1:" in line
        port = int(line.split("mqtt://127.0.0.1:")[1].split()[0])
        c = ok(Client(port, "cli"))
        c.close()
    finally:
        p.kill()
        p.wait(timeout=TIMEOUT)
