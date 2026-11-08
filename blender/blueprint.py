"""Read and check a blueprint.json before anything is built.

A blueprint is scenario/field.json plus optional "x" and "y" (meters) on any asset.
This module uses only the standard library, so it runs inside Blender and in plain
Python (the tests check validation without starting Blender).

check() returns (bp, errors, warnings). Any error means the build must stop and no
glb is written. bp is a plain dict of normalized assets in blueprint order.
"""
import json
import math
import re

ID_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
LISTS = ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards")
COUNT_LIMITS = {"tanks": (1, 20), "disposal_wells": (1, 4), "trucks": (0, 6), "yards": (0, 1)}
WELLS_PER_TANK = 8
PUMPS_PER_TANK = 2
XO_PER_TANK = 4
MAX_COORD = 5000.0


def load(path):
    """Returns (data, errors). Errors cover unreadable files and invalid JSON."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), []
    except OSError as e:
        return None, [f"cannot read blueprint {path}: {e.strerror or e}"]
    except json.JSONDecodeError as e:
        return None, [f"blueprint {path} is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}"]


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _position(entry, where, errors):
    has_x, has_y = "x" in entry, "y" in entry
    if not has_x and not has_y:
        return None
    if has_x != has_y:
        errors.append(f"{where} has only one of x and y; give both or neither")
        return None
    x, y = entry["x"], entry["y"]
    if not (_num(x) and _num(y)):
        errors.append(f"{where} x and y must be numbers in meters")
        return None
    if abs(x) > MAX_COORD or abs(y) > MAX_COORD:
        errors.append(f"{where} position ({x}, {y}) is more than {MAX_COORD:.0f} m from the origin")
        return None
    return (float(x), float(y))


def _entries(data, key, errors):
    """The list under key as (index, dict) pairs, with structural errors recorded."""
    raw = data.get(key, [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        errors.append(f'"{key}" must be a list')
        return []
    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"{key}[{i}] must be an object")
            continue
        out.append((i, entry))
    return out


def check(data):
    errors, warnings = [], []
    if not isinstance(data, dict):
        return None, ["the blueprint must be a JSON object"], warnings

    raw = {key: _entries(data, key, errors) for key in LISTS}
    seen = {}        # id -> where it was first defined
    kinds = {}       # id -> list key
    for key in LISTS:
        for i, entry in raw[key]:
            where = f"{key}[{i}]"
            aid = entry.get("id")
            if not isinstance(aid, str):
                errors.append(f"{where} needs an \"id\" string")
                continue
            if not ID_RE.match(aid):
                errors.append(f'{where} id "{aid}" must match ^[A-Z][A-Z0-9_]*$ '
                              "(capital letters, digits, and underscores, starting with a letter)")
                continue
            if aid in seen:
                errors.append(f'duplicate ID "{aid}" in {seen[aid]} and {where}')
                continue
            seen[aid] = where
            kinds[aid] = key

    def ref(where, field, value, allowed):
        """Check one reference. Returns the ID if it names an asset of an allowed kind."""
        if not isinstance(value, str):
            errors.append(f"{where} {field} must be an asset ID string")
            return None
        if value not in kinds:
            errors.append(f'{where} {field} "{value}" is not an asset in this blueprint')
            return None
        if allowed and kinds[value] not in allowed:
            errors.append(f'{where} {field} "{value}" is a {kinds[value][:-1].replace("_", " ")}, '
                          f"expected one of: {', '.join(allowed)}")
            return None
        return value

    def valid(key):
        for i, entry in raw[key]:
            aid = entry.get("id")
            if isinstance(aid, str) and seen.get(aid) == f"{key}[{i}]":
                yield i, entry, f"{key}[{i}] {aid}"

    bp = {"name": data.get("name") if isinstance(data.get("name"), str) else "",
          "tanks": [], "pumps": [], "discharge": [], "crossovers": [], "wells": [],
          "swds": [], "trucks": [], "yard": None}

    for i, e, where in valid("tanks"):
        cap = e.get("capacity_bbl")
        bp["tanks"].append({"id": e["id"], "capacity_bbl": cap if _num(cap) else None,
                            "pos": _position(e, where, errors)})
    for i, e, where in valid("disposal_wells"):
        bp["swds"].append({"id": e["id"], "pos": _position(e, where, errors)})
    for i, e, where in valid("yards"):
        if bp["yard"] is None:
            bp["yard"] = {"id": e["id"], "pos": _position(e, where, errors)}
    for i, e, where in valid("wells"):
        tank = ref(where, "tank_id", e.get("tank_id"), ["tanks"])
        bp["wells"].append({"id": e["id"], "tank_id": tank, "pos": _position(e, where, errors)})

    valves = {e["id"]: (e, where) for _, e, where in valid("valves")}
    pumps = []
    for i, e, where in valid("pumps"):
        tank = ref(where, "tank_id", e.get("tank_id"), ["tanks"])
        valve = ref(where, "valve_id", e["valve_id"], ["valves"]) if e.get("valve_id") is not None else None
        if e.get("swd_id") is not None:
            ref(where, "swd_id", e["swd_id"], ["disposal_wells"])
        pumps.append({"id": e["id"], "tank_id": tank, "valve_id": valve, "pos": _position(e, where, errors)})

    # valves: discharge valves ride on a pump, crossovers join two tanks
    pump_ids = {p["id"] for p in pumps}
    owner = {}
    for vid, (e, where) in valves.items():
        kind = str(e.get("kind", "")).upper()
        connects = e.get("connects")
        if connects is not None and not (isinstance(connects, list) and all(isinstance(c, str) for c in connects)):
            errors.append(f"{where} connects must be a list of asset IDs")
            connects = None
        for c in connects or []:
            ref(where, "connects", c, None)
        if not kind:
            tank_ends = [c for c in connects or [] if kinds.get(c) == "tanks"]
            kind = "CROSSOVER" if len(tank_ends) == 2 else "DISCHARGE"
            warnings.append(f"{where} has no kind; treating it as {kind}")
        if "x" in e or "y" in e:
            _position(e, where, errors)
            warnings.append(f"{where} x and y are ignored: valves sit on the pipe they control")
        if kind == "CROSSOVER":
            ends = connects or []
            if len(ends) != 2 or any(kinds.get(c) != "tanks" for c in ends):
                errors.append(f"{where} is a CROSSOVER, so connects must name exactly two tanks")
            elif ends[0] == ends[1]:
                errors.append(f"{where} connects a tank to itself")
            else:
                bp["crossovers"].append({"id": vid, "a": ends[0], "b": ends[1]})
        elif kind == "DISCHARGE":
            pid = e.get("pump_id")
            if pid is not None:
                pid = ref(where, "pump_id", pid, ["pumps"])
            elif connects and kinds.get(connects[0]) == "pumps":
                pid = connects[0]
            owner[vid] = pid
        else:
            errors.append(f'{where} kind "{e.get("kind")}" is not supported; use DISCHARGE or CROSSOVER')

    # pair each pump with its discharge valve, from either side of the link
    taken = {}
    for p in pumps:
        vid = p["valve_id"]
        if vid is None:
            vid = next((v for v, pid in owner.items() if pid == p["id"]), None)
        elif vid not in owner:
            if vid in valves and any(x["id"] == vid for x in bp["crossovers"]):
                errors.append(f'{p["id"]} valve_id "{vid}" is a CROSSOVER valve, not a discharge valve')
            vid = None
        elif owner[vid] not in (None, p["id"]):
            errors.append(f'{p["id"]} names {vid}, but {vid} belongs to {owner[vid]}')
            vid = None
        if vid is not None:
            if vid in taken:
                errors.append(f"{vid} is the discharge valve of both {taken[vid]} and {p['id']}")
                continue
            taken[vid] = p["id"]
        else:
            warnings.append(f"{p['id']} has no discharge valve; its line runs straight to the trunk")
        p["valve_id"] = vid
        bp["pumps"].append(p)
        if vid:
            bp["discharge"].append({"id": vid, "pump_id": p["id"]})
    uses = {pid: vid for vid, pid in taken.items()}
    for vid, pid in owner.items():
        if vid in taken:
            continue
        if pid in uses:
            errors.append(f"{vid} names {pid}, but {pid} already belongs to {uses[pid]}; "
                          "a pump has one discharge valve")
        else:
            errors.append(f"{vid} is a discharge valve, but no pump in the blueprint uses it"
                          + (f" ({pid} is not a pump)" if pid and pid not in pump_ids else ""))

    for i, e, where in valid("trucks"):
        home = e.get("home_id")
        if home is not None:
            ref(where, "home_id", home, ["yards"])
        minutes = e.get("minutes_to")
        if isinstance(minutes, dict):
            for target in minutes:
                ref(where, "minutes_to", target, None)
        bp["trucks"].append({"id": e["id"], "pos": _position(e, where, errors)})

    for j, link in enumerate(data.get("connections") or []):
        ends = link if isinstance(link, list) else [link.get("from"), link.get("to")] if isinstance(link, dict) else []
        if len(ends) != 2:
            errors.append(f"connections[{j}] must be [from, to] or {{\"from\": ..., \"to\": ...}}")
            continue
        for end in ends:
            ref(f"connections[{j}]", "end", end, None)
    if data.get("connections"):
        warnings.append("connections are checked but not drawn; piping follows tank_id, pump, and valve fields")

    # scenario fields the geometry does not use: warn, do not block
    initial = data.get("initial", {}) if isinstance(data.get("initial"), dict) else {}
    for tid in (initial.get("tank_pct") or {}):
        if tid not in kinds:
            warnings.append(f'initial.tank_pct names "{tid}", which is not an asset')
    for j, fault in enumerate(data.get("faults") or []):
        if isinstance(fault, dict) and fault.get("asset_id") not in kinds:
            warnings.append(f'faults[{j}] asset_id "{fault.get("asset_id")}" is not an asset')

    # field size
    sizes = {"tanks": len(bp["tanks"]), "disposal_wells": len(bp["swds"]),
             "trucks": len(bp["trucks"]), "yards": len(raw["yards"])}
    for key, (lo, hi) in COUNT_LIMITS.items():
        if not lo <= sizes[key] <= hi:
            label = key.replace("_", " ")
            errors.append(f"{sizes[key]} {label}: the generator builds {lo} to {hi} {label}")
    if sizes["yards"] == 0:
        warnings.append("no yard in the blueprint; trucks park south of the road with no YARD asset")
    per_tank = {t["id"]: {"wells": 0, "pumps": 0, "xo": 0} for t in bp["tanks"]}
    for w in bp["wells"]:
        if w["tank_id"] in per_tank:
            per_tank[w["tank_id"]]["wells"] += 1
    for p in bp["pumps"]:
        if p["tank_id"] in per_tank:
            per_tank[p["tank_id"]]["pumps"] += 1
    for x in bp["crossovers"]:
        for t in (x["a"], x["b"]):
            per_tank[t]["xo"] += 1
    for tid, c in per_tank.items():
        if c["wells"] > WELLS_PER_TANK:
            errors.append(f"{tid} has {c['wells']} wells; the generator builds 0 to {WELLS_PER_TANK} per tank")
        if c["pumps"] > PUMPS_PER_TANK:
            errors.append(f"{tid} has {c['pumps']} pumps; the generator builds 0 to {PUMPS_PER_TANK} per tank")
        if c["xo"] > XO_PER_TANK:
            errors.append(f"{tid} is on {c['xo']} crossovers; the generator builds up to {XO_PER_TANK} per tank")
        if c["pumps"] == 0:
            warnings.append(f"{tid} has no pump, so nothing drains it to the trunk")
    return (None if errors else bp), errors, warnings


def counts(bp):
    return {"tanks": len(bp["tanks"]), "pumps": len(bp["pumps"]),
            "discharge_valves": len(bp["discharge"]), "crossover_valves": len(bp["crossovers"]),
            "wells": len(bp["wells"]), "disposal_wells": len(bp["swds"]),
            "trucks": len(bp["trucks"]), "yards": 1 if bp["yard"] else 0}
