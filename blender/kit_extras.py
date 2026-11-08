"""The model kit's loader, and the parts of the no-Blender build that mirror one generator
each: equipment (#47), IoT hardware (#46), valve actuation, each pad's berm (#98), and the
report's draw_calls, look and optimize.

Each part runs only when the generator it mirrors is there: equipment comes from layout.py's
L["equipment"] (siting.py, #47) and sensors from names.sensors() (#46). The kit has the
matching types once blender/export_kit.py runs on a checkout with equipment_parts.py and
iot_parts.py. If the generator has a part the kit lacks, the assembler's self-check names
the missing nodes and asks for a new kit, so a stale kit never ships a field silently.
"""
import hashlib
import json
import os
from collections import Counter

import kit_routes as routes
import names
from glb_write import Mesh, accessor, cross, read_glb, scale, sub, unit

HERE = os.path.dirname(os.path.abspath(__file__))
KIT_DIR = os.path.join(HERE, "kit")


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


def berm(field, n, parent, runs):
    """field_build.berm: PAD_n_berm, the low ridge along the pad's sides and back, with gaps for
    its flowlines and neighbors (siting.berm_runs). Flat-shaded quads facing up, pad-local."""
    m = Mesh()
    for rows in routes.berm(runs):
        for ra, rb in zip(rows, rows[1:]):
            for i in range(len(ra) - 1):
                quad = (ra[i], rb[i], rb[i + 1], ra[i + 1])
                nrm = cross(sub(quad[2], quad[0]), sub(quad[3], quad[1]))
                nrm = unit(nrm if nrm[2] >= 0 else scale(nrm, -1.0))
                m.quad(*(m.vert(p, nrm) for p in quad))
    if m.tris:
        field.mesh_node(f"PAD_{n}_berm", m, "M_BERM", parent)


def summary(j):
    """The report's draw_calls, look and optimize for a kit build, from the glTF JSON about to be
    written. draw_calls counts what build_field.glb_summary counts: one per primitive of each
    mesh node. look and optimize keep the keys look.apply() and optimize.run() report, with what
    the kit build did instead: no restyling (look.py needs Blender, so every part keeps the kit
    palette material it was exported with), and no post-pass (each kit file's meshes are
    written once and every copy shares them; nothing is merged, baked or instanced)."""
    meshes, mats = j.get("meshes", []), [m.get("name", "") for m in j.get("materials", [])]
    draws, mesh_nodes, used = 0, 0, Counter()
    for nd in j.get("nodes", []):
        if "mesh" in nd:
            mesh_nodes += 1
            for p in meshes[nd["mesh"]]["primitives"]:
                draws += 1
                used[mats[p["material"]] if "material" in p else ""] += 1
    look = {"mode": "kit", "applied": False, "textures": False, "materials": {}, "styled": 0, "looks": 0,
            "theirs": 0, "gaps_filled": 0, "ao_uv_meshes": 0, "kit_materials": dict(sorted(used.items())),
            "seconds": {"total": 0.0}}
    optimize = {"pass": "none: kit copies share each kit file's meshes", "baked_modifiers": 0, "merged": 0,
                "meshes_before": mesh_nodes, "meshes_after": len(meshes), "unshared": 0, "instanced": {},
                "instances": 0, "no_shadow": 0, "purged": 0, "seconds": 0.0,
                "glb_meshes": len(meshes), "instanced_nodes": 0}
    return draws, look, optimize


def equipment(field, item):
    """equipment_parts.build: the item's kit model, or a labeled placeholder box."""
    eid, model = item["id"], item.get("model")
    kind = f"equipment_{(model or 'placeholder').lower()}"
    extras = {"asset_id": eid, "kind": "equipment", "equipment_type": item["type"],
              "model": "generic" if model else "placeholder"}
    if item.get("label"):
        extras["label"] = item["label"]
    loc = (*item["pos"], 0.0)
    if field.kit.has(kind):
        root = field.place(kind, eid, loc, item["rot"], extras)
    else:                     # a kit exported before #47: a plain box until export_kit.py runs again
        root = field.place_empty(eid, loc, item["rot"], extras)
        m = Mesh()
        m.box((2.8, 2.8, 2.0), (0, 0, 1.15))
        field.mesh_node(f"{eid}_steel", m, "M_STEEL", root)
    if not model or not field.kit.has(kind):     # the painted type and ID, as equipment_parts.placeholder
        title = (item.get("label") or item["type"].replace("_", " ")).upper()
        text = f"{title}\n{eid.replace('_', ' ')}"
        size = min(0.5, 2.5 / (0.62 * max(len(t) for t in text.split("\n"))))
        field.label(f"{eid}_label", text, size, (0.0, 0.0, 2.24), (0.0, 0.0, 0.0), root)


def valve_modes(data):
    """field_parts.valve_modes, in plain Python: {valve ID: manual?} from the raw blueprint. A
    valve is manual when it says manual: true or remote: false; a listed valve that says neither
    has an actuator, and one the blueprint does not list is manual (field_parts.build_valve)."""
    out = {}
    for v in (data or {}).get("valves") or []:
        if isinstance(v, dict) and isinstance(v.get("id"), str):
            out[v["id"]] = v.get("manual") is True or v.get("remote") is False
    return out


def valve(field, kind, vid, loc, rot):
    """field_parts.build_valve: the handwheel or the actuator type, with extras actuation."""
    manual = field.manual.get(vid, True)
    if not manual and field.kit.has(kind + "_remote"):
        kind += "_remote"
    extras = {"asset_id": vid, "kind": "valve", "actuation": "manual" if manual else "remote"}
    return field.place(kind, vid, loc, rot, extras)


def iot(field):
    """iot_parts.build: a sensor node on each monitored asset, parented to it, and an RTU on each
    pad. Returns the report's "iot" counts, or None before #46.

    The sensor's hardware follows its asset (#100): the kit has one type per device and mount
    (sensor_radar_level_surface on a tank roof, sensor_pressure_bracket on a pump, and so on),
    listed in the manifest's "iot"."""
    sensors = getattr(names, "sensors", None)
    info = field.kit.m.get("iot")
    if sensors is None or "rtu" not in field.t or info is None:
        return None
    kinds = names.asset_kinds(field.bp)
    sizes = {e["id"]: e["size"] for e in field.L.get("equipment", [])}
    found = sensors(field.bp)
    for aid, signals, connectors in found:
        kind = kinds[aid]
        if kind == "equipment":
            w, d = sizes.get(aid, (3.4, 3.4))
            x, y, z = -w / 2 + 0.3, -d / 2 + 0.3, 0.15
        else:
            x, y, z, _ = info["mounts"][kind]
        extras = {"kind": "sensor", "for_asset": aid, "signals": ",".join(signals),
                  "connectors": ",".join(connectors), "device": info["devices"][kind]}
        field.place(info["types"][kind], f"SENSOR_{aid}", (x, y, z), 0.0, extras, parent=field.roots[aid])
    for p in field.L["pads"]:
        pad = f"PAD_{p['n']}"
        field.place("rtu", f"RTU_{pad}", field.t["rtu"]["spot"], 0.0, {"kind": "rtu", "pad": pad},
                    parent=field.roots[pad])
    return {"sensors": len(found), "rtus": len(field.bp["tanks"])}
