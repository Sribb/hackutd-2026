"""/api/dataset: the backend of the /dataset page (web/src/dataset/data.js apiSource), and its 3D.

docs/CONTRACT.md, "Dataset mode (EOG sponsor data)" > "/api/dataset (the /dataset page)", has the
shapes. Every number here comes from service.SERVICE's cached detect and plan calls, the same
ones /api/eog/* serves, so the two never disagree: countdowns are the Event's countdown block
(plan.countdown), plans are plan.plan_failures (mode "now", joint per system), the order is
plan.alerts_order, and the do-nothing cost is the "none" option of plan.alternatives.

    GET  /api/dataset                    {folder, folders}
    POST /api/dataset/load {"folder"}    activate it (Service.activate) and pre-build its 3D
    GET  /api/dataset/file/{name}        network.json, ops.json or telemetry.csv; anything else 404
    GET  /api/dataset/alerts?at=&folder= {folder, at, now, alerts}: ongoing in SPEC order, then resolved
    GET  /api/dataset/events.json?at=    SPEC events.json, as a download
    GET  /api/dataset/field.glb?system=  ingest.eog_field.field_glb (Blender when installed, else the kit)
    GET  /api/dataset/field_ids?system=  the ids.json beside that glb
    GET  /api/eog/blueprint?system=      ingest.eog_network's blueprint (every system without ?system=)

?folder= picks a dataset for one request without changing the active one; ?at= (ts_unix or ISO
8601 UTC) cuts the data, clamped into its range, default the folder's last ts_unix. answer_key.json
is never opened. 3D builds run in a threadpool (one at a time, ingest.eog_field's lock), are cached
on disk by ingest.eog_field and here by folder, and every system of a dataset is built in a
background thread once it is activated. Nothing here talks to a running Blender (no socket).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from . import dataset as dsmod
from . import service
from .dataset import Dataset, DatasetError
from .model import Event, iso

log = logging.getLogger("freeboard.eog")

FILES = {"network.json": "application/json", "ops.json": "application/json", "telemetry.csv": "text/csv"}
ENV_ENGINE = "FREEBOARD_GLB_ENGINE"   # kit or blender; unset: Blender when it is installed, else the kit
CACHE_DIR: Optional[Path] = None      # None: ingest.eog_field's default (blender/out/eog); tests use a tmp dir
PREBUILD = True                       # build every system of a dataset in the background when it is activated

router = APIRouter(tags=["dataset page"])


def _svc() -> service.Service:
    return service.SERVICE


def _fail(e: service.ServiceError) -> HTTPException:
    return HTTPException(e.status, e.sentence)


# ---------- which folder, which moment ----------

def _active() -> tuple[str, Path]:
    try:
        return _svc()._ensure_active()
    except service.ServiceError as e:
        raise _fail(e) from None


def _folders() -> dict[str, Path]:
    _active()   # 503 with the sentence while dataset mode is off
    return {d["id"]: Path(d["path"]) for d in dsmod.discover()}


def _pick(folder: Optional[str]) -> tuple[str, Path]:
    """(id, path) of ?folder=, else the active dataset. 404 for a folder that isn't there."""
    active = _active()
    if folder is None or not str(folder).strip():
        return active
    folder = str(folder).strip()
    if folder == active[0]:
        return active
    found = _folders()
    if folder not in found:
        raise HTTPException(404, f"No dataset folder named {folder[:80]!r}; the known ones are {', '.join(found) or 'none'}.")
    return folder, found[folder]


def _load(ds_id: str, path: Path) -> Dataset:
    svc = _svc()
    try:
        if ds_id == svc._ensure_active()[0]:
            return svc.dataset()
        return svc._load(ds_id, path)
    except service.ServiceError as e:
        raise _fail(e) from None
    except (DatasetError, OSError) as e:
        raise HTTPException(503, f"The dataset {ds_id} can't be loaded ({service._sentence(e)}).") from None


def _cut(ds: Dataset, at_raw: Any) -> int:
    try:
        v = service.parse_ts(at_raw, "at")
    except service.ServiceError as e:
        raise _fail(e) from None
    return service.clamp_ts(ds.last if v is None else v, ds.start, ds.last)


# ---------- alerts and events.json, from the service's cached detect and plan ----------

def _none_only(p: Any) -> bool:
    acts = list(getattr(p, "actions", None) or [])
    return all(a.get("type") == "none" for a in acts)


def do_nothing_cost(ds: Dataset, view: Dataset, ev: Event, ongoing: list[Event], plans: dict) -> Optional[float]:
    """The cost of leaving an ongoing pump failure alone: the "none" option of plan.alternatives,
    with the trucks free that /api/eog/plans gives it (the joint plan's split)."""
    svc = _svc()
    chosen = plans.get(ev.id)
    if chosen is not None and _none_only(chosen):
        return round(float(chosen.cost), 2)
    free = svc.trucks_free(ds, ev, ongoing, plans)
    for p in svc.alternatives(view, ev, free):
        if _none_only(p):
            return round(float(p.cost), 2)
    return None


def _spec_entries(view: Dataset) -> tuple[list[Event], dict, dict]:
    """(events up to the view's now, the plans from now, the reference plans of resolved failures)."""
    svc = _svc()
    events = svc.events(view)
    now = svc.plans_now(view, events)
    ref = (svc.plans_reference(view, events)
           if any(e.category == "pump_failure" and e.status != "ongoing" for e in events) else {})
    return events, now, ref


def _spec(ev: Event, now: dict, ref: dict) -> dict:
    """SPEC events.json for one event, as /api/eog/events builds it: an ongoing pump failure carries
    the plan from now, a resolved one the plan made at its onset."""
    p = None
    if ev.category == "pump_failure":
        p = now.get(ev.id) if ev.status == "ongoing" else ref.get(ev.id)
    if p is None:
        return ev.to_events_json()
    return ev.to_events_json(actions=list(p.actions or []), est_cost=p.cost)


def alerts_payload(ds_id: str, ds: Dataset, at: int) -> dict:
    svc = _svc()
    view = ds.at(at)
    events, now, ref = _spec_entries(view)
    order, _ = svc.ordered(view)
    ongoing = svc.ongoing_failures(events)
    resolved = sorted((e for e in events if e.status != "ongoing"), key=lambda e: (e.onset, e.id))
    alerts = []
    for n, ev in enumerate(order + resolved, 1):
        d = _spec(ev, now, ref)
        d.setdefault("est_cost", None)   # every alert has every key; null when there is no plan
        pump = ev.category == "pump_failure"
        live = pump and ev.status == "ongoing"
        repair_end = svc.repair_end_of(ds, ev) if pump else None
        d.update({
            "order": n,
            "title": svc._title(ds, view, ev),
            "detected_at": ev.detected_at,
            "trips_at_unix": ev.trips_at if pump else None, "trips_at_ts": iso(ev.trips_at) if pump else None,
            "repair_end_unix": repair_end, "repair_end_ts": iso(repair_end),
            "tripped": bool(ev.tripped) if pump else False,
            "lost_revenue_per_hour": ev.lost_revenue_per_hour if pump else None,
            "do_nothing_cost": do_nothing_cost(ds, view, ev, ongoing, now) if live else None,
            "plan": svc.plan_action(now.get(ev.id)) if live else None,
            "approval": svc.approval_for(ds.id, ev),
        })
        alerts.append(d)
    return {"folder": ds_id, "dataset": ds_id, "at": at, "at_ts": iso(at), "now": view.now, "now_ts": iso(view.now),
            "last": ds.last, "count": len(alerts), "ongoing": len(order), "alerts": alerts}


def events_doc(ds: Dataset, at: int) -> list[dict]:
    view = ds.at(at)
    events, now, ref = _spec_entries(view)
    return [_spec(ev, now, ref) for ev in events]


# ---------- 3D: field.glb per system ----------

_glb: dict[tuple, Path] = {}
_glb_lock = threading.Lock()
_prebuilt: set[tuple] = set()


def engine() -> str:
    """blender when Blender is installed (ingest.eog_network.BLENDER_APP: $BLENDER or the Mac app), else kit."""
    want = os.environ.get(ENV_ENGINE, "").strip().lower()
    if want in ("kit", "blender"):
        return want
    from ingest.eog_network import BLENDER_APP
    return "blender" if Path(BLENDER_APP).is_file() else "kit"


def _systems(path: Path) -> list[str]:
    try:
        net = json.loads((path / "network.json").read_text(encoding="utf-8"))
        return [str(s["id"]) for s in net.get("systems", []) if isinstance(s, dict) and "id" in s]
    except (OSError, ValueError, TypeError) as e:
        raise HTTPException(503, f"network.json can't be read ({service._sentence(e)}).") from None


def _sig(path: Path) -> tuple:
    return tuple((n, (path / n).stat().st_mtime_ns, (path / n).stat().st_size) for n in ("network.json", "ops.json"))


def build_glb(path: Path, system: str) -> Path:
    """field.glb for one system: Blender when installed, the kit when Blender is missing or its
    build fails. Cached per folder; raises ingest's DatasetError when no engine builds it."""
    from ingest.eog_field import field_glb
    from ingest.eog_network import DatasetError as BuildError

    eng = engine()
    key = (str(path), system, eng, _sig(path))
    with _glb_lock:
        hit = _glb.get(key)
    if hit is not None and hit.is_file() and hit.with_name("ids.json").is_file():
        return hit
    try:
        glb = field_glb(path, system, cache_dir=CACHE_DIR, engine=eng)
    except BuildError as e:
        if eng != "blender":
            raise
        log.warning("Blender could not build %s of %s (%s); using the model kit", system, path, e)
        glb = field_glb(path, system, cache_dir=CACHE_DIR, engine="kit")
    with _glb_lock:
        _glb[key] = glb
    return glb


def _prebuild(path: Path, systems: list[str]) -> None:
    for s in systems:
        try:
            build_glb(path, s)
        except Exception as e:  # noqa: BLE001 - a bad system must not stop the others
            log.warning("3D pre-build of %s in %s failed: %s", s, path, e)


def prebuild(path: Path) -> Optional[threading.Thread]:
    """Build every system of a dataset in a background thread, once per folder and engine."""
    if not PREBUILD:
        return None
    try:
        key = (str(path), engine(), _sig(path))
        systems = _systems(path)
    except (HTTPException, OSError):
        return None
    with _glb_lock:
        if key in _prebuilt:
            return None
        _prebuilt.add(key)
    t = threading.Thread(target=_prebuild, args=(path, systems), name="dataset-3d-prebuild", daemon=True)
    t.start()
    return t


def _on_activate(ds_id: str, path: Any) -> None:
    """Service.activate's hook: whichever route switched the dataset, start building its 3D."""
    prebuild(Path(path))


if _on_activate.__name__ not in {getattr(f, "__name__", "") for f in service.ON_ACTIVATE}:
    service.ON_ACTIVATE.append(_on_activate)


def _system_glb(system: Optional[str]) -> Path:
    if not isinstance(system, str) or not system.strip():
        raise HTTPException(422, "system is required, for example ?system=<a system id from network.json>.")
    ds_id, path = _active()
    known = _systems(path)
    if system.strip() not in known:
        raise HTTPException(404, f"No system {system.strip()[:40]!r} in {ds_id}; it has {', '.join(known) or 'none'}.")
    from ingest.eog_network import DatasetError as BuildError
    try:
        return build_glb(path, system.strip())
    except BuildError as e:
        raise HTTPException(503, f"The 3D field for {system.strip()} did not build: {e}") from None
    finally:
        prebuild(path)   # e.g. a startup default nobody activated: its other systems next


# ---------- routes ----------

@router.get("/api/dataset")
def get_dataset() -> JSONResponse:
    ds_id, path = _active()
    prebuild(path)    # also covers a dataset activated through /api/eog, or the startup default
    return JSONResponse({"folder": ds_id, "folders": list(_folders())})


# POST /api/dataset/load is behind origin_guard (app-wide: same-site only) but takes no operator
# token: it only picks which recording is on screen, it sends no command, and the /dataset page
# posts it with plain fetch (web/src/dataset/main.js apiSource(API) has no authFetch).
@router.post("/api/dataset/load")
async def post_load(request: Request) -> JSONResponse:
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise HTTPException(422, "The request body must be a JSON object like {\"folder\": \"history/full_week\"}.") from None
    if not isinstance(body, dict) or not isinstance(body.get("folder"), str) or not body["folder"].strip():
        raise HTTPException(422, "folder must name a dataset, one of the folders GET /api/dataset lists.")
    try:
        await run_in_threadpool(_svc().activate, body["folder"].strip())
    except service.ServiceError as e:
        raise _fail(e) from None
    ds_id, _ = _active()   # Service.activate ran the ON_ACTIVATE hook: the 3D pre-build has started
    return JSONResponse({"folder": ds_id, "folders": list(_folders())})


@router.get("/api/dataset/file/{name}")
def get_file(name: str, folder: Optional[str] = None) -> Response:
    if name not in FILES:   # answer_key.json above all: the app never serves or opens it
        raise HTTPException(404, f"Only {', '.join(FILES)} are served from a dataset folder.")
    _, path = _pick(folder)
    f = path / name
    if not f.is_file():
        raise HTTPException(404, f"{name} is missing from the dataset folder.")
    return FileResponse(f, media_type=FILES[name], filename=None)


@router.get("/api/dataset/alerts")
def get_alerts(at: Optional[str] = None, folder: Optional[str] = None) -> JSONResponse:
    ds_id, path = _pick(folder)
    ds = _load(ds_id, path)
    try:
        payload = alerts_payload(ds_id, ds, _cut(ds, at))
    except service.ServiceError as e:
        raise _fail(e) from None
    return JSONResponse(service.jsonable(payload))


@router.get("/api/dataset/events.json")
def get_events_json(at: Optional[str] = None, folder: Optional[str] = None) -> Response:
    ds_id, path = _pick(folder)
    ds = _load(ds_id, path)
    try:
        doc = events_doc(ds, _cut(ds, at))
    except service.ServiceError as e:
        raise _fail(e) from None
    return Response(service.dumps(doc, indent=2) + "\n", media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="events.json"'})


@router.get("/api/dataset/field.glb")
async def get_field_glb(system: Optional[str] = None) -> FileResponse:
    glb = await run_in_threadpool(_system_glb, system)
    return FileResponse(glb, media_type="model/gltf-binary")


@router.get("/api/dataset/field_ids")
async def get_field_ids(system: Optional[str] = None) -> JSONResponse:
    glb = await run_in_threadpool(_system_glb, system)
    try:
        return JSONResponse(json.loads(glb.with_name("ids.json").read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        raise HTTPException(503, f"ids.json for that system can't be read ({service._sentence(e)}).") from None


@router.get("/api/eog/blueprint")
def get_blueprint(system: Optional[str] = None) -> JSONResponse:
    from ingest.eog_network import DatasetError as BuildError, load_folder, network_to_blueprints

    ds_id, path = _active()
    try:
        network, ops, _ = load_folder(path)
        bps = network_to_blueprints(network, ops)
    except BuildError as e:
        raise HTTPException(503, f"{ds_id} can't be turned into blueprints: {e}") from None
    if system is None or not system.strip():
        return JSONResponse({"dataset": ds_id, "systems": bps})
    if system.strip() not in bps:
        raise HTTPException(404, f"No system {system.strip()[:40]!r} in {ds_id}; it has {', '.join(bps) or 'none'}.")
    return JSONResponse(bps[system.strip()])
