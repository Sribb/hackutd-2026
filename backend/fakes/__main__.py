"""Start every fake the connectors demo needs, on 127.0.0.1 only. No real equipment.

    python -m backend.fakes [--opcua-port 4840] [--sparkplug-port 1886] [--workdir DIR]

It starts the fake OPC UA server (backend/fakes/opcua.py) and a fake Sparkplug B edge
node with its own MQTT broker (backend/fakes/sparkplug.py), the two connectors that
backend/data/samples/connectors_demo.json declares. Then it prints the environment
variables to export in the backend's shell before starting it. The ports in the demo
blueprint are 4840 and 1886; with other ports, change the blueprint to match.

The OPC UA login password comes from FREEBOARD_CONN_OPCUA_PASSWORD when it is set, and
is then not printed. When it is not set, the fake makes up a throwaway password for this
run and prints it in the export line, since it opens nothing but this local fake.
Certificates go to --workdir, or to a temp dir that is deleted when this stops.
Ctrl+C stops everything.

The story. The fakes play the demo blueprint's night on the live clock
(FREEBOARD_SPEED live minutes per real second, 0.25 by default, the backend's own
speed). Nothing moves until the backend's Sparkplug host comes online on the broker,
which happens when the demo blueprint is activated. That moment is live minute 0. Then:
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
from typing import Optional

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


class DemoFakes:
    """The running fakes. step() plays the story, stop() stops them all and removes a
    temp certificate dir."""

    def __init__(self, opcua_port: int = 4840, sparkplug_port: int = 1886, workdir: Optional[str] = None,
                 password: Optional[str] = None, period_s: float = 1.0, blueprint: Optional[dict] = None,
                 speed: Optional[float] = None) -> None:
        self.password_generated = not password
        self.temp_workdir = workdir is None
        self.story = st = Story(blueprint or json.loads(DEMO_BLUEPRINT.read_text(encoding="utf-8")))
        self.speed = speed or _speed()
        # The OPC UA fake's levels move only in step(), on the live clock, so its rates
        # are percent per live minute here.
        self.opcua = opcua.start(port=opcua_port or None, workdir=workdir,
                                 password=password or secrets.token_urlsafe(16),
                                 fill_pct_per_s=st.fill["TANK_6"], drain_pct_per_s=st.drain["TANK_6"],
                                 pump_rate=st.pump_rate["PUMP_6"], well_rate=st.well_1,
                                 min_level_pct=st.low_pct["TANK_6"], auto_levels=False)
        self.opcua.set("TANK_6.Level", st.start_pct["TANK_6"])
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
        """Make now live minute 0, without waiting for a host."""
        if self.t_min is None:
            self.t_min = 0.0

    def step(self, dt_min: Optional[float] = None) -> None:
        """Move the story on by dt_min live minutes (by default, the real time since the
        last call times the speed). Before a host comes online nothing moves."""
        wall = time.monotonic()
        if dt_min is None:
            dt_min = 0.0 if self._last_wall is None else (wall - self._last_wall) * self.speed
        self._last_wall = wall
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
    a = ap.parse_args(argv)
    fakes = DemoFakes(a.opcua_port, a.sparkplug_port, a.workdir, os.environ.get(opcua.PASSWORD_ENV), a.period)
    print(f"fake OPC UA server   {fakes.opcua.endpoint} (SignAndEncrypt, username {fakes.opcua.username})")
    print(f"fake Sparkplug B     edge node FREEBOARD/EDGE_1, device PLC_1, broker mqtt://127.0.0.1:{fakes.sparkplug.port}")
    print("Export these in the backend's shell, then start it and activate "
          "backend/data/samples/connectors_demo.json:")
    for k, v in fakes.env().items():
        print(f"  export {k}='{v}'")
    if not fakes.password_generated:
        print(f"  ({opcua.PASSWORD_ENV} is already set; it is not printed)")
    trip = fakes.story.trip_at_min
    print(f"The story starts when the backend connects (speed {fakes.speed:g} live min per second)"
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
