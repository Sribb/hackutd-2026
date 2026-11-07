"""Build the Freeboard oil field, save blender/field.blend, and export blender/field.glb.

Run from the repo root:
  /Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
      --python blender/build_field.py -- [--render-dir DIR]

Every name in docs/CONTRACT.md exists in the output. The layout (west to east):
disposal well SWD_1, tank pads 1 to 6 north of the lease road, disposal well SWD_2.
Wells sit north of their pad, the truck yard sits south of the road, and the trunk
line runs along the road's north shoulder to both disposal wells.
"""
import importlib
import math
import os
import sys

import bpy
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import field_parts as fp  # noqa: E402
import detail_equipment as de  # noqa: E402
import detail_site as ds  # noqa: E402

for _mod in (fp, de, ds):
    importlib.reload(_mod)

PAD_X = [-137.5, -82.5, -27.5, 27.5, 82.5, 137.5]
PAD_TOP = 0.15
TANK_Y = 46.0
PUMP_DX, PUMP_Y = 4.83, 33.0
TRUNK_Y, TRUNK_Z = 8.0, 0.6
ROAD_HALF = 3.5
SWD_X, SWD_Y = 205.0, 16.0
HEADER_Y = 56.5
WELLS = [(-16, 72, -8), (-6, 82, 3), (6, 82, -3), (16, 72, 8)]   # dx, y, rotation (deg)
YARD = (0.0, -31.0)
DRIVE_W = 6.0


def ground_height(x, y):
    return (-0.2 + 0.07 * math.sin(x / 23) + 0.05 * math.sin(y / 17 + x / 41)
            + 0.04 * math.sin((x + y) / 9.3))


def ground(site):
    import bmesh
    bm = bmesh.new()
    x0, x1, y0, y1, nx, ny = -235, 235, -55, 100, 94, 31
    grid = [[bm.verts.new((x0 + (x1 - x0) * i / nx, y0 + (y1 - y0) * j / ny,
                           ground_height(x0 + (x1 - x0) * i / nx, y0 + (y1 - y0) * j / ny)))
             for i in range(nx + 1)] for j in range(ny + 1)]
    for j in range(ny):
        for i in range(nx):
            bm.faces.new((grid[j][i], grid[j][i + 1], grid[j + 1][i + 1], grid[j + 1][i]))
    site.mesh("SITE_ground", bm, "M_GROUND", recalc=False)


def slab(kit, mat, x0, x1, y0, y1, height):
    fp.box(kit[mat], (x1 - x0, y1 - y0, height), ((x0 + x1) / 2, (y0 + y1) / 2, height / 2))


def drive_ranges():
    return [(cx - 7 - DRIVE_W / 2, cx - 7 + DRIVE_W / 2) for cx in PAD_X]


def trunk_run(x0, x1):
    """Trunk points from x0 to x1 along the shoulder, dipping under each driveway."""
    lo, hi = sorted((x0, x1))
    pts = [Vector((x0, TRUNK_Y, TRUNK_Z))]
    dips = [(a, b) for a, b in drive_ranges() if lo < a and b < hi]
    if x1 < x0:
        dips = [(b, a) for a, b in reversed(dips)]
    step = 1 if x1 >= x0 else -1
    for a, b in dips:
        pts += [Vector((a - step * 1.2, TRUNK_Y, TRUNK_Z)), Vector((a - step * 0.4, TRUNK_Y, -0.7)),
                Vector((b + step * 0.4, TRUNK_Y, -0.7)), Vector((b + step * 1.2, TRUNK_Y, TRUNK_Z))]
    pts.append(Vector((x1, TRUNK_Y, TRUNK_Z)))
    return pts


def build(collection):
    site = fp.Site(collection)
    ground(site)
    civil = fp.Kit(site, "SITE", None)
    supports = fp.Kit(site, "SITE_supports", None)
    slab(civil, "M_ROAD", -225, 225, -ROAD_HALF, ROAD_HALF, 0.08)
    for a, b in drive_ranges():
        slab(civil, "M_ROAD", a, b, ROAD_HALF, 29.0, 0.07)
    slab(civil, "M_ROAD", -4, 4, -19.0, -ROAD_HALF, 0.07)
    for sx in (-1, 1):
        slab(civil, "M_ROAD", sx * SWD_X - 4, sx * SWD_X + 4, ROAD_HALF, 5.2, 0.07)

    ties = []
    for n, cx in enumerate(PAD_X, start=1):
        pad = site.empty(f"PAD_{n}", (cx, 0, 0), size=1)
        padkit = fp.Kit(site, f"PAD_{n}", pad)
        fp.box(padkit["M_GRAVEL"], (24, 29, PAD_TOP), (0, 43.5, PAD_TOP / 2))
        for dy in (-6.2, 6.2):
            fp.box(padkit["M_STEEL"], (13, 0.3, 0.8), (0, TANK_Y + dy, PAD_TOP + 0.4))
        for dx in (-6.2, 6.2):
            fp.box(padkit["M_STEEL"], (0.3, 12.1, 0.8), (dx, TANK_Y, PAD_TOP + 0.4))
        padkit.done()
        ds.pad_extras(site, n, pad, TANK_Y, PAD_TOP)
        ds.light_tower(site, f"PAD_{n}", (cx + 10.5, 56.8, PAD_TOP), 226)

        xo = {3: 0.0, 4: 180.0}.get(n)
        tank_root, tank = fp.build_tank(site, n, (cx, TANK_Y, PAD_TOP), xo_angle=xo)
        lo = de.tank_extras(site, n, tank_root)
        px = cx + PUMP_DX
        pump_root, pump = fp.build_pump(site, n, (px, PUMP_Y, PAD_TOP))
        de.pump_extras(site, n, pump_root)
        vx, vy, vz = px - 0.8, PUMP_Y - 0.9, PAD_TOP + 2.05
        valve_root, valve = fp.build_valve(site, f"VALVE_D_{n}", (vx, vy, vz))
        de.valve_extras(site, f"VALVE_D_{n}", valve_root, f"D-{n}")
        stand = (cx - 7.6, 35.0)
        de.build_loadout(site, n, (*stand, PAD_TOP))
        site.pipe(f"PIPE_TANK_{n}_LOADOUT_{n}", [lo, (lo.x, lo.y, 1.75), (lo.x, stand[1], 1.75),
                                                 (stand[0], stand[1], 1.75)], 0.1)
        o, s = tank["outlet"], pump["suction"]
        site.pipe(f"PIPE_TANK_{n}_PUMP_{n}", [o, (o.x, o.y, 1.6), (o.x, s.y, 1.6), (o.x, s.y, s.z), s], 0.11)
        d = pump["discharge"]
        site.pipe(f"PIPE_PUMP_{n}_VALVE_D_{n}", [d, (d.x, d.y, vz), valve["north"]], 0.09)
        sv = valve["south"]
        site.pipe(f"PIPE_VALVE_D_{n}_TRUNK", [sv, (vx, vy - 1.0, vz), (vx, vy - 1.0, TRUNK_Z),
                                              (vx, TRUNK_Y, TRUNK_Z)], 0.09)
        ties.append(vx)
        for y in (26.0, 20.0, 14.0):
            fp.box(supports["M_CONCRETE"], (0.8, 0.5, TRUNK_Z - 0.08), (vx, y, (TRUNK_Z - 0.08) / 2))

        inlet = tank["inlet"]
        site.pipe(f"PIPE_INLET_{n}", [(cx - 4, HEADER_Y, 0.5), (cx + 4, HEADER_Y, 0.5)], 0.08)
        site.pipe(f"PIPE_INLET_{n}_RISER", [(cx, HEADER_Y, 0.5), (cx, HEADER_Y, inlet.z),
                                            (inlet.x, HEADER_Y, inlet.z), inlet], 0.08)
        for j, (dx, wy, rot) in enumerate(WELLS):
            k = 4 * (n - 1) + j + 1
            well_root, well = fp.build_pumpjack(site, f"WELL_{k}", (cx + dx, wy, 0.0), rot)
            de.well_extras(site, k, well_root)
            w = well["outlet"]
            hx = cx - 3 + 2 * j
            site.pipe(f"PIPE_WELL_{k}_TANK_{n}", [w, (w.x + 0.5, w.y, 0.12), (w.x + 0.5, 60.0, 0.12),
                                                  (hx, 60.0, 0.12), (hx, HEADER_Y + 0.2, 0.12),
                                                  (hx, HEADER_Y, 0.5)], 0.06, segs=8)

        site.empty(f"ROAD_JCT_TANK_{n}", (cx - 7, 0, 0.1), size=1)["kind"] = "waypoint"
        site.empty(f"STOP_TANK_{n}", (cx - 10.2, 35.0, PAD_TOP), size=1)["kind"] = "waypoint"

    # crossover between Tank 3 and Tank 4, with its valve on a small pad in the gap
    xo_root, xo_valve = fp.build_valve(site, "VALVE_XO_34", (0.0, TANK_Y, 1.6), rot_z=math.pi / 2)
    de.valve_extras(site, "VALVE_XO_34", xo_root, "XO-34")
    t3 = Vector((PAD_X[2] + fp.NOZZLE_R, TANK_Y, PAD_TOP + 0.85))
    t4 = Vector((PAD_X[3] - fp.NOZZLE_R, TANK_Y, PAD_TOP + 0.85))
    west, east = sorted((xo_valve["north"], xo_valve["south"]), key=lambda v: v.x)
    site.pipe("PIPE_XO_34_W", [t3, (t3.x, TANK_Y, 1.6), west], 0.11)
    site.pipe("PIPE_XO_34_E", [east, (t4.x, TANK_Y, 1.6), t4], 0.11)
    fp.box(supports["M_CONCRETE"], (1.6, 1.6, 0.16), (0, TANK_Y, 0.08))
    for x in (-15.0, -8.0, 8.0, 15.0):
        fp.box(supports["M_CONCRETE"], (0.6, 0.6, 0.3), (x, TANK_Y, 0.15))
        fp.tube(supports["M_STEEL"], (x, TANK_Y, 0.3), (x, TANK_Y, 1.48), 0.06, 8)
        fp.box(supports["M_STEEL"], (0.1, 0.7, 0.08), (x, TANK_Y, 1.45))

    # disposal wells and the trunk line that feeds them
    swd_in = {}
    for sx, sid in ((-1, "SWD_1"), (1, "SWD_2")):
        swd_root, swd = fp.build_swd(site, sid, (sx * SWD_X, SWD_Y, 0.0), fx=-sx)
        ds.swd_extras(site, sid, swd_root, -sx, sid[-1])
        swd_in[sid] = swd["inlet"]
        site.empty(f"ROAD_JCT_{sid}", (sx * SWD_X, 0, 0.1), size=1)["kind"] = "waypoint"
        site.empty(f"STOP_{sid}", (sx * SWD_X, 7.5, 0.15), size=1)["kind"] = "waypoint"
    w_in, e_in = swd_in["SWD_1"], swd_in["SWD_2"]
    nodes = [w_in.x] + ties + [e_in.x]
    for i, (a, b) in enumerate(zip(nodes, nodes[1:])):
        pts = trunk_run(a, b)
        if i == 0:
            pts = [w_in] + pts
        if i == len(nodes) - 2:
            pts = pts + [e_in]
        site.pipe(f"PIPE_TRUNK_{i}", pts, 0.16, segs=12)
    blocked = drive_ranges()
    x = nodes[0] + 4
    while x < nodes[-1] - 2:
        if all(not (a - 1.5 < x < b + 1.5) for a, b in blocked):
            fp.box(supports["M_CONCRETE"], (0.5, 0.9, TRUNK_Z - 0.15), (x, TRUNK_Y, (TRUNK_Z - 0.15) / 2))
        x += 10.0

    yard = fp.build_yard(site, (YARD[0], YARD[1], 0.0))
    ds.yard_extras(site, yard)
    for tid, x in (("TRUCK_1", -2.0), ("TRUCK_2", 12.5)):
        ds.truck_extras(site, tid, fp.build_truck(site, tid, (x, -27.0, 0.12)))
    ds.power(site, [(cx - 12.8, 30.0) for cx in PAD_X] + [(-216.0, 7.0), (216.0, 7.0), (-20.0, -20.0)])
    site.empty("ROAD_JCT_YARD_1", (0, 0, 0.1), size=1)["kind"] = "waypoint"
    site.empty("STOP_YARD_1", (0, -21.0, 0.12), size=1)["kind"] = "waypoint"
    site.empty("ROAD_END_W", (-225, 0, 0.1), size=1)["kind"] = "waypoint"
    site.empty("ROAD_END_E", (225, 0, 0.1), size=1)["kind"] = "waypoint"
    civil.done()
    supports.done()
    return site


def stage(collection, scene):
    cam_data = bpy.data.cameras.new("CAM_OVERVIEW")
    cam_data.lens = 24
    cam_data.clip_end = 3000
    cam = bpy.data.objects.new("CAM_OVERVIEW", cam_data)
    collection.objects.link(cam)
    cam.location = (0, -235, 170)
    cam.rotation_euler = (Vector((0, 32, 0)) - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    sun_data = bpy.data.lights.new("SUN", "SUN")
    sun_data.energy = 3.5
    sun = bpy.data.objects.new("SUN", sun_data)
    sun.rotation_euler = (math.radians(50), 0, math.radians(35))
    collection.objects.link(sun)
    world = bpy.data.worlds.new("SKY")
    world.color = (0.55, 0.6, 0.66)
    scene.world = world


def export_glb(path):
    kwargs = dict(filepath=path, export_format="GLB", use_active_scene=True, export_apply=True,
                  export_extras=True, export_cameras=True, export_lights=False, export_yup=True,
                  export_animations=False)
    try:
        bpy.ops.export_scene.gltf(**kwargs)
    except TypeError as exc:
        print("exporter rejected an option, retrying with defaults:", exc)
        for key in ("use_active_scene", "export_animations", "export_lights", "export_cameras"):
            kwargs.pop(key, None)
        bpy.ops.export_scene.gltf(**kwargs)


def report(scene):
    tris = 0
    for obj in scene.objects:
        if obj.type == "MESH":
            tris += sum(len(p.vertices) - 2 for p in obj.data.polygons)
    assets = sorted(o.name for o in scene.objects if "asset_id" in o.keys())
    print(f"objects={len(scene.objects)} triangles={tris} assets={len(assets)}")
    print("dotted names:", [o.name for o in scene.objects if "." in o.name])


def render_checks(scene, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    scene.render.engine = "BLENDER_WORKBENCH"
    shading = scene.display.shading
    shading.light, shading.color_type = "STUDIO", "MATERIAL"
    shading.show_cavity, shading.show_shadows = True, True
    scene.render.resolution_x, scene.render.resolution_y = 1600, 900
    shots = {"overview": None,
             "tank_3": ((-12.0, 22.0, 9.0), (-27.5, 40.0, 2.5)),
             "pumpjack": ((-36.0, 61.0, 4.5), (-43.5, 70.5, 2.0)),
             "truck": ((9.0, -38.0, 5.0), (-1.0, -27.0, 1.8)),
             "swd_2": ((176, -12, 20), (205, 16, 2))}
    for name, view in shots.items():
        cam = scene.camera
        if view:
            data = bpy.data.cameras.new(f"CHECK_{name}")
            data.lens = 28
            cam = bpy.data.objects.new(f"CHECK_{name}", data)
            scene.collection.objects.link(cam)
            cam.location = view[0]
            cam.rotation_euler = (Vector(view[1]) - Vector(view[0])).to_track_quat("-Z", "Y").to_euler()
        scene.camera = cam
        scene.render.filepath = os.path.join(out_dir, f"{name}.png")
        bpy.ops.render.render(write_still=True)
        if view:
            bpy.data.objects.remove(cam)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    scene = bpy.context.scene
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for col in list(bpy.data.collections):
        bpy.data.collections.remove(col)
    scene.name = "Field"
    build(scene.collection)
    stage(scene.collection, scene)
    report(scene)
    export_glb(os.path.join(HERE, "field.glb"))
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(HERE, "field.blend"), compress=True)
    if "--render-dir" in argv:
        render_checks(scene, argv[argv.index("--render-dir") + 1])
        scene.camera = bpy.data.objects["CAM_OVERVIEW"]


if __name__ == "__main__":
    main()
