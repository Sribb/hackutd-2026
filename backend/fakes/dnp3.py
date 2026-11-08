"""Fake DNP3 outstation for tests and the demo, on 127.0.0.1 only.

It speaks the JSON line protocol that LineMaster in backend/app/connectors/dnp3.py
uses, not DNP3 framing: one JSON object per line, each request with a sequence
number its answer repeats. It keeps the DNP3 model: AI and BI points with flags,
events in classes 1-3 (binary inputs class 1, analog inputs class 2) stamped with
the time they happened, unsolicited responses when the master enables them, and
CROB and AO controls that must be selected before they are operated.

    python -m backend.fakes.dnp3 --port 20000

    f = start()                      # free port; f.port, f.endpoint
    f.set("AI:0", 42.0)              # new static value and a class 2 event
    f.set_flags("AI:0", "COMM_LOST") # same value, bad flags (ONLINE stays unless online=False)
    f.add_event("AI:0", 10.0, age_s=900)   # a buffered old event; the static value is unchanged
    f.writes                         # every operate that succeeded
    f.stop()

Default points match the demo spec: AI:0 TANK_6 level, AI:1 PUMP_6 flow, BI:0
PUMP_6 running, BI:1 VALVE_D_6 position; CROB:0 drives BI:1 (the valve), CROB:1
drives BI:0 (the pump), AO:0 is a well choke.
"""
from __future__ import annotations

import argparse
import json
import socket
import socketserver
import threading
import time
from typing import Any, Optional

ONLINE, RESTART, COMM_LOST, REMOTE_FORCED, LOCAL_FORCED = 0x01, 0x02, 0x04, 0x08, 0x10
FLAG_BITS = {
    "BI": {"ONLINE": ONLINE, "RESTART": RESTART, "COMM_LOST": COMM_LOST, "REMOTE_FORCED": REMOTE_FORCED,
           "LOCAL_FORCED": LOCAL_FORCED, "CHATTER_FILTER": 0x20},
    "AI": {"ONLINE": ONLINE, "RESTART": RESTART, "COMM_LOST": COMM_LOST, "REMOTE_FORCED": REMOTE_FORCED,
           "LOCAL_FORCED": LOCAL_FORCED, "OVER_RANGE": 0x20, "REFERENCE_ERR": 0x40},
}
DEFAULT_POINTS = {"AI:0": 50.0, "AI:1": 120.0, "BI:0": True, "BI:1": True}
DEFAULT_LINKS = {"CROB:0": "BI:1", "CROB:1": "BI:0", "AO:0": None}
DEFAULT_CLASSES = {"BI": 1, "AI": 2}
ON_CODES = ("LATCH_ON", "PULSE_ON_CLOSE")
OFF_CODES = ("LATCH_OFF", "PULSE_ON_TRIP")
SUCCESS, NO_SELECT, FORMAT_ERROR, NOT_SUPPORTED = 0, 2, 3, 4


def _split(addr: str) -> tuple[str, int]:
    kind, _, index = addr.partition(":")
    return kind.upper(), int(index)


class _Conn:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.wlock = threading.Lock()
        self.unsolicited: set = set()

    def send(self, msg: dict) -> None:
        data = (json.dumps(msg) + "\n").encode()
        with self.wlock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass


class FakeOutstation:
    """A running fake outstation. Change its points with set(), set_flags() and
    add_event(); read what reached it in writes, selects, direct_operates and
    requests. Knobs: silent (stop answering, keep the connection), select_tamper and
    operate_tamper (dicts merged into the echo), select_status, operate_delay_s
    (answer an operate late, after acting on it), and iin (IIN bit names, such as
    "DEVICE_RESTART", sent with every poll answer)."""

    def __init__(self, port: int = 0, points: Optional[dict] = None, links: Optional[dict] = None,
                 select_timeout_s: float = 10.0) -> None:
        self.lock = threading.RLock()
        self.points: dict[str, dict] = {a: {"value": v, "flags": ONLINE}
                                        for a, v in (points or DEFAULT_POINTS).items()}
        self.links = dict(DEFAULT_LINKS if links is None else links)
        self.select_timeout_s = select_timeout_s
        self.events: list[dict] = []
        self.writes: list[dict] = []
        self.selects: list[dict] = []
        self.direct_operates: list[dict] = []
        self.requests: list[str] = []
        self.silent = False
        self.select_tamper: Optional[dict] = None
        self.operate_tamper: Optional[dict] = None
        self.select_status = SUCCESS
        self.operate_delay_s = 0.0
        self.iin: set = set()
        self._selected: dict[tuple, tuple] = {}
        self._conns: set[_Conn] = set()
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                conn = _Conn(self.request)
                with outer.lock:
                    outer._conns.add(conn)
                try:
                    for line in self.rfile:
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(msg, dict):
                            outer._handle(conn, msg)
                except OSError:
                    pass
                finally:
                    with outer.lock:
                        outer._conns.discard(conn)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", port), Handler)
        self.host, self.port = self.server.server_address[:2]
        self.endpoint = f"{self.host}:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                        name="fake-dnp3", daemon=True)
        self._thread.start()

    # ---------- changing the field ----------
    def set(self, addr: str, value: Any = None, *, flags: Optional[int] = None, event: bool = True) -> None:
        """Set a point's static value (and flags), and report the change as an event."""
        with self.lock:
            pt = self.points.setdefault(addr, {"value": value, "flags": ONLINE})
            if value is not None:
                pt["value"] = value
            if flags is not None:
                pt["flags"] = flags
            if event:
                self._event(addr, pt["value"], pt["flags"], time.time())

    def set_flags(self, addr: str, *names: str, online: bool = True) -> None:
        """Set a point's flags by name, such as "COMM_LOST" or "OVER_RANGE". ONLINE is
        added unless online=False. No names and online=True means healthy again."""
        kind, _ = _split(addr)
        bits = ONLINE if online else 0
        for n in names:
            bits |= FLAG_BITS[kind][n.upper()]
        self.set(addr, flags=bits)

    def add_event(self, addr: str, value: Any, *, age_s: float, flags: int = ONLINE) -> None:
        """Report an event that happened age_s ago, as an outstation does with events it
        buffered while the link was down. The static value doesn't change."""
        with self.lock:
            self._event(addr, value, flags, time.time() - age_s)

    def _event(self, addr: str, value: Any, flags: int, t: float) -> None:
        kind, index = _split(addr)
        ev = {"type": kind, "index": index, "value": value, "flags": flags, "time": t,
              "class": DEFAULT_CLASSES.get(kind, 1)}
        pushed = False
        for conn in list(self._conns):
            if ev["class"] in conn.unsolicited:
                conn.send({"unsolicited": True, "points": [_wire(ev)], "iin": []})
                pushed = True
        if not pushed:
            self.events.append(ev)

    # ---------- requests ----------
    def _handle(self, conn: _Conn, msg: dict) -> None:
        fn, seq = msg.get("fn"), msg.get("seq")
        with self.lock:
            self.requests.append(str(fn))
            if self.silent:
                return
            if fn == "integrity":
                evs, self.events = self.events, []
                statics = []
                for addr, pt in self.points.items():
                    kind, index = _split(addr)
                    statics.append({"type": kind, "index": index, "value": pt["value"], "flags": pt["flags"],
                                    "time": None})
                conn.send({"seq": seq, "points": [_wire(e) for e in evs] + statics,
                           "iin": sorted(self.iin)})
            elif fn == "events":
                classes = set(msg.get("classes") or [])
                out = [e for e in self.events if e["class"] in classes]
                self.events = [e for e in self.events if e["class"] not in classes]
                conn.send({"seq": seq, "points": [_wire(e) for e in out], "iin": sorted(self.iin)})
            elif fn == "unsolicited":
                conn.unsolicited = set(msg.get("classes") or []) if msg.get("enable") else set()
                conn.send({"seq": seq, "ok": True})
            elif fn == "select":
                conn.send({"seq": seq, "echo": self._select(msg.get("control") or {})})
            elif fn == "operate":
                echo, delay = self._operate(msg.get("control") or {}), self.operate_delay_s
                if delay > 0:
                    threading.Timer(delay, conn.send, args=({"seq": seq, "echo": echo},)).start()
                else:
                    conn.send({"seq": seq, "echo": echo})
            elif fn == "direct_operate":
                self.direct_operates.append(dict(msg.get("control") or {}))
                conn.send({"seq": seq, "echo": {**(msg.get("control") or {}), "status": NOT_SUPPORTED}})

    def _key(self, ctl: dict) -> Optional[tuple]:
        kind, index = str(ctl.get("kind", "")).upper(), ctl.get("index")
        if f"{kind}:{index}" not in self.links:
            return None
        return kind, index

    def _select(self, ctl: dict) -> dict:
        self.selects.append(dict(ctl))
        key = self._key(ctl)
        status = self.select_status if key is not None else NOT_SUPPORTED
        if status == SUCCESS:
            self._selected[key] = (json.dumps(ctl, sort_keys=True), time.monotonic())
        echo = {**ctl, "status": status}
        if self.select_tamper:
            echo.update(self.select_tamper)
        return echo

    def _operate(self, ctl: dict) -> dict:
        key = self._key(ctl)
        sel = self._selected.pop(key, None) if key is not None else None
        if key is None:
            status = NOT_SUPPORTED
        elif (sel is None or sel[0] != json.dumps(ctl, sort_keys=True)
              or time.monotonic() - sel[1] > self.select_timeout_s):
            status = NO_SELECT
        else:
            status = SUCCESS
            self.writes.append(dict(ctl))
            target = self.links.get(f"{key[0]}:{key[1]}")
            if key[0] == "CROB" and target:
                code = ctl.get("code")
                if code in ON_CODES:
                    self.set(target, True)
                elif code in OFF_CODES:
                    self.set(target, False)
            elif key[0] == "AO" and target:
                self.set(target, ctl.get("value"))
        echo = {**ctl, "status": status}
        if self.operate_tamper:
            echo.update(self.operate_tamper)
        return echo

    # ---------- lifetime ----------
    def stop(self) -> None:
        """Stop listening and drop every connection. Returns within about a second."""
        self.server.shutdown()
        self.server.server_close()
        with self.lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.sock.close()
            except OSError:
                pass


def _wire(ev: dict) -> dict:
    return {k: ev[k] for k in ("type", "index", "value", "flags", "time")}


def start(port: int = 0, **kwargs: Any) -> FakeOutstation:
    """Start a fake outstation on 127.0.0.1 (a free port by default) and return it."""
    return FakeOutstation(port=port, **kwargs)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake DNP3 outstation (JSON lines, not DNP3 framing), 127.0.0.1 only")
    ap.add_argument("--port", type=int, default=20000)
    args = ap.parse_args()
    f = start(port=args.port)
    print(f"fake DNP3 outstation listening on {f.endpoint} (JSON line protocol, not DNP3 framing); "
          f"Ctrl-C to stop", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        f.stop()


if __name__ == "__main__":
    main()
