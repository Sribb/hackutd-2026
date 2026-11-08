"""Build the template fields that the generation replay shows (blender/preview/generate.html).

    .venv/bin/python blender/build_templates.py            # needs Blender for Pecos and Delaware

Each template goes through the same steps as an upload: ingest reads the source file, the review
items get the demo values a person would type, and the real generator builds the glb. Avon is
main's committed build (blender/field.glb), so it isn't stored twice. Everything lands in
blender/templates/<id>/, with an index at blender/templates/index.json.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ingest import ingest_bytes  # noqa: E402

OUT = ROOT / "blender" / "templates"
BLENDER = os.environ.get("BLENDER") or "/Applications/Blender.app/Contents/MacOS/Blender"
SECTIONS = ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards")
TEMPLATES = [
    {"id": "avon", "name": "Avon lease, 6 tanks", "source": "scenario/field.json", "build": False,
     "note": "The demo field. Pump 3 trips at 1:47 AM."},
    {"id": "pecos", "name": "Pecos lease, 8 tanks", "source": "scenario/samples/sample1_pecos_8tank.json", "build": True,
     "note": "A complete blueprint for a bigger lease, with a simulated SCADA connector."},
    {"id": "delaware", "name": "Delaware lease, 4 tanks (from a spreadsheet)", "source": "scenario/samples/sample2_delaware_4tank.csv",
     "build": True, "note": "An equipment list the way most operators keep it. Review asks for 14 numbers."},
]
# What a person types on the review screen for the demo. Same numbers as sample 3's drawing sheet.
DEMO_VALUES = {"shutdown_pct": 90, "lost_usd_hr": 260, "manual": True, "minutes_to": 20}


def fill_review(bp, review):
    filled, tanks = [], [t["id"] for t in bp["tanks"]]
    by_id = {a["id"]: a for s in SECTIONS for a in bp.get(s, []) or []}
    for item in review:
        asset = by_id.get(item["id"])
        if asset is None or not item["issue"].startswith("Missing "):
            continue
        for field in item["issue"].split(".")[0].removeprefix("Missing ").split(", "):
            if field in asset:
                continue
            asset[field] = {t: DEMO_VALUES["minutes_to"] for t in tanks} if field == "minutes_to" else DEMO_VALUES.get(field)
            filled.append({"id": item["id"], "field": field, "value": asset[field]})
    return filled


def source_preview(path, text):
    lines = text.splitlines()
    if path.suffix == ".json":
        lines = json.dumps(json.loads(text), indent=1).splitlines()
    return "\n".join(lines[:40]) + ("\n..." if len(lines) > 40 else "")


def main():
    OUT.mkdir(exist_ok=True)
    index = []
    for t in TEMPLATES:
        src = ROOT / t["source"]
        text = src.read_text()
        res = ingest_bytes(src.read_bytes(), src.name)
        bp = res["blueprint"]
        filled = fill_review(bp, res["review"])
        folder = OUT / t["id"]
        folder.mkdir(exist_ok=True)
        items = [{"id": a["id"], "section": s, "label": (a.get("_ingest") or {}).get("label") or a["id"],
                  "confidence": (a.get("_ingest") or {}).get("confidence"), "evidence": (a.get("_ingest") or {}).get("evidence")}
                 for s in SECTIONS for a in bp.get(s, []) or []]
        (folder / "ingest.json").write_text(json.dumps({
            "file": src.name, "bytes": len(src.read_bytes()), "read_by": res["source"].get("read_by"),
            "preview": source_preview(src, text), "counts": res["counts"], "review": res["review"],
            "filled": filled, "items": items, "errors": res["errors"]}, indent=1))
        (folder / "blueprint.json").write_text(json.dumps(bp, indent=1))
        if t["build"]:
            t0 = time.time()
            proc = subprocess.run([BLENDER, "--background", "--factory-startup", "--python", str(ROOT / "blender" / "build_field.py"),
                                   "--", "--blueprint", str(folder / "blueprint.json"), "--out", str(folder)],
                                  cwd=ROOT, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                sys.exit(f"{t['id']}: build failed with code {proc.returncode}\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
            (folder / "field.blend").unlink(missing_ok=True)
            glb, report = f"../templates/{t['id']}/field.glb", f"../templates/{t['id']}/build_report.json"
            print(f"{t['id']}: built in {time.time() - t0:.1f} s")
        else:
            glb, report = "../field.glb", "../build_report.json"
        index.append({"id": t["id"], "name": t["name"], "note": t["note"], "source": t["source"], "glb": glb, "report": report,
                      "ingest": f"../templates/{t['id']}/ingest.json", "blueprint": f"../templates/{t['id']}/blueprint.json",
                      "counts": res["counts"], "review": len(res["review"]), "filled": len(filled)})
        print(f"{t['id']}: {sum(res['counts'].values())} assets, {len(res['review'])} review items, {len(filled)} filled")
    (OUT / "index.json").write_text(json.dumps({"templates": index}, indent=1))
    shutil.rmtree(OUT / "__pycache__", ignore_errors=True)


if __name__ == "__main__":
    main()
