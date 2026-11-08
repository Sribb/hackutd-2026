"""OPC UA connector (kind "opcua"): subscriptions in, confirmed writes out.

An asyncua client runs on the connector's own daemon thread with its own asyncio
loop, so read(), status() and close() never wait on the network. The thread opens an
encrypted session, subscribes to every declared signal (address is a NodeId string
such as ns=2;s=TANK_6.Level), and hands mapped values to read() through an Inbox.

    {"id": "CONN_OPCUA", "kind": "opcua",
     "config": {"endpoint": "opc.tcp://10.0.0.5:4840",
                "security_policy": "Basic256Sha256", "security_mode": "SignAndEncrypt",
                "cert_path_env": "FREEBOARD_CONN_OPCUA_CERT", "key_path_env": "FREEBOARD_CONN_OPCUA_KEY",
                "server_cert_path_env": "FREEBOARD_CONN_OPCUA_SERVER_CERT",
                "username": "freeboard", "password_env": "FREEBOARD_CONN_OPCUA_PASSWORD",
                "publish_ms": 500, "timeout_s": 5, "max_age_s": 300},
     "signals": [{"asset_id": "TANK_6", "signal": "level_pct", "address": "ns=2;s=TANK_6.Level"}],
     "commands": [{"asset_id": "VALVE_D_6", "command": "SET_VALVE", "address": "ns=2;s=VALVE_D_6.Command"}]}

Rules this module keeps:

- Security. SignAndEncrypt with Basic256Sha256 unless config says otherwise.
  security_mode "None" is allowed only with allow_insecure: true and an endpoint on
  127.0.0.1 or localhost. The client certificate and key are files whose paths come
  from FREEBOARD_CONN_ variables. server_cert_path_env pins the server certificate.
  An endpoint off this machine needs it, unless config says allow_unpinned_server_cert:
  true; without a pin the client takes whatever certificate the endpoint shows, and
  status() says the certificate is not pinned.
- Values. A notification is used only when its StatusCode is Good and its
  SourceTimestamp (or ServerTimestamp, when the server sends no source time) is no
  older than max_age_s (default 300 s). Bad or Uncertain status, an old timestamp, or
  a value its signal_map rule rejects gives no reading, and self.quality says why.
  Monitored items use the StatusValueTimestamp trigger, so a server that restamps an
  unchanged value still sends it. Each notification gives one reading, stamped with the
  live minute read() hands it on. A value is never handed on again: the live layer
  measures staleness from the last real update, so a tag the server stopped updating
  (still Good, never restamped) gets SENSOR_STALE at stale_after_min, the same moment
  /api/state shows it UNKNOWN. The connector still watches the last good value of each
  signal: once its device timestamp (or its arrival time, when it came with none) is
  older than max_age_s, or stale_after_min live minutes (default 5, the blueprint's
  usual sensors.stale_after_min; set it to that value or less) have passed since it
  arrived, its quality turns "stale" until a new value comes. max_age_s is wall time,
  and at FREEBOARD_SPEED 0.25 the default 300 s is 75 live minutes, which is why the
  live-minute cap is there too.
- A good value's quality turns good when read() hands it on, not when it arrives, so an
  asset never shows the value from before a bad period as fresh while the new one waits.
- Hosts. The endpoint host must be one an operator allows (FREEBOARD_CONN_ALLOWED_HOSTS,
  see hosts.py; only this machine when it is not set), or the connector is not built.
- Session loss. When the keepalive or the watchdog fails, status() turns unavailable
  with the reason, every declared signal turns comm_lost and gives no reading, and
  the thread reconnects with Backoff and subscribes again. Nothing held from before
  the loss is read again.
- Writes. Only from send(), which the live layer calls only after a person confirms.
  send() writes the command's NodeId once, with the variable's own VariantType, within
  timeout_s. No session means nothing is sent. Nothing is queued or retried; when the
  write left but no answer came, send() returns uncertain_result(). Once send() has
  given up, the I/O loop never starts that write.
"""
from __future__ import annotations

import asyncio
import importlib
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from ..fieldmap import FieldMap
from . import signalmap as sm
from .base import Connector, Reading
from .hosts import check_host

POLICIES = ("Basic256Sha256", "Aes128Sha256RsaOaep", "Aes256Sha256RsaPss")
MODES = ("SignAndEncrypt", "Sign", "None")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
COMMAND_TYPES = ("SET_VALVE", "SET_PUMP", "CHOKE_WELL")
DEFAULT_MAX_AGE_S = 300.0
DEFAULT_HOLD_LIVE_MIN = 5.0     # live minutes a held value is read again (detect's default stale window)
COMMAND_WORDS = {"SET_VALVE": ("OPEN", "CLOSED"), "SET_PUMP": ("ON", "OFF")}


def _positive(config: dict, key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"config {key} must be a positive number, got {value!r}")
    return float(value)


def _short(e: BaseException, config: Optional[dict] = None) -> str:
    """An exception as a status line, with the value of every FREEBOARD_CONN_ variable
    the config names replaced by the variable's name. The password is never part of an
    asyncua error, and an endpoint with credentials in it is refused, but a file path
    from the environment can show up in one."""
    text = str(e).strip()
    for key, var in (config or {}).items():
        if isinstance(key, str) and key.endswith("_env") and var:
            value = sm.env_secret(config, key[:-4])
            if value and len(value) >= 3:
                text = _redact(text, value, f"${var}")
    name = type(e).__name__
    if not text:
        return name
    return f"{name}: {text[:160]}"


def _redact(text: str, value: str, tag: str) -> str:
    """text with every spelling of `value` an error message can carry replaced by `tag`:
    as written, as repr() shows it (an OSError prints its filename with doubled
    backslashes on Windows), and with either kind of slash."""
    forms = {value, repr(value)[1:-1], value.replace("\\", "/"), value.replace("/", "\\")}
    forms |= {f.replace("\\", "\\\\") for f in list(forms)}
    for form in sorted(forms, key=len, reverse=True):
        if len(form) >= 3:
            text = text.replace(form, tag)
    return text


class OpcUaConnector(Connector):
    kind = "opcua"

    def __init__(self, spec: dict, fmap: FieldMap, now: Callable[[], float]) -> None:
        super().__init__(spec, fmap, now)
        cfg = self.config
        sm.check_env_names(cfg)
        endpoint = cfg.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.startswith("opc.tcp://"):
            raise ValueError("config endpoint must be an opc.tcp:// URL, for example opc.tcp://10.0.0.5:4840")
        parts = urlsplit(endpoint)
        if "@" in parts.netloc:
            raise ValueError("config endpoint must not carry credentials; use username and password_env")
        if not parts.hostname:
            raise ValueError("config endpoint has no host")
        self.endpoint = endpoint
        self.host = parts.hostname
        self.policy = str(cfg.get("security_policy") or "Basic256Sha256")
        if self.policy not in POLICIES:
            raise ValueError(f"config security_policy must be one of {', '.join(POLICIES)}, got {self.policy!r}")
        self.mode = str(cfg.get("security_mode") or "SignAndEncrypt")
        if self.mode not in MODES:
            raise ValueError(f"config security_mode must be one of {', '.join(MODES)}, got {self.mode!r}")
        if self.mode == "None":
            if cfg.get("allow_insecure") is not True or self.host not in LOCAL_HOSTS:
                raise ValueError("security_mode None is allowed only with allow_insecure: true and an endpoint "
                                 "on 127.0.0.1 or localhost")
        else:
            for key in ("cert_path_env", "key_path_env"):
                if not cfg.get(key):
                    raise ValueError(f"security_mode {self.mode} needs config {key} (a FREEBOARD_CONN_ variable "
                                     f"holding a file path)")
        check_host(self.host, "config endpoint host")
        if (self.mode != "None" and self.host not in LOCAL_HOSTS and not cfg.get("server_cert_path_env")
                and cfg.get("allow_unpinned_server_cert") is not True):
            raise ValueError("an endpoint off this machine needs config server_cert_path_env (a FREEBOARD_CONN_ "
                             "variable holding the server certificate's path), so the session is encrypted to "
                             "the real server and not to whoever answers; set allow_unpinned_server_cert: true "
                             "only to accept any certificate the endpoint shows")
        self.username = cfg.get("username")
        if self.username is not None and (not isinstance(self.username, str) or not self.username):
            raise ValueError("config username must be a non-empty string")
        if self.username and not cfg.get("password_env"):
            raise ValueError("config username needs password_env (a FREEBOARD_CONN_ variable)")
        self.publish_ms = _positive(cfg, "publish_ms", 500)
        self.timeout_s = _positive(cfg, "timeout_s", 5)
        self.max_age_s = _positive(cfg, "max_age_s", DEFAULT_MAX_AGE_S)
        self.hold_live_min = _positive(cfg, "stale_after_min", DEFAULT_HOLD_LIVE_MIN)
        self.smap = sm.SignalMap({**spec, "config": {**cfg, "max_age_s": self.max_age_s}})

        self.ua, why = sm.import_optional("asyncua", "asyncua")
        # nodeid string -> [(asset_id, signal)]
        self.by_node: dict[str, list[tuple]] = {}
        for (aid, sig), addr in self.signals.items():
            self.by_node.setdefault(self._node_text(addr, f"signal {aid}.{sig}"), []).append((aid, sig))
        self.command_nodes: dict[tuple, str] = {}
        for (aid, typ), addr in self.commands.items():
            if typ not in COMMAND_TYPES:
                raise ValueError(f"command {aid}.{typ}: an OPC UA connector writes {', '.join(COMMAND_TYPES)}")
            self.command_nodes[(aid, typ)] = self._node_text(addr, f"command {aid}.{typ}")

        self.inbox = sm.Inbox()
        self.quality: dict[tuple, dict] = {}
        self._lock = threading.Lock()
        self._held: dict[tuple, tuple] = {}       # (aid, sig) -> (raw, source epoch s, arrival live min)
        self._stop = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client = None                       # set only while a session is up
        self._vtypes: dict[str, Any] = {}         # command nodeid -> VariantType, per session
        self._detail = why or "connecting"
        self._thread: Optional[threading.Thread] = None
        if self.ua is not None:
            self._thread = threading.Thread(target=self._run, name=f"opcua-{self.id}", daemon=True)
            self._thread.start()

    def _node_text(self, addr: Any, what: str) -> str:
        if not isinstance(addr, str) or not addr.strip():
            raise ValueError(f"{what} needs an address, a NodeId string such as ns=2;s=TANK_6.Level")
        if self.ua is not None:
            try:
                return self.ua.ua.NodeId.from_string(addr.strip()).to_string()
            except Exception:  # noqa: BLE001
                raise ValueError(f"{what}: {addr!r} is not a NodeId string such as ns=2;s=TANK_6.Level") from None
        return addr.strip()

    # ---------- the interface ----------
    def read(self) -> list[Reading]:
        out: list[Reading] = []
        with self._lock:
            fresh = {(aid, sig): value for aid, sig, value in self.inbox.drain()}
            connected = self._client is not None
            for key, value in fresh.items():
                out.append(self.reading(key[0], key[1], value))
                self._mark(key, sm.GOOD, "")          # good from the moment it is handed on
            if not connected and not self._subscribing:
                self._held.clear()
            now_min = self._live_min()
            for key, (raw, src, arrived) in list(self._held.items()):
                if key in fresh:
                    continue
                if now_min - arrived > self.hold_live_min:
                    self._held.pop(key, None)
                    self._mark(key, sm.STALE, f"no new value from the server for {now_min - arrived:.1f} live "
                                              f"minutes (stale_after_min {self.hold_live_min:g})")
                    continue
                # Checked, never handed on again: a re-read would carry a fresh t and keep
                # the detector from seeing that the server stopped updating.
                mapped = self.smap.apply(key[0], key[1], raw, age_s=self._age(src))
                if not mapped.good:
                    self._held.pop(key, None)
                    self._mark(key, mapped.quality, mapped.detail)
        return out

    def send(self, command: dict) -> dict:
        try:
            typ, aid, value = sm.normalize(command)
        except ValueError as e:
            return {"ok": False, "detail": f"{self.id}: {e}"}
        node = self.command_nodes.get((aid, typ))
        if node is None:
            return {"ok": False, "detail": f"{self.id} has no {typ} address for {aid}; nothing was sent."}
        with self._lock:
            client, loop = self._client, self._loop
        if client is None or loop is None:
            return {"ok": False, "detail": f"{self.id} has no OPC UA session ({self._detail}); nothing was sent."}
        # "sent" and "given_up" change only under state["lock"], so the I/O loop either
        # starts the write before send() gives up (uncertain) or never starts it.
        state = {"sent": False, "given_up": False, "lock": threading.Lock()}
        try:
            fut = asyncio.run_coroutine_threadsafe(self._write(client, node, typ, value, state), loop)
        except RuntimeError:
            return {"ok": False, "detail": f"{self.id} has no OPC UA session; nothing was sent."}
        try:
            return fut.result(timeout=self.timeout_s)
        except Exception as e:  # noqa: BLE001 - a timeout, or the loop went away
            fut.cancel()
            with state["lock"]:
                state["given_up"] = True
                sent = state["sent"]
            if sent:
                return sm.uncertain_result()
            why = _short(e, self.config)
            return {"ok": False, "detail": f"{self.id}: the write was not sent ({why}); nothing was sent."}

    def status(self) -> dict:
        with self._lock:
            up = self._client is not None
        return {"id": self.id, "kind": self.kind, "available": up, "detail": self._detail}

    def close(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)    # wake the loop
            except RuntimeError:
                pass
        if self._thread is not None:
            sm.close_later(lambda: self._thread.join(timeout=10), name=f"opcua-close-{self.id}")

    # ---------- values ----------
    @staticmethod
    def _epoch(ts: Any) -> Optional[float]:
        if not isinstance(ts, datetime):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()

    @staticmethod
    def _age(src: Optional[float]) -> Optional[float]:
        return None if src is None else time.time() - src

    def _live_min(self) -> float:
        try:
            return float(self.now())
        except Exception:  # noqa: BLE001
            return 0.0

    def _mark(self, key: tuple, quality: str, detail: str) -> None:
        self.quality[key] = {"quality": quality, "detail": detail, "t": round(self.now(), 3)}

    def _quality_of(self, status: Any) -> tuple[str, str]:
        try:
            if status is None or status.is_good():
                return sm.GOOD, ""
            name = getattr(status, "name", None) or hex(int(status.value))
            if status.is_uncertain():
                return sm.UNCERTAIN, f"the server reports {name}"
            comm = ("BadNoCommunication", "BadCommunicationError", "BadNotConnected", "BadServerNotConnected",
                    "BadWaitingForInitialData", "BadDeviceFailure")
            return (sm.COMM_LOST if name in comm else sm.BAD), f"the server reports {name}"
        except Exception:  # noqa: BLE001
            return sm.BAD, "unreadable StatusCode"

    def notify(self, node_text: str, datavalue: Any) -> None:
        """One data change from the server: map it for every signal on that node."""
        status = getattr(datavalue, "StatusCode", None)
        q, why = self._quality_of(status)
        src = self._epoch(getattr(datavalue, "SourceTimestamp", None))
        if src is None:
            src = self._epoch(getattr(datavalue, "ServerTimestamp", None))
        variant = getattr(datavalue, "Value", None)
        raw = getattr(variant, "Value", None)
        with self._lock:
            if self._client is None and not self._subscribing:
                return
            for aid, sig in self.by_node.get(node_text, []):
                mapped = self.smap.apply(aid, sig, raw, quality=q, age_s=self._age(src))
                self.inbox.put(aid, sig, mapped)
                if mapped.good:
                    # With no timestamp at all, the value is as old as its arrival, so a
                    # held copy still runs out after max_age_s. Its quality turns good in read().
                    self._held[(aid, sig)] = (raw, time.time() if src is None else src, self._live_min())
                else:
                    self._held.pop((aid, sig), None)
                    self._mark((aid, sig), mapped.quality, why if q != sm.GOOD and why else mapped.detail)

    def _lost(self, why: str) -> None:
        with self._lock:
            self._client = None
            self._subscribing = False
            self._vtypes = {}
            self._held.clear()
            self.inbox.clear()
            for key in self.signals:
                self._mark(key, sm.COMM_LOST, why)
            self._detail = why

    # ---------- writes (on the I/O loop) ----------
    async def _write(self, client: Any, node_text: str, typ: str, value: Any, state: dict) -> dict:
        ua = self.ua.ua
        with self._lock:
            if client is not self._client:
                return {"ok": False, "detail": f"{self.id}: the OPC UA session ended; nothing was sent."}
        node = client.get_node(node_text)
        vtype = self._vtypes.get(node_text)
        try:
            if vtype is None:
                vtype = await asyncio.wait_for(node.read_data_type_as_variant_type(), self.timeout_s)
                self._vtypes[node_text] = vtype
            wire = self._wire_value(vtype, typ, value)
        except ValueError as e:
            return {"ok": False, "detail": f"{self.id}: {e}; nothing was sent."}
        except Exception as e:  # noqa: BLE001
            why = _short(e, self.config)
            return {"ok": False,
                    "detail": f"{self.id}: could not read the type of {node_text} ({why}); nothing was sent."}
        with state["lock"]:
            if state["given_up"]:
                return {"ok": False, "detail": f"{self.id}: send() had already given up; nothing was sent."}
            state["sent"] = True
        try:
            await node.write_value(ua.DataValue(ua.Variant(wire, vtype)))
        except asyncio.CancelledError:
            raise
        except self.ua.ua.UaStatusCodeError as e:
            why = _short(e, self.config)
            return {"ok": False, "detail": f"{self.id}: the server refused the write to {node_text} ({why})."}
        except Exception:  # noqa: BLE001 - the link broke while waiting for the answer
            return sm.uncertain_result()
        return {"ok": True, "detail": f"{self.id} wrote {wire!r} to {node_text}."}

    def _wire_value(self, vtype: Any, typ: str, value: Any) -> Any:
        """The normalized command value in the variable's own type."""
        VT = self.ua.ua.VariantType
        name = getattr(vtype, "name", str(vtype))
        if vtype == VT.Boolean:
            if typ == "CHOKE_WELL" and value not in (0.0, 1.0):
                raise ValueError(f"{name} variable can't take a choke of {value:g}")
            return bool(value)
        if vtype in (VT.Float, VT.Double):
            return float(value)
        if vtype in (VT.SByte, VT.Byte, VT.Int16, VT.UInt16, VT.Int32, VT.UInt32, VT.Int64, VT.UInt64):
            x = float(value)
            if x != int(x):
                raise ValueError(f"{name} variable can't take a choke of {x:g}")
            return int(x)
        if vtype == VT.String:
            if typ in COMMAND_WORDS:
                on, off = COMMAND_WORDS[typ]
                return on if value else off
            return f"{float(value):g}"
        raise ValueError(f"{name} variable is not a type this connector writes")

    # ---------- the I/O thread ----------
    _subscribing = False

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._loop = None
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < end:
            await asyncio.sleep(min(0.1, max(0.0, end - time.monotonic())))

    async def _main(self) -> None:
        backoff = sm.Backoff(initial_s=0.5, max_s=30.0)
        while not self._stop.is_set():
            client = None
            try:
                client = await self._connect()
                backoff.reset()
                while not self._stop.is_set():
                    await asyncio.sleep(0.1)
                    await client.check_connection()
                    if self._client is not client:
                        raise _SessionEnded()      # _lost() already ran, with the reason
            except _SessionEnded:
                pass
            except Exception as e:  # noqa: BLE001
                if not self._stop.is_set():
                    why = _short(e, self.config)
                    self._lost(f"OPC UA session to {self.endpoint} lost or refused ({why}); reconnecting")
            if client is not None:
                with self._lock:
                    if self._client is client:
                        self._client = None
                try:
                    await asyncio.wait_for(client.disconnect(), 1.0)
                except BaseException:  # noqa: BLE001
                    try:
                        client.disconnect_socket()
                    except Exception:  # noqa: BLE001
                        pass
            if self._stop.is_set():
                break
            await self._sleep(backoff.next_delay())
        self._lost("closed")

    async def _connect(self) -> Any:
        asyncua = self.ua
        ua = asyncua.ua
        missing = sm.missing_env(self.config)
        if missing:
            raise RuntimeError(f"environment variable {missing} is not set")
        client = asyncua.Client(self.endpoint, timeout=self.timeout_s, watchdog_intervall=1.0)
        client.session_timeout = 30000
        if self.mode != "None":
            cert = sm.env_secret(self.config, "cert_path")
            key = sm.env_secret(self.config, "key_path")
            server_cert = sm.env_secret(self.config, "server_cert_path") if self.config.get(
                "server_cert_path_env") else None
            uri = await self._cert_uri(cert)
            if uri:
                client.application_uri = uri
            policies = importlib.import_module("asyncua.crypto.security_policies")
            policy = getattr(policies, f"SecurityPolicy{self.policy}")
            mode = getattr(ua.MessageSecurityMode, self.mode)
            await asyncio.wait_for(client.set_security(policy, cert, key, server_certificate=server_cert,
                                                       mode=mode), self.timeout_s * 2)
        if self.username:
            client.set_user(self.username)
            client.set_password(sm.env_secret(self.config, "password") or "")
        await asyncio.wait_for(client.connect(), self.timeout_s * 2)
        try:
            handler = _Handler(self)
            with self._lock:
                self._subscribing = True
            sub = await client.create_subscription(self.publish_ms, handler)
            nodes = [client.get_node(n) for n in self.by_node]
            results = await self._subscribe(sub, nodes) if nodes else []
            bad = []
            for text, res in zip(self.by_node, results):
                if isinstance(res, ua.StatusCode):
                    bad.append(text)
                    with self._lock:
                        for key in self.by_node[text]:
                            self._mark(key, sm.BAD, f"could not subscribe to {text}: {res.name}")
            with self._lock:
                self._client = client
                self._subscribing = False
                self._vtypes = {}
                self._detail = (f"connected to {self.endpoint} ({self.policy if self.mode != 'None' else 'no'} "
                                f"security, {self.mode}), {len(nodes) - len(bad)} of {len(nodes)} nodes subscribed"
                                + (f"; not subscribed: {', '.join(bad)}" if bad else "")
                                + ("; server certificate not pinned (set server_cert_path_env)"
                                   if self.mode != "None" and not self.config.get("server_cert_path_env") else ""))
            return client
        except BaseException:
            with self._lock:
                self._subscribing = False
            try:
                await asyncio.wait_for(client.disconnect(), 1.0)
            except BaseException:  # noqa: BLE001
                pass
            raise

    async def _subscribe(self, sub: Any, nodes: list) -> list:
        """Monitor every node with the StatusValueTimestamp trigger, so a value the server
        restamps without changing still arrives and its age stays the server's latest
        stamp. Nodes whose server refuses that filter are monitored with the default
        trigger (value or status changes only); their values then run out after
        max_age_s unless they change. One result per node: a handle or a StatusCode."""
        ua = self.ua.ua
        flt = ua.DataChangeFilter(Trigger=ua.DataChangeTrigger.StatusValueTimestamp)
        results = list(await sub._subscribe(nodes, ua.AttributeIds.Value, flt))
        retry = [i for i, res in enumerate(results) if isinstance(res, ua.StatusCode)]
        if retry:
            again = await sub.subscribe_data_change([nodes[i] for i in retry])
            for i, res in zip(retry, again if isinstance(again, list) else [again]):
                results[i] = res
        return results

    async def _cert_uri(self, path: Optional[str]) -> Optional[str]:
        """The application URI in the client certificate, so the session's URI matches it."""
        try:
            from cryptography import x509
            uacrypto = importlib.import_module("asyncua.crypto.uacrypto")
            cert = await uacrypto.load_certificate(path)
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            uris = san.value.get_values_for_type(x509.UniformResourceIdentifier)
            return uris[0] if uris else None
        except Exception:  # noqa: BLE001 - set_security reports a bad certificate itself
            return None


class _SessionEnded(Exception):
    """The subscription handler already marked the session lost."""


class _Handler:
    """asyncua subscription handler; runs on the connector's I/O loop."""

    def __init__(self, conn: OpcUaConnector) -> None:
        self.conn = conn

    def datachange_notification(self, node: Any, val: Any, data: Any) -> None:
        try:
            self.conn.notify(node.nodeid.to_string(), data.monitored_item.Value)
        except Exception:  # noqa: BLE001
            pass

    def status_change_notification(self, status: Any) -> None:
        """The server says the subscription itself failed: treat it as a lost link."""
        try:
            code = getattr(status, "Status", None)
            if code is not None and not code.is_good():
                self.conn._lost(f"OPC UA subscription on {self.conn.endpoint} ended ({code.name}); reconnecting")
        except Exception:  # noqa: BLE001
            pass
