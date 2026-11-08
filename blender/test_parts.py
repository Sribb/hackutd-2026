"""Tests for equipment types (#47), IoT sensors and RTUs (#46), and the generated parts.
test_generator.py runs these; they need its ok() and its Blender runner, so they take them
as arguments.

- Validation (no Blender): the equipment list, type names and aliases, placeholders, references.
- A blueprint with one of each equipment type, an unknown type, and an item with no type
  builds with every name, in its footprint, off the pads and the road.
- Every build has a SENSOR_<asset> under each monitored asset and an RTU_PAD_n on each pad,
  with M_LAMP lights. A blueprint whose connectors name every kind of asset mounts on each.
- parts_preview.py builds every equipment model and scenery part inside its triangle limit.
"""
import copy
import json
import math
import os

import blueprint
import names
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


IOT_SIGNALS = [("TANK_2", "level_pct"), ("PUMP_1", "running"), ("PUMP_1", "motor_amps"), ("VALVE_D_2", "position"),
               ("WELL_1", "running"), ("SWD_1", "pressure_psi"), ("TRUCK_1", "running"), ("YARD_1", "running"),
               ("SEP_1", "pressure_psi"), ("TANK_99", "level_pct")]


def iot_blueprint(small):
    """Connectors that name every kind of asset, plus one signal on an asset that doesn't exist."""
    data = equipment_blueprint(small)
    data["name"] = "Variant: IoT"
    data["connectors"] = [
        {"id": "CONN_SCADA", "kind": "mqtt", "config": {"host": "10.0.0.5"},
         "signals": [{"asset_id": a, "signal": s} for a, s in IOT_SIGNALS[:5]]},
        {"id": "CONN_RADIO", "kind": "modbus", "signals": [{"asset_id": a, "signal": s} for a, s in IOT_SIGNALS[4:]]},
    ]
    return data


def check_iot(ok, label, glb, bp):
    """Every build: a sensor under each monitored asset, an RTU on each pad, lights on M_LAMP."""
    def material(name):
        nd = glb.nodes[glb.idx(name)]
        prim = glb.g["meshes"][nd["mesh"]]["primitives"][0] if "mesh" in nd else {}
        return glb.g["materials"][prim["material"]]["name"] if "material" in prim else None

    def ancestors(name):
        i, out = glb.idx(name), []
        while i in glb.parent:
            i = glb.parent[i]
            out.append(glb.nodes[i].get("name"))
        return out

    want = names.sensors(bp)
    built = sorted(n for n in glb.by_name if glb.nodes[glb.idx(n)].get("extras", {}).get("kind") == "sensor")
    ok(built == sorted(f"SENSOR_{a}" for a, _, _ in want), f"{label}: {len(want)} sensor nodes, one per monitored asset")
    bad = []
    for aid, signals, conns in want:
        s = f"SENSOR_{aid}"
        ex = glb.nodes[glb.idx(s)].get("extras", {}) if s in glb.by_name else {}
        if (aid not in ancestors(s) if s in glb.by_name else True) or ex.get("for_asset") != aid \
                or ex.get("signals") != ",".join(signals) or ex.get("connectors") != ",".join(conns) \
                or material(f"{s}_light") != "M_LAMP" or math.dist(glb.plan_xy(s), glb.plan_xy(aid)) > 16:
            bad.append(aid)
    ok(not bad, f"{label}: each sensor sits on its asset, under it, with signals and an M_LAMP light {bad[:4]}")
    rtus = []
    for t in bp["tanks"]:
        n = names.suffix(t["id"])
        pad, r = f"PAD_{n}", f"RTU_PAD_{n}"
        good = r in glb.by_name and pad in ancestors(r) and material(f"{r}_light") == "M_LAMP"
        if good:
            (x, y), g = glb.plan_xy(r), glb.box(f"PAD_{n}_gravel")
            good = glb.box(f"{r}_antenna")[5] > 7 and g[0] < x < g[1] and g[2] < y < g[3]
        rtus.append(good)
    ok(all(rtus), f"{label}: an RTU with a light and an antenna on each of {len(rtus)} pads, inside the pad")


def check_iot_variant(ok, rep, glb, data):
    bp = blueprint.check(data)[0]
    mounted = {a for a, _, _ in names.sensors(bp)}
    kinds = {names.asset_kinds(bp)[a] for a in mounted}
    ok(mounted == {a for a, _ in IOT_SIGNALS[:-1]} and len(kinds) == 8,
       f"iot: signals on 8 kinds of asset get sensors, the unknown asset is skipped {sorted(kinds)}")
    ok(any("TANK_99" in w for w in rep["warnings"]), "iot: a signal on an unknown asset is a warning")
    ex = glb.nodes[glb.idx("SENSOR_PUMP_1")].get("extras", {})
    ok(ex.get("signals") == "running,motor_amps" and ex.get("connectors") == "CONN_SCADA",
       f"iot: a sensor lists every signal and connector on its asset {ex}")
    ex = glb.nodes[glb.idx("SENSOR_WELL_1")].get("extras", {})
    ok(ex.get("connectors") == "CONN_SCADA,CONN_RADIO", "iot: an asset on two connectors gets one sensor")
    ok(rep["iot"] == {"sensors": len(mounted), "rtus": len(bp["tanks"])}, f"iot: the report counts {rep['iot']}")


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
    bp = blueprint.check(small)[0]
    ok([a for a, _, _ in names.sensors(bp)] == [t["id"] for t in bp["tanks"]] + [p["id"] for p in bp["pumps"]],
       "iot: with no connectors, every tank and pump gets a sensor")
    data = copy.deepcopy(small)
    data["connectors"] = [{"id": "CONN_EMPTY", "kind": "sim", "signals": []}]
    ok(names.sensors(blueprint.check(data)[0]) == [], "iot: connectors with no signals mount no sensors")


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
