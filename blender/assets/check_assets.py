#!/usr/bin/env python3
"""Validate blender/assets/manifest.json against the files on disk.

Checks that every listed file exists, every license is CC0 1.0, each model is under
15,000 triangles and 3 MB (counted from the GLB itself), each GLB has one root node
named after its ID, textures are at most 1K (2K for the one main ground), the HDRI is a
real Radiance file, no asset file is missing from the manifest, and the whole kit is
under 60 MB. Prints a short PASS or FAIL summary and exits 1 on any failure.

Usage: python3 blender/assets/check_assets.py
"""
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWED_LICENSES = {"CC0 1.0"}
MAX_TRIS = 15000
MAX_GLB_BYTES = 3 * 1024 * 1024
MAX_TOTAL_BYTES = 60 * 1024 * 1024
TEX_PX, MAIN_GROUND_PX, HDRI_MAX_W = 1024, 2048, 2048
ASSET_EXTS = {".glb", ".jpg", ".jpeg", ".png", ".hdr"}
REQUIRED = ("id", "category", "file", "source_url", "license", "author", "triangles", "bytes", "notes")

failures, passes = [], []


def fail(msg):
    failures.append(msg)


def glb_info(path):
    """Return (root node names, all node names, triangle count) from a GLB file."""
    data = path.read_bytes()
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or length != len(data):
        raise ValueError("not a valid glTF 2.0 binary")
    jlen, jtype = struct.unpack_from("<I4s", data, 12)
    if jtype != b"JSON":
        raise ValueError("first chunk is not JSON")
    gltf = json.loads(data[20:20 + jlen])
    nodes = gltf.get("nodes", [])
    scene = gltf.get("scenes", [{}])[gltf.get("scene", 0)]
    roots = [nodes[i].get("name", "") for i in scene.get("nodes", [])]
    tris = 0

    def walk(i):
        nonlocal tris
        node = nodes[i]
        if "mesh" in node:
            for prim in gltf["meshes"][node["mesh"]]["primitives"]:
                if prim.get("mode", 4) != 4:
                    raise ValueError("non-triangle primitive")
                acc = prim["indices"] if "indices" in prim else prim["attributes"]["POSITION"]
                tris += gltf["accessors"][acc]["count"] // 3
        for c in node.get("children", []):
            walk(c)

    for i in scene.get("nodes", []):
        walk(i)
    return roots, [n.get("name", "") for n in nodes], tris


def jpeg_size(path):
    data = path.read_bytes()
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack_from(">H", data, i + 2)[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack_from(">HH", data, i + 5)
            return w, h
        i += 2 + seg
    raise ValueError("no JPEG size marker")


def hdr_size(path):
    with path.open("rb") as f:
        head = f.read(4096)
    if not (head.startswith(b"#?RADIANCE") or head.startswith(b"#?RGBE")):
        raise ValueError("not a Radiance HDR file")
    m = re.search(rb"\n([-+]Y) (\d+) ([-+]X) (\d+)\n", head)
    if not m:
        raise ValueError("no resolution line")
    return int(m.group(4)), int(m.group(2))


def check_entry(a, seen_ids, listed):
    aid = a.get("id", "?")
    missing = [k for k in REQUIRED if k not in a]
    if missing:
        fail(f"{aid}: manifest entry is missing {', '.join(missing)}")
        return
    if aid in seen_ids:
        fail(f"{aid}: duplicate id")
    seen_ids.add(aid)
    if a["license"] not in ALLOWED_LICENSES:
        fail(f"{aid}: license is '{a['license']}', must be CC0 1.0")
    files = a.get("files") or [a["file"]]
    if a["file"] not in files:
        fail(f"{aid}: 'file' is not in 'files'")
    total = 0
    for rel in files:
        p = (ROOT / rel).resolve()
        if ROOT not in p.parents or not p.is_file():
            fail(f"{aid}: file not found: {rel}")
            return
        listed.add(p)
        total += p.stat().st_size
    if total != a["bytes"]:
        fail(f"{aid}: manifest says {a['bytes']} bytes, files are {total}")

    path = ROOT / a["file"]
    try:
        if path.suffix == ".glb":
            roots, names, tris = glb_info(path)
            if roots != [aid]:
                fail(f"{aid}: GLB root nodes are {roots}, expected exactly ['{aid}']")
            dupes = [n for n in names if re.search(r"\.\d{3}$", n)]
            if dupes:
                fail(f"{aid}: '.001'-style node names: {dupes}")
            if tris != a["triangles"]:
                fail(f"{aid}: manifest says {a['triangles']} triangles, GLB has {tris}")
            if tris > MAX_TRIS:
                fail(f"{aid}: {tris} triangles, budget is {MAX_TRIS}")
            if total > MAX_GLB_BYTES:
                fail(f"{aid}: {total / 1e6:.2f} MB, budget is 3 MB")
        elif a["category"] == "textures":
            limit = MAIN_GROUND_PX if a.get("main_ground") else TEX_PX
            for rel in files:
                w, h = jpeg_size(ROOT / rel)
                if max(w, h) > limit:
                    fail(f"{aid}: {rel} is {w}x{h}, budget is {limit}")
        elif path.suffix == ".hdr":
            w, h = hdr_size(path)
            if w > HDRI_MAX_W:
                fail(f"{aid}: HDRI is {w}x{h}, budget is 2K")
        else:
            fail(f"{aid}: unexpected file type {path.suffix}")
    except (ValueError, KeyError, IndexError, struct.error) as e:
        fail(f"{aid}: could not read {a['file']}: {e}")


def main():
    try:
        manifest = json.loads((ROOT / "manifest.json").read_text())
    except (OSError, ValueError) as e:
        print(f"FAIL  manifest.json: {e}")
        return 1
    assets = manifest.get("assets", [])
    seen_ids, listed = set(), set()
    for a in assets:
        check_entry(a, seen_ids, listed)
    mains = [a["id"] for a in assets if a.get("main_ground")]
    if len(mains) > 1:
        fail(f"only one texture may be the 2K main ground, found {mains}")

    on_disk = [p for p in ROOT.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    for p in on_disk:
        if p.suffix.lower() in ASSET_EXTS and p.resolve() not in listed:
            fail(f"{p.relative_to(ROOT)} is not in manifest.json")
    total = sum(p.stat().st_size for p in on_disk)
    if total > MAX_TOTAL_BYTES:
        fail(f"kit is {total / 1e6:.1f} MB, budget is 60 MB")

    cats = {}
    for a in assets:
        cats[a["category"]] = cats.get(a["category"], 0) + 1
    models = [a for a in assets if a.get("file", "").endswith(".glb")]
    licenses = sorted({a.get("license") for a in assets})
    print(f"assets: {len(assets)} ({', '.join(f'{k} {v}' for k, v in sorted(cats.items()))})")
    print(f"models: {len(models)}, largest {max((a['triangles'] for a in models), default=0)} triangles, "
          f"{max((a['bytes'] for a in models), default=0) / 1e6:.2f} MB")
    print(f"licenses: {', '.join(licenses)}")
    print(f"kit size: {total / 1e6:.1f} MB of 60 MB ({len(on_disk)} files)")
    print(f"gaps listed: {len(manifest.get('gaps', []))}")
    if failures:
        for f in failures:
            print(f"FAIL  {f}")
        print(f"FAIL: {len(failures)} problem(s)")
        return 1
    print("PASS: every file exists, every license is CC0 1.0, and every budget holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
