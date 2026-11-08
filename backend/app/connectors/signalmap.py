"""What every field connector shares: secrets, value rules, quality, backoff, commands.

A connector speaks one protocol. The rest of what it needs is here, so the safety
rules are written once:

- Secrets. A config key ending in `_env` names an environment variable, and the name
  must start with FREEBOARD_CONN_. check_env_names() raises ValueError otherwise; call
  it in __init__ so the registry shows the connector unavailable with the reason.
  env_secret() reads the variable. A secret value is never logged, printed, or put in
  a status or an error message.
- Values. coerce() turns a raw device value into the live layer's type for a signal.
  SignalMap applies each declared signal's `scale` and its rule in config.signal_map
  (4-20 mA with NAMUR NE43 checks, linear scaling, unit conversion, range, invert,
  open and closed values) and returns Mapped(value, quality, detail).
- Quality. Anything but GOOD has value None. Bad or uncertain device quality, a lost
  link, NaN or inf, a value out of range, a 4-20 mA fault, or an old device timestamp
  is never read as a number, and never replaced by the last good one. A retained,
  replayed, or historical message is STALE: pass quality=STALE, or skip it.
- Inbox holds the newest mapped value per signal between the connector's I/O thread
  and read(). A value that isn't good removes the good one still waiting.
- Backoff paces reconnects, close_later() lets close() return at once, and
  uncertain_result() is send()'s answer when a write's acknowledgement never came.
- normalize() turns a confirmed command into (type, asset_id, value) for the wire.

For now a value that isn't good becomes no reading, not a reading of None. At this
commit detect.py calls float() on tank levels, well flows, and pump flows, so a None
reading makes the live tick raise (a None tank level keeps it raising for 10 live
minutes, while it sits in the rate window), and a pump whose `running` is None still
shows OK. With no reading, the asset goes stale after `sensors.stale_after_min` and
shows UNKNOWN. Inbox(none_readings=True) turns None readings on once detect.py skips
None values.
"""
from __future__ import annotations

import importlib
import math
import numbers
import os
import random
import re
import threading
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable, Optional

from ..fieldmap import SIGNALS

# ---------- secrets ----------

ENV_PREFIX = "FREEBOARD_CONN_"
ENV_NAME = re.compile(r"FREEBOARD_CONN_[A-Za-z0-9_]+")


def _env_example(key: str) -> str:
    return ENV_PREFIX + (re.sub(r"[^A-Z0-9_]", "_", key[:-4].upper()).strip("_") or "SECRET")


def check_env_names(config: Any) -> None:
    """Raise ValueError unless every `*_env` key names a FREEBOARD_CONN_ variable.

    Nested objects and lists are checked too. An empty or null `*_env` names nothing
    and is allowed. The error names the key and never repeats the value, in case a
    secret was pasted there by mistake."""
    _check_env(config, "")


def _check_env(node: Any, where: str) -> None:
    if isinstance(node, list):
        for i, item in enumerate(node):
            _check_env(item, f"{where}[{i}]")
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        path = f"{where}.{key}" if where else str(key)
        if isinstance(key, str) and key.endswith("_env"):
            if value is None or (isinstance(value, str) and value == ""):
                continue
            if not (isinstance(value, str) and ENV_NAME.fullmatch(value)):
                raise ValueError(
                    f"config {path} must name an environment variable that starts with {ENV_PREFIX} "
                    f"(for example {_env_example(key)}); the value given doesn't, so it is not read")
        elif isinstance(value, (dict, list)):
            _check_env(value, path)


def env_secret(config: Optional[dict], key: str) -> Optional[str]:
    """The value of the environment variable that config[key + "_env"] names, or None.

    Only FREEBOARD_CONN_ variables are ever read, even when check_env_names() was
    skipped. The value goes back to the caller and nowhere else: nothing is logged."""
    var = (config or {}).get(f"{key}_env")
    if not (isinstance(var, str) and ENV_NAME.fullmatch(var)):
        return None
    return os.environ.get(var)


def missing_env(config: Optional[dict]) -> Optional[str]:
    """The name of the first FREEBOARD_CONN_ variable a top-level `*_env` key names
    that isn't set, else None. Only the name, for a status line."""
    for key, var in (config or {}).items():
        if (isinstance(key, str) and key.endswith("_env") and isinstance(var, str)
                and ENV_NAME.fullmatch(var) and os.environ.get(var) is None):
            return var
    return None


# ---------- optional libraries ----------

def import_optional(module: str, package: str) -> tuple[Optional[ModuleType], Optional[str]]:
    """(module, None), or (None, why) when the library is missing or fails to import.
    The backend runs without any protocol library installed."""
    try:
        return importlib.import_module(module), None
    except ImportError as e:
        return None, f"{package} is not installed (pip install {package}): {e}"
    except Exception as e:  # noqa: BLE001 - a broken install must not stop the backend
        return None, f"{package} is installed but failed to import: {type(e).__name__}: {e}"


# ---------- raw values ----------

ANALOG = ("level_pct", "flow_bbl_hr", "pressure_psi", "motor_amps")
BOOLEAN = ("running", "position")
TRUE_WORDS = frozenset({"OPEN", "OPENED", "ON", "TRUE", "1", "RUNNING", "RUN"})
FALSE_WORDS = frozenset({"CLOSED", "CLOSE", "OFF", "FALSE", "0", "STOPPED", "STOP", "TRIPPED"})
# The text of a contact state. `invert` flips these, and numbers and booleans. A word
# like OPEN or STOPPED already says what it means, so it is taken as written.
CONTACT_WORDS = frozenset({"TRUE", "FALSE", "1", "0"})


def _text(value: Any) -> Optional[str]:
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return value if isinstance(value, str) else None


def finite(value: Any) -> Optional[float]:
    """value as a finite float, or None: a bool, None, text that isn't a number, NaN,
    inf, or a number too large for a float. Bytes are read as UTF-8 text."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (bytes, bytearray)):
        value = _text(value)
        if value is None:
            return None
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def _on_off(value: Any) -> tuple[Optional[bool], bool]:
    """(state, contact): the on/off state or None, and whether it came from a contact
    (a bool, a number, or its text) rather than a word like OPEN or STOPPED."""
    if value is None:
        return None, False
    if isinstance(value, bool):
        return value, True
    if isinstance(value, numbers.Real):
        x = finite(value)
        return (None if x is None else x != 0), True
    text = _text(value)
    if text is None:
        if isinstance(value, (bytes, bytearray)):
            return None, False
        text = str(value)
    word = text.strip().upper()
    if word in TRUE_WORDS or word in FALSE_WORDS:
        return word in TRUE_WORDS, word in CONTACT_WORDS
    x = finite(word)
    return (None if x is None else x != 0), True


def coerce(signal: str, value: Any) -> Any:
    """A raw device value as the live layer's type for `signal`, or None if it makes no sense.

    Analog signals are finite floats (a bool is not a number here), `running` is a
    bool, `position` is "OPEN" or "CLOSED". On/off words: OPEN, OPENED, ON, TRUE, 1,
    RUNNING, RUN against CLOSED, CLOSE, OFF, FALSE, 0, STOPPED, STOP, TRIPPED; any
    other number is on when nonzero. These are the MQTT and Modbus drivers' rules
    (#62), except that NaN and inf are None for running and position too (a NaN
    contact is not "on"), bytes are read as UTF-8, a number too large for a float is
    None, and a signal the live layer doesn't know is None."""
    if signal in ANALOG:
        return finite(value)
    if signal not in BOOLEAN:
        return None
    on, _ = _on_off(value)
    if on is None:
        return None
    if signal == "position":
        return "OPEN" if on else "CLOSED"
    return on


# ---------- quality ----------

GOOD = "good"                  # the value can be used
UNCERTAIN = "uncertain"        # the device isn't sure, or a 4-20 mA input is saturated
BAD = "bad"                    # unreadable or nonsense, or the device says bad
FAULT = "fault"                # the transmitter signals a fault (NAMUR NE43 current)
OUT_OF_RANGE = "out_of_range"  # outside the rule's range
COMM_LOST = "comm_lost"        # the device or a gateway says the field point is unreachable
STALE = "stale"                # old or future device timestamp; retained, replayed, or historical
NULL = "null"                  # no value at all
QUALITIES = (GOOD, UNCERTAIN, BAD, FAULT, OUT_OF_RANGE, COMM_LOST, STALE, NULL)


@dataclass(frozen=True)
class Mapped:
    """One mapped value. `value` is None unless `quality` is GOOD, whatever it was built with."""

    value: Any
    quality: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.quality not in QUALITIES:
            object.__setattr__(self, "detail", f"unknown quality {self.quality!r}. {self.detail}".strip())
            object.__setattr__(self, "quality", BAD)
        if self.quality != GOOD:
            object.__setattr__(self, "value", None)
        elif self.value is None:
            object.__setattr__(self, "quality", NULL)

    @property
    def good(self) -> bool:
        return self.quality == GOOD


# ---------- rules (config.signal_map) ----------

# NAMUR NE43: a transmitter signals a fault at or below 3.6 mA or at or above 21 mA.
# It measures from 3.8 to 20.5 mA (4-20 plus linear under and over range); between
# the two it is saturated, and the value is not used.
MA_FAULT_LOW = 3.6
MA_FAULT_HIGH = 21.0
MA_MIN = 3.8
MA_MAX = 20.5
MA_SPAN = (4.0, 20.0)

DEFAULT_MAX_AGE_S = 60.0      # oldest device timestamp accepted, unless config says otherwise

M_PER_FT = 0.3048
# Each unit times its factor is the live layer's unit. 1 bbl = 42 US gal; 1 m3 =
# 6.28981 bbl; 1 kPa = 0.145038 psi; 1 bar = 100 kPa; 1 MPa = 1000 kPa.
UNITS = {
    "level_pct": {"pct": 1.0, "ft": None, "in": None, "m": None},   # a length needs a height
    "flow_bbl_hr": {"bbl_hr": 1.0, "gpm": 60.0 / 42.0, "bbl_day": 1.0 / 24.0,
                    "m3_h": 6.28981, "l_s": 22.643316},              # 1 L/s = 3.6 m3/h
    "pressure_psi": {"psi": 1.0, "kpa": 0.145038, "bar": 14.5038, "mpa": 145.038},
    "motor_amps": {"a": 1.0},
}
UNIT_LABEL = {"level_pct": "%", "flow_bbl_hr": "bbl/hr", "pressure_psi": "psi", "motor_amps": "A"}
UNIT_ALIASES = {
    "%": "pct", "percent": "pct",
    "bbl/hr": "bbl_hr", "bbl/h": "bbl_hr", "bph": "bbl_hr", "bbl_h": "bbl_hr",
    "bbl/day": "bbl_day", "bbl/d": "bbl_day", "bpd": "bbl_day", "bbl_d": "bbl_day",
    "gal/min": "gpm", "us_gpm": "gpm",
    "m3/h": "m3_h", "m3/hr": "m3_h", "m3_hr": "m3_h", "m³/h": "m3_h",
    "l/s": "l_s", "lps": "l_s",
    "feet": "ft", "foot": "ft", "inch": "in", "inches": "in",
    "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "amp": "a", "amps": "a", "ampere": "a", "amperes": "a",
}

_ANALOG_KEYS = frozenset({"raw", "eng", "ma420", "unit", "range", "max_age_s"})
RULE_KEYS = {
    "level_pct": _ANALOG_KEYS | {"height_ft", "height_m"},
    "flow_bbl_hr": _ANALOG_KEYS,
    "pressure_psi": _ANALOG_KEYS,
    "motor_amps": _ANALOG_KEYS,
    "running": frozenset({"invert", "max_age_s"}),
    "position": frozenset({"invert", "open_values", "closed_values", "max_age_s"}),
}


@dataclass(frozen=True)
class Rule:
    """One signal's checked rule. Build it with parse_rule(); Rule(signal) changes nothing."""

    signal: str
    raw: Optional[tuple] = None             # (lo, hi) in, scaled linearly to eng
    eng: Optional[tuple] = None             # (lo, hi) out, in `unit`
    ma420: bool = False
    unit: str = ""
    factor: float = 1.0                     # `unit` to the live layer's unit
    range: Optional[tuple] = None           # (lo, hi) in the live layer's unit
    invert: bool = False
    open_values: Optional[frozenset] = None
    closed_values: Optional[frozenset] = None
    max_age_s: Optional[float] = None

    def to_signal(self, x: float) -> float:
        """A number (already times `scale`) through the linear scaling and the unit conversion."""
        if self.raw is not None and self.eng is not None:
            (r0, r1), (e0, e1) = self.raw, self.eng
            x = e0 + (x - r0) * (e1 - e0) / (r1 - r0)
        return x * self.factor


def _number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ValueError(f"{what} must be a number, got {value!r}")
    x = finite(value)
    if x is None:
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return x


def _positive(value: Any, what: str) -> float:
    x = _number(value, what)
    if x <= 0:
        raise ValueError(f"{what} must be above 0, got {value!r}")
    return x


def _pair(value: Any, what: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{what} must be [lo, hi], got {value!r}")
    return _number(value[0], f"{what} lo"), _number(value[1], f"{what} hi")


def _flag(rule: dict, key: str) -> bool:
    value = rule.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false, got {value!r}")
    return value


def _word(value: Any) -> Optional[str]:
    """A value as comparable text: 1, 1.0, "1", and True are all "1"; text is stripped
    and upper case. None when it can't be compared (NaN, None, undecodable bytes)."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (bytes, bytearray)) and _text(value) is None:
        return None
    text = _text(value)
    word = None if text is None else text.strip().upper()
    x = finite(value if word is None else word)    # numbers, Decimal, and number text
    if x is not None:
        return str(int(x)) if x.is_integer() else repr(x)
    return word or None


def _words(value: Any, what: str) -> frozenset:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{what} must be a non-empty list, got {value!r}")
    out = set()
    for v in value:
        w = _word(v)
        if w is None:
            raise ValueError(f"{what} has {v!r}, which isn't a word or a finite number")
        out.add(w)
    return frozenset(out)


def unit_name(unit: Any) -> str:
    """A unit as written in a rule ("m3/h", "kPa", "%") as its name in UNITS ("m3_h", "kpa", "pct")."""
    u = re.sub(r"\s+", "", str(unit).lower())
    return UNIT_ALIASES.get(u, u.replace("/", "_"))


def _unit(signal: str, rule: dict) -> tuple[str, float]:
    table = UNITS[signal]
    given = rule.get("unit")
    unit = next(iter(table)) if given is None else unit_name(given)
    if unit not in table:
        raise ValueError(f"unit {given!r} doesn't fit {signal}; use {', '.join(table)}")
    if signal != "level_pct":
        return unit, table[unit]
    hf = None if rule.get("height_ft") is None else _positive(rule["height_ft"], "height_ft")
    hm = None if rule.get("height_m") is None else _positive(rule["height_m"], "height_m")
    if hf is not None and hm is not None:
        raise ValueError("give height_ft or height_m, not both")
    if unit == "pct":
        if hf is not None or hm is not None:
            raise ValueError("height_ft and height_m are for a level read as a length; set unit to ft, in, or m")
        return unit, 1.0
    if hf is None and hm is None:
        raise ValueError(f"a level in {unit} needs height_ft or height_m: the liquid height that reads 100%")
    if unit == "m":
        return unit, 100.0 / (hm if hm is not None else hf * M_PER_FT)
    height_ft = hf if hf is not None else hm / M_PER_FT
    return unit, 100.0 / (height_ft * (12.0 if unit == "in" else 1.0))


def parse_rule(signal: str, rule: Any) -> Rule:
    """Check one config.signal_map entry and build its Rule. Raises ValueError saying
    what is wrong, so the connector shows unavailable instead of reading wrong numbers.

    Analog signals take: `raw` and `eng` ([lo, hi] each) for linear scaling; `ma420`
    (true for a 4-20 mA input: raw is then 4 to 20, eng is required, and a current in
    NE43's fault or saturation bands gives no value); `unit` (flow: bbl_hr, gpm,
    bbl_day, m3_h, l_s; pressure: psi, kpa, bar, mpa; level: pct, or ft, in, m with
    `height_ft` or `height_m`, the liquid height that reads 100%; motor_amps: a); and
    `range` ([lo, hi] in the live layer's unit, checked last; outside is OUT_OF_RANGE).
    `running` takes `invert`. `position` takes `invert`, or `open_values` and
    `closed_values` together (then any other value is BAD). Every signal takes
    `max_age_s`. Keys starting with "_" are comments; any other key is an error,
    so a typo can't silently switch a check off."""
    if signal not in SIGNALS:
        raise ValueError(f"{signal!r} is not a live signal; use one of {', '.join(SIGNALS)}")
    if not isinstance(rule, dict):
        raise ValueError(f"the rule must be an object, got {rule!r}")
    rule = {k: v for k, v in rule.items() if not str(k).startswith("_")}
    allowed = RULE_KEYS[signal]
    unknown = sorted(str(k) for k in rule if k not in allowed)
    if unknown:
        raise ValueError(f"{', '.join(unknown)} doesn't apply to {signal}; it takes {', '.join(sorted(allowed))}")
    max_age = None if rule.get("max_age_s") is None else _positive(rule["max_age_s"], "max_age_s")

    if signal in BOOLEAN:
        invert = _flag(rule, "invert")
        opens, closes = rule.get("open_values"), rule.get("closed_values")
        if opens is None and closes is None:
            return Rule(signal, invert=invert, max_age_s=max_age)
        if opens is None or closes is None:
            raise ValueError("give both open_values and closed_values")
        if invert:
            raise ValueError("invert doesn't combine with open_values and closed_values; list the values as they read")
        o, c = _words(opens, "open_values"), _words(closes, "closed_values")
        if o & c:
            raise ValueError(f"{', '.join(sorted(o & c))} is in both open_values and closed_values")
        return Rule(signal, open_values=o, closed_values=c, max_age_s=max_age)

    ma420 = _flag(rule, "ma420")
    raw = None if rule.get("raw") is None else _pair(rule["raw"], "raw")
    eng = None if rule.get("eng") is None else _pair(rule["eng"], "eng")
    if ma420:
        if raw is not None and raw != MA_SPAN:
            raise ValueError("ma420 always maps 4 to 20 mA; leave raw out (or make it [4, 20]) and set eng")
        if eng is None:
            raise ValueError("ma420 needs eng [lo, hi]: the values at 4 mA and at 20 mA")
        raw = MA_SPAN
    elif raw is not None and eng is None:
        raise ValueError("raw needs eng [lo, hi]: the values raw lo and raw hi stand for")
    elif eng is not None and raw is None:
        raise ValueError("eng needs raw [lo, hi] (or ma420: true for a 4-20 mA input)")
    if raw is not None and raw[0] == raw[1]:
        raise ValueError("raw lo and hi must differ")
    if eng is not None and eng[0] == eng[1]:
        raise ValueError("eng lo and hi must differ")
    unit, factor = _unit(signal, rule)
    rng = None if rule.get("range") is None else _pair(rule["range"], "range")
    if rng is not None and rng[0] > rng[1]:
        raise ValueError(f"range lo {rng[0]:g} is above hi {rng[1]:g}")
    return Rule(signal, raw=raw, eng=eng, ma420=ma420, unit=unit, factor=factor, range=rng,
                max_age_s=max_age)


def _show(raw: Any) -> str:
    r = repr(raw)
    return r if len(r) <= 40 else r[:37] + "..."


def _amount(value: float, signal: str) -> str:
    label = UNIT_LABEL[signal]
    return f"{value:.4g}{label}" if label == "%" else f"{value:.4g} {label}"


def map_value(signal: str, raw: Any, rule: Optional[Rule] = None, *, scale: float = 1.0,
              quality: Any = GOOD, age_s: Any = None, max_age_s: Optional[float] = None) -> Mapped:
    """Map one raw device value to the live layer's value for `signal`.

    In order: the device's own quality (anything but GOOD wins), the device
    timestamp's age, a missing value, then for an analog signal a finite number, times
    `scale`, the 4-20 mA checks, the linear scaling, the unit conversion, and the
    range; for running and position the open and closed values, else on/off words
    and numbers, then invert. `age_s` is seconds since the device stamped the value,
    when it does: older than max_age_s (DEFAULT_MAX_AGE_S when None), or that far in
    the future, is STALE. Never raises."""
    if signal not in SIGNALS:
        return Mapped(None, BAD, f"{signal!r} is not a live signal")
    q = quality.strip().lower() if isinstance(quality, str) else quality
    if q != GOOD:
        if q in QUALITIES:
            return Mapped(None, q, f"the device reports {q}")
        return Mapped(None, BAD, f"the device reports quality {_show(quality)}")
    if age_s is not None:
        limit = DEFAULT_MAX_AGE_S if max_age_s is None else max_age_s
        age = finite(age_s)
        if age is None:
            return Mapped(None, STALE, f"the device timestamp is unreadable ({_show(age_s)})")
        if age > limit:
            return Mapped(None, STALE, f"the device stamped it {age:.0f} s ago (limit {limit:g} s)")
        if age < -limit:
            return Mapped(None, STALE, f"the device clock is {-age:.0f} s ahead (limit {limit:g} s)")
    if raw is None:
        return Mapped(None, NULL, "no value")
    rule = rule or Rule(signal)
    if rule.signal != signal:
        return Mapped(None, BAD, f"the rule is for {rule.signal}, not {signal}")
    if signal in ANALOG:
        return _map_analog(signal, raw, rule, scale)
    return _map_on_off(signal, raw, rule)


def _map_analog(signal: str, raw: Any, rule: Rule, scale: Any) -> Mapped:
    x = finite(raw)
    if x is None:
        return Mapped(None, BAD, f"{_show(raw)} is not a finite number")
    k = finite(scale)
    if not k:
        return Mapped(None, BAD, f"scale {_show(scale)} is not a nonzero finite number")
    x *= k
    if not math.isfinite(x):
        return Mapped(None, BAD, f"{_show(raw)} times scale {k:g} is not a finite number")
    if rule.ma420:
        if x <= MA_FAULT_LOW or x >= MA_FAULT_HIGH:
            return Mapped(None, FAULT, f"{x:.2f} mA: the transmitter signals a fault (NAMUR NE43: at or below "
                                       f"{MA_FAULT_LOW:g} mA, or at or above {MA_FAULT_HIGH:g} mA)")
        if x < MA_MIN or x > MA_MAX:
            edge = MA_MIN if x < MA_MIN else MA_MAX
            return Mapped(None, UNCERTAIN, f"{x:.2f} mA is saturated (NAMUR NE43); clamped to {edge:g} mA it reads "
                                           f"{_amount(rule.to_signal(edge), signal)}, not used")
    v = rule.to_signal(x)
    if not math.isfinite(v):
        return Mapped(None, BAD, f"{_show(raw)} scales to a number that isn't finite")
    if rule.range is not None and not rule.range[0] <= v <= rule.range[1]:
        lo, hi = rule.range
        return Mapped(None, OUT_OF_RANGE, f"{_amount(v, signal)} is outside its range, {lo:g} to {hi:g}")
    return Mapped(v, GOOD)


def _map_on_off(signal: str, raw: Any, rule: Rule) -> Mapped:
    if rule.open_values is not None and rule.closed_values is not None:
        word = _word(raw)
        if word is not None and word in rule.open_values:
            return Mapped("OPEN", GOOD)
        if word is not None and word in rule.closed_values:
            return Mapped("CLOSED", GOOD)
        return Mapped(None, BAD, f"{_show(raw)} is in neither open_values nor closed_values")
    on, contact = _on_off(raw)
    if on is None:
        return Mapped(None, BAD, f"{_show(raw)} is not an on/off value")
    if rule.invert and contact:
        on = not on
    if signal == "position":
        return Mapped("OPEN" if on else "CLOSED", GOOD)
    return Mapped(on, GOOD)


class SignalMap:
    """The value rules for one connector spec: each declared signal's `scale` and its
    config.signal_map rule, checked at construction (ValueError says what is wrong).

        smap = SignalMap(spec)                                # in the connector's __init__
        inbox.put("TANK_3", "level_pct", smap.apply("TANK_3", "level_pct", 12.0))   # I/O thread

    config.signal_map is keyed "ASSET_ID.signal". A key for a signal the connector
    doesn't declare is an error, not ignored: a typo there would leave the real signal
    unscaled. `scale` multiplies the raw number first (so it can turn register counts
    into mA), then the rule runs. `scale` is ignored for running and position.
    config.max_age_s (default DEFAULT_MAX_AGE_S) is the oldest device timestamp
    accepted; a rule's own max_age_s overrides it."""

    def __init__(self, spec: dict) -> None:
        config = spec.get("config") or {}
        if not isinstance(config, dict):
            raise ValueError("config must be an object")
        self.scales: dict[tuple, float] = {}
        for s in spec.get("signals") or []:
            if not isinstance(s, dict) or not s.get("asset_id") or not s.get("signal"):
                continue
            aid, sig = s["asset_id"], s["signal"]
            if sig not in SIGNALS:
                raise ValueError(f"signals: {aid}.{sig} is not a live signal; use one of {', '.join(SIGNALS)}")
            scale = 1.0
            if sig in ANALOG and s.get("scale") is not None:
                scale = _number(s["scale"], f"signals: {aid}.{sig} scale")
                if scale == 0:
                    raise ValueError(f"signals: {aid}.{sig} scale must not be 0")
            self.scales[(aid, sig)] = scale
        smap = config.get("signal_map")
        if smap is None:
            smap = {}
        if not isinstance(smap, dict):
            raise ValueError("config signal_map must be an object keyed by ASSET_ID.signal")
        self.rules: dict[tuple, Rule] = {}
        for key, rule in smap.items():
            if str(key).startswith("_"):
                continue
            aid, dot, sig = str(key).partition(".")
            if not (aid and dot and sig):
                raise ValueError(f"signal_map key {key!r} must look like ASSET_ID.signal, for example TANK_3.level_pct")
            if (aid, sig) not in self.scales:
                raise ValueError(f"signal_map {key}: this connector doesn't declare {key} in its signals")
            try:
                self.rules[(aid, sig)] = parse_rule(sig, rule)
            except ValueError as e:
                raise ValueError(f"signal_map {key}: {e}") from None
        age = config.get("max_age_s")
        self.max_age_s = DEFAULT_MAX_AGE_S if age is None else _positive(age, "config max_age_s")

    def apply(self, asset_id: str, signal: str, raw: Any, *, quality: Any = GOOD,
              age_s: Any = None) -> Mapped:
        """map_value() with this signal's scale, rule, and age limit. A signal the
        connector doesn't declare is BAD. Never raises."""
        key = (asset_id, signal)
        if key not in self.scales:
            return Mapped(None, BAD, f"{asset_id}.{signal} is not a signal this connector declares")
        rule = self.rules.get(key)
        limit = rule.max_age_s if rule is not None and rule.max_age_s is not None else self.max_age_s
        return map_value(signal, raw, rule, scale=self.scales[key], quality=quality, age_s=age_s,
                         max_age_s=limit)


# ---------- between the I/O thread and read() ----------

class Inbox:
    """The newest value per (asset, signal) since the last drain(), shared by the
    connector's I/O thread (put) and read() (drain). Thread-safe; nothing waits long.

    A good value replaces whatever was waiting. Anything else removes the waiting
    value, so read() never hands on a good value older than a bad one. With
    none_readings=True it queues None instead, so the asset shows UNKNOWN at once;
    leave that off until detect.py skips None values (see the module docstring)."""

    def __init__(self, none_readings: bool = False) -> None:
        self.none_readings = none_readings
        self._lock = threading.Lock()
        self._items: dict[tuple, Any] = {}

    def put(self, asset_id: str, signal: str, mapped: Mapped) -> None:
        key = (asset_id, signal)
        with self._lock:
            if mapped.good:
                self._items[key] = mapped.value
            elif self.none_readings:
                self._items[key] = None
            else:
                self._items.pop(key, None)

    def clear(self) -> None:
        """Drop everything waiting, for a connector that wants nothing from before a lost link."""
        with self._lock:
            self._items = {}

    def drain(self) -> list[tuple[str, str, Any]]:
        """[(asset_id, signal, value)] waiting since the last drain, and empty the inbox."""
        with self._lock:
            items, self._items = self._items, {}
        return [(aid, sig, value) for (aid, sig), value in items.items()]


# ---------- reconnects and shutdown ----------

class Backoff:
    """Delays between reconnect attempts: initial_s, then `factor` times longer after
    each failure, up to max_s, each moved up or down by up to `jitter` (a fraction) so
    connectors that lost the same network don't all retry at once. Never above max_s.
    reset() once a connection works. One per connection loop, on that loop's thread."""

    def __init__(self, initial_s: float = 1.0, max_s: float = 30.0, factor: float = 2.0,
                 jitter: float = 0.2, rng: Optional[random.Random] = None) -> None:
        self.initial_s = _positive(initial_s, "initial_s")
        self.max_s = _number(max_s, "max_s")
        if self.max_s < self.initial_s:
            raise ValueError(f"max_s ({max_s!r}) must be at least initial_s ({initial_s!r})")
        self.factor = _number(factor, "factor")
        if self.factor < 1:
            raise ValueError(f"factor must be at least 1, got {factor!r}")
        self.jitter = _number(jitter, "jitter")
        if not 0 <= self.jitter < 1:
            raise ValueError(f"jitter must be at least 0 and below 1, got {jitter!r}")
        self._rng = rng or random.Random()
        self._base = self.initial_s
        self.attempts = 0

    def next_delay(self) -> float:
        """Seconds to wait before the next attempt."""
        base = self._base
        self._base = min(self.max_s, base * self.factor)   # stops growing at the cap: no overflow
        self.attempts += 1
        return min(self.max_s, base * (1.0 + self._rng.uniform(-self.jitter, self.jitter)))

    def reset(self) -> None:
        self._base = self.initial_s
        self.attempts = 0


def close_later(fn: Callable[[], Any], name: str = "freeboard-close") -> threading.Thread:
    """Run fn on a daemon thread and return at once, so close() never blocks the event
    loop on a socket or a thread join. Returns the thread. An exception from fn is
    swallowed and not logged: closing a dead device must never raise, and the message
    could carry a URL with credentials in it."""
    def run() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass
    t = threading.Thread(target=run, name=name, daemon=True)
    t.start()
    return t


# ---------- commands ----------

UNCERTAIN_DETAIL = "sent; delivery not confirmed - check the field before retrying"


def uncertain_result() -> dict:
    """send()'s answer when the write left but its acknowledgement never came (it timed
    out, or the link dropped while waiting). The device may have acted, so nothing
    sends it again: a person checks the field first."""
    return {"ok": False, "uncertain": True, "detail": UNCERTAIN_DETAIL}


COMMAND_WORDS = {"SET_VALVE": {"OPEN": True, "CLOSED": False}, "SET_PUMP": {"ON": True, "OFF": False}}


def normalize(command: Any) -> tuple[str, str, Any]:
    """A confirmed command as (type, asset_id, value) for a protocol write.

    SET_VALVE OPEN or CLOSED gives True or False, SET_PUMP ON or OFF gives True or
    False, and CHOKE_WELL gives the fraction kept as a float, from 0 (shut in) to 1
    (full flow). Anything else raises ValueError saying what was wrong, and the
    connector sends nothing. SEND_TRUCK is a dispatch the simulator handles; no field
    connector sends it."""
    if not isinstance(command, dict):
        raise ValueError(f"a command must be an object with type, asset_id, and value, got {_show(command)}")
    typ, aid, value = command.get("type"), command.get("asset_id"), command.get("value")
    name = typ.strip().upper() if isinstance(typ, str) else ""
    if name == "SEND_TRUCK":
        raise ValueError("SEND_TRUCK is a truck dispatch for the simulator; no field connector sends it")
    if name not in COMMAND_WORDS and name != "CHOKE_WELL":
        raise ValueError(f"unknown command type {typ!r}; a field connector sends SET_VALVE, SET_PUMP, or CHOKE_WELL")
    if not isinstance(aid, str) or not aid.strip():
        raise ValueError(f"{name} needs an asset_id, got {aid!r}")
    if name in COMMAND_WORDS:
        words = COMMAND_WORDS[name]
        word = value.strip().upper() if isinstance(value, str) else None
        if word not in words:
            raise ValueError(f"{name} takes {' or '.join(words)}, got {value!r}; nothing was sent")
        return name, aid, words[word]
    x = None if isinstance(value, bool) or not isinstance(value, numbers.Real) else finite(value)
    if x is None or not 0.0 <= x <= 1.0:
        raise ValueError(f"CHOKE_WELL takes a fraction from 0 (shut in) to 1 (full flow), got {value!r}; "
                         f"nothing was sent")
    return name, aid, x
