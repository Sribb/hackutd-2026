"""Write a small synthetic EOG dataset folder (network.json, ops.json, telemetry.csv) for tests.

The readings follow SPEC.md: every series reports every grid step, the tank level makes a sawtooth
between the pump's start and stop setpoints, level change per hour is
(inflow - pump flow) / 24 / capacity * 100, and pressures read disposal + offset while the pump runs
and the disposal pressure otherwise. Problems can be injected:

  {"type": "pump_failure", "facility": F, "onset_h": 20, "level_pct": 55}
      status 2 from onset for ops expected_repair_hours; flow 0; the tank fills, and at shutdown the
      wells shut in (status 0, water 0) and the level holds until the repair ends. "level_pct" sets
      the tank level at onset, as the sponsor's generator does (optional).
  {"type": "pipeline_leak", "facility": F, "onset_h": 9, "end_h": 30,
   "step_psi": 13.5, "psi_per_h": 3.3, "loss_step": 0.015, "loss_per_h": 0.004}
      while the pump runs, pipeline pressure drops by step + growth and the pipeline receives
      (1 - loss) of the pumped water. Nothing shows while the pump is off.
  {"type": "pump_degradation", "facility": F, "onset_h": 6, "end_h": 30,
   "flow_loss_per_h": 0.004, "psi_loss_per_h": 1.6}
      while running, flow falls by flow_loss_per_h of max_flow_bpd and discharge by psi_loss_per_h
      for every hour since onset. It ends at end_h (repaired) or at a pump failure on the same pump.

Defaults for magnitudes and noise are close to what the sponsor's history week shows. Every row
draws the same number of random numbers whatever happens, so two datasets with the same seed are
identical up to the first moment their problems differ: that is how the no-lookahead test works.
Nothing here is specific to the sponsor's network; ids, sizes, prices and offsets are parameters.
"""
from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

START = 1_830_000_000          # 2027-12-28, far from both the sponsor's week and the hackathon
STEP_S = 300


def facility_spec(fid_letter: str, wells: list[float], cap: float, max_flow: float, start: float = 70.0,
                  stop: float = 30.0, oil: Optional[list[float]] = None, gas: Optional[list[float]] = None,
                  level0: float = 50.0) -> dict:
    """One facility's parameters (water per well, tank capacity, pump rating and setpoints)."""
    return {"letter": fid_letter, "wells": wells, "cap": cap, "max_flow": max_flow, "start": start,
            "stop": stop, "oil": oil or [100.0 + 40.0 * k for k in range(len(wells))],
            "gas": gas or [400.0 + 150.0 * k for k in range(len(wells))], "level0": level0}


DEFAULT_SYSTEMS = {
    "Q7": {"pressure": 980.0, "facilities": [
        facility_spec("A", [900, 700, 500], 1000, 3200, level0=40),
        facility_spec("B", [600, 450, 350], 650, 2200, level0=62),
        facility_spec("C", [800, 500], 800, 2000, level0=35)]},
    "Q8": {"pressure": 1130.0, "facilities": [
        facility_spec("A", [700, 650, 400], 900, 2600, level0=55),
        facility_spec("B", [500, 500, 300], 700, 2000, level0=45),
        facility_spec("C", [650, 600], 750, 1800, level0=66)]},
}


def build_network(systems: dict, shutdown: float = 92.0, warning: float = 85.0) -> dict:
    out = []
    for k, (sid, s) in enumerate(systems.items()):
        lat0, lon0 = 30.0 + k, -100.0 - k
        facs = []
        for j, f in enumerate(s["facilities"]):
            fid = f"{sid}_FACILITY_{f['letter']}"
            facs.append({
                "id": fid, "lat": lat0 + 0.01 * (j + 1), "lon": lon0 + 0.01 * (j + 1),
                "wells": [{"id": f"{sid}_WELL_{f['letter']}{n + 1}", "baseline_water_bpd": w,
                           "oil_bpd": f["oil"][n], "gas_mcfd": f["gas"][n]} for n, w in enumerate(f["wells"])],
                "tank": {"id": f"{sid}_TANK_{f['letter']}", "capacity_bbl": f["cap"]},
                "pump": {"id": f"{sid}_PUMP_{f['letter']}", "max_flow_bpd": f["max_flow"],
                         "start_level_pct": f["start"], "stop_level_pct": f["stop"]},
                "pipeline": {"id": f"{sid}_PIPELINE_{f['letter']}", "max_bpd": f["max_flow"] * 1.15,
                             "max_pressure_psi": s["pressure"] + 600},
            })
        out.append({"id": sid, "center": [lat0, lon0], "truck_yard": [lat0 - 0.2, lon0 - 0.2],
                    "disposal_site": {"id": f"{sid}_DISPOSAL", "lat": lat0 + 0.02, "lon": lon0,
                                      "pressure_psi": s["pressure"]},
                    "facilities": facs})
    return {"meta": {"name": "synthetic test network"},
            "defaults": {"tank_limits_pct": {"warning": warning, "shutdown": shutdown}}, "systems": out}


def build_ops(network: dict, repair_hours: float = 5.0, oil: float = 70.0, gas: float = 2.5,
              truck_bbl: float = 120.0, truck_cost: float = 800.0, fleet: int = 2) -> dict:
    systems = {}
    for s in network["systems"]:
        systems[s["id"]] = {"emergency_fleet_size": fleet, "facilities": {
            f["id"]: {"response_min": 60 + 5 * j, "cycle_min": 200 + 10 * j} for j, f in enumerate(s["facilities"])}}
    return {"prices": {"oil_per_bbl": oil, "gas_per_mcf": gas}, "expected_repair_hours": {"pump_failure": repair_hours},
            "emergency_trucking": {"truck_capacity_bbl": truck_bbl, "cost_per_trip": truck_cost}, "systems": systems}


@dataclass
class Fixture:
    folder: Path
    start: int
    step: int
    rows: int
    labels: list[dict] = field(default_factory=list)     # injected problems, with onset/end ts and truth
    status: dict[str, list[int]] = field(default_factory=dict)   # pump id -> status per row
    level: dict[str, list[float]] = field(default_factory=dict)  # tank id -> true level per row

    def ts(self, hours: float) -> int:
        """The grid timestamp nearest to start + hours."""
        return self.start + int(round(hours * 3600 / self.step)) * self.step

    def running_rows(self, pump_id: str) -> list[int]:
        return [self.start + i * self.step for i, s in enumerate(self.status[pump_id]) if s == 1]


def write_dataset(folder, *, systems: Optional[dict] = None, hours: float = 48.0, start: int = START,
                  step: int = STEP_S, seed: int = 1, problems: tuple | list = (), repair_hours: float = 5.0,
                  noise: float = 1.0, offsets: tuple[float, float] = (150.0, 50.0), shutdown: float = 92.0,
                  drop_metrics: tuple[str, ...] = (), answer_key: Optional[object] = None) -> Fixture:
    """Simulate the network and write the three files (plus answer_key.json if given) into folder."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    systems = systems or DEFAULT_SYSTEMS
    network = build_network(systems, shutdown=shutdown)
    ops = build_ops(network, repair_hours=repair_hours)
    rng = random.Random(seed)
    n = int(round(hours * 3600 / step))
    dt_h = step / 3600.0
    off_dis, off_pipe = offsets

    def row_of(h: float) -> int:
        return int(round(h * 3600 / step))

    facs = []
    for s in network["systems"]:
        P = s["disposal_site"]["pressure_psi"]
        spec = {f"{s['id']}_FACILITY_{f['letter']}": f for f in systems[s["id"]]["facilities"]}
        for f in s["facilities"]:
            facs.append((s, f, P, spec[f["id"]]))
    probs = {f["id"]: [] for _, f, _, _ in facs}
    for p in problems:
        q = dict(p)
        q["on_row"] = row_of(q["onset_h"])
        if q["type"] == "pump_failure":
            q["off_row"] = q["on_row"] + row_of(repair_hours)
        else:
            q["off_row"] = row_of(q["end_h"]) if q.get("end_h") is not None else n + 1
        probs[q["facility"]].append(q)

    labels: list[dict] = []
    status_out: dict[str, list[int]] = {}
    level_out: dict[str, list[float]] = {}
    series: list[list[tuple[str, str, float]]] = [[] for _ in range(n)]
    for s, f, P, spec in facs:
        fid, pump, tank, pipe = f["id"], f["pump"]["id"], f["tank"]["id"], f["pipeline"]["id"]
        wells = f["wells"]
        cap, maxf = spec["cap"], spec["max_flow"]
        start_pct, stop_pct = spec["start"], spec["stop"]
        level = spec["level0"]
        running = False
        tripped_until: Optional[int] = None
        fails = [q for q in probs[fid] if q["type"] == "pump_failure"]
        leaks = [q for q in probs[fid] if q["type"] == "pipeline_leak"]
        wears = [q for q in probs[fid] if q["type"] == "pump_degradation"]
        for q in wears:     # a degradation ends at a failure of the same pump, if one comes first
            for fq in fails:
                if q["on_row"] <= fq["on_row"] < q["off_row"]:
                    q["off_row"] = fq["on_row"]
        prev_in, prev_out = 0.0, 0.0
        st_col, lv_col = [], []
        trip_ts: dict[int, Optional[int]] = {id(q): None for q in fails}
        lvl_on: dict[int, float] = {}
        for i in range(n):
            g = [rng.gauss(0.0, 1.0) for _ in range(len(wells) + 7)]   # fixed draws per row
            fail = next((q for q in fails if q["on_row"] <= i < q["off_row"]), None)
            base = level
            level += (prev_in - prev_out) / 24.0 / cap * 100.0 * dt_h
            if fail is not None and i == fail["on_row"] and fail.get("level_pct") is not None:
                level = base = float(fail["level_pct"])
            if fail is not None and i == fail["on_row"]:
                lvl_on[id(fail)] = level
            if tripped_until is not None and i >= tripped_until:
                tripped_until = None                 # repair done: wells resume in the same row
            if fail is not None and level >= shutdown:
                if tripped_until is None:            # the true crossing, between the last row and this one
                    rise = level - base
                    frac = min(1.0, max(0.0, (shutdown - base) / rise)) if rise > 0 else 1.0
                    trip_ts[id(fail)] = start + int(round((i - 1 + frac) * step)) if base < shutdown else start + i * step
                    tripped_until = fail["off_row"]
                level = shutdown
            shut = tripped_until is not None
            if fail is not None:
                st = 2
                running = False
            else:
                if level >= start_pct:
                    running = True
                elif level <= stop_pct:
                    running = False
                st = 1 if running else 0
            wear = next((q for q in wears if q["on_row"] <= i < q["off_row"]), None)
            leak = next((q for q in leaks if q["on_row"] <= i < q["off_row"]), None)
            health = 1.0
            dis_loss = 0.0
            if wear is not None:
                hrs = (i - wear["on_row"]) * dt_h
                health = max(0.0, 1.0 - wear.get("flow_loss_per_h", 0.004) * hrs)
                dis_loss = wear.get("psi_loss_per_h", 1.6) * hrs
            flow_true = maxf * health if st == 1 else 0.0
            water = []
            for k, w in enumerate(wells):
                if shut:
                    water.append(0.0)
                else:
                    water.append(max(0.0, w["baseline_water_bpd"] * (1.0 + 0.006 * noise * g[k])))
            inflow_true = 0.0 if shut else sum(w["baseline_water_bpd"] for w in wells)
            j = len(wells)
            if st == 1:
                flow = flow_true * (1.0 + 0.0058 * noise * g[j])
                dis = P + off_dis - dis_loss + 3.0 * noise * g[j + 1]
                loss = 0.0
                drop = 0.0
                if leak is not None:
                    hrs = (i - leak["on_row"]) * dt_h
                    loss = leak.get("loss_step", 0.015) + leak.get("loss_per_h", 0.004) * hrs
                    drop = leak.get("step_psi", 13.5) + leak.get("psi_per_h", 3.3) * hrs
                rcv = flow_true * (1.0 - loss) * (1.0 + 0.0058 * noise * g[j + 2])
                pp = P + off_pipe - drop + 3.0 * noise * g[j + 3]
            else:
                flow, rcv = 0.0, 0.0
                dis = P + 3.0 * noise * g[j + 1]
                pp = P + 3.0 * noise * g[j + 3]
            lv_read = level if (shut and level >= shutdown) else level + 0.05 * noise * g[j + 4]
            row = series[i]
            for k, w in enumerate(wells):
                row.append((w["id"], "water_bpd", round(water[k], 1)))
                row.append((w["id"], "status", 0 if shut else 1))
            row.append((fid, "inflow_bpd", round(sum(water), 1)))
            row.append((tank, "level_pct", round(lv_read, 2)))
            row.append((pump, "status", st))
            row.append((pump, "flow_bpd", round(max(0.0, flow), 1)))
            row.append((pump, "discharge_psi", round(dis, 1)))
            row.append((pipe, "receive_flow_bpd", round(max(0.0, rcv), 1)))
            row.append((pipe, "pressure_psi", round(pp, 1)))
            st_col.append(st)
            lv_col.append(level)
            prev_in, prev_out = inflow_true, flow_true
        status_out[pump] = st_col
        level_out[tank] = lv_col
        for q in probs[fid]:
            on = start + q["on_row"] * step
            end = start + q["off_row"] * step if q["off_row"] < n else None
            asset = pipe if q["type"] == "pipeline_leak" else pump
            lab = {"category": q["type"], "system": s["id"], "facility": fid, "asset": asset, "onset": on, "end": end}
            if q["type"] == "pump_failure":
                lab["level_at_onset_pct"] = lvl_on.get(id(q))
                lab["trips_at"] = trip_ts[id(q)]
            labels.append(lab)

    with open(folder / "network.json", "w", encoding="utf-8") as fh:
        json.dump(network, fh, indent=1)
    with open(folder / "ops.json", "w", encoding="utf-8") as fh:
        json.dump(ops, fh, indent=1)
    with open(folder / "telemetry.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["timestamp", "ts_unix", "asset_id", "metric", "value"])
        for i in range(n):
            t = start + i * step
            stamp = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for asset, metric, v in series[i]:
                if metric in drop_metrics:
                    continue
                wr.writerow([stamp, t, asset, metric, v])
    if answer_key is not None:
        with open(folder / "answer_key.json", "w", encoding="utf-8") as fh:
            json.dump(answer_key, fh)
    labels.sort(key=lambda d: (d["onset"], d["asset"]))
    return Fixture(folder=folder, start=start, step=step, rows=n, labels=labels, status=status_out, level=level_out)
