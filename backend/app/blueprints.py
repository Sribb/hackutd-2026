"""Blueprints API (#41): upload a blueprint, validate it, build its field.glb, serve it, activate it.

    POST /api/blueprints                 {"blueprint": {...}, "activate"?: true}, a raw blueprint,
                                         or multipart with a .json in field "file"
    GET  /api/blueprints                 {"active", "blueprints": [record, ...]}
    GET  /api/blueprints/{id}            record (polled while status is "building")
    GET  /api/blueprints/{id}/field.glb  the built model, or main's blender/field.glb as the fallback
    POST /api/blueprints/{id}/activate   forecast, plans, field, and state use this blueprint
                                         ("default" goes back to the server's starting scenario)

A record is the shape the upload screen (web/src/upload.js, #43) reads:
    {id, status: building|ready|fallback|failed, errors, warnings, counts, glb_url, note, ...}

The demo never waits on a build. With no Blender, or a generator that can't take a
blueprint, or a build that fails or times out, the status is "fallback" and the glb URL
serves main's blender/field.glb. A valid upload is activated right away (the upload screen
reloads the console on /api/field as soon as the status is ready or fallback), unless the
request says "activate": false.

Stored under backend/data/blueprints/<id>/ (gitignored): blueprint.json, record.json, and
the generator's build/ output.

Activating a blueprint builds every connector it lists, and a network connector starts
its own thread and connects at once. So a blueprint with more than MAX_CONNECTORS
connectors, or a connector with more than MAX_CONNECTOR_SIGNALS signals or
MAX_CONNECTOR_COMMANDS commands, is refused before anything is built.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .auth import Caller, OPERATOR, require_role
from .forecast import forecast
from .scenario import BACKEND_DIR, REPO_DIR, TEAM_SCENARIO, load_scenario

log = logging.getLogger("freeboard.blueprints")
router = APIRouter()

DATA_DIR = Path(os.environ.get("FREEBOARD_BLUEPRINTS_DIR") or BACKEND_DIR / "data" / "blueprints")
GENERATOR = REPO_DIR / "blender" / "build_field.py"
FALLBACK_GLB = REPO_DIR / "blender" / "field.glb"
VALIDATOR_SCRIPT = REPO_DIR / "scenario" / "validate_blueprint.py"
SCHEMA_FILE = REPO_DIR / "docs" / "blueprint.schema.json"
MAC_BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"
BUILD_TIMEOUT_S = float(os.environ.get("FREEBOARD_BUILD_TIMEOUT", "90"))
MAX_BYTES = 5_000_000
KINDS = ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards")
ID_RE = re.compile(r"^bp_[0-9a-f]{12}$")
DEFAULT_ID = "default"
MAX_CONNECTORS = 16             # each opcua or sparkplug connector is a thread and a socket
MAX_CONNECTOR_SIGNALS = 500
MAX_CONNECTOR_COMMANDS = 200

_lock = threading.RLock()
_records: dict[str, dict] = {}
_active: dict = {"id": DEFAULT_ID}
_default: dict = {}
_use: Optional[Callable[[dict], None]] = None


def setup(default: dict, use: Callable[[dict], None]) -> None:
    """main.py hands over its starting scenario and a function that swaps the live one."""
    global _use
    _default["scenario"] = copy.deepcopy(default)
    _use = use
    _restore_active()


def _save_active(bid: str) -> None:
    """Remember the active blueprint, so a restart comes back on it (active.txt in DATA_DIR)."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "active.txt").write_text(bid, encoding="utf-8")
    except OSError:
        log.exception("couldn't save the active blueprint id")


def _restore_active() -> None:
    """On startup, go back to the blueprint that was active before the restart, if it still loads."""
    try:
        bid = (DATA_DIR / "active.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return
    if not ID_RE.match(bid) or not (DATA_DIR / bid / "blueprint.json").is_file():
        return
    try:
        _activate({"id": bid})
        log.info("restored active blueprint %s", bid)
    except Exception:  # noqa: BLE001 - a bad saved blueprint must not stop the server
        log.exception("couldn't restore blueprint %s; staying on the default", bid)


# ------------------------------------------------------------------ validation ----

_validator_cache: dict = {}


def _validator_module():
    if "mod" not in _validator_cache:
        mod = None
        if VALIDATOR_SCRIPT.exists():
            spec = importlib.util.spec_from_file_location("validate_blueprint", VALIDATOR_SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        _validator_cache["mod"] = mod
    return _validator_cache["mod"]


def _schema_validator():
    if "schema" not in _validator_cache:
        try:
            from jsonschema import Draft202012Validator
            _validator_cache["schema"] = Draft202012Validator(json.loads(SCHEMA_FILE.read_text(encoding="utf-8")))
        except ImportError:
            _validator_cache["schema"] = None
    return _validator_cache["schema"]


def check_blueprint(bp, path: Path) -> tuple[list[str], list[str], Optional[dict]]:
    """(errors, warnings, loaded scenario). Errors mean the blueprint can't run."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(bp, dict):
        return ["A blueprint must be a JSON object."], warnings, None
    errors += connector_limits(bp)
    if errors:
        return errors, warnings, None

    mod = _validator_module()
    if mod is None:
        warnings.append("scenario/validate_blueprint.py is missing, so the schema wasn't checked.")
    else:
        validator = _schema_validator()
        if validator is None:
            warnings.append("jsonschema isn't installed on the server, so only cross-references were checked.")
            errors += mod.cross_checks(bp)
        else:
            errors += mod.check(bp, validator)
    if errors:
        return errors, warnings, None

    try:
        scen = load_scenario(path)
    except Exception as e:  # noqa: BLE001 - any loader failure is the blueprint's problem
        return [f"The backend couldn't load it: {e}"], warnings, None
    try:
        from . import validate  # #29's checks, when that branch is in
        errors += validate.problems(scen)
    except ImportError:
        pass
    if errors:
        return errors, warnings, None
    try:  # a smoke run, so a blueprint that would crash /api/forecast is refused here
        forecast(scen, [], 30, 5, with_range=False)
    except Exception as e:  # noqa: BLE001
        return [f"The forecast couldn't run on it: {e}"], warnings, None
    return [], warnings, scen


def connector_limits(bp: dict) -> list[str]:
    """Errors for a blueprint whose connectors are over the limits above."""
    conns = bp.get("connectors")
    if conns is None:
        return []
    if not isinstance(conns, list):
        return ["connectors must be a list."]
    if len(conns) > MAX_CONNECTORS:
        return [f"The blueprint lists {len(conns)} connectors. The most it may list is {MAX_CONNECTORS}, "
                f"since each one connects as soon as the blueprint is activated."]
    errors = []
    for i, c in enumerate(conns):
        if not isinstance(c, dict):
            continue
        cid = c.get("id") if isinstance(c.get("id"), str) else f"connectors[{i}]"
        for key, cap in (("signals", MAX_CONNECTOR_SIGNALS), ("commands", MAX_CONNECTOR_COMMANDS)):
            items = c.get(key)
            if isinstance(items, list) and len(items) > cap:
                errors.append(f"{cid[:40]} lists {len(items)} {key}. The most one connector may list is {cap}.")
    return errors


def _counts(bp: dict) -> dict:
    return {k: len(bp.get(k) or []) for k in KINDS}


def _ids(bp: dict) -> set[str]:
    return {a["id"] for k in KINDS for a in (bp.get(k) or []) if isinstance(a, dict) and isinstance(a.get("id"), str)}


def _not_in_standard_model(bp: dict) -> list[str]:
    """IDs main's field.glb has no shape for (it is built from scenario/field.json)."""
    try:
        std = _ids(json.loads(TEAM_SCENARIO.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return []
    return sorted(_ids(bp) - std)


# ------------------------------------------------------------------ build ----

def find_blender() -> Optional[str]:
    env = os.environ.get("BLENDER")
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return env
    on_path = shutil.which("blender")
    if on_path:
        return on_path
    if Path(MAC_BLENDER).is_file():
        return MAC_BLENDER
    return None


def _generator_takes_blueprints() -> bool:
    """main's build_field.py only rebuilds the fixed field into blender/; never run that from here."""
    try:
        return "--blueprint" in Path(GENERATOR).read_text(encoding="utf-8")
    except OSError:
        return False


def _why_no_build() -> Optional[str]:
    if not Path(GENERATOR).exists():
        return "blender/build_field.py isn't on this server"
    if not _generator_takes_blueprints():
        return "this server's blender/build_field.py can't build from a blueprint yet (#33)"
    if not find_blender():
        return "Blender isn't installed on this server"
    return None


def _fallback_note(bp: dict, why: str) -> str:
    note = f"Showing the standard field model: {why}. The forecast and plans use your blueprint."
    extra = _not_in_standard_model(bp)
    if extra:
        shown = ", ".join(extra[:6]) + (f" and {len(extra) - 6} more" if len(extra) > 6 else "")
        note += f" {len(extra)} asset(s) have no 3D shape in it: {shown}."
    return note


def _run_build(bid: str, bp: dict) -> None:
    folder = DATA_DIR / bid
    out = folder / "build"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    blender = find_blender()
    cmd = [blender, "--background", "--factory-startup", "--python", str(GENERATOR), "--",
           "--blueprint", str(folder / "blueprint.json"), "--out", str(out)]
    t0 = time.time()
    why = None
    report: dict = {}
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
        (out / "blender.log").write_text((proc.stdout or "") + (proc.stderr or ""), encoding="utf-8")
        try:
            report = json.loads((out / "build_report.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            why = f"the build exited with code {proc.returncode} and wrote no build_report.json"
        if not why and not (report.get("ok") and (out / "field.glb").exists()):
            why = "the build failed"
    except subprocess.TimeoutExpired:
        why = f"the build took over {BUILD_TIMEOUT_S:.0f} s"
    except OSError as e:
        why = f"Blender couldn't start ({e})"

    secs = round(time.time() - t0, 1)
    with _lock:
        rec = _records.get(bid)
        if rec is None:
            return
        rec["build_seconds"] = secs
        rec["build_report"] = {k: report.get(k) for k in ("ok", "errors", "warnings", "counts", "placed") if k in report}
        rec["warnings"] = _merge(rec["warnings"], [f"Generator: {w}" for w in report.get("warnings") or []])
        if why is None:
            rec.update(status="ready", model="built", note=f"Built in {secs} s.")
            if isinstance(report.get("counts"), dict) and report["counts"]:
                rec["counts"] = report["counts"]
            if report.get("placed") is not None:
                rec["placed"] = report["placed"]
        else:
            errs = [str(e) for e in report.get("errors") or []]
            rec["warnings"] = _merge(rec["warnings"], [f"Generator error: {e}" for e in errs])
            rec.update(status="fallback", model="fallback", note=_fallback_note(bp, why))
        _save(rec)


def _merge(a: list, b: list) -> list:
    return a + [x for x in b if x not in a]


# ------------------------------------------------------------------ storage ----

def _public(rec: dict) -> dict:
    out = {k: v for k, v in rec.items() if not k.startswith("_")}
    out["active"] = _active["id"] == rec["id"]
    return out


def _save(rec: dict) -> None:
    folder = DATA_DIR / rec["id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "record.json").write_text(json.dumps(_public(rec), indent=2) + "\n", encoding="utf-8")


def _get(bid: str) -> dict:
    if not ID_RE.match(bid):
        raise HTTPException(404, f"No blueprint {bid}.")
    with _lock:
        rec = _records.get(bid)
        if rec is None:  # e.g. after a restart
            try:
                rec = json.loads((DATA_DIR / bid / "record.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise HTTPException(404, f"No blueprint {bid}.") from None
            if rec.get("status") == "building":  # the server stopped mid-build
                rec.update(status="fallback", model="fallback",
                           note="The server restarted during the build. Showing the standard field model.")
            rec.pop("active", None)
            _records[bid] = rec
        return rec


def _activate(rec: dict) -> None:
    path = DATA_DIR / rec["id"] / "blueprint.json"
    scen = load_scenario(path)
    with _lock:
        if _use:
            _use(scen)
        _active["id"] = rec["id"]
    _save_active(rec["id"])


# ------------------------------------------------------------------ routes ----

async def _read_upload(request: Request) -> tuple[object, bool, str]:
    """(blueprint, activate, source name) from a JSON body or a multipart "file"."""
    ctype = request.headers.get("content-type", "").lower()
    if not (ctype.startswith("application/json") or ctype.startswith("multipart/form-data")):
        # text/plain and form-urlencoded are what a page on another site can send without a
        # CORS preflight, so they never reach validation, let alone activation.
        raise HTTPException(415, "Send the blueprint as application/json, or as a .json file in multipart "
                                 "field \"file\". Drawings and CSV files go to /api/ingest first.")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(400, "Bad Content-Length header.") from None
    if declared > MAX_BYTES:
        raise HTTPException(413, "A blueprint must be under 5 MB.")
    # Enforce the cap while reading, so a body that lies about (or omits) its length
    # can't fill memory. Starlette's form() and json() then read the cached copy.
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MAX_BYTES:
            raise HTTPException(413, "A blueprint must be under 5 MB.")
    request._body = bytes(buf)  # noqa: SLF001 - Starlette's own cache for body()/json()/form()
    if ctype.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except AssertionError:  # python-multipart missing
            raise HTTPException(415, "File uploads need python-multipart on the server. Send JSON instead.") from None
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise HTTPException(422, 'Send the blueprint as a .json file in the form field "file".')
        name = getattr(f, "filename", "") or "upload"
        if not name.lower().endswith(".json"):
            raise HTTPException(415, f"{name} isn't a blueprint JSON. Drawings and CSV files go to /api/ingest first.")
        raw = await f.read()
        if len(raw) > MAX_BYTES:
            raise HTTPException(413, "A blueprint must be under 5 MB.")
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HTTPException(422, f"{name} isn't valid JSON: {e}") from None
        activate = str(form.get("activate", "true")).lower() not in ("false", "0", "no")
    else:
        try:
            body = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HTTPException(422, f"The body isn't valid JSON: {e}") from None
        name = "body"
        activate = True
    if isinstance(body, dict) and isinstance(body.get("blueprint"), dict):
        if body.get("activate") is False:
            activate = False
        body = body["blueprint"]
    return body, activate, name


@router.post("/api/blueprints")
async def post_blueprint(request: Request, caller: Caller = Depends(require_role(OPERATOR))):
    bp, activate, source = await _read_upload(request)
    text = json.dumps(bp, sort_keys=True, separators=(",", ":"))
    bid = "bp_" + hashlib.sha256(text.encode()).hexdigest()[:12]

    with _lock:
        rec = _records.get(bid)
        if rec is None:
            try:
                rec = _get(bid)
            except HTTPException:
                rec = None
        if rec is not None and rec["status"] in ("building", "ready"):
            if activate:  # same file again: no rebuild, just switch to it
                _activate(rec)
            return _public(rec)

    folder = DATA_DIR / bid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "blueprint.json").write_text(json.dumps(bp, indent=2) + "\n", encoding="utf-8")
    errors, warnings, _scen = check_blueprint(bp, folder / "blueprint.json")
    name = bp.get("name") if isinstance(bp, dict) else None
    rec = {
        "id": bid, "name": name, "source": source, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ok": not errors, "errors": errors, "warnings": warnings,
        "counts": _counts(bp) if isinstance(bp, dict) else {},
        "glb_url": None if errors else f"/api/blueprints/{bid}/field.glb",
        "model": None, "placed": None, "build_report": None, "build_seconds": None,
    }
    if errors:
        rec.update(status="failed", note=f"{len(errors)} problem(s) to fix before it can be built.")
    else:
        why = _why_no_build()
        if why:
            rec.update(status="fallback", model="fallback", note=_fallback_note(bp, why))
        else:
            rec.update(status="building", note="Building the field in Blender.")
    with _lock:
        _records[bid] = rec
        if not errors and activate:
            _activate(rec)
        _save(rec)
    if rec["status"] == "building":
        threading.Thread(target=_run_build, args=(bid, copy.deepcopy(bp)), daemon=True).start()

    out = _public(rec)
    if errors:
        detail = f"The blueprint has {len(errors)} problem(s): " + " ".join(errors[:5])
        return JSONResponse({**out, "detail": detail}, status_code=422)
    return out


@router.get("/api/blueprints")
def list_blueprints() -> dict:
    with _lock:
        recs = sorted(_records.values(), key=lambda r: r.get("created_at") or "", reverse=True)
        return {"active": _active["id"], "blueprints": [_public(r) for r in recs]}


@router.get("/api/blueprints/{bid}")
def get_blueprint(bid: str) -> dict:
    with _lock:
        return _public(_get(bid))


@router.get("/api/blueprints/{bid}/field.glb")
def get_glb(bid: str):
    rec = _get(bid)
    if rec["status"] == "failed":
        raise HTTPException(409, f"{bid} has problems, so it has no model. Fix them and upload again.")
    built = DATA_DIR / bid / "build" / "field.glb"
    if rec["status"] == "ready" and built.exists():
        path, model = built, "built"
    elif FALLBACK_GLB.exists():
        path, model = FALLBACK_GLB, "fallback"
    else:
        raise HTTPException(404, "No model: blender/field.glb isn't on this server.")
    return FileResponse(path, media_type="model/gltf-binary", filename="field.glb",
                        headers={"X-Freeboard-Model": model, "Cache-Control": "no-cache"})


def _audit_activation(request: Request, caller: Caller, bid: str) -> None:
    """One audit entry per activation (CF-10), next to the live layer's own entries."""
    audit = getattr(getattr(request.app, "state", None), "audit", None)
    if audit is None:
        return
    try:
        audit.append("blueprint_activated", source=caller.source(), role=caller.role, blueprint_id=bid)
    except Exception:  # noqa: BLE001 - the audit must never undo an activation that happened
        log.exception("audit entry for activating %s failed", bid)


@router.post("/api/blueprints/{bid}/activate")
def activate_blueprint(bid: str, request: Request, caller: Caller = Depends(require_role(OPERATOR))) -> dict:
    if bid == DEFAULT_ID:
        with _lock:
            if _use and "scenario" in _default:
                _use(copy.deepcopy(_default["scenario"]))
            _active["id"] = DEFAULT_ID
        _save_active(DEFAULT_ID)
        _audit_activation(request, caller, DEFAULT_ID)
        return {"id": DEFAULT_ID, "active": True, "status": "ready", "note": "Back on the default scenario."}
    rec = _get(bid)
    if rec["status"] == "failed":
        raise HTTPException(409, f"{bid} has problems, so it can't run. Fix them and upload again.")
    try:
        _activate(rec)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(409, f"{bid} couldn't be loaded: {e}") from None
    _audit_activation(request, caller, bid)
    with _lock:
        return _public(rec)
