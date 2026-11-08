"""Equipment (#47) and IoT hardware (#46) for the no-Blender build, placed from the model kit.

Each part runs only when the generator it mirrors is there: equipment comes from layout.py's
L["equipment"] (siting.py, #47) and sensors from names.sensors() (#46). The kit has the
matching types once blender/export_kit.py runs on a checkout with equipment_parts.py and
iot_parts.py. If the generator has a part the kit lacks, the assembler's self-check names
the missing nodes and asks for a new kit, so a stale kit never ships a field silently.
"""
import names
from glb_write import Mesh


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


def iot(field):
    """iot_parts.build: a sensor node on each monitored asset, parented to it, and an RTU on each
    pad. Returns the report's "iot" counts, or None before #46."""
    sensors = getattr(names, "sensors", None)
    if sensors is None or "rtu" not in field.t:
        return None
    mounts = field.t["sensor_post"]["mounts"]
    kinds = names.asset_kinds(field.bp)
    sizes = {e["id"]: e["size"] for e in field.L.get("equipment", [])}
    found = sensors(field.bp)
    for aid, signals, connectors in found:
        if kinds[aid] == "equipment":
            w, d = sizes.get(aid, (3.4, 3.4))
            x, y, z, mount = -w / 2 + 0.3, -d / 2 + 0.3, 0.15, "post"
        else:
            x, y, z, mount = mounts[kinds[aid]]
        extras = {"kind": "sensor", "for_asset": aid, "signals": ",".join(signals),
                  "connectors": ",".join(connectors)}
        field.place(f"sensor_{mount}", f"SENSOR_{aid}", (x, y, z), 0.0, extras, parent=field.roots[aid])
    for p in field.L["pads"]:
        pad = f"PAD_{p['n']}"
        field.place("rtu", f"RTU_{pad}", field.t["rtu"]["spot"], 0.0, {"kind": "rtu", "pad": pad},
                    parent=field.roots[pad])
    return {"sensors": len(found), "rtus": len(field.bp["tanks"])}
