"""Modbus TCP connector (pymodbus).

Addresses are 0-based protocol addresses with an optional table:

    "hr:12" or "12"   holding register 12        (read and write)
    "ir:12"           input register 12           (read only)
    "coil:3"          coil 3                      (read and write)
    "di:3"            discrete input 3            (read only)

Readings: a background thread polls every signal every `poll_s` seconds. A register
holds an unsigned 16-bit integer; the value is `raw * scale` (the signal's `scale`,
default 1). A register reading `bad_value` (default 65535, the usual PLC "bad
quality" marker) is no reading at all, so the asset goes stale instead of showing a
made-up number. `running` and `position` read nonzero as running / OPEN.

Commands, written only after a person confirms:

    SET_VALVE   OPEN 1, CLOSED 0     (coil, or holding register)
    SET_PUMP    ON 1, OFF 0
    CHOKE_WELL  percent kept, 0 (shut in) to 100 (untouched), in a holding register

Only a Modbus exception that means "refused, nothing moved" makes a write FAILED:
0x01 illegal function, 0x02 illegal data address, 0x03 illegal data value (REFUSED).
Every other exception may come after the PLC started on the write, so it is
`uncertain` (UNCERTAIN, never FAILED): 0x04 slave device failure, 0x05 acknowledge
(accepted, still working), 0x06 busy, 0x0A/0x0B gateway path unavailable / target
didn't respond, and any code this list doesn't know. A write that was sent but got no
reply within timeout_s (or whose link dropped while waiting) is `uncertain` too: the
PLC may have applied it. The client runs with retries=0, so pymodbus never re-sends a
write on its own, and the connector never retries one either.

Config: host (required), port (502), unit_id (1), poll_s (1), timeout_s (2),
bad_value (65535).

An unreachable PLC never blocks the console: polling happens off the event loop,
the connector reports `unavailable` with the reason, its assets go stale (UNKNOWN),
and commands fail cleanly.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ..fieldmap import FieldMap
from .base import UNCERTAIN_DETAIL, Connector, Reading, coerce, import_optional

TABLES = {"hr": "hr", "holding": "hr", "ir": "ir", "input": "ir", "coil": "coil", "co": "coil",
          "di": "di", "discrete": "di"}
WRITABLE = ("hr", "coil")
MAX_BACKOFF_S = 10.0


def parse_address(address) -> tuple[str, int]:
    """'hr:12' -> ('hr', 12). A bare number is a holding register. Raises ValueError."""
    if address is None or str(address).strip() == "":
        raise ValueError("no address")
    text = str(address).strip().lower()
    table, _, num = text.rpartition(":")
    table = TABLES.get(table or "hr")
    if table is None:
        raise ValueError(f"unknown Modbus table in {address!r}; use hr, ir, coil, or di")
    n = int(num)
    if not 0 <= n <= 65535:
        raise ValueError(f"address {n} is outside 0 to 65535")
    return table, n


def command_raw(command: dict) -> Optional[int]:
    """The integer a command writes, or None if its value makes no sense."""
    typ, v = str(command.get("type", "")).upper(), command.get("value")
    if typ == "SET_VALVE":
        return {"OPEN": 1, "CLOSED": 0}.get(str(v).upper())
    if typ == "SET_PUMP":
        return {"ON": 1, "OFF": 0}.get(str(v).upper())
    if typ == "CHOKE_WELL":
        try:
            return max(0, min(100, int(round(float(v) * 100))))
        except (TypeError, ValueError):
            return None
    return None


# Exception codes that mean the PLC rejected the request before acting on it.
REFUSED_CODES = {0x01: "illegal function", 0x02: "illegal data address", 0x03: "illegal data value"}
# Every other code is UNCERTAIN for a write: the PLC (or a gateway) may have acted on it.
EXCEPTION_NAMES = {**REFUSED_CODES, 0x04: "slave device failure", 0x05: "acknowledge (still working)",
                   0x06: "slave device busy", 0x08: "memory parity error",
                   0x0A: "gateway path unavailable", 0x0B: "gateway target device failed to respond"}


class DeviceRefused(Exception):
    """The PLC answered with a Modbus exception (bad address, say). The link itself is fine.
    `code` is the exception code; only REFUSED_CODES mean nothing moved."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code

    @property
    def nothing_moved(self) -> bool:
        return self.code in REFUSED_CODES


def _unit_kw(method) -> str:
    """pymodbus 3.8 calls the unit `slave`; newer releases call it `device_id`."""
    try:
        import inspect
        return "device_id" if "device_id" in inspect.signature(method).parameters else "slave"
    except (TypeError, ValueError):
        return "slave"


class ModbusConnector(Connector):
    kind = "modbus"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float]) -> None:
        super().__init__(spec, fmap, now)
        cfg = self.config
        self.host = str(cfg.get("host") or "")
        self.port = int(cfg.get("port") or 502)
        self.unit = int(cfg.get("unit_id", 1))
        self.poll_s = max(0.02, float(cfg.get("poll_s", 1)))
        self.timeout = float(cfg.get("timeout_s", 2))
        self.bad_value = int(cfg.get("bad_value", 65535))
        scales = {(s.get("asset_id"), s.get("signal")): s.get("scale") for s in spec.get("signals") or []}
        self.points: list[tuple[tuple, str, int, float]] = []   # (key, table, address, scale)
        self.bad_addresses: list[str] = []
        for key, addr in self.signals.items():
            try:
                table, n = parse_address(addr)
                self.points.append((key, table, n, float(scales.get(key) or 1.0)))
            except ValueError as e:
                self.bad_addresses.append(f"{key[0]}.{key[1]}: {e}")
        self.targets: dict[tuple, tuple[str, int]] = {}
        for key, addr in self.commands.items():
            try:
                table, n = parse_address(addr)
            except ValueError as e:
                self.bad_addresses.append(f"{key[0]} {key[1]}: {e}")
                continue
            if table not in WRITABLE:
                self.bad_addresses.append(f"{key[0]} {key[1]}: {addr!r} is read-only; use hr or coil")
                continue
            self.targets[key] = (table, n)
        self._io = threading.Lock()                     # one request at a time on the socket
        self._lock = threading.Lock()
        self._latest: dict[tuple, object] = {}
        self._stop = threading.Event()
        self.connected = False
        self.last_error = ""
        self.polls = 0
        self.refused: list[str] = []                    # points the PLC answered with an exception
        self._retry_at = 0.0                            # monotonic time of the next reconnect attempt
        self._backoff = self.poll_s
        self.client = None
        self._thread: Optional[threading.Thread] = None
        self.why = self._start()

    def _start(self) -> str:
        mod, err = import_optional("pymodbus.client", "pymodbus")
        if mod is None:
            return err or "pymodbus is not installed"
        if not self.host:
            return "not configured: config needs host"
        var = self.missing_env()
        if var:
            return f"environment variable {var} is not set"
        # pymodbus logs every refused connect at ERROR; status() already carries the reason.
        logging.getLogger("pymodbus.logging").setLevel(logging.CRITICAL)
        try:
            self.client = mod.ModbusTcpClient(self.host, port=self.port, timeout=self.timeout, retries=0)
        except Exception as e:  # noqa: BLE001
            return f"can't start the Modbus client: {e}"
        self._unit_kw = _unit_kw(self.client.read_holding_registers)
        self._thread = threading.Thread(target=self._poll_loop, name=f"modbus-{self.id}", daemon=True)
        self._thread.start()
        return ""

    # ---- polling (background thread) ----
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001 - keep polling whatever happens
                self._fail(str(e))
            self._stop.wait(self.poll_s)

    def _ensure(self, force: bool = False) -> bool:
        """Connected, or try to connect. Between failed attempts the poller backs off up to
        MAX_BACKOFF_S; a confirmed command (force) always gets one attempt."""
        if self.client.connected:
            return True
        if not force and time.monotonic() < self._retry_at:
            return False
        if not self.client.connect():
            self._fail(f"can't reach {self.host}:{self.port}")
            return False
        self._backoff = self.poll_s
        return True

    def _fail(self, why: str) -> None:
        self.connected = False
        self.last_error = why
        self._retry_at = time.monotonic() + self._backoff
        self._backoff = min(MAX_BACKOFF_S, self._backoff * 2)
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass

    def _call(self, name: str, address: int, **kw):
        rr = getattr(self.client, name)(address, **kw, **{self._unit_kw: self.unit})
        code = int(getattr(rr, "exception_code", 0) or 0) if rr is not None else 0
        if code:
            what = EXCEPTION_NAMES.get(code, "unknown exception")
            raise DeviceRefused(f"{name}({address}): the PLC answered exception 0x{code:02X} ({what})", code)
        if rr is None or rr.isError():
            raise IOError(f"{name}({address}) failed: {rr}")
        return rr

    def poll_once(self) -> None:
        """Read every signal once. Public so tests can poll without waiting."""
        if self.client is None:
            return
        got: dict[tuple, object] = {}
        refused: list[str] = []
        with self._io:
            if not self._ensure():
                return
            try:
                for key, table, n, scale in self.points:
                    try:
                        if table in ("hr", "ir"):
                            name = "read_holding_registers" if table == "hr" else "read_input_registers"
                            raw = self._call(name, n, count=1).registers[0]
                            if raw == self.bad_value:
                                continue
                            value = raw * scale if key[1] not in ("running", "position") else raw
                        else:
                            name = "read_coils" if table == "coil" else "read_discrete_inputs"
                            value = bool(self._call(name, n, count=1).bits[0])
                    except DeviceRefused as e:  # that point goes stale; the rest still read
                        refused.append(f"{key[0]}.{key[1]}: {e}")
                        continue
                    v = coerce(key[1], value)
                    if v is not None:
                        got[key] = round(v, 4) if isinstance(v, float) else v
            except Exception as e:  # noqa: BLE001 - a broken link: drop it and reconnect with backoff
                self._fail(str(e))
                return
        self.connected = True
        self.last_error = ""
        self.refused = refused
        self.polls += 1
        with self._lock:
            self._latest.update(got)

    # ---- the interface ----
    def read(self) -> list[Reading]:
        with self._lock:
            latest, self._latest = self._latest, {}
        return [self.reading(aid, sig, v) for (aid, sig), v in latest.items()]

    def send(self, command: dict) -> dict:
        if self.client is None:
            return {"ok": False, "detail": f"{self.id} (modbus) is not available: {self.why}."}
        target = self.targets.get((command.get("asset_id"), command.get("type")))
        if target is None:
            return {"ok": False, "detail": f"{self.id} has no register for {command.get('type')} on {command.get('asset_id')}."}
        raw = command_raw(command)
        if raw is None:
            return {"ok": False, "detail": f"{self.id} can't encode {command.get('type')} {command.get('value')!r}."}
        table, n = target
        where = f"{table}:{n} on {self.host}:{self.port} (unit {self.unit})"
        with self._io:
            try:
                if not self._ensure(force=True):
                    return {"ok": False, "detail": f"Couldn't reach {self.id} at {self.host}:{self.port}. Nothing was sent."}
            except Exception as e:  # noqa: BLE001 - connecting failed: nothing was sent
                self._fail(str(e))
                return {"ok": False, "detail": f"Couldn't reach {self.id} at {self.host}:{self.port}: {e}. Nothing was sent."}
            try:
                if table == "coil":
                    self._call("write_coil", n, value=bool(raw))
                else:
                    self._call("write_register", n, value=raw)
            except DeviceRefused as e:
                if e.nothing_moved:  # illegal function, address, or value: rejected, nothing moved
                    return {"ok": False, "detail": f"{self.id} refused the write to {where}: {e}. Nothing moved."}
                # Busy, device failure, acknowledge, gateway: it may have acted (or still be acting).
                return {"ok": False, "uncertain": True,
                        "detail": f"{self.id}: wrote {raw} to {where}, {UNCERTAIN_DETAIL} ({e})."}
            except Exception as e:  # noqa: BLE001 - the request went out; the reply never came
                self._fail(str(e))
                return {"ok": False, "uncertain": True,
                        "detail": f"{self.id}: wrote {raw} to {where}, {UNCERTAIN_DETAIL} ({e})."}
        return {"ok": True, "detail": f"Wrote {raw} to {where}."}

    def status(self) -> dict:
        if self.client is None:
            return {"id": self.id, "kind": self.kind, "available": False, "detail": self.why}
        if not self.connected:
            return {"id": self.id, "kind": self.kind, "available": False,
                    "detail": self.last_error or f"connecting to {self.host}:{self.port}"}
        detail = f"polling {len(self.points)} points on {self.host}:{self.port} every {self.poll_s:g} s"
        if self.bad_addresses:
            detail += f"; ignored {len(self.bad_addresses)} bad addresses ({self.bad_addresses[0]})"
        if self.refused:
            detail += f"; the PLC refused {len(self.refused)} points ({self.refused[0]})"
        return {"id": self.id, "kind": self.kind, "available": True, "detail": detail}

    def close(self):
        """Returns at once. The poller stops at its next wait; joining it and closing the
        socket run on a daemon thread, so a PLC mid-timeout never blocks the event loop."""
        self._stop.set()
        thread, client = self._thread, self.client
        if thread is None and client is None:
            return None

        def stop() -> None:
            if thread is not None:
                thread.join(timeout=self.timeout + self.poll_s + 1)
            if client is not None:
                with self._io:
                    client.close()
        return self.close_later(stop, f"modbus-close-{self.id}")
