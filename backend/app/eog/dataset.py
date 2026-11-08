"""Load one EOG dataset folder: network.json, ops.json and telemetry.csv (SPEC.md "Files").

Only those three files are read. answer_key.json may sit in the same folder, but the app never
opens it: at judging there is none. The time range comes only from the telemetry, and its last
`ts_unix` is "now" (SPEC.md "Time range"). `Dataset.at(ts)` gives a time-travel view that ends at
an earlier "now", sharing the parsed arrays, so the same code serves any moment of the week.

Everything is plain stdlib so the backend keeps no extra dependencies.
"""
from __future__ import annotations

import csv
import json
import math
import os
import threading
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

FILES = ("network.json", "ops.json", "telemetry.csv")
TELEMETRY_COLUMNS = ["timestamp", "ts_unix", "asset_id", "metric", "value"]
STEP_S = 300                      # every series reports every 5 minutes
ENV_ROOT = "FREEBOARD_EOG_DIR"    # a checkout of the sponsor repo, or any folder of datasets
NAN = float("nan")


class DatasetError(ValueError):
    """The folder is not a usable dataset. The message says what is wrong, in plain English."""


@dataclass(frozen=True)
class Well:
    id: str
    facility: str
    system: str
    baseline_water_bpd: float
    oil_bpd: float
    gas_mcfd: float


@dataclass(frozen=True)
class Facility:
    id: str
    system: str
    lat: float
    lon: float
    wells: tuple[Well, ...]
    tank_id: str
    capacity_bbl: float
    pump_id: str
    max_flow_bpd: float
    start_level_pct: float
    stop_level_pct: float
    pipeline_id: str
    pipeline_max_bpd: Optional[float]
    pipeline_max_pressure_psi: Optional[float]
    response_min: float           # truck yard to this facility, first trip only
    cycle_min: float              # one round trip: haul away, empty, come back

    @property
    def well_ids(self) -> list[str]:
        return [w.id for w in self.wells]

    @property
    def baseline_water_bpd(self) -> float:
        return sum(w.baseline_water_bpd for w in self.wells)


@dataclass(frozen=True)
class System:
    id: str
    center: tuple[float, float]
    truck_yard: tuple[float, float]
    disposal_id: str
    disposal_lat: float
    disposal_lon: float
    disposal_pressure_psi: float
    fleet_size: int
    facility_ids: tuple[str, ...]


def _need(obj: dict, key: str, where: str):
    if not isinstance(obj, dict) or key not in obj:
        raise DatasetError(f"{where} is missing '{key}'.")
    return obj[key]


def _num(obj: dict, key: str, where: str, optional: bool = False) -> Optional[float]:
    if optional and (not isinstance(obj, dict) or obj.get(key) is None):
        return None
    v = _need(obj, key, where)
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise DatasetError(f"{where}.{key} must be a number, got {v!r}.") from None
    if math.isnan(f) or math.isinf(f):
        raise DatasetError(f"{where}.{key} must be a finite number, got {v!r}.")
    return f


def _pair(v, where: str) -> tuple[float, float]:
    if not (isinstance(v, (list, tuple)) and len(v) == 2):
        raise DatasetError(f"{where} must be [lat, lon].")
    try:
        return float(v[0]), float(v[1])
    except (TypeError, ValueError):
        raise DatasetError(f"{where} must be [lat, lon] numbers.") from None


class Dataset:
    """A parsed dataset, or a time-travel view of one (see `at`)."""

    def __init__(self, folder: Path, ds_id: str, network: dict, ops: dict,
                 times: array, series: dict[tuple[str, str], array], end: Optional[int] = None):
        self.folder = folder
        self.id = ds_id
        self.network = network
        self.ops = ops
        self.times = times
        self._series = series
        self.end = len(times) if end is None else end
        self._index()

    # ---------- structure ----------
    def _index(self) -> None:
        net, ops = self.network, self.ops
        limits = _need(_need(net, "defaults", "network.json"), "tank_limits_pct", "network.json defaults")
        self.warning_pct = _num(limits, "warning", "network.json defaults.tank_limits_pct")
        self.shutdown_pct = _num(limits, "shutdown", "network.json defaults.tank_limits_pct")
        prices = _need(ops, "prices", "ops.json")
        self.oil_price = _num(prices, "oil_per_bbl", "ops.json prices")
        self.gas_price = _num(prices, "gas_per_mcf", "ops.json prices")
        self.repair_hours = _num(_need(ops, "expected_repair_hours", "ops.json"), "pump_failure",
                                 "ops.json expected_repair_hours")
        trucking = _need(ops, "emergency_trucking", "ops.json")
        self.truck_capacity_bbl = _num(trucking, "truck_capacity_bbl", "ops.json emergency_trucking")
        self.truck_cost = _num(trucking, "cost_per_trip", "ops.json emergency_trucking")
        ops_systems = _need(ops, "systems", "ops.json")

        systems = _need(net, "systems", "network.json")
        if isinstance(systems, dict):            # tolerate a dict keyed by id as well as a list
            systems = [dict(v, id=v.get("id", k)) for k, v in systems.items()]
        self.systems: dict[str, System] = {}
        self.facilities: dict[str, Facility] = {}
        self.wells: dict[str, Well] = {}
        # asset id -> {"kind", "system", "facility"} for every id the telemetry can name
        self.assets: dict[str, dict] = {}
        for s in systems:
            sid = str(_need(s, "id", "network.json system"))
            where = f"network.json system {sid}"
            disposal = _need(s, "disposal_site", where)
            sops = _need(ops_systems, sid, "ops.json systems")
            fops = _need(sops, "facilities", f"ops.json systems.{sid}")
            fac_ids = []
            for f in _need(s, "facilities", where):
                fid = str(_need(f, "id", f"{where} facility"))
                fw = f"network.json facility {fid}"
                tank, pump, pipe = _need(f, "tank", fw), _need(f, "pump", fw), _need(f, "pipeline", fw)
                tops = _need(fops, fid, f"ops.json systems.{sid}.facilities")
                wells = []
                for w in _need(f, "wells", fw):
                    wid = str(_need(w, "id", f"{fw} well"))
                    ww = f"network.json well {wid}"
                    well = Well(wid, fid, sid, _num(w, "baseline_water_bpd", ww), _num(w, "oil_bpd", ww),
                                _num(w, "gas_mcfd", ww))
                    wells.append(well)
                    self.wells[wid] = well
                    self.assets[wid] = {"kind": "well", "system": sid, "facility": fid}
                fac = Facility(
                    id=fid, system=sid, lat=_num(f, "lat", fw), lon=_num(f, "lon", fw), wells=tuple(wells),
                    tank_id=str(_need(tank, "id", f"{fw} tank")),
                    capacity_bbl=_num(tank, "capacity_bbl", f"{fw} tank"),
                    pump_id=str(_need(pump, "id", f"{fw} pump")),
                    max_flow_bpd=_num(pump, "max_flow_bpd", f"{fw} pump"),
                    start_level_pct=_num(pump, "start_level_pct", f"{fw} pump"),
                    stop_level_pct=_num(pump, "stop_level_pct", f"{fw} pump"),
                    pipeline_id=str(_need(pipe, "id", f"{fw} pipeline")),
                    pipeline_max_bpd=_num(pipe, "max_bpd", f"{fw} pipeline", optional=True),
                    pipeline_max_pressure_psi=_num(pipe, "max_pressure_psi", f"{fw} pipeline", optional=True),
                    response_min=_num(tops, "response_min", f"ops.json {fid}"),
                    cycle_min=_num(tops, "cycle_min", f"ops.json {fid}"),
                )
                if fac.capacity_bbl <= 0:
                    raise DatasetError(f"{fw} tank capacity_bbl must be above 0.")
                self.facilities[fid] = fac
                fac_ids.append(fid)
                self.assets[fid] = {"kind": "facility", "system": sid, "facility": fid}
                self.assets[fac.tank_id] = {"kind": "tank", "system": sid, "facility": fid}
                self.assets[fac.pump_id] = {"kind": "pump", "system": sid, "facility": fid}
                self.assets[fac.pipeline_id] = {"kind": "pipeline", "system": sid, "facility": fid}
            center = _pair(s.get("center", [disposal.get("lat"), disposal.get("lon")]), f"{where} center")
            yard = _pair(s.get("truck_yard", list(center)), f"{where} truck_yard")
            fleet = _num(sops, "emergency_fleet_size", f"ops.json systems.{sid}")
            system = System(
                id=sid, center=center, truck_yard=yard,
                disposal_id=str(_need(disposal, "id", f"{where} disposal_site")),
                disposal_lat=_num(disposal, "lat", f"{where} disposal_site"),
                disposal_lon=_num(disposal, "lon", f"{where} disposal_site"),
                disposal_pressure_psi=_num(disposal, "pressure_psi", f"{where} disposal_site"),
                fleet_size=int(fleet), facility_ids=tuple(fac_ids),
            )
            self.systems[sid] = system
            self.assets[system.disposal_id] = {"kind": "disposal", "system": sid, "facility": None}

    # ---------- time ----------
    @property
    def start(self) -> int:
        return int(self.times[0])

    @property
    def now(self) -> int:
        """The last visible telemetry timestamp. Never the system clock."""
        return int(self.times[self.end - 1])

    @property
    def last(self) -> int:
        """The last timestamp in the file, whatever view this is."""
        return int(self.times[-1])

    def index_at(self, ts: float) -> int:
        """Index of the last visible row at or before ts (clamped to the first row)."""
        i = bisect_right(self.times, int(ts), 0, self.end) - 1
        return max(0, i)

    def at(self, ts: Optional[float]) -> "Dataset":
        """A view whose "now" is the last row at or before ts. ts None returns this view."""
        if ts is None:
            return self
        ts = int(ts)
        if ts < self.start:
            raise DatasetError(f"{ts} is before the first telemetry row ({self.start}).")
        end = bisect_right(self.times, ts, 0, len(self.times))
        if end == self.end:
            return self
        view = object.__new__(Dataset)
        view.__dict__.update(self.__dict__)
        view.end = end
        return view

    @property
    def is_cut(self) -> bool:
        return self.end < len(self.times)

    # ---------- readings ----------
    def has(self, asset_id: str, metric: str) -> bool:
        return (asset_id, metric) in self._series

    def metrics(self, asset_id: str) -> list[str]:
        return sorted(m for (a, m) in self._series if a == asset_id)

    def series_keys(self) -> list[tuple[str, str]]:
        return sorted(self._series)

    def values(self, asset_id: str, metric: str) -> array:
        """The whole column for this series, aligned with `times` (NaN where a row is missing).
        Only indexes below `self.end` are visible in this view; callers must respect that."""
        try:
            return self._series[(asset_id, metric)]
        except KeyError:
            raise DatasetError(f"No telemetry for {asset_id} {metric}.") from None

    def value_at(self, asset_id: str, metric: str, ts: Optional[float] = None) -> Optional[float]:
        """The reading at the last visible row at or before ts (default: now). None if missing."""
        col = self._series.get((asset_id, metric))
        if col is None:
            return None
        i = self.end - 1 if ts is None else self.index_at(ts)
        v = col[i]
        return None if math.isnan(v) else v

    def series(self, asset_id: str, metric: str, t0: Optional[float] = None,
               t1: Optional[float] = None) -> list[tuple[int, float]]:
        """(ts_unix, value) pairs for the visible window [t0, t1], skipping missing rows."""
        col = self.values(asset_id, metric)
        i0 = 0 if t0 is None else bisect_right(self.times, int(t0) - 1, 0, self.end)
        i1 = self.end if t1 is None else bisect_right(self.times, int(t1), 0, self.end)
        out = []
        for i in range(i0, i1):
            v = col[i]
            if not math.isnan(v):
                out.append((int(self.times[i]), v))
        return out

    # ---------- money and water ----------
    def revenue_per_day(self, well_id: str) -> float:
        w = self.wells[well_id]
        return w.oil_bpd * self.oil_price + w.gas_mcfd * self.gas_price

    def revenue_per_hour(self, well_id: str) -> float:
        return self.revenue_per_day(well_id) / 24.0

    def facility_revenue_per_hour(self, facility_id: str) -> float:
        return sum(self.revenue_per_hour(w.id) for w in self.facilities[facility_id].wells)

    def cost_per_bbl_avoided(self, well_id: str) -> Optional[float]:
        w = self.wells[well_id]
        return None if w.baseline_water_bpd <= 0 else self.revenue_per_day(well_id) / w.baseline_water_bpd

    def facility_of(self, asset_id: str) -> Optional[str]:
        a = self.assets.get(asset_id)
        return a["facility"] if a else None

    def system_of(self, asset_id: str) -> Optional[str]:
        a = self.assets.get(asset_id)
        return a["system"] if a else None

    def summary(self) -> dict:
        return {
            "id": self.id, "start": self.start, "now": self.now, "last": self.last,
            "step_s": STEP_S, "rows": self.end, "is_cut": self.is_cut,
            "systems": [s for s in self.systems],
            "facilities": len(self.facilities), "wells": len(self.wells),
        }


# ---------- parsing ----------
def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise DatasetError(f"{path.name} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise DatasetError(f"{path.name} must hold a JSON object.")
    return data


def _read_telemetry(path: Path) -> tuple[array, dict[tuple[str, str], array]]:
    rows_ts: list[int] = []
    rows_key: list[tuple[str, str]] = []
    rows_val: list[float] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.reader(f)
        try:
            header = [h.strip() for h in next(rd)]
        except StopIteration:
            raise DatasetError("telemetry.csv is empty.") from None
        if header != TELEMETRY_COLUMNS:
            raise DatasetError(f"telemetry.csv columns must be {', '.join(TELEMETRY_COLUMNS)}; got {', '.join(header)}.")
        intern: dict[tuple[str, str], tuple[str, str]] = {}
        for n, r in enumerate(rd, start=2):
            if not r:
                continue
            if len(r) != 5:
                raise DatasetError(f"telemetry.csv line {n} has {len(r)} fields, expected 5.")
            try:
                ts = int(float(r[1]))
                v = float(r[4])
            except ValueError:
                raise DatasetError(f"telemetry.csv line {n}: ts_unix and value must be numbers.") from None
            key = (r[2], r[3])
            key = intern.setdefault(key, key)
            rows_ts.append(ts)
            rows_key.append(key)
            rows_val.append(v)
    if not rows_ts:
        raise DatasetError("telemetry.csv has no readings.")
    times = sorted(set(rows_ts))
    pos = {t: i for i, t in enumerate(times)}
    n = len(times)
    series: dict[tuple[str, str], array] = {}
    for ts, key, v in zip(rows_ts, rows_key, rows_val):
        col = series.get(key)
        if col is None:
            col = series[key] = array("d", [NAN]) * n
        col[pos[ts]] = v
    return array("q", times), series


# folder -> (file signature, (network, ops, times, series)). The heavy parse is cached; a Dataset
# object is cheap to build, so each load() gets its own, carrying the id it was asked for.
_CACHE: dict[str, tuple[tuple, tuple]] = {}
_LOCK = threading.Lock()


def _signature(folder: Path) -> tuple:
    sig = []
    for name in FILES:
        st = (folder / name).stat()
        sig.append((name, st.st_mtime_ns, st.st_size))
    return tuple(sig)


def is_dataset(folder: Path) -> bool:
    return all((folder / name).is_file() for name in FILES)


def load(folder: os.PathLike | str, ds_id: Optional[str] = None) -> Dataset:
    """Parse a dataset folder (cached until one of its three files changes)."""
    folder = Path(folder).resolve()
    missing = [name for name in FILES if not (folder / name).is_file()]
    if missing:
        raise DatasetError(f"{folder.name} is not a dataset: missing {', '.join(missing)}.")
    key = str(folder)
    sig = _signature(folder)
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and hit[0] == sig:
        parsed = hit[1]
    else:
        network = _read_json(folder / "network.json")
        ops = _read_json(folder / "ops.json")
        times, series = _read_telemetry(folder / "telemetry.csv")
        parsed = (network, ops, times, series)
        Dataset(folder, ds_id or folder.name, *parsed)      # validate before caching
        with _LOCK:
            _CACHE[key] = (sig, parsed)
    return Dataset(folder, ds_id or folder.name, *parsed)


def open_dataset(ds_id: str, base: Optional[Path] = None) -> Dataset:
    """Load a discovered dataset by id (e.g. "history/full_week" or "test")."""
    return load(resolve(ds_id, base), ds_id)


def root() -> Optional[Path]:
    """The datasets root from FREEBOARD_EOG_DIR, or None when dataset mode is off."""
    raw = os.environ.get(ENV_ROOT, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def discover(base: Optional[Path] = None, max_depth: int = 3) -> list[dict]:
    """Every folder under base (default: FREEBOARD_EOG_DIR) holding the three files, by id.

    The id is the folder's path relative to base, with forward slashes, e.g.
    "history/full_week" or "test". Hidden folders and .git are skipped.
    """
    base = base or root()
    if base is None:
        return []
    base = Path(base).resolve()
    found: list[dict] = []

    def walk(d: Path, depth: int) -> None:
        if is_dataset(d):
            rel = d.relative_to(base).as_posix()
            found.append({"id": rel if rel != "." else d.name, "path": str(d)})
        if depth >= max_depth:
            return
        try:
            kids = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return
        for k in kids:
            walk(k, depth + 1)

    walk(base, 0)
    return found


def resolve(ds_id: str, base: Optional[Path] = None) -> Path:
    """The folder for a discovered id. Only folders under the root can be selected."""
    for d in discover(base):
        if d["id"] == ds_id:
            return Path(d["path"])
    raise DatasetError(f"No dataset named {ds_id!r}. Known: {', '.join(d['id'] for d in discover(base)) or 'none'}.")


def iter_rows(ds: Dataset, keys: Iterable[tuple[str, str]]) -> Iterable[tuple[int, str, str, float]]:
    """(ts, asset, metric, value) for the visible rows of the given series, in time order."""
    cols = [(a, m, ds.values(a, m)) for a, m in keys]
    for i in range(ds.end):
        t = int(ds.times[i])
        for a, m, col in cols:
            v = col[i]
            if not math.isnan(v):
                yield t, a, m, v
