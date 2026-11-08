"""Turn an extraction (from Claude, or from a CSV) into blueprint.json plus a review list.

Assets keep scenario/field.json's shape, so the backend and generator read them as is.
IDs follow docs/CONTRACT.md: TANK_n, PUMP_n, VALVE_D_n, VALVE_XO_ab, WELL_n, SWD_n,
TRUCK_n, YARD_1. A number is copied only if it was on the plan. A missing number goes on
the review list, because the backend refuses to forecast a tank it lacks numbers for.
"""
import math
import re

from .schema import DEFAULT_SITE_WIDTH_M, ID_RE, REQUIRED_NUMBERS, REVIEW_CONFIDENCE


def _number(label):
    m = re.search(r"(\d+)", label or "")
    return int(m.group(1)) if m else None


def _key(label):
    return re.sub(r"[^a-z0-9]", "", (label or "").lower())


class _Namer:
    def __init__(self):
        self.used = set()

    def take(self, prefix, preferred=None):
        name = f"{prefix}_{preferred}" if preferred is not None else None
        if name is None or name in self.used:
            n = 1
            while f"{prefix}_{n}" in self.used:
                n += 1
            name = f"{prefix}_{n}"
        self.used.add(name)
        return name

    def exact(self, name):
        """Use this exact name, or name_2, name_3 if it is taken."""
        out, n = name, 2
        while out in self.used:
            out, n = f"{name}_{n}", n + 1
        self.used.add(out)
        return out


def _meta(item):
    return {"label": item.get("label"), "confidence": item.get("confidence", 1.0), "evidence": item.get("evidence", "")}


def assemble(extraction, *, aspect=0.5, positions="fraction", source=None):
    """positions is "fraction" (0 to 1 across the drawing, from Claude) or "meters" (from a CSV)."""
    items = extraction.get("items", [])
    review, namer = [], _Namer()
    width = extraction.get("meters_per_drawing_width") or DEFAULT_SITE_WIDTH_M
    bp = {"name": extraction.get("site_name") or "Uploaded field", "tanks": [], "pumps": [], "valves": [],
          "wells": [], "disposal_wells": [], "trucks": [], "yards": []}

    def place(asset, item):
        x, y = item.get("x"), item.get("y")
        if x is None or y is None:
            return
        if positions == "fraction":
            asset["x"], asset["y"] = round((x - 0.5) * width, 1), round((0.5 - y) * width * aspect, 1)
        else:
            asset["x"], asset["y"] = float(x), float(y)

    def flag(asset_id, item, issue):
        review.append({"id": asset_id, "label": item.get("label"), "issue": issue})

    by_kind = {}
    for item in items:
        by_kind.setdefault(item.get("kind"), []).append(item)

    tank_ids, tank_items = {}, {}
    for item in sorted(by_kind.get("tank", []), key=lambda i: (_number(i.get("label")) is None, _number(i.get("label")) or 0)):
        tid = namer.take("TANK", _number(item.get("label")))
        tank_ids[_key(item.get("label"))] = tid
        tank_items[tid] = item
        asset = {"id": tid, "_ingest": _meta(item)}
        if item.get("capacity_bbl") is not None:
            asset["capacity_bbl"] = item["capacity_bbl"]
        place(asset, item)
        bp["tanks"].append(asset)

    def find_tank(label, item=None):
        """Return (tank ID or None, guessed). guessed is True when it fell back to the nearest tank."""
        if label:
            if _key(label) in tank_ids:
                return tank_ids[_key(label)], False
            n = _number(label)
            if n is not None and f"TANK_{n}" in tank_items:
                return f"TANK_{n}", False
        placed_tanks = [t for t in tank_items if tank_items[t].get("x") is not None and tank_items[t].get("y") is not None]
        if item is not None and item.get("x") is not None and item.get("y") is not None and placed_tanks:
            near = min(placed_tanks, key=lambda t: math.hypot(tank_items[t]["x"] - item["x"], tank_items[t]["y"] - item["y"]))
            return near, True
        return None, False

    def note_guess(asset_id, item, tank):
        flag(asset_id, item, f"Linked to the nearest tank, {tank}, because the plan doesn't say which tank. Confirm it.")

    simple = {"disposal_well": ("SWD", "disposal_wells", {"rate_bbl_hr": "max_bbl_hr"}),
              "truck": ("TRUCK", "trucks", {"capacity_bbl": "capacity_bbl"}),
              "yard": ("YARD", "yards", {})}
    lookup = {}
    for kind, (prefix, key, fields) in simple.items():
        for item in by_kind.get(kind, []):
            aid = namer.take(prefix, _number(item.get("label")))
            lookup[_key(item.get("label"))] = aid
            asset = {"id": aid, "_ingest": _meta(item)}
            for src, dst in fields.items():
                if item.get(src) is not None:
                    asset[dst] = item[src]
            place(asset, item)
            bp[key].append(asset)

    pump_ids = {}
    for item in by_kind.get("pump", []):
        tank, guessed = find_tank(item.get("feeds_label"), item)
        pid = namer.take("PUMP", _number(tank) if tank else _number(item.get("label")))
        pump_ids[_key(item.get("label"))] = pid
        asset = {"id": pid, "_ingest": _meta(item)}
        if tank:
            asset["tank_id"] = tank
            if guessed:
                note_guess(pid, item, tank)
        else:
            flag(pid, item, "The plan doesn't show which tank this pump empties.")
        if item.get("rate_bbl_hr") is not None:
            asset["rate_bbl_hr"] = item["rate_bbl_hr"]
        place(asset, item)
        bp["pumps"].append(asset)

    for item in by_kind.get("well", []):
        wid = namer.take("WELL", _number(item.get("label")))
        tank, guessed = find_tank(item.get("feeds_label"), item)
        asset = {"id": wid, "_ingest": _meta(item)}
        if tank:
            asset["tank_id"] = tank
            if guessed:
                note_guess(wid, item, tank)
        else:
            flag(wid, item, "The plan doesn't show which tank this well flows to.")
        if item.get("rate_bbl_hr") is not None:
            asset["water_bbl_hr"] = item["rate_bbl_hr"]
        place(asset, item)
        bp["wells"].append(asset)

    for item in by_kind.get("crossover_valve", []):
        tanks = [t for t in (find_tank(lbl)[0] for lbl in item.get("connects_labels", [])) if t]
        if len(tanks) == 2:
            a, b = sorted(_number(t) for t in tanks)
            vid = namer.exact(f"VALVE_XO_{a}{b}" if max(a, b) < 10 else f"VALVE_XO_{a}_{b}")
        else:
            vid = namer.take("VALVE_XO")
            flag(vid, item, "This crossover doesn't clearly connect two tanks.")
        asset = {"id": vid, "kind": "CROSSOVER", "connects": tanks, "_ingest": _meta(item)}
        if item.get("rate_bbl_hr") is not None:
            asset["max_bbl_hr"] = item["rate_bbl_hr"]
        flag(vid, item, "Is this crossover manual or remote, and what flow can it carry? The plan doesn't say.")
        place(asset, item)
        bp["valves"].append(asset)

    for item in by_kind.get("discharge_valve", []):
        ends = [pump_ids.get(_key(lbl)) or lookup.get(_key(lbl)) for lbl in item.get("connects_labels", [])]
        pump = next((e for e in ends if e and e.startswith("PUMP_")), None)
        swd = next((e for e in ends if e and e.startswith("SWD_")), None)
        vid = namer.take("VALVE_D", _number(pump) if pump else _number(item.get("label")))
        asset = {"id": vid, "kind": "DISCHARGE", "connects": [e for e in (pump, swd) if e], "_ingest": _meta(item)}
        if pump:
            asset["pump_id"] = pump
        if not (pump and swd):
            flag(vid, item, "The plan doesn't show both the pump and the disposal well this valve sits between.")
        place(asset, item)
        bp["valves"].append(asset)

    review += missing_numbers(bp)
    for section in ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards"):
        for asset in bp[section]:
            conf = asset["_ingest"]["confidence"]
            if conf is not None and conf < REVIEW_CONFIDENCE:
                review.append({"id": asset["id"], "label": asset["_ingest"]["label"],
                               "issue": f"Claude is only {round(conf * 100)}% sure about this one. Check it."})
    bp["_ingest"] ={"source": source or {}, "unclear": extraction.get("unclear", [])}
    return bp, review


def missing_numbers(bp):
    """Review entries for every number the backend needs but the blueprint lacks."""
    out = []
    for kind, fields in REQUIRED_NUMBERS.items():
        for asset in bp.get(kind, []) or []:
            missing = [f for f in fields if f not in asset]
            if missing:
                out.append({"id": asset.get("id"), "label": (asset.get("_ingest") or {}).get("label"),
                            "issue": f"Missing {', '.join(missing)}. The backend won't forecast it until these are set."})
    for valve in bp.get("valves", []) or []:
        if valve.get("kind") == "CROSSOVER" and "max_bbl_hr" not in valve:
            out.append({"id": valve.get("id"), "label": (valve.get("_ingest") or {}).get("label"),
                        "issue": "Missing max_bbl_hr for this crossover."})
    return out


def validate(bp):
    """Hard errors that stop a build: bad or duplicate IDs, and references to IDs that don't exist."""
    errors, seen = [], set()
    sections = ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards")
    for section in sections:
        for asset in bp.get(section, []) or []:
            aid = asset.get("id")
            if not isinstance(aid, str) or not ID_RE.match(aid):
                errors.append(f"{section}: bad ID {aid!r}. IDs use capital letters, digits and underscores.")
            elif aid in seen:
                errors.append(f"Duplicate ID {aid}.")
            seen.add(aid)
    for section, field in (("pumps", "tank_id"), ("wells", "tank_id"), ("valves", "pump_id")):
        for asset in bp.get(section, []) or []:
            ref = asset.get(field)
            if ref is not None and ref not in seen:
                errors.append(f"{asset.get('id')}: {field} {ref} isn't in the blueprint.")
    for valve in bp.get("valves", []) or []:
        for ref in valve.get("connects", []) or []:
            if ref not in seen:
                errors.append(f"{valve.get('id')}: connects to {ref}, which isn't in the blueprint.")
    if not bp.get("tanks"):
        errors.append("The blueprint has no tanks.")
    return errors
