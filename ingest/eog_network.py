"""Turn an EOG sponsor dataset (network.json plus ops.json) into one blueprint and 3D field per system.

    python3 -m ingest.eog_network <folder> --out <dir> [--system S1] [--glb kit|blender]

writes <dir>/<system>/blueprint.json and ids.json (sponsor asset_id -> 3D node name), and with
--glb a field.glb plus build_report.json. "kit" (the default) is blender/assemble_glb.py, plain
Python in well under a second. "blender" runs blender/build_field.py headless for full scenery.

From code: network_to_blueprints(network, ops) -> {system_id: blueprint}, convert() for the ID
maps too, and field_glb(folder, system) for a cached glb (what /api/dataset/field.glb serves).

Only network.json and ops.json are read. Nothing is taken from any one folder: every name,
number and position comes from the files, so the unseen test/ network builds the same way.

Names. A facility's tag is its ID without the system prefix and the word FACILITY
(S1_FACILITY_A -> A). Its tank, pump and pipeline become TANK_A, PUMP_A and VALVE_D_A (a
DISCHARGE valve from PUMP_A to the disposal well), its wells WELL_<id without the system and
WELL> (S1_WELL_A1 -> WELL_A1), the disposal site SWD_1, the truck yard YARD_1, and the fleet
TRUCK_1..n. A name that is empty or taken falls back to a counter. Every asset keeps the sponsor
ID as _source_id and label, and the glb carries it as extras.source_id.

Positions. Lat/long are projected to meters around the system's map center, then compressed,
because facilities sit 5 to 10 km apart and the truck yard 30 km out, while a pad is 50 m wide.
Facilities, the disposal site and the yard each get their own column along the lease road,
west to east in true longitude order. Neighbors sit GAP_MIN m apart plus GAP_PER_KM m per km of
real east-west distance (at most GAP_EXTRA_MAX more), so nothing can overlap and far apart
sites still look far apart. A facility north of the disposal site goes north of the road,
otherwise south, set back DEPTH_PER_KM m per km of real north-south distance (at most
DEPTH_MAX m) behind the generator's usual 46 m. The disposal site sits beside the road on the
north side and the yard south of it, where the trunk line and the trucks expect them.
"""
import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLENDER_DIR = ROOT / "blender"
BLENDER_APP = os.environ.get("BLENDER", "/Applications/Blender.app/Contents/MacOS/Blender")
VERSION = 1                       # part of the field_glb cache key: bump when names or layout change
ID_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

M_PER_DEG_LAT, M_PER_DEG_LON = 110574.0, 111320.0
GAP_MIN, GAP_PER_KM, GAP_EXTRA_MAX = 60.0, 10.0, 80.0
DEPTH_PER_KM, DEPTH_MAX = 6.0, 40.0
TANK_Y, SWD_Y, YARD_Y = 46.0, 16.0, -31.0     # blender/layout.py: TANK_Y, SWD_Y, YARD_Y
MAX_TRUCKS, MAX_WELLS = 6, 8                  # blender/blueprint.py: generator limits


class DatasetError(Exception):
    pass


# ---------- names ----------

def _clean(raw):
    return re.sub(r"_+", "_", re.sub(r"[^A-Z0-9]+", "_", str(raw or "").upper())).strip("_")


def _token(source_id, system_id, words):
    """S1_WELL_A1 with words (WELL,) -> A1. Drops the system prefix and the type word."""
    parts = _clean(source_id).split("_") if source_id else []
    sys_parts = _clean(system_id).split("_") if system_id else []
    if sys_parts and parts[:len(sys_parts)] == sys_parts:
        parts = parts[len(sys_parts):]
    return "_".join(p for p in parts if p and p not in words)


class _Namer:
    def __init__(self):
        self.used = set()

    def take(self, prefix, token=""):
        """PREFIX_TOKEN, or PREFIX_TOKEN_2, _3 when taken, or PREFIX_1, _2 with no usable token."""
        base = f"{prefix}_{token}" if token and ID_RE.match(f"{prefix}_{token}") else prefix
        name, n = (base if base != prefix else None), (2 if base != prefix else 1)
        while name is None or name in self.used:
            name, n = f"{base}_{n}", n + 1
        self.used.add(name)
        return name


# ---------- numbers and places ----------

def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _r(v, nd=3):
    return round(float(v), nd)


def _many(obj, one, many):
    """A facility's tank as obj["tank"] (the spec) or obj["tanks"] (a list), always a list."""
    v = obj.get(many) if obj.get(many) is not None else obj.get(one)
    v = v if isinstance(v, list) else [v] if isinstance(v, dict) else []
    return [x for x in v if isinstance(x, dict)]


def _latlon(obj):
    """(lat, lon) from {"lat", "lon"}, {"latitude", "longitude"} or [lat, lon], or None."""
    if isinstance(obj, (list, tuple)) and len(obj) >= 2 and _num(obj[0]) and _num(obj[1]):
        return float(obj[0]), float(obj[1])
    if isinstance(obj, dict):
        lat, lon = obj.get("lat", obj.get("latitude")), obj.get("lon", obj.get("lng", obj.get("longitude")))
        if _num(lat) and _num(lon):
            return float(lat), float(lon)
    return None


def _project(ll, origin):
    """(east, north) meters from origin on a local flat map, good to a few meters over 50 km."""
    return ((ll[1] - origin[1]) * M_PER_DEG_LON * math.cos(math.radians(origin[0])),
            (ll[0] - origin[0]) * M_PER_DEG_LAT)


def _layout(fac_ll, swd_ll, yard_ll, center):
    """Plan (x, y) for every facility, the disposal site and the yard: see the module docstring.

    fac_ll is {key: (lat, lon) or None}. Returns ({key or "SWD" or "YARD": (x, y)}, note)."""
    pts = {k: _project(ll, center) for k, ll in fac_ll.items() if ll}
    if swd_ll:
        pts["SWD"] = _project(swd_ll, center)
    if yard_ll:
        pts["YARD"] = _project(yard_ll, center)
    if not any(k not in ("SWD", "YARD") for k in pts):
        return {}, "no facility has a lat/long, so the generator lays the field out itself"
    order = sorted(pts, key=lambda k: (pts[k][0], pts[k][1], k))
    xs, x = {}, 0.0
    for i, k in enumerate(order):
        if i:
            km = max(0.0, pts[k][0] - pts[order[i - 1]][0]) / 1000.0
            x += GAP_MIN + min(GAP_EXTRA_MAX, GAP_PER_KM * km)
        xs[k] = x
    mid = (min(xs.values()) + max(xs.values())) / 2
    ref_n = pts["SWD"][1] if "SWD" in pts else 0.0
    out = {}
    for k in order:
        px = _r(xs[k] - mid, 1)
        if k == "SWD":
            out[k] = (px, SWD_Y)
        elif k == "YARD":
            out[k] = (px, YARD_Y)
        else:
            dn = pts[k][1] - ref_n
            depth = TANK_Y + min(DEPTH_MAX, DEPTH_PER_KM * abs(dn) / 1000.0)
            out[k] = (px, _r(depth if dn >= 0 else -depth, 1))
    span = (max(p[0] for p in pts.values()) - min(p[0] for p in pts.values())) / 1000.0
    note = (f"lat/long projected to meters around the map center, then compressed: {len(order)} columns "
            f"west to east in true order, {GAP_MIN:g} m apart plus {GAP_PER_KM:g} m per real km (at most "
            f"+{GAP_EXTRA_MAX:g}); facilities north of the disposal site go north of the road, set back "
            f"{DEPTH_PER_KM:g} m per real km north or south (at most {DEPTH_MAX:g} m) beyond {TANK_Y:g} m. "
            f"Real east-west span {span:.1f} km, plan road about {max(xs.values()) - min(xs.values()):.0f} m")
    return out, note


# ---------- one system ----------

def system_blueprint(system, network, ops=None):
    """(blueprint, ids, review) for one system. ids maps each sponsor asset_id to a node name."""
    sid = str(system.get("id") or "").strip()
    if not sid:
        raise DatasetError("a system in network.json has no id")
    ops = ops if isinstance(ops, dict) else {}
    limits = ((network.get("defaults") or {}).get("tank_limits_pct") or {})
    prices = ops.get("prices") or {}
    trucking = ops.get("emergency_trucking") or {}
    sys_ops = (ops.get("systems") or {}).get(sid) or {}
    fac_ops = sys_ops.get("facilities") or {}
    namer, review, ids = _Namer(), [], {}
    bp = {"version": 1, "name": f"{sid} produced water system", "tanks": [], "pumps": [], "valves": [],
          "wells": [], "disposal_wells": [], "trucks": [], "yards": []}

    def gap(aid, source, issue):
        review.append({"id": aid, "label": source, "issue": issue})

    # disposal site: every pipeline in the system ends here
    sites = _many(system, "disposal_site", "disposal_sites")
    swd_src, swd_ids = (sites[0] if sites else None), {}
    for s in sites or [{}]:
        src = s.get("id")
        sw = {"id": namer.take("SWD", "1" if not swd_ids else str(len(swd_ids) + 1)), "label": src or "disposal site"}
        if src:
            sw["_source_id"], ids[src] = src, sw["id"]
        else:
            gap(sw["id"], None, "network.json gives this system no disposal site; this one is a placeholder")
        if _num(s.get("pressure_psi")):
            sw["_pressure_psi"] = s["pressure_psi"]
        swd_ids[src] = sw
        bp["disposal_wells"].append(sw)
    swd_default = bp["disposal_wells"][0]

    facilities = [f for f in system.get("facilities") or [] if isinstance(f, dict)]
    if not facilities:
        raise DatasetError(f"system {sid} has no facilities")
    fac_ll, fac_tank, swd_cap = {}, {}, 0.0
    for fi, fac in enumerate(facilities):
        fsrc = fac.get("id") or f"{sid}_FACILITY_{fi + 1}"
        tag = _token(fsrc, sid, {"FACILITY", "FAC"}) or str(fi + 1)
        tanks = _many(fac, "tank", "tanks") or [{}]
        tank_ids = []
        for ti, t in enumerate(tanks):
            tid = namer.take("TANK", tag if ti == 0 else f"{tag}_{ti + 1}")
            tank = {"id": tid, "label": t.get("id") or fsrc, "_facility_id": fsrc}
            if t.get("id"):
                tank["_source_id"], ids[t["id"]] = t["id"], tid
            if _num(t.get("capacity_bbl")) and t["capacity_bbl"] > 0:
                tank["capacity_bbl"] = t["capacity_bbl"]
            for key, src_key in (("shutdown_pct", "shutdown"), ("warning_pct", "warning")):
                v = t.get(src_key + "_pct", limits.get(src_key))
                if _num(v):
                    tank[key] = v
            missing = [k for k in ("capacity_bbl", "shutdown_pct") if k not in tank]
            if missing:
                gap(tid, tank["label"], f"Missing {', '.join(missing)}. network.json does not give it.")
            bp["tanks"].append(tank)
            tank_ids.append(tid)
        ids.setdefault(fsrc, f"PAD_{tank_ids[0][5:]}")
        fac_tank[fsrc] = tank_ids[0]
        fac_ll[tank_ids[0]] = _latlon(fac)
        pipes, pumps = _many(fac, "pipeline", "pipelines"), _many(fac, "pump", "pumps")
        if len(pumps) > 2 * len(tank_ids):
            gap(tank_ids[0], fsrc, f"{len(pumps)} pumps; the 3D field builds 2 per tank, so the rest are left out")
        for pi, p in enumerate(pumps[:2 * len(tank_ids)]):
            tid = tank_ids[min(pi // 2, len(tank_ids) - 1)]
            ptag = tag if pi == 0 else f"{tag}_{pi + 1}"
            pid, vid = namer.take("PUMP", ptag), namer.take("VALVE_D", ptag)
            pipe = pipes[pi] if pi < len(pipes) else {}
            swd = swd_ids.get(pipe.get("to") or pipe.get("disposal_site_id")) or swd_default
            pump = {"id": pid, "tank_id": tid, "valve_id": vid, "swd_id": swd["id"], "label": p.get("id") or pid}
            if p.get("id"):
                pump["_source_id"], ids[p["id"]] = p["id"], pid
            if _num(p.get("max_flow_bpd")):
                pump["rate_bbl_hr"] = _r(p["max_flow_bpd"] / 24.0)
                pump["_max_flow_bpd"] = p["max_flow_bpd"]
            else:
                gap(pid, pump["label"], "Missing rate_bbl_hr. network.json gives no max_flow_bpd.")
            for k in ("start_level_pct", "stop_level_pct"):
                if _num(p.get(k)):
                    pump["_" + k] = p[k]
            tank = next(t for t in bp["tanks"] if t["id"] == tid)
            if _num(p.get("start_level_pct")):
                tank.setdefault("setpoint_pct", p["start_level_pct"])
            if _num(p.get("stop_level_pct")):
                tank.setdefault("pump_low_pct", p["stop_level_pct"])
            valve = {"id": vid, "kind": "DISCHARGE", "pump_id": pid, "connects": [pid, swd["id"]], "default": "OPEN",
                     "label": pipe.get("id") or f"{pid} pipeline"}
            if pipe.get("id"):
                valve["_source_id"], ids[pipe["id"]] = pipe["id"], vid
            for k in ("max_bpd", "max_pressure_psi"):
                if _num(pipe.get(k)):
                    valve["_" + k] = pipe[k]
            cap = pipe.get("max_bpd") if _num(pipe.get("max_bpd")) else p.get("max_flow_bpd")
            swd["_cap_bpd"] = swd.get("_cap_bpd", 0.0) + (cap if _num(cap) else 0.0)
            bp["pumps"].append(pump)
            bp["valves"].append(valve)
        wells = _many(fac, "well", "wells")
        if len(wells) > MAX_WELLS * len(tank_ids):
            gap(tank_ids[0], fsrc, f"{len(wells)} wells; the 3D field builds up to {MAX_WELLS} per tank")
        for wi, w in enumerate(wells):
            wtok = _token(w.get("id"), sid, {"WELL"}) or f"{tag}{wi + 1}"
            wid = namer.take("WELL", wtok)
            well = {"id": wid, "tank_id": tank_ids[0], "label": w.get("id") or wid}
            if w.get("id"):
                well["_source_id"], ids[w["id"]] = w["id"], wid
            if _num(w.get("baseline_water_bpd")):
                well["water_bbl_hr"] = _r(w["baseline_water_bpd"] / 24.0)
            oil, gas = w.get("oil_bpd"), w.get("gas_mcfd")
            po, pg = prices.get("oil_per_bbl"), prices.get("gas_per_mcf")
            if _num(oil) and _num(gas) and _num(po) and _num(pg):
                well["lost_usd_hr"] = _r((oil * po + gas * pg) / 24.0, 2)
            for k in ("baseline_water_bpd", "oil_bpd", "gas_mcfd"):
                if _num(w.get(k)):
                    well["_" + k] = w[k]
            missing = [k for k in ("water_bbl_hr", "lost_usd_hr") if k not in well]
            if missing:
                why = " ops.json prices are needed for lost_usd_hr." if "lost_usd_hr" in missing else ""
                gap(wid, well["label"], f"Missing {', '.join(missing)}.{why}")
            bp["wells"].append(well)

    for sw in bp["disposal_wells"]:
        cap = sw.pop("_cap_bpd", 0.0)
        if cap > 0:   # the site "never goes down", so it takes all its pipelines can deliver
            sw["max_bbl_hr"] = _r(cap / 24.0)
            sw["_max_bbl_hr_basis"] = "sum of the max_bpd of the pipelines that end here (the site never goes down)"
        else:
            gap(sw["id"], sw["label"], "Missing max_bbl_hr. No pipeline or pump rate to take it from.")

    yard_ll = _latlon(system.get("truck_yard"))
    if system.get("truck_yard") is not None or sys_ops:
        bp["yards"].append({"id": "YARD_1", "label": f"{sid} truck yard"})
    fleet = sys_ops.get("emergency_fleet_size")
    if not ops:
        gap(None, "ops.json", "No ops.json, so the truck fleet, prices and drive times are unknown.")
    elif not (_num(fleet) and fleet >= 0):
        gap(None, f"{sid} emergency_fleet_size", "ops.json gives this system no emergency_fleet_size.")
    else:
        if fleet > MAX_TRUCKS:
            gap(None, f"{sid} emergency_fleet_size", f"{fleet:g} trucks; the 3D field parks {MAX_TRUCKS}")
        minutes, cycle = {}, {}
        for fsrc, tid in fac_tank.items():
            f = fac_ops.get(fsrc) or {}
            if _num(f.get("response_min")):
                minutes[tid] = f["response_min"]
            if _num(f.get("cycle_min")):
                cycle[tid] = f["cycle_min"]
        for i in range(int(min(fleet, MAX_TRUCKS))):
            truck = {"id": f"TRUCK_{i + 1}", "label": f"{sid} emergency truck {i + 1}"}
            if bp["yards"]:
                truck["home_id"] = bp["yards"][0]["id"]
            if _num(trucking.get("truck_capacity_bbl")):
                truck["capacity_bbl"] = trucking["truck_capacity_bbl"]
            if minutes:
                truck["minutes_to"] = dict(minutes)
            if cycle:
                truck["_cycle_min"] = dict(cycle)
            if _num(trucking.get("cost_per_trip")):
                truck["_cost_per_trip"] = trucking["cost_per_trip"]
            missing = [k for k in ("capacity_bbl", "minutes_to") if k not in truck]
            if missing:
                gap(truck["id"], truck["label"], f"Missing {', '.join(missing)}. ops.json does not give it.")
            bp["trucks"].append(truck)

    costs = {"oil_usd_bbl": prices.get("oil_per_bbl"), "gas_usd_mcf": prices.get("gas_per_mcf"),
             "truck_usd_trip": trucking.get("cost_per_trip")}
    costs = {k: v for k, v in costs.items() if _num(v) and v >= 0}
    if costs:
        bp["costs"] = costs

    center = _latlon(system.get("center"))
    lls = [ll for ll in fac_ll.values() if ll]
    if not center and lls:
        center = (sum(a for a, _ in lls) / len(lls), sum(b for _, b in lls) / len(lls))
    swd_ll = _latlon(swd_src) if swd_src else None
    places, note = _layout(fac_ll, swd_ll, yard_ll, center) if center else ({}, "no lat/long in network.json")
    for t in bp["tanks"]:
        if t["id"] in places:
            t["x"], t["y"] = places[t["id"]]
    if "SWD" in places:
        swd_default["x"], swd_default["y"] = places["SWD"]
    if "YARD" in places and bp["yards"]:
        bp["yards"][0]["x"], bp["yards"][0]["y"] = places["YARD"]
    bp["site"] = {"name": f"{sid} produced water system", "timezone": (network.get("meta") or {}).get("timezone", "UTC")}
    if center:
        bp["site"].update({"lat": _r(center[0], 5), "lon": _r(center[1], 5)})
    geo = {"center": list(center) if center else None, "disposal_site": list(swd_ll) if swd_ll else None,
           "truck_yard": list(yard_ll) if yard_ll else None,
           "facilities": {src: list(fac_ll[tid]) for src, tid in fac_tank.items() if fac_ll.get(tid)}}
    bp["_ingest"] = {"source": {"read_by": "ingest/eog_network.py", "system": sid, "dataset_id": ops.get("dataset_id"),
                                "version": VERSION},
                     "layout": note, "geo": geo, "review": review,
                     "repair_hours": ops.get("expected_repair_hours"), "fleet_size": fleet}
    return bp, ids, review


def systems_of(network):
    if not isinstance(network, dict) or not isinstance(network.get("systems"), (list, dict)):
        raise DatasetError('network.json needs a "systems" list')
    raw = network["systems"]
    if isinstance(raw, dict):   # {"S1": {...}} works too
        raw = [{"id": k, **v} for k, v in raw.items() if isinstance(v, dict)]
    return [s for s in raw if isinstance(s, dict)]


def convert(network, ops=None):
    """{system_id: {"blueprint", "ids", "review"}} in network.json order."""
    out = {}
    for s in systems_of(network):
        bp, ids, review = system_blueprint(s, network, ops)
        out[bp["_ingest"]["source"]["system"]] = {"blueprint": bp, "ids": ids, "review": review}
    return out


def network_to_blueprints(network, ops=None):
    """{system_id: blueprint} for every system in network.json."""
    return {sid: r["blueprint"] for sid, r in convert(network, ops).items()}


def load_folder(folder):
    """(network, ops, raw bytes) from a dataset folder. Only the two JSON files are read."""
    folder = Path(folder)
    try:
        net_raw = (folder / "network.json").read_bytes()
        network = json.loads(net_raw)
    except (OSError, json.JSONDecodeError) as e:
        raise DatasetError(f"cannot read {folder / 'network.json'}: {e}")
    ops, ops_raw = None, b""
    if (folder / "ops.json").exists():
        try:
            ops_raw = (folder / "ops.json").read_bytes()
            ops = json.loads(ops_raw)
        except (OSError, json.JSONDecodeError) as e:
            raise DatasetError(f"cannot read {folder / 'ops.json'}: {e}")
    return network, ops, net_raw + b"\0" + ops_raw


# ---------- 3D (ingest/eog_field.py) ----------

def write_system(result, out_dir, engine=None):
    """Write blueprint.json and ids.json into out_dir, and with engine "kit" or "blender" field.glb."""
    from .eog_field import write_system as write
    return write(result, out_dir, engine)


def field_glb(folder, system, cache_dir=None, engine="kit"):
    """The path of one system's field.glb for a dataset folder, built on first call, then cached."""
    from .eog_field import field_glb as build
    return build(folder, system, cache_dir, engine)


def main(argv=None):
    ap = argparse.ArgumentParser(description="EOG network.json + ops.json -> blueprint.json, ids.json, field.glb per system")
    ap.add_argument("folder", help="a dataset folder holding network.json (and ops.json)")
    ap.add_argument("--out", required=True, help="writes <out>/<system>/...")
    ap.add_argument("--system", action="append", help="only this system (repeatable)")
    ap.add_argument("--glb", nargs="?", const="kit", choices=["kit", "blender"], help="also build field.glb")
    args = ap.parse_args(argv)
    try:
        network, ops, _ = load_folder(args.folder)
        results = convert(network, ops)
    except DatasetError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    unknown = [s for s in args.system or [] if s not in results]
    if unknown:
        print(f"error: no system {', '.join(unknown)}; the folder has {', '.join(results)}", file=sys.stderr)
        return 1
    code = 0
    for sid, result in results.items():
        if args.system and sid not in args.system:
            continue
        s = write_system(result, Path(args.out) / sid, args.glb)
        line = f"{sid}: {len(result['blueprint']['tanks'])} tanks, {len(result['blueprint']['wells'])} wells, " \
               f"{len(result['blueprint']['trucks'])} trucks, {len(result['review'])} review items"
        if args.glb:
            line += f", glb {'OK' if s['ok'] else 'FAILED'} {s.get('glb_mb', 0)} MB in {s['seconds']} s"
            code = code or (0 if s["ok"] else 2)
            line += "".join(f"\n  error: {e}" for e in s["errors"])
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
