"""The node names a built field must contain, worked out from the blueprint alone.

Pure Python. build_field.py checks its own output against this list before it writes the
report, and test_generator.py checks every sample glb against it.
"""
from layout import suffix


def valve_base(vid):
    return vid[6:] if vid.startswith("VALVE_") and len(vid) > 6 else vid


def expected(bp):
    """Returns {name: why}. Small labels and signs are left out: the budget may drop them."""
    need = {}

    def add(why, *names):
        for n in names:
            need[n] = why

    for t in bp["tanks"]:
        tid, n = t["id"], suffix(t["id"])
        add("tank", tid)
        add("tank part", f"{tid}_LEVEL", f"{tid}_gauge", f"{tid}_lt", f"{tid}_lshh", f"{tid}_label")
        add("pad", f"PAD_{n}", f"PAD_{n}_lamps", f"LOADOUT_{n}")
        add("waypoint", f"STOP_{tid}", f"ROAD_JCT_{tid}")
        add("pipe", f"PIPE_INLET_{n}", f"PIPE_INLET_{n}_RISER", f"PIPE_{tid}_LOADOUT_{n}")
    for p in bp["pumps"]:
        pid, vid = p["id"], p["valve_id"]
        add("pump", pid)
        add("pump part", f"{pid}_rotor", f"{pid}_light")
        add("pipe", f"PIPE_{p['tank_id']}_{pid}")
        if vid:
            add("valve", vid)
            add("valve part", f"{vid}_wheel")
            add("pipe", f"PIPE_{pid}_{vid}", f"PIPE_{vid}_TRUNK")
        else:
            add("pipe", f"PIPE_{pid}_TRUNK")
    for x in bp["crossovers"]:
        add("valve", x["id"])
        add("valve part", f"{x['id']}_wheel")
        add("pipe", f"PIPE_{valve_base(x['id'])}_W", f"PIPE_{valve_base(x['id'])}_E")
    for w in bp["wells"]:
        add("well", w["id"])
        add("well part", f"{w['id']}_beam", f"{w['id']}_crank", f"{w['id']}_light")
        add("pipe", f"PIPE_{w['id']}_{w['tank_id']}")
    for s in bp["swds"]:
        add("disposal_well", s["id"])
        add("disposal well part", f"{s['id']}_gate", f"{s['id']}_lamps")
        add("waypoint", f"STOP_{s['id']}", f"ROAD_JCT_{s['id']}")
    for t in bp["trucks"]:
        add("truck", t["id"])
        add("truck part", f"{t['id']}_axle_1", f"{t['id']}_axle_2", f"{t['id']}_axle_3",
            f"{t['id']}_beacon", f"{t['id']}_headlights")
    if bp["yard"]:
        add("yard", bp["yard"]["id"])
        add("waypoint", f"STOP_{bp['yard']['id']}", f"ROAD_JCT_{bp['yard']['id']}")
    add("waypoint", "ROAD_END_W", "ROAD_END_E")
    add("camera", "CAM_OVERVIEW")
    if bp["pumps"]:
        add("pipe", "PIPE_TRUNK_0")
    return need


def asset_kinds(bp):
    """{asset ID: kind} as the glb carries it in extras."""
    kinds = {t["id"]: "tank" for t in bp["tanks"]}
    kinds.update({p["id"]: "pump" for p in bp["pumps"]})
    kinds.update({v["id"]: "valve" for v in bp["discharge"] + bp["crossovers"]})
    kinds.update({w["id"]: "well" for w in bp["wells"]})
    kinds.update({s["id"]: "disposal_well" for s in bp["swds"]})
    kinds.update({t["id"]: "truck" for t in bp["trucks"]})
    if bp["yard"]:
        kinds[bp["yard"]["id"]] = "yard"
    return kinds
