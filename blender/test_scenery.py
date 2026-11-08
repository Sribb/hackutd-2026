"""Tests for the desert scenery pass (#88). test_generator.py runs these with its ok() and
its Blender runner.

- The plan (no Blender): deterministic for a seed, inside its triangle budget, inside the
  fence, and off everything in siting.occupied(), with the fence dropped when it can't fit.
- Every build: one SCENERY root, every node under it named SCENERY_*, none with an asset
  ID, kit copies off pads, roads, assets, and truck waypoints, textured ground and road.
- --no-scenery builds the plain field, and two builds of one blueprint are identical.
"""
import json
import os

import blueprint
import layout
import siting
from glb_read import Glb, touch


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
