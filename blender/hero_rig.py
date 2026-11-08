"""Lighting rigs for render_hero.py: world, sun or moon, haze, horizon ground, lamps, and bloom.

Everything here is added to the open scene for one render and removed again by Rig.clear(). Nothing
is saved, and lamp meshes get object-linked copies of M_LAMP, so the model's meshes and materials
are never edited.
"""
import math

import bpy
from mathutils import Vector

# Status colors from the web console (web/src/main.js and style.css), so renders match the console.
ALARM, LAMP_OK, BEACON = "#ff4b1f", "#8fa36b", "#ff9a1f"


def srgb(hexstr):
    """'#rrggbb' to a linear RGB tuple, the way Blender stores colors."""
    c = [int(hexstr[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return tuple(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c)


def new_material(name):
    mat = bpy.data.materials.new(name)
    if mat.node_tree is None:  # Blender before 5.0 makes node trees on request
        mat.use_nodes = True
    return mat


def sun_vec(az, el):
    a, e = math.radians(az), math.radians(el)
    return Vector((math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)))

def box_of(objs):
    pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    if not pts:
        return Vector((-50, -50, 0)), Vector((50, 50, 10))
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi

def setup_bloom(scene, strength, threshold):
    """Compositor glare for lamps and status lights. Skipped quietly if this Blender's API differs."""
    try:
        tree = bpy.data.node_groups.new("HERO_comp", "CompositorNodeTree")
        tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        rl, glare = tree.nodes.new("CompositorNodeRLayers"), tree.nodes.new("CompositorNodeGlare")
        out = tree.nodes.new("NodeGroupOutput")
        for name, val in (("Type", "Bloom"), ("Quality", "High"), ("Threshold", threshold), ("Strength", strength),
                          ("Size", 0.7), ("Saturation", 1.0)):
            sock = glare.inputs.get(name)
            if sock is not None:
                try:
                    sock.default_value = val
                except TypeError:
                    sock.default_value = val.upper()
        tree.links.new(rl.outputs["Image"], glare.inputs["Image"])
        tree.links.new(glare.outputs[0], out.inputs[0])
        scene.compositing_node_group = tree
        scene.render.use_compositing = True
    except Exception as e:  # noqa: BLE001 - bloom is a nicety, never a reason to fail a render
        print(f"[hero] bloom skipped: {e}")

# ---------------------------------------------------------------- environment ----
class Rig:
    """Adds the per-shot world, lights, haze, and horizon ground, and removes them again."""

    def __init__(self, field):
        self.f, self.scene, self.temp = field, field.scene, []
        for o in field.scene.objects:
            if o.type == "LIGHT":
                o.hide_render = True  # the build's own SUN; the rigs bring their own
        self.horizon_ground()
        self.saved_mats = {}

    def add(self, ob):
        self.scene.collection.objects.link(ob)
        self.temp.append(ob)
        return ob

    def clear(self):
        for ob in self.temp:
            bpy.data.objects.remove(ob, do_unlink=True)
        self.temp = []
        for (ob, i), mat in self.saved_mats.items():
            ob.material_slots[i].link = "DATA"
        self.saved_mats = {}

    def horizon_ground(self):
        """A ground ring around the lease, in M_GROUND with the same 8 m UV tiling, so the field
        sits on desert all the way to the haze instead of floating. When the build has its own
        terrain (SCENERY_terrain, #98), the ring only fills in past the terrain's far edge."""
        g, terrain = self.f.get("SITE_ground"), self.f.get("SCENERY_terrain")
        if terrain and terrain.type == "MESH":
            lo, hi = box_of([terrain])
            x0, y0, x1, y1, z, R = lo.x + 100, lo.y + 100, hi.x - 100, hi.y - 100, lo.z - 0.5, 2.5 * max(hi.x, hi.y, 6000.0)
        else:
            lo, hi = (box_of([g]) if g else (self.f.lo, self.f.hi))
            x0, y0, x1, y1, z, R = lo.x + 2, lo.y + 2, hi.x - 2, hi.y - 2, -0.42, 6000.0
        vs = [(-R, -R), (R, -R), (R, R), (-R, R), (x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        faces = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
        me = bpy.data.meshes.new("HERO_horizon")
        me.from_pydata([(x, y, z) for x, y in vs], [], faces)
        uv = me.uv_layers.new(name="UVMap")
        for poly in me.polygons:
            for li in poly.loop_indices:
                x, y, _ = me.vertices[me.loops[li].vertex_index].co
                uv.data[li].uv = (x / 8.0, y / 8.0)
        me.materials.append(self.horizon_material(lo, hi))
        self.scene.collection.objects.link(bpy.data.objects.new("HERO_horizon", me))

    def horizon_material(self, lo, hi):
        """The lease's own ground texture at the fence, fading to its average color with distance,
        broken up by large soft noise, so the 8 m texture tiles never show on the horizon."""
        src = bpy.data.materials.get("M_GROUND")
        img = next((n.image for n in src.node_tree.nodes if n.type == "TEX_IMAGE" and n.image
                    and "normal" not in n.image.name.lower()), None) if src and src.node_tree else None
        avg = (0.33, 0.24, 0.16)
        if img:
            try:
                import numpy as np
                px = np.empty(len(img.pixels), dtype=np.float32)
                img.pixels.foreach_get(px)
                rgb = px.reshape(-1, 4)[::7, :3]
                if img.colorspace_settings.name.lower().startswith("srgb"):  # pixels are display-encoded
                    rgb = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
                avg = tuple(float(v) for v in rgb.mean(axis=0))
            except Exception:  # noqa: BLE001 - fall back to a plain desert tan
                pass
        mat = new_material("HERO_horizon")
        nt = mat.node_tree
        N, L = nt.nodes.new, nt.links.new
        col = lambda node, name, outs=False: next(s for s in (node.outputs if outs else node.inputs)
                                                  if s.name == name and s.type == "RGBA")
        bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
        bsdf.inputs["Roughness"].default_value = 1.0
        coord, sep = N("ShaderNodeTexCoord"), N("ShaderNodeSeparateXYZ")
        L(coord.outputs["Object"], sep.inputs[0])
        cx, cy, hx, hy = (lo.x + hi.x) / 2, (lo.y + hi.y) / 2, (hi.x - lo.x) / 2, (hi.y - lo.y) / 2

        def edge(axis, c, h):
            a, b = N("ShaderNodeMath"), N("ShaderNodeMath")
            a.operation, b.operation = "SUBTRACT", "ABSOLUTE"
            L(sep.outputs[axis], a.inputs[0])
            a.inputs[1].default_value = c
            L(a.outputs[0], b.inputs[0])
            d = N("ShaderNodeMath")
            d.operation = "SUBTRACT"
            L(b.outputs[0], d.inputs[0])
            d.inputs[1].default_value = h
            return d.outputs[0]

        mx, fade = N("ShaderNodeMath"), N("ShaderNodeMapRange")
        mx.operation = "MAXIMUM"
        L(edge("X", cx, hx), mx.inputs[0])
        L(edge("Y", cy, hy), mx.inputs[1])
        L(mx.outputs[0], fade.inputs["Value"])
        fade.inputs["From Min"].default_value, fade.inputs["From Max"].default_value = 0.0, 90.0
        flat, noise, ramp = N("ShaderNodeMix"), N("ShaderNodeTexNoise"), N("ShaderNodeMix")
        flat.data_type = ramp.data_type = "RGBA"
        noise.inputs["Scale"].default_value = 0.012
        noise.inputs["Detail"].default_value = 3.0
        L(coord.outputs["Object"], noise.inputs["Vector"])
        col(ramp, "A").default_value = tuple(v * 0.82 for v in avg) + (1,)
        col(ramp, "B").default_value = tuple(v * 1.1 for v in avg) + (1,)
        L(noise.outputs["Fac"], ramp.inputs["Factor"])
        if img:
            tex = N("ShaderNodeTexImage")
            tex.image = img
            L(coord.outputs["UV"], tex.inputs["Vector"])
            L(tex.outputs["Color"], col(flat, "A"))
        else:
            col(flat, "A").default_value = (*avg, 1)
        L(col(ramp, "Result", True), col(flat, "B"))
        L(fade.outputs["Result"], flat.inputs["Factor"])
        L(col(flat, "Result", True), bsdf.inputs["Base Color"])
        # The lease's normal map too, fading out with the color, so low sun shades both sides of the
        # fence alike and no lease rectangle shows from the air.
        nimg = next((n.image for n in src.node_tree.nodes if n.type == "TEX_IMAGE" and n.image
                     and "normal" in n.image.name.lower()), None) if src and src.node_tree else None
        if nimg:
            ntex, nmap, inv = N("ShaderNodeTexImage"), N("ShaderNodeNormalMap"), N("ShaderNodeMath")
            ntex.image = nimg
            inv.operation = "SUBTRACT"
            inv.inputs[0].default_value = 1.0
            L(coord.outputs["UV"], ntex.inputs["Vector"])
            L(ntex.outputs["Color"], nmap.inputs["Color"])
            L(fade.outputs["Result"], inv.inputs[1])
            L(inv.outputs[0], nmap.inputs["Strength"])
            L(nmap.outputs["Normal"], bsdf.inputs["Normal"])
        return mat

    def world(self, rig, cam_az=None):
        w = bpy.data.worlds.new("HERO_world")
        self.scene.world = w
        try:
            w.use_nodes = True
        except AttributeError:
            pass
        nt = w.node_tree
        nt.nodes.clear()
        bg, out = nt.nodes.new("ShaderNodeBackground"), nt.nodes.new("ShaderNodeOutputWorld")
        nt.links.new(bg.outputs[0], out.inputs[0])
        if "hdri" in rig and rig["hdri"].is_file():
            env = nt.nodes.new("ShaderNodeTexEnvironment")
            env.image = bpy.data.images.load(str(rig["hdri"]), check_existing=True)
            mapping, coord = nt.nodes.new("ShaderNodeMapping"), nt.nodes.new("ShaderNodeTexCoord")
            mapping.inputs["Rotation"].default_value[2] = math.radians(rig.get("hdri_rot", 0.0))
            nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
            nt.links.new(mapping.outputs[0], env.inputs["Vector"])
            # The HDRI's own hills are not West Texas: fade everything below about 9 degrees into a
            # flat horizon color, keeping its moon, stars, and Milky Way overhead.
            sep, fade, mix = nt.nodes.new("ShaderNodeSeparateXYZ"), nt.nodes.new("ShaderNodeMapRange"), nt.nodes.new("ShaderNodeMix")
            mix.data_type = "RGBA"
            nt.links.new(coord.outputs["Generated"], sep.inputs[0])
            nt.links.new(sep.outputs["Z"], fade.inputs["Value"])
            fade.inputs["From Min"].default_value, fade.inputs["From Max"].default_value = 0.32, 0.1
            rgba = [s for s in mix.inputs if s.type == "RGBA"]
            nt.links.new(env.outputs["Color"], rgba[0])
            rgba[1].default_value = (*rig["horizon_rgb"], 1)
            nt.links.new(fade.outputs["Result"], mix.inputs["Factor"])
            nt.links.new(next(s for s in mix.outputs if s.type == "RGBA"), bg.inputs["Color"])
            bg.inputs["Strength"].default_value = rig["hdri_w"]
        else:
            sky = nt.nodes.new("ShaderNodeTexSky")
            types = [t.identifier for t in sky.bl_rna.properties["sky_type"].enum_items]
            sky.sky_type = next(t for t in ("MULTIPLE_SCATTERING", "NISHITA", "SINGLE_SCATTERING", "HOSEK_WILKIE") if t in types)
            az, el = rig["sun"]
            sky.sun_elevation, sky.sun_rotation = math.radians(el), math.radians(az)  # rotation is a compass bearing
            for k, v in (("sun_disc", False), ("altitude", 900.0), ("air_density", 1.0), ("aerosol_density", 1.6)):
                try:
                    setattr(sky, k, v)
                except (AttributeError, TypeError):
                    pass
            nt.links.new(sky.outputs[0], bg.inputs["Color"])
            bg.inputs["Strength"].default_value = rig["sky"]
        try:
            w.sun_threshold = 1e6  # EEVEE: keep the rig's own sun or moon as the only shadow caster
        except AttributeError:
            pass

    def sun(self, name, az, el, rgb, watts, angle=0.6):
        if watts <= 0:
            return
        d = bpy.data.lights.new(name, "SUN")
        d.energy, d.color, d.angle = watts, rgb, math.radians(angle)
        ob = self.add(bpy.data.objects.new(name, d))
        ob.rotation_euler = (-sun_vec(az, el)).to_track_quat("-Z", "Y").to_euler()

    def spot(self, name, at, aim, rgb, watts, size=110, blend=0.55, radius=0.25):
        d = bpy.data.lights.new(name, "SPOT")
        d.energy, d.color, d.spot_size, d.spot_blend, d.shadow_soft_size = watts, rgb, math.radians(size), blend, radius
        ob = self.add(bpy.data.objects.new(name, d))
        ob.location = at
        ob.rotation_euler = (aim - at).to_track_quat("-Z", "Y").to_euler()
        return ob

    def point(self, name, at, rgb, watts, radius=0.15):
        d = bpy.data.lights.new(name, "POINT")
        d.energy, d.color, d.shadow_soft_size = watts, rgb, radius
        ob = self.add(bpy.data.objects.new(name, d))
        ob.location = at
        return ob

    def haze(self, density, rgb, height=28.0):
        """Ground haze that thins out with height (density * e^(-z / height)), so it has no hard top."""
        if density <= 0:
            return
        me = bpy.data.meshes.new("HERO_haze")
        R, top = 8000.0, 6 * height  # past the terrain's mesas (#98), so they sit in the haze too
        vs = [(x, y, z) for z in (-2.0, top) for x, y in ((-R, -R), (R, -R), (R, R), (-R, R))]
        me.from_pydata(vs, [], [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)])
        mat = new_material("HERO_haze")
        nt = mat.node_tree
        nt.nodes.clear()
        N = nt.nodes.new
        vol, out = N("ShaderNodeVolumePrincipled"), N("ShaderNodeOutputMaterial")
        coord, sep, k, ex, mul = N("ShaderNodeTexCoord"), N("ShaderNodeSeparateXYZ"), N("ShaderNodeMath"), N("ShaderNodeMath"), N("ShaderNodeMath")
        k.operation, ex.operation, mul.operation = "MULTIPLY", "EXPONENT", "MULTIPLY"
        k.inputs[1].default_value, mul.inputs[1].default_value = -1.0 / height, density
        for a, b in ((coord.outputs["Object"], sep.inputs[0]), (sep.outputs["Z"], k.inputs[0]), (k.outputs[0], ex.inputs[0]),
                     (ex.outputs[0], mul.inputs[0]), (mul.outputs[0], vol.inputs["Density"])):
            nt.links.new(a, b)
        vol.inputs["Color"].default_value = (*rgb, 1)
        vol.inputs["Anisotropy"].default_value = 0.55
        nt.links.new(vol.outputs[0], out.inputs["Volume"])
        me.materials.append(mat)
        self.add(bpy.data.objects.new("HERO_haze", me))

    def emit(self, ob, rgb, strength):
        """Light one lamp mesh with its own copy of M_LAMP (object-linked, so the mesh is untouched)."""
        if not ob or ob.type != "MESH" or not ob.material_slots:
            return
        base = ob.material_slots[0].material
        mat = base.copy() if base else new_material("HERO_lamp")
        bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf:
            bsdf.inputs["Emission Color"].default_value = (*rgb, 1)
            bsdf.inputs["Emission Strength"].default_value = strength
        self.saved_mats[(ob, 0)] = base
        ob.material_slots[0].link = "OBJECT"
        ob.material_slots[0].material = mat

    def lamps(self, level, alarm_pump):
        """Night shift: light towers and truck lights on, status lights lit, one pump in ALARM."""
        if level <= 0:
            return
        f, warm = self.f, (1.0, 0.86, 0.68)
        for name, ob in f.by_name.items():
            if not name.endswith("_lamps") or ob.type != "MESH":
                continue
            lo, hi = box_of([ob])
            at = (lo + hi) / 2 - Vector((0, 0, 0.2))
            owner = f.get(name[:-len("_lamps")])
            aim = owner.matrix_world.translation.copy() if owner else at - Vector((0, 0, 5))
            if name.startswith("PAD_"):
                tank = f.nearest(f.tanks, at)
                pump = f.nearest(f.pumps, tank.matrix_world.translation) if tank else None
                pts = [o.matrix_world.translation for o in (tank, pump) if o]
                aim = sum(pts, Vector()) / len(pts) if pts else aim
            aim.z = 0.0
            self.spot(f"HERO_{name}", at, aim, warm, 4000 * level, size=118, blend=0.7)
            self.emit(ob, warm, 30)
        for t in f.trucks:
            axle, lights = f.get(f"{t.name}_axle_1"), f.get(f"{t.name}_headlights")
            if not (axle and lights):
                continue
            fwd = axle.matrix_world.translation - t.matrix_world.translation
            fwd.z = 0
            fwd.normalize()
            pts = [lights.matrix_world @ v.co for v in lights.data.vertices]
            front = max(p.dot(fwd) for p in pts)
            head = [p for p in pts if p.dot(fwd) > front - 0.3]
            at = sum(head, Vector()) / len(head) + fwd * 0.3
            self.spot(f"HERO_{t.name}_beam", at, at + fwd * 25 - Vector((0, 0, 2.2)), (1.0, 0.93, 0.82),
                      1000 * level, size=70, blend=0.5)
            self.emit(lights, (1.0, 0.92, 0.8), 20)
        if f.trucks:
            self.emit(f.get(f"{f.trucks[0].name}_beacon"), srgb(BEACON), 40)
        ok = srgb(LAMP_OK)
        for name, ob in f.by_name.items():
            if name.endswith("_light") and ob.type == "MESH":
                self.emit(ob, ok, 6)
        if alarm_pump:
            light = f.get(f"{alarm_pump.name}_light")
            self.emit(light, srgb(ALARM), 60)
            if light:
                lo, hi = box_of([light])
                self.point("HERO_alarm_glow", (lo + hi) / 2 + Vector((0, 0, 0.3)), srgb(ALARM), 60, radius=0.2)
