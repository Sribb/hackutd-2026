"""A fake PI Web API server for tests and the demo. 127.0.0.1 only; no real PI System.

It serves the two endpoints the piwebapi connector uses, behind Basic auth:

    GET /piwebapi/streamsets/value?webId=...&webId=...       current values
    GET /piwebapi/streams/{webId}/recorded?startTime=*-60m    archive values

Any other method on any path is recorded in `writes` and refused, so a test can prove
the read-only connector never writes.

    python -m backend.fakes.piwebapi [--port 0] [--user freeboard] [--password pi-fake-pass]

prints its base URL, user, and the WebIds it serves. In process:

    fake = start()                       # free port; fake.base_url, fake.username, fake.password
    fake.set(TANK_6_LEVEL, 55.0)         # a new value, stamped now
    fake.set_flags(TANK_6_LEVEL, good=False, questionable=True, substituted=True)
    fake.set_age(TANK_6_LEVEL, 900)      # stamp the current value 900 s ago
    fake.set_digital(TANK_6_LEVEL, "Bad Input")
    fake.set_recorded(TANK_6_LEVEL, [(age_s, value), ...])
    fake.stop(); fake = start(port=fake.port)    # the same address again
"""
from __future__ import annotations

import argparse
import base64
import secrets
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

TANK_6_LEVEL = "F1DPfbTANK6LEVEL"
PUMP_6_RUNNING = "F1DPfbPUMP6RUNNING"
PUMP_6_FLOW = "F1DPfbPUMP6FLOW"
VALVE_D_6_POSITION = "F1DPfbVALVED6POSITION"

DEFAULT_POINTS = {
    TANK_6_LEVEL: ("TANK_6.Level", 52.0),
    PUMP_6_RUNNING: ("PUMP_6.Running", True),
    PUMP_6_FLOW: ("PUMP_6.Flow", 180.0),
    VALVE_D_6_POSITION: ("VALVE_D_6.Position", {"Name": "Open", "Value": 1, "IsSystem": False}),
}


def pi_time(unix_s: float) -> str:
    """Unix seconds as a PI timestamp, seven fraction digits like the real server."""
    dt = datetime.fromtimestamp(unix_s, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}0Z"


def _since(start: Optional[str], now: float) -> float:
    """startTime as Unix seconds: "*-<n>m", "*-<n>h", "*-<n>s", or "*" (now)."""
    s = (start or "*-1d").strip()
    if s == "*":
        return now
    if s.startswith("*-") and s[-1] in "smhd":
        try:
            n = float(s[2:-1])
        except ValueError:
            return now - 86400
        return now - n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[s[-1]]
    return now - 86400


class Point:
    def __init__(self, name: str, value: Any) -> None:
        self.name = name
        self.value = value
        self.stamp = time.time()
        self.age_s: Optional[float] = None    # when set, the value is always this old
        self.good, self.questionable, self.substituted = True, False, False
        self.recorded: list[tuple[float, Any]] = []   # (unix_s, value), oldest first

    def as_pi(self) -> dict:
        stamp = time.time() - self.age_s if self.age_s is not None else self.stamp
        return {"Timestamp": pi_time(stamp), "Value": self.value, "UnitsAbbreviation": "",
                "Good": self.good, "Questionable": self.questionable, "Substituted": self.substituted,
                "Annotated": False}


class FakePiWebApi:
    """One running fake server. Change values with set*(); `writes` lists every non-GET
    request; `requests` counts the GETs served."""

    def __init__(self, port: int = 0, username: str = "freeboard", password: Optional[str] = None,
                 points: Optional[dict] = None) -> None:
        self.username = username
        self.password = password or "pi-fake-" + secrets.token_hex(6)
        self.lock = threading.Lock()
        self.points = {w: Point(n, v) for w, (n, v) in (points or DEFAULT_POINTS).items()}
        self.writes: list[dict] = []
        self.requests = 0
        now = time.time()
        for p in self.points.values():
            if isinstance(p.value, (int, float)) and not isinstance(p.value, bool):
                p.recorded = [(now - 60 * k, float(p.value) - 0.1 * k) for k in range(60, 0, -1)]
        self.app = self._app()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform != "win32":   # on Windows this flag would let two servers share a port
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.port = self.sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}/piwebapi"
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning",
                                lifespan="off", access_log=False, ws="none")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]},
                                       name=f"fake-piwebapi-{self.port}", daemon=True)

    # ---------- the HTTP side ----------
    def _authorized(self, request: Request) -> bool:
        want = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return secrets.compare_digest(request.headers.get("authorization", ""), want)

    def _app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/piwebapi/streamsets/value")
        async def streamset(request: Request):
            if not self._authorized(request):
                return JSONResponse({"Errors": ["Authorization required"]}, status_code=401,
                                    headers={"WWW-Authenticate": 'Basic realm="fake-pi"'})
            items = []
            with self.lock:
                self.requests += 1
                for w in request.query_params.getlist("webId"):
                    p = self.points.get(w)
                    if p is None:
                        items.append({"WebId": w, "Errors": [f"Unknown WebId {w}"]})
                    else:
                        items.append({"WebId": w, "Name": p.name, "Path": f"\\\\FAKE\\{p.name}",
                                      "Value": p.as_pi()})
            return {"Links": {}, "Items": items}

        @app.get("/piwebapi/streams/{webid}/recorded")
        async def recorded(webid: str, request: Request):
            if not self._authorized(request):
                return JSONResponse({"Errors": ["Authorization required"]}, status_code=401)
            now = time.time()
            since = _since(request.query_params.get("startTime"), now)
            with self.lock:
                self.requests += 1
                p = self.points.get(webid)
                if p is None:
                    return JSONResponse({"Errors": [f"Unknown WebId {webid}"]}, status_code=404)
                items = [{"Timestamp": pi_time(t), "Value": v, "UnitsAbbreviation": "",
                          "Good": not (isinstance(v, dict) and v.get("IsSystem")),
                          "Questionable": False, "Substituted": False, "Annotated": False}
                         for t, v in p.recorded if since <= t <= now]
            return {"Links": {}, "Items": items}

        @app.api_route("/{path:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
        async def refuse(path: str, request: Request):
            body = (await request.body())[:2000].decode("utf-8", "replace")
            with self.lock:
                self.writes.append({"method": request.method, "path": "/" + path, "body": body})
            return JSONResponse({"Errors": ["This fake is read only"]}, status_code=405)

        return app

    # ---------- control ----------
    def start(self, timeout: float = 10.0) -> "FakePiWebApi":
        self.thread.start()
        end = time.monotonic() + timeout
        while not self.server.started:
            if time.monotonic() > end or not self.thread.is_alive():
                self.stop()
                raise RuntimeError("the fake PI Web API server did not start")
            time.sleep(0.02)
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join(timeout)
        try:
            self.sock.close()
        except OSError:
            pass

    def set(self, webid: str, value: Any) -> None:
        """A new current value, stamped now and Good."""
        with self.lock:
            p = self.points[webid]
            p.value, p.stamp = value, time.time()

    def set_flags(self, webid: str, good: bool = True, questionable: bool = False,
                  substituted: bool = False) -> None:
        with self.lock:
            p = self.points[webid]
            p.good, p.questionable, p.substituted = good, questionable, substituted

    def set_age(self, webid: str, age_s: Optional[float]) -> None:
        """Stamp the current value age_s seconds ago on every request; None stamps it
        when it was last set()."""
        with self.lock:
            self.points[webid].age_s = age_s

    def set_digital(self, webid: str, name: str, is_system: bool = True) -> None:
        """A digital state as the value, such as a system state "Bad Input"."""
        self.set(webid, {"Name": name, "Value": 307, "IsSystem": is_system})

    def set_recorded(self, webid: str, series: list) -> None:
        """The archive for a point: [(age_s, value), ...], any order."""
        now = time.time()
        with self.lock:
            self.points[webid].recorded = sorted((now - float(a), v) for a, v in series)


def start(port: int = 0, username: str = "freeboard", password: Optional[str] = None,
          points: Optional[dict] = None) -> FakePiWebApi:
    """Start a fake on 127.0.0.1 (a free port unless one is given) and return its handle."""
    return FakePiWebApi(port=port, username=username, password=password, points=points).start()


def main() -> None:
    ap = argparse.ArgumentParser(description="Fake PI Web API on 127.0.0.1 (no real PI System).")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--user", default="freeboard")
    ap.add_argument("--password", default="pi-fake-pass",
                    help="the Basic auth password; put the same value in FREEBOARD_CONN_PI_PASSWORD")
    args = ap.parse_args()
    fake = start(port=args.port, username=args.user, password=args.password)
    print(f"fake PI Web API at {fake.base_url} (user {fake.username})", flush=True)
    for w, p in fake.points.items():
        print(f"  {p.name:22s} webId {w}", flush=True)
    try:
        while fake.thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()


if __name__ == "__main__":
    main()
