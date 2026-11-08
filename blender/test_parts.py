"""Tests for equipment types (#47) and the generated parts. test_generator.py runs these;
they need its ok() and its Blender runner, so they take them as arguments.

- Validation (no Blender): the equipment list, type names and aliases, placeholders, references.
- A blueprint with one of each equipment type, an unknown type, and an item with no type
  builds with every name, in its footprint, off the pads and the road.
- parts_preview.py builds every equipment model and scenery part inside its triangle limit.
"""
import copy
import json
import math
import os

import blueprint
import siting
from glb_read import Glb, read_glb, touch

HERE = os.path.dirname(os.path.abspath(__file__))
EQUIPMENT = [
    {"id": "SEP_1", "type": "SEPARATOR", "tank_id": "TANK_1"},
    {"id": "HT_1", "type": "HEATER_TREATER", "tank_id": "TANK_1"},
    {"id": "FLARE_1", "type": "FLARE"},
    {"id": "COMP_1", "type": "COMPRESSOR", "connects": ["SWD_1"]},
    {"id": "LACT_1", "type": "LACT", "tank_id": "TANK_2", "label": "Sales LACT"},
    {"id": "METER_1", "type": "meter run", "x": 40.0, "y": -45.0},
    {"id": "EQ_1", "type": "GLYCOL_DEHYDRATOR", "label": "Glycol dehy"},
    {"id": "EQ_2"},
]
PART_LIMITS = {"SEP_1": 2500, "HT_1": 2500, "FLARE_1": 2500, "COMP_1": 2500, "LACT_1": 2500, "METER_1": 2500,
               "EQ_1": 800, "SCENERY_yucca": 1200, "SCENERY_prickly_pear": 1800,
               "SCENERY_barbed_wire_fence": 700, "SCENERY_cattle_guard": 1000}


def equipment_blueprint(small):
    data = copy.deepcopy(small)
    data["name"] = "Variant: equipment"
    data["equipment"] = copy.deepcopy(EQUIPMENT)
    return data


def test_validation(ok, small):
    bp, errs, warns = blueprint.check(equipment_blueprint(small))
    types = {e["id"]: (e["type"], e["model"]) for e in bp["equipment"]} if bp else {}
    ok(not errs and types.get("METER_1") == ("METER_RUN", "METER_RUN") and types.get("EQ_2") == ("OTHER", None),
       f"equipment: types and aliases are read, a missing type is OTHER {errs[:1]}")
    ok(sum("labeled placeholder" in w for w in warns) == 2, "equipment: unknown types are a warning, never an error")

    def broken(change, expect, what):
        data = equipment_blueprint(small)
        change(data)
        errs = blueprint.check(data)[1]
        ok(any(expect in e for e in errs), f"equipment: rejects {what}: {errs[:1]}")

    broken(lambda d: d["equipment"][0].update(tank_id="TANK_9"), "not an asset", "an unknown tank_id")
    broken(lambda d: d["equipment"][0].update(tank_id="PUMP_1"), "expected one of", "a tank_id that is a pump")
    broken(lambda d: d["equipment"][3].update(connects=["SWD_9"]), "not an asset", "an unknown connects end")
    broken(lambda d: d["equipment"].append({"id": "TANK_1", "type": "FLARE"}), "duplicate ID", "an ID used twice")
    broken(lambda d: d["equipment"].extend({"id": f"EQ_{i}", "type": "X"} for i in range(3, 30)), "0 to 30",
           "more than 30 items")
    data = equipment_blueprint(small)
    data["connections"] = [["COMP_1", "SWD_1"]]
    ok(not blueprint.check(data)[1], "equipment: a connection may end at equipment")


def check_build(ok, rep, glb, data, label="equipment"):
    """Checks on a built field with the EQUIPMENT list."""
    items = {e["id"]: e for e in data["equipment"]}
    extras = {nd["name"]: nd.get("extras", {}) for nd in glb.nodes if nd.get("name") in items}
    ok(all(extras.get(i, {}).get("kind") == "equipment" for i in items) and rep["kinds"].get("equipment") == len(items),
       f"{label}: all {len(items)} items are asset nodes of kind equipment")
    placeholders = [i for i, e in extras.items() if e.get("model") == "placeholder"]
    ok(sorted(placeholders) == ["EQ_1", "EQ_2"] and all(f"{i}_label" in glb.by_name for i in placeholders),
       f"{label}: unknown types build as labeled placeholders {placeholders}")
    ok(extras["METER_1"].get("equipment_type") == "METER_RUN" and extras["LACT_1"].get("label") == "Sales LACT",
       f"{label}: extras carry equipment_type and label")
    x, y = glb.plan_xy("METER_1")
    ok(abs(x - 40) < 0.01 and abs(y + 45) < 0.01, f"{label}: a given x and y is honored ({x:.1f}, {y:.1f})")
    near = {"LACT_1": "TANK_2", "SEP_1": "TANK_1", "COMP_1": "SWD_1"}
    dist = {i: math.dist(glb.plan_xy(i), glb.plan_xy(a)) for i, a in near.items()}
    ok(all(d < 45 for d in dist.values()), f"{label}: items sit near their tank or link {dist}")
    model = {e["id"]: blueprint.equipment_type(e.get("type"))[1] for e in data["equipment"]}
    loose = []
    for i in items:
        w, d = siting.FOOTPRINTS[model[i]]
        b, (cx, cy) = glb.box(i), glb.plan_xy(i)
        if b[0] < cx - w / 2 - 0.2 or b[1] > cx + w / 2 + 0.2 or b[2] < cy - d / 2 - 0.2 or b[3] > cy + d / 2 + 0.2:
            loose.append(i)
    ok(not loose, f"{label}: every model fits its footprint in siting.FOOTPRINTS {loose}")
    pads = [n for n in glb.by_name if n.startswith("PAD_") and n.count("_") == 1]
    hits = [f"{i}/{p}" for i in items for p in pads if touch(glb.box(i), glb.box(p), -0.05)]
    ok(not hits, f"{label}: no equipment on a tank pad {hits[:4]}")


def test_preview(ok, blender, tmp):
    out = os.path.join(tmp, "parts")
    p = blender(["--out", out], script=os.path.join(HERE, "parts_preview.py"))
    if not ok(p.returncode == 0, "parts_preview: Blender exited 0"):
        print("\n".join((p.stdout + p.stderr).splitlines()[-15:]))
        return
    with open(os.path.join(out, "parts_report.json")) as f:
        rep = json.load(f)
    parts = rep["parts"]
    ok(set(parts) == set(PART_LIMITS), f"parts_preview: every model and scenery part built ({len(parts)})")
    over = {k: v for k, v in parts.items() if not 0 < v <= PART_LIMITS.get(k, 0)}
    ok(not over, f"parts_preview: every part inside its triangle limit {over or parts}")
    names = [nd.get("name", "") for nd in read_glb(os.path.join(out, "parts.glb"))["nodes"]]
    ok(len(names) == len(set(names)) and not any("." in n for n in names), "parts_preview: node names are unique")
    g = Glb(os.path.join(out, "parts.glb"))
    ok(all(not g.nodes[g.idx(k)].get("extras", {}).get("asset_id") for k in parts if k.startswith("SCENERY_")),
       "parts_preview: scenery carries no asset ID")
