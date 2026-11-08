"""A fake OPC UA server for tests and the demo. No real equipment.

It listens on 127.0.0.1 only, with Basic256Sha256 SignAndEncrypt and username login
(no anonymous sessions). Its server certificate, and a client certificate and key for
the connector, are generated at start into a temporary directory and never written
anywhere else. The password is random unless given.

Variables (namespace 2, string NodeIds):

    TANK_3.Level, TANK_6.Level        Double, percent; filled slowly, drained while the pump runs
    PUMP_3.Running, PUMP_6.Running    Boolean; follows PUMP_n.Command
    PUMP_3.Flow, PUMP_6.Flow          Double, bbl/hr
    PUMP_3.Command, PUMP_6.Command    Boolean, writable: True starts the pump
    VALVE_D_6.Position                Boolean, True is open; follows VALVE_D_6.Command
    VALVE_D_6.Command                 Boolean, writable: True opens
    WELL_1.Flow                       Double, bbl/hr; WELL_1.Choke times the full rate
    WELL_1.Choke                      Double, writable, 0 to 1

    python -m backend.fakes.opcua [--port 4840]

takes the login password from FREEBOARD_CONN_OPCUA_PASSWORD (set it first; the
connector reads the same variable) and prints the endpoint, the username, and the
certificate paths to put in the connector's environment. It never prints the
password. In a test:

    fake = start()                       # handle; fake.endpoint, fake.client_cert, fake.password
    fake.set("TANK_6.Level", 42.0)       # change a value
    fake.set_quality("TANK_6.Level", "bad")
    fake.set_age("TANK_6.Level", 900)    # stamp its SourceTimestamp 15 minutes ago
    fake.writes                          # [{"node", "name", "value", "variant", "t"}] from clients
    fake.write_delay_s = 3               # answer client writes late (the write still lands)
    fake.stop()
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import ipaddress
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

USERNAME = "freeboard"
APP_URI = "urn:freeboard:fake-opcua"
CLIENT_URI = "urn:freeboard:connector"
NS_URI = "urn:freeboard:field"
PASSWORD_ENV = "FREEBOARD_CONN_OPCUA_PASSWORD"

READS = {
    "TANK_3.Level": ("Double", 62.0), "TANK_6.Level": ("Double", 48.0),
    "PUMP_3.Running": ("Boolean", True), "PUMP_6.Running": ("Boolean", True),
    "PUMP_3.Flow": ("Double", 220.0), "PUMP_6.Flow": ("Double", 220.0),
    "VALVE_D_6.Position": ("Boolean", True),
    "WELL_1.Flow": ("Double", 38.0),
}
COMMANDS = {
    "PUMP_3.Command": ("Boolean", True), "PUMP_6.Command": ("Boolean", True),
    "VALVE_D_6.Command": ("Boolean", True),
    "WELL_1.Choke": ("Double", 1.0),
}
PUMP_RATE = 220.0
WELL_RATE = 38.0
QUALITY = {"good": "Good", "bad": "BadSensorFailure", "uncertain": "UncertainLastUsableValue",
           "comm_lost": "BadNoCommunication"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_certs(workdir: Path) -> dict:
    """Self-signed server and client certificates (DER) and keys (PEM) in workdir,
    reused when they are already there so a restarted fake keeps its identity."""
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    from asyncua.crypto import cert_gen

    out = {}
    for who, uri, use in (("server", APP_URI, [ExtendedKeyUsageOID.SERVER_AUTH]),
                          ("client", CLIENT_URI, [ExtendedKeyUsageOID.CLIENT_AUTH])):
        cert, key = workdir / f"{who}_cert.der", workdir / f"{who}_key.pem"
        if not (cert.exists() and key.exists()):
            pk = cert_gen.generate_private_key()
            names = [x509.UniformResourceIdentifier(uri), x509.DNSName("localhost"),
                     x509.DNSName(socket.gethostname()), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            c = cert_gen.generate_self_signed_app_certificate(pk, uri, {"organizationName": "Freeboard fake"}, names,
                                                              extended=use, days=30)
            from cryptography.hazmat.primitives.serialization import Encoding
            key.write_bytes(cert_gen.dump_private_key_as_pem(pk))
            cert.write_bytes(c.public_bytes(Encoding.DER))
        out[f"{who}_cert"], out[f"{who}_key"] = str(cert), str(key)
    return out


class _Users:
    """Username and password only; anonymous and wrong passwords are refused. Writes still
    reach only the command variables, the only ones marked writable."""

    def __init__(self, username: str, password: str) -> None:
        self.username, self.password = username, password

    def get_user(self, iserver, username=None, password=None, certificate=None):
        from asyncua.server.users import User, UserRole
        if username == self.username and password is not None and secrets.compare_digest(
                str(password), self.password):
            return User(role=UserRole.Admin, name=username)   # asyncua lets only Admin write
        return None


class FakeOpcUa:
    """A running fake server on its own thread and asyncio loop."""

    def __init__(self, port: Optional[int] = None, workdir: Optional[str] = None, password: Optional[str] = None,
                 fill_pct_per_s: float = 0.02, tick_s: float = 0.2, write_delay_s: float = 0.0) -> None:
        self.port = port or free_port()
        self.endpoint = f"opc.tcp://127.0.0.1:{self.port}/freeboard/"
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="freeboard-opcua-"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.username = USERNAME
        self.password = password or secrets.token_urlsafe(16)
        self.fill_pct_per_s = fill_pct_per_s
        self.tick_s = tick_s
        self.write_delay_s = write_delay_s
        paths = make_certs(self.workdir)
        self.server_cert, self.server_key = paths["server_cert"], paths["server_key"]
        self.client_cert, self.client_key = paths["client_cert"], paths["client_key"]
        self.values = {k: v for k, (_, v) in {**READS, **COMMANDS}.items()}
        self.quality = {k: "good" for k in self.values}
        self.age_s = {k: 0.0 for k in self.values}        # how old the stamped SourceTimestamp is
        self.writes: list[dict] = []
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopping: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, name="fake-opcua", daemon=True)

    # ---------- control from tests ----------
    def start(self, timeout: float = 20.0) -> "FakeOpcUa":
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("fake OPC UA server did not start in time")
        if self._error:
            raise RuntimeError(f"fake OPC UA server failed to start: {self._error}")
        return self

    def set(self, name: str, value: Any) -> None:
        with self._lock:
            self.values[name] = value

    def set_quality(self, name: str, quality: str) -> None:
        """good, bad, uncertain, comm_lost, or an OPC UA StatusCode name."""
        with self._lock:
            self.quality[name] = quality

    def set_age(self, name: str, seconds: float) -> None:
        """Stamp the variable's SourceTimestamp this many seconds in the past."""
        with self._lock:
            self.age_s[name] = float(seconds)

    def node(self, name: str) -> str:
        return f"ns=2;s={name}"

    def stop(self, timeout: float = 10.0) -> None:
        loop, ev = self._loop, self._stopping
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass
        self._thread.join(timeout)

    # ---------- the server ----------
    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except BaseException as e:  # noqa: BLE001
            self._error = e
            self._ready.set()
        finally:
            loop.close()

    async def _serve(self) -> None:
        from asyncua import Server, ua
        from asyncua.common.callback import CallbackType

        self._stopping = asyncio.Event()
        server = Server(user_manager=_Users(self.username, self.password))
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name("Freeboard fake OPC UA")
        await server.set_application_uri(APP_URI)
        server.set_security_policy([ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt])
        server.set_security_IDs(["Username"])
        await server.load_certificate(self.server_cert)
        await server.load_private_key(self.server_key)
        idx = await server.register_namespace(NS_URI)
        folder = await server.nodes.objects.add_folder(idx, "Field")
        self._nodes = {}
        for name, (vt, value) in {**READS, **COMMANDS}.items():
            var = await folder.add_variable(ua.NodeId(name, idx), name, ua.Variant(value, getattr(ua.VariantType, vt)))
            if name in COMMANDS:
                await var.set_writable()
            self._nodes[name] = (var, getattr(ua.VariantType, vt))
        by_id = {var.nodeid: name for name, (var, _) in self._nodes.items()}

        def on_write(event, _dispatcher) -> None:
            if not event.is_external:
                return
            for wv, res in zip(event.request_params.NodesToWrite, event.response_params or []):
                name = by_id.get(wv.NodeId)
                if name is None or not res.is_good():
                    continue
                value = wv.Value.Value.Value
                with self._lock:
                    self.values[name] = value
                    self.writes.append({"node": wv.NodeId.to_string(), "name": name, "value": value,
                                        "variant": wv.Value.Value.VariantType.name, "t": time.time()})

        async def slow_answer(event, _dispatcher) -> None:
            if event.is_external and self.write_delay_s > 0:
                await asyncio.sleep(self.write_delay_s)

        server.subscribe_server_callback(CallbackType.PostWrite, on_write)
        server.subscribe_server_callback(CallbackType.PreWrite, slow_answer)
        async with server:
            self._ready.set()
            last = time.monotonic()
            while not self._stopping.is_set():
                try:
                    await asyncio.wait_for(self._stopping.wait(), self.tick_s)
                except asyncio.TimeoutError:
                    pass
                now = time.monotonic()
                self._model(now - last)
                last = now
                await self._publish(ua)

    def _model(self, dt_s: float) -> None:
        """A slow fill: wells fill the tanks, a running pump drains a little faster than
        it fills only when the valve is open."""
        with self._lock:
            v = self.values
            for n in ("3", "6"):
                v[f"PUMP_{n}.Running"] = bool(v[f"PUMP_{n}.Command"])
                v[f"PUMP_{n}.Flow"] = PUMP_RATE if v[f"PUMP_{n}.Running"] else 0.0
            v["VALVE_D_6.Position"] = bool(v["VALVE_D_6.Command"])
            v["WELL_1.Flow"] = WELL_RATE * float(v["WELL_1.Choke"])
            for n in ("3", "6"):
                pumping = v[f"PUMP_{n}.Running"] and (n != "6" or v["VALVE_D_6.Position"])
                rate = self.fill_pct_per_s * (0.5 if pumping else 1.0)
                v[f"TANK_{n}.Level"] = min(100.0, float(v[f"TANK_{n}.Level"]) + rate * dt_s)

    async def _publish(self, ua) -> None:
        with self._lock:
            snap = {k: (self.values[k], self.quality[k], self.age_s[k]) for k in READS}
        now = dt.datetime.now(dt.timezone.utc)
        for name, (value, q, age) in snap.items():
            var, vt = self._nodes[name]
            code = getattr(ua.StatusCodes, QUALITY.get(q, q), ua.StatusCodes.Bad)
            dv = ua.DataValue(ua.Variant(value, vt), StatusCode_=ua.StatusCode(code),
                              SourceTimestamp=now - dt.timedelta(seconds=age), ServerTimestamp=now)
            await var.write_value(dv)


def start(port: Optional[int] = None, workdir: Optional[str] = None, password: Optional[str] = None,
          **kw: Any) -> FakeOpcUa:
    """Start a fake server in this process and return its handle (call stop() when done).
    Pass the port and workdir of a stopped fake to bring the same server back."""
    return FakeOpcUa(port=port, workdir=workdir, password=password, **kw).start()


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake OPC UA server for Freeboard (127.0.0.1 only).")
    ap.add_argument("--port", type=int, default=0, help="TCP port (default: a free one)")
    ap.add_argument("--workdir", default=None, help="where to keep the generated certificates (default: a temp dir)")
    args = ap.parse_args()
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        raise SystemExit(f"set {PASSWORD_ENV} to the password the fake should accept (the connector reads it too)")
    fake = start(port=args.port or None, workdir=args.workdir, password=password)
    print(f"fake OPC UA server at {fake.endpoint} (Basic256Sha256 SignAndEncrypt, username {fake.username})")
    print("set these for the connector, then start the backend:")
    print(f"  FREEBOARD_CONN_OPCUA_CERT={fake.client_cert}")
    print(f"  FREEBOARD_CONN_OPCUA_KEY={fake.client_key}")
    print(f"  FREEBOARD_CONN_OPCUA_SERVER_CERT={fake.server_cert}")
    print(f"  {PASSWORD_ENV} (already set; the password is not printed)")
    print("Ctrl+C stops it.", flush=True)
    try:
        while fake._thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()
        if args.workdir is None:
            shutil.rmtree(fake.workdir, ignore_errors=True)    # the generated keys go with it


if __name__ == "__main__":
    main()
