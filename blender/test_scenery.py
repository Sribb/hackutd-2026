"""Tests for the desert scenery pass (#88) and the land around the lease (#98).
test_generator.py runs these with its ok() and its Blender runner.

- The plan (no Blender): deterministic for a seed, inside its triangle budget, inside the
  fence, and off everything in siting.occupied(), with the fence dropped when it can't fit.
- The terrain (no Blender): its first loop is exactly the lease ground's edge, no triangle
  folds, it reaches the horizon on every side, and the mesas sit on the horizon. A pad's berm
  leaves gaps for flowlines and blockers.
- Every build: one SCENERY root, every node under it named SCENERY_*, none with an asset
  ID, kit copies off pads, roads, assets, and truck waypoints, textured ground and road, the
  terrain and mesas under SCENERY and nothing else past the lease ground, vertex colors on the
  ground and road, and black HDPE flowlines.
- --no-scenery builds the plain field, and two builds of one blueprint are identical.
"""
import json
import math
import os

import blueprint
import layout
import siting
import terrain
from glb_read import Glb, touch


def test_terrain(ok, root):
    bp = blueprint.check(json.load(open(os.path.join(root, "scenario", "field.json"))))[0]
    L = layout.plan(bp)
    for label, b, rings in (("default", L["ground"], terrain.RINGS), ("wide", (-612.5, 497.5, -140.0, 121.0), terrain.RINGS),
                            ("lite", L["ground"], terrain.RINGS_LITE)):
        pts, tris, first = terrain.ring(b, rings)
        x0, x1, y0, y1 = b
        nx, ny = terrain.ground_grid(b)
        edge = {(x0 + (x1 - x0) * i / nx, y) for i in range(nx + 1) for y in (y0, y1)}
        edge |= {(x, y0 + (y1 - y0) * j / ny) for j in range(ny + 1) for x in (x0, x1)}
        ok(set(pts[:first]) == edge and first == len(edge),
           f"terrain {label}: its first loop is exactly the lease ground's {first} edge vertices")
        folded = [t for t in tris if terrain.signed_area(pts[t[0]], pts[t[1]], pts[t[2]]) <= 1e-9]
        ok(not folded and len(tris) < 9000, f"terrain {label}: {len(tris)} triangles, none folded")
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        far = {}
        for x, y in pts:
            k = int((math.degrees(math.atan2(y - cy, x - cx)) + 180) // 30) % 12
            far[k] = max(far.get(k, 0.0), terrain.outside(x, y, b))
        reach = min(far.get(k, 0.0) for k in range(12))
        ok(reach > 0.9 * terrain.HORIZON, f"terrain {label}: reaches {reach:.0f} m past the lease on every side")
        ok(all(terrain.height(x, y, b) == terrain.lease_height(x, y) for x, y in pts[:first]),
           f"terrain {label}: heights meet the lease ground at its edge")
    b = L["ground"]
    cx, cy = (b[0] + b[1]) / 2, (b[2] + b[3]) / 2
    ms = terrain.mesas(b, bp["name"])
    dist = [math.hypot(x - cx, y - cy) for v, _, _ in ms for x, y, _ in v]
    tris = sum(len(f) - 2 for _, fs, _ in ms for f in fs)
    ok(len(ms) == 24 and 2000 < min(dist) and max(dist) < terrain.HORIZON + 250 and tris < 2500,
       f"mesas: {len(ms)} on the horizon, {min(dist):.0f} to {max(dist):.0f} m out, {tris} triangles")
    ok(terrain.mesas(b, bp["name"]) == ms, "mesas: the same seed gives the same mesas")
    frame = layout.Frame(0.0, 0.0)
    line = [(-5.5, 76.0), (-5.5, 59.5), (-1.0, 59.5)]
    runs = siting.berm_runs([line], [((10.0, 18.0, 40.0, 50.0), "a test blocker")], frame)
    pts = [p for r in runs for p in r]
    ok(len(runs) >= 3 and not any(abs(x + 5.5) < 1.2 and y > 60 for x, y in pts)
       and not any(13.0 < x < 15.0 and 39.0 < y < 51.0 for x, y in pts),
       f"berm: {len(runs)} runs, with gaps where a flowline crosses and a blocker sits")


def test_plan(ok, root):
    bp = blueprint.check(json.load(open(os.path.join(root, "scenario", "field.json"))))[0]
    L = layout.plan(bp)
    plan = siting.plan_scenery(L, 130_000, bp["name"])
    ok(plan == siting.plan_scenery(layout.plan(bp), 130_000, bp["name"]), "scenery plan: the same seed gives the same plan")
    ok(plan["scatter"] != siting.plan_scenery(L, 130_000, "another field")["scatter"],
       "scenery plan: another seed gives another scatter")
    est = sum(siting.SPECIES[k][0] for k, *_ in plan["scatter"])
    ok(plan["fence"] and len(plan["scatter"]) > 100 and est <= siting.SCATTER_MAX,
       f"scenery plan: a fence and {len(plan['scatter'])} plants and rocks in budget ({est} triangles)")
    taken = siting.occupied(L)
    gx0, gx1, gy0, gy1 = L["ground"]
    bad = [s for s in plan["scatter"] if siting.clash(siting.around(s[1], s[2], 0.5, 0.5), taken, 1.0)
           or not gx0 + 2 < s[1] < gx1 - 2 or not gy0 + 2 < s[2] < gy1 - 2]
    ok(not bad, f"scenery plan: nothing on pads, roads, pipes, waypoints, or outside the fence {bad[:2]}")
    ok(all(abs(y) < 1e-6 for _, y in plan["guards"]) and len(plan["guards"]) == 2,
       "scenery plan: a cattle guard where the lease road leaves at each end")
    small = siting.plan_scenery(L, 3000, bp["name"])
    none = siting.plan_scenery(L, -5000, bp["name"])
    ok(not small["fence"] and not none["scatter"] and not none["fence"] and none["dropped"],
       f"scenery plan: a small budget drops the fence, none drops everything {none['dropped']}")


def check_build(ok, label, rep, glb):
    """Every build with scenery on."""
    sc = rep.get("scenery", {})
    if not ok(sc.get("on") and "SCENERY" in glb.by_name, f"{label}: a SCENERY root ({sc.get('items')})"):
        return
    root = glb.idx("SCENERY")
    under, stack = [], list(glb.nodes[root].get("children", []))
    while stack:
        i = stack.pop()
        under.append(i)
        stack += glb.nodes[i].get("children", [])
    names = [glb.nodes[i].get("name", "") for i in under]
    ok(root not in glb.parent and all(n.startswith("SCENERY_") for n in names)
       and not any("asset_id" in glb.nodes[i].get("extras", {}) for i in under + [root]),
       f"{label}: SCENERY is a root, its {len(names)} nodes are SCENERY_*, none has an asset ID")
    ok(sc["triangles"] + sc["field_triangles"] == rep["triangles"],
       f"{label}: the report splits {rep['triangles']} triangles into field and scenery ({sc['triangles']})")
    mats = {m["name"]: m for m in glb.g.get("materials", [])}
    ok(all("baseColorTexture" in mats.get(m, {}).get("pbrMetallicRoughness", {}) for m in ("M_GROUND", "M_ROAD")),
       f"{label}: the ground and the road are textured")
    blocked = [n for n in glb.by_name if n.startswith("PAD_") and n.count("_") == 1]
    blocked += [n for n in glb.by_name if glb.nodes[glb.idx(n)].get("extras", {}).get("asset_id")]
    walls = [glb.box(n) for n in blocked if glb.box(n)]
    walls += [(x - 3, x + 3, y - 3, y + 3) for n in glb.by_name if n.startswith(("STOP_", "ROAD_JCT_"))
              for x, y in [glb.plan_xy(n)]]
    w, e = glb.plan_xy("ROAD_END_W")[0], glb.plan_xy("ROAD_END_E")[0]
    walls.append((w, e, -3.5, 3.5))
    copies = [n for n in names if n.split("_")[-1].isdigit()]
    hits = [n for n in copies if any(touch(glb.box(n), b, -0.05) for b in walls)]
    ok(not hits, f"{label}: {len(copies)} kit copies stay off pads, assets, the road, and truck stops {hits[:4]}")

    ground, terr = glb.box("SITE_ground"), glb.box("SCENERY_terrain") if "SCENERY_terrain" in glb.by_name else None
    ok(terr and ("SCENERY_mesas" in glb.by_name or "mesas" in sc["dropped"]) and all(terr[i] < ground[i] - 4500 for i in (0, 2))
       and all(terr[i] > ground[i] + 4500 for i in (1, 3)),
       f"{label}: the terrain runs to the horizon with mesas on it, under SCENERY")
    scenery = set(under) | {root}
    past = []
    for i, nd in enumerate(glb.nodes):
        if i in scenery or "mesh" not in nd:
            continue
        b = glb.box(nd["name"])
        if b and not (ground[0] - 1 <= b[0] and b[1] <= ground[1] + 1 and ground[2] - 1 <= b[2] and b[3] <= ground[3] + 1):
            past.append(nd["name"])
    ok(not past, f"{label}: nothing but SCENERY reaches past the lease ground {past[:4]}")
    colored = []
    for n in ("SITE_ground", "SCENERY_terrain", "SITE_road", "SCENERY_road"):
        if n == "SCENERY_road" and n not in glb.by_name and "the road out of the lease" in sc["dropped"]:
            continue
        nd = glb.nodes[glb.idx(n)] if n in glb.by_name else {}
        prims = glb.g["meshes"][nd["mesh"]]["primitives"] if "mesh" in nd else []
        colored.append(bool(prims) and all("COLOR_0" in p["attributes"] for p in prims))
    ok(all(colored), f"{label}: the ground, terrain, and roads carry their vertex colors {colored}")
    flow = [nd for nd in glb.nodes if nd.get("name", "").startswith("PIPE_WELL") and "mesh" in nd]
    ok(all(mats_of(glb, nd) == {"M_HDPE"} for nd in flow), f"{label}: {len(flow)} well flowlines are black HDPE")


def mats_of(glb, nd):
    ms = glb.g.get("materials", [])
    return {ms[p["material"]]["name"] for p in glb.g["meshes"][nd["mesh"]]["primitives"] if "material" in p}


def test_plain_and_repeat(ok, blender, tmp, small_path, small_rep):
    out = os.path.join(tmp, "small_plain")
    p = blender(["--blueprint", small_path, "--out", out, "--no-scenery"])
    if not ok(p.returncode == 0, "--no-scenery: Blender exited 0"):
        return
    rep = json.load(open(os.path.join(out, "build_report.json")))
    g = Glb(os.path.join(out, "field.glb"))
    mats = {m["name"]: m for m in g.g.get("materials", [])}
    ok("SCENERY" not in g.by_name and rep["scenery"] == {"on": False} and not g.g.get("images")
       and "baseColorTexture" not in mats["M_GROUND"].get("pbrMetallicRoughness", {}),
       "--no-scenery: no SCENERY layer and no textures")
    ok(rep["triangles"] == small_rep["scenery"]["field_triangles"],
       f"--no-scenery: the plain field is the field without its scenery ({rep['triangles']} triangles)")
    out2 = os.path.join(tmp, "small_again")
    p = blender(["--blueprint", small_path, "--out", out2])
    if ok(p.returncode == 0, "repeat build: Blender exited 0"):
        again = json.load(open(os.path.join(out2, "build_report.json")))
        a = Glb(os.path.join(out2, "field.glb"))
        first = Glb(os.path.join(tmp, "small", "field.glb"))
        ok(again["triangles"] == small_rep["triangles"] and again["scenery"]["items"] == small_rep["scenery"]["items"]
           and [n.get("name") for n in a.nodes] == [n.get("name") for n in first.nodes]
           and all(a.plan_xy(n) == first.plan_xy(n) for n in a.by_name if n.startswith("SCENERY_")),
           "repeat build: the same blueprint gives the same scenery, node for node")
