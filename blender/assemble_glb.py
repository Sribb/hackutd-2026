"""Build field.glb from a blueprint without Blender: the model kit, placed where layout.py says.

Run from the repo root with plain Python 3 (standard library only):
  python3 blender/assemble_glb.py [--blueprint PATH.json] [--out DIR] [--detail full|lite|bare]

The options, outputs and exit codes match build_field.py: it writes field.glb and
build_report.json (the same keys, plus "model": "kit"). A blueprint with errors exits with
code 1 and writes only the report, never a glb. A crash or a failed self-check exits with
code 2. With no --blueprint it builds scenario/field.json, and with no --out it writes to
blender/out/<blueprint name>-kit/. It never writes into blender/ itself, so the committed,
Blender-built field.glb stays as it is. --render-dir is accepted and skipped (it needs Blender).

blueprint.py checks the file and layout.py places every asset, as in the Blender build, so
positions match. Each asset is a copy of its type from blender/kit/ (export_kit.py made it in
Blender), renamed to its contract name, with its meshes shared between copies. This file
adds what depends on the field: the ground, the lease road and driveways, the tank numbers,
each tank's outlet and crossover nozzles, the pipe runs and supports (kit_routes.py, the same
routes as piping.py), waypoints, and the overview camera. Small tags and signs, the power
line, and the ground relief are left out.
"""
import hashlib
import json
import math
import os
import sys
import time
import traceback
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import blueprint  # noqa: E402
import kit_extras  # noqa: E402
import kit_routes as routes  # noqa: E402
import layout as lay  # noqa: E402
import names  # noqa: E402
from glb_write import (Mesh, Writer, accessor, add, euler_xyz, gltf_quat, look_at, quat_axis,  # noqa: E402
                       read_glb, to_gltf)
from kit_routes import at, polar  # noqa: E402

ROOT = os.path.dirname(HERE)
KIT_DIR = os.path.join(HERE, "kit")
DEFAULT_BLUEPRINT = os.path.join(ROOT, "scenario", "field.json")
GROUND_Z = -0.02          # flat ground just under every slab, pad, and gravel bed
KIT_OMITS = ["pump, valve, well, pad and yard tags and signs", "the power line", "ground relief"]


class UsageError(Exception):
    pass


def parse_args(argv):
    opts = {"blueprint": None, "out": None, "detail": None, "render_dir": None, "render_assets": None}
    flags = {"--blueprint": "blueprint", "--out": "out", "--detail": "detail",
             "--render-dir": "render_dir", "--render-assets": "render_assets"}
    i = 0
    while i < len(argv):
        key = flags.get(argv[i])
        if key is None or i + 1 >= len(argv):
            raise UsageError(f"unknown or incomplete option {argv[i]!r}; options: {', '.join(flags)}")
        opts[key] = argv[i + 1]
        i += 2
    if opts["detail"] not in (None, "full", "lite", "bare"):
        raise UsageError("--detail must be one of full, lite, bare")
    return opts


def rel(path):
    path = os.path.abspath(path)
    return os.path.relpath(path, ROOT) if path.startswith(ROOT + os.sep) else path


def rot_z(angle):
    return gltf_quat(quat_axis((0, 0, 1), angle))


class Kit:
    """blender/kit/: the manifest, the type glbs (read when first used), and the glyphs."""

    def __init__(self, folder=KIT_DIR):
        with open(os.path.join(folder, "manifest.json")) as f:
            self.m = json.load(f)
        self.folder, self.files, self.glyph_cache = folder, {}, {}
        self.glyphs = read_glb(os.path.join(folder, self.m["glyphs"]["file"]))
        self.glyph_index = {nd["name"]: nd["mesh"] for nd in self.glyphs[0]["nodes"] if "mesh" in nd}
        self.advance = self.m["glyphs"]["advance"]

    def has(self, kind):
        return kind in self.m["types"]

    def file(self, kind, level):
        name = self.m["types"][kind]["levels"][level]
        if name not in self.files:
            self.files[name] = read_glb(os.path.join(self.folder, name))
        return name, self.files[name]

    def glyph(self, ch):
        """(positions, normals, indices) of one character at size 1, or None."""
        if ch not in self.glyph_cache:
            g, binary = self.glyphs
            mi = self.glyph_index.get(f"GLYPH_{ord(ch)}")
            p = g["meshes"][mi]["primitives"][0] if mi is not None else None
            self.glyph_cache[ch] = p and (accessor(g, binary, p["attributes"]["POSITION"]),
                                          accessor(g, binary, p["attributes"]["NORMAL"]),
                                          accessor(g, binary, p["indices"]))
        return self.glyph_cache[ch]

    def stale(self):
        """Builder sources that changed since the kit was exported."""
        out = []
        for name, digest in self.m.get("sources", {}).items():
            path = os.path.join(HERE, name)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    if hashlib.sha256(f.read()).hexdigest()[:16] != digest:
                        out.append(name)
        return out


class Field:
    """Plans one field as a list of steps, then writes it at a detail level.

    Only the kit copies change with the detail level, so the plan is made once, and the
    triangles at every level are known before anything is written.
    """

    def __init__(self, kit, bp, L):
        self.kit, self.bp, self.L, self.t = kit, bp, L, kit.m["types"]
        self.ops, self.cameras, self.roots, self.iot = [], [], {}, None
        self.site = {"M_ROAD": Mesh(), "M_CONCRETE": Mesh(), "M_STEEL": Mesh()}

    # the plan: each call returns a step number that later steps can name as their parent

    def _op(self, *op):
        self.ops.append(op)
        return len(self.ops) - 1

    def place(self, kind, prefix, loc, rot, extras=None, parent=None):
        """A copy of a kit type, renamed prefix + suffix. loc and rot (about z) are Blender axes,
        in the parent's frame when there is one."""
        op = self._op("place", kind, prefix, tuple(loc), rot, extras, parent)
        self.roots[prefix + self.t[kind].get("root", "")] = op
        return op

    def place_empty(self, name, loc, rot, extras=None):
        self.roots[name] = self._op("node", name, loc, rot_z(rot), extras, None)
        return self.roots[name]

    def mesh_node(self, name, mesh, material, parent=None, loc=None, rot=None, extras=None, levels=None):
        """A generated mesh. rot is a glTF rotation; levels limits it to some detail levels."""
        return self._op("mesh", name, mesh, material, parent, loc, rot, extras, levels)

    def empty(self, name, loc, rot=None, extras=None, camera=None):
        return self._op("node", name, loc, rot, extras, camera)

    def triangles(self, level):
        tris = 0
        for op in self.ops:
            if op[0] == "place":
                tris += self.t[op[1]]["triangles"][level]
            elif op[0] == "mesh" and (op[8] is None or level in op[8]):
                tris += op[2].tris
        return tris

    def write(self, level):
        w = Writer("Freeboard blender/assemble_glb.py (model kit)")
        w.j["asset"]["extras"] = {"model": "kit", "detail": level}
        w.import_file(*read_glb(os.path.join(self.kit.folder, self.kit.m["palette"]["file"])), meshes=False)
        w.j["cameras"] = list(self.cameras)
        imported, index = {}, {}
        for i, op in enumerate(self.ops):
            parent = index.get(op[-1] if op[0] == "place" else op[4] if op[0] == "mesh" else None)
            if op[0] == "place":
                index[i] = self._copy(w, imported, level, *op[1:6], parent)
            elif op[0] == "mesh":
                _, name, mesh, mat, _, loc, rot, extras, levels = op
                if levels is None or level in levels:
                    index[i] = w.node(name, parent, to_gltf(loc) if loc else None, rot, mesh=w.mesh(name, mesh, mat),
                                      extras=extras)
            else:
                _, name, loc, rot, extras, camera = op
                index[i] = w.node(name, None, to_gltf(loc), rot, extras=extras, camera=camera)
        return w

    def _copy(self, w, imported, level, kind, prefix, loc, rot, extras, parent):
        """Copy a kit type's node tree in, sharing its meshes with every other copy."""
        fname, (g, binary) = self.kit.file(kind, level)
        if fname not in imported:
            imported[fname] = w.import_file(g, binary)
        meshes, token = imported[fname], self.t[kind]["token"]
        root = g["scenes"][g.get("scene", 0)]["nodes"]
        if len(root) != 1:
            raise RuntimeError(f"kit {fname} must have one root node")

        def copy(i, parent_i, top):
            nd = g["nodes"][i]
            if not nd["name"].startswith(token):
                raise RuntimeError(f"kit {fname}: node {nd['name']} does not start with {token}")
            new = prefix + nd["name"][len(token):]
            if top:
                j = w.node(new, parent_i, to_gltf(loc), rot_z(rot), extras=extras)
            else:
                j = w.node(new, parent_i, nd.get("translation"), nd.get("rotation"), nd.get("scale"),
                           meshes.get(nd["mesh"]) if "mesh" in nd else None)
            for c in nd.get("children", []):
                copy(c, j, False)
            return j

        return copy(root[0], parent, True)

    # generated pieces

    def pipes(self, pipes):
        """One mesh per (name, points, radius, sides), each with its pipe_id."""
        for name, points, radius, segs in pipes:
            m = Mesh()
            m.pipe(points, radius, segs)
            self.mesh_node(name, m, "M_PIPE", extras={"pipe_id": name})

    def supports(self, concrete=(), steel=(), posts=()):
        for size, center in concrete:
            self.site["M_CONCRETE"].box(size, center)
        for size, center in steel:
            self.site["M_STEEL"].box(size, center)
        for a, b, r in posts:
            self.site["M_STEEL"].tube(a, b, r, 8, caps=True)

    def label(self, name, text, size, loc, rot, parent, material="M_STEEL_DARK", levels=None):
        """detail_equipment.label() from the kit's glyphs. rot is a Blender XYZ Euler."""
        m = Mesh()
        m.text(text, size, self.kit.advance, self.kit.glyph, self.kit.m["glyphs"]["line"])
        return self.mesh_node(name, m, material, parent, loc, gltf_quat(euler_xyz(*rot)), levels=levels)

    # the field, in field_build.build()'s order

    def plan(self):
        L = self.L
        self.ground()
        ties = []
        for pad in L["pads"]:
            ties += self.pad(pad, [p for p in L["pumps"] if p["tank"] == pad["tank"]],
                             [w for w in L["wells"] if w["tank"] == pad["tank"]])
        va = self.t["valve_crossover"]["anchors"]
        for x in L["crossovers"]:
            loc, rot = tuple(x["valve"]), x["valve_rot"]
            self.place("valve_crossover", x["id"], loc, rot, {"asset_id": x["id"], "kind": "valve"})
            pipes, concrete, steel, posts = routes.crossover_lines(
                x, names.valve_base(x["id"]), [at(loc, rot, va["north"]), at(loc, rot, va["south"])])
            self.pipes(pipes)
            self.supports(concrete, steel, posts)
        inlets = []
        for s in L["swds"]:
            kind = "disposal_well" if s["fx"] > 0 else "disposal_well_mirror"
            f, a = s["frame"], self.t[kind]["anchors"]
            loc = (f.x, f.y, 0.0)
            self.place(kind, s["id"], loc, f.rot, {"asset_id": s["id"], "kind": "disposal_well"})
            self.place("tower", s["id"], at(loc, f.rot, a["tower"]["loc"]), f.rot + math.radians(a["tower"]["aim_deg"]))
            inlets.append((s["id"], at(loc, f.rot, a["inlet"])))
        pipes, concrete = routes.trunk(ties, inlets, L["trunk_dips"])
        self.pipes(pipes)
        self.supports(concrete)
        yard = L["yard"]
        if yard["id"]:
            f = yard["frame"]
            root = self.place("yard", yard["id"], (f.x, f.y, 0.0), f.rot, {"asset_id": yard["id"], "kind": "yard"})
            curb, m = self.t["yard"]["curb"], Mesh()
            for x, y in lay.YARD_SLOTS[:sum(1 for t in L["trucks"] if not t["given"])]:
                m.box(curb["size"], add((x, y, 0.0), curb["offset"]), math.radians(curb["rot_deg"]))
            if m.tris:            # yard_extras puts the curb stops in the yard's detail concrete
                self.mesh_node(f"{yard['id']}_detail_concrete", m, "M_CONCRETE", root)
        for t in L["trucks"]:
            self.place("truck", t["id"], (*t["pos"], 0.12), t["rot"], {"asset_id": t["id"], "kind": "truck"})
        for item in L.get("equipment", []):
            kit_extras.equipment(self, item)
        self.iot = kit_extras.iot(self)
        for name, loc, aim in L["towers"]:
            self.place("tower", name, loc, math.radians(aim))
        for name, loc in L["waypoints"]:
            self.empty(name, loc, extras={"kind": "waypoint"})
        self.camera()
        for mat, name in (("M_ROAD", "SITE_road"), ("M_CONCRETE", "SITE_supports_concrete"),
                          ("M_STEEL", "SITE_supports_steel")):
            if self.site[mat].tris:
                self.mesh_node(name, self.site[mat], mat)
        return self

    def ground(self):
        x0, x1, y0, y1 = self.L["ground"]
        m, up = Mesh(), (0.0, 0.0, 1.0)
        m.quad(*(m.vert((x, y, GROUND_Z), up) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))))
        self.mesh_node("SITE_ground", m, "M_GROUND")
        r0, r1 = self.L["road"]
        self.site["M_ROAD"].box((r1 - r0, 2 * lay.ROAD_HALF, 0.08), ((r0 + r1) / 2, 0.0, 0.04))
        for dx0, dx1, dy0, dy1 in self.L["driveways"]:
            self.site["M_ROAD"].box((dx1 - dx0, dy1 - dy0, 0.07), ((dx0 + dx1) / 2, (dy0 + dy1) / 2, 0.035))

    def pad(self, pad, pumps, wells):
        """field_build.build_pad: pad, tank, pumps and valves, load-out, inlet header, wells."""
        f, tid, n = pad["frame"], pad["tank"], pad["n"]
        self.place("pad", f"PAD_{n}", (f.x, f.y, 0.0), f.rot)
        consts, anchors = self.t["tank"]["tank"], self.t["tank"]["anchors"]
        tloc = f.world3(0, lay.TANK_Y, lay.PAD_TOP)
        troot = self.place("tank", tid, tloc, f.rot, {"asset_id": tid, "kind": "tank"})
        text = n.replace("_", " ")
        face = (math.pi / 2, 0.0, math.radians(consts["label_angle"]) + math.pi / 2)
        self.label(f"{tid}_label", text, min(1.7, 2.4 / (0.6 * max(1, len(text)))),
                   polar(consts["R"] + 0.05, consts["label_angle"], 3.0), face, troot)
        if pad["capacity"]:       # a small label, so full detail only
            self.label(f"{tid}_label_cap", f"{pad['capacity']:g} BBL", 0.32,
                       polar(consts["R"] + 0.1, consts["label_angle"], 1.95), face, troot, levels={"full"})
        angles = [(lay.OUTLET_ANGLES[p["slot"]], consts["outlet_z"]) for p in pumps]
        angles += [(a, consts["xo_z"]) for a in self.L["xo_nozzles"][tid]]
        for tubes, suffix, mat in zip(routes.nozzles(consts, angles), ("_nozzles", "_nozzle_flanges"),
                                      ("M_PIPE", "M_STEEL")):
            m = Mesh()
            for a, b, r in tubes:
                m.tube(a, b, r, 12, caps=True)
            if m.tris:
                self.mesh_node(tid + suffix, m, mat, troot)

        ties = []
        for p in pumps:
            outlet = at(tloc, f.rot, anchors["outlets"][str(float(lay.OUTLET_ANGLES[p["slot"]]))])
            pipes, boxes, tie, vloc = routes.pump_lines(tid, p, outlet, self.t["pump"]["anchors"],
                                                        self.t["valve_discharge"]["anchors"])
            self.place("pump", p["id"], (*p["pos"], lay.PAD_TOP), p["rot"], {"asset_id": p["id"], "kind": "pump"})
            if p["valve"]:
                self.place("valve_discharge", p["valve"], vloc, p["rot"], {"asset_id": p["valve"], "kind": "valve"})
            self.pipes(pipes)
            self.supports(boxes)
            ties.append(tie)
        self.place("loadout", f"LOADOUT_{n}", f.world3(-7.6, 35.0, lay.PAD_TOP), f.rot, {"kind": "loadout"})
        self.pipes(routes.pad_lines(f, tid, n, at(tloc, f.rot, anchors["loadout"]), at(tloc, f.rot, anchors["inlet"])))
        outs, out_local = [], self.t["well"]["anchors"]["outlet"]
        for w in wells:
            loc = (*w["pos"], 0.0)
            self.place("well", w["id"], loc, w["rot"], {"asset_id": w["id"], "kind": "well"})
            outs.append((w["id"], at(loc, w["rot"], out_local)))
        self.pipes(routes.flowlines(f, tid, outs))
        return ties

    def camera(self):
        """build_field.stage(): the overview camera framing the whole field."""
        L = self.L
        x0, x1 = L["road"]
        gx0, gx1, gy0, gy1 = L["ground"]
        tank_ys = [p["frame"].world(0, lay.TANK_Y)[1] for p in L["pads"]]
        ty = sum(tank_ys) / len(tank_ys) - (14.0 if L["rows"] == 1 else 0.0)
        s = max(1.0, (x1 - x0) / 450.0, (gy1 - gy0) / 155.0)
        cx = (x0 + x1) / 2
        eye, target = (cx, ty - 267 * s, 170 * s), (cx, ty, 0.0)
        # a 24 mm lens on Blender's 36 mm sensor at 16:9, as Blender's exporter writes it
        self.cameras.append({"name": "CAM_OVERVIEW", "type": "perspective", "perspective": {
            "aspectRatio": 16 / 9, "yfov": 2 * math.atan(36 / (2 * 24) / (16 / 9)), "zfar": 3000 * s, "znear": 0.1}})
        self.empty("CAM_OVERVIEW", eye, look_at(to_gltf(eye), to_gltf(target)), camera=len(self.cameras) - 1)


# ---------- the run ----------

def paths(opts):
    src = os.path.abspath(opts["blueprint"]) if opts["blueprint"] else DEFAULT_BLUEPRINT
    stem = os.path.splitext(os.path.basename(src))[0]
    out_dir = os.path.abspath(opts["out"]) if opts["out"] else os.path.join(HERE, "out", f"{stem}-kit")
    os.makedirs(out_dir, exist_ok=True)
    return src, out_dir


def run(opts, src, out_dir, report, kit=None):
    """The build itself. Returns the exit code."""
    report["blueprint"] = rel(src)
    glb = os.path.join(out_dir, "field.glb")
    if os.path.exists(glb):
        os.remove(glb)            # a failed run must not leave an older glb behind
    data, errors = blueprint.load(src)
    bp, more, warnings = blueprint.check(data) if data is not None else (None, [], [])
    report["errors"] += errors + more
    report["warnings"] += warnings
    if report["errors"]:
        return 1
    L = lay.plan(bp)
    report["warnings"] += L["warnings"]
    report["errors"] += L["errors"]
    if report["errors"]:
        return 1
    report["name"] = bp["name"]
    if bp["site"]:
        report["site"] = bp["site"]
    if bp["connectors"]:
        report["connectors"] = [c["id"] for c in bp["connectors"]]
    report["counts"] = blueprint.counts(bp)

    kit = kit or Kit()
    policy = kit.m["detail"]
    order = list(policy["levels"])
    field = Field(kit, bp, L).plan()
    # build_field.py's rule: full detail up to 12 tanks, then the first level inside the budget.
    # The plan knows each level's triangles exactly, so the field is written once.
    floor = 0 if len(bp["tanks"]) <= policy["full_detail_max_tanks"] else 1
    level = opts["detail"] or next((lv for lv in order[floor:] if field.triangles(lv) <= policy["budget_triangles"]),
                                   order[-1])
    w = field.write(level)
    tris = w.triangles()

    nodes = w.j["nodes"]
    node_names = [nd["name"] for nd in nodes]
    clash = sorted(n for n, c in Counter(node_names).items() if c > 1 or "." in n)
    if clash:
        report["errors"].append("names clash with each other: " + ", ".join(clash[:12]))
    missing = sorted(set(names.expected(bp)) - set(node_names))
    if missing:
        stale = kit.stale()
        report["errors"].append("the build is missing contract names: " + ", ".join(missing[:20])
                                + (f" (the model kit is older than {', '.join(stale)}; rebuild it with "
                                   "blender/export_kit.py)" if stale else ""))
    if report["errors"]:
        return 2
    w.write(glb + ".tmp")
    os.replace(glb + ".tmp", glb)
    mb = os.path.getsize(glb) / 1e6
    kinds = Counter(nd["extras"]["kind"] for nd in nodes if "asset_id" in nd.get("extras", {}))
    report.update({
        "ok": True,
        "model": "kit",
        "outputs": {"glb": rel(glb), "blend": None, "report": rel(os.path.join(out_dir, "build_report.json"))},
        "kinds": dict(sorted(kinds.items())),
        "placed": {k: sorted(v) for k, v in L["placed"].items()},
        "layout": {"rows": L["rows"], "pad_pitch_m": lay.PAD_PITCH, "road_m": round(L["road"][1] - L["road"][0], 1),
                   "road_x": [round(v, 1) for v in L["road"]], "ground": [round(v, 1) for v in L["ground"]],
                   "crossovers": {x["id"]: x["style"] for x in L["crossovers"]}},
        "detail": {"level": level, "dropped": policy["dropped"][level] + KIT_OMITS,
                   "passes": [{"detail": level, "estimated": field.triangles(level), "triangles": tris}]},
        **({"iot": field.iot} if field.iot is not None else {}),
        "triangles": tris, "glb_mb": round(mb, 2), "nodes": len(nodes), "objects": len(nodes),
        "budget": {"triangles": policy["budget_triangles"], "mb": policy["budget_mb"]},
    })
    report["warnings"].append("built from the model kit without Blender; left out: " + ", ".join(KIT_OMITS))
    if level != "full":
        why = (f"more than {policy['full_detail_max_tanks']} tanks" if floor
               else "full detail would pass the triangle budget") if not opts["detail"] else "--detail was set"
        report["warnings"].append(f"built at {level} detail ({why}); dropped: {', '.join(policy['dropped'][level])}")
    if tris > policy["budget_triangles"] or mb > policy["budget_mb"]:
        report["warnings"].append(f"over budget: {tris} triangles and {mb:.1f} MB "
                                  f"(budget {policy['budget_triangles']} and {policy['budget_mb']:.0f} MB)")
    stale = kit.stale()
    if stale:
        report["warnings"].append("the model kit is older than " + ", ".join(stale)
                                  + "; rebuild it with blender/export_kit.py")
    if opts["render_dir"]:
        report["warnings"].append("--render-dir needs Blender, so no renders were made")
    return 0


def write_report(report, out_dir, t0):
    report["seconds"] = round(time.time() - t0, 3)
    if out_dir:
        with open(os.path.join(out_dir, "build_report.json"), "w") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
    status = "OK" if report["ok"] else "FAILED"
    print(f"build_report: {status} in {report['seconds']} s (model kit)"
          + "".join(f"\n  error: {e}" for e in report["errors"]))


def main(argv=None):
    t0 = time.time()
    report = {"ok": False, "errors": [], "warnings": [], "model": "kit"}
    code, out_dir = 2, None
    try:
        opts = parse_args(sys.argv[1:] if argv is None else argv)
        src, out_dir = paths(opts)
        code = run(opts, src, out_dir, report)
    except UsageError as e:
        report["errors"].append(str(e))
    except Exception:  # noqa: BLE001
        report["errors"].append("the build crashed:\n" + traceback.format_exc())
    if code:
        report["ok"] = False
        if not report["errors"]:
            report["errors"].append("the build failed")
    write_report(report, out_dir, t0)
    return code


if __name__ == "__main__":
    sys.exit(main())
