"""Dataset mode service: the active dataset, the clock, a small cache and the approvals overlay.

api.py is a thin HTTP layer over this module, and everything here is plain Python, so it can be
tested without a server.

- **Active dataset.** FREEBOARD_EOG_DATASET (an id), else `test`, else `history/full_week`,
  else the first folder found under FREEBOARD_EOG_DIR. POST /api/eog/datasets/activate
  switches it. With FREEBOARD_EOG_DIR unset, dataset mode is off and every call raises
  `Unavailable` with one sentence saying how to turn it on.
- **Clock.** A view cursor `at` (time travel) and an autoplay cursor that moves N data-minutes
  per real second. Every read without its own `at` sees the data as of the cursor. Real time
  is used for one thing only, moving the autoplay cursor (time.monotonic). The data's "now"
  is always the last telemetry row at or before the cursor, never the system clock.
- **Cache.** Detection and plans are kept per (dataset, view) in a small LRU, so the console,
  the WebSocket and autoplay never redo the same work.
- **Approvals.** An overlay kept in memory: who approved which plan, at which data time. It
  never changes the data, the events or the plans.

detect, plan and blueprint are imported lazily inside functions, so this module runs before
they land. A view that needs one answers 503 with a sentence until it does, except the state
view: the map stays up on the readings alone, with a warning. Everything that reaches a
client goes through `jsonable` (no NaN, no Infinity).
"""
from __future__ import annotations

import dataclasses
import importlib
import json
import logging
import math
import os
import threading
import time
from array import array
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from . import dataset as dsmod
from .dataset import STEP_S, Dataset, DatasetError
from .model import ACTION_TYPES, Event, iso, money

log = logging.getLogger("freeboard.eog")

ENV_DATASET = "FREEBOARD_EOG_DATASET"
DEFAULT_IDS = ("test", "history/full_week")   # preferred defaults, in order (task rule)
OFF = ("Dataset mode is off: start the backend with FREEBOARD_EOG_DIR set to a folder of EOG "
       "datasets (for example a checkout of the sponsor repo) to turn it on.")
DEFAULT_SPEED = 5.0          # data-minutes per real second: one 5-minute row per second
MAX_SPEED = 1440.0           # a whole day per second
SHIFT_HOURS = 12.0           # "this shift" in the handoff
HANDOFF_FULL = 3             # pump failures the handoff writes out in full; the rest share one line
READ_WPM = 240               # an average adult reading speed, for the handoff's read_s
HANDOFF_WORDS = READ_WPM // 2   # 30 seconds of reading: lists past it end in "and N more"
APPROVAL_MATCH_S = 2 * 3600  # an approval follows its event if a later view moves the onset a little
STATUSES = ("OK", "INSPECT", "WARNING", "ALARM")   # least to most severe
SEVERITY = {s: i for i, s in enumerate(STATUSES)}
URGENT = ("pump_failure", "pipeline_leak")
WORDS = {"pump_failure": "failure", "pump_degradation": "worn pump",
         "pipeline_leak": "leak", "other": "problem"}
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
NO_DETECTION = "so these statuses come from the readings alone and leaks or worn pumps are not flagged"


# ---------- errors: each carries one plain-English sentence ----------

class ServiceError(Exception):
    status = 500

    def __init__(self, sentence: str):
        super().__init__(sentence)
        self.sentence = sentence


class Unavailable(ServiceError):
    """Dataset mode is off, or the active dataset (or a module it needs) can't be used.
    `missing` names the module (detect, plan, blueprint) when that is the reason."""
    status = 503

    def __init__(self, sentence: str, missing: Optional[str] = None):
        super().__init__(sentence)
        self.missing = missing


class NotFound(ServiceError):
    status = 404


class BadInput(ServiceError):
    status = 422


def _sentence(e: BaseException) -> str:
    return str(e).strip().rstrip(".")


# ---------- JSON safety ----------

def jsonable(x: Any) -> Any:
    """Plain JSON types only. NaN and Infinity become null, tuples and arrays become lists."""
    if x is None or isinstance(x, (bool, str, int)):
        return x
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, array)):
        return [jsonable(v) for v in x]
    if isinstance(x, (set, frozenset)):
        return [jsonable(v) for v in sorted(x, key=str)]
    to_dict = getattr(x, "to_dict", None)
    if callable(to_dict):
        return jsonable(to_dict())
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return jsonable(dataclasses.asdict(x))
    if isinstance(x, Path):
        return x.as_posix()
    try:
        return jsonable(float(x))
    except (TypeError, ValueError):
        return str(x)


def dumps(x: Any, indent: Optional[int] = None) -> str:
    """Strict JSON text (raises on NaN rather than writing it)."""
    return json.dumps(jsonable(x), allow_nan=False, ensure_ascii=False, indent=indent,
                      separators=None if indent else (",", ":"))


# ---------- input parsing: every failure is a BadInput sentence ----------

def parse_ts(raw: Any, name: str = "at") -> Optional[float]:
    """ts_unix seconds or ISO 8601 (UTC when no offset is given). None or blank means "not given".
    Values of 10^11 or more are read as milliseconds (what JavaScript's Date gives)."""
    if raw is None:
        return None
    bad = BadInput(f"{name} must be a ts_unix number or an ISO 8601 UTC time "
                   f"(YYYY-MM-DDTHH:MM:SSZ); got {str(raw)[:40]!r}.")
    if isinstance(raw, bool):
        raise bad
    if isinstance(raw, (int, float)):
        v = float(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            try:
                d = datetime.fromisoformat(s[:-1] + "Z" if s[-1:] == "z" else s)
            except ValueError:
                raise bad from None
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            v = d.timestamp()
    else:
        raise bad
    if not math.isfinite(v):
        raise bad
    if abs(v) >= 1e11:
        v /= 1000.0
    return v


def clamp_ts(v: float, lo: int, hi: int) -> int:
    return int(min(max(math.floor(v), lo), hi))


def parse_number(raw: Any, name: str, default: Optional[float], lo: float, hi: float,
                 integer: bool = False) -> Optional[float]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    kind = "a whole number" if integer else "a number"
    bad = BadInput(f"{name} must be {kind} from {lo:g} to {hi:g}; got {str(raw)[:40]!r}.")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise bad
    try:
        v = float(raw)
    except ValueError:
        raise bad from None
    if not math.isfinite(v) or v < lo or v > hi or (integer and v != int(v)):
        raise bad
    return int(v) if integer else v


TRUE, FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off"}


def parse_bool(raw: Any, name: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str) and raw.strip().lower() in TRUE | FALSE:
        return raw.strip().lower() in TRUE
    raise BadInput(f"{name} must be true or false; got {str(raw)[:40]!r}.")


# ---------- text helpers (UTC, the data's time zone) ----------

def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def clock_text(ts: Optional[float], ref: Optional[float] = None) -> str:
    """'13:25' on the same UTC day as ref, else 'Thu 13:25'."""
    if ts is None:
        return "unknown"
    d = _utc(ts)
    if ref is not None and _utc(ref).date() == d.date():
        return d.strftime("%H:%M")
    return f"{DAYS[d.weekday()]} {d.strftime('%H:%M')}"


def day_text(ts: float) -> str:
    d = _utc(ts)
    return f"{DAYS[d.weekday()]} {d.strftime('%Y-%m-%d %H:%M')}"


def duration_text(hours: Optional[float]) -> str:
    if hours is None or not math.isfinite(hours):
        return "unknown"
    mins = max(0, int(round(hours * 60)))
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def _plural(n: float, word: str) -> str:
    return f"{n:g} {word}" + ("" if n == 1 else "s")


def finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Field of a Plan dataclass or of a plan dict, so either shape works."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def p50(ev: Event) -> Optional[float]:
    h = ev.hours_to_limit
    if isinstance(h, dict):
        h = h.get("p50")
    try:
        h = float(h) if h is not None else None
    except (TypeError, ValueError):
        return None
    return h if h is not None and math.isfinite(h) else None


def describe_actions(actions: list[dict]) -> str:
    """Plain words for events.json actions, used when a plan carries no summary."""
    parts = []
    for a in actions or []:
        t = a.get("type")
        if t == "dispatch_truck":
            parts.append(f"{_plural(a.get('trucks', 1), 'truck')}, "
                         f"{_plural(a.get('trips_per_truck', 1), 'trip')} each")
        elif t == "shut_in_wells":
            m = a.get("start_after_min") or 0
            parts.append(f"shut in {', '.join(a.get('wells', []))}" + (f" at +{m:g} min" if m else " now"))
        elif t == "field_inspection":
            parts.append(f"inspect {a.get('asset_id')}")
    return "; ".join(parts) or "no action"


def touches(ds: Dataset, ev: Event) -> set[str]:
    """Assets an event touches: its asset and facility, plus the tank and wells a failed pump puts at risk."""
    out = {ev.asset, ev.facility}
    fac = ds.facilities.get(ev.facility)
    if fac is not None and ev.category == "pump_failure":
        out.add(fac.tank_id)
        out.update(fac.well_ids)
    return {a for a in out if a}


def downsample(points: list[tuple[int, float]], max_points: int) -> list[tuple[int, float]]:
    """At most max_points points: split into max_points // 2 buckets and keep each bucket's
    lowest and highest reading, in time order, so spikes and dips survive."""
    n = len(points)
    if n <= max_points:
        return list(points)
    buckets = max(1, max_points // 2)
    out: list[tuple[int, float]] = []
    for b in range(buckets):
        seg = points[b * n // buckets:(b + 1) * n // buckets]
        if not seg:
            continue
        lo = min(range(len(seg)), key=lambda k: seg[k][1])
        hi = max(range(len(seg)), key=lambda k: seg[k][1])
        out.extend(seg[k] for k in sorted({lo, hi}))
    return out


def _module(name: str):
    """detect, plan or blueprint, imported on first use (they land from other branches)."""
    full = f"{__package__}.{name}"
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as e:
        if e.name != full:
            raise          # the module exists but one of its own imports is missing: a real bug
        raise Unavailable(f"backend/app/eog/{name}.py is not on this branch yet, so this view "
                          f"can't be built until the {name} work is merged.", missing=name) from None


def step_of(times: array) -> int:
    """The telemetry's row spacing in seconds: the most common gap between its timestamps
    (SPEC: every 5 minutes), so even that is read from the file, not assumed."""
    gaps = Counter(int(b) - int(a) for a, b in zip(times, times[1:]) if b > a)
    return gaps.most_common(1)[0][0] if gaps else STEP_S


def _listing(items: list[str], cap: int) -> str:
    """'a, b, c and 2 more': at most `cap` items by name."""
    shown, more = items[:cap], len(items) - min(len(items), cap)
    return ", ".join(shown) + (f" and {more} more" if more else "")


def _words(lines: list[str]) -> int:
    return sum(len(line.split()) for line in lines)


def _fit(prefix: str, items: list[str], budget: int) -> str:
    """'<prefix>a, b and 3 more.' with as many items as fit in `budget` words, and always at
    least the first, so nothing on the list goes unmentioned."""
    for k in range(len(items), 0, -1):
        line = prefix + _listing(items, k) + "."
        if k == 1 or len(line.split()) <= budget:
            return line
    return prefix.rstrip(": ") + "."


# ---------- cache ----------

class LRU:
    """A small thread-safe LRU. Concurrent misses on one key compute it once; the rest wait."""

    def __init__(self, maxsize: int = 64):
        self.maxsize = maxsize
        self._data: OrderedDict = OrderedDict()   # key -> (keep_alive, value)
        self._busy: dict = {}                     # key -> threading.Event while computing
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def __len__(self) -> int:
        return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = self.misses = 0

    def get(self, key: tuple, compute: Callable[[], Any], keep: Any = None) -> Any:
        """The cached value, computing it on a miss. `keep` is held with the entry; the keys
        include id(ds.times), so holding the array keeps that id from being reused."""
        while True:
            with self._lock:
                if key in self._data:
                    self._data.move_to_end(key)
                    self.hits += 1
                    return self._data[key][1]
                waiting = self._busy.get(key)
                if waiting is None:
                    mine = self._busy[key] = threading.Event()
            if waiting is not None:
                waiting.wait()
                continue      # cached now, or the computing thread failed and this one retries
            try:
                value = compute()
                with self._lock:
                    self.misses += 1
                    self._data[key] = (keep, value)
                    self._data.move_to_end(key)
                    while len(self._data) > self.maxsize:
                        self._data.popitem(last=False)
                return value
            finally:
                with self._lock:
                    self._busy.pop(key, None)
                mine.set()


# ---------- clock ----------

class Clock:
    """The view cursor and the autoplay cursor, in ts_unix seconds.

    `real` returns real seconds (time.monotonic by default) and is used only to move the
    autoplay cursor. The cursor is always clamped into [start, last]; autoplay pauses by
    itself at `last`. Callers hold the service lock."""

    def __init__(self, real: Callable[[], float], speed: float = DEFAULT_SPEED):
        self._real = real
        self.start: Optional[int] = None
        self.last: Optional[int] = None
        self.speed = speed
        self.playing = False
        self._at: Optional[float] = None
        self._anchor_at = 0.0
        self._anchor_real = 0.0

    def bind(self, start: int, last: int) -> None:
        """Set the dataset range. A cursor at the old end follows the new end."""
        if (start, last) == (self.start, self.last):
            return
        follow = self._at is None or self.last is None or self.position() >= self.last
        self.start, self.last = start, last
        pos = float(last) if follow else min(max(self.position(), start), last)
        self._at = pos
        if self.playing:
            self._anchor_at, self._anchor_real = pos, self._real()

    def position(self) -> float:
        if self._at is None:
            return float(self.last if self.last is not None else 0)
        if self.playing:
            pos = self._anchor_at + (self._real() - self._anchor_real) * self.speed * 60.0
            if pos >= self.last:
                self._at, self.playing = float(self.last), False   # reached the end: pause there
                return self._at
            return pos
        return self._at

    def set(self, at: Optional[int] = None, play: Optional[bool] = None,
            speed: Optional[float] = None) -> None:
        pos = self.position()               # freeze where autoplay has got to
        if at is not None:
            pos = float(at)
        if speed is not None:
            self.speed = float(speed)
        if play is not None:
            self.playing = bool(play)
        self._at = pos
        if self.playing:
            if pos >= self.last:
                self.playing = False        # nothing left to play
            else:
                self._anchor_at, self._anchor_real = pos, self._real()


# ---------- the service ----------

class Service:
    def __init__(self, cache_size: int = 64):
        self._lock = threading.RLock()
        # One parse at a time: concurrent first requests would otherwise each parse the same
        # telemetry.csv (tens of MB) under the GIL. Later loads are a stat and a cache hit.
        self._load_lock = threading.Lock()
        self.real: Callable[[], float] = time.monotonic   # tests swap this for a fake clock
        self.cache = LRU(cache_size)
        self.poll_s = 0.25            # how often the WebSocket checks the cursor
        self.reset()

    def _log_once(self, what: str, e: BaseException) -> None:
        """Log a failure with its traceback the first time it happens, not on every poll."""
        key = (what, type(e).__name__, str(e))
        with self._lock:
            if key in self._logged:
                return
            if len(self._logged) > 200:
                self._logged.clear()
            self._logged.add(key)
        log.error("Dataset mode: %s failed: %s: %s", what, type(e).__name__, e, exc_info=e)

    def _load(self, ds_id: str, path: Path) -> Dataset:
        with self._load_lock:
            return dsmod.load(path, ds_id)

    def reset(self) -> None:
        """Forget the active dataset, the clock, the cache and the approvals."""
        with self._lock:
            self._root: Optional[Path] = None
            self._active: Optional[str] = None
            self._path: Optional[Path] = None
            self._warnings: list[str] = []
            self.clock = Clock(lambda: self.real())
            self._approvals: dict[tuple, dict] = {}
            self._seq = 0
            self._logged: set = set()
            self.version = 0
            self.cache.clear()

    # ----- dataset selection -----
    def _root_or_off(self) -> Path:
        raw = os.environ.get(dsmod.ENV_ROOT, "").strip()
        if not raw:
            raise Unavailable(OFF)
        root = dsmod.root()
        if root is None:
            raise Unavailable(f"Dataset mode is off because FREEBOARD_EOG_DIR ({raw}) is not a folder; "
                              f"point it at a folder of EOG datasets and restart the backend.")
        return root.resolve()

    @staticmethod
    def _pick_default(ids: list[str], wanted: str) -> tuple[str, list[str]]:
        warnings = []
        if wanted:
            for i in ids:
                if i == wanted:
                    return i, warnings
            for i in ids:
                if i.endswith("/" + wanted.strip("/")):
                    return i, warnings
            warnings.append(f"{ENV_DATASET}={wanted} names no dataset here, so the default is used.")
        for pref in DEFAULT_IDS:   # exact id first, then a folder of that name deeper down
            for i in ids:
                if i == pref:
                    return i, warnings
            for i in ids:
                if i.endswith("/" + pref):
                    return i, warnings
        return ids[0], warnings

    def _ensure_active(self) -> tuple[str, Path]:
        root = self._root_or_off()
        with self._lock:
            if self._root != root or self._active is None:
                found = dsmod.discover(root)
                if not found:
                    raise Unavailable(f"Dataset mode has nothing to show: no folder under {root} holds "
                                      f"network.json, ops.json and telemetry.csv.")
                ids = [d["id"] for d in found]
                pick, warnings = self._pick_default(ids, os.environ.get(ENV_DATASET, "").strip())
                speed = self.clock.speed
                self._root, self._active = root, pick
                self._path = Path(next(d["path"] for d in found if d["id"] == pick))
                self._warnings = warnings
                self.clock = Clock(lambda: self.real(), speed)
                self.version += 1
            return self._active, self._path

    def require_enabled(self) -> None:
        """Raise Unavailable (503) unless dataset mode is on and has a dataset to show."""
        self._ensure_active()

    def active_id(self) -> Optional[str]:
        """The id of the dataset in use, or None while there is none: dataset mode is off, or no
        dataset has been picked yet (any /api/eog view or an activation picks one). Never
        discovers or loads anything, so the legacy routes (legacy.py) can ask on every request."""
        root = dsmod.root()
        with self._lock:
            if self._active is None or root is None or self._root != root.resolve():
                return None
            return self._active

    def dataset(self) -> Dataset:
        """The active dataset. Parsing is cached by dataset.load until one of its files changes."""
        ds_id, path = self._ensure_active()
        try:
            ds = self._load(ds_id, path)
        except (DatasetError, OSError) as e:
            raise Unavailable(f"The dataset {ds_id} can't be loaded ({_sentence(e)}); activate another "
                              f"with POST /api/eog/datasets/activate.") from None
        with self._lock:
            self.clock.bind(ds.start, ds.last)
        return ds

    def view(self, at_raw: Any = None) -> tuple[Dataset, Dataset, int]:
        """(dataset, its view as of `at` or the cursor, the clamped `at`)."""
        ds = self.dataset()
        v = parse_ts(at_raw, "at")
        if v is None:
            with self._lock:
                v = self.clock.position()
        at = clamp_ts(v, ds.start, ds.last)
        return ds, ds.at(at), at

    @staticmethod
    def header(view: Dataset, at: int) -> dict:
        return {"dataset": view.id, "at": at, "at_ts": iso(at), "now": view.now, "now_ts": iso(view.now)}

    # ----- /datasets -----
    def datasets_payload(self) -> dict:
        ds_id, _ = self._ensure_active()
        out = {"enabled": True, "active": ds_id,
               "datasets": [{"id": d["id"]} for d in dsmod.discover(self._root)],
               "clock": None, "warnings": list(self._warnings)}
        try:
            out["clock"] = self.clock_payload(self.dataset())
        except Unavailable as e:
            out["error"] = e.sentence
        return out

    def activate(self, ds_id: Any) -> dict:
        if not isinstance(ds_id, str) or not ds_id.strip():
            raise BadInput("id must name a dataset, one of the ids GET /api/eog/datasets lists.")
        ds_id = ds_id.strip()
        self._ensure_active()
        found = {d["id"]: Path(d["path"]) for d in dsmod.discover(self._root)}
        if ds_id not in found:
            raise NotFound(f"No dataset named {ds_id!r}; the known ones are {', '.join(found) or 'none'}.")
        try:
            ds = self._load(ds_id, found[ds_id])
        except (DatasetError, OSError) as e:
            raise BadInput(f"The dataset {ds_id} can't be loaded ({_sentence(e)}), so it was not activated.") from None
        with self._lock:
            speed = self.clock.speed
            self._active, self._path, self._warnings = ds_id, found[ds_id], []
            self.clock = Clock(lambda: self.real(), speed)
            self.clock.bind(ds.start, ds.last)
            self.version += 1
        return self.datasets_payload()

    # ----- /clock -----
    def clock_payload(self, ds: Optional[Dataset] = None) -> dict:
        ds = ds or self.dataset()
        with self._lock:
            self.clock.bind(ds.start, ds.last)
            pos = self.clock.position()
            playing, speed = self.clock.playing, self.clock.speed
        at = clamp_ts(pos, ds.start, ds.last)
        now = ds.at(at).now
        step = self.cache.get(("step", ds.id, str(ds.folder), id(ds.times)), lambda: step_of(ds.times),
                              keep=ds.times)
        return {"dataset": ds.id, "start": ds.start, "start_ts": iso(ds.start),
                "now": now, "now_ts": iso(now), "last": ds.last, "last_ts": iso(ds.last),
                "step_s": step, "at": at, "at_ts": iso(at), "playing": playing,
                "speed": speed, "live": at >= ds.last}

    def set_clock(self, body: dict) -> dict:
        ds = self.dataset()
        at = parse_ts(body.get("at"), "at")
        play = None if body.get("play") is None else parse_bool(body["play"], "play")
        speed = None
        if body.get("speed") is not None:
            speed = parse_number(body["speed"], "speed", None, 0.0, MAX_SPEED)
            if speed <= 0:
                raise BadInput(f"speed must be above 0 and at most {MAX_SPEED:g} data-minutes per second; got {body['speed']!r}.")
        with self._lock:
            self.clock.bind(ds.start, ds.last)
            self.clock.set(at=None if at is None else clamp_ts(at, ds.start, ds.last), play=play, speed=speed)
            self.version += 1
        out = self.clock_payload(ds)
        if play and not out["playing"]:
            out["note"] = "The cursor is at the end of the data, so there is nothing to play; send an earlier at with play."
        return out

    # ----- cached detection and planning -----
    @staticmethod
    def _key(view: Dataset, *parts: Any) -> tuple:
        return (view.id, str(view.folder), id(view.times), view.end) + parts

    def events(self, view: Dataset) -> list[Event]:
        """Every event up to the view's now, with countdowns on ongoing pump failures."""
        def compute() -> list[Event]:
            detect = _module("detect")
            evs = list(detect.detect(view))
            for i, ev in enumerate(evs):
                if ev.category == "pump_failure" and ev.status == "ongoing":
                    out = detect.countdown(view, ev)
                    if isinstance(out, Event):
                        evs[i] = out
            return evs
        return self.cache.get(self._key(view, "events"), compute, keep=view.times)

    @staticmethod
    def ongoing_failures(events: list[Event]) -> list[Event]:
        return [e for e in events if e.category == "pump_failure" and e.status == "ongoing"]

    def plans_now(self, view: Dataset, events: list[Event]) -> dict:
        """Plans from the view's now for its ongoing pump failures, joint per system."""
        ongoing = self.ongoing_failures(events)
        if not ongoing:
            return {}
        return self.cache.get(self._key(view, "plans", "now"),
                              lambda: dict(_module("plan").plan_failures(view, ongoing, mode="now")),
                              keep=view.times)

    def plans_reference(self, view: Dataset, events: list[Event]) -> dict:
        """History mode: every pump failure up to the view's now, planned at its onset."""
        failures = [e for e in events if e.category == "pump_failure"]
        if not failures:
            return {}
        return self.cache.get(self._key(view, "plans", "reference"),
                              lambda: dict(_module("plan").plan_failures(view, failures, mode="reference")),
                              keep=view.times)

    def alternatives(self, view: Dataset, ev: Event, trucks_free: Optional[int]) -> list:
        return self.cache.get(self._key(view, "alternatives", ev.id, trucks_free),
                              lambda: list(_module("plan").alternatives(view, ev, start_ts=None,
                                                                        trucks_free=trucks_free)),
                              keep=view.times)

    def wait_curve(self, view: Dataset, ongoing: list[Event], ev: Event) -> list:
        """Last Call's price of waiting, from plan.py. Each point's start goes out as the API's
        usual pair, `start` (ts_unix) and `start_ts` (ISO): plan.py names the number start_ts."""
        def compute() -> list:
            out = []
            for p in _module("plan").wait_curve(view, ongoing, ev.id, step_min=5, until=None):
                d = dict(p) if isinstance(p, dict) else jsonable(p)
                s = d.get("start", d.get("start_ts")) if isinstance(d, dict) else None
                if finite(s):
                    d["start"], d["start_ts"] = int(s), iso(int(s))
                out.append(d)
            return out
        return self.cache.get(self._key(view, "wait_curve", ev.id), compute, keep=view.times)

    def ordered(self, view: Dataset) -> tuple[list[Event], dict]:
        """(ongoing events in the alerts-view order, the now plans)."""
        events = self.events(view)
        plans = self.plans_now(view, events)
        ongoing = [e for e in events if e.status == "ongoing"]
        if not ongoing:
            return [], plans
        order = self.cache.get(self._key(view, "alerts"),
                               lambda: list(_module("plan").alerts_order(ongoing, plans)),
                               keep=view.times)
        return order, plans

    # ----- approvals overlay -----
    def approval_for(self, ds_id: str, ev: Event) -> Optional[dict]:
        """The approval recorded for this event (same dataset, category and asset, onset within
        two hours, so a later view that re-dates the onset a little still shows it)."""
        best, gap = None, None
        with self._lock:
            for r in self._approvals.get((ds_id, ev.category, ev.asset), []):
                g = abs(int(r["onset"]) - int(ev.onset))
                if g <= APPROVAL_MATCH_S and (gap is None or g < gap or (g == gap and r["seq"] > best["seq"])):
                    best, gap = r, g
        return dict(best) if best else None

    def _record(self, ds_id: str, ev: Event, record: dict) -> dict:
        with self._lock:
            self._seq += 1
            record = dict(record, seq=self._seq)
            records = self._approvals.setdefault((ds_id, ev.category, ev.asset), [])
            records[:] = [r for r in records if r["onset"] != ev.onset] + [record]
            self.version += 1
        return dict(record)

    # ----- views -----
    def network(self, at_raw: Any = None) -> dict:
        ds, view, at = self.view(at_raw)
        body = self.cache.get(("network", ds.id, str(ds.folder), id(ds.times)),
                              lambda: self._network(ds), keep=ds.times)
        return {**self.header(view, at), **body}

    def _network(self, ds: Dataset) -> dict:
        systems, facilities, wells, links = [], [], [], []
        for s in ds.systems.values():
            systems.append({
                "id": s.id, "center": list(s.center), "truck_yard": list(s.truck_yard),
                "disposal": {"id": s.disposal_id, "lat": s.disposal_lat, "lon": s.disposal_lon,
                             "pressure_psi": s.disposal_pressure_psi},
                "fleet_size": s.fleet_size, "facility_ids": list(s.facility_ids)})
        for f in ds.facilities.values():
            disposal = ds.systems[f.system].disposal_id
            facilities.append({
                "id": f.id, "system": f.system, "lat": f.lat, "lon": f.lon,
                "tank": {"id": f.tank_id, "capacity_bbl": f.capacity_bbl,
                         "warning_pct": ds.warning_pct, "shutdown_pct": ds.shutdown_pct,
                         "warning_bbl": f.capacity_bbl * ds.warning_pct / 100.0,
                         "shutdown_bbl": f.capacity_bbl * ds.shutdown_pct / 100.0},
                "pump": {"id": f.pump_id, "max_flow_bpd": f.max_flow_bpd,
                         "start_level_pct": f.start_level_pct, "stop_level_pct": f.stop_level_pct},
                "pipeline": {"id": f.pipeline_id, "max_bpd": f.pipeline_max_bpd,
                             "max_pressure_psi": f.pipeline_max_pressure_psi, "disposal_id": disposal},
                "response_min": f.response_min, "cycle_min": f.cycle_min,
                "well_ids": f.well_ids, "baseline_water_bpd": f.baseline_water_bpd,
                "revenue_per_hour": ds.facility_revenue_per_hour(f.id)})
            for w in f.wells:
                wells.append({
                    "id": w.id, "facility": f.id, "system": f.system,
                    "baseline_water_bpd": w.baseline_water_bpd, "oil_bpd": w.oil_bpd, "gas_mcfd": w.gas_mcfd,
                    "revenue_per_day": ds.revenue_per_day(w.id), "revenue_per_hour": ds.revenue_per_hour(w.id),
                    "cost_per_bbl_avoided": ds.cost_per_bbl_avoided(w.id)})
                links.append({"from": w.id, "to": f.tank_id, "kind": "water"})
            links += [{"from": f.tank_id, "to": f.pump_id, "kind": "tank_outlet"},
                      {"from": f.pump_id, "to": f.pipeline_id, "kind": "discharge"},
                      {"from": f.pipeline_id, "to": disposal, "kind": "pipeline"}]
        layout, warnings = None, []
        try:
            layout = _module("blueprint").field_assets(ds)
        except Unavailable:
            pass                          # blueprint.py not merged yet: no layout, no warning
        except Exception as e:            # noqa: BLE001 - a layout bug must not take the map down
            self._log_once("field layout", e)
            warnings.append(f"The field layout could not be built ({type(e).__name__}: {_sentence(e)}).")
        return {"limits": {"warning_pct": ds.warning_pct, "shutdown_pct": ds.shutdown_pct},
                "prices": {"oil_per_bbl": ds.oil_price, "gas_per_mcf": ds.gas_price},
                "repair_hours": ds.repair_hours,
                "trucking": {"truck_capacity_bbl": ds.truck_capacity_bbl, "cost_per_trip": ds.truck_cost},
                "systems": systems, "facilities": facilities, "wells": wells, "links": links,
                "layout": layout, "warnings": warnings}

    def state(self, at_raw: Any = None) -> dict:
        """Every asset's readings at `at` and its status. The map must stay up even when
        detection is missing or broken: then the statuses come from the readings alone (tank
        limits, pump faults, shut-in wells) and `warnings` says so."""
        ds, view, at = self.view(at_raw)
        warnings: list[str] = []
        try:
            events = self.events(view)
        except Unavailable as e:
            events = []
            warnings.append(f"backend/app/eog/{e.missing}.py is not on this branch yet, {NO_DETECTION}."
                            if e.missing else f"Detection is unavailable ({_sentence(e)}), {NO_DETECTION}.")
        except Exception as e:  # noqa: BLE001 - a detection bug must not take the map down
            self._log_once("detection", e)
            events = []
            warnings.append(f"Detection failed ({type(e).__name__}: {_sentence(e)}), {NO_DETECTION}.")
        ongoing = [e for e in events if e.status == "ongoing"]
        touching: dict[str, list[str]] = {}
        for e in ongoing:
            for a in sorted(touches(ds, e)):
                touching.setdefault(a, []).append(e.id)
        W, S = ds.warning_pct, ds.shutdown_pct
        assets: dict[str, dict] = {}

        def put(asset_id: str, kind: str, status: str, reasons: list[str], system: str,
                facility: Optional[str]) -> dict:
            a = {"kind": kind, "system": system, "facility": facility, "status": status,
                 "readings": {m: view.value_at(asset_id, m) for m in ds.metrics(asset_id)},
                 "events": touching.get(asset_id, []), "reasons": reasons}
            assets[asset_id] = a
            return a

        for f in ds.facilities.values():
            mine = [e for e in ongoing if e.facility == f.id]
            failed = [e for e in mine if e.category == "pump_failure"]
            worn = [e for e in mine if e.category == "pump_degradation"]
            leaks = [e for e in mine if e.category == "pipeline_leak"]
            pump_status = view.value_at(f.pump_id, "status")
            fault = pump_status == 2                             # SPEC: pump status 2 is a fault
            parts = []
            # pump (missing data is never called OK)
            if failed or fault:
                st, why = "ALARM", ["Pump failure: the pump has stopped moving water." if failed
                                    else "The pump reports a fault (status 2)."]
            elif worn:
                st, why = "INSPECT", ["The pump is wearing out and moves less water: send an inspector."]
            elif pump_status is None:
                st, why = "WARNING", ["No pump status reading at this time, so the pump can't be called healthy."]
            else:
                st, why = "OK", []
            parts.append(put(f.pump_id, "pump", st, why, f.system, f.id))
            # tank
            level = view.value_at(f.tank_id, "level_pct")
            if level is not None and level >= S:
                st, why = "ALARM", [f"Level {level:.1f}% is at the {S:g}% shutdown limit."]
            elif failed or fault:
                st, why = "ALARM", ["Its pump has failed, so the tank is filling."
                                    + (" There is no level reading at this time." if level is None else "")]
            elif level is None:
                st, why = "WARNING", ["No level reading at this time, so the tank can't be called safe."]
            elif level >= W:
                st, why = "WARNING", [f"Level {level:.1f}% is above the {W:g}% warning limit."]
            else:
                st, why = "OK", []
            parts.append(put(f.tank_id, "tank", st, why, f.system, f.id))
            # pipeline
            if leaks:
                st, why = "INSPECT", ["Possible leak between the pump and the disposal site: send an inspector now."]
            elif ds.metrics(f.pipeline_id) and all(view.value_at(f.pipeline_id, m) is None
                                                   for m in ds.metrics(f.pipeline_id)):
                st, why = "WARNING", ["No pipeline readings at this time, so a leak can't be ruled out."]
            else:
                st, why = "OK", []
            parts.append(put(f.pipeline_id, "pipeline", st, why, f.system, f.id))
            # wells
            for w in f.wells:
                ws = view.value_at(w.id, "status")
                if ws is None:
                    st, why = "WARNING", ["No status reading at this time."]
                elif ws == 0:
                    st, why = "ALARM", ["Shut in: producing nothing."]
                else:
                    st, why = "OK", []
                parts.append(put(w.id, "well", st, why, f.system, f.id))
            worst = max(parts, key=lambda a: SEVERITY[a["status"]])
            reasons = [r for a in parts if a["status"] == worst["status"] for r in a["reasons"]][:3]
            put(f.id, "facility", worst["status"], reasons, f.system, f.id)
        for s in ds.systems.values():
            put(s.disposal_id, "disposal", "OK", [], s.id, None)
        systems = {}
        for s in ds.systems.values():
            systems[s.id] = max((assets[f]["status"] for f in s.facility_ids), key=SEVERITY.get, default="OK")
        counts = {k: 0 for k in STATUSES}
        for a in assets.values():
            counts[a["status"]] += 1
        return {**self.header(view, at),
                "limits": {"warning_pct": W, "shutdown_pct": S},
                "assets": assets, "systems": systems, "counts": counts,
                "events": {e.id: {"category": e.category, "system": e.system, "facility": e.facility,
                                  "asset": e.asset, "onset": e.onset, "onset_ts": iso(e.onset)}
                           for e in ongoing},
                "warnings": warnings}

    def _title(self, ds: Dataset, view: Dataset, ev: Event) -> str:
        fac = ds.facilities.get(ev.facility)
        tank = fac.tank_id if fac else "the tank"
        if ev.category == "pump_failure":
            if ev.tripped:
                return f"{ev.asset} failed: {tank} is at shutdown and its wells are shut in"
            if ev.trips_at is not None:
                h = p50(ev)
                h = h if h is not None else max(0.0, (ev.trips_at - view.now) / 3600.0)
                return f"{ev.asset} failed: {tank} reaches shutdown at {clock_text(ev.trips_at, view.now)} (in {duration_text(h)})"
            return f"{ev.asset} failed: {tank} stays under shutdown until the repair"
        if ev.category == "pipeline_leak":
            return f"Leak on {ev.asset}: send an inspector now"
        if ev.category == "pump_degradation":
            return f"{ev.asset} is wearing out: send an inspector"
        return f"Problem on {ev.asset}"

    @staticmethod
    def plan_action(p: Any) -> dict:
        if p is None:
            return {"type": "plan", "summary": "No plan is available yet.", "actions": [], "cost": None,
                    "expires_at": None, "expires_at_ts": None}
        actions = list(_get(p, "actions", []) or [])
        return {"type": "plan", "summary": _get(p, "summary") or describe_actions(actions),
                "actions": actions, "cost": _get(p, "cost"),
                "truck_cost": _get(p, "truck_cost"), "shut_in_cost": _get(p, "shut_in_cost"),
                "auto_shut_in_cost": _get(p, "auto_shut_in_cost"),
                "trips": _get(p, "trips"), "trip_at": _get(p, "trip_at"), "trip_at_ts": iso(_get(p, "trip_at")),
                "peak_level_pct": _get(p, "peak_level_pct"),
                "expires_at": _get(p, "expires_at"), "expires_at_ts": iso(_get(p, "expires_at")),
                "shares_with": list(_get(p, "shares_with", []) or [])}

    def alert_entries(self, ds: Dataset, view: Dataset) -> list[dict]:
        order, plans = self.ordered(view)
        out = []
        for n, ev in enumerate(order, 1):
            fac = ds.facilities.get(ev.facility)
            d = {"order": n, "event_id": ev.id, "category": ev.category, "system": ev.system,
                 "facility": ev.facility, "asset": ev.asset, "status": ev.status,
                 "onset": ev.onset, "onset_ts": iso(ev.onset), "detected_at": ev.detected_at,
                 "confidence": ev.confidence, "evidence": list(ev.evidence or []),
                 "urgent": ev.category in URGENT, "title": self._title(ds, view, ev),
                 "countdown": None, "approval": self.approval_for(ds.id, ev)}
            if ev.category == "pump_failure":
                repair_end = self.repair_end_of(ds, ev)
                d["countdown"] = {
                    "tank_id": fac.tank_id if fac else None, "level_pct": ev.level_pct,
                    "hours_to_limit": ev.hours_to_limit, "trips_at": ev.trips_at, "trips_at_ts": iso(ev.trips_at),
                    "repair_end": repair_end, "repair_end_ts": iso(repair_end), "tripped": ev.tripped,
                    "lost_revenue_per_hour": ev.lost_revenue_per_hour}
                d["action"] = self.plan_action(plans.get(ev.id))
            else:
                d["action"] = {"type": "field_inspection", "asset_id": ev.asset,
                               "summary": f"Send an inspector to {ev.asset}."}
            out.append(d)
        return out

    def alerts(self, at_raw: Any = None) -> dict:
        ds, view, at = self.view(at_raw)
        entries = self.alert_entries(ds, view)
        counts = {c: 0 for c in ("pump_failure", "pipeline_leak", "pump_degradation", "other")}
        for a in entries:
            counts[a["category"] if a["category"] in counts else "other"] += 1
        return {**self.header(view, at), "count": len(entries), "counts": counts, "alerts": entries}

    def events_export(self, at_raw: Any = None) -> tuple[dict, list[dict]]:
        """(header, every event up to `at` in SPEC's events.json shape). Ongoing pump failures
        carry the plan from now, resolved ones the plan made at their onset (history mode)."""
        ds, view, at = self.view(at_raw)
        events = self.events(view)
        now = self.plans_now(view, events)
        ref = (self.plans_reference(view, events)
               if any(e.category == "pump_failure" and e.status != "ongoing" for e in events) else {})
        out = []
        for ev in events:
            p = None
            if ev.category == "pump_failure":
                p = now.get(ev.id) if ev.status == "ongoing" else ref.get(ev.id)
            if p is not None:
                out.append(ev.to_events_json(actions=list(_get(p, "actions", []) or []), est_cost=_get(p, "cost")))
            else:
                out.append(ev.to_events_json())
        return self.header(view, at), out

    @staticmethod
    def trucks_free(ds: Dataset, ev: Event, ongoing: list[Event], plans: dict) -> int:
        """The system's fleet minus the trucks the joint plan gives the system's other failures."""
        fleet = ds.systems[ev.system].fleet_size if ev.system in ds.systems else 0
        used = 0
        for other in ongoing:
            if other.id == ev.id or other.system != ev.system:
                continue
            for a in _get(plans.get(other.id), "actions", []) or []:
                if a.get("type") == "dispatch_truck":
                    used += int(a.get("trucks", 0) or 0)
        return max(0, fleet - used)

    def plans(self, at_raw: Any = None, mode_raw: Any = None) -> dict:
        mode = (mode_raw or "now").strip().lower() if isinstance(mode_raw, (str, type(None))) else None
        if mode not in ("now", "reference"):
            raise BadInput(f"mode must be now or reference; got {str(mode_raw)[:40]!r}.")
        ds, view, at = self.view(at_raw)
        events = self.events(view)
        items = []
        if mode == "reference":
            plans = self.plans_reference(view, events)
            for ev in events:
                if ev.category == "pump_failure":
                    items.append({"event": ev, "plan": plans.get(ev.id),
                                  "approval": self.approval_for(ds.id, ev)})
        else:
            ongoing = self.ongoing_failures(events)
            plans = self.plans_now(view, events)
            for ev in ongoing:
                free = self.trucks_free(ds, ev, ongoing, plans)
                items.append({"event": ev, "plan": plans.get(ev.id), "trucks_free": free,
                              "alternatives": self.alternatives(view, ev, free),
                              "wait_curve": self.wait_curve(view, ongoing, ev),
                              "approval": self.approval_for(ds.id, ev)})
        # Per system: the fleet, its events, and (now mode) the trucks the joint plan uses.
        systems: dict[str, dict] = {}
        for it in items:
            ev, p = it["event"], it["plan"]
            s = systems.setdefault(ev.system, {
                "system": ev.system, "fleet_size": ds.systems[ev.system].fleet_size if ev.system in ds.systems else 0,
                "events": [], "trucks_used": 0 if mode == "now" else None, "cost": 0.0})
            s["events"].append(ev.id)
            if mode == "now":
                s["trucks_used"] += sum(int(a.get("trucks", 0) or 0) for a in _get(p, "actions", []) or []
                                        if a.get("type") == "dispatch_truck")
            s["cost"] += float(_get(p, "cost", 0) or 0)
        return {**self.header(view, at), "mode": mode, "plans": items, "systems": list(systems.values())}

    def series(self, asset_id: Any = None, metric: Any = None, from_raw: Any = None, to_raw: Any = None,
               max_points_raw: Any = None, at_raw: Any = None) -> dict:
        ds, view, at = self.view(at_raw)
        if not isinstance(asset_id, str) or not asset_id.strip():
            raise BadInput("asset_id is required, for example asset_id=<a tank id>&metric=level_pct.")
        asset_id = asset_id.strip()
        metrics = ds.metrics(asset_id)
        if not metrics:
            raise NotFound(f"No telemetry for {asset_id!r} in {ds.id}.")
        metric = metric.strip() if isinstance(metric, str) and metric.strip() else None
        if metric is not None and metric not in metrics:
            raise NotFound(f"{asset_id} has no {metric!r} readings; it has {', '.join(metrics)}.")
        max_points = parse_number(max_points_raw, "max_points", 500, 2, 20000, integer=True)
        t0, t1 = parse_ts(from_raw, "from"), parse_ts(to_raw, "to")
        if t0 is not None and t1 is not None and t0 > t1:
            raise BadInput("from must be at or before to.")
        lo, hi = ds.start, view.now
        t0 = lo if t0 is None else clamp_ts(t0, lo, hi)
        t1 = hi if t1 is None else clamp_ts(t1, lo, hi)
        out = {**self.header(view, at), "asset_id": asset_id, "from": t0, "from_ts": iso(t0),
               "to": t1, "to_ts": iso(t1), "max_points": max_points}

        def points(m: str) -> tuple[list, int]:
            raw = view.series(asset_id, m, t0, t1)
            return [[t, v] for t, v in downsample(raw, max_points)], len(raw)

        if metric is not None:
            pts, n = points(metric)
            out.update(metric=metric, raw_points=n, points=pts)
        else:
            both = {m: points(m) for m in metrics}
            out.update(metrics={m: v[0] for m, v in both.items()}, raw_points={m: v[1] for m, v in both.items()})
        return out

    @staticmethod
    def repair_end_of(ds: Dataset, ev: Event) -> int:
        """When the repair ends: the detector's repair_end, else onset + expected repair hours."""
        return int(ev.repair_end) if ev.repair_end is not None else int(ev.onset + ds.repair_hours * 3600)

    def _fate(self, ds: Dataset, e: Event, now: int, short: bool = False) -> str:
        """What a failed pump's tank does if nobody acts, in a few words."""
        t = lambda ts: clock_text(ts, now)   # noqa: E731
        fac = ds.facilities.get(e.facility)
        tank = fac.tank_id if fac else "its tank"
        level = f" {e.level_pct:.0f}%," if finite(e.level_pct) else ""
        repair = t(self.repair_end_of(ds, e))
        if e.tripped:
            return "at shutdown" if short else f"{tank} at shutdown, wells off until the repair at {repair}."
        if e.trips_at is not None:
            if short:
                return f"shuts in at {t(e.trips_at)}"
            h = p50(e)
            h = h if h is not None else max(0.0, (e.trips_at - now) / 3600.0)
            return f"{tank}{level} shuts in at {t(e.trips_at)}, {duration_text(h)} left."
        if short:
            return f"holds until the repair at {repair}"
        return f"{tank}{level} stays under shutdown until the repair at {repair}."

    def _todo(self, ds: Dataset, e: Event, p: Any, now: int) -> str:
        """The plan in a few words, and by when it must start (or who approved it)."""
        if p is None:
            return "No plan yet."
        actions = list(_get(p, "actions", []) or [])
        summary = str(_get(p, "summary") or describe_actions(actions)).rstrip(". ")
        idle = all(a.get("type") == "none" for a in actions)    # accept the trip, or nothing needed
        head = f"Plan: {summary}" if idle else f"Do: {summary}"
        ap = self.approval_for(ds.id, e)
        if ap:
            return f"{head}, approved by {ap['by']}."
        exp = _get(p, "expires_at")
        if idle or exp is None:
            return f"{head}."
        return f"{head}, start by {clock_text(exp, now)}."

    def handoff(self, at_raw: Any = None, shift_hours_raw: Any = None) -> dict:
        """A plain-English shift handoff built from the alerts, plans and events (no AI): what is
        down and when each tank shuts in, the action and its cost, what to inspect, and what
        resolved this shift. Every failed pump is named. The inspect and resolved lists take
        what is left of a 30-second reading budget (HANDOFF_WORDS) and end in "and N more";
        the alerts view has the rest."""
        ds, view, at = self.view(at_raw)
        hours = parse_number(shift_hours_raw, "shift_hours", SHIFT_HOURS, 1, 168)
        order, plans = self.ordered(view)
        events = self.events(view)
        now = view.now
        shift_start = max(ds.start, now - int(hours * 3600))
        failing = [e for e in order if e.category == "pump_failure"]
        urgency = {"pipeline_leak": 0, "pump_degradation": 1}     # leaks are the urgent inspections
        inspect = sorted((e for e in order if e.category != "pump_failure"),
                         key=lambda e: (urgency.get(e.category, 2), e.onset))
        resolved = sorted((e for e in events if e.status != "ongoing" and e.end is not None
                           and shift_start < e.end <= now), key=lambda e: -e.end)   # latest first
        t = lambda ts: clock_text(ts, now)   # noqa: E731
        if failing or inspect:
            head = f"{_plural(len(failing), 'pump')} down, {_plural(len(inspect), 'inspection')} open."
        else:
            head = "all clear, no pump down and no inspection open."
        lines = [f"Handoff {day_text(now)} UTC: {head}"]
        total = 0.0
        for i, e in enumerate(failing, 1):
            p = plans.get(e.id)
            cost = _get(p, "cost") if p is not None else None
            total += float(cost) if finite(cost) else 0.0
            if i <= HANDOFF_FULL:
                lines.append(f"{i}. {e.asset} down since {t(e.onset)}: {self._fate(ds, e, now)} "
                             f"{self._todo(ds, e, p, now)}")
        if len(failing) > HANDOFF_FULL:     # every failed pump is named, the rest more briefly
            lines.append("Also down: " + ", ".join(f"{e.asset} ({self._fate(ds, e, now, short=True)})"
                                                  for e in failing[HANDOFF_FULL:]) + ".")
        tail = [f"Plans total {money(total)}."] if failing else []
        what = {"pipeline_leak": "leak on", "pump_degradation": "worn"}
        checks = [f"{what.get(e.category, 'problem on')} {e.asset} since {t(e.onset)}" for e in inspect]
        done = [f"{e.asset} {WORDS.get(e.category, 'problem')} (ended {t(e.end)})" for e in resolved]
        left = HANDOFF_WORDS - _words(lines + tail)
        done_min = _fit("Resolved this shift: ", done, 0) if done else "Nothing resolved this shift."
        if checks:
            lines.append(_fit("Inspect: ", checks, left - _words([done_min])))
            left -= _words(lines[-1:])
        lines.append(_fit("Resolved this shift: ", done, left) if done else done_min)
        lines += tail
        text = "\n".join(lines)
        words = len(text.split())
        return {**self.header(view, at), "shift_hours": hours, "shift_start": shift_start,
                "shift_start_ts": iso(shift_start), "text": text, "lines": lines, "words": words,
                "read_s": math.ceil(words * 60 / READ_WPM),
                "counts": {"failing": len(failing), "inspect": len(inspect), "resolved": len(resolved)},
                "total_cost": total}

    # ----- what-if -----
    @staticmethod
    def normalize_actions(ds: Dataset, facility_id: str, raw: Any) -> list[dict]:
        """events.json actions for one facility, checked: unknown shapes, wells of another
        facility, or more trucks than the system has are a BadInput sentence."""
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise BadInput('actions must be a list of events.json actions, for example '
                           '[{"type": "dispatch_truck", "trucks": 1, "trips_per_truck": 1}].')
        if len(raw) > 20:
            raise BadInput("actions can hold at most 20 actions.")
        fac = ds.facilities[facility_id]
        fleet = ds.systems[fac.system].fleet_size
        fields = {"dispatch_truck": {"facility_id", "trucks", "trips_per_truck"},
                  "shut_in_wells": {"wells", "start_after_min"},
                  "field_inspection": {"asset_id"}, "none": set()}
        out, trucks = [], 0
        for i, a in enumerate(raw):
            where = f"actions[{i}]"
            if not isinstance(a, dict) or not isinstance(a.get("type"), str):
                raise BadInput(f"{where} must be an object with a type ({', '.join(ACTION_TYPES)}).")
            kind = a["type"]
            if kind not in fields:
                raise BadInput(f"{where} has an unknown type {kind[:40]!r}; use {', '.join(ACTION_TYPES)}.")
            extra = sorted(set(a) - fields[kind] - {"type"})
            if extra:
                takes = ", ".join(sorted(fields[kind])) or "nothing else"
                raise BadInput(f"{where} ({kind}) has an unknown field {extra[0][:40]!r}; it takes {takes}.")
            if kind == "dispatch_truck":
                fid = a.get("facility_id", facility_id)
                if fid != facility_id:
                    raise BadInput(f"{where} sends trucks to {str(fid)[:40]!r}, but this what-if is for {facility_id}.")
                if fleet < 1:
                    raise BadInput(f"{fac.system} has no trucks, so {where} can't dispatch any.")
                n = parse_number(a.get("trucks", 1), f"{where}.trucks", 1, 1, fleet, integer=True)
                k = parse_number(a.get("trips_per_truck", 1), f"{where}.trips_per_truck", 1, 1, 100, integer=True)
                trucks += n
                out.append({"type": kind, "facility_id": facility_id, "trucks": n, "trips_per_truck": k})
            elif kind == "shut_in_wells":
                wells = a.get("wells")
                if isinstance(wells, str):
                    wells = [wells]
                if not isinstance(wells, list) or not wells or not all(isinstance(w, str) for w in wells):
                    raise BadInput(f"{where}.wells must be a non-empty list of well ids at {facility_id}.")
                bad = [w for w in wells if w not in fac.well_ids]
                if bad:
                    raise BadInput(f"{where}.wells names {bad[0][:40]!r}, which is not a well at {facility_id} "
                                   f"(its wells are {', '.join(fac.well_ids)}).")
                m = parse_number(a.get("start_after_min", 0), f"{where}.start_after_min", 0, 0, 100000)
                out.append({"type": kind, "wells": list(dict.fromkeys(wells)),
                            "start_after_min": int(m) if m == int(m) else m})
            elif kind == "field_inspection":
                asset = a.get("asset_id")
                if not isinstance(asset, str) or not asset.strip():
                    raise BadInput(f"{where}.asset_id must name the asset to inspect.")
                out.append({"type": kind, "asset_id": asset.strip()})
            else:
                out.append({"type": "none"})
        if trucks > fleet:
            raise BadInput(f"{fac.system} has {_plural(fleet, 'truck')}, but the actions call {trucks}.")
        return out

    def start_level(self, plan: Any, ds: Dataset, view: Dataset, ev: Event) -> Optional[float]:
        """The tank level a replay starts from. First the planner's own level (plan.countdown, the
        de-noised level its plans start from), so replaying the recommended actions costs what
        the recommended plan says. Then the event's level, then the reading. A tank already at
        shutdown starts there. None when there is no level at all."""
        countdown = getattr(plan, "countdown", None)
        if callable(countdown):
            try:
                level = _get(countdown(view, ev), "level_pct")
                if finite(level):
                    return float(level)
            except Exception as e:  # noqa: BLE001 - fall back to the event's own level
                self._log_once("countdown for a what-if", e)
        if ev.tripped:
            return float(ds.shutdown_pct)
        level = ev.level_pct if finite(ev.level_pct) else view.value_at(ds.facilities[ev.facility].tank_id, "level_pct")
        return float(level) if finite(level) else None

    def whatif(self, body: dict) -> dict:
        facility_id = body.get("facility_id")
        if not isinstance(facility_id, str) or not facility_id.strip():
            raise BadInput("facility_id is required: the facility whose ongoing pump failure to replay.")
        facility_id = facility_id.strip()
        ds, view, at = self.view(body.get("at"))
        if facility_id not in ds.facilities:
            raise NotFound(f"No facility named {facility_id[:60]!r} in {ds.id}.")
        actions = self.normalize_actions(ds, facility_id, body.get("actions"))
        events = self.events(view)
        ev = next((e for e in self.ongoing_failures(events) if e.facility == facility_id), None)
        if ev is None:
            raise BadInput(f"{facility_id} has no ongoing pump failure at {iso(view.now)}, so there is nothing to replay.")
        fac = ds.facilities[facility_id]
        plan = _module("plan")
        level = self.start_level(plan, ds, view, ev)
        if level is None:
            raise BadInput(f"{fac.tank_id} has no level reading at {iso(view.now)}, so there is no level to replay from.")
        repair_end = self.repair_end_of(ds, ev)
        effective = [a for a in actions if a["type"] in ("dispatch_truck", "shut_in_wells")]
        result = plan.simulate(view, facility_id, view.now, level, repair_end, effective)
        baseline = plan.simulate(view, facility_id, view.now, level, repair_end, [])
        out = {**self.header(view, at), "facility_id": facility_id, "event": ev, "start": view.now,
               "level_pct": level, "repair_end": repair_end, "repair_end_ts": iso(repair_end),
               "actions": actions, "result": result, "baseline": baseline, "savings": None}
        rc, bc = _get(result, "cost"), _get(baseline, "cost")
        if finite(rc) and finite(bc):
            out["savings"] = round(bc - rc, 2)      # cents, without float noise like -95.90000000000009
        return out

    # ----- approve -----
    def plan_steps(self, ds: Dataset, view: Dataset, ev: Event, p: Any) -> list[dict]:
        """Numbered steps for a plan: call the trucks, when each load arrives, when to shut in
        each well, and when the repair is due. Truck times follow SPEC: every truck is called
        when the plan starts and arrives response_min later, then one trip every cycle_min."""
        fac = ds.facilities[ev.facility]
        start = int(_get(p, "start") or view.now)
        now = view.now
        t = lambda ts: clock_text(ts, now)   # noqa: E731
        steps: list[dict] = []

        def add(ts: int, asset: Optional[str], text: str, order: int) -> None:
            steps.append({"at": int(ts), "at_ts": iso(ts), "asset_id": asset, "text": text, "_o": order})

        repair_end = self.repair_end_of(ds, ev)
        repair = f" at {t(repair_end)}"
        for a in _get(p, "actions", []) or []:
            kind = a.get("type")
            if kind == "dispatch_truck":
                n, k = int(a.get("trucks", 1) or 1), int(a.get("trips_per_truck", 1) or 1)
                f = ds.facilities.get(a.get("facility_id") or fac.id, fac)
                add(start, f.id, f"Call {_plural(n, 'truck')} from the {f.system} truck yard to {f.id} now "
                                 f"({t(start)}), {_plural(k, 'trip')} per truck.", 0)
                for j in range(k):
                    ts = start + int(round((f.response_min + j * f.cycle_min) * 60))
                    add(ts, f.id, f"{'First' if j == 0 else 'Next'} load: {_plural(n, 'truck')} at {f.id} at {t(ts)}, "
                                  f"hauling {n * ds.truck_capacity_bbl:g} bbl.", 1)
            elif kind == "shut_in_wells":
                m = float(a.get("start_after_min") or 0)
                ts = start + int(round(m * 60))
                when = f"now ({t(ts)})" if m <= 0 else f"at {t(ts)}, {duration_text(m / 60)} from now"
                for w in a.get("wells", []):
                    add(ts, w, f"Shut in {w} {when} and keep it off until the repair{repair}.", 2)
            elif kind == "none":
                if ev.tripped:
                    add(start, fac.tank_id, f"{fac.tank_id} is already at shutdown: its wells stay shut in until the repair{repair}.", 2)
                elif _get(p, "trips") and _get(p, "trip_at") is not None:
                    add(int(_get(p, "trip_at")), fac.tank_id,
                        f"Let {fac.tank_id} reach shutdown at {t(_get(p, 'trip_at'))}: its wells shut in by "
                        f"themselves until the repair{repair}.", 2)
                else:
                    add(start, fac.tank_id, f"No action needed: {fac.tank_id} stays under shutdown until the repair{repair}.", 2)
            elif kind == "field_inspection":
                add(start, a.get("asset_id"), f"Send an inspector to {a.get('asset_id')} now.", 0)
        add(repair_end, ev.asset, f"The {ev.asset} repair is due at {t(repair_end)}.", 3)
        steps.sort(key=lambda s: (s["at"], s["_o"]))
        for n, s in enumerate(steps, 1):
            s.pop("_o")
            s["n"] = n
        return [{"n": s["n"], **{k: v for k, v in s.items() if k != "n"}} for s in steps]

    def approve(self, event_id: str, by: Optional[str] = None, at_raw: Any = None) -> dict:
        ds, view, at = self.view(at_raw)
        events = self.events(view)
        ev = next((e for e in events if e.id == event_id), None)
        if ev is None:
            raise NotFound(f"No event {event_id[:40]!r} in {ds.id} as of {iso(view.now)}.")
        if ev.status != "ongoing":
            raise BadInput(f"{ev.id} was resolved at {iso(ev.end)}, so there is nothing to approve.")
        now = view.now
        t = lambda ts: clock_text(ts, now)   # noqa: E731
        fac = ds.facilities.get(ev.facility)
        if ev.category == "pump_failure":
            p = self.plans_now(view, events).get(ev.id)
            if p is None:
                raise BadInput(f"No plan is available for {ev.id} yet, so there is nothing to approve.")
            actions = list(_get(p, "actions", []) or [])
            summary = _get(p, "summary") or describe_actions(actions)
            cost, expires = _get(p, "cost"), _get(p, "expires_at")
            steps = self.plan_steps(ds, view, ev, p)
            tank = fac.tank_id if fac else "the tank"
            said = [f"Field, this is control. {ev.asset} at {ev.facility} has been down since {t(ev.onset)}."]
            if finite(ev.level_pct):
                said.append(f"{tank} is at {ev.level_pct:.0f} percent.")
            if ev.tripped:
                said.append("It is at shutdown and its wells are shut in.")
            elif ev.trips_at is not None:
                said.append(f"With no action it shuts in at {t(ev.trips_at)}.")
            said.append(f"Plan: {summary}.")
            said += [s["text"] for s in steps]
            if expires is not None:
                said.append(f"This plan only works if it starts by {t(expires)}.")
            said.append("Read back when ready. Control out.")
        else:
            actions = [{"type": "field_inspection", "asset_id": ev.asset}]
            summary, cost, expires = f"Send an inspector to {ev.asset}.", None, None
            what = "a possible leak" if ev.category == "pipeline_leak" else "a pump that is wearing out"
            steps = [{"n": 1, "at": now, "at_ts": iso(now), "asset_id": ev.asset,
                      "text": f"Send an inspector to {ev.asset} at {ev.facility} now: {what} since {t(ev.onset)}."}]
            said = [f"Field, this is control. We see {what} on {ev.asset} at {ev.facility} since {t(ev.onset)}.",
                    f"Send an inspector to {ev.asset} now and report what you find.",
                    "Read back when ready. Control out."]
        record = self._record(ds.id, ev, {
            "event_id": ev.id, "dataset": ds.id, "category": ev.category, "facility_id": ev.facility,
            "asset_id": ev.asset, "onset": ev.onset, "by": (by or "operator").strip()[:60] or "operator",
            "at": now, "at_ts": iso(now), "summary": summary, "actions": actions, "cost": cost})
        return {**self.header(view, at), "event_id": ev.id, "category": ev.category,
                "facility_id": ev.facility, "summary": summary, "actions": actions, "cost": cost,
                "expires_at": expires, "expires_at_ts": iso(expires), "steps": steps,
                "radio_script": " ".join(said), "approval": record}

    # ----- WebSocket -----
    def tick(self) -> tuple[dict, tuple]:
        """The WebSocket push (clock and alerts at the cursor) and the signature it was built at.
        The clock always goes out, so a time slider keeps moving: when the alerts can't be
        built, `alerts` is null and `error` says why in one sentence."""
        ds = self.dataset()
        clock = self.clock_payload(ds)
        view = ds.at(clock["at"])
        payload: dict = {"type": "tick", "dataset": ds.id, "clock": clock, "alerts": None}
        try:
            payload["alerts"] = self.alert_entries(ds, view)
        except ServiceError as e:
            payload["error"] = {"status": e.status, "detail": e.sentence}
        except Exception as e:  # noqa: BLE001 - one bad moment must not stop the clock
            self._log_once("alerts", e)
            payload["error"] = {"status": 500, "detail": f"Internal error: {type(e).__name__}: {_sentence(e)}."}
        return payload, self._signature(ds, clock)

    def _signature(self, ds: Dataset, clock: dict) -> tuple:
        # While playing, a push per new data row; while paused, any cursor move, play/pause,
        # speed, dataset switch, file change or approval pushes.
        return (ds.id, id(ds.times), clock["now"], clock["playing"], clock["speed"],
                None if clock["playing"] else clock["at"], self.version)

    def signature(self) -> tuple:
        ds = self.dataset()
        return self._signature(ds, self.clock_payload(ds))


SERVICE = Service()
