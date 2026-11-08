"""Blueprint lifecycle: upload, review, validate, build in 3D, activate.

    UPLOADED -> REVIEW -> VALID -> BUILDING -> READY -> ACTIVE
          \\-> INVALID              \\-> BUILD_FAILED

Storage: backend/data/blueprints/<id>/ holds blueprint.json, meta.json, and the
generator output. It's gitignored. Ingest (ingest/, Sriyan) reads the upload,
backend/app/validate.py checks it, and blender/build_field.py (Sriyan) builds
field.glb. A blueprint can be activated once it's VALID: the 3D build is
optional, so a missing Blender never blocks the forecast.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import fields
from .scenario import REPO_DIR, ScenarioError, prepare

STORE = Path(os.environ.get("BLUEPRINT_DIR", REPO_DIR / "backend" / "data" / "blueprints"))
GENERATOR = REPO_DIR / "blender" / "build_field.py"
BUILD_TIMEOUT_S = 180
_lock = threading.RLock()


class BlueprintError(Exception):
    def __init__(self, status: int, message: str, detail: dict | None = None):
        self.status, self.message, self.detail = status, message, detail or {}
        super().__init__(message)


def _dir(bid: str) -> Path:
    if not bid.startswith("bp_") or not bid[3:].isalnum():
        raise BlueprintError(404, f"No blueprint {bid}.")
    return STORE / bid


def _read_meta(bid: str) -> dict:
    p = _dir(bid) / "meta.json"
    if not p.exists():
        raise BlueprintError(404, f"No blueprint {bid}.")
    return json.loads(p.read_text(encoding="utf-8"))


def _write(bid: str, meta: dict, blueprint: dict | None = None) -> None:
    d = _dir(bid)
    d.mkdir(parents=True, exist_ok=True)
    if blueprint is not None:
        (d / "blueprint.json").write_text(json.dumps(blueprint, indent=2), encoding="utf-8")
    meta["updated_ms"] = int(time.time() * 1000)
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _blueprint(bid: str) -> dict:
    p = _dir(bid) / "blueprint.json"
    if not p.exists():
        raise BlueprintError(409, f"{bid} has no blueprint yet.")
    return json.loads(p.read_text(encoding="utf-8"))


def _check(blueprint: dict) -> list[str]:
    try:
        prepare(copy.deepcopy(blueprint), "blueprint")
        return []
    except ScenarioError as e:
        return e.problems


def _ingest(data: bytes, filename: str, content_type: str | None) -> dict:
    """Ingest through ingest/ when it's installed. Without it, take a JSON blueprint as is."""
    try:
        from ingest import IngestError, ingest_bytes
    except ImportError:
        try:
            bp = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BlueprintError(415, "Only JSON blueprints can be read until ingest/ is installed.")
        return {"blueprint": bp, "review": [], "errors": [], "source": {"file": filename, "read_by": "backend"}}
    try:
        return ingest_bytes(data, filename, content_type)
    except IngestError as e:
        raise BlueprintError(getattr(e, "status", 400), getattr(e, "message", str(e)))


def create(data: bytes, filename: str, content_type: str | None) -> dict:
    if not data:
        raise BlueprintError(400, "The upload is empty.")
    res = _ingest(data, filename, content_type)
    bp = res.get("blueprint")
    if not isinstance(bp, dict):
        raise BlueprintError(422, "Ingest returned no blueprint.")
    bid = "bp_" + uuid.uuid4().hex[:10]
    problems = _check(bp)
    errors = list(res.get("errors") or [])
    review = list(res.get("review") or [])
    status = "INVALID" if (errors or problems) else ("REVIEW" if review else "VALID")
    meta = {"id": bid, "status": status, "filename": filename, "source": res.get("source"),
            "review": review, "errors": errors, "problems": problems, "build_report": None,
            "created_ms": int(time.time() * 1000)}
    with _lock:
        _write(bid, meta, bp)
    return get(bid)


def get(bid: str) -> dict:
    meta = _read_meta(bid)
    try:
        meta["blueprint"] = _blueprint(bid)
    except BlueprintError:
        meta["blueprint"] = None
    meta["has_glb"] = (_dir(bid) / "field.glb").exists()
    snap = fields.snapshot()
    meta["active"] = snap.blueprint_id == bid
    return meta


def list_all() -> list[dict]:
    if not STORE.exists():
        return []
    out = []
    for d in sorted(STORE.iterdir()):
        if (d / "meta.json").exists():
            m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            out.append({k: m.get(k) for k in ("id", "status", "filename", "created_ms", "updated_ms")})
    return out


def update(bid: str, blueprint: dict, reviewed: bool = True) -> dict:
    """The human's edits after review. Validates again."""
    if not isinstance(blueprint, dict):
        raise BlueprintError(422, "The blueprint must be a JSON object.")
    with _lock:
        meta = _read_meta(bid)
        if meta["status"] == "BUILDING":
            raise BlueprintError(409, "Wait for the build to finish before editing.")
        problems = _check(blueprint)
        meta["problems"], meta["errors"] = problems, []
        if reviewed:
            meta["review"] = []
        meta["status"] = "INVALID" if problems else ("REVIEW" if meta["review"] else "VALID")
        meta["build_report"] = None
        _write(bid, meta, blueprint)
        glb = _dir(bid) / "field.glb"
        if glb.exists():
            glb.unlink()
    return get(bid)


def _blender() -> str | None:
    return os.environ.get("BLENDER_BIN") or shutil.which("blender")


def build(bid: str, wait: bool = False) -> dict:
    with _lock:
        meta = _read_meta(bid)
        if meta["status"] not in ("VALID", "READY", "BUILD_FAILED", "ACTIVE"):
            raise BlueprintError(409, f"{bid} is {meta['status']}. Only a valid blueprint can be built.")
        blender = _blender()
        if not GENERATOR.exists():
            raise BlueprintError(503, "The 3D generator (blender/build_field.py) isn't in this checkout yet.")
        if not blender:
            raise BlueprintError(503, "Blender isn't installed. Set BLENDER_BIN to the Blender executable.")
        meta["status_before_build"] = meta["status"]
        meta["status"] = "BUILDING"
        _write(bid, meta)
    t = threading.Thread(target=_run_build, args=(bid, blender), daemon=True)
    t.start()
    if wait:
        t.join(BUILD_TIMEOUT_S + 5)
    return get(bid)


def _run_build(bid: str, blender: str) -> None:
    d = _dir(bid)
    cmd = [blender, "--background", "--factory-startup", "--python", str(GENERATOR), "--",
           "--blueprint", str(d / "blueprint.json"), "--out", str(d)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
        code, tail = p.returncode, (p.stdout + p.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        code, tail = -1, f"Build timed out after {BUILD_TIMEOUT_S} s."
    except OSError as e:
        code, tail = -2, f"Couldn't start Blender: {e}"
    report = None
    rp = d / "build_report.json"
    if rp.exists():
        try:
            report = json.loads(rp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = None
    with _lock:
        meta = _read_meta(bid)
        ok = code == 0 and (d / "field.glb").exists() and (report or {}).get("ok", True)
        was_active = meta.get("status_before_build") == "ACTIVE"
        meta["status"] = ("ACTIVE" if was_active else "READY") if ok else "BUILD_FAILED"
        meta["build_report"] = report or {"ok": False, "errors": [tail.strip() or f"Exit code {code}."]}
        meta["build_exit_code"] = code
        _write(bid, meta)


def glb_path(bid: str) -> Path:
    p = _dir(bid) / "field.glb"
    if not p.exists():
        raise BlueprintError(404, f"{bid} has no built model yet. POST /api/blueprints/{bid}/build first.")
    return p


def activate(bid: str) -> dict:
    with _lock:
        meta = _read_meta(bid)
        if meta["status"] not in ("VALID", "READY", "ACTIVE"):
            raise BlueprintError(409, f"{bid} is {meta['status']}. Fix and confirm it before activating.",
                                 {"problems": meta.get("problems"), "review": meta.get("review")})
        try:
            scenario = prepare(copy.deepcopy(_blueprint(bid)), f"blueprint {bid}")
        except ScenarioError as e:
            raise BlueprintError(422, "The blueprint no longer passes validation.", {"problems": e.problems})
        # Only one blueprint is ACTIVE at a time.
        for other in list_all():
            if other["status"] == "ACTIVE" and other["id"] != bid:
                om = _read_meta(other["id"])
                om["status"] = "READY" if (_dir(other["id"]) / "field.glb").exists() else "VALID"
                _write(other["id"], om)
        fields.activate(scenario, blueprint_id=bid)
        meta["status"] = "ACTIVE"
        _write(bid, meta)
    return get(bid)
