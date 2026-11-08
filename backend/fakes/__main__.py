"""Start every fake the connectors demo needs, on 127.0.0.1 only. No real equipment.

    python -m backend.fakes [--opcua-port 4840] [--sparkplug-port 1886] [--workdir DIR]
                            [--backend http://127.0.0.1:8000]

It starts the fake OPC UA server (backend/fakes/opcua.py) and a fake Sparkplug B edge
node with its own MQTT broker (backend/fakes/sparkplug.py), the two connectors that
backend/data/samples/connectors_demo.json declares. Then it prints the environment
variables to export in the backend's shell before starting it, with
FREEBOARD_SCENARIO=backend/data/samples/connectors_demo.json so the backend starts on the
demo blueprint. Started on any other field, the backend would run that field's own story
first (scenario/field.json trips PUMP_3 at 01:47 too), and activating the demo blueprint
later keeps the live clock, so the console would show two PUMP_3 trips. The ports in the
demo blueprint are 4840 and 1886; with other ports, change the blueprint to match.

The OPC UA login password comes from FREEBOARD_CONN_OPCUA_PASSWORD when it is set, and
is then not printed. When it is not set, the fake makes up a throwaway password for this
run and prints it in the export line, since it opens nothing but this local fake.
Certificates go to --workdir, or to a temp dir that is deleted when this stops.
Ctrl+C stops everything.

The story. The fakes play the demo blueprint's night on the backend's own live clock:
about once a second they read `t` from GET {backend}/api/live/state and move by the live
minutes it moved. So a speed change (POST /api/live/sim), a pause, or an advance moves
the fake tanks exactly as far as the detector expects. While the backend can't be
reached the story waits; nothing moves on wall time. Nothing moves either until the
backend's Sparkplug host comes online on the broker, which happens when the backend
starts on the demo blueprint (or when it is activated). That moment is live minute 0.
Until then the OPC UA fake answers every variable with BadWaitingForInitialData, so the
console shows the TANK_6 line UNKNOWN rather than a running pump over a tank that isn't
moving. Then:
- TANK_3 starts at the blueprint's initial level (71.76 %) and TANK_6 at its own (50 %).
- A running pump with its valve open drains its tank at (wells in - pump rate), which is
  what the detector expects, so both lines start calm.
- At the blueprint's PUMP_3 TRIP fault (at_min 3, so 01:47 from a 01:44 start), the
  edge node reports PUMP_3/Running False and PUMP_3/Flow 0. From then on TANK_3 fills at
  the rate its wells give and reaches the 90 % shutdown around 2:47. Sparkplug owns
  PUMP_3, so the simulator's copy of that fault does nothing; this is the trip the
  console shows.
- A person can still start PUMP_3 again (a confirmed SET_PUMP ON). A running pump drains
  its tank again.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import shutil
import time
from pathlib import Path
import urllib.request
from typing import Callable, Optional

from backend.fakes import opcua, sparkplug

# The demo edge node: the stock demo device plus VALVE_D_3, so Sparkplug B owns the TANK_3,
# PUMP_3, VALVE_D_3 line and OPC UA owns TANK_6, PUMP_6, VALVE_D_6 (no signal has two owners).
DEMO_DEVICES = copy.deepcopy(sparkplug.DEMO_DEVICES)
DEMO_DEVICES["PLC_1"].pop("VALVE_D_6/Open", None)
DEMO_DEVICES["PLC_1"]["VALVE_D_3/Open"] = ("Boolean", True)
DEMO_BLUEPRINT = Path(__file__).resolve().parents[1] / "data" / "samples" / "connectors_demo.json"
LINES = ("3", "6")


class Story:
    """What the demo blueprint says about the TANK_3 and TANK_6 lines, in percent per live
    minute: the starting levels, how fast the wells fill each tank, how fast its pump
    drains it, and when PUMP_3 trips."""

    def __init__(self, blueprint: dict) -> None:
        tanks = {t["id"]: t for t in blueprint["tanks"]}
        pumps = {p["id"]: p for p in blueprint["pumps"]}
        initial = (blueprint.get("initial") or {}).get("tank_pct") or {}

        def pct_per_min(bbl_hr: float, tid: str) -> float:
            return float(bbl_hr) / float(tanks[tid]["capacity_bbl"]) * 100.0 / 60.0

        self.start_pct, self.low_pct, self.fill, self.drain, self.pump_rate = {}, {}, {}, {}, {}
        for n in LINES:
            tid, pid = f"TANK_{n}", f"PUMP_{n}"
            self.start_pct[tid] = float(initial.get(tid, 50.0))
            self.low_pct[tid] = float(tanks[tid].get("pump_low_pct", 0.0))
            self.fill[tid] = pct_per_min(sum(w["water_bbl_hr"] for w in blueprint["wells"] if w["tank_id"] == tid), tid)
            self.pump_rate[pid] = float(pumps[pid]["rate_bbl_hr"])
            self.drain[tid] = pct_per_min(self.pump_rate[pid], tid)
        self.well_1 = float(next(w["water_bbl_hr"] for w in blueprint["wells"] if w["id"] == "WELL_1"))
        trip = next((f for f in blueprint.get("faults") or []
                     if f.get("asset_id") == "PUMP_3" and f.get("type") == "TRIP"), None)
        self.trip_at_min: Optional[float] = float(trip["at_min"]) if trip else None


def _speed() -> float:
    try:
        v = float(os.environ.get("FREEBOARD_SPEED", "0.25"))
    except ValueError:
        return 0.25
    return v if v > 0 else 0.25


def backend_clock(url: str, timeout_s: float = 0.5) -> Callable[[], Optional[float]]:
    """A function that returns the backend's live minute (`t` from GET /api/live/state),
    or None when the backend can't be reached or answers without one."""
    endpoint = url.rstrip("/") + "/api/live/state"

    def clock() -> Optional[float]:
        try:
            with urllib.request.urlopen(endpoint, timeout=timeout_s) as r:
                t = json.loads(r.read().decode("utf-8")).get("t")
        except Exception:  # noqa: BLE001 - a backend that is starting, stopping, or busy
            return None
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t != t:
            return None
        return float(t)

    return clock


class DemoFakes:
    """The running fakes. step() plays the story, stop() stops them all and removes a
    temp certificate dir.

    `clock`, when given, returns the backend's live minute (see backend_clock()); step()
    with no dt_min then moves the story by what that clock moved. Without it, step() with
    no dt_min moves by real time times `speed` (tests pass dt_min themselves)."""

    def __init__(self, opcua_port: int = 4840, sparkplug_port: int = 1886, workdir: Optional[str] = None,
                 password: Optional[str] = None, period_s: float = 1.0, blueprint: Optional[dict] = None,
                 speed: Optional[float] = None, clock: Optional[Callable[[], Optional[float]]] = None) -> None:
        self.password_generated = not password
        self.temp_workdir = workdir is None
        self.story = st = Story(blueprint or json.loads(DEMO_BLUEPRINT.read_text(encoding="utf-8")))
        self.speed = speed or _speed()
        self.clock = clock
        self._last_clock: Optional[float] = None
        # The OPC UA fake's levels move only in step(), on the live clock, so its rates
        # are percent per live minute here.
        self.opcua = opcua.start(port=opcua_port or None, workdir=workdir,
                                 password=password or secrets.token_urlsafe(16),
                                 fill_pct_per_s=st.fill["TANK_6"], drain_pct_per_s=st.drain["TANK_6"],
                                 pump_rate=st.pump_rate["PUMP_6"], well_rate=st.well_1,
                                 min_level_pct=st.low_pct["TANK_6"], auto_levels=False,
                                 initial={"TANK_6.Level": st.start_pct["TANK_6"]}, initial_quality="waiting")
        devices = copy.deepcopy(DEMO_DEVICES)
        devices["PLC_1"]["TANK_3/Level"] = ("Float", st.start_pct["TANK_3"])
        devices["PLC_1"]["PUMP_3/Flow"] = ("Float", st.pump_rate["PUMP_3"])
        try:
            self.sparkplug = sparkplug.start(None, "FREEBOARD", "EDGE_1", devices=devices,
                                             period_s=period_s, broker_listen_port=sparkplug_port)
        except Exception:
            self._stop_opcua()
            raise
        self.t_min: Optional[float] = None     # live minutes since the story started
        self.tripped = False
        self._last_wall: Optional[float] = None

    def env(self) -> dict[str, str]:
        """What the backend's connectors read. The password only when it was made up here."""
        out = {"FREEBOARD_CONN_OPCUA_CERT": str(Path(self.opcua.client_cert).resolve()),
               "FREEBOARD_CONN_OPCUA_KEY": str(Path(self.opcua.client_key).resolve()),
               "FREEBOARD_CONN_OPCUA_SERVER_CERT": str(Path(self.opcua.server_cert).resolve())}
        if self.password_generated:
            out[opcua.PASSWORD_ENV] = self.opcua.password
        return out

    # ---------- the story ----------
    def host_online(self) -> bool:
        """True once a Sparkplug host application has a retained STATE online on the
        broker. The backend's connector publishes one when it connects."""
        broker = getattr(self.sparkplug, "broker", None)
        if broker is None:
            return True
        try:
            retained = broker.retained()
        except Exception:  # noqa: BLE001 - a broker that is stopping
            return False
        for topic, payload in retained.items():
            if not topic.startswith(f"{sparkplug.NS}/STATE/"):
                continue
            try:
                if json.loads(payload).get("online") is True:
                    return True
            except (ValueError, AttributeError, TypeError):
                continue
        return False

    def start_story(self) -> None:
        """Make now live minute 0, without waiting for a host. The OPC UA values turn good."""
        if self.t_min is None:
            self.t_min = 0.0
            for name in opcua.READS:
                self.opcua.set_quality(name, "good")

    def _elapsed(self, dt_min: Optional[float]) -> float:
        """Live minutes since the last step: dt_min when given, else what the backend's
        clock moved (0 while it can't be read, or if it went back), else wall time."""
        wall = time.monotonic()
        last_wall, self._last_wall = self._last_wall, wall
        if dt_min is not None:
            return dt_min
        if self.clock is not None:
            t = self.clock()
            if t is None:
                return 0.0                     # keep the last reading; move on from it later
            last, self._last_clock = self._last_clock, t
            return 0.0 if last is None else max(0.0, t - last)
        return 0.0 if last_wall is None else (wall - last_wall) * self.speed

    def step(self, dt_min: Optional[float] = None) -> None:
        """Move the story on by dt_min live minutes (by default, what the backend's live
        clock moved since the last call, or real time times the speed without a clock).
        Before a host comes online nothing moves."""
        dt_min = self._elapsed(dt_min)
        if self.t_min is None:
            if self.host_online():
                self.start_story()
            return
        self.t_min += dt_min
        st, edge = self.story, self.sparkplug
        if not self.tripped and st.trip_at_min is not None and self.t_min >= st.trip_at_min - 1e-9:
            self.tripped = True
            edge.set("PLC_1", "PUMP_3/Running", False)       # tripped on motor overload
        pumping = edge.get("PLC_1", "PUMP_3/Running") is True and edge.get("PLC_1", "VALVE_D_3/Open") is True
        flow = st.pump_rate["PUMP_3"] if pumping else 0.0
        if edge.get("PLC_1", "PUMP_3/Flow") != flow:
            edge.set("PLC_1", "PUMP_3/Flow", flow)
        level = edge.get("PLC_1", "TANK_3/Level")
        if isinstance(level, (int, float)) and not isinstance(level, bool) and dt_min > 0:
            rate = st.fill["TANK_3"] - (st.drain["TANK_3"] if pumping else 0.0)
            new = float(level) + rate * dt_min
            if pumping and rate < 0:
                new = max(new, min(float(level), st.low_pct["TANK_3"]))
            edge.set("PLC_1", "TANK_3/Level", round(min(100.0, max(0.0, new)), 4))
        self.opcua.step_levels(dt_min)

    # ---------- shutdown ----------
    def _stop_opcua(self) -> None:
        try:
            self.opcua.stop()
        finally:
            if self.temp_workdir:
                shutil.rmtree(self.opcua.workdir, ignore_errors=True)

    def stop(self) -> None:
        try:
            self.sparkplug.stop()
        finally:
            self._stop_opcua()


def main(argv: Optional[list] = None) -> None:
    ap = argparse.ArgumentParser(description="Start the fakes for the Freeboard connectors demo (127.0.0.1 only).")
    ap.add_argument("--opcua-port", type=int, default=4840)
    ap.add_argument("--sparkplug-port", type=int, default=1886, help="port of the fake MQTT broker")
    ap.add_argument("--workdir", default=None, help="where to keep the OPC UA certificates (default: a temp dir)")
    ap.add_argument("--period", type=float, default=1.0, help="seconds between Sparkplug data reports")
    ap.add_argument("--backend", default="http://127.0.0.1:8000",
                    help="the backend whose live clock the story follows (GET /api/live/state)")
    a = ap.parse_args(argv)
    fakes = DemoFakes(a.opcua_port, a.sparkplug_port, a.workdir, os.environ.get(opcua.PASSWORD_ENV), a.period,
                      clock=backend_clock(a.backend))
    print(f"fake OPC UA server   {fakes.opcua.endpoint} (SignAndEncrypt, username {fakes.opcua.username})")
    print(f"fake Sparkplug B     edge node FREEBOARD/EDGE_1, device PLC_1, broker mqtt://127.0.0.1:{fakes.sparkplug.port}")
    print("Export these in the backend's shell, then start it (it starts on the demo blueprint):")
    print(f"  export FREEBOARD_SCENARIO='{DEMO_BLUEPRINT.relative_to(DEMO_BLUEPRINT.parents[3]).as_posix()}'")
    for k, v in fakes.env().items():
        print(f"  export {k}='{v}'")
    if not fakes.password_generated:
        print(f"  ({opcua.PASSWORD_ENV} is already set; it is not printed)")
    trip = fakes.story.trip_at_min
    print(f"The story starts when the backend connects and follows its live clock ({a.backend})"
          + (f"; PUMP_3 trips {trip:g} live minutes later." if trip is not None else "."))
    print("Ctrl+C stops the fakes.", flush=True)
    try:
        while True:
            time.sleep(1.0)
            fakes.step()
    except KeyboardInterrupt:
        pass
    finally:
        fakes.stop()


if __name__ == "__main__":
    main()
