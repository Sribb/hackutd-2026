"""DNP3 master connector (kind "dnp3"): polls and unsolicited reports in, select-before-operate out.

What works today, honestly: there is no maintained DNP3 library for Python on
Windows (opendnp3 is end of life, dnp3-python builds only on Linux). So this module
drives a small master interface (Master below) and ships one implementation of it,
LineMaster, which talks to the fake outstation in backend/fakes/dnp3.py over a JSON
line protocol on 127.0.0.1. That protocol carries the DNP3 model (points with flags
and timestamps, event classes, unsolicited responses, select and operate with
echoes), not DNP3 framing. A config with backend "dnp3" (the default) shows the
connector unavailable with the reason, since no DNP3 library imports here; a real
library needs an adapter that implements Master.

    {"id": "CONN_DNP3", "kind": "dnp3",
     "config": {"backend": "fake", "host": "127.0.0.1", "port": 20000,
                "integrity_s": 60, "event_s": 5, "timeout_s": 5, "select_timeout_s": 5,
                "unsolicited": true, "crob_style": "latch", "choke_full_scale": 1.0},
     "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "AI:0"},
                 {"asset_id": "VALVE_D_6", "signal": "position", "address": "BI:1"}],
     "commands": [{"asset_id": "VALVE_D_6", "command": "SET_VALVE", "address": "CROB:0"},
                  {"asset_id": "WELL_1", "command": "CHOKE_WELL", "address": "AO:0"}]}

Rules this module keeps:

- Addresses. A signal reads AI:n (analog input; level, flow, pressure, amps) or BI:n
  (binary input; running, position). SET_VALVE and SET_PUMP write CROB:n, CHOKE_WELL
  writes AO:n. Only declared points are used; anything else from the outstation is
  ignored.
- Polling. A class 0 integrity poll on connect and every integrity_s (default 60),
  and a class 1/2/3 event poll every event_s (default 5). Unsolicited responses are
  enabled and accepted when config unsolicited is true (the default), and turned off
  otherwise. An outstation that reports a restart or an event buffer overflow in its
  IIN bits gets an integrity poll right away.
- Values. A point is used only when its ONLINE flag is set and none of RESTART,
  COMM_LOST, REMOTE_FORCED, LOCAL_FORCED, CHATTER_FILTER (binary), OVER_RANGE or
  REFERENCE_ERR (analog) is. Otherwise, or when the signal_map rule rejects the
  value, there is no reading and self.quality says why. A timestamped event older
  than max_age_s (default 60 s), or that far in the future, is history: never a
  reading, and it doesn't replace a newer held value (self.quality says stale only
  when nothing is held). An event older than the last event used for that point is
  skipped too, so events an outstation buffered during an outage never replace a
  newer value. DNP3 reports by exception, so every answered poll (integrity or
  event) also confirms the values it didn't change: the held good values go to
  read() once per answered poll, and never again until the next answer. Once the
  outstation goes quiet nothing is read, not even while the unanswered poll waits
  out timeout_s. Keep event_s well under sensors.stale_after_min at the live speed.
  A declared point missing from an integrity poll is dropped (quality null).
- Link loss. A poll with no answer within timeout_s, or a dropped connection, turns
  status() unavailable with the reason, marks every declared signal comm_lost, drops
  every held value (so no reading until the outstation answers again), and reconnects
  with Backoff. Nothing held from before the loss is read again.
- Commands. Only send() writes, and the live layer calls it only after a person
  confirms. Every command is select-before-operate: select, check that the echo
  matches what was sent and says SUCCESS, operate within select_timeout_s (default 5),
  and check the operate echo. Any mismatch, refusal or timeout returns not ok and
  nothing is retried; an operate that left but got no answer returns
  uncertain_result(), and an operate echo that doesn't match returns the same,
  with what didn't match added after the standard detail. Direct operate is never used. The whole exchange holds the
  channel, so polls wait, and it never takes longer than timeout_s.
"""
from __future__ import annotations

import json
import math
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..fieldmap import FieldMap
from . import signalmap as sm
from .base import Connector, Reading

# ---------- the DNP3 model ----------

ONLINE, RESTART, COMM_LOST, REMOTE_FORCED, LOCAL_FORCED = 0x01, 0x02, 0x04, 0x08, 0x10
CHATTER_FILTER = 0x20          # binary inputs
OVER_RANGE, REFERENCE_ERR = 0x20, 0x40   # analog inputs
BAD_FLAGS = {
    "BI": (("RESTART", RESTART), ("COMM_LOST", COMM_LOST), ("REMOTE_FORCED", REMOTE_FORCED),
           ("LOCAL_FORCED", LOCAL_FORCED), ("CHATTER_FILTER", CHATTER_FILTER)),
    "AI": (("RESTART", RESTART), ("COMM_LOST", COMM_LOST), ("REMOTE_FORCED", REMOTE_FORCED),
           ("LOCAL_FORCED", LOCAL_FORCED), ("OVER_RANGE", OVER_RANGE), ("REFERENCE_ERR", REFERENCE_ERR)),
}
# Control status codes (IEEE 1815 table 11-1), for messages.
STATUS = {0: "SUCCESS", 1: "TIMEOUT", 2: "NO_SELECT", 3: "FORMAT_ERROR", 4: "NOT_SUPPORTED",
          5: "ALREADY_ACTIVE", 6: "HARDWARE_ERROR", 7: "LOCAL", 8: "TOO_MANY_OPS", 9: "NOT_AUTHORIZED",
          10: "AUTOMATION_INHIBIT", 11: "PROCESSING_LIMITED", 12: "OUT_OF_RANGE", 126: "NON_PARTICIPATING",
          127: "UNDEFINED"}
CROB_CODES = {"latch": ("LATCH_ON", "LATCH_OFF"), "pulse": ("PULSE_ON_CLOSE", "PULSE_ON_TRIP")}
RESTART_IIN = frozenset({"DEVICE_RESTART", "EVENT_BUFFER_OVERFLOW"})
EVENT_CLASSES = (1, 2, 3)

ADDRESS = re.compile(r"^\s*(AI|BI|AO|CROB)\s*:\s*(\d{1,5})\s*$", re.IGNORECASE)
SIGNAL_POINT = {"level_pct": "AI", "flow_bbl_hr": "AI", "pressure_psi": "AI", "motor_amps": "AI",
                "running": "BI", "position": "BI"}
COMMAND_POINT = {"SET_VALVE": "CROB", "SET_PUMP": "CROB", "CHOKE_WELL": "AO"}
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass
class Point:
    """One reported point: kind "AI" or "BI", index, value, DNP3 flags, and the device
    timestamp in epoch seconds (None for static values, which carry no time)."""
    kind: str
    index: int
    value: Any
    flags: int
    time: Optional[float] = None


@dataclass
class Response:
    points: list = field(default_factory=list)
    iin: frozenset = frozenset()


@dataclass(frozen=True)
class Control:
    """A control request: CROB with an operation code, or AO with a value."""
    kind: str
    index: int
    code: Optional[str] = None
    value: Optional[float] = None

    def wire(self) -> dict:
        out: dict = {"kind": self.kind, "index": self.index}
        if self.kind == "CROB":
            out["code"] = self.code
        else:
            out["value"] = self.value
        return out


class NotSent(ConnectionError):
    """The request never left: nothing reached the outstation."""


class Master:
    """What the connector needs from a DNP3 master. Every call may raise TimeoutError
    (sent, no answer within timeout), NotSent (never left), or ConnectionError (the
    link dropped). select() and operate() return the outstation's echo as
    {"kind", "index", "code" or "value", "status"}."""

    def connect(self, timeout: float) -> None: raise NotImplementedError
    def integrity_poll(self, timeout: float) -> Response: raise NotImplementedError
    def event_poll(self, classes: tuple, timeout: float) -> Response: raise NotImplementedError
    def on_unsolicited(self, callback: Callable[[Response], None]) -> None: raise NotImplementedError
    def enable_unsolicited(self, enable: bool, classes: tuple, timeout: float) -> None: raise NotImplementedError
    def select(self, control: Control, timeout: float) -> dict: raise NotImplementedError
    def operate(self, control: Control, timeout: float) -> dict: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError


class LineMaster(Master):
    """Master for the fake outstation: one JSON object per line over TCP, each request
    with a sequence number its response repeats. A reader thread routes responses to
    their waiters and unsolicited responses to the callback."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self._sock: Optional[socket.socket] = None
        self._wlock = threading.Lock()
        self._waiters: dict[int, list] = {}
        self._seq = 0
        self._callback: Optional[Callable[[Response], None]] = None
        self._dead = threading.Event()

    def connect(self, timeout: float) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(None)
        self._sock = sock
        threading.Thread(target=self._reader, args=(sock,), name="dnp3-line-reader", daemon=True).start()

    def _reader(self, sock: socket.socket) -> None:
        try:
            with sock.makefile("rb") as f:
                for line in f:
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("unsolicited"):
                        cb = self._callback
                        if cb is not None:
                            try:
                                cb(_response(msg))
                            except Exception:  # noqa: BLE001
                                pass
                        continue
                    with self._wlock:
                        slot = self._waiters.get(msg.get("seq"))
                    if slot is not None:
                        slot[1] = msg
                        slot[0].set()
        except (OSError, ValueError):
            pass
        finally:
            self._dead.set()
            with self._wlock:
                waiters = list(self._waiters.values())
            for slot in waiters:
                slot[0].set()

    def _request(self, fn: str, timeout: float, **args: Any) -> dict:
        sock = self._sock
        if sock is None or self._dead.is_set():
            raise NotSent("not connected")
        with self._wlock:
            self._seq += 1
            seq = self._seq
            slot = [threading.Event(), None]
            self._waiters[seq] = slot
            try:
                sock.sendall((json.dumps({"seq": seq, "fn": fn, **args}) + "\n").encode())
            except OSError as e:
                self._waiters.pop(seq, None)
                raise NotSent(f"send failed: {type(e).__name__}") from None
        try:
            if not slot[0].wait(max(0.0, timeout)):
                raise TimeoutError(f"no answer to {fn} within {timeout:g} s")
            if slot[1] is None:
                raise ConnectionError(f"the link dropped while waiting for the {fn} answer")
            return slot[1]
        finally:
            with self._wlock:
                self._waiters.pop(seq, None)

    def integrity_poll(self, timeout: float) -> Response:
        return _response(self._request("integrity", timeout))

    def event_poll(self, classes: tuple, timeout: float) -> Response:
        return _response(self._request("events", timeout, classes=list(classes)))

    def on_unsolicited(self, callback: Callable[[Response], None]) -> None:
        self._callback = callback

    def enable_unsolicited(self, enable: bool, classes: tuple, timeout: float) -> None:
        self._request("unsolicited", timeout, enable=bool(enable), classes=list(classes))

    def select(self, control: Control, timeout: float) -> dict:
        return self._request("select", timeout, control=control.wire()).get("echo") or {}

    def operate(self, control: Control, timeout: float) -> dict:
        return self._request("operate", timeout, control=control.wire()).get("echo") or {}

    def close(self) -> None:
        self._callback = None
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def _response(msg: dict) -> Response:
    points = []
    for p in msg.get("points") or []:
        if not isinstance(p, dict):
            continue
        kind, index = str(p.get("type", "")).upper(), p.get("index")
        if kind not in BAD_FLAGS or isinstance(index, bool) or not isinstance(index, int):
            continue
        t = p.get("time")
        t = float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None
        flags = p.get("flags")
        points.append(Point(kind, index, p.get("value"),
                            flags if isinstance(flags, int) and not isinstance(flags, bool) else 0, t))
    return Response(points, frozenset(str(x) for x in msg.get("iin") or []))


def flag_quality(kind: str, flags: int) -> Optional[sm.Mapped]:
    """None when the flags allow the value, else a Mapped with value None saying which flags."""
    names = [] if flags & ONLINE else ["not ONLINE"]
    names += [n for n, bit in BAD_FLAGS.get(kind, ()) if flags & bit]
    if not names:
        return None
    if "COMM_LOST" in names:
        q = sm.COMM_LOST
    elif "OVER_RANGE" in names:
        q = sm.OUT_OF_RANGE
    elif set(names) <= {"REMOTE_FORCED", "LOCAL_FORCED", "CHATTER_FILTER"}:
        q = sm.UNCERTAIN
    else:
        q = sm.BAD
    return sm.Mapped(None, q, "DNP3 flags: " + ", ".join(names))


def parse_address(addr: Any, allowed: str, what: str) -> tuple[str, int]:
    m = ADDRESS.match(addr) if isinstance(addr, str) else None
    if not m:
        raise ValueError(f"{what} needs an address like {allowed}:3, got {addr!r}")
    kind, index = m.group(1).upper(), int(m.group(2))
    if kind != allowed:
        raise ValueError(f"{what} must use a {allowed} point, got {addr!r}")
    if index > 65535:
        raise ValueError(f"{what}: index {index} is above 65535")
    return kind, index


def _positive(config: dict, key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"config {key} must be a positive number, got {value!r}")
    return float(value)


def _why(e: BaseException) -> str:
    if isinstance(e, TimeoutError):
        return str(e) or "no answer from the outstation"
    text = str(e).strip()
    return f"{type(e).__name__}: {text[:160]}" if text else type(e).__name__


# ---------- the connector ----------

class Dnp3Connector(Connector):
    kind = "dnp3"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float],
                 master_factory: Optional[Callable[[], Master]] = None) -> None:
        super().__init__(spec, fmap, now)
        cfg = self.config
        sm.check_env_names(cfg)
        self.backend = str(cfg.get("backend") or "dnp3").lower()
        if self.backend not in ("dnp3", "fake"):
            raise ValueError(f"config backend must be \"dnp3\" or \"fake\", got {cfg.get('backend')!r}")
        self.host = cfg.get("host")
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("config host must name the outstation, for example 127.0.0.1")
        self.host = self.host.strip()
        port = cfg.get("port", 20000)
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError(f"config port must be a TCP port from 1 to 65535, got {port!r}")
        self.port = port
        if self.backend == "fake" and self.host not in LOCAL_HOSTS:
            raise ValueError("backend fake talks to the fake outstation only, on 127.0.0.1 or localhost")
        self.integrity_s = _positive(cfg, "integrity_s", 60)
        self.event_s = _positive(cfg, "event_s", 5)
        self.timeout_s = _positive(cfg, "timeout_s", 5)
        self.select_timeout_s = _positive(cfg, "select_timeout_s", 5)
        self.choke_full_scale = _positive(cfg, "choke_full_scale", 1.0)
        self.unsolicited = cfg.get("unsolicited", True)
        if not isinstance(self.unsolicited, bool):
            raise ValueError("config unsolicited must be true or false")
        style = str(cfg.get("crob_style") or "latch").lower()
        if style not in CROB_CODES:
            raise ValueError(f"config crob_style must be latch or pulse, got {cfg.get('crob_style')!r}")
        self.crob_codes = CROB_CODES[style]
        self.backoff = sm.Backoff(_positive(cfg, "reconnect_initial_s", 1.0), _positive(cfg, "reconnect_max_s", 30.0))
        self.smap = sm.SignalMap(spec)

        self.by_point: dict[tuple, list[tuple]] = {}
        self.addr_of: dict[tuple, str] = {}
        for (aid, sig), addr in self.signals.items():
            want = SIGNAL_POINT.get(sig)
            if want is None:
                raise ValueError(f"signal {aid}.{sig} is not a live signal")
            point = parse_address(addr, want, f"signal {aid}.{sig}")
            self.by_point.setdefault(point, []).append((aid, sig))
            self.addr_of[(aid, sig)] = f"{point[0]}:{point[1]}"
        self.controls: dict[tuple, tuple] = {}
        for (aid, typ), addr in self.commands.items():
            want = COMMAND_POINT.get(typ)
            if want is None:
                raise ValueError(f"command {aid}.{typ}: a DNP3 connector writes {', '.join(COMMAND_POINT)}")
            self.controls[(aid, typ)] = parse_address(addr, want, f"command {aid}.{typ}")

        self.inbox = sm.Inbox()
        self.quality: dict[tuple, dict] = {}
        self._lock = threading.Lock()              # state shared with read() and the reader thread
        self._chan = threading.Lock()              # one request on the channel at a time
        self._held: dict[tuple, Any] = {}          # (aid, sig) -> last good value, while polls are answered
        self._last_t: dict[tuple, float] = {}      # (kind, index) -> epoch time of the value held
        self._master: Optional[Master] = None      # set only while connected
        self._up = False                            # True once the first integrity poll after a connect is answered
        self._want_integrity = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        if master_factory is None and self.backend == "fake":
            master_factory = lambda: LineMaster(self.host, self.port)  # noqa: E731
        self._factory = master_factory
        if self._factory is None:
            _, why = sm.import_optional("pydnp3", "dnp3-python")
            self._detail = ("no DNP3 library is usable on this machine ("
                            + (why or "pydnp3 imports, but this connector has no adapter for it yet")
                            + "); use backend \"fake\" with backend/fakes/dnp3.py, or add a Master adapter")
            return
        self._detail = "connecting"
        self._thread = threading.Thread(target=self._run, name=f"dnp3-{self.id}", daemon=True)
        self._thread.start()

    # ---------- the interface ----------
    def read(self) -> list[Reading]:
        out: list[Reading] = []
        with self._lock:
            fresh = self.inbox.drain()
            if self._master is None:
                return out
            for aid, sig, value in fresh:
                out.append(self.reading(aid, sig, value))
        return out

    def send(self, command: dict) -> dict:
        try:
            typ, aid, value = sm.normalize(command)
        except ValueError as e:
            return {"ok": False, "detail": f"{self.id}: {e}"}
        point = self.controls.get((aid, typ))
        if point is None:
            return {"ok": False, "detail": f"{self.id} has no {typ} point for {aid}; nothing was sent."}
        kind, index = point
        if kind == "CROB":
            ctl = Control(kind, index, code=self.crob_codes[0] if value else self.crob_codes[1])
        else:
            ctl = Control(kind, index, value=round(value * self.choke_full_scale, 6))
        what = f"{aid} {command.get('value')} ({kind}:{index} {ctl.code or ctl.value})"
        start = time.monotonic()
        deadline = start + self.timeout_s
        if not self._chan.acquire(timeout=self.timeout_s):
            return {"ok": False, "detail": f"{self.id}: the channel stayed busy for {self.timeout_s:g} s; "
                                           f"nothing was sent for {what}."}
        try:
            with self._lock:
                master = self._master
            if master is None:
                return {"ok": False, "detail": f"{self.id} is not connected to the outstation; "
                                               f"nothing was sent for {what}."}
            # --- select ---
            t_sel = time.monotonic()
            try:
                echo = master.select(ctl, max(0.05, deadline - t_sel))
            except NotSent as e:
                return {"ok": False, "detail": f"{self.id}: select for {what} was not sent ({_why(e)}); "
                                               f"nothing was operated."}
            except (TimeoutError, ConnectionError, OSError) as e:
                return {"ok": False, "detail": f"{self.id}: select for {what} got no answer ({_why(e)}); "
                                               f"nothing was operated."}
            bad = self._echo_problem(ctl, echo)
            if bad:
                return {"ok": False, "detail": f"{self.id}: select for {what} {bad}; nothing was operated."}
            elapsed = time.monotonic() - t_sel
            left = min(deadline - time.monotonic(), self.select_timeout_s - elapsed)
            if left <= 0:
                return {"ok": False, "detail": f"{self.id}: the select answer for {what} came too late to operate "
                                               f"within select_timeout_s or timeout_s; nothing was operated."}
            # --- operate: once, never retried ---
            try:
                echo = master.operate(ctl, left)
            except NotSent as e:
                return {"ok": False, "detail": f"{self.id}: operate for {what} was not sent ({_why(e)}); "
                                               f"nothing was operated."}
            except (TimeoutError, ConnectionError, OSError):
                return sm.uncertain_result()
            bad = self._echo_problem(ctl, echo)
            if bad and "refused" in bad:
                return {"ok": False, "detail": f"{self.id}: operate for {what} {bad}; nothing moved."}
            if bad:
                return {**sm.uncertain_result(),
                        "detail": f"{sm.UNCERTAIN_DETAIL} ({self.id}: operate for {what} {bad})"}
            return {"ok": True, "detail": f"{self.id}: {what} selected and operated, echo SUCCESS."}
        finally:
            self._chan.release()

    def status(self) -> dict:
        with self._lock:
            up = self._up and self._master is not None
            detail = self._detail
        return {"id": self.id, "kind": self.kind, "available": up, "detail": detail}

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            master, self._master = self._master, None
            self._up = False
            self._held.clear()
        if master is not None:
            sm.close_later(master.close, name=f"dnp3-close-{self.id}")

    # ---------- helpers ----------
    @staticmethod
    def _echo_problem(ctl: Control, echo: Any) -> Optional[str]:
        """None when the echo matches the request and says SUCCESS, else what is wrong."""
        if not isinstance(echo, dict):
            return "got no echo"
        want = ctl.wire()
        if str(echo.get("kind", "")).upper() != ctl.kind or echo.get("index") != ctl.index:
            return f"echo names {echo.get('kind')}:{echo.get('index')}, not {ctl.kind}:{ctl.index}"
        if ctl.kind == "CROB" and echo.get("code") != want["code"]:
            return f"echo code {echo.get('code')!r} does not match {want['code']!r}"
        if ctl.kind == "AO":
            v = echo.get("value")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isclose(
                    float(v), ctl.value, rel_tol=1e-6, abs_tol=1e-6):
                return f"echo value {v!r} does not match {ctl.value!r}"
        status = echo.get("status")
        if status != 0:
            return f"was refused by the outstation: status {STATUS.get(status, status)}"
        return None

    def _max_age(self, aid: str, sig: str) -> float:
        rule = self.smap.rules.get((aid, sig))
        return rule.max_age_s if rule is not None and rule.max_age_s is not None else self.smap.max_age_s

    def _mark(self, key: tuple, quality: str, detail: str) -> None:
        self.quality[key] = {"quality": quality, "detail": detail, "t": self.now()}

    def _process(self, master: Master, resp: Response, poll: Optional[str] = None) -> None:
        """Map the declared points in one response from `master`. poll is "integrity" or
        "events" for a poll answer, None for an unsolicited response. Called on the I/O
        thread or the reader thread (unsolicited). Nothing happens unless `master` is
        still the current link, so an answer that lands after a loss or close() is dropped."""
        wall = time.time()
        with self._lock:
            if self._master is not master or self._stop.is_set():
                return
            statics = set()
            for p in resp.points:
                if p.time is None:
                    statics.add((p.kind, p.index))
                keys = self.by_point.get((p.kind, p.index))
                if not keys:
                    continue
                pk = (p.kind, p.index)
                addr = f"{p.kind}:{p.index}"
                if p.time is None:
                    # A static value is the present. Event times after it are compared afresh,
                    # so an outstation clock that was reset after a restart can't block them.
                    self._last_t.pop(pk, None)
                else:
                    held_t = self._last_t.get(pk)
                    if held_t is not None and p.time < held_t:
                        continue                   # an older buffered event: the held value is newer
                for aid, sig in keys:
                    if p.time is not None and abs(wall - p.time) > self._max_age(aid, sig):
                        # History (buffered during an outage, or replayed): never a reading, and
                        # it says nothing about the present, so a newer held value stays.
                        if (aid, sig) not in self._held:
                            self._mark((aid, sig), sm.STALE,
                                       f"{addr}: the event was stamped {wall - p.time:.0f} s ago")
                        continue
                    bad = flag_quality(p.kind, p.flags)
                    m = bad if bad is not None else self.smap.apply(aid, sig, p.value)
                    self.inbox.put(aid, sig, m)
                    if m.good:
                        self._held[(aid, sig)] = m.value
                        self._mark((aid, sig), m.quality, f"{addr} ok")
                    else:
                        self._held.pop((aid, sig), None)
                        self._mark((aid, sig), m.quality, f"{addr}: {m.detail}")
                if p.time is not None and abs(wall - p.time) <= self.smap.max_age_s:
                    self._last_t[pk] = p.time
            if poll == "integrity":
                # The integrity poll is the outstation's whole static picture: a declared
                # point it no longer reports is not held.
                for pk, keys in self.by_point.items():
                    if pk in statics:
                        continue
                    for aid, sig in keys:
                        self._held.pop((aid, sig), None)
                        m = sm.Mapped(None, sm.NULL, f"{pk[0]}:{pk[1]}: not reported in the integrity poll")
                        self.inbox.put(aid, sig, m)
                        self._mark((aid, sig), m.quality, m.detail)
            if poll is not None:
                # An answered poll confirms every held value it didn't change.
                for (aid, sig), value in self._held.items():
                    self.inbox.put(aid, sig, sm.Mapped(value, sm.GOOD))
            if resp.iin & RESTART_IIN:
                self._want_integrity = True

    def _on_unsolicited(self, master: Master, resp: Response) -> None:
        if self.unsolicited:
            self._process(master, resp)

    def _poll(self, master: Master, integrity: bool) -> None:
        with self._chan:
            if self._stop.is_set():
                return
            resp = (master.integrity_poll(self.timeout_s) if integrity
                    else master.event_poll(EVENT_CLASSES, self.timeout_s))
        self._process(master, resp, "integrity" if integrity else "events")

    def _lost(self, why: str) -> None:
        with self._lock:
            self._master = None
            self._up = False
            self._held.clear()
            self._last_t.clear()
            self.inbox.clear()
            for key in self.signals:
                self._mark(key, sm.COMM_LOST, f"{self.addr_of.get(key, '?')}: {why}")
            self._detail = why

    def _connected_detail(self) -> str:
        how = "fake outstation, JSON lines, not DNP3 framing" if self.backend == "fake" else "DNP3"
        return (f"polling {self.host}:{self.port} ({how}); integrity every {self.integrity_s:g} s, "
                f"events every {self.event_s:g} s, unsolicited {'on' if self.unsolicited else 'off'}")

    def _run(self) -> None:
        while not self._stop.is_set():
            master = self._factory()
            why = "stopped"
            try:
                master.connect(self.timeout_s)
                master.on_unsolicited(lambda resp, m=master: self._on_unsolicited(m, resp))
                with self._chan:
                    master.enable_unsolicited(self.unsolicited, EVENT_CLASSES, self.timeout_s)
                with self._lock:
                    if self._stop.is_set():
                        break
                    self._master = master
                    self._want_integrity = False
                self._poll(master, integrity=True)
                with self._lock:
                    if self._stop.is_set():
                        break
                    self._detail = self._connected_detail()
                    self._up = True
                self.backoff.reset()
                next_int = time.monotonic() + self.integrity_s
                next_ev = time.monotonic() + self.event_s
                while not self._stop.is_set():
                    now = time.monotonic()
                    if self._want_integrity or now >= next_int:
                        with self._lock:
                            self._want_integrity = False
                        self._poll(master, integrity=True)
                        next_int = time.monotonic() + self.integrity_s
                        next_ev = time.monotonic() + self.event_s
                    elif now >= next_ev:
                        self._poll(master, integrity=False)
                        next_ev = time.monotonic() + self.event_s
                    else:
                        self._stop.wait(min(next_int, next_ev) - now if not self._want_integrity else 0)
            except TimeoutError as e:
                why = f"no answer from the outstation within {self.timeout_s:g} s ({_why(e)})"
            except Exception as e:  # noqa: BLE001 - the link is down; say why and reconnect
                why = f"link to {self.host}:{self.port} is down ({_why(e)})"
            finally:
                try:
                    master.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.is_set():
                break
            delay = self.backoff.next_delay()
            self._lost(f"{why}; reconnecting in {delay:.1f} s")
            self._stop.wait(delay)
        with self._lock:                            # stopped: no link is left behind
            self._master = None
            self._up = False
            self._held.clear()
            self.inbox.clear()
