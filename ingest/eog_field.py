"""The 3D half of ingest/eog_network.py: build each system's blueprint into field.glb.

build_glb() runs blender/assemble_glb.py in this process (the model kit is loaded once, so a
system builds in well under a second) or blender/build_field.py headless with --factory-startup.
write_system() writes blueprint.json, ids.json and the glb into one folder, stamps every
sponsor ID into its node's extras.source_id, and lists each pipeline's pipes. field_glb()
caches the result per dataset, for the backend's GET /api/dataset/field.glb.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from .eog_network import BLENDER_APP, BLENDER_DIR, ROOT, VERSION, DatasetError, convert, load_folder

_KIT = None
_LOCK = threading.Lock()      # one build at a time per process: builds take well under a second


def _assembler():
    """blender/assemble_glb.py, imported once, with its model kit loaded once."""
    global _KIT
    if str(BLENDER_DIR) not in sys.path:
        sys.path.insert(0, str(BLENDER_DIR))
    import assemble_glb  # noqa: E402
    if _KIT is None:
        _KIT = assemble_glb.Kit()
    return assemble_glb, _KIT


def build_glb(bp_path, out_dir, engine="kit"):
    """Build field.glb from a blueprint file. Returns build_report.json as a dict."""
    out_dir = Path(out_dir)
    if engine == "blender":
        if not Path(BLENDER_APP).exists():
            raise DatasetError(f"Blender not found at {BLENDER_APP}; set BLENDER or use --glb kit")
        subprocess.run([BLENDER_APP, "--background", "--factory-startup", "--python", str(BLENDER_DIR / "build_field.py"),
                        "--", "--blueprint", str(bp_path), "--out", str(out_dir)], cwd=ROOT, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif engine == "kit":
        ag, kit = _assembler()
        t0, report = time.time(), {"ok": False, "errors": [], "warnings": [], "model": "kit"}
        opts = ag.parse_args(["--blueprint", str(bp_path), "--out", str(out_dir)])
        out_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stdout(io.StringIO()):     # keep the caller's stdout clean
            try:
                code = ag.run(opts, str(Path(bp_path).resolve()), str(out_dir.resolve()), report, kit=kit)
            except Exception as e:  # noqa: BLE001
                code = 2
                report["errors"].append(f"the build crashed: {e!r}")
            report["ok"] = code == 0 and not report["errors"]
            ag.write_report(report, str(out_dir), t0)
    else:
        raise DatasetError(f"--glb must be kit or blender, not {engine!r}")
    try:
        return json.loads((out_dir / "build_report.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "errors": ["the build wrote no report"]}


def _stamp_and_route(glb_path, ids, bp):
    """Write each sponsor ID into its node's extras.source_id, and find each pipeline's pipes:
    its own run from the pump to the trunk, then the trunk pieces on the way to the disposal well."""
    raw = bytearray(Path(glb_path).read_bytes())
    n = struct.unpack_from("<I", raw, 12)[0]
    g = json.loads(raw[20:20 + n])
    source = {node: src for src, node in ids.items()}
    for nd in g.get("nodes", []):
        src = source.get(nd.get("name"))
        if src:
            nd.setdefault("extras", {})["source_id"] = src
    text = json.dumps(g, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    rest = raw[20 + n:]
    out = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(text) + len(rest)) + struct.pack("<II", len(text), 0x4E4F534A)
    Path(glb_path).write_bytes(out + text + bytes(rest))

    if str(BLENDER_DIR) not in sys.path:
        sys.path.insert(0, str(BLENDER_DIR))
    from glb_read import Glb  # noqa: E402
    glb, routes = Glb(str(glb_path)), {}
    trunk = {nm: glb.box(nm) for nm in glb.by_name if re.fullmatch(r"PIPE_TRUNK_\d+", nm)}
    trunk = {nm: b for nm, b in trunk.items() if b}
    mid = lambda b: (b[0] + b[1]) / 2  # noqa: E731
    for v in bp["valves"]:
        pid, vid, swd = v["pump_id"], v["id"], v["connects"][1]
        own = [nm for nm in (f"PIPE_{pid}_{vid}", f"PIPE_{vid}_TRUNK") if nm in glb.by_name]
        branch = f"PIPE_TRUNK_{swd}" if f"PIPE_TRUNK_{swd}" in glb.by_name else None
        path = []
        if f"PIPE_{vid}_TRUNK" in glb.by_name and trunk and (branch or swd in glb.by_name):
            tx = mid(glb.box(f"PIPE_{vid}_TRUNK"))
            if branch:            # a disposal well mid-trunk has its own branch off the trunk
                ex = mid(glb.box(branch))
            else:                 # one at a trunk end feeds in at that end
                ex = (min(b[0] for b in trunk.values()) if mid(glb.box(swd)) < tx
                      else max(b[1] for b in trunk.values()))
            lo, hi = min(tx, ex) - 2.0, max(tx, ex) + 2.0
            path = sorted((nm for nm, b in trunk.items() if b[0] >= lo and b[1] <= hi), key=lambda nm: abs(mid(trunk[nm]) - tx))
        routes[v.get("_source_id") or vid] = own + path + ([branch] if branch else [])
    return routes


def write_system(result, out_dir, engine=None):
    """Write <out_dir>/blueprint.json and ids.json, and with engine "kit" or "blender" field.glb.
    Returns a summary dict (seconds, glb_mb, report, missing node names)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    bp, ids = result["blueprint"], result["ids"]
    bp_path = out_dir / "blueprint.json"
    bp_path.write_text(json.dumps(bp, indent=2) + "\n")
    sid = bp["_ingest"]["source"]["system"]
    doc = {"system": sid, "dataset_id": bp["_ingest"]["source"].get("dataset_id"), "glb": None,
           "assets": ids, "nodes": {v: k for k, v in ids.items() if not v.startswith("PAD_")},
           "pads": {k: v for k, v in ids.items() if v.startswith("PAD_")},
           "pipes": {v["_source_id"]: [f"PIPE_{v['pump_id']}_{v['id']}", f"PIPE_{v['id']}_TRUNK"]
                     for v in bp["valves"] if v.get("_source_id")},
           "truck_yard": bp["yards"][0]["id"] if bp["yards"] else None,
           "trucks": [t["id"] for t in bp["trucks"]], "review": result["review"]}
    summary = {"system": sid, "blueprint": str(bp_path), "ids": str(out_dir / "ids.json")}
    if engine:
        t0 = time.time()
        for stale in ("field.glb", "build_report.json"):
            if (out_dir / stale).exists():
                (out_dir / stale).unlink()
        report = build_glb(bp_path, out_dir, engine)
        glb = out_dir / "field.glb"
        if report.get("ok") and glb.exists():
            doc["glb"] = "field.glb"
            doc["pipes"].update(_stamp_and_route(glb, ids, bp))
            summary["glb_mb"] = round(glb.stat().st_size / 1e6, 2)
        summary.update({"glb": str(glb) if doc["glb"] else None, "ok": bool(doc["glb"]),
                        "errors": report.get("errors", []), "seconds": round(time.time() - t0, 3),
                        "triangles": report.get("triangles")})
    (out_dir / "ids.json").write_text(json.dumps(doc, indent=2) + "\n")
    return summary


def field_glb(folder, system, cache_dir=None, engine="kit"):
    """The path of field.glb for one system of a dataset folder, built on first call and cached.

    The cache key is the bytes of network.json and ops.json, this converter's VERSION and the
    model kit, so a new folder (test/) builds once and every later request is a file read.
    Raises DatasetError for an unknown system or a failed build."""
    network, ops, raw = load_folder(folder)
    results = convert(network, ops)
    if system not in results:
        raise DatasetError(f"no system {system!r} in {folder}; it has {', '.join(results) or 'none'}")
    code = hashlib.sha256()
    for f in (BLENDER_DIR / "kit" / "manifest.json", Path(__file__), Path(__file__).with_name("eog_network.py"),
              BLENDER_DIR / "assemble_glb.py", BLENDER_DIR / "layout.py"):
        code.update(f.read_bytes())
    key = hashlib.sha256(raw + f"|{VERSION}|{engine}|{code.hexdigest()}".encode()).hexdigest()[:16]
    base = Path(cache_dir) if cache_dir else BLENDER_DIR / "out" / "eog"
    out_dir = base / key / system
    glb = out_dir / "field.glb"
    with _LOCK:
        if glb.exists() and (out_dir / "ids.json").exists():
            return glb
        tmp = base / key / f".{system}.{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        s = write_system(results[system], tmp, engine)
        if not s.get("ok"):
            shutil.rmtree(tmp, ignore_errors=True)
            raise DatasetError(f"{system} did not build: {'; '.join(s.get('errors') or ['unknown error'])}")
        shutil.rmtree(out_dir, ignore_errors=True)
        os.replace(tmp, out_dir)
    return glb
