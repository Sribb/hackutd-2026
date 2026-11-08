"""Upload presets and the asset library to the SpacetimeDB library module.

    SPACETIME_TOKEN=... .venv/bin/python -m library.upload --db hackutd-freeboard-library \
        [--assets blender/assets] [--field-glb blender/field.glb] [--only presets|assets]

SPACETIME_TOKEN must belong to a library admin (the identity that published the module).
Never commit it. Uploads are idempotent: an asset whose sha256 is already complete on the
server is skipped.
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHUNK = 256 * 1024
MEDIA = {".glb": "model/gltf-binary", ".hdr": "image/vnd.radiance", ".jpg": "image/jpeg", ".png": "image/png"}


class Library:
    def __init__(self, url, db, token):
        self.url, self.db, self.token = url.rstrip("/"), db, token

    def _post(self, path, body, content_type):
        req = urllib.request.Request(f"{self.url}/v1/database/{self.db}/{path}", data=body, method="POST",
                                     headers={"Content-Type": content_type, "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return res.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{path}: HTTP {exc.code} {exc.read().decode(errors='replace')[:300]}") from None

    def call(self, reducer, *args):
        self._post(f"call/{reducer}", json.dumps(list(args)).encode(), "application/json")

    def sql(self, query):
        out = json.loads(self._post("sql", query.encode(), "text/plain"))
        return [row for result in out for row in result.get("rows", [])]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def upload_presets(lib):
    field = json.loads((ROOT / "scenario" / "field.json").read_text())
    presets = [("avon_field_6_tanks", field.get("name", "Avon field"), "The demo field: 6 tanks, 24 wells, 2 disposal wells. Pump 3 trips at 1:47 AM.", field)]
    for sample in sorted((ROOT / "blender" / "samples").glob("*.json")):
        bp = json.loads(sample.read_text())
        presets.append((f"sample_{sample.stem}", bp.get("name", sample.stem), f"Generator sample blender/samples/{sample.name}", bp))
    for pid, name, desc, bp in presets:
        lib.call("upsert_field_preset", pid, name, desc, json.dumps(bp))
        print(f"field preset {pid}: {len(bp.get('tanks', []))} tanks")
    for p in json.loads((ROOT / "library" / "presets" / "equipment.json").read_text())["presets"]:
        lib.call("upsert_equipment_preset", p["preset_id"], p["kind"], p["name"], json.dumps(p["values"]), p["source"])
    print(f"equipment presets: {len(json.loads((ROOT / 'library' / 'presets' / 'equipment.json').read_text())['presets'])}")


def asset_entries(assets_dir, field_glb):
    entries = []
    if assets_dir:
        manifest = json.loads((assets_dir / "manifest.json").read_text())
        for a in manifest["assets"]:
            files = a.get("files") or [a["file"]]
            for rel in files:
                asset_id = a["id"] if len(files) == 1 else f"{a['id']}/{Path(rel).name}"
                entries.append({"asset_id": asset_id, "path": assets_dir / rel, "library": "cc0_scenery",
                                "category": a["category"], "license": a["license"], "source_url": a.get("source_url", ""),
                                "author": a.get("author") or "", "triangles": int(a.get("triangles") or 0),
                                "notes": a.get("notes", "")[:500]})
    if field_glb and field_glb.exists():
        entries.append({"asset_id": "FREEBOARD_field_avon", "path": field_glb, "library": "freeboard", "category": "field",
                        "license": "Team-made for HackUTD 2026", "source_url": "blender/build_field.py", "author": "Freeboard team",
                        "triangles": 0, "notes": "The generated demo field, built by blender/build_field.py."})
    return entries


def upload_assets(lib, entries):
    have = {r[0]: (r[1], r[2]) for r in lib.sql("SELECT asset_id, sha256, complete FROM library_asset")}
    sent = skipped = 0
    for e in entries:
        data = e["path"].read_bytes()
        digest = sha256(data)
        if have.get(e["asset_id"]) == (digest, True):
            skipped += 1
            continue
        chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
        media = MEDIA.get(e["path"].suffix.lower()) or mimetypes.guess_type(e["path"].name)[0] or "application/octet-stream"
        lib.call("begin_asset", e["asset_id"], e["library"], e["category"], e["path"].name, media, len(data), digest,
                 len(chunks), e["license"], e["source_url"], e["author"], e["triangles"], e["notes"])
        for seq, chunk in enumerate(chunks):
            lib.call("put_chunk", e["asset_id"], seq, base64.b64encode(chunk).decode())
        lib.call("finish_asset", e["asset_id"])
        sent += 1
        print(f"uploaded {e['asset_id']}: {len(data) / 1e6:.2f} MB in {len(chunks)} chunks")
    print(f"assets: {sent} uploaded, {skipped} already current")


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--url", default="https://maincloud.spacetimedb.com")
    p.add_argument("--assets", type=Path, help="folder with manifest.json, e.g. blender/assets")
    p.add_argument("--field-glb", type=Path, default=ROOT / "blender" / "field.glb")
    p.add_argument("--only", choices=["presets", "assets"])
    args = p.parse_args(argv)
    token = os.environ.get("SPACETIME_TOKEN")
    if not token:
        print("Set SPACETIME_TOKEN to a library admin's token.", file=sys.stderr)
        return 1
    lib = Library(args.url, args.db, token)
    if args.only != "assets":
        upload_presets(lib)
    if args.only != "presets":
        upload_assets(lib, asset_entries(args.assets, args.field_glb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
