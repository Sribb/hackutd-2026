"""Connector foundation (#126): secrets, value rules (NAMUR NE43, units, range), quality,
the inbox, backoff, close_later, and command normalization. Pure unit tests: no network."""
import logging
import random
import threading
import time
from decimal import Decimal

import pytest

from backend.app.connectors import KINDS, Registry
from backend.app.connectors import signalmap as sm
from backend.app.connectors.base import Connector
from backend.app.connectors.signalmap import (
    ANALOG, BAD, BOOLEAN, COMM_LOST, DEFAULT_MAX_AGE_S, ENV_PREFIX, FAULT, GOOD, NULL, OUT_OF_RANGE,
    QUALITIES, RULE_KEYS, STALE, UNCERTAIN, UNCERTAIN_DETAIL, UNITS, Backoff, Inbox, Mapped, Rule,
    SignalMap, check_env_names, close_later, coerce, env_secret, import_optional, map_value,
    missing_env, normalize, parse_rule, uncertain_result)
from backend.app.fieldmap import SIGNALS, FieldMap
from backend.app.scenario import load_scenario

EXACT = 1e-12          # relative tolerance for "exact" conversion factors (float rounding only)
NAN, INF = float("nan"), float("inf")
SECRET = "hunter2-SECRET-do-not-show"


def rule(signal: str, **kw) -> Rule:
    return parse_rule(signal, kw)


def spec(signals: list, **config) -> dict:
    return {"id": "CONN_TEST", "kind": "foundation_test", "config": config, "signals": signals}


def sig(asset_id: str, signal: str, **kw) -> dict:
    return {"asset_id": asset_id, "signal": signal, "address": f"{asset_id}/{signal}", **kw}


# ---------- the signal list stays in step with the live layer ----------

def test_analog_and_boolean_split_the_live_signals():
    assert set(ANALOG) | set(BOOLEAN) == set(SIGNALS)
    assert not set(ANALOG) & set(BOOLEAN)
    assert set(UNITS) == set(ANALOG)
    assert set(RULE_KEYS) == set(SIGNALS)


# ---------- secrets ----------

def test_env_prefix():
    assert ENV_PREFIX == "FREEBOARD_CONN_"


def test_check_env_names_accepts_freeboard_conn_names_and_unset():
    check_env_names({"password_env": "FREEBOARD_CONN_MQTT_PASSWORD", "token_env": None, "cert_env": "",
                     "port": 1883, "host": "127.0.0.1", "tls": {"key_password_env": "FREEBOARD_CONN_TLS_KEY"},
                     "brokers": [{"password_env": "FREEBOARD_CONN_B2"}, "plain", 5]})
    check_env_names({})
    check_env_names(None)


@pytest.mark.parametrize("bad", [
    "ANTHROPIC_API_KEY", "HOME", "PATH", "FREEBOARD_TOKEN", "OPERATOR_PIN",
    "FREEBOARD_CONN_",            # the prefix alone names nothing
    "freeboard_conn_x",           # lower case is a different name
    "FREEBOARD_CONN_A-B", "FREEBOARD_CONN_A B", " FREEBOARD_CONN_X", "FREEBOARD_CONN_X\n",
    "XFREEBOARD_CONN_X", 5, True, ["FREEBOARD_CONN_X"], {"name": "FREEBOARD_CONN_X"},
])
def test_check_env_names_rejects_anything_else(bad):
    with pytest.raises(ValueError, match="password_env"):
        check_env_names({"password_env": bad})


def test_env_name_error_names_the_key_and_never_the_value():
    with pytest.raises(ValueError) as e:
        check_env_names({"password_env": SECRET})
    msg = str(e.value)
    assert "password_env" in msg and "FREEBOARD_CONN_PASSWORD" in msg   # names the key, suggests a name
    assert SECRET not in msg and "hunter2" not in msg


def test_env_name_error_names_nested_keys():
    with pytest.raises(ValueError, match=r"tls\.key_password_env"):
        check_env_names({"tls": {"key_password_env": "TLS_PASSWORD"}})
    with pytest.raises(ValueError, match=r"brokers\[1\]\.password_env"):
        check_env_names({"brokers": [{"password_env": "FREEBOARD_CONN_OK"}, {"password_env": "HOME"}]})


def test_env_secret_reads_only_freeboard_conn_variables(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("FREEBOARD_CONN_TEST_TOKEN", SECRET)
    monkeypatch.setenv("NOT_FREEBOARD_SECRET", SECRET)
    monkeypatch.delenv("FREEBOARD_CONN_TEST_UNSET", raising=False)
    assert env_secret({"token_env": "FREEBOARD_CONN_TEST_TOKEN"}, "token") == SECRET
    assert env_secret({"token_env": "FREEBOARD_CONN_TEST_UNSET"}, "token") is None
    # Set in the environment, but not a FREEBOARD_CONN_ name: never read, even if the load check was skipped.
    assert env_secret({"token_env": "NOT_FREEBOARD_SECRET"}, "token") is None
    assert env_secret({"token_env": "PATH"}, "token") is None
    assert env_secret({"token_env": 5}, "token") is None
    assert env_secret({"token_env": None}, "token") is None
    assert env_secret({}, "token") is None
    assert env_secret(None, "token") is None
    assert SECRET not in caplog.text


def test_missing_env_returns_the_name_only(monkeypatch):
    monkeypatch.setenv("FREEBOARD_CONN_TEST_SET", SECRET)
    monkeypatch.delenv("FREEBOARD_CONN_TEST_UNSET", raising=False)
    assert missing_env({"password_env": "FREEBOARD_CONN_TEST_SET"}) is None
    assert missing_env({"password_env": "FREEBOARD_CONN_TEST_SET",
                        "token_env": "FREEBOARD_CONN_TEST_UNSET"}) == "FREEBOARD_CONN_TEST_UNSET"
    assert missing_env({"token_env": None, "host": "x"}) is None
    assert missing_env(None) is None


class FoundationConnector(Connector):
    """What a new connector does in __init__: check the env names, build its SignalMap."""

    kind = "foundation_test"

    def __init__(self, spec, fmap, now):
        super().__init__(spec, fmap, now)
        check_env_names(self.config)
        self.smap = SignalMap(spec)


@pytest.fixture
def registry_with(monkeypatch):
    monkeypatch.setitem(KINDS, "foundation_test", FoundationConnector)
    fmap = FieldMap.from_scenario(load_scenario())
    made = []

    def build(conn_spec: dict) -> dict:
        reg = Registry([conn_spec], fmap, lambda: 0.0)
        made.append(reg)
        (row,) = [r for r in reg.status() if r["id"] == conn_spec["id"]]
        return row

    yield build
    for reg in made:
        reg.close()


def test_registry_shows_a_bad_env_name_as_unavailable_with_the_reason(registry_with):
    row = registry_with(spec([sig("TANK_3", "level_pct")], host="127.0.0.1", password_env=SECRET))
    assert row["status"] == "unavailable"
    assert row["detail"].startswith("bad config:") and "password_env" in row["detail"]
    assert ENV_PREFIX in row["detail"] and SECRET not in row["detail"]


def test_registry_shows_a_bad_signal_rule_as_unavailable_with_the_reason(registry_with):
    row = registry_with(spec([sig("TANK_3", "level_pct")],
                             signal_map={"TANK_3.level_pct": {"ma420": True}}))
    assert row["status"] == "unavailable"
    assert "signal_map TANK_3.level_pct" in row["detail"] and "eng" in row["detail"]
    ok = registry_with(spec([sig("TANK_3", "level_pct")], password_env="FREEBOARD_CONN_TEST_PW",
                            signal_map={"TANK_3.level_pct": {"ma420": True, "eng": [0, 100]}}))
    assert ok["status"] == "connected"


# ---------- optional libraries ----------

def test_import_optional_returns_the_module():
    mod, why = import_optional("json", "json")
    assert why is None and mod is not None and hasattr(mod, "dumps")


def test_import_optional_says_what_to_install():
    mod, why = import_optional("freeboard_no_such_module_126", "no-such-pkg")
    assert mod is None
    assert "no-such-pkg is not installed" in why and "pip install no-such-pkg" in why


def test_import_optional_survives_a_broken_install(monkeypatch):
    def boom(name):
        raise RuntimeError("compiled extension is the wrong version")
    monkeypatch.setattr(sm.importlib, "import_module", boom)
    mod, why = import_optional("asyncua", "asyncua")
    assert mod is None
    assert "asyncua" in why and "failed to import" in why and "wrong version" in why


# ---------- coerce: the drivers' rules ----------

COERCE = [
    # analog: finite floats only
    ("level_pct", "72.4", 72.4), ("level_pct", 72, 72.0), ("level_pct", 72.4, 72.4),
    ("level_pct", " 72.4 ", 72.4), ("level_pct", b"72.4", 72.4), ("flow_bbl_hr", Decimal("1.5"), 1.5),
    ("pressure_psi", "-3", -3.0), ("motor_amps", "1e2", 100.0),
    ("level_pct", True, None), ("level_pct", False, None), ("level_pct", None, None),
    ("level_pct", "abc", None), ("level_pct", "", None), ("level_pct", [1], None),
    ("level_pct", NAN, None), ("level_pct", INF, None), ("pressure_psi", -INF, None),
    ("motor_amps", "NaN", None), ("flow_bbl_hr", "inf", None), ("flow_bbl_hr", "-Infinity", None),
    ("level_pct", 10 ** 400, None), ("level_pct", Decimal("NaN"), None), ("level_pct", Decimal("sNaN"), None),
    ("level_pct", b"\xff\xfe", None),
    # running: on/off words, numbers nonzero is on
    ("running", True, True), ("running", False, False), ("running", 1, True), ("running", 0, False),
    ("running", "1", True), ("running", "0", False), ("running", "ON", True), ("running", "off", False),
    ("running", "Running", True), ("running", "RUN", True), ("running", "stopped", False),
    ("running", "STOP", False), ("running", "TRIPPED", False), ("running", "true", True),
    ("running", "2", True), ("running", -1, True), ("running", 0.0, False), ("running", b"ON", True),
    ("running", Decimal("1"), True),
    ("running", NAN, None), ("running", INF, None), ("running", "nan", None), ("running", "-inf", None),
    ("running", "maybe", None), ("running", "", None), ("running", None, None), ("running", b"\xff", None),
    ("running", 10 ** 400, None),
    # position: OPEN or CLOSED
    ("position", "OPEN", "OPEN"), ("position", "closed", "CLOSED"), ("position", "opened", "OPEN"),
    ("position", "close", "CLOSED"), ("position", 1, "OPEN"), ("position", 0, "CLOSED"),
    ("position", True, "OPEN"), ("position", False, "CLOSED"), ("position", " Open\n", "OPEN"),
    ("position", "ajar", None), ("position", NAN, None), ("position", None, None),
    # not a live signal
    ("phase", "EN_ROUTE", None), ("bogus", 1, None),
]


@pytest.mark.parametrize("signal,raw,want", COERCE)
def test_coerce(signal, raw, want):
    got = coerce(signal, raw)
    if isinstance(want, float):
        assert type(got) is float and got == pytest.approx(want)
    else:
        assert got is want or got == want
        assert type(got) is type(want)


@pytest.mark.parametrize("signal,raw,want", [c for c in COERCE if c[2] is not None and c[0] in SIGNALS])
def test_map_value_without_a_rule_matches_coerce(signal, raw, want):
    m = map_value(signal, raw)
    assert m.good and m.value == want


# ---------- quality ----------

def test_quality_constants():
    assert (GOOD, UNCERTAIN, BAD, FAULT, OUT_OF_RANGE, COMM_LOST, STALE, NULL) == (
        "good", "uncertain", "bad", "fault", "out_of_range", "comm_lost", "stale", "null")
    assert set(QUALITIES) == {GOOD, UNCERTAIN, BAD, FAULT, OUT_OF_RANGE, COMM_LOST, STALE, NULL}
    assert len(QUALITIES) == 8


@pytest.mark.parametrize("quality", [q for q in QUALITIES if q != GOOD])
def test_anything_not_good_has_no_value(quality):
    m = Mapped(72.4, quality, "x")
    assert m.value is None and m.quality == quality and not m.good


def test_mapped_keeps_good_values_and_fixes_impossible_ones():
    assert Mapped(72.4, GOOD).value == 72.4 and Mapped(72.4, GOOD).good
    assert Mapped(None, GOOD).quality == NULL                  # good with no value is no value
    weird = Mapped(72.4, "great")
    assert weird.quality == BAD and weird.value is None and "great" in weird.detail


@pytest.mark.parametrize("quality", [UNCERTAIN, BAD, FAULT, COMM_LOST, STALE, NULL, OUT_OF_RANGE])
def test_device_quality_wins_over_a_good_looking_value(quality):
    m = map_value("level_pct", 55.0, quality=quality)
    assert m.value is None and m.quality == quality


def test_device_quality_spelling():
    assert map_value("level_pct", 55.0, quality=" Good ").good
    for q in ("weird", None, 0, "OK"):
        m = map_value("level_pct", 55.0, quality=q)
        assert m.quality == BAD and m.value is None


def test_no_value_is_null():
    for signal in SIGNALS:
        m = map_value(signal, None)
        assert m.quality == NULL and m.value is None


def test_unknown_signal_or_wrong_rule_is_bad():
    assert map_value("phase", "EN_ROUTE").quality == BAD
    assert map_value("flow_bbl_hr", 5.0, Rule("level_pct")).quality == BAD


# ---------- device timestamps ----------

def test_age_limit_default_and_edges():
    assert DEFAULT_MAX_AGE_S == 60.0
    assert map_value("level_pct", 55.0, age_s=0).good
    assert map_value("level_pct", 55.0, age_s=59.9).good
    assert map_value("level_pct", 55.0, age_s=60.0).good          # at the limit is still accepted
    old = map_value("level_pct", 55.0, age_s=60.1)
    assert old.quality == STALE and old.value is None and "60" in old.detail
    assert map_value("level_pct", 55.0, age_s=-59).good             # small clock skew
    ahead = map_value("level_pct", 55.0, age_s=-61)
    assert ahead.quality == STALE and "ahead" in ahead.detail
    for unreadable in (NAN, INF, "soon", True):
        assert map_value("level_pct", 55.0, age_s=unreadable).quality == STALE
    assert map_value("running", True, age_s=3600).quality == STALE   # booleans too
    assert map_value("level_pct", 55.0, age_s=300, max_age_s=600).good


def test_age_limit_from_config_and_rule():
    smap = SignalMap(spec([sig("TANK_3", "level_pct"), sig("TANK_4", "level_pct")], max_age_s=5,
                          signal_map={"TANK_4.level_pct": {"max_age_s": 900}}))
    assert smap.max_age_s == 5
    assert smap.apply("TANK_3", "level_pct", 50, age_s=4).good
    assert smap.apply("TANK_3", "level_pct", 50, age_s=6).quality == STALE
    assert smap.apply("TANK_4", "level_pct", 50, age_s=600).good
    assert smap.apply("TANK_4", "level_pct", 50, age_s=901).quality == STALE
    assert SignalMap(spec([sig("TANK_3", "level_pct")])).max_age_s == DEFAULT_MAX_AGE_S
    for bad in (0, -1, "60", NAN, True):
        with pytest.raises(ValueError, match="max_age_s"):
            SignalMap(spec([sig("TANK_3", "level_pct")], max_age_s=bad))
    with pytest.raises(ValueError, match="max_age_s"):
        parse_rule("level_pct", {"max_age_s": 0})


# ---------- NAMUR NE43 4-20 mA ----------

LEVEL_420 = rule("level_pct", ma420=True, eng=[0, 100])

NE43 = [
    (0.0, FAULT), (2.0, FAULT), (3.59, FAULT),
    (3.6, FAULT),          # NE43 failure signal is at or below 3.6 mA
    (3.61, UNCERTAIN), (3.7, UNCERTAIN), (3.79, UNCERTAIN),
    (3.8, GOOD), (3.9, GOOD), (4.0, GOOD), (12.0, GOOD), (20.0, GOOD), (20.3, GOOD), (20.5, GOOD),
    (20.51, UNCERTAIN), (20.7, UNCERTAIN), (20.99, UNCERTAIN),
    (21.0, FAULT),         # and at or above 21 mA
    (21.01, FAULT), (22.5, FAULT), (24.0, FAULT),
]


@pytest.mark.parametrize("ma,quality", NE43)
def test_ne43_edges(ma, quality):
    m = map_value("level_pct", ma, LEVEL_420)
    assert m.quality == quality
    if quality == GOOD:
        assert m.value == pytest.approx((ma - 4.0) / 16.0 * 100.0)   # linear, under and over range included
    else:
        assert m.value is None


def test_ne43_saturation_says_the_clamped_value():
    high = map_value("level_pct", 20.7, LEVEL_420)
    assert high.quality == UNCERTAIN and "clamped to 20.5 mA" in high.detail and "103.1%" in high.detail
    low = map_value("level_pct", 3.7, LEVEL_420)
    assert low.quality == UNCERTAIN and "clamped to 3.8 mA" in low.detail and "-1.25%" in low.detail
    fault = map_value("level_pct", 3.5, LEVEL_420)
    assert "NE43" in fault.detail and "3.50 mA" in fault.detail


def test_ne43_reverse_acting_and_other_units():
    rev = rule("level_pct", ma420=True, eng=[100, 0])
    assert map_value("level_pct", 4, rev).value == pytest.approx(100)
    assert map_value("level_pct", 20, rev).value == pytest.approx(0)
    assert map_value("level_pct", 12, rev).value == pytest.approx(50)
    flow = rule("flow_bbl_hr", ma420=True, eng=[0, 500], unit="gpm")
    assert map_value("flow_bbl_hr", 12, flow).value == pytest.approx(250 * 60 / 42, rel=EXACT)
    psi = rule("pressure_psi", ma420=True, eng=[0, 10], unit="bar")
    assert map_value("pressure_psi", 20, psi).value == pytest.approx(145.038, rel=EXACT)
    assert map_value("pressure_psi", 21.5, psi).quality == FAULT


def test_ne43_reads_the_current_after_the_signal_scale():
    # A register in microamps: scale 0.001 turns it into mA before the NE43 checks.
    smap = SignalMap(spec([sig("TANK_3", "level_pct", scale=0.001)],
                          signal_map={"TANK_3.level_pct": {"ma420": True, "eng": [0, 100]}}))
    assert smap.apply("TANK_3", "level_pct", 12000).value == pytest.approx(50)
    assert smap.apply("TANK_3", "level_pct", 3500).quality == FAULT
    assert smap.apply("TANK_3", "level_pct", 20700).quality == UNCERTAIN
    assert smap.apply("TANK_3", "level_pct", 0).quality == FAULT        # an open loop reads 0 mA


def test_ne43_with_a_range():
    r = rule("level_pct", ma420=True, eng=[0, 100], range=[0, 100])
    assert map_value("level_pct", 12, r).value == pytest.approx(50)
    assert map_value("level_pct", 3.8, r).quality == OUT_OF_RANGE     # -1.25% is a good current, out of range
    assert map_value("level_pct", 20.5, r).quality == OUT_OF_RANGE


def test_ma420_config_errors():
    with pytest.raises(ValueError, match="eng"):
        parse_rule("level_pct", {"ma420": True})
    with pytest.raises(ValueError, match="4 to 20"):
        parse_rule("level_pct", {"ma420": True, "raw": [0, 20], "eng": [0, 100]})
    assert parse_rule("level_pct", {"ma420": True, "raw": [4, 20], "eng": [0, 100]}).ma420
    with pytest.raises(ValueError, match="true or false"):
        parse_rule("level_pct", {"ma420": "yes", "eng": [0, 100]})
    assert not parse_rule("level_pct", {"ma420": False}).ma420


# ---------- linear scaling ----------

def test_linear_scaling_without_ma420():
    r = rule("level_pct", raw=[0, 4095], eng=[0, 100])                  # a 12-bit input card
    assert map_value("level_pct", 0, r).value == pytest.approx(0)
    assert map_value("level_pct", 4095, r).value == pytest.approx(100)
    assert map_value("level_pct", 2047.5, r).value == pytest.approx(50)
    s7 = rule("flow_bbl_hr", raw=[0, 27648], eng=[0, 300])                # a Siemens S7 analog word
    assert map_value("flow_bbl_hr", 13824, s7).value == pytest.approx(150)
    offset = rule("pressure_psi", raw=[1, 5], eng=[0, 200])               # 1-5 V
    assert map_value("pressure_psi", 3, offset).value == pytest.approx(100)


@pytest.mark.parametrize("bad,match", [
    ({"raw": [0, 100]}, "eng"), ({"eng": [0, 100]}, "raw"),
    ({"raw": [5, 5], "eng": [0, 100]}, "differ"), ({"raw": [0, 10], "eng": [3, 3]}, "differ"),
    ({"raw": [0], "eng": [0, 100]}, r"\[lo, hi\]"), ({"raw": "0-10", "eng": [0, 100]}, r"\[lo, hi\]"),
    ({"raw": ["0", "10"], "eng": [0, 100]}, "number"), ({"raw": [0, NAN], "eng": [0, 100]}, "finite"),
    ({"raw": [0, True], "eng": [0, 100]}, "number"),
])
def test_linear_scaling_config_errors(bad, match):
    with pytest.raises(ValueError, match=match):
        parse_rule("level_pct", bad)


# ---------- unit conversion ----------

@pytest.mark.parametrize("unit,raw,want", [
    ("bbl_hr", 150, 150.0),
    ("gpm", 42, 60.0),               # 42 gal/min is 1 bbl/min, 60 bbl/hr (1 bbl = 42 US gal)
    ("gpm", 1, 60 / 42),
    ("bbl_day", 24, 1.0),
    ("bbl_day", 3600, 150.0),
    ("m3_h", 1, 6.28981),            # 1 m3 = 6.28981 bbl
    ("m3_h", 10, 62.8981),
    ("l_s", 1, 3.6 * 6.28981),       # 1 L/s = 3.6 m3/h
    ("l_s", 0.5, 1.8 * 6.28981),
])
def test_flow_units(unit, raw, want):
    assert map_value("flow_bbl_hr", raw, rule("flow_bbl_hr", unit=unit)).value == pytest.approx(want, rel=EXACT)


@pytest.mark.parametrize("unit,raw,want", [
    ("psi", 100, 100.0),
    ("kpa", 1, 0.145038),            # 1 kPa = 0.145038 psi
    ("kpa", 100, 14.5038),
    ("bar", 1, 14.5038),             # 1 bar = 14.5038 psi
    ("bar", 10, 145.038),
    ("mpa", 1, 145.038),             # 1 MPa = 1000 kPa
    ("mpa", 0.5, 72.519),
])
def test_pressure_units(unit, raw, want):
    assert map_value("pressure_psi", raw, rule("pressure_psi", unit=unit)).value == pytest.approx(want, rel=EXACT)


def test_unit_factors_agree_with_each_other():
    flow, p = UNITS["flow_bbl_hr"], UNITS["pressure_psi"]
    assert flow["gpm"] == 60 / 42 and flow["bbl_day"] == 1 / 24 and flow["m3_h"] == 6.28981
    assert flow["l_s"] == pytest.approx(3.6 * flow["m3_h"], rel=EXACT)
    assert p["kpa"] == 0.145038 and p["bar"] == 14.5038
    assert p["bar"] == pytest.approx(100 * p["kpa"], rel=EXACT)
    assert p["mpa"] == pytest.approx(1000 * p["kpa"], rel=EXACT)


@pytest.mark.parametrize("given,raw,want", [
    ({"unit": "pct"}, 72.5, 72.5),
    ({}, 72.5, 72.5),
    ({"unit": "ft", "height_ft": 20}, 15, 75.0),
    ({"unit": "in", "height_ft": 20}, 180, 75.0),
    ({"unit": "m", "height_m": 6.096}, 4.572, 75.0),
    ({"unit": "ft", "height_m": 6.096}, 15, 75.0),       # 20 ft = 6.096 m exactly
    ({"unit": "m", "height_ft": 20}, 4.572, 75.0),
    ({"unit": "in", "height_m": 6.096}, 180, 75.0),
    ({"unit": "ft", "height_ft": 16}, 16, 100.0),
    ({"unit": "ft", "height_ft": 16}, 17, 106.25),      # an overfill still reads (no range set)
])
def test_level_units(given, raw, want):
    assert map_value("level_pct", raw, parse_rule("level_pct", given)).value == pytest.approx(want, rel=EXACT)


@pytest.mark.parametrize("bad,match", [
    ({"unit": "ft"}, "height_ft or height_m"),
    ({"unit": "m"}, "height_ft or height_m"),
    ({"unit": "in"}, "height_ft or height_m"),
    ({"height_ft": 20}, "set unit to ft, in, or m"),                 # feet read as percent would be wrong
    ({"unit": "pct", "height_m": 6}, "set unit to ft, in, or m"),
    ({"unit": "ft", "height_ft": 20, "height_m": 6.096}, "not both"),
    ({"unit": "ft", "height_ft": 0}, "above 0"),
    ({"unit": "ft", "height_ft": -5}, "above 0"),
    ({"unit": "ft", "height_ft": "20"}, "number"),
    ({"unit": "gpm"}, "pct, ft, in, m"),
])
def test_level_unit_config_errors(bad, match):
    with pytest.raises(ValueError, match=match):
        parse_rule("level_pct", bad)


def test_unit_spellings():
    for written, name in (("GPM", "gpm"), ("m3/h", "m3_h"), ("M3/HR", "m3_h"), ("L/s", "l_s"), ("lps", "l_s"),
                          ("%", "pct"), ("kPa", "kpa"), ("MPa", "mpa"), (" bar ", "bar"), ("bbl/day", "bbl_day"),
                          ("BPD", "bbl_day"), ("bbl/hr", "bbl_hr"), ("Feet", "ft"), ("amps", "a")):
        assert sm.unit_name(written) == name, written
    assert map_value("flow_bbl_hr", 1, rule("flow_bbl_hr", unit="m3/h")).value == pytest.approx(6.28981)


def test_a_unit_must_fit_its_signal():
    with pytest.raises(ValueError, match="psi, kpa, bar, mpa"):
        parse_rule("pressure_psi", {"unit": "gpm"})
    with pytest.raises(ValueError, match="bbl_hr, gpm, bbl_day, m3_h, l_s"):
        parse_rule("flow_bbl_hr", {"unit": "kpa"})
    with pytest.raises(ValueError, match="doesn't apply to flow_bbl_hr"):
        parse_rule("flow_bbl_hr", {"height_ft": 20})
    assert map_value("motor_amps", 41.5, rule("motor_amps", unit="A")).value == 41.5


# ---------- range ----------

def test_range_is_inclusive_and_out_of_range_is_none():
    r = rule("level_pct", range=[0, 100])
    assert map_value("level_pct", 0, r).value == 0
    assert map_value("level_pct", 100, r).value == 100
    out = map_value("level_pct", 100.5, r)
    assert out.quality == OUT_OF_RANGE and out.value is None and "0 to 100" in out.detail
    assert map_value("level_pct", -0.1, r).quality == OUT_OF_RANGE


def test_range_is_in_the_live_layers_unit():
    r = rule("flow_bbl_hr", unit="gpm", range=[0, 100])          # 100 bbl/hr = 70 gpm
    assert map_value("flow_bbl_hr", 70, r).value == pytest.approx(100)
    assert map_value("flow_bbl_hr", 71, r).quality == OUT_OF_RANGE
    amps = rule("motor_amps", range=[0, 60])
    assert map_value("motor_amps", 75, amps).quality == OUT_OF_RANGE


def test_range_config_errors():
    with pytest.raises(ValueError, match="above hi"):
        parse_rule("level_pct", {"range": [100, 0]})
    with pytest.raises(ValueError, match="finite"):
        parse_rule("level_pct", {"range": [0, INF]})
    with pytest.raises(ValueError, match=r"\[lo, hi\]"):
        parse_rule("level_pct", {"range": 100})


# ---------- NaN, inf, and junk never become numbers ----------

@pytest.mark.parametrize("raw", [NAN, INF, -INF, "nan", "inf", "-Infinity", 1e309, 10 ** 400, True, "72%",
                                 Decimal("sNaN"), b"\xff", object(), [], {}])
def test_analog_junk_is_bad(raw):
    for r in (None, LEVEL_420, rule("level_pct", range=[0, 100])):
        m = map_value("level_pct", raw, r)
        assert m.quality == BAD and m.value is None


def test_scale_overflow_and_bad_scale_are_bad():
    assert map_value("level_pct", 1e308, scale=10).quality == BAD
    for scale in (0, NAN, INF, "x", None):
        assert map_value("level_pct", 5, scale=scale).quality == BAD


def test_map_value_never_raises():
    odd = [None, NAN, INF, "", " ", "nan", b"", b"\x00", b"\xff", object(), [], {}, (1, 2), complex(1, 2),
           Decimal("NaN"), Decimal("sNaN"), 10 ** 400, -0.0, True, False]
    rules = [None, LEVEL_420, rule("position", open_values=[1], closed_values=[0]), rule("running", invert=True)]
    for signal in SIGNALS:
        for r in rules:
            for raw in odd:
                m = map_value(signal, raw, r if r is None or r.signal == signal else None)
                assert m.value is None or m.good


# ---------- running and position rules ----------

def test_invert_flips_contacts():
    inv = rule("running", invert=True)
    assert map_value("running", 1, inv).value is False
    assert map_value("running", 0, inv).value is True
    assert map_value("running", True, inv).value is False
    assert map_value("running", "1", inv).value is False
    assert map_value("running", "false", inv).value is True
    pos = rule("position", invert=True)
    assert map_value("position", 1, pos).value == "CLOSED"
    assert map_value("position", 0, pos).value == "OPEN"


def test_invert_takes_words_as_written():
    # A stopped pump that says STOPPED must never read as running.
    inv = rule("running", invert=True)
    assert map_value("running", "STOPPED", inv).value is False
    assert map_value("running", "TRIPPED", inv).value is False
    assert map_value("running", "RUNNING", inv).value is True
    assert map_value("position", "OPEN", rule("position", invert=True)).value == "OPEN"
    assert map_value("running", NAN, inv).quality == BAD


def test_open_and_closed_values():
    r = rule("position", open_values=[1], closed_values=[0])
    assert map_value("position", 1, r).value == "OPEN"
    assert map_value("position", 1.0, r).value == "OPEN"
    assert map_value("position", "1", r).value == "OPEN"
    assert map_value("position", True, r).value == "OPEN"
    assert map_value("position", 0, r).value == "CLOSED"
    assert map_value("position", Decimal("1"), r).value == "OPEN"
    assert map_value("position", b"0", r).value == "CLOSED"
    assert map_value("position", b"\xff", r).quality == BAD
    assert map_value("position", "", r).quality == BAD
    travel = map_value("position", 2, r)            # moving, or a fault code: not OPEN
    assert travel.quality == BAD and travel.value is None
    words = rule("position", open_values=["OPN", "Fully Open"], closed_values=["CLS"])
    assert map_value("position", "opn", words).value == "OPEN"
    assert map_value("position", " fully open ", words).value == "OPEN"
    assert map_value("position", " cls ", words).value == "CLOSED"
    assert map_value("position", "OPEN", words).quality == BAD     # only the listed words count
    assert map_value("position", "TRAVEL", words).quality == BAD
    assert map_value("position", NAN, r).quality == BAD


@pytest.mark.parametrize("signal,bad,match", [
    ("position", {"open_values": [1]}, "both"),
    ("position", {"closed_values": [0]}, "both"),
    ("position", {"open_values": [1, 2], "closed_values": [2]}, "both open_values and closed_values"),
    ("position", {"open_values": ["1"], "closed_values": [1.0]}, "both open_values and closed_values"),
    ("position", {"open_values": [], "closed_values": [0]}, "non-empty"),
    ("position", {"open_values": "OPEN", "closed_values": [0]}, "non-empty list"),
    ("position", {"open_values": [NAN], "closed_values": [0]}, "finite"),
    ("position", {"open_values": [1], "closed_values": [0], "invert": True}, "invert"),
    ("running", {"open_values": [1], "closed_values": [0]}, "doesn't apply to running"),
    ("running", {"invert": "yes"}, "true or false"),
    ("running", {"unit": "a"}, "doesn't apply to running"),
    ("position", {"range": [0, 1]}, "doesn't apply to position"),
])
def test_boolean_rule_config_errors(signal, bad, match):
    with pytest.raises(ValueError, match=match):
        parse_rule(signal, bad)


def test_rule_typos_and_comments():
    with pytest.raises(ValueError, match="rnage"):
        parse_rule("level_pct", {"rnage": [0, 100]})        # a typo must not switch the range off
    assert parse_rule("level_pct", {"_note": "LT-301", "range": [0, 100]}).range == (0.0, 100.0)
    with pytest.raises(ValueError, match="object"):
        parse_rule("level_pct", [0, 100])
    with pytest.raises(ValueError, match="not a live signal"):
        parse_rule("phase", {})


# ---------- SignalMap ----------

def test_signal_map_applies_scale_then_rule():
    smap = SignalMap(spec(
        [sig("TANK_5", "level_pct", scale=0.1), sig("PUMP_5", "flow_bbl_hr"), sig("PUMP_5", "running", scale=0.5),
         sig("VALVE_XO_56", "position")],
        signal_map={"_note": "commissioned 2026-11-07",
                    "PUMP_5.flow_bbl_hr": {"ma420": True, "eng": [0, 400], "unit": "gpm", "range": [0, 600]},
                    "VALVE_XO_56.position": {"open_values": [1], "closed_values": [0]}}))
    assert smap.apply("TANK_5", "level_pct", 723).value == pytest.approx(72.3)     # raw * scale, as Modbus does
    assert smap.apply("PUMP_5", "flow_bbl_hr", 12).value == pytest.approx(200 * 60 / 42)
    assert smap.apply("PUMP_5", "flow_bbl_hr", 3.0).quality == FAULT
    assert smap.apply("PUMP_5", "running", 1).value is True                       # scale ignored for booleans
    assert smap.apply("VALVE_XO_56", "position", 2).quality == BAD
    assert smap.apply("TANK_5", "level_pct", 723, quality=COMM_LOST).quality == COMM_LOST
    undeclared = smap.apply("TANK_9", "level_pct", 50)
    assert undeclared.quality == BAD and undeclared.value is None


@pytest.mark.parametrize("signal_map,match", [
    ({"TANK_9.level_pct": {"range": [0, 100]}}, "doesn't declare TANK_9.level_pct"),
    ({"TANK_3.flow_bbl_hr": {}}, "doesn't declare"),
    ({"TANK_3": {}}, "ASSET_ID.signal"),
    ({".level_pct": {}}, "ASSET_ID.signal"),
    ({"TANK_3.": {}}, "ASSET_ID.signal"),
    ({"TANK_3.level_pct": {"rnage": [0, 100]}}, "signal_map TANK_3.level_pct: rnage"),
    ({"TANK_3.level_pct": {"unit": "ft"}}, "signal_map TANK_3.level_pct: a level in ft"),
    ({"TANK_3.level_pct": "4-20"}, "signal_map TANK_3.level_pct: the rule must be an object"),
])
def test_signal_map_config_errors(signal_map, match):
    with pytest.raises(ValueError, match=match):
        SignalMap(spec([sig("TANK_3", "level_pct")], signal_map=signal_map))


@pytest.mark.parametrize("bad", [0, "0.1", True, NAN, INF, [0.1]])
def test_signal_scale_must_be_a_nonzero_number(bad):
    with pytest.raises(ValueError, match="TANK_3.level_pct scale"):
        SignalMap(spec([sig("TANK_3", "level_pct", scale=bad)]))


def test_signal_map_shape_errors():
    with pytest.raises(ValueError, match="signal_map must be an object"):
        SignalMap(spec([sig("TANK_3", "level_pct")], signal_map=["TANK_3.level_pct"]))
    with pytest.raises(ValueError, match="not a live signal"):
        SignalMap(spec([sig("TANK_3", "volume_bbl")]))
    with pytest.raises(ValueError, match="config must be an object"):
        SignalMap({"config": ["x"], "signals": []})
    empty = SignalMap({"id": "CONN_X"})
    assert empty.scales == {} and empty.rules == {}


# ---------- Inbox ----------

def good(v):
    return Mapped(v, GOOD)


def test_inbox_hands_on_the_newest_good_value_once():
    box = Inbox()
    box.put("TANK_3", "level_pct", good(70.0))
    box.put("TANK_3", "level_pct", good(70.5))
    box.put("PUMP_3", "running", good(True))
    assert sorted(box.drain()) == [("PUMP_3", "running", True), ("TANK_3", "level_pct", 70.5)]
    assert box.drain() == []


def test_inbox_never_carries_a_good_value_past_a_bad_one():
    box = Inbox()
    box.put("TANK_3", "level_pct", good(70.0))
    box.put("TANK_4", "level_pct", good(40.0))
    box.put("TANK_3", "level_pct", Mapped(None, FAULT, "3.2 mA"))
    assert box.drain() == [("TANK_4", "level_pct", 40.0)]
    box.put("TANK_3", "level_pct", Mapped(None, STALE))
    assert box.drain() == []


def test_inbox_none_readings_queue_none():
    box = Inbox(none_readings=True)
    box.put("TANK_3", "level_pct", good(70.0))
    box.put("TANK_3", "level_pct", Mapped(None, COMM_LOST))
    assert box.drain() == [("TANK_3", "level_pct", None)]
    assert Inbox().none_readings is False          # off by default: see the module docstring


def test_inbox_clear():
    box = Inbox()
    box.put("TANK_3", "level_pct", good(70.0))
    box.clear()
    assert box.drain() == []


def test_inbox_is_safe_across_threads():
    box = Inbox()
    seen: list = []
    errors: list = []

    def writer(n: int) -> None:
        try:
            for i in range(400):
                box.put(f"TANK_{n}", "level_pct", good(float(i)) if i % 7 else Mapped(None, BAD))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,), daemon=True) for n in range(4)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        seen += box.drain()
    for t in threads:
        t.join(timeout=5)
    seen += box.drain()
    assert not errors
    assert all(isinstance(v, float) and 0 <= v < 400 for _, _, v in seen)


# ---------- Backoff ----------

def test_backoff_defaults():
    b = Backoff()
    assert (b.initial_s, b.max_s, b.factor, b.jitter) == (1.0, 30.0, 2.0, 0.2)


def test_backoff_grows_then_caps_and_resets():
    b = Backoff(jitter=0)
    assert [b.next_delay() for _ in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert b.attempts == 8
    b.reset()
    assert b.attempts == 0 and b.next_delay() == 1
    c = Backoff(initial_s=0.5, max_s=5, factor=3, jitter=0)
    assert [c.next_delay() for _ in range(5)] == [0.5, 1.5, 4.5, 5, 5]
    flat = Backoff(initial_s=2, max_s=10, factor=1, jitter=0)
    assert [flat.next_delay() for _ in range(3)] == [2, 2, 2]


def test_backoff_jitter_stays_within_its_band_and_under_the_cap():
    b = Backoff(rng=random.Random(126))
    bases = [1, 2, 4, 8, 16] + [30] * 195
    delays = [b.next_delay() for _ in bases]
    for base, d in zip(bases, delays):
        assert 0.8 * base - 1e-9 <= d <= min(1.2 * base, 30) + 1e-9
    assert max(delays) <= 30
    assert len({round(d, 6) for d in delays[5:]}) > 10       # really jittered, not one fixed value
    assert delays[4] > 1.2                                     # 16 s +-20% is past anything the first try waits


def test_backoff_never_overflows():
    b = Backoff(initial_s=1, max_s=30, factor=10, jitter=0.2)
    for _ in range(5000):
        d = b.next_delay()
    assert 24 - 1e-9 <= d <= 30


@pytest.mark.parametrize("kw", [
    {"initial_s": 0}, {"initial_s": -1}, {"initial_s": True}, {"initial_s": "1"},
    {"initial_s": 10, "max_s": 5}, {"factor": 0.5}, {"factor": NAN},
    {"jitter": 1}, {"jitter": -0.1}, {"jitter": "x"}, {"max_s": INF},
])
def test_backoff_rejects_bad_settings(kw):
    with pytest.raises(ValueError):
        Backoff(**kw)


# ---------- close_later ----------

def test_close_later_returns_at_once():
    release, done = threading.Event(), threading.Event()

    def slow_close():
        release.wait(5)
        done.set()

    t0 = time.monotonic()
    t = close_later(slow_close, name="test-close")
    assert time.monotonic() - t0 < 0.5
    assert t.daemon and t.name == "test-close" and t.is_alive() and not done.is_set()
    release.set()
    t.join(timeout=5)
    assert done.is_set() and not t.is_alive()


def test_close_later_swallows_errors(monkeypatch):
    hooked = []
    monkeypatch.setattr(threading, "excepthook", lambda args: hooked.append(args))

    def broken():
        raise OSError("socket already closed by the PLC")

    t = close_later(broken)
    t.join(timeout=5)
    assert not t.is_alive() and hooked == []


# ---------- commands ----------

def test_uncertain_detail_and_result():
    assert UNCERTAIN_DETAIL == "sent; delivery not confirmed - check the field before retrying"
    r = uncertain_result()
    assert r == {"ok": False, "uncertain": True, "detail": UNCERTAIN_DETAIL}
    r["detail"] = "changed"
    assert uncertain_result()["detail"] == UNCERTAIN_DETAIL         # a fresh dict every time


@pytest.mark.parametrize("command,want", [
    ({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}, ("SET_VALVE", "VALVE_XO_34", True)),
    ({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "CLOSED"}, ("SET_VALVE", "VALVE_XO_34", False)),
    ({"type": "set_valve", "asset_id": "VALVE_D_3", "value": " open "}, ("SET_VALVE", "VALVE_D_3", True)),
    ({"type": "SET_PUMP", "asset_id": "PUMP_3", "value": "ON"}, ("SET_PUMP", "PUMP_3", True)),
    ({"type": "SET_PUMP", "asset_id": "PUMP_3", "value": "OFF"}, ("SET_PUMP", "PUMP_3", False)),
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 0}, ("CHOKE_WELL", "WELL_9", 0.0)),
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 0.5}, ("CHOKE_WELL", "WELL_9", 0.5)),
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 1}, ("CHOKE_WELL", "WELL_9", 1.0)),
    ({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "target_id": None},
     ("SET_VALVE", "VALVE_XO_34", True)),
])
def test_normalize(command, want):
    got = normalize(command)
    assert got == want and type(got[2]) is type(want[2])


@pytest.mark.parametrize("command,match", [
    ({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "ON"}, "SET_VALVE takes OPEN or CLOSED"),
    ({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": True}, "SET_VALVE takes OPEN or CLOSED"),
    ({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": 1}, "SET_VALVE takes OPEN or CLOSED"),
    ({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": None}, "SET_VALVE takes OPEN or CLOSED"),
    ({"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "HALF"}, "SET_VALVE takes OPEN or CLOSED"),
    ({"type": "SET_PUMP", "asset_id": "PUMP_3", "value": "OPEN"}, "SET_PUMP takes ON or OFF"),
    ({"type": "SET_PUMP", "asset_id": "PUMP_3", "value": 0}, "SET_PUMP takes ON or OFF"),
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 1.5}, "fraction from 0"),
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": -0.1}, "fraction from 0"),
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 50}, "fraction from 0"),   # 50% is 0.5, not 50
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": "0.5"}, "fraction from 0"),
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": True}, "fraction from 0"),
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": NAN}, "fraction from 0"),
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": INF}, "fraction from 0"),
    ({"type": "CHOKE_WELL", "asset_id": "WELL_9", "value": None}, "fraction from 0"),
    ({"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3"}, "simulator"),
    ({"type": "OPEN_EVERYTHING", "asset_id": "VALVE_XO_34", "value": "OPEN"}, "unknown command type"),
    ({"type": None, "asset_id": "VALVE_XO_34", "value": "OPEN"}, "unknown command type"),
    ({"type": "SET_VALVE", "value": "OPEN"}, "needs an asset_id"),
    ({"type": "SET_VALVE", "asset_id": "  ", "value": "OPEN"}, "needs an asset_id"),
    ({"type": "SET_VALVE", "asset_id": 34, "value": "OPEN"}, "needs an asset_id"),
    ("SET_VALVE VALVE_XO_34 OPEN", "must be an object"),
])
def test_normalize_rejects_anything_else(command, match):
    with pytest.raises(ValueError, match=match):
        normalize(command)
