"""Hero renders of a generated field: cinematic stills and a turntable. Headless only.

  Blender --background --factory-startup blender/field.blend --python blender/render_hero.py -- \\
      --out DIR [--quality draft|final] [--turntable] [--shots golden_hour,night_shift] [--engine eevee|cycles]

Works on any generator build: every asset is found by its contract name (docs/CONTRACT.md,
"What's in field.glb"), and a shot whose asset is missing is skipped. Lights, cameras, the haze,
the horizon ground, and poses (a pumpjack mid-stroke, PUMP_3 in ALARM) exist only for the render.
This script never saves the .blend or the glb, so the model files stay exactly as built.

Writes DIR/<shot>.jpg (1600 px wide, under 700 KB), DIR/renders.json (settings and timings), and
with --turntable DIR/turntable.mp4 (1080p, via ffmpeg) or DIR/turntable/ as PNG frames.
"""
import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import bpy
from mathutils import Vector

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from hero_rig import Rig, box_of, setup_bloom  # noqa: E402  (needs the path above)

NIGHT_HDRI = HERE / "assets/hdri/rogland_moonlit_night_2k.hdr"
MAX_JPEG = 700_000

# Light rigs. Sun azimuth is a compass bearing (0 north = +Y, 90 east = +X), elevation in degrees.
RIGS = {
    "golden": dict(sun=(236, 5.5), sun_rgb=(1.0, 0.52, 0.26), sun_w=5.5, sky=0.16, haze=0.0008,
                   haze_rgb=(1.0, 0.8, 0.62), exposure=-0.7, lamps=0.0),
    "afternoon": dict(sun=(222, 24), sun_rgb=(1.0, 0.84, 0.66), sun_w=4.0, sky=0.14, haze=0.0006,
                      haze_rgb=(0.95, 0.88, 0.8), exposure=-0.9, lamps=0.0),
    "blue": dict(sun=(78, -2.5), sun_rgb=(1.0, 0.7, 0.5), sun_w=0.0, sky=1.6, haze=0.0010,
                 haze_rgb=(0.6, 0.68, 0.9), exposure=0.3, lamps=0.3),
    "night": dict(hdri=NIGHT_HDRI, hdri_w=0.06, horizon_rgb=(0.06, 0.09, 0.16), moon=(150, 38),
                  moon_rgb=(0.6, 0.72, 1.0), moon_w=0.045, haze=0.0012, haze_rgb=(0.5, 0.6, 0.9),
                  exposure=1.5, lamps=0.22),
}


def args():
    p = argparse.ArgumentParser(prog="render_hero.py")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--quality", choices=("draft", "final"), default="draft")
    p.add_argument("--engine", choices=("eevee", "cycles"), default=None)
    p.add_argument("--shots", default="", help="comma-separated shot names (default: all)")
    p.add_argument("--turntable", action="store_true")
    p.add_argument("--width", type=int, default=1600, help="still width in px (16:9)")
    p.add_argument("--seconds", type=float, default=11.0)
    p.add_argument("--fps", type=int, default=24)
    return p.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])



# ---------------------------------------------------------------- the field ----
class Field:
    """Everything the shots need, found by contract name."""

    def __init__(self, scene):
        self.scene = scene
        obs = list(scene.objects)
        self.by_name = {o.name: o for o in obs}
        self.assets = {o.name: o for o in obs if "asset_id" in o.keys()}
        kind = lambda k: sorted((o for o in self.assets.values() if o.get("kind") == k), key=lambda o: o.matrix_world.translation.x)
        self.tanks, self.pumps, self.wells = kind("tank"), kind("pump"), kind("well")
        self.swds, self.trucks = kind("disposal_well"), kind("truck")
        meshes = [o for o in obs if o.type == "MESH" and not o.name.startswith("SCENERY")]
        self.lo, self.hi = box_of(meshes)
        self.mid = (self.lo + self.hi) / 2

    def get(self, name):
        return self.by_name.get(name)

    def nearest(self, items, p):
        return min(items, key=lambda o: (o.matrix_world.translation - p).length) if items else None

    def hero_tank(self):
        return self.nearest(self.tanks, self.mid)

    def alarm_pump(self):
        return self.get("PUMP_3") if "PUMP_3" in self.assets else (self.pumps[len(self.pumps) // 2] if self.pumps else None)



def tree_box(root):
    return box_of([root] + [c for c in root.children_recursive if c.type == "MESH"])


# ---------------------------------------------------------------- render setup ----
def setup_render(scene, quality, engine, width, height):
    engines = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    eevee = next((e for e in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE") if e in engines), "CYCLES")
    r = scene.render
    r.engine = "CYCLES" if engine == "cycles" else eevee
    r.resolution_x, r.resolution_y, r.resolution_percentage = width, height, 100
    r.film_transparent = False
    if r.engine == "CYCLES":
        prefs = bpy.context.preferences.addons["cycles"].preferences
        try:
            prefs.compute_device_type = "METAL"
            prefs.get_devices()
            for d in prefs.devices:
                d.use = d.type != "CPU"
            scene.cycles.device = "GPU"
        except (TypeError, AttributeError):
            pass
        scene.cycles.samples = 256 if quality == "final" else 48
        scene.cycles.use_denoising = True
        scene.cycles.max_bounces, scene.cycles.volume_bounces = 6, 1
    else:
        ee = scene.eevee
        ee.taa_render_samples = 96 if quality == "final" else 24
        for k, v in (("use_raytracing", quality == "final"), ("volumetric_end", 4000.0), ("volumetric_start", 0.5),
                     ("volumetric_tile_size", "4" if quality == "final" else "8"), ("volumetric_samples", 96),
                     ("volumetric_sample_distribution", 0.85), ("use_volumetric_shadows", True),
                     ("shadow_ray_count", 3 if quality == "final" else 1), ("shadow_step_count", 8),
                     ("use_shadows", True), ("bokeh_max_size", 60.0)):
            try:
                setattr(ee, k, v)
            except (AttributeError, TypeError):
                pass
    vs = scene.view_settings
    for vt in ("AgX", "Filmic"):
        try:
            vs.view_transform = vt
            break
        except TypeError:
            continue
    for look in ("AgX - Medium High Contrast", "Medium High Contrast"):
        try:
            vs.look = look
            break
        except TypeError:
            continue
    return r.engine




# ---------------------------------------------------------------- poses ----
def pose_pumpjacks(field, t=0.6):
    """Walking beams mid-stroke, using the console's own motion (beam 0.25 sin, crank angle)."""
    for i, w in enumerate(field.wells):
        phase = (i * 2.399) % (2 * math.pi)
        beam, crank = field.get(f"{w.name}_beam"), field.get(f"{w.name}_crank")
        if beam:
            beam.rotation_euler.x = 0.25 * math.sin(1.7 * t + phase)
        if crank:
            crank.rotation_euler.x = 1.7 * t + phase


def set_level(field, tank, level):
    lv, gauge = field.get(f"{tank.name}_LEVEL"), field.get(f"{tank.name}_gauge")
    if lv:
        lv.scale.z = level
    if gauge and abs(gauge.location.z - (0.3 + 4.3 * 0.55)) < 0.6:
        gauge.location.z = 0.3 + 4.3 * level


# ---------------------------------------------------------------- cameras ----
def camera(scene, name, at, target, lens, fstop=None, focus=None):
    d = bpy.data.cameras.new(name)
    d.lens, d.sensor_width, d.clip_start, d.clip_end = lens, 36.0, 0.1, 20000.0
    if fstop:
        d.dof.use_dof, d.dof.aperture_fstop = True, fstop
        d.dof.focus_distance = ((focus if focus is not None else target) - at).length
    ob = bpy.data.objects.new(name, d)
    scene.collection.objects.link(ob)
    ob.location = at
    ob.rotation_euler = (target - at).to_track_quat("-Z", "Y").to_euler()
    scene.camera = ob
    return ob


def fit(lo, hi, direction, lens, aspect, margin=0.92):
    """Distance along direction (target to camera) that fits the box lo..hi in frame."""
    target = (lo + hi) / 2
    f = -direction.normalized()
    r = f.cross(Vector((0, 0, 1))).normalized()
    u = r.cross(f)
    th = 18.0 / lens * margin
    tv = th / aspect
    corners = [Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    a, b = 1.0, 50000.0
    for _ in range(60):
        d = (a + b) / 2
        cam = target - f * d
        ok = all((p - cam).dot(f) > 0 and abs((p - cam).dot(r)) <= th * (p - cam).dot(f)
                 and abs((p - cam).dot(u)) <= tv * (p - cam).dot(f) for p in corners)
        a, b = (a, d) if ok else (d, b)
    return target, target - f * b


def local(ob, x, y, z):
    return ob.matrix_world @ Vector((x, y, z))


def closeup(ob, cam, tgt, lens, fstop):
    return dict(at=local(ob, *cam), target=local(ob, *tgt), lens=lens, fstop=fstop)


def shot_list(f, aspect):
    """Shot name -> (rig, camera builder). A shot whose asset is missing from this field is skipped."""
    lo, hi = f.lo.copy(), f.hi.copy()
    hi.z = max(hi.z, 12.0)

    L, my = max(hi.x - lo.x, 120.0), f.mid.y
    # Overviews look down the length of the lease, so the pads recede into the haze.
    shots = {"golden_hour": ("golden", lambda: dict(at=Vector((hi.x - 0.01 * L, lo.y + 0.04 * L, 0.06 * L)),
                                                    target=Vector((lo.x + 0.42 * L, my + 0.04 * L, 0)), lens=30))}
    pump, tank = f.alarm_pump(), f.hero_tank()
    if pump:
        p = pump.matrix_world.translation
        t = f.nearest(f.tanks, p).matrix_world.translation if f.tanks else p
        shots["night_shift"] = ("night", lambda: dict(at=Vector((p.x + 42, p.y - 58, 25)),
                                                      target=Vector((t.x - 32, t.y - 4, 1.5)), lens=30))
    shots["blue_hour"] = ("blue", lambda: dict(at=Vector((lo.x + 0.01 * L, lo.y + 0.04 * L, 0.055 * L)),
                                               target=Vector((hi.x - 0.42 * L, my + 0.04 * L, 0)), lens=30))
    well = f.nearest(f.wells, tank.matrix_world.translation if tank else f.mid)
    if well:
        shots["pumpjack"] = ("golden", lambda: closeup(well, (13.5, -4.0, 1.8), (0, -0.4, 3.0), 45, 1.8))
    if tank:
        shots["tank_battery"] = ("afternoon", lambda: closeup(tank, (7.0, -17.0, 7.0), (0, -1.0, 2.4), 35, 4.0))
    if f.swds:
        slo, shi = tree_box(f.swds[0])
        c = (slo + shi) / 2
        sx, sy = (1 if c.x < f.mid.x else -1), (-1 if c.y >= 0 else 1)
        shots["disposal"] = ("golden", lambda: dict(at=c + Vector((20 * sx, 27 * sy, 5.5)),
                                                     target=c + Vector((0, 0, 2.0)), lens=35, fstop=4.0))
    if pump:
        shots["night_alarm"] = ("night", lambda: closeup(pump, (6.5, -8.0, 3.0), (0, 0.4, 1.1), 40, 2.8))
    return shots


def save_jpeg(scene, path):
    """Save the render as JPEG, stepping quality down until it is under 700 KB."""
    img, s = bpy.data.images["Render Result"], scene.render.image_settings
    s.file_format, s.color_mode = "JPEG", "RGB"
    for q in (92, 89, 86, 83, 80, 76, 72, 68):
        s.quality = q
        img.save_render(str(path), scene=scene)
        if path.stat().st_size <= MAX_JPEG:
            break
    return q, path.stat().st_size


def light_shot(rig, rig_name, f):
    R = RIGS[rig_name]
    rig.clear()
    rig.world(R)
    if "moon" in R:
        rig.sun("HERO_moon", *R["moon"], R["moon_rgb"], R["moon_w"], angle=0.6)
    else:
        rig.sun("HERO_sun", *R["sun"], R["sun_rgb"], R["sun_w"], angle=0.8)
    rig.haze(R["haze"], R["haze_rgb"])
    rig.lamps(R["lamps"], f.alarm_pump() if rig_name == "night" else None)
    f.scene.view_settings.exposure = R["exposure"]
    setup_bloom(f.scene, 0.9 if rig_name == "night" else 0.5 if rig_name == "blue" else 0.25,
                0.8 if R["lamps"] else 1.6)


def turntable(f, rig, scene, out, a, aspect):
    """An orbit of the golden hour overview, pumpjacks working, encoded with ffmpeg if present."""
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    light_shot(rig, "golden", f)
    n = round(a.seconds * a.fps)
    scene.frame_start, scene.frame_end, scene.render.fps = 1, n, a.fps
    lo, hi = f.lo.copy(), f.hi.copy()
    hi.z = max(hi.z, 12.0)
    start = Vector((0.40, -0.86, 0.30)).normalized()
    target, at = fit(lo, hi, start, 35, aspect, 0.9)
    radius, el, az0 = (at - target).length, math.asin(start.z), math.atan2(start.y, start.x)
    cam = camera(scene, "HERO_turntable", at, target, 35)
    for i in range(n):
        az = az0 + 2 * math.pi * i / n
        cam.location = target + radius * Vector((math.cos(az) * math.cos(el), math.sin(az) * math.cos(el), math.sin(el)))
        cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
        cam.keyframe_insert("location", frame=i + 1)
        cam.keyframe_insert("rotation_euler", frame=i + 1)
        pose_pumpjacks(f, t=i / a.fps)
        for w in f.wells:
            for part in ("beam", "crank"):
                if f.get(f"{w.name}_{part}"):
                    f.get(f"{w.name}_{part}").keyframe_insert("rotation_euler", index=0, frame=i + 1)
    ff = shutil.which("ffmpeg")
    frames = Path(tempfile.mkdtemp(prefix="hero_tt_")) if ff else out / "turntable"
    frames.mkdir(parents=True, exist_ok=True)
    s = scene.render.image_settings
    s.file_format, s.color_mode = "PNG", "RGB"
    scene.render.filepath = str(frames / "f_")
    t0 = time.time()
    bpy.ops.render.render(animation=True)
    info = {"frames": n, "fps": a.fps, "render_seconds": round(time.time() - t0, 1), "size": [1920, 1080]}
    if not ff:
        return {**info, "file": "turntable/", "encoded": False, "note": "ffmpeg not found, PNG frames written"}
    mp4 = out / "turntable.mp4"
    cmd = [ff, "-y", "-loglevel", "error", "-framerate", str(a.fps), "-i", str(frames / "f_%04d.png"), "-c:v", "libx264",
           "-preset", "slow", "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)]
    ok = subprocess.run(cmd).returncode == 0 and mp4.is_file()
    if ok:
        shutil.rmtree(frames, ignore_errors=True)
    return {**info, "file": mp4.name if ok else str(frames), "encoded": ok, "bytes": mp4.stat().st_size if ok else 0}


def main():
    a = args()
    a.out.mkdir(parents=True, exist_ok=True)
    scene = bpy.context.scene
    f = Field(scene)
    used = setup_render(scene, a.quality, a.engine or "eevee", a.width, round(a.width * 9 / 16))
    aspect, rig = 16 / 9, Rig(f)
    levels = {t.name: f.get(f"{t.name}_LEVEL").scale.z for t in f.tanks if f.get(f"{t.name}_LEVEL")}
    wanted = [s for s in a.shots.split(",") if s]
    report = {"blend": Path(bpy.data.filepath).name, "engine": used, "quality": a.quality, "width": a.width, "shots": []}
    t_all = time.time()
    for name, (rig_name, build) in shot_list(f, aspect).items():
        if wanted and name not in wanted:
            continue
        t0 = time.time()
        for t in f.tanks:
            if t.name in levels:
                set_level(f, t, levels[t.name])
        pose_pumpjacks(f, t=0.6)
        if rig_name == "night" and f.alarm_pump():
            set_level(f, f.nearest(f.tanks, f.alarm_pump().matrix_world.translation), 0.88)  # the tank nearing full
        if name == "tank_battery":
            set_level(f, f.hero_tank(), 0.72)
        light_shot(rig, rig_name, f)
        cam = camera(scene, f"HERO_cam_{name}", **build())
        bpy.ops.render.render()
        q, size = save_jpeg(scene, a.out / f"{name}.jpg")
        bpy.data.objects.remove(cam, do_unlink=True)
        secs = round(time.time() - t0, 1)
        report["shots"].append({"name": name, "rig": rig_name, "file": f"{name}.jpg", "seconds": secs,
                                "jpeg_quality": q, "bytes": size})
        print(f"[hero] {name:13s} {rig_name:9s} {secs:6.1f} s  {size / 1000:5.0f} KB  q{q}", flush=True)
    if a.turntable:
        report["turntable"] = turntable(f, rig, scene, a.out, a, aspect)
        print(f"[hero] turntable {report['turntable']}", flush=True)
    report["seconds"] = round(time.time() - t_all, 1)
    (a.out / "renders.json").write_text(json.dumps(report, indent=1) + "\n")
    print(f"[hero] done in {report['seconds']} s, wrote {a.out}")


if __name__ == "__main__":
    main()
