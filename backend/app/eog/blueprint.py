"""A dataset's network as the team's blueprint (so the 3D generator builds that field) and as the
console's asset list.

    to_blueprint(ds, system=None) -> dict        blueprint v1 (docs/BLUEPRINT.md), valid against
                                                  docs/blueprint.schema.json
    field_assets(ds, system=None) -> list[dict]   every asset with its ids, position and links
    id_map(ds, system=None) -> dict               dataset id -> blueprint id

Everything comes from network.json and ops.json through the Dataset; the telemetry only says which
metrics each asset reports. Nothing here opens answer_key.json or reads the clock, and the result
is the same for every time-travel view of a dataset.

IDs
---
The dataset's ids are kept wherever the schema allows. The schema ties most asset kinds to a type
prefix (ID_RULES: $defs.tank_id is ^TANK_[A-Z0-9_]+$, and so on), and an id such as N1_TANK_A fails
it. Such an id gets its kind's prefix in front of the whole id, so the real id stays inside it:
N1_TANK_A becomes TANK_N1_TANK_A, and the number the generator paints on that tank is N1 TANK A. An
id that already fits its pattern is kept as it is. Nothing is renamed silently: every blueprint
carries `_id_map` (dataset id -> blueprint id) and `_id_rules` (the schema rule behind each rename),
and SCHEMA_CHANGE gives the smallest schema change that would let the real ids through unchanged.

Blueprint v1 has no list for three of the dataset's kinds:
- A pipeline (pump to disposal site) rides on its pump's DISCHARGE valve, whose `connects` is
  [pump, disposal well]: the pipeline's two ends. The valve id is VALVE_D_<pipeline id>, and the
  generator draws the line as PIPE_<pump>_<valve> and PIPE_<valve>_TRUNK.
- A facility is its tank's pad (PAD_<n> in the glb), named in the tank's `_facility`.
- A system is a whole blueprint.
The dataset names no truck yard and no truck (a yard is a [lat, lon], a fleet is a size), so they
get YARD_<system> and TRUCK_<system>_<k>, listed in `_synthesized`.

Layout
------
Each system is projected to local meters, equirectangular around its center, and scaled by one
factor, so directions and relative distances stay real. The generator's lease road runs east-west
through the center (y = 0). Every site keeps its side of the road (north or south, by latitude)
and is set back by the depth it needs in front of it (a pad's tank 46 m, as the generator's own
pads), so nothing sits on the road. The scale is the smallest at which no two sites on the same
side share any east-west extent, so every pad, the disposal site and the yard reach the road
without crossing another site. It is capped so the farthest site stays within FIELD_HALF_M of the
center, because the generator pays triangles for every square meter of ground. When the cap binds,
the sites on that side keep their east-west order and move the least distance that clears them
(least squares), and `_layout.notes` says which moved and how far. A facility's tank sits on the
facility's point; its pump and wells take the generator's own pad template around it, and trucks
park in the generator's yard slots, so field_assets and the built glb agree.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Optional

from .dataset import Dataset, Facility

# ---------- schema rules (docs/blueprint.schema.json; the tests check every one against the file) ----------
# kind -> the rule its blueprint id must pass, and `refs`: other rules the id must also pass where
# another asset names it. A tank, pump or disposal id is checked by one $defs rule everywhere; a
# pipeline's valve id has its own pattern and a second one in pumps[].valve_id.
ID_RULES = {
    "tank": {"rule": "/$defs/tank_id/pattern", "pattern": "^TANK_[A-Z0-9_]+$", "prefix": "TANK_"},
    "pump": {"rule": "/$defs/pump_id/pattern", "pattern": "^PUMP_[A-Z0-9_]+$", "prefix": "PUMP_"},
    "well": {"rule": "/$defs/well/properties/id/pattern", "pattern": "^WELL_[A-Z0-9_]+$", "prefix": "WELL_"},
    "pipeline": {"rule": "/$defs/discharge_valve/properties/id/pattern", "pattern": "^VALVE_D_[A-Z0-9_]+$",
                 "prefix": "VALVE_D_",
                 "refs": [{"rule": "/$defs/valve_id/pattern", "pattern": "^VALVE_(D|XO)_[A-Z0-9_]+$",
                           "used_by": "pumps[].valve_id"}]},
    "disposal": {"rule": "/$defs/swd_id/pattern", "pattern": "^SWD_[A-Z0-9_]+$", "prefix": "SWD_"},
    "truck_yard": {"rule": "/$defs/yard_id/pattern", "pattern": "^YARD_[A-Z0-9_]+$", "prefix": "YARD_"},
    "truck": {"rule": "/$defs/truck/properties/id/pattern", "pattern": "^TRUCK_[A-Z0-9_]+$", "prefix": "TRUCK_"},
}
COUNT_RULES = {
    "tanks": {"rule": "/properties/tanks/maxItems", "max": 20},
    "disposal_wells": {"rule": "/properties/disposal_wells/maxItems", "max": 4},
    "trucks": {"rule": "/properties/trucks/maxItems", "max": 6},
    "yards": {"rule": "/properties/yards/maxItems", "max": 1},
}
NO_LIST_RULE = "/additionalProperties"     # top level: false, so a blueprint takes no other lists
ANY_ID = "^[A-Z][A-Z0-9_]*$"               # $defs.asset_id, and blender/blueprint.py ID_RE
SCHEMA_CHANGE = {
    "ids": ("Set these six patterns in docs/blueprint.schema.json to " + ANY_ID + " (the rule $defs.asset_id "
            "already states, and the one blender/blueprint.py applies to every id): $defs.tank_id, $defs.pump_id, "
            "$defs.swd_id, $defs.well.properties.id, $defs.discharge_valve.properties.id and $defs.valve_id. Then "
            "every dataset id made of capital letters, digits and underscores (as the sponsor's are) goes into the "
            "blueprint unchanged, a pipeline's id as its discharge valve's id. Each of the six blocks some dataset id "
            "on its own. Yards and trucks need nothing: the dataset gives them no ids. An id with other characters "
            "can't be a blueprint id under any schema the generator reads, so it stays upper-cased with _."),
    "whole_network": ("Raise /properties/disposal_wells/maxItems from 4 to at least the number of systems (each "
                      "has one disposal site), and /properties/tanks/maxItems from 20 to the number of facilities "
                      "when there are more. Yards and trucks are optional, so a whole-network blueprint leaves them "
                      "out (listed in _omitted) and they need no change. blender/blueprint.py COUNT_LIMITS caps the "
                      "same counts, and the generator would still run every system along one lease road and one "
                      "trunk line, so one blueprint per system stays the faithful build."),
}

# ---------- generator facts (blender/blueprint.py and blender/layout.py; the tests compare with them) ----------
WELLS_PER_TANK = 8                 # blueprint.WELLS_PER_TANK: the most wells it builds on one pad
BLENDER_NAME_MAX = 63              # longest object name Blender keeps without cutting it
PAD_TANK_Y = 46.0                  # layout.TANK_Y: a pad's tank center, measured from the road centerline
PUMP_SLOTS = ((4.83, 33.0), (9.83, 33.0))          # layout.PUMP_SLOTS at layout.PUMP_Y, pad-local
WELLS_UP_TO_4 = {0: (), 1: ((0.0, 76.0),), 2: ((-8.0, 76.0), (8.0, 76.0)),
                 3: ((-14.0, 72.0), (0.0, 82.0), (14.0, 72.0)),
                 4: ((-16.0, 72.0), (-6.0, 82.0), (6.0, 82.0), (16.0, 72.0))}   # layout.WELLS_UP_TO_4
YARD_SLOTS = ((-2.0, 4.0), (12.5, 4.0), (-2.0, 0.6), (12.5, 0.6), (-16.5, 0.6), (12.5, -2.8))  # layout.YARD_SLOTS
PAD_PITCH_M = 55.0                 # layout.PAD_PITCH: the spacing of the generator's own row of pads
# site kind -> (half its east-west extent, its set-back from the road centerline, its side when it
# lies exactly on the road). Extents: layout.FOOTPRINT (a pad with its wells), the disposal well's
# and the yard's rects. Set-backs: layout.TANK_Y, layout.SWD_Y and -layout.YARD_Y.
SITES = {"facility": (25.0, PAD_TANK_Y, 1), "disposal": (13.0, 16.0, 1), "truck_yard": (22.0, 31.0, -1)}

# ---------- layout choices ----------
EARTH_R_M = 6_371_008.8            # mean Earth radius
GAP_M = 10.0                       # clear ground between neighbors on the same side of the road
FIELD_HALF_M = 600.0               # the farthest site's scaled offset stays within this
MAX_COORD = 5000.0                 # schema $defs.coord: x and y within +-5000 m


class BlueprintError(ValueError):
    """The network can't be written as a valid blueprint v1. `rules` lists the schema rules in the way."""

    def __init__(self, message: str, rules: Iterable[dict] = ()):
        super().__init__(message)
        self.rules = list(rules)


# ---------- small helpers ----------
def _sanitize(raw: str) -> str:
    """Upper case, and any character other than A-Z, 0-9 and _ becomes _."""
    return re.sub(r"[^A-Z0-9_]", "_", str(raw).upper()) or "X"


def _fits(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern[1:-1] if pattern.startswith("^") and pattern.endswith("$") else pattern,
                        value) is not None


def _num(x: float):
    """Round to 1e-6 and write whole numbers as ints, so 1200.0 reads 1200 and 1000/24 reads 41.666667."""
    v = round(float(x), 6)
    return int(v) if v.is_integer() else v


def _m(x: float) -> float:
    """Meters to the centimeter, never -0.0."""
    return round(float(x), 2) + 0.0


def label_of(asset_id: str) -> str:
    """N1_TANK_A -> "N1 Tank A": words of three letters or more in title case, everything else as is."""
    parts = [p for p in re.split(r"[_\s]+", str(asset_id)) if p]
    return " ".join(p.capitalize() if p.isalpha() and len(p) > 2 else p for p in parts) or str(asset_id)


def pad_suffix(tank_id: str) -> str:
    """layout.suffix(): TANK_N1_TANK_A -> N1_TANK_A, which names PAD_<n>, LOADOUT_<n> and the painted number."""
    return tank_id[5:] if tank_id.startswith("TANK_") and len(tank_id) > 5 else tank_id


def well_slots(count: int) -> list[tuple[float, float]]:
    """layout.well_slots() without the turn: pad-local (x, y) of count wells, west to east."""
    if count <= 4:
        return list(WELLS_UP_TO_4[count])
    n1 = (count + 1) // 2
    row1 = [(i - (n1 - 1) / 2) * 11.0 for i in range(n1)]
    gaps = [row1[0] - 5.5] + [x + 5.5 for x in row1]
    row2 = sorted(sorted(gaps, key=lambda x: (abs(x), x))[:count - n1])
    return [(x, 70.0) for x in row1] + [(x, 88.0) for x in row2]


def pad_point(tank_xy: tuple[float, float], lx: float, ly: float) -> tuple[float, float]:
    """A pad-local point in plan meters. A pad south of the road is the same pad turned 180 degrees
    (layout.tank_frame), with its tank at pad-local (0, PAD_TANK_Y)."""
    x, y = tank_xy
    if y < 0:
        return x - lx, y + PAD_TANK_Y - ly
    return x + lx, y - PAD_TANK_Y + ly


def yard_point(yard_xy: tuple[float, float], slot: int) -> tuple[float, float]:
    """Where the generator parks the slot-th truck of a yard placed at yard_xy (layout.YARD_SLOTS)."""
    x, y = yard_xy
    lx, ly = YARD_SLOTS[slot]
    s = -1.0 if y > 0 else 1.0           # layout.plan: Frame(x, y, flip=y > 0)
    return x + s * lx, y + s * ly


def _project(lat: float, lon: float, origin: tuple[float, float]) -> tuple[float, float]:
    """(east, north) in meters from origin, equirectangular."""
    lat0, lon0 = origin
    k = math.radians(1.0) * EARTH_R_M
    return (lon - lon0) * k * math.cos(math.radians(lat0)), (lat - lat0) * k


def _isotonic(values: list[float]) -> list[float]:
    """The non-decreasing sequence closest to values in least squares (pool adjacent violators)."""
    blocks: list[list[float]] = []          # [sum, count]
    for v in values:
        blocks.append([v, 1.0])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    out: list[float] = []
    for s, c in blocks:
        out += [s / c] * int(c)
    return out


# ---------- ids ----------
class _Names:
    """Blueprint ids for the whole network, worked out once, so every blueprint of a dataset (one
    system or all of them) gives an asset the same id.

    An id that fits its kind's pattern is kept, whatever order the network lists it in. Every other
    id gets its kind's prefix, with _2, _3 ... only when that would repeat an id already in use. The
    synthesized yard and truck ids avoid every id of the dataset (systems, facilities, assets and
    whatever the telemetry names), so field_assets never lists one id twice."""

    def __init__(self, ds: Dataset):
        self.of: dict[str, str] = {}             # dataset id -> blueprint id
        self.kind: dict[str, str] = {}           # dataset id -> kind
        self.yard: dict[str, str] = {}           # system id -> synthesized yard id
        self.trucks: dict[str, list[str]] = {}   # system id -> synthesized truck ids, the whole fleet
        self.uniquified: list[str] = []          # dataset ids that needed a _2 to stay unique
        items: list[tuple[str, str]] = []        # (kind, dataset id) in network order
        for sid, sysm in ds.systems.items():
            for fid in sysm.facility_ids:
                f = ds.facilities[fid]
                items += [("well", w.id) for w in f.wells]
                items += [("tank", f.tank_id), ("pump", f.pump_id), ("pipeline", f.pipeline_id)]
            items.append(("disposal", sysm.disposal_id))
        listed = {sid: "system" for sid in ds.systems}          # ids field_assets lists as they are
        for kind, real in [("facility", fid) for fid in ds.facilities] + items:
            if real in listed:
                raise BlueprintError(f"{real} is used twice in network.json (as a {listed[real]} and as a {kind}); "
                                     "every asset needs its own id.")
            listed[real] = kind
            if kind != "facility":
                self.kind[real] = kind
        self._taken: set[str] = set(listed) | {a for a, _ in ds.series_keys()}
        for kind, real in items:                 # pass 1: ids the schema already takes stay as they are
            if _fits(ID_RULES[kind]["pattern"], real):
                self.of[real] = real
        for kind, real in items:                 # pass 2: the rest get their kind's prefix
            if real not in self.of:
                candidate = ID_RULES[kind]["prefix"] + _sanitize(real)
                self.of[real] = self._unique(candidate)
                if self.of[real] != candidate:
                    self.uniquified.append(real)
        for sid, sysm in ds.systems.items():
            base = _sanitize(sid)
            self.yard[sid] = self._unique(ID_RULES["truck_yard"]["prefix"] + base)
            self.trucks[sid] = [self._unique(f"{ID_RULES['truck']['prefix']}{base}_{k}")
                                for k in range(1, max(0, int(sysm.fleet_size)) + 1)]

    def _unique(self, candidate: str) -> str:
        out, k = candidate, 2
        while out in self._taken:
            out, k = f"{candidate}_{k}", k + 1
        self._taken.add(out)
        return out


# ---------- layout ----------
def _plan(sites: list[tuple[str, str, float, float]], origin: tuple[float, float]) -> dict:
    """sites: [(key, kind, lat, lon)] -> {"origin", "scale", "pos": {key: (x, y)}, "side", "notes"}."""
    pts = []
    for key, kind, lat, lon in sites:
        e, n = _project(lat, lon, origin)
        half, depth, default_side = SITES[kind]
        side = 1 if n > 0 else -1 if n < 0 else default_side
        pts.append({"key": key, "kind": kind, "e": e, "n": n, "half": half, "depth": depth, "side": side})

    need = 0.0                     # smallest scale that gives same-side neighbors separate frontage
    for i, a in enumerate(pts):
        for b in pts[i + 1:]:
            de = abs(a["e"] - b["e"])
            if a["side"] == b["side"] and de > 1e-9:
                need = max(need, (a["half"] + b["half"] + GAP_M) / de)
    fac = [p for p in pts if p["kind"] == "facility"]
    nearest = min((math.hypot(a["e"] - b["e"], a["n"] - b["n"]) for i, a in enumerate(fac) for b in fac[i + 1:]),
                  default=0.0)
    floor = PAD_PITCH_M / nearest if nearest > 0 else 0.0     # facilities never closer than the generator's pitch
    far = max((max(abs(p["e"]), abs(p["n"])) for p in pts), default=0.0)
    cap = FIELD_HALF_M / far if far > 0 else 1.0
    scale = min(max(need, floor) or cap, cap)

    x = {p["key"]: scale * p["e"] for p in pts}
    notes = []
    for side in (1, -1):
        row = sorted((p for p in pts if p["side"] == side), key=lambda p: (x[p["key"]], p["key"]))
        if len(row) < 2:
            continue
        offsets, d = [0.0], 0.0
        for a, b in zip(row, row[1:]):
            d += a["half"] + b["half"] + GAP_M
            offsets.append(d)
        fit = _isotonic([x[p["key"]] - o for p, o in zip(row, offsets)])
        for p, f, o in zip(row, fit, offsets):
            moved = f + o - x[p["key"]]
            if abs(moved) >= 0.005:
                notes.append(f"{p['key']} moved {abs(moved):.1f} m {'east' if moved > 0 else 'west'} along the road: at "
                             f"the capped scale ({scale * 1000:.2f} m per km) it would share frontage with a neighbor "
                             f"{'north' if side > 0 else 'south'} of the road")
            x[p["key"]] = f + o
    pos = {p["key"]: (x[p["key"]], scale * p["n"] + p["side"] * p["depth"]) for p in pts}
    worst = max((max(abs(a), abs(b)) for a, b in pos.values()), default=0.0)
    if worst > MAX_COORD:
        raise BlueprintError(f"The layout reaches {worst:.0f} m from the center, past the schema's {MAX_COORD:.0f} m "
                             "($defs.coord).", [{"rule": "/$defs/coord", "max": MAX_COORD, "value": round(worst, 1)}])
    return {"origin": origin, "scale": scale, "pos": pos, "side": {p["key"]: p["side"] for p in pts},
            "half": {p["key"]: p["half"] for p in pts}, "notes": notes}


def _sites(ds: Dataset, names: _Names, sids: list[str], with_yards: bool) -> list[tuple[str, str, float, float]]:
    out = []
    for sid in sids:
        sysm = ds.systems[sid]
        for fid in sysm.facility_ids:
            f = ds.facilities[fid]
            out.append((fid, "facility", f.lat, f.lon))
        out.append((sysm.disposal_id, "disposal", sysm.disposal_lat, sysm.disposal_lon))
        if with_yards:
            out.append((names.yard[sid], "truck_yard", sysm.truck_yard[0], sysm.truck_yard[1]))
    return out


def _system_plan(ds: Dataset, names: _Names, sid: str) -> dict:
    return _plan(_sites(ds, names, [sid], True), ds.systems[sid].center)


def _network_plan(ds: Dataset, names: _Names, with_yards: bool) -> dict:
    sids = list(ds.systems)
    origin = (sum(ds.systems[s].center[0] for s in sids) / len(sids),
              sum(ds.systems[s].center[1] for s in sids) / len(sids))
    return _plan(_sites(ds, names, sids, with_yards), origin)


def _layout_info(plan: dict) -> dict:
    return {"projection": "equirectangular", "origin": [plan["origin"][0], plan["origin"][1]],
            "scale": round(plan["scale"], 9), "m_per_km": round(plan["scale"] * 1000.0, 3),
            "road": "the generator's lease road runs east-west through the origin, at y = 0",
            "notes": list(plan["notes"])}


# ---------- checks ----------
def _system(ds: Dataset, system: str) -> None:
    if system not in ds.systems:
        raise BlueprintError(f"No system named {system!r} in {ds.id}. Known: {', '.join(ds.systems) or 'none'}.")


def _check_places(ds: Dataset, sids: list[str]) -> None:
    """Every point the layout projects must be a place on Earth, or no position can be worked out.
    Raises BlueprintError naming the point and the schema rule that would end up broken."""
    site = [{"rule": "/$defs/site/properties/lat"}, {"rule": "/$defs/site/properties/lon"}]
    coord = [{"rule": "/$defs/coord"}]
    for sid in sids:
        sysm = ds.systems[sid]
        points = [(f"system {sid} center", sysm.center, site), (f"system {sid} truck_yard", sysm.truck_yard, coord),
                  (f"system {sid} disposal_site {sysm.disposal_id}", (sysm.disposal_lat, sysm.disposal_lon), coord)]
        points += [(f"facility {fid}", (ds.facilities[fid].lat, ds.facilities[fid].lon), coord)
                   for fid in sysm.facility_ids]
        for what, (lat, lon), rules in points:
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):          # false for NaN and infinity too
                raise BlueprintError(f"network.json {what} is [{lat:g}, {lon:g}], not a place on Earth "
                                     "(latitude -90 to 90, longitude -180 to 180).", rules)


def _check_numbers(ds: Dataset, sids: list[str]) -> None:
    """Numbers a blueprint can't hold. Raises BlueprintError naming the dataset field and the schema rule."""
    if not 0 < ds.shutdown_pct <= 100:
        raise BlueprintError(f"network.json defaults.tank_limits_pct.shutdown is {ds.shutdown_pct:g}; blueprint v1 "
                             "needs above 0 and at most 100.", [{"rule": "/$defs/tank/properties/shutdown_pct"}])
    _check_places(ds, sids)
    for sid in sids:
        sysm = ds.systems[sid]
        if not sysm.facility_ids:
            raise BlueprintError(f"System {sid} has no facilities; a blueprint needs at least one tank.",
                                 [{"rule": "/properties/tanks/minItems", "min": 1}])
        if len(sysm.facility_ids) > COUNT_RULES["tanks"]["max"]:
            raise BlueprintError(f"System {sid} has {len(sysm.facility_ids)} facilities; blueprint v1 holds at most "
                                 f"{COUNT_RULES['tanks']['max']} tanks.", [dict(COUNT_RULES["tanks"])])
        for fid in sysm.facility_ids:
            f = ds.facilities[fid]
            if f.max_flow_bpd < 0:
                raise BlueprintError(f"{f.pump_id} max_flow_bpd is {f.max_flow_bpd:g}; a pump's rate can't be "
                                     "negative.", [{"rule": "/$defs/pump/properties/rate_bbl_hr"}])
            for w in f.wells:
                if w.baseline_water_bpd < 0 or ds.revenue_per_day(w.id) < 0:
                    raise BlueprintError(f"{w.id} has a negative water rate or revenue; blueprint v1 needs 0 or more.",
                                         [{"rule": "/$defs/well/properties/water_bbl_hr"},
                                          {"rule": "/$defs/well/properties/lost_usd_hr"}])


def _check_whole(ds: Dataset, single: bool) -> list[dict]:
    """The whole network in one blueprint: raises when a count is past the schema, else returns
    what has to be left out (the yards and trucks of a network with more than one system)."""
    sids = list(ds.systems)
    tanks = sum(len(ds.systems[s].facility_ids) for s in sids)
    over = []
    if tanks > COUNT_RULES["tanks"]["max"]:
        over.append(dict(COUNT_RULES["tanks"], count=tanks, what="tanks (one per facility)"))
    if len(sids) > COUNT_RULES["disposal_wells"]["max"]:
        over.append(dict(COUNT_RULES["disposal_wells"], count=len(sids), what="disposal sites (one per system)"))
    if over:
        said = "; ".join(f"{o['count']} {o['what']}, but {o['rule']} is {o['max']}" for o in over)
        raise BlueprintError(f"The whole network of {ds.id} doesn't fit one blueprint v1: {said}. Build one blueprint "
                             f"per system (to_blueprint(ds, system)), or change the schema: "
                             f"{SCHEMA_CHANGE['whole_network']}", over)
    if single:
        return []
    fleet = sum(max(0, int(ds.systems[s].fleet_size)) for s in sids)
    return [dict(COUNT_RULES["yards"], count=len(sids), what="truck yards (one per system)", left_out=len(sids)),
            dict(COUNT_RULES["trucks"], count=fleet, what="trucks (every system's fleet)", left_out=fleet,
                 why="each system's trucks belong to its own yard, which can't be in the blueprint")]


def _kept_trucks(ds: Dataset, names: _Names, sid: str) -> tuple[list[str], Optional[str]]:
    """The system's truck ids its blueprint holds, the first COUNT_RULES["trucks"]["max"] of the fleet,
    or none, with the reason, when ops.json gives its trucks numbers the schema can't take."""
    fleet = names.trucks[sid]
    responses = [ds.facilities[fid].response_min for fid in ds.systems[sid].facility_ids]
    if not fleet:
        return [], None
    if not responses:
        return [], f"{sid} has no facility for its trucks to serve, so its {len(fleet)} trucks are left out."
    if ds.truck_capacity_bbl <= 0 or min(responses) < 0:
        return [], (f"ops.json gives trucks capacity {ds.truck_capacity_bbl:g} bbl and response times down to "
                    f"{min(responses):g} min; a blueprint truck needs capacity above 0 and times of 0 or more, "
                    f"so {sid}'s {len(fleet)} trucks are left out.")
    return fleet[:COUNT_RULES["trucks"]["max"]], None


def _longest_name(doc: dict) -> str:
    """The longest node name the generator derives from these ids (names.expected and the moving parts)."""
    names = []
    for t in doc["tanks"]:
        n = pad_suffix(t["id"])
        names += [f"PIPE_{t['id']}_LOADOUT_{n}", f"PIPE_INLET_{n}_RISER", f"RTU_PAD_{n}_antenna",
                  f"SENSOR_{t['id']}_light", f"{t['id']}_label_cap", f"ROAD_JCT_{t['id']}"]
    for p in doc["pumps"]:
        names += [f"PIPE_{p['tank_id']}_{p['id']}", f"PIPE_{p['id']}_{p['valve_id']}", f"PIPE_{p['valve_id']}_TRUNK",
                  f"SENSOR_{p['id']}_light"]
    for w in doc["wells"]:
        names += [f"PIPE_{w['id']}_{w['tank_id']}", f"{w['id']}_pitman", f"{w['id']}_bridle"]
    for s in doc["disposal_wells"]:
        names += [f"ROAD_JCT_{s['id']}", f"{s['id']}_lamps"]
    for t in doc.get("trucks", []):
        names += [f"{t['id']}_headlights"]
    for y in doc.get("yards", []):
        names += [f"ROAD_JCT_{y['id']}"]
    return max(names, key=len)


def _scope_map(ds: Dataset, names: _Names, sids: list[str]) -> dict[str, str]:
    out = {}
    for sid in sids:
        sysm = ds.systems[sid]
        for fid in sysm.facility_ids:
            f = ds.facilities[fid]
            for real in [w.id for w in f.wells] + [f.tank_id, f.pump_id, f.pipeline_id]:
                out[real] = names.of[real]
        out[sysm.disposal_id] = names.of[sysm.disposal_id]
    return out


# ---------- public ----------
def id_map(ds: Dataset, system: Optional[str] = None) -> dict[str, str]:
    """Dataset id -> blueprint id for every dataset asset a blueprint of that scope holds (wells, tanks,
    pumps, pipelines as their discharge valves, disposal sites), in network order. The same id is
    used in every scope."""
    names = _Names(ds)
    if system is not None:
        _system(ds, system)
    return _scope_map(ds, names, [system] if system is not None else list(ds.systems))


def to_blueprint(ds: Dataset, system: Optional[str] = None) -> dict:
    """A blueprint v1 document for one system, or for the whole network when blueprint v1 can hold it.

    Valid against docs/blueprint.schema.json. Raises BlueprintError for an unknown system, or for a
    network the schema can't hold in one blueprint (its `rules` say which). Keys that start with _
    are metadata every reader ignores: `_id_map`, `_id_rules`, `_synthesized`, `_layout`, `_derived`,
    `_source`, and `_omitted` / `_warnings` when there is something to say.
    """
    names = _Names(ds)
    if system is None:
        sids = list(ds.systems)
        if not sids:
            raise BlueprintError(f"{ds.id} has no systems.", [{"rule": "/properties/tanks/minItems", "min": 1}])
        single = len(sids) == 1
        _check_numbers(ds, sids)
        omitted = _check_whole(ds, single)
        plan = _network_plan(ds, names, with_yards=single) if not single else _system_plan(ds, names, sids[0])
        with_yards = single
    else:
        _system(ds, system)
        sids, omitted, with_yards = [system], [], True
        _check_numbers(ds, sids)
        plan = _system_plan(ds, names, system)
    pos = plan["pos"]
    warnings: list[str] = []

    def num_or_skip(value: float, lo: float, hi: float, field: str, where: str) -> Optional[float]:
        if lo <= value <= hi:
            return _num(value)
        warnings.append(f"{where} is {value:g}, outside {lo:g} to {hi:g}, so the blueprint leaves {field} out.")
        return None

    tanks, pumps, valves, wells, swds, trucks, yards, synthesized = [], [], [], [], [], [], [], []
    trucking = {}
    for sid in sids:
        sysm = ds.systems[sid]
        swd = names.of[sysm.disposal_id]
        through = 0.0
        for fid in sysm.facility_ids:
            f: Facility = ds.facilities[fid]
            tid, pid, vid = names.of[f.tank_id], names.of[f.pump_id], names.of[f.pipeline_id]
            x, y = pos[fid]
            tank = {"id": tid, "label": label_of(f.tank_id), "capacity_bbl": _num(f.capacity_bbl),
                    "shutdown_pct": _num(ds.shutdown_pct)}
            for field, value, where in (("warning_pct", ds.warning_pct, "network.json defaults.tank_limits_pct.warning"),
                                        ("pump_low_pct", f.stop_level_pct, f"{f.pump_id} stop_level_pct")):
                v = num_or_skip(value, 0.0, 100.0, field, where)
                if v is not None:
                    tank[field] = v
            tank.update({"x": _m(x), "y": _m(y), "_dataset_id": f.tank_id, "_facility": fid})
            tanks.append(tank)
            pumps.append({"id": pid, "label": label_of(f.pump_id), "tank_id": tid,
                          "rate_bbl_hr": _num(f.max_flow_bpd / 24.0), "valve_id": vid, "swd_id": swd,
                          "_dataset_id": f.pump_id, "_start_level_pct": _num(f.start_level_pct),
                          "_stop_level_pct": _num(f.stop_level_pct)})
            valve = {"id": vid, "kind": "DISCHARGE", "label": label_of(f.pipeline_id), "pump_id": pid,
                     "connects": [pid, swd], "default": "OPEN", "_dataset_id": f.pipeline_id,
                     "_dataset_kind": "pipeline"}
            if f.pipeline_max_bpd is not None:
                valve["_max_bpd"] = _num(f.pipeline_max_bpd)
            if f.pipeline_max_pressure_psi is not None:
                valve["_max_pressure_psi"] = _num(f.pipeline_max_pressure_psi)
            valves.append(valve)
            for w in f.wells:
                wells.append({"id": names.of[w.id], "label": label_of(w.id), "tank_id": tid,
                              "water_bbl_hr": _num(w.baseline_water_bpd / 24.0),
                              "lost_usd_hr": _num(ds.revenue_per_hour(w.id)), "_dataset_id": w.id})
            if len(f.wells) > WELLS_PER_TANK:
                warnings.append(f"{fid} has {len(f.wells)} wells; the generator builds at most {WELLS_PER_TANK} per "
                                "tank (blender/blueprint.py WELLS_PER_TANK), so it will refuse this blueprint.")
            through += max(f.max_flow_bpd, f.pipeline_max_bpd or 0.0)
        capacity = through / 24.0
        if capacity <= 0:
            capacity = 1.0
            warnings.append(f"No pump or pipeline in {sid} can move water, so {swd} max_bbl_hr is set to 1 to keep "
                            "the blueprint valid.")
        dx, dy = pos[sysm.disposal_id]
        swds.append({"id": swd, "label": label_of(sysm.disposal_id), "max_bbl_hr": _num(capacity), "x": _m(dx),
                     "y": _m(dy), "_dataset_id": sysm.disposal_id, "_pressure_psi": _num(sysm.disposal_pressure_psi)})
        if not with_yards:
            continue
        yid = names.yard[sid]
        yx, yy = pos[yid]
        yards.append({"id": yid, "label": f"{label_of(sid)} truck yard", "x": _m(yx), "y": _m(yy)})
        synthesized.append({"id": yid, "kind": "truck_yard",
                            "from": f"network.json systems {sid} truck_yard (a position with no id)"})
        fleet = names.trucks[sid]
        keep, why = _kept_trucks(ds, names, sid)
        trucking = {"system": sid, "fleet_size": len(fleet), "truck_capacity_bbl": _num(ds.truck_capacity_bbl),
                    "cost_per_trip": _num(ds.truck_cost)}
        if why:
            warnings.append(why)
        elif len(fleet) > len(keep):
            omitted.append(dict(COUNT_RULES["trucks"], count=len(fleet), what=f"trucks in {sid}'s fleet",
                                left_out=len(fleet) - len(keep), ids=fleet[len(keep):]))
        for k, trid in enumerate(keep, start=1):
            trucks.append({
                "id": trid, "label": f"{label_of(sid)} truck {k}", "capacity_bbl": _num(ds.truck_capacity_bbl),
                "home_id": yid,
                "minutes_to": {names.of[ds.facilities[fid].tank_id]: _num(ds.facilities[fid].response_min)
                               for fid in sysm.facility_ids},
                "_cycle_min": {names.of[ds.facilities[fid].tank_id]: _num(ds.facilities[fid].cycle_min)
                               for fid in sysm.facility_ids},
            })
            synthesized.append({"id": trid, "kind": "truck",
                                "from": f"ops.json systems {sid} emergency_fleet_size ({len(fleet)})"})

    scope = _scope_map(ds, names, sids)
    rules = []
    for kind in ("tank", "pump", "well", "pipeline", "disposal"):
        reals = [r for r in scope if names.kind[r] == kind]
        renamed = [r for r in reals if names.of[r] != r]
        if not renamed:
            continue
        entry = {"kind": kind, "rule": ID_RULES[kind]["rule"], "pattern": ID_RULES[kind]["pattern"],
                 "renamed": len(renamed), "kept": len(reals) - len(renamed),
                 "example": {renamed[0]: names.of[renamed[0]]}}
        if ID_RULES[kind].get("refs"):
            entry["refs"] = [dict(r) for r in ID_RULES[kind]["refs"]]
        if kind == "pipeline":
            entry["carried_by"] = "the pump's DISCHARGE valve, connects [pump, disposal well]"
        if any(r in names.uniquified for r in renamed):
            entry["made_unique"] = [r for r in renamed if r in names.uniquified]
        rules.append(entry)
    rules.append({"kind": "facility", "rule": NO_LIST_RULE, "carried_by": "its tank's pad (PAD_<n>) and the "
                  "tank's _facility; blueprint v1 has no facility list"})
    rules.append({"kind": "system", "rule": NO_LIST_RULE, "carried_by": "the whole blueprint (_source.systems)"})

    lat0, lon0 = plan["origin"]
    half = plan["half"]
    width = max(pos[k][0] + half[k] for k in pos) - min(pos[k][0] - half[k] for k in pos)
    meta = ds.network.get("meta")
    tz = meta.get("timezone") if isinstance(meta, dict) else None
    site = {"name": f"System {sids[0]}" if system is not None or len(sids) == 1 else f"{len(sids)} systems",
            "lat": lat0, "lon": lon0, "width_m": _m(max(width, 1.0))}
    if isinstance(tz, str) and tz:
        site["timezone"] = tz
    doc = {
        "version": 1,
        "name": f"{site['name']} ({ds.id})",
        "site": site,
        "tanks": tanks, "pumps": pumps, "valves": valves, "wells": wells, "disposal_wells": swds,
    }
    if trucks:
        doc["trucks"] = trucks
    if yards:
        doc["yards"] = yards
    doc["_source"] = {"dataset": ds.id, "system": system, "systems": sids, "read": ["network.json", "ops.json"]}
    doc["_id_map"] = scope
    doc["_id_rules"] = rules
    doc["_synthesized"] = synthesized
    doc["_layout"] = dict(_layout_info(plan), placed=("tanks, disposal wells and yards get x and y; pumps, wells "
                                                      "and trucks take the generator's pad and yard slots"))
    doc["_derived"] = {
        "rate_bbl_hr": "pump max_flow_bpd / 24", "water_bbl_hr": "well baseline_water_bpd / 24",
        "lost_usd_hr": "(oil_bpd x prices.oil_per_bbl + gas_mcfd x prices.gas_per_mcf) / 24",
        "shutdown_pct, warning_pct": "network.json defaults.tank_limits_pct", "pump_low_pct": "pump stop_level_pct",
        "max_bbl_hr": ("the sum over its pipelines of max(pump max_flow_bpd, pipeline max_bpd) / 24: the dataset "
                       "gives a disposal site no limit and says it never goes down, so it never limits a pump"),
        "trucks": "ops.json emergency_trucking truck_capacity_bbl; minutes_to is each facility's response_min",
    }
    if trucking:
        doc["_trucking"] = trucking
    if omitted:
        doc["_omitted"] = omitted
    longest = _longest_name(doc)
    if len(longest) > BLENDER_NAME_MAX:
        warnings.append(f"The generator would make the node name {longest} ({len(longest)} characters); Blender keeps "
                        f"{BLENDER_NAME_MAX}, so the build may fail its name check.")
    if warnings:
        doc["_warnings"] = warnings
    return doc


def field_assets(ds: Dataset, system: Optional[str] = None) -> list[dict]:
    """Every asset of the network (or of one system), in network order: each system, then for each of
    its facilities the facility, its wells, tank, pump and pipeline, then the disposal site, the
    truck yard and the trucks. With system None, any asset the telemetry names that network.json
    doesn't describe is listed last, as kind "unknown", so nothing in the readings is hidden.

    Each entry: id (the dataset's id; the yard and trucks have none, so theirs is synthesized and
    `synthetic` is true), kind, system, facility, label, lat, lon, latlon_from, x_m and y_m (meters in
    the system's layout, the same frame as to_blueprint(ds, system)), links (the ids it sends water
    to: well -> tank -> pump -> pipeline -> disposal site), fed_by (the reverse), blueprint_id (its id
    in to_blueprint(ds, system), None for kinds a blueprint has no list for), node (the glb node to
    highlight), metrics (what the telemetry reports for it). A pipeline also has path_m, path_latlon
    and pipes; a system and a facility have contains; a system has layout. Raises BlueprintError for
    an unknown system, an id used twice, or a point that is not a place on Earth.
    """
    names = _Names(ds)
    if system is not None:
        _system(ds, system)
    _check_places(ds, [system] if system is not None else list(ds.systems))
    metrics: dict[str, list[str]] = {}
    for a, m in ds.series_keys():
        metrics.setdefault(a, []).append(m)

    def entry(aid, kind, sid, fid, lat, lon, latlon_from, xy, links=(), fed_by=(), bid=None, node=None,
              synthetic=False, label=None, **extra):
        e = {"id": aid, "kind": kind, "system": sid, "facility": fid, "label": label or label_of(aid),
             "lat": lat, "lon": lon, "latlon_from": latlon_from,
             "x_m": None if xy is None else _m(xy[0]), "y_m": None if xy is None else _m(xy[1]),
             "links": list(links), "fed_by": list(fed_by), "blueprint_id": bid, "node": node,
             "metrics": sorted(metrics.get(aid, [])), "synthetic": synthetic}
        e.update(extra)
        return e

    out = []
    for sid in ([system] if system is not None else list(ds.systems)):
        sysm = ds.systems[sid]
        plan = _system_plan(ds, names, sid)
        pos = plan["pos"]
        yid = names.yard[sid]
        dpos = pos[sysm.disposal_id]
        out.append(entry(sid, "system", sid, None, sysm.center[0], sysm.center[1], "dataset", (0.0, 0.0),
                         label=f"System {label_of(sid)}",
                         contains=list(sysm.facility_ids) + [sysm.disposal_id, yid], layout=_layout_info(plan)))
        for fid in sysm.facility_ids:
            f = ds.facilities[fid]
            tid, pid, vid = names.of[f.tank_id], names.of[f.pump_id], names.of[f.pipeline_id]
            tank_xy = pos[fid]
            pump_xy = pad_point(tank_xy, *PUMP_SLOTS[0])
            out.append(entry(fid, "facility", sid, fid, f.lat, f.lon, "dataset", tank_xy, node=f"PAD_{pad_suffix(tid)}",
                             contains=f.well_ids + [f.tank_id, f.pump_id, f.pipeline_id]))
            for w, slot in zip(f.wells, well_slots(len(f.wells))):
                out.append(entry(w.id, "well", sid, fid, f.lat, f.lon, "facility", pad_point(tank_xy, *slot),
                                 links=[f.tank_id], bid=names.of[w.id], node=names.of[w.id]))
            out.append(entry(f.tank_id, "tank", sid, fid, f.lat, f.lon, "facility", tank_xy, links=[f.pump_id],
                             fed_by=f.well_ids, bid=tid, node=tid))
            out.append(entry(f.pump_id, "pump", sid, fid, f.lat, f.lon, "facility", pump_xy, links=[f.pipeline_id],
                             fed_by=[f.tank_id], bid=pid, node=pid))
            mid = ((pump_xy[0] + dpos[0]) / 2, (pump_xy[1] + dpos[1]) / 2)
            out.append(entry(f.pipeline_id, "pipeline", sid, fid, (f.lat + sysm.disposal_lat) / 2,
                             (f.lon + sysm.disposal_lon) / 2, "midpoint of its facility and disposal site", mid,
                             links=[sysm.disposal_id], fed_by=[f.pump_id], bid=vid, node=vid,
                             path_m=[[_m(pump_xy[0]), _m(pump_xy[1])], [_m(dpos[0]), _m(dpos[1])]],
                             path_latlon=[[f.lat, f.lon], [sysm.disposal_lat, sysm.disposal_lon]],
                             pipes=[f"PIPE_{pid}_{vid}", f"PIPE_{vid}_TRUNK"]))
        swd = names.of[sysm.disposal_id]
        out.append(entry(sysm.disposal_id, "disposal", sid, None, sysm.disposal_lat, sysm.disposal_lon, "dataset",
                         dpos, fed_by=[ds.facilities[fid].pipeline_id for fid in sysm.facility_ids], bid=swd, node=swd))
        yard_xy = pos[yid]
        out.append(entry(yid, "truck_yard", sid, None, sysm.truck_yard[0], sysm.truck_yard[1], "dataset", yard_xy,
                         bid=yid, node=yid, synthetic=True, label=f"{label_of(sid)} truck yard"))
        kept = len(_kept_trucks(ds, names, sid)[0])       # the fleet's first `kept` trucks are in the blueprint
        for k, trid in enumerate(names.trucks[sid]):
            built = k < kept
            out.append(entry(trid, "truck", sid, None, sysm.truck_yard[0], sysm.truck_yard[1], "truck_yard",
                             yard_point(yard_xy, k) if built else yard_xy, bid=trid if built else None,
                             node=trid if built else None, synthetic=True, label=f"{label_of(sid)} truck {k + 1}",
                             home=yid))
    if system is None:
        known = {e["id"] for e in out}
        for aid in sorted(a for a in metrics if a not in known):
            out.append(entry(aid, "unknown", None, None, None, None, None, None,
                             note="in telemetry.csv but not in network.json"))
    return out
