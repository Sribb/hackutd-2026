"""EtherNet/IP connector (kind "enip"): Allen-Bradley Logix tags in, confirmed writes out.

    {"id": "CONN_PLC", "kind": "enip",
     "config": {"host": "10.0.0.20", "slot": 0, "poll_ms": 1000, "timeout_s": 5,
                "heartbeat_tag": "Heartbeat", "heartbeat_s": 10},
     "signals":  [{"asset_id": "TANK_6", "signal": "level_pct", "address": "Tank6_Level"},
                  {"asset_id": "PUMP_6", "signal": "running", "address": "Program:Main.Pump6_Run"},
                  {"asset_id": "TANK_5", "signal": "level_pct", "address": "Levels[2]"}],
     "commands": [{"asset_id": "VALVE_D_6", "command": "SET_VALVE", "address": "Valve6_Cmd"}]}

The connector talks to the controller with pycomm3's LogixDriver on its own daemon
thread. Every poll_ms it reads all declared tags in one multi-tag read, maps each
value through signalmap (scale, signal_map rules, range), and queues the good ones
for read(). A tag that comes back with an error, no value, or a value the rules
reject gives no reading, and its quality says why. config.port changes the TCP port
(44818 by default).

Addresses are Logix tag names: Tank6_Level, Program:Main.Pump6_Run, Levels[2],
Motor.Status.3 (a bit of an integer). The syntax is checked when the blueprint loads.

Nothing moves without a person. The only write is in send(), which the live layer
calls after a person confirms. send() hands the write to the connector's thread and
waits up to timeout_s. The value takes the tag's type: BOOL gets True or False, an
integer tag 1 or 0, a REAL or LREAL tag 1.0 or 0.0 or the choke fraction. CHOKE_WELL
needs a REAL or LREAL tag. A write that never left is reported as not sent; a write
that left without an answer is reported as uncertain and is never sent again, not
even after a reconnect.

Never falsely safe. When the link drops, every declared signal's quality becomes
comm_lost and nothing waiting from before is handed on. A controller can answer
while its program has stopped; config.heartbeat_tag names a counter the program
changes every scan. After every connect the connector withholds every value until
it has seen the heartbeat change, and when it stays the same for heartbeat_s
seconds the connector withholds every value again and says the controller looks
dead. A heartbeat that isn't a number (or is NaN) counts as unreadable.

config fake: true swaps LogixDriver for backend/fakes/enip.py's RemoteLogix, which
talks to a simulated controller on 127.0.0.1 over a small JSON protocol with the
same read / write / get_tag_info calls and Tag-like results. Tests and the demo use
it. The EtherNet/IP wire protocol (CIP) is exercised only against real hardware:
cpppo, the usual simulator, is GPL and is not used.

EtherNet/IP has no login, so this connector reads no secret. Any `*_env` key in its
config must still name a FREEBOARD_CONN_ variable, or the connector is unavailable.
"""
from __future__ import annotations

import math
import re
import threading
import time
from typing import Any, Callable, Optional

from ..fieldmap import FieldMap
from . import signalmap as sm
from .base import Connector, Reading

IDENT = r"[A-Za-z_][A-Za-z0-9_]{0,39}"
INDEX = r"\[\d{1,9}(?:,\d{1,9}){0,2}\]"
SEGMENT = rf"{IDENT}(?:{INDEX})?"
TAG = re.compile(rf"(?:Program:{IDENT}\.)?{SEGMENT}(?:\.{SEGMENT})*(?:\.\d{{1,2}})?")
BIT = re.compile(r".*\.\d{1,2}")
INTEGER_TYPES = ("SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT")
FLOAT_TYPES = ("REAL", "LREAL")
WRITABLE = ("SET_VALVE", "SET_PUMP", "CHOKE_WELL")
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def check_tag(tag: Any, where: str) -> str:
    """The tag name, or ValueError saying it isn't a Logix tag name."""
    if not isinstance(tag, str) or not TAG.fullmatch(tag):
        raise ValueError(f"{where}: {tag!r} is not a Logix tag name (for example Tank6_Level, "
                         f"Program:Main.Pump6_Run, or Levels[2])")
    return tag


def _number(config: dict, key: str, default: float, low: float, high: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError(f"config {key} must be a number from {low:g} to {high:g}, got {value!r}")
    return float(value)


def tag_value(tag: str, typ: Any, kind: str, value: Any) -> Any:
    """The confirmed value in the tag's type, or ValueError saying why it can't be sent."""
    typ = "BOOL" if BIT.fullmatch(tag) else typ
    if not isinstance(typ, str):
        raise ValueError(f"tag {tag} is a structure or of unknown type; write to a BOOL, integer, or REAL tag")
    typ = typ.upper()
    if kind == "CHOKE_WELL":
        if typ in FLOAT_TYPES:
            return float(value)
        raise ValueError(f"CHOKE_WELL needs a REAL or LREAL tag; {tag} is {typ}")
    if typ == "BOOL":
        return bool(value)
    if typ in INTEGER_TYPES:
        return 1 if value else 0
    if typ in FLOAT_TYPES:
        return 1.0 if value else 0.0
    raise ValueError(f"tag {tag} is {typ}; write to a BOOL, integer, or REAL tag")


def typed_value(drv: Any, tag: str, kind: str, value: Any) -> Any:
    """The confirmed value in the type the driver knows for `tag`. ValueError when it
    can't be sent: an unknown tag, a structure, a type that doesn't fit. pycomm3 looks
    the type up in the tag list it uploaded on connect, so a failed lookup there is a
    config problem; only an OSError (the simulated controller's socket) means the link
    failed, and that one is raised as it is."""
    if BIT.fullmatch(tag):
        return tag_value(tag, "BOOL", kind, value)
    try:
        info = drv.get_tag_info(tag)
    except OSError:
        raise
    except Exception as e:  # noqa: BLE001 - pycomm3 raises RequestError for an unknown tag
        raise ValueError(f"couldn't look up tag {tag}: {type(e).__name__}: {e}") from e
    typ = info.get("data_type") if isinstance(info, dict) else None
    return tag_value(tag, typ, kind, value)


def _beat(value: Any) -> Any:
    """A heartbeat value that can be compared with the last one, or None. NaN never
    equals itself, so it would look like a heartbeat that changes on every poll."""
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


class _Write:
    """One confirmed write, handed from send() to the I/O thread. `state` moves from
    queued to sending (the I/O thread took it) or cancelled (send() gave up first),
    under the lock, so a write that send() already reported is never sent late."""

    def __init__(self, kind: str, tag: str, value: Any) -> None:
        self.kind, self.tag, self.value = kind, tag, value
        self.state = "queued"
        self.result: Optional[dict] = None
        self.done = threading.Event()


class EnipConnector(Connector):
    kind = "enip"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float]) -> None:
        super().__init__(spec, fmap, now)
        cfg = self.config
        sm.check_env_names(cfg)
        self.smap = sm.SignalMap(spec)
        self.fake = cfg.get("fake") is True
        host = cfg.get("host")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("config host is required (the controller's IP address)")
        self.host = host.strip()
        port = cfg.get("port")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536):
            raise ValueError(f"config port must be a TCP port number, got {port!r}")
        self.port = port
        slot = cfg.get("slot", 0)
        if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot <= 31:
            raise ValueError(f"config slot must be a whole number from 0 to 31, got {slot!r}")
        self.slot = slot
        self.poll_s = _number(cfg, "poll_ms", 1000, 20, 3_600_000) / 1000.0
        self.timeout_s = _number(cfg, "timeout_s", 5, 0.1, 60)
        hb = cfg.get("heartbeat_tag")
        self.heartbeat_tag = check_tag(hb, "config heartbeat_tag") if hb is not None else None
        self.heartbeat_s = _number(cfg, "heartbeat_s", 10, 0.1, 3600)

        for (aid, sig), addr in self.signals.items():
            check_tag(addr, f"signals {aid}.{sig} address")
        for (aid, cmd), addr in self.commands.items():
            if cmd not in WRITABLE:
                raise ValueError(f"commands: {aid} {cmd} can't go to a controller; use {', '.join(WRITABLE)}")
            check_tag(addr, f"commands {aid} {cmd} address")
        self.tags = sorted(set(self.signals.values()) | ({self.heartbeat_tag} if self.heartbeat_tag else set()))

        if self.fake:
            if self.host not in LOOPBACK or port is None:
                raise ValueError("config fake: true talks to the simulated controller, so host must be "
                                 "127.0.0.1 and port its port")
            from ...fakes import enip as fake_enip
            self._make = lambda: fake_enip.RemoteLogix(self.host, self.port, self.timeout_s)
            self.where = f"simulated controller at {self.host}:{port} (fake: no CIP traffic)"
        else:
            pycomm3, why = sm.import_optional("pycomm3", "pycomm3")
            if pycomm3 is None:
                raise ValueError(why)
            path = f"{self.host}:{port}/{slot}" if port else f"{self.host}/{slot}"
            programs = any(t.startswith("Program:") for t in self.tags + list(self.commands.values()))

            def make() -> Any:
                drv = pycomm3.LogixDriver(path, init_program_tags=programs)
                # pycomm3 1.2.14's socket_timeout setter stores the value under a misspelled
                # key that open() never reads, so set the key open() does read.
                drv._cfg["socket_timeout"] = self.timeout_s
                return drv
            self._make = make
            self.where = f"Logix controller at {path}"

        # Every declared signal has an entry from the start, so the dict never changes size
        # while another thread reads it.
        self.quality: dict[tuple, dict] = {key: {"quality": sm.NULL, "detail": "no value yet", "t": now()}
                                           for key in self.signals}
        self.inbox = sm.Inbox()
        self._lock = threading.Lock()
        self._writes: list[_Write] = []
        self._up = False
        self._detail = f"connecting to {self.where}"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"enip-{self.id}", daemon=True)
        self._thread.start()

    # ---------- the interface ----------
    def read(self) -> list[Reading]:
        return [self.reading(aid, sig, value) for aid, sig, value in self.inbox.drain()]

    def status(self) -> dict:
        with self._lock:
            return {"id": self.id, "kind": self.kind, "available": self._up, "detail": self._detail}

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        sm.close_later(lambda: self._thread.join(timeout=self.timeout_s + 1), name=f"enip-close-{self.id}")

    def send(self, command: dict) -> dict:
        try:
            kind, aid, value = sm.normalize(command)
        except ValueError as e:
            return {"ok": False, "detail": str(e)}
        tag = self.commands.get((aid, kind))
        if tag is None:
            return {"ok": False, "detail": f"{self.id} has no {kind} tag for {aid}; nothing was sent"}
        with self._lock:
            if not self._up or self._stop.is_set():
                return {"ok": False, "detail": f"not sent: {self.id} is not connected ({self._detail})"}
            w = _Write(kind, tag, value)
            self._writes.append(w)
        self._wake.set()
        if w.done.wait(self.timeout_s):
            return w.result or sm.uncertain_result()
        with self._lock:
            if w.state == "queued":
                w.state = "cancelled"
                if w in self._writes:
                    self._writes.remove(w)
                return {"ok": False, "detail": f"not sent: {self.id} was busy for {self.timeout_s:g} s; "
                                               f"nothing reached the controller"}
        if w.done.wait(0.05):     # it finished just now
            return w.result or sm.uncertain_result()
        return sm.uncertain_result()

    # ---------- the I/O thread ----------
    def _set(self, up: bool, detail: str) -> None:
        with self._lock:
            self._up, self._detail = up, detail

    def _mark(self, aid: str, sig: str, quality: str, detail: str) -> None:
        self.quality[(aid, sig)] = {"quality": quality, "detail": detail, "t": self.now()}

    def _lost(self, why: str) -> None:
        """The link is down: no value from before it goes out, every signal is comm_lost,
        and writes still waiting are failed, never kept for after a reconnect."""
        self._set(False, why)
        self.inbox.clear()
        for aid, sig in self.signals:
            self.inbox.put(aid, sig, sm.Mapped(None, sm.COMM_LOST, why))
            self._mark(aid, sig, sm.COMM_LOST, why)
        with self._lock:
            waiting, self._writes = self._writes, []
            for w in waiting:
                if w.state == "queued":
                    w.state = "cancelled"
                    w.result = {"ok": False, "detail": f"not sent: {why}"}
                    w.done.set()

    def _run(self) -> None:
        backoff = sm.Backoff(initial_s=min(1.0, self.poll_s * 4), max_s=30.0)
        while not self._stop.is_set():
            drv = None
            try:
                drv = self._make()
                if drv.open() is False:
                    raise ConnectionError("the controller refused the connection")
            except Exception as e:  # noqa: BLE001
                self._close(drv)
                self._lost(f"can't reach {self.where}: {type(e).__name__}: {e}; retrying")
                self._stop.wait(backoff.next_delay())
                continue
            backoff.reset()
            self._set(True, f"connected to {self.where}, polling {len(self.tags)} tags every "
                            f"{self.poll_s * 1000:g} ms")
            try:
                self._poll_loop(drv)
            except Exception as e:  # noqa: BLE001
                self._lost(f"link to {self.where} lost: {type(e).__name__}: {e}; reconnecting")
            finally:
                self._close(drv)
            if not self._stop.is_set():
                self._stop.wait(backoff.next_delay())
        self._lost(f"{self.id} closed")

    @staticmethod
    def _close(drv: Any) -> None:
        if drv is not None:
            try:
                drv.close()
            except Exception:  # noqa: BLE001
                pass

    def _poll_loop(self, drv: Any) -> None:
        # The heartbeat starts untrusted on every connect: a controller whose program has
        # stopped still answers, so its values count only once the heartbeat has changed.
        hb = {"last": None, "seen": None, "since": time.monotonic()}
        next_poll = 0.0
        while not self._stop.is_set():
            self._do_writes(drv)
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + self.poll_s
                self._poll(drv, hb)
            self._wake.wait(max(0.0, next_poll - time.monotonic()))
            self._wake.clear()

    def _heartbeat_problem(self, by_tag: dict, hb: dict) -> Optional[tuple[str, str]]:
        """(quality, why) when the heartbeat says the values can't be trusted, else None."""
        r = by_tag.get(self.heartbeat_tag)
        value = None if r is None or getattr(r, "error", None) else _beat(getattr(r, "value", None))
        now = time.monotonic()
        tag = self.heartbeat_tag
        if value is None:
            return sm.COMM_LOST, f"controller heartbeat {tag} can't be read as a number"
        if hb["last"] is not None and value != hb["last"]:
            hb["seen"] = now
        hb["last"] = value
        if hb["seen"] is None:
            waited = now - hb["since"]
            if waited > self.heartbeat_s:
                return sm.COMM_LOST, (f"controller heartbeat {tag} has not changed in {waited:.0f} s "
                                      f"since connecting; the program looks stopped")
            return sm.UNCERTAIN, f"connected; waiting for controller heartbeat {tag} to change"
        if now - hb["seen"] > self.heartbeat_s:
            return sm.COMM_LOST, f"controller heartbeat {tag} stopped changing {now - hb['seen']:.0f} s ago"
        return None

    def _poll(self, drv: Any, hb: dict) -> None:
        if not self.tags:
            return
        result = drv.read(*self.tags)
        results = result if isinstance(result, list) else [result]
        by_tag = {getattr(r, "tag", None): r for r in results}
        if self.heartbeat_tag:
            problem = self._heartbeat_problem(by_tag, hb)
            if problem is not None:
                quality, why = problem
                why += f"; values from {self.where} withheld"
                self._set(False, why)
                for aid, sig in self.signals:
                    self.inbox.put(aid, sig, sm.Mapped(None, quality, why))
                    self._mark(aid, sig, quality, why)
                return
        self._set(True, f"connected to {self.where}, polling {len(self.tags)} tags every "
                        f"{self.poll_s * 1000:g} ms")
        for (aid, sig), tag in self.signals.items():
            r = by_tag.get(tag)
            if r is None:
                mapped = sm.Mapped(None, sm.NULL, f"{tag}: no answer for this tag")
            elif getattr(r, "error", None):
                mapped = sm.Mapped(None, sm.BAD, f"{tag}: {r.error}")
            elif r.value is None:
                mapped = sm.Mapped(None, sm.NULL, f"{tag}: no value")
            else:
                mapped = self.smap.apply(aid, sig, r.value)
            self.inbox.put(aid, sig, mapped)
            self._mark(aid, sig, mapped.quality, mapped.detail or "")

    def _do_writes(self, drv: Any) -> None:
        while True:
            with self._lock:
                if not self._writes:
                    return
                w = self._writes.pop(0)
                if w.state != "queued":
                    continue
                if not self._up:      # the heartbeat died after send() took it
                    w.state = "cancelled"
                    w.result = {"ok": False, "detail": f"not sent: {self._detail}"}
                    w.done.set()
                    continue
                w.state = "sending"
            try:
                value = typed_value(drv, w.tag, w.kind, w.value)
            except ValueError as e:
                w.result = {"ok": False, "detail": f"not sent: {e}"}
                w.done.set()
                continue
            except Exception as e:  # noqa: BLE001 - the link failed before the write left
                w.result = {"ok": False, "detail": f"not sent: couldn't look up tag {w.tag}: {type(e).__name__}"}
                w.done.set()
                raise
            try:
                res = drv.write((w.tag, value))
            except Exception:  # noqa: BLE001 - it may have reached the controller
                w.result = sm.uncertain_result()
                w.done.set()
                raise
            res = res[0] if isinstance(res, list) and res else res
            err = getattr(res, "error", None)
            if res is None or err:
                w.result = {"ok": False, "detail": f"{self.where} rejected {w.tag} = {value!r}: {err or 'no answer'}"}
            else:
                w.result = {"ok": True, "detail": f"wrote {w.tag} = {value!r} on {self.where}"}
            w.done.set()
