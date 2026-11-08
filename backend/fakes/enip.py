"""A simulated Allen-Bradley Logix controller for the EtherNet/IP connector.

This is not an EtherNet/IP device. It speaks no CIP. It holds a table of Logix tags
and serves them on 127.0.0.1 over a small line-of-JSON protocol, and RemoteLogix is
the client side of that protocol with the same read(*tags) / write((tag, value)) /
get_tag_info(tag) calls as pycomm3's LogixDriver, returning Tag-like results (tag,
value, type, error). The connector uses RemoteLogix when its config says fake: true,
so its polling, value rules, and confirmed writes run without any CIP traffic. The
wire protocol itself is exercised only against real hardware (cpppo, the usual
EtherNet/IP simulator, is GPL, so it is not used here).

    python -m backend.fakes.enip [--port 0] [--animate 0.5]   # prints the endpoint, runs until Ctrl+C

In tests, start() returns a handle: set() and set_error() change a tag's value or
make its reads fail, freeze_heartbeat() makes the controller look dead, animate()
makes Tank6_Level fill while Pump6 is off and drain while it runs, writes lists
every write received, write_delay_s holds write replies back (an acknowledgement
that never comes in time), and stop() / restart() take the controller off and back
on the same port.

Writing a tag checks that the value has the tag's type (bool for BOOL, int for the
integer types, float for REAL and LREAL), as a controller would reject a bad write.
Writing a command tag moves the matching status tag, the way a valve or a pump
starter answers a command: Valve6_Cmd sets Valve6_Open, Pump6_Cmd sets
Program:Main.Pump6_Run.
"""
from __future__ import annotations

import argparse
import json
import socket
import socketserver
import threading
import time
from typing import Any, NamedTuple, Optional

HOST = "127.0.0.1"
INTEGER_TYPES = ("SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT")
FLOAT_TYPES = ("REAL", "LREAL")


class Tag(NamedTuple):
    """The same fields as pycomm3.tag.Tag."""
    tag: str
    value: Any
    type: Optional[str] = None
    error: Optional[str] = None

    def __bool__(self) -> bool:
        return self.value is not None and self.error is None


def default_tags() -> dict:
    """name -> {"type", "value"} for the demo field: TANK_6, PUMP_6, VALVE_D_6, a well."""
    return {
        "Tank6_Level": {"type": "REAL", "value": 41.0},
        "Program:Main.Pump6_Run": {"type": "BOOL", "value": True},
        "Pump6_Flow": {"type": "REAL", "value": 118.0},
        "Pump6_Amps": {"type": "REAL", "value": 42.5},
        "Valve6_Open": {"type": "BOOL", "value": True},
        "Levels": {"type": "REAL", "value": [12.0, 25.0, 38.5, 50.0]},
        "Well_Flow": {"type": "DINT", "value": 90},
        "Valve6_Cmd": {"type": "BOOL", "value": True},
        "Pump6_Cmd": {"type": "BOOL", "value": True},
        "Well_Choke": {"type": "REAL", "value": 1.0},
        "Pump6_Speed": {"type": "DINT", "value": 0},
        "Heartbeat": {"type": "DINT", "value": 0},
    }


# A command tag and the status tag it moves.
FOLLOWS = {"Valve6_Cmd": "Valve6_Open", "Pump6_Cmd": "Program:Main.Pump6_Run"}


def _split(tag: str) -> tuple[str, Optional[int]]:
    """("Levels", 2) for "Levels[2]", ("Tank6_Level", None) otherwise."""
    if tag.endswith("]") and "[" in tag:
        base, _, idx = tag[:-1].partition("[")
        try:
            return base, int(idx)
        except ValueError:
            return tag, None
    return tag, None


def _type_ok(typ: str, value: Any) -> bool:
    if typ == "BOOL":
        return isinstance(value, bool)
    if typ in INTEGER_TYPES:
        return isinstance(value, int) and not isinstance(value, bool)
    if typ in FLOAT_TYPES:
        return isinstance(value, float)
    return False


class Controller:
    """The tag table. Thread-safe; every method is quick."""

    def __init__(self, tags: Optional[dict] = None) -> None:
        self._lock = threading.Lock()
        self.tags = {k: dict(v) for k, v in (tags or default_tags()).items()}
        self.errors: dict[str, str] = {}
        self.writes: list[tuple[str, Any]] = []
        self.reads = 0
        self.heartbeat_frozen = False
        self._t0 = time.monotonic()

    def set(self, tag: str, value: Any) -> None:
        with self._lock:
            base, idx = _split(tag)
            if idx is None:
                self.tags[base]["value"] = value
            else:
                self.tags[base]["value"][idx] = value

    def set_error(self, tag: str, error: Optional[str]) -> None:
        with self._lock:
            if error:
                self.errors[tag] = error
            else:
                self.errors.pop(tag, None)

    def _heartbeat(self) -> None:
        if not self.heartbeat_frozen and "Heartbeat" in self.tags:
            self.tags["Heartbeat"]["value"] = int((time.monotonic() - self._t0) * 20)

    def drift(self, tank: str, pump: str, delta: float) -> None:
        """Move `tank` up by delta while `pump` is off and down while it runs, within
        0..100. A stopped program (frozen heartbeat) updates nothing."""
        with self._lock:
            if self.heartbeat_frozen or tank not in self.tags or pump not in self.tags:
                return
            level = self.tags[tank]["value"]
            if isinstance(level, bool) or not isinstance(level, (int, float)):
                return
            level += -delta if self.tags[pump]["value"] else delta
            self.tags[tank]["value"] = float(min(100.0, max(0.0, level)))

    def read_one(self, tag: str) -> Tag:
        with self._lock:
            self._heartbeat()
            if tag in self.errors:
                return Tag(tag, None, None, self.errors[tag])
            base, idx = _split(tag)
            entry = self.tags.get(base)
            if entry is None:
                return Tag(tag, None, None, "Path destination unknown")
            value = entry["value"]
            if idx is not None:
                if not isinstance(value, list) or not 0 <= idx < len(value):
                    return Tag(tag, None, None, "Path segment error")
                value = value[idx]
            elif isinstance(value, list):
                value = list(value)
            return Tag(tag, value, entry["type"], None)

    def read(self, tags: list) -> list:
        with self._lock:
            self.reads += 1
        return [self.read_one(t) for t in tags]

    def info(self, tag: str) -> Optional[dict]:
        base, _ = _split(tag)
        with self._lock:
            entry = self.tags.get(base)
            if entry is None:
                return None
            dims = len(entry["value"]) if isinstance(entry["value"], list) else 0
            return {"tag_name": base, "data_type": entry["type"], "dimensions": [dims, 0, 0]}

    def write_one(self, tag: str, value: Any) -> Tag:
        with self._lock:
            self.writes.append((tag, value))
            base, idx = _split(tag)
            entry = self.tags.get(base)
            if entry is None:
                return Tag(tag, None, None, "Path destination unknown")
            if not _type_ok(entry["type"], value):
                return Tag(tag, None, entry["type"], f"value {value!r} does not fit a {entry['type']} tag")
            if idx is None:
                entry["value"] = value
            else:
                entry["value"][idx] = value
            follower = FOLLOWS.get(tag)
            if follower in self.tags:
                self.tags[follower]["value"] = value
            return Tag(tag, value, entry["type"], None)


# ---------- the line-of-JSON server ----------

class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        fake: FakeLogix = self.server.fake  # type: ignore[attr-defined]
        fake._track(self.connection, True)
        try:
            for line in self.rfile:
                try:
                    req = json.loads(line)
                except ValueError:
                    break
                op = req.get("op")
                if op == "read":
                    out = {"tags": [t._asdict() for t in fake.controller.read(list(req.get("tags") or []))]}
                elif op == "write":
                    if fake.write_delay_s:
                        time.sleep(fake.write_delay_s)
                    out = {"tags": [fake.controller.write_one(t, v)._asdict() for t, v in req.get("items") or []]}
                elif op == "info":
                    out = {"info": fake.controller.info(str(req.get("tag")))}
                else:
                    out = {"error": f"unknown op {op!r}"}
                self.wfile.write((json.dumps(out) + "\n").encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            fake._track(self.connection, False)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeLogix:
    """A running simulated controller. Use start() to make one."""

    def __init__(self, port: int = 0, tags: Optional[dict] = None) -> None:
        self.controller = Controller(tags)
        self.host = HOST
        self.port = port
        self.write_delay_s = 0.0
        self._conns: set = set()
        self._lock = threading.Lock()
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._animation: Optional[tuple] = None
        self._animator: Optional[threading.Thread] = None
        self._anim_stop = threading.Event()

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def writes(self) -> list:
        return self.controller.writes

    def set(self, tag: str, value: Any) -> None:
        self.controller.set(tag, value)

    def set_error(self, tag: str, error: Optional[str]) -> None:
        """Make reads of `tag` fail with this CIP error text; None clears it."""
        self.controller.set_error(tag, error)

    def freeze_heartbeat(self, frozen: bool = True) -> None:
        """A frozen heartbeat is a controller that answers but stopped scanning."""
        self.controller.heartbeat_frozen = frozen

    def animate(self, rate_pct_s: float = 0.5, every_s: float = 0.5, tank: str = "Tank6_Level",
                pump: str = "Program:Main.Pump6_Run") -> None:
        """Make `tank` fill at rate_pct_s percent a second while `pump` is off and drain
        while it runs, for a demo that moves. stop() ends it; restart() picks it up again."""
        self._animation = (rate_pct_s, every_s, tank, pump)
        self._start_animator()

    def _start_animator(self) -> None:
        if self._animation is None or (self._animator is not None and self._animator.is_alive()):
            return
        rate, every, tank, pump = self._animation
        self._anim_stop = stop = threading.Event()

        def run() -> None:
            while not stop.wait(every):
                self.controller.drift(tank, pump, rate * every)
        self._animator = threading.Thread(target=run, name=f"fake-enip-animate-{self.port}", daemon=True)
        self._animator.start()

    def _track(self, conn: socket.socket, add: bool) -> None:
        with self._lock:
            (self._conns.add if add else self._conns.discard)(conn)

    def serve(self) -> "FakeLogix":
        self._server = _Server((HOST, self.port), _Handler)
        self._server.fake = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05},
                                        name=f"fake-enip-{self.port}", daemon=True)
        self._thread.start()
        self._start_animator()
        return self

    def stop(self) -> None:
        """Stop listening and drop every open connection, like a controller losing power."""
        self._anim_stop.set()
        if self._animator is not None:
            self._animator.join(timeout=2)
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        with self._lock:
            conns, self._conns = list(self._conns), set()
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    def restart(self) -> "FakeLogix":
        """Listen again on the same port, with the same tag table."""
        self.stop()
        return self.serve()


def start(port: int = 0, tags: Optional[dict] = None) -> FakeLogix:
    """Start a simulated controller on 127.0.0.1 (a free port by default)."""
    return FakeLogix(port, tags).serve()


# ---------- the client the connector uses when config says fake: true ----------

class RemoteLogix:
    """The LogixDriver calls the connector uses, against a FakeLogix. Not thread-safe:
    one thread owns it, as with pycomm3."""

    def __init__(self, host: str, port: int, timeout_s: float = 5.0) -> None:
        self.host, self.port, self.timeout_s = host, int(port), float(timeout_s)
        self._sock: Optional[socket.socket] = None
        self._file = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def open(self) -> bool:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        sock.settimeout(self.timeout_s)
        self._sock, self._file = sock, sock.makefile("rb")
        return True

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def _call(self, req: dict) -> dict:
        if self._sock is None or self._file is None:
            raise ConnectionError("not connected")
        try:
            self._sock.sendall((json.dumps(req) + "\n").encode())
            line = self._file.readline()
        except OSError:
            self.close()
            raise
        if not line:
            self.close()
            raise ConnectionError("the simulated controller closed the connection")
        return json.loads(line)

    def read(self, *tags: str):
        out = [Tag(**t) for t in self._call({"op": "read", "tags": list(tags)})["tags"]]
        return out[0] if len(out) == 1 else out

    def write(self, *tags_values):
        if len(tags_values) == 2 and isinstance(tags_values[0], str):
            tags_values = (tuple(tags_values),)
        out = [Tag(**t) for t in self._call({"op": "write", "items": [list(tv) for tv in tags_values]})["tags"]]
        return out[0] if len(out) == 1 else out

    def get_tag_info(self, tag: str) -> Optional[dict]:
        return self._call({"op": "info", "tag": tag}).get("info")


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulated Logix controller (no CIP; 127.0.0.1 only)")
    ap.add_argument("--port", type=int, default=0, help="TCP port on 127.0.0.1 (default: a free one)")
    ap.add_argument("--animate", type=float, default=0.0, metavar="PCT_PER_S",
                    help="Tank6_Level fills at this rate while Pump6 is off and drains while it runs")
    args = ap.parse_args()
    fake = start(args.port)
    if args.animate > 0:
        fake.animate(rate_pct_s=args.animate)
    print(f"fake EtherNet/IP controller (simulated, no CIP) at {fake.endpoint}", flush=True)
    print("tags: " + ", ".join(sorted(fake.controller.tags)), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()


if __name__ == "__main__":
    main()
