"""Generic equipment the generator places by blueprint type (#47), all generated with bmesh.

Separator, heater treater, flare stack, compressor, LACT unit, and meter run. Any other
type is a labeled placeholder box, so a blueprint never fails over a missing model.

Each item is an Empty named with its blueprint ID (kind "equipment", plus equipment_type
and model in its extras), with one child mesh per material. Its local -Y faces the lease
road, its origin sits on the ground at its center, and it fits inside its footprint in
siting.FOOTPRINTS. Neutral gray and steel, like everything else: the console adds color.
"""
import bmesh
from mathutils import Matrix, Vector

import field_parts as fp
from detail_equipment import flange, id_text, label
from field_parts import AXES, PI, Kit, box, cyl, line, ring, tube

_AXIS = {"X": Vector((1, 0, 0)), "Y": Vector((0, 1, 0)), "Z": Vector((0, 0, 1))}


def vessel(bm, r, length, center, axis="X", segs=18):
    """A pressure vessel: a shell with 2:1 elliptical heads."""
    cyl(bm, r, length, center, axis=axis, segs=segs)
    c, d = Vector(center), _AXIS[axis] * (length / 2)
    for s in (-1, 1):
        m = Matrix.Translation(c + d * s) @ AXES[axis] @ Matrix.Diagonal((1.0, 1.0, 0.45, 1.0))
        bmesh.ops.create_uvsphere(bm, u_segments=segs, v_segments=6, radius=r, matrix=m)


def pad(k, w, d):
    """Concrete foundation, sunk so it meets the uneven ground."""
    box(k["M_CONCRETE"], (w, d, 0.4), (0, 0, -0.05))


def sign(site, k, eid, root, x, y, z=1.25):
    """A post with a plate and the painted ID, facing the road. The tag can be dropped for budget."""
    tube(k["M_STEEL"], (x, y + 0.05, 0.0), (x, y + 0.05, z + 0.25), 0.035, 6)
    box(k["M_STEEL"], (0.7, 0.04, 0.3), (x, y, z))
    label(site, f"{eid}_tag", id_text(eid), 0.1, (x, y - 0.025, z), (PI / 2, 0, 0), root)


def separator(site, k, eid, root):
    st, dk, tk, pp = k["M_STEEL"], k["M_STEEL_DARK"], k["M_TANK"], k["M_PIPE"]
    pad(k, 6.4, 2.8)
    box(dk, (5.4, 1.4, 0.18), (0, 0, 0.24))
    for x in (-1.5, 1.5):
        box(dk, (0.3, 1.1, 0.62), (x, 0, 0.64))
    vessel(tk, 0.65, 4.0, (0, 0, 1.45))
    line(pp, [(-1.6, 0, 2.0), (-1.6, 0, 2.75), (-2.9, 0, 2.75), (-2.9, 0, 0.15)], 0.08)    # inlet
    flange(st, (-1.6, 0, 2.15), (0, 0, 1), 0.16)
    tube(pp, (1.6, 0, 2.0), (1.6, 0, 2.5), 0.08, 10)                                         # gas out
    cyl(dk, 0.11, 0.36, (1.6, 0, 2.68), segs=10)
    tube(pp, (0.9, 0, 2.0), (0.9, 0, 2.4), 0.05, 8)                                          # relief valve
    cyl(st, 0.08, 0.26, (0.9, 0, 2.52), segs=8)
    for x in (-0.6, 0.6):                                                                    # oil and water dumps
        tube(pp, (x, 0, 0.9), (x, 0, 0.33), 0.06, 8)
        box(dk, (0.22, 0.22, 0.22), (x, 0, 0.56))
    box(dk, (0.06, 0.06, 0.9), (0.2, -0.68, 1.45))                                           # sight glass
    box(st, (0.25, 0.18, 0.3), (-0.4, -0.72, 1.25))                                          # level controller
    cyl(st, 0.26, 0.14, (2.3, 0, 1.45), axis="X", segs=14)                                   # manway
    sign(site, k, eid, root, -2.2, -1.2)


def heater_treater(site, k, eid, root):
    st, dk, tk, pp = k["M_STEEL"], k["M_STEEL_DARK"], k["M_TANK"], k["M_PIPE"]
    pad(k, 3.6, 3.6)
    cyl(dk, 0.9, 0.42, (0, 0.3, 0.36), segs=20)                                              # skirt
    vessel(tk, 0.85, 5.2, (0, 0.3, 3.15), axis="Z", segs=20)
    cyl(dk, 0.42, 0.22, (0, -0.6, 1.2), axis="Y", segs=16)                                   # firetube flange
    box(dk, (0.62, 0.5, 0.62), (0, -1.1, 1.2))                                               # burner
    line(pp, [(-1.5, -1.6, 0.2), (-1.5, -1.6, 0.7), (-0.31, -1.6, 0.7), (-0.31, -1.1, 0.95)], 0.03)
    tube(st, (0.4, -1.15, 1.5), (0.4, -1.15, 7.2), 0.15, 10)                                 # exhaust stack
    cyl(dk, 0.32, 0.22, (0.4, -1.15, 7.35), segs=10, r2=0.05)
    line(pp, [(-1.75, 0.3, 0.15), (-1.75, 0.3, 3.5), (-0.84, 0.3, 3.5)], 0.08)              # inlet
    line(pp, [(0.84, 0.75, 4.8), (1.6, 0.75, 4.8), (1.6, 0.75, 0.15)], 0.06)               # oil out
    line(pp, [(0.6, -0.1, 1.0), (1.4, -0.4, 1.0), (1.4, -0.4, 0.15)], 0.06)                # water out
    tube(pp, (0, 0.3, 6.1), (0, 0.3, 6.5), 0.07, 8)                                         # gas out
    cyl(st, 0.09, 0.28, (0, 0.3, 6.62), segs=8)
    for side in (-0.2, 0.2):                                                                 # ladder
        tube(st, (-1.08, 0.3 + side, 0.2), (-1.08, 0.3 + side, 6.0), 0.022, 5)
    z = 0.6
    while z < 5.9:
        tube(st, (-1.08, 0.1, z), (-1.08, 0.5, z), 0.016, 4)
        z += 0.45
    ring(st, 0.95, 0.02, (0, 0.3, 5.95), segs=20, rsegs=4)                                   # top handrail
    sign(site, k, eid, root, -1.2, -1.6)


def flare(site, k, eid, root):
    st, dk, tk, pp, cc = k["M_STEEL"], k["M_STEEL_DARK"], k["M_TANK"], k["M_PIPE"], k["M_CONCRETE"]
    box(cc, (1.6, 1.6, 0.55), (0, 0, 0.05))
    cyl(st, 0.21, 14.0, (0, 0, 7.3), segs=12, r2=0.17)                                       # stack
    cyl(dk, 0.25, 0.7, (0, 0, 14.6), segs=12)                                                # tip
    cyl(dk, 0.34, 0.25, (0, 0, 14.88), segs=12, r2=0.3)                                      # windshield
    tube(pp, (0.3, 0, 0.3), (0.3, 0, 14.5), 0.025, 5)                                        # pilot line
    for i in range(3):                                                                       # guy wires
        a = fp.polar(3.55, 90 + 120 * i)
        tube(k["M_WIRE"], (0, 0, 10.0), (a.x, a.y, 0.3), 0.012, 4)
        box(cc, (0.45, 0.45, 0.5), (a.x, a.y, 0.05))
    vessel(tk, 0.42, 1.4, (0, -2.6, 0.82))                                                   # knockout drum
    for x in (-0.45, 0.45):
        box(dk, (0.2, 0.6, 0.45), (x, -2.6, 0.3))
    line(pp, [(0, -2.6, 1.24), (0, -2.6, 1.6), (0, -0.2, 1.6)], 0.08)
    line(pp, [(-1.8, -2.6, 0.15), (-1.8, -2.6, 0.82), (-0.89, -2.6, 0.82)], 0.08)
    sign(site, k, eid, root, 2.0, -3.3, z=1.3)
    box(st, (0.45, 0.3, 0.55), (2.0, -3.0, 0.75))                                            # ignition panel


def compressor(site, k, eid, root):
    st, dk, tk, pp = k["M_STEEL"], k["M_STEEL_DARK"], k["M_TANK"], k["M_PIPE"]
    pad(k, 9.0, 3.6)
    box(dk, (8.2, 2.6, 0.25), (0, 0, 0.27))
    box(st, (2.6, 1.3, 1.3), (-1.7, 0, 1.05))                                                # engine
    for y in (-0.33, 0.33):
        box(dk, (1.1, 0.34, 0.18), (-1.7, y, 1.78))
    for y in (-0.5, 0.5):
        cyl(dk, 0.24, 0.5, (-3.25, y, 1.55), axis="X", segs=12)                              # air cleaners
    cyl(st, 0.3, 1.8, (-1.7, 0, 2.45), axis="X", segs=14)                                    # muffler
    tube(st, (-0.7, 0, 2.45), (-0.7, 0, 3.7), 0.11, 8)
    cyl(dk, 0.55, 0.35, (-0.2, 0, 1.05), axis="X", segs=16)                                  # flywheel housing
    box(dk, (1.4, 1.0, 0.8), (0.75, 0, 0.8))                                                 # compressor frame
    for x in (0.45, 1.05):
        for y in (-1, 1):
            cyl(st, 0.3, 0.8, (x, y * 0.9, 0.85), axis="Y", segs=12)                        # cylinders
            cyl(dk, 0.34, 0.08, (x, y * 1.3, 0.85), axis="Y", segs=12)
    for y in (-0.75, 0.75):
        vessel(tk, 0.28, 1.6, (2.2, y, 1.35), axis="Z", segs=12)                            # scrubbers
        line(pp, [(2.2, y, 2.3), (2.2, y, 2.55), (0.75, y * 0.6, 2.55), (0.75, y * 0.6, 1.2)], 0.06)
    box(st, (1.6, 2.6, 2.6), (3.6, 0, 1.7))                                                  # cooler
    cyl(dk, 0.95, 0.06, (4.42, 0, 1.7), axis="X", segs=20)
    ring(st, 1.0, 0.05, (4.45, 0, 1.7), axis="X", segs=20, rsegs=4)
    line(pp, [(1.05, 0, 1.25), (1.05, 0, 2.2), (2.8, 0, 2.2)], 0.07)
    box(st, (0.8, 0.3, 1.2), (-3.6, -1.55, 1.0))                                             # control panel
    label(site, f"{eid}_tag", id_text(eid), 0.1, (-3.6, -1.71, 1.4), (PI / 2, 0, 0), root)


def lact(site, k, eid, root):
    st, dk, tk, pp = k["M_STEEL"], k["M_STEEL_DARK"], k["M_TANK"], k["M_PIPE"]
    pad(k, 6.6, 3.0)
    box(dk, (6.0, 2.2, 0.2), (0, 0, 0.25))
    line(pp, [(-2.9, 0.2, 0.15), (-2.9, 0.2, 0.95), (2.9, 0.2, 0.95), (2.9, 0.2, 0.15)], 0.1)   # main line
    for x in (-2.6, -1.6, -0.9, 1.0, 1.7, 2.6):
        flange(st, (x, 0.2, 0.95), (1, 0, 0), 0.17)
    cyl(st, 0.22, 0.6, (-2.25, -0.6, 0.62), axis="X", segs=12)                               # charge pump motor
    cyl(dk, 0.26, 0.2, (-1.8, -0.6, 0.62), axis="Y", segs=14)                                # volute
    line(pp, [(-1.8, -0.6, 0.85), (-1.8, -0.6, 0.95), (-1.8, 0.1, 0.95)], 0.07)
    cyl(st, 0.2, 0.6, (-1.25, 0.2, 1.0), segs=12)                                            # strainer
    cyl(dk, 0.24, 0.06, (-1.25, 0.2, 1.33), segs=12)
    cyl(st, 0.12, 0.5, (-0.6, 0.2, 1.3), segs=10)                                            # air eliminator
    box(dk, (0.14, 0.14, 0.2), (-0.15, 0.2, 1.15))                                           # BS&W probe
    box(dk, (0.6, 0.45, 0.5), (0.45, 0.2, 0.95))                                             # meter
    box(st, (0.26, 0.2, 0.26), (0.45, 0.2, 1.33))
    box(dk, (0.3, 0.3, 0.35), (1.35, 0.2, 0.95))                                             # divert valve
    cyl(st, 0.12, 0.3, (1.35, 0.2, 1.3), segs=10)
    vessel(tk, 0.2, 0.6, (1.4, -0.6, 0.85), axis="Z", segs=12)                               # sampler pot
    for x in (2.1, 2.5):                                                                     # prover connections
        tube(pp, (x, 0.2, 1.05), (x, 0.2, 1.5), 0.07, 8)
        cyl(dk, 0.13, 0.05, (x, 0.2, 1.52), segs=10)
    for x in (2.0, 2.8):
        tube(st, (x, -0.95, 0.35), (x, -0.95, 1.6), 0.03, 6)
    box(st, (0.95, 0.25, 0.95), (2.4, -0.95, 1.2))                                           # control panel
    label(site, f"{eid}_tag", id_text(eid), 0.1, (2.4, -1.08, 1.5), (PI / 2, 0, 0), root)


def meter_run(site, k, eid, root):
    st, dk, pp, cc = k["M_STEEL"], k["M_STEEL_DARK"], k["M_PIPE"], k["M_CONCRETE"]
    line(pp, [(-3.5, 0, -0.3), (-3.5, 0, 0.75), (3.5, 0, 0.75), (3.5, 0, -0.3)], 0.14)
    for x in (-2.4, 0.0, 2.4):
        box(cc, (0.45, 0.65, 0.5), (x, 0, 0.05))
        box(st, (0.1, 0.5, 0.42), (x, 0, 0.48))
    for x in (-1.4, -1.3, 0.62, 1.18):
        flange(st, (x, 0, 0.75), (1, 0, 0), 0.24)
    box(dk, (0.45, 0.42, 0.5), (0.9, 0, 0.75))                                               # orifice fitting
    box(dk, (0.3, 0.3, 0.35), (0.9, 0, 1.15))
    ring(st, 0.1, 0.012, (0.9, -0.24, 1.15), axis="Y", segs=10, rsegs=4)
    line(dk, [(0.9, 0.0, 1.32), (0.9, 0.0, 1.5), (1.25, 0.0, 1.5)], 0.012)                  # impulse tubing
    box(dk, (0.14, 0.14, 0.2), (1.3, 0, 1.5))                                                # DP transmitter
    tube(st, (1.9, -0.7, 0.0), (1.9, -0.7, 1.9), 0.04, 6)                                    # flow computer
    box(st, (0.42, 0.22, 0.5), (1.9, -0.82, 1.35))
    box(dk, (0.7, 0.5, 0.03), (1.9, -0.7, 2.0), Matrix.Rotation(-0.5, 4, "X"))              # solar panel
    label(site, f"{eid}_tag", id_text(eid), 0.07, (1.9, -0.935, 1.45), (PI / 2, 0, 0), root)


def placeholder(site, k, eid, root, title):
    """A plain box with what it is and its ID painted on the top and the front."""
    box(k["M_CONCRETE"], (3.2, 3.2, 0.4), (0, 0, -0.05))
    box(k["M_STEEL"], (2.8, 2.8, 2.0), (0, 0, 1.15))
    box(k["M_STEEL_DARK"], (2.9, 2.9, 0.08), (0, 0, 2.19))
    text = f"{title}\n{eid.replace('_', ' ')}"
    longest = max(len(t) for t in text.split("\n"))
    size = min(0.5, 2.5 / (0.62 * longest))
    label(site, f"{eid}_label", text, size, (0, 0, 2.24), (0, 0, 0), root, small=False)
    label(site, f"{eid}_label_front", text, size * 0.75, (0, -1.41, 1.15), (PI / 2, 0, 0), root)


MODELS = {"SEPARATOR": separator, "HEATER_TREATER": heater_treater, "FLARE": flare,
          "COMPRESSOR": compressor, "LACT": lact, "METER_RUN": meter_run}


def build(site, item):
    """Build one placed equipment item from siting.place_equipment(). Returns its root."""
    eid = item["id"]
    root = site.empty(eid, (*item["pos"], 0.0), item["rot"], kind="equipment", size=2)
    root["equipment_type"] = item["type"]
    root["model"] = "generic" if item["model"] else "placeholder"
    if item["label"]:
        root["label"] = item["label"]
    k = Kit(site, eid, root)
    if item["model"]:
        MODELS[item["model"]](site, k, eid, root)
    else:
        title = (item["label"] or item["type"].replace("_", " ")).upper()
        placeholder(site, k, eid, root, title)
    k.done()
    return root
