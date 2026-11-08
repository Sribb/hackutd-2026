"""The Sparkplug B codec: round trips, signed integers, bad bytes, unknown fields, and a
birth certificate built by hand from the protobuf wire rules."""
import math
import struct

import pytest

from backend.app.connectors import sparkplug_codec as sp
from backend.app.connectors.sparkplug_codec import CodecError, decode_payload, encode_payload

# NBIRTH, byte by byte. Key byte = field number << 3 | wire type
# (0 varint, 1 fixed64, 2 length-delimited, 5 fixed32).
NBIRTH_HEX = " ".join([
    "08 80 d0 95 ff bc 31",                  # timestamp 1 = 1700000000000
    # metric: bdSeq, Int64, long_value 0
    "12 0b",
    "0a 05 62 64 53 65 71",                  # name "bdSeq"
    "20 04",                                 # datatype 4 (Int64)
    "58 00",                                 # long_value 11 = 0
    # metric: Node Control/Rebirth, Boolean false
    "12 1a",
    "0a 14 4e 6f 64 65 20 43 6f 6e 74 72 6f 6c 2f 52 65 62 69 72 74 68",
    "20 0b",                                 # datatype 11 (Boolean)
    "70 00",                                 # boolean_value 14 = false
    # metric: TANK_3/level_ft, alias 2, Float 12.5
    "12 1a",
    "0a 0f 54 41 4e 4b 5f 33 2f 6c 65 76 65 6c 5f 66 74",
    "10 02",                                 # alias 2
    "20 09",                                 # datatype 9 (Float)
    "65 00 00 48 41",                        # float_value 12 = 12.5 (0x41480000, little-endian)
    # metric: TANK_3/temp_f, alias 3, Int16 -7 sign-extended into uint32
    "12 19",
    "0a 0d 54 41 4e 4b 5f 33 2f 74 65 6d 70 5f 66",
    "10 03",                                 # alias 3
    "20 02",                                 # datatype 2 (Int16)
    "50 f9 ff ff ff 0f",                     # int_value 10 = 0xFFFFFFF9
    # metric: PUMP_3/running, alias 1, Boolean true
    "12 16",
    "0a 0e 50 55 4d 50 5f 33 2f 72 75 6e 6e 69 6e 67",
    "10 01",                                 # alias 1
    "20 0b",                                 # datatype 11 (Boolean)
    "70 01",                                 # boolean_value 14 = true
    "18 00",                                 # seq 3 = 0
])
NBIRTH = bytes.fromhex(NBIRTH_HEX)

NBIRTH_DICT = {
    "timestamp": 1700000000000,
    "seq": 0,
    "metrics": [
        {"name": "bdSeq", "datatype": sp.INT64, "value": 0},
        {"name": "Node Control/Rebirth", "datatype": "Boolean", "value": False},
        {"name": "TANK_3/level_ft", "alias": 2, "datatype": sp.FLOAT, "value": 12.5},
        {"name": "TANK_3/temp_f", "alias": 3, "datatype": sp.INT16, "value": -7},
        {"name": "PUMP_3/running", "alias": 1, "datatype": sp.BOOLEAN, "value": True},
    ],
}


def one(datatype, value, **extra):
    m = {"name": "m", "datatype": datatype, "value": value, **extra}
    out = decode_payload(encode_payload({"metrics": [m]}))["metrics"]
    assert len(out) == 1
    return out[0]


def test_nbirth_fixture_decodes():
    p = decode_payload(NBIRTH)
    assert p["timestamp"] == 1700000000000
    assert p["seq"] == 0
    assert p["uuid"] is None and p["body"] is None
    got = [(m["name"], m["alias"], m["datatype"], m["value"]) for m in p["metrics"]]
    assert got == [
        ("bdSeq", None, 4, 0),
        ("Node Control/Rebirth", None, 11, False),
        ("TANK_3/level_ft", 2, 9, 12.5),
        ("TANK_3/temp_f", 3, 2, -7),
        ("PUMP_3/running", 1, 11, True),
    ]
    for m in p["metrics"]:
        assert m["detail"] == "" and not m["is_null"]
        assert not m["is_historical"] and not m["is_transient"]


def test_encoder_matches_nbirth_fixture_byte_for_byte():
    assert encode_payload(NBIRTH_DICT) == NBIRTH


@pytest.mark.parametrize("datatype,values", [
    (sp.INT8, [0, 1, 127, -1, -128]),
    (sp.INT16, [0, 32767, -1, -32768]),
    (sp.INT32, [0, 2**31 - 1, -1, -2**31]),
    (sp.INT64, [0, 2**63 - 1, -1, -2**63]),
    (sp.UINT8, [0, 255]),
    (sp.UINT16, [0, 65535]),
    (sp.UINT32, [0, 2**32 - 1]),
    (sp.UINT64, [0, 2**64 - 1]),
    (sp.FLOAT, [0.0, 1.5, -12.25, math.inf, -math.inf]),
    (sp.DOUBLE, [0.0, 0.1, -1e300, math.inf]),
    (sp.BOOLEAN, [True, False]),
    (sp.STRING, ["", "OPEN", "naïve °F"]),
    (sp.DATETIME, [0, 1700000000000]),
    (sp.TEXT, ["a longer note\nwith a newline"]),
])
def test_round_trip_every_datatype(datatype, values):
    for v in values:
        m = one(datatype, v, alias=7, timestamp=1700000000123)
        assert m["value"] == v and type(m["value"]) is type(v)
        assert m["datatype"] == datatype and m["detail"] == ""
        assert m["alias"] == 7 and m["timestamp"] == 1700000000123


def test_negative_integers_are_sign_extended_on_the_wire():
    # Int8 -1 goes out as uint32 0xFFFFFFFF (five varint bytes), Int64 -1 as ten.
    assert encode_payload({"metrics": [{"datatype": sp.INT8, "value": -1}]}) == \
        bytes.fromhex("12 08 20 01 50 ff ff ff ff 0f")
    assert encode_payload({"metrics": [{"datatype": sp.INT64, "value": -1}]}) == \
        bytes.fromhex("12 0d 20 04 58 ff ff ff ff ff ff ff ff ff 01")


def test_signed_value_sent_in_its_own_width_still_reads():
    # Some edge nodes send Int8 -1 as 255 and Int16 -2 as 65534.
    assert decode_payload(bytes.fromhex("12 05 20 01 50 ff 01"))["metrics"][0]["value"] == -1
    assert decode_payload(bytes.fromhex("12 06 20 02 50 fe ff 03"))["metrics"][0]["value"] == -2


def test_integer_that_fits_no_reading_is_none_not_a_guess():
    m = decode_payload(bytes.fromhex("12 05 20 01 50 b4 24"))["metrics"][0]   # Int8 4660
    assert m["value"] is None and "doesn't fit Int8" in m["detail"]
    m = decode_payload(bytes.fromhex("12 05 20 05 50 80 02"))["metrics"][0]   # UInt8 256
    assert m["value"] is None and "doesn't fit UInt8" in m["detail"]


def test_float_nan_round_trips_as_nan():
    for dt in (sp.FLOAT, sp.DOUBLE):
        m = one(dt, math.nan)
        assert isinstance(m["value"], float) and math.isnan(m["value"])


def test_float_value_too_big_for_32_bits_raises():
    with pytest.raises(CodecError):
        encode_payload({"metrics": [{"datatype": sp.FLOAT, "value": 1e40}]})


def test_null_and_flags_round_trip():
    m = one(sp.FLOAT, None, is_null=True, is_historical=True, is_transient=True)
    assert m["is_null"] and m["is_historical"] and m["is_transient"]
    assert m["value"] is None and m["value_type"] is None and m["detail"]


def test_empty_payload():
    assert encode_payload({}) == b""
    assert decode_payload(b"") == {"timestamp": None, "seq": None, "uuid": None,
                                   "body": None, "metrics": []}


def test_uuid_and_body_round_trip():
    p = decode_payload(encode_payload({"uuid": "abc-123", "body": b"\x00\x01", "seq": 255}))
    assert p["uuid"] == "abc-123" and p["body"] == b"\x00\x01" and p["seq"] == 255


def test_truncated_input_raises_value_error_at_every_cut():
    # Cutting exactly between top-level fields leaves a valid, shorter payload.
    boundaries = {7, 20, 48, 76, 103, 127}
    for cut in range(1, len(NBIRTH)):
        if cut in boundaries:
            decode_payload(NBIRTH[:cut])
        else:
            with pytest.raises(ValueError):
                decode_payload(NBIRTH[:cut])


@pytest.mark.parametrize("hexdata", [
    "08",                                    # varint cut off
    "08 ff ff ff ff ff ff ff ff ff ff 01",   # varint past 10 bytes
    "08 ff ff ff ff ff ff ff ff ff 7f",      # varint over 64 bits
    "12 05 0a",                              # metric longer than the payload
    "12 7f",                                 # length past the end
    "00 01",                                 # field number 0
    "0e",                                    # wire type 6
    "0f",                                    # wire type 7
    "0c",                                    # end-group with no start
    "0b 08 01",                              # group never closed
    "0b 14",                                 # group closed by the wrong field
    "0a 01 41",                              # timestamp sent length-delimited
    "10 01",                                 # metrics sent as a varint
    "12 03 65 00 00",                        # float_value cut short inside a metric
])
def test_malformed_input_raises_value_error(hexdata):
    with pytest.raises(ValueError):
        decode_payload(bytes.fromhex(hexdata))


def test_wrong_wire_type_for_a_value_field_raises():
    with pytest.raises(CodecError):
        decode_payload(bytes.fromhex("12 02 60 01"))      # float_value 12 as a varint


def test_not_bytes_raises():
    with pytest.raises(CodecError):
        decode_payload("08 01")
    with pytest.raises(CodecError):
        encode_payload([1, 2])


def test_random_garbage_never_raises_anything_but_value_error():
    import random
    rng = random.Random(126)
    for _ in range(3000):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
        try:
            decode_payload(data)
        except ValueError:
            pass
    for _ in range(2000):                    # flip bytes inside a real payload
        data = bytearray(NBIRTH)
        for _ in range(rng.randrange(1, 4)):
            data[rng.randrange(len(data))] = rng.randrange(256)
        try:
            decode_payload(bytes(data))
        except ValueError:
            pass


def test_unknown_fields_of_every_wire_type_are_skipped():
    metric = bytes.fromhex(
        "0a 01 78"                           # name "x"
        "42 03 0a 01 41"                     # metadata 8 (length-delimited)
        "4a 04 0a 02 75 6e"                  # properties 9 (length-delimited)
        "a8 06 05"                           # field 101 varint
        "a9 06 01 02 03 04 05 06 07 08"      # field 101 fixed64
        "ad 06 01 02 03 04"                  # field 101 fixed32
        "ab 06 08 01 ac 06"                  # field 101 group with a varint inside
        "20 09 65 00 00 c0 3f"               # datatype Float, value 1.5
    )
    payload = (bytes.fromhex("12") + bytes([len(metric)]) + metric
               + bytes.fromhex("c8 3e 07"))  # payload field 1001 varint
    m = decode_payload(payload)["metrics"][0]
    assert (m["name"], m["datatype"], m["value"]) == ("x", sp.FLOAT, 1.5)


@pytest.mark.parametrize("number,kind", [(17, "dataset"), (18, "template"), (19, "extension")])
def test_dataset_template_extension_values_give_none(number, kind):
    key = bytes([(number << 3 | 2) & 0x7F | 0x80, (number << 3 | 2) >> 7])
    metric = bytes.fromhex("20 0a") + key + bytes.fromhex("02 08 01")
    m = decode_payload(bytes([0x12, len(metric)]) + metric)["metrics"][0]
    assert m["value"] is None and m["value_type"] == kind and kind in m["detail"]


def test_field_that_does_not_match_datatype_gives_none():
    # Datatype Float but the value came in boolean_value.
    m = decode_payload(bytes.fromhex("12 04 20 09 70 01"))["metrics"][0]
    assert m["value"] is None and "doesn't fit Float" in m["detail"]


def test_ndata_without_datatype_reads_with_the_birth_datatype():
    data = encode_payload({"seq": 1, "metrics": [
        {"alias": 3, "datatype": sp.INT16, "value": -7},
        {"alias": 2, "datatype": sp.FLOAT, "value": 12.5},
    ]}, datatypes=False)
    a, b = decode_payload(data)["metrics"]
    assert a["datatype"] is None and a["value"] == 0xFFFFFFF9 and a["value_type"] == "int"
    assert sp.typed_value(sp.INT16, a["value_type"], a["raw"]) == (-7, "")
    assert sp.typed_value(sp.FLOAT, b["value_type"], b["raw"]) == (12.5, "")
    assert sp.typed_value(sp.BOOLEAN, b["value_type"], b["raw"])[0] is None


@pytest.mark.parametrize("datatype,value", [
    (sp.INT8, 128), (sp.INT8, -129), (sp.UINT8, -1), (sp.UINT32, 2**32),
    (sp.INT64, 2**63), (sp.INT32, 1.5), (sp.INT32, True), (sp.BOOLEAN, 1),
    (sp.STRING, 5), (sp.FLOAT, "1.0"), (None, 5), (99, 1), ("Bogus", 1),
])
def test_encoding_a_value_that_does_not_fit_raises(datatype, value):
    with pytest.raises(CodecError):
        encode_payload({"metrics": [{"datatype": datatype, "value": value}]})


def test_float_on_the_wire_is_little_endian_fixed32():
    data = encode_payload({"metrics": [{"datatype": sp.FLOAT, "value": 12.5}]})
    assert data.endswith(bytes([0x65]) + struct.pack("<f", 12.5))


def test_quality_property_round_trips():
    props = {"Quality": 500, "engUnit": "%", "ok": True, "gain": 1.5, "nothing": None, "big": 1 << 40}
    data = encode_payload({"metrics": [{"name": "L", "datatype": "Float", "value": 1.0, "properties": props}]})
    m = decode_payload(data)["metrics"][0]
    assert m["properties"] == props
    assert m["value"] == 1.0


def test_quality_property_decodes_from_hand_built_bytes():
    # PropertySet {keys: ["Quality"], values: [{type: Int32, int_value: 192}]}
    pv = bytes.fromhex("08 03 18 c0 01")
    pset = bytes.fromhex("0a 07") + b"Quality" + bytes([0x12, len(pv)]) + pv
    metric = bytes.fromhex("0a 01 78 20 09") + bytes([0x4a, len(pset)]) + pset + bytes.fromhex("65 00 00 c0 3f")
    m = decode_payload(bytes([0x12, len(metric)]) + metric)["metrics"][0]
    assert m["properties"] == {"Quality": 192}
    assert m["value"] == 1.5


def test_quality_key_without_a_value_is_still_seen():
    pset = bytes.fromhex("0a 07") + b"Quality"
    metric = bytes.fromhex("0a 01 78") + bytes([0x4a, len(pset)]) + pset
    m = decode_payload(bytes([0x12, len(metric)]) + metric)["metrics"][0]
    assert "Quality" in m["properties"] and m["properties"]["Quality"] is None


def test_malformed_properties_raise_value_error():
    metric = bytes.fromhex("0a 01 78 4a 03 12 05 08")     # a property value cut short
    with pytest.raises(ValueError):
        decode_payload(bytes([0x12, len(metric)]) + metric)
