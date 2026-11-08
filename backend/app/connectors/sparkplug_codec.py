"""Sparkplug B payloads: a small hand-written protobuf encoder and decoder.

There is no protobuf package in the venv, and Freeboard only needs the parts of Eclipse
Sparkplug B's sparkplug_b.proto (Sparkplug 3.0) that carry live values. Field numbers:

    Payload  timestamp 1 (uint64), metrics 2 (Metric, repeated), seq 3 (uint64),
             uuid 4 (string), body 5 (bytes)
    Metric   name 1 (string), alias 2 (uint64), timestamp 3 (uint64), datatype 4 (uint32),
             is_historical 5, is_transient 6, is_null 7 (bool), int_value 10 (uint32),
             long_value 11 (uint64), float_value 12 (float), double_value 13 (double),
             boolean_value 14 (bool), string_value 15 (string), bytes_value 16 (bytes)

Every other field (metadata 8, properties 9, and anything a newer edge node adds) is
skipped by its wire type, so it never breaks the decoder. A dataset 17, template 18, or
extension 19 value is skipped too, and the metric decodes with value None and a detail
saying why, never as a number.

Datatypes: Int8, Int16, Int32, Int64 = 1 to 4; UInt8, UInt16, UInt32, UInt64 = 5 to 8;
Float 9, Double 10, Boolean 11, String 12, DateTime 13, Text 14. The 8, 16 and 32 bit
integers travel in int_value and the 64 bit ones (and DateTime, milliseconds since 1970
UTC) in long_value. A signed value is sign-extended into the field, so Int8 -1 is
0xFFFFFFFF in int_value. Some edge nodes send it in its own width instead (255 for Int8
-1), so the decoder reads that too. A value that fits neither reading, or a field that
doesn't match the datatype, decodes as value None with a detail. It is never guessed.

decode_payload(bytes) returns a dict:

    {"timestamp", "seq", "uuid", "body", "metrics": [metric, ...]}

and each metric is a dict:

    {"name", "alias", "timestamp", "datatype", "is_null", "is_historical",
     "is_transient", "value", "value_type", "raw", "detail"}

Absent fields are None (the flags are False). value_type is the field the value came in
("int", "long", "float", "double", "boolean", "string", "bytes", or "dataset",
"template", "extension"; None when there was no value field), raw is that field as it
was on the wire, and detail is "" or why value is None. A metric in NDATA or DDATA
usually leaves its datatype out because the birth certificate already gave it; the
caller then calls typed_value(birth_datatype, m["value_type"], m["raw"]).

encode_payload(dict) takes the same shape (only name, alias, timestamp, datatype, value
and the flags are read from a metric; datatype may be a number or a name like "Float").
Bytes that aren't a valid payload, and a value that doesn't fit its datatype when
encoding, raise CodecError, which is a ValueError. Nothing here raises IndexError or
loops on bad input: every read is bounds-checked and every loop moves forward.
"""
from __future__ import annotations

import struct
from typing import Any, Iterator, Optional

# ---------- datatypes ----------

INT8, INT16, INT32, INT64 = 1, 2, 3, 4
UINT8, UINT16, UINT32, UINT64 = 5, 6, 7, 8
FLOAT, DOUBLE, BOOLEAN, STRING, DATETIME, TEXT = 9, 10, 11, 12, 13, 14

DATATYPE_NAMES = {
    INT8: "Int8", INT16: "Int16", INT32: "Int32", INT64: "Int64",
    UINT8: "UInt8", UINT16: "UInt16", UINT32: "UInt32", UINT64: "UInt64",
    FLOAT: "Float", DOUBLE: "Double", BOOLEAN: "Boolean", STRING: "String",
    DATETIME: "DateTime", TEXT: "Text",
}
DATATYPES = {name: number for number, name in DATATYPE_NAMES.items()}   # "Float" -> 9

SIGNED_BITS = {INT8: 8, INT16: 16, INT32: 32, INT64: 64}
UNSIGNED_BITS = {UINT8: 8, UINT16: 16, UINT32: 32, UINT64: 64, DATETIME: 64}
TEXTS = frozenset((STRING, TEXT))
UNSUPPORTED_VALUES = ("dataset", "template", "extension")


def type_name(datatype: Optional[int]) -> str:
    if datatype is None:
        return "no datatype"
    return DATATYPE_NAMES.get(datatype, f"datatype {datatype}")


class CodecError(ValueError):
    """The bytes aren't a Sparkplug B payload this decoder can read, or a value doesn't
    fit its datatype."""


# ---------- the wire ----------

VARINT, I64, LEN, SGROUP, EGROUP, I32 = 0, 1, 2, 3, 4, 5
MASK32 = (1 << 32) - 1
MASK64 = (1 << 64) - 1
MAX_GROUP_DEPTH = 32

# field number -> (value_type, wire type)
VALUE_FIELDS = {10: ("int", VARINT), 11: ("long", VARINT), 12: ("float", I32),
                13: ("double", I64), 14: ("boolean", VARINT), 15: ("string", LEN),
                16: ("bytes", LEN), 17: ("dataset", LEN), 18: ("template", LEN),
                19: ("extension", LEN)}
FIELD_NUMBERS = {"int": 10, "long": 11, "float": 12, "double": 13, "boolean": 14,
                 "string": 15, "bytes": 16}
FLAGS = {5: "is_historical", 6: "is_transient", 7: "is_null"}


def _varint(n: int) -> bytes:
    if n < 0 or n > MASK64:
        raise CodecError(f"{n} doesn't fit a protobuf varint")
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(number: int, wire_type: int) -> bytes:
    return _varint(number << 3 | wire_type)


def _chunk(number: int, data: bytes) -> bytes:
    return _key(number, LEN) + _varint(len(data)) + data


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.i = 0

    def done(self) -> bool:
        return self.i >= len(self.data)

    def varint(self) -> int:
        result = shift = 0
        for _ in range(10):
            if self.i >= len(self.data):
                raise CodecError("the payload ends inside a varint")
            byte = self.data[self.i]
            self.i += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                if result > MASK64:
                    raise CodecError("a varint is longer than 64 bits")
                return result
            shift += 7
        raise CodecError("a varint runs past 10 bytes")

    def take(self, n: int) -> bytes:
        if n < 0 or self.i + n > len(self.data):
            raise CodecError("the payload ends inside a field")
        out = self.data[self.i:self.i + n]
        self.i += n
        return out

    def fields(self) -> Iterator[tuple[int, int, Any]]:
        """(field number, wire type, value) for each field; value is an int for a
        varint and bytes otherwise. Groups (deprecated in protobuf) are skipped whole."""
        while not self.done():
            key = self.varint()
            number, wire_type = key >> 3, key & 7
            if number == 0:
                raise CodecError("field number 0 is not allowed")
            if wire_type == SGROUP:
                self._skip_group(number, 1)
                continue
            yield number, wire_type, self._value(wire_type)

    def _value(self, wire_type: int) -> Any:
        if wire_type == VARINT:
            return self.varint()
        if wire_type == I64:
            return self.take(8)
        if wire_type == LEN:
            return self.take(self.varint())
        if wire_type == I32:
            return self.take(4)
        raise CodecError(f"wire type {wire_type} is not valid here")

    def _skip_group(self, number: int, depth: int) -> None:
        if depth > MAX_GROUP_DEPTH:
            raise CodecError("groups nest too deeply")
        while True:
            if self.done():
                raise CodecError("the payload ends inside a group")
            key = self.varint()
            inner, wire_type = key >> 3, key & 7
            if inner == 0:
                raise CodecError("field number 0 is not allowed")
            if wire_type == EGROUP:
                if inner != number:
                    raise CodecError("a group ends with the wrong field number")
                return
            if wire_type == SGROUP:
                self._skip_group(inner, depth + 1)
            else:
                self._value(wire_type)


def _want(number: int, wire_type: int, expected: int, what: str) -> None:
    if wire_type != expected:
        raise CodecError(f"{what} (field {number}) has wire type {wire_type}, expected {expected}")


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _wire_value(value_type: str, data: Any) -> Any:
    if value_type == "int":
        return data & MASK32
    if value_type == "long":
        return data
    if value_type == "float":
        return struct.unpack("<f", data)[0]
    if value_type == "double":
        return struct.unpack("<d", data)[0]
    if value_type == "boolean":
        return data != 0
    if value_type == "string":
        return _text(data)
    return bytes(data)


def _signed(raw: int, value_type: str, bits: int) -> Optional[int]:
    """raw read as a signed `bits` wide integer, or None when it fits no reading."""
    width = 64 if value_type == "long" else 32
    v = raw & (MASK64 if width == 64 else MASK32)
    if v >= 1 << (width - 1):
        v -= 1 << width                      # sign-extended, the spec's way
    if -(1 << (bits - 1)) <= v < 1 << (bits - 1):
        return v
    if bits < width and 0 <= raw < 1 << bits:
        return raw - (1 << bits) if raw >= 1 << (bits - 1) else raw   # its own width
    return None


def typed_value(datatype: Optional[int], value_type: Optional[str], raw: Any) -> tuple[Any, str]:
    """(value, "") read from a value field with `datatype`, or (None, why).

    An integer datatype reads int_value or long_value (signed ones sign-extended),
    Float and Double read either float field, Boolean reads boolean_value, String and
    Text read string_value, DateTime reads an integer field. With no datatype (or 0,
    Unknown) the field is read as it is. A field that doesn't match the datatype, a
    value out of the datatype's range, or a dataset, template or extension value gives
    (None, why)."""
    if value_type is None:
        return None, "the metric has no value"
    if value_type in UNSUPPORTED_VALUES:
        return None, f"a {value_type} value isn't supported"
    if datatype in SIGNED_BITS and value_type in ("int", "long"):
        v = _signed(raw, value_type, SIGNED_BITS[datatype])
        if v is None:
            return None, f"{raw} doesn't fit {type_name(datatype)}"
        return v, ""
    if datatype in UNSIGNED_BITS and value_type in ("int", "long"):
        if raw >= 1 << UNSIGNED_BITS[datatype]:
            return None, f"{raw} doesn't fit {type_name(datatype)}"
        return raw, ""
    if datatype in (FLOAT, DOUBLE) and value_type in ("float", "double"):
        return raw, ""
    if datatype == BOOLEAN and value_type == "boolean":
        return raw, ""
    if datatype in TEXTS and value_type == "string":
        return raw, ""
    if datatype in (None, 0):
        return raw, ""
    return None, f"the value is in {value_type}_value, which doesn't fit {type_name(datatype)}"


# ---------- decoding ----------

def decode_payload(data: Any) -> dict:
    """The payload dict for these bytes. Raises CodecError for bytes that aren't one."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise CodecError(f"a payload is bytes, got {type(data).__name__}")
    p: dict = {"timestamp": None, "seq": None, "uuid": None, "body": None, "metrics": []}
    for number, wire_type, value in _Reader(bytes(data)).fields():
        if number == 1:
            _want(number, wire_type, VARINT, "timestamp")
            p["timestamp"] = value
        elif number == 2:
            _want(number, wire_type, LEN, "metrics")
            p["metrics"].append(_decode_metric(value))
        elif number == 3:
            _want(number, wire_type, VARINT, "seq")
            p["seq"] = value
        elif number == 4:
            _want(number, wire_type, LEN, "uuid")
            p["uuid"] = _text(value)
        elif number == 5:
            _want(number, wire_type, LEN, "body")
            p["body"] = bytes(value)
    return p


def _decode_metric(data: bytes) -> dict:
    m: dict = {"name": None, "alias": None, "timestamp": None, "datatype": None,
               "is_null": False, "is_historical": False, "is_transient": False,
               "value": None, "value_type": None, "raw": None, "detail": ""}
    for number, wire_type, value in _Reader(data).fields():
        if number == 1:
            _want(number, wire_type, LEN, "metric name")
            m["name"] = _text(value)
        elif number == 2:
            _want(number, wire_type, VARINT, "alias")
            m["alias"] = value
        elif number == 3:
            _want(number, wire_type, VARINT, "metric timestamp")
            m["timestamp"] = value
        elif number == 4:
            _want(number, wire_type, VARINT, "datatype")
            m["datatype"] = value & MASK32
        elif number in FLAGS:
            _want(number, wire_type, VARINT, FLAGS[number])
            m[FLAGS[number]] = value != 0
        elif number in VALUE_FIELDS:
            value_type, expected = VALUE_FIELDS[number]
            _want(number, wire_type, expected, f"{value_type}_value")
            m["value_type"], m["raw"] = value_type, _wire_value(value_type, value)
        # anything else (metadata 8, properties 9, newer fields) is skipped
    if m["is_null"]:
        m["detail"] = "the metric is null"
    else:
        m["value"], m["detail"] = typed_value(m["datatype"], m["value_type"], m["raw"])
    return m


# ---------- encoding ----------

def encode_payload(p: dict, *, datatypes: bool = True) -> bytes:
    """Bytes for a payload dict. datatypes=False leaves each metric's datatype off the
    wire (Sparkplug lets NDATA and DDATA rely on the birth), though the datatype still
    picks the value field. Raises CodecError when a value doesn't fit its datatype."""
    if not isinstance(p, dict):
        raise CodecError(f"a payload is a dict, got {type(p).__name__}")
    out = bytearray()
    if p.get("timestamp") is not None:
        out += _key(1, VARINT) + _varint(_uint(p["timestamp"], 64, "payload timestamp"))
    for m in p.get("metrics") or ():
        out += _chunk(2, _encode_metric(m, datatypes))
    if p.get("seq") is not None:
        out += _key(3, VARINT) + _varint(_uint(p["seq"], 64, "seq"))
    if p.get("uuid") is not None:
        out += _chunk(4, str(p["uuid"]).encode("utf-8"))
    if p.get("body") is not None:
        out += _chunk(5, bytes(p["body"]))
    return bytes(out)


def _uint(value: Any, bits: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodecError(f"{what} must be an integer, got {value!r}")
    if not 0 <= value < 1 << bits:
        raise CodecError(f"{what} {value} doesn't fit {bits} unsigned bits")
    return value


def _datatype(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, str):
        if value not in DATATYPES:
            raise CodecError(f"unknown datatype {value!r}")
        return DATATYPES[value]
    return _uint(value, 32, "datatype")


def _encode_metric(m: dict, datatypes: bool) -> bytes:
    if not isinstance(m, dict):
        raise CodecError(f"a metric is a dict, got {type(m).__name__}")
    datatype = _datatype(m.get("datatype"))
    out = bytearray()
    if m.get("name") is not None:
        out += _chunk(1, str(m["name"]).encode("utf-8"))
    if m.get("alias") is not None:
        out += _key(2, VARINT) + _varint(_uint(m["alias"], 64, "alias"))
    if m.get("timestamp") is not None:
        out += _key(3, VARINT) + _varint(_uint(m["timestamp"], 64, "metric timestamp"))
    if datatype is not None and datatypes:
        out += _key(4, VARINT) + _varint(datatype)
    for number, flag in FLAGS.items():
        if m.get(flag):
            out += _key(number, VARINT) + b"\x01"
    if not m.get("is_null"):
        out += _encode_value(datatype, m.get("value"), m.get("name"))
    return bytes(out)


def _encode_value(datatype: Optional[int], value: Any, name: Any) -> bytes:
    what = f"metric {name!r}" if name is not None else "a metric"
    if datatype in SIGNED_BITS or datatype in UNSIGNED_BITS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CodecError(f"{what} is {type_name(datatype)}, so its value must be an integer, got {value!r}")
        if datatype in SIGNED_BITS:
            bits = SIGNED_BITS[datatype]
            if not -(1 << (bits - 1)) <= value < 1 << (bits - 1):
                raise CodecError(f"{what}: {value} doesn't fit {type_name(datatype)}")
            wire = value & (MASK64 if bits == 64 else MASK32)    # sign-extend
        else:
            bits = UNSIGNED_BITS[datatype]
            if not 0 <= value < 1 << bits:
                raise CodecError(f"{what}: {value} doesn't fit {type_name(datatype)}")
            wire = value
        return _key(FIELD_NUMBERS["long" if bits == 64 else "int"], VARINT) + _varint(wire)
    if datatype in (FLOAT, DOUBLE):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CodecError(f"{what} is {type_name(datatype)}, so its value must be a number, got {value!r}")
        try:
            if datatype == FLOAT:
                return _key(FIELD_NUMBERS["float"], I32) + struct.pack("<f", float(value))
            return _key(FIELD_NUMBERS["double"], I64) + struct.pack("<d", float(value))
        except (OverflowError, struct.error) as e:
            raise CodecError(f"{what}: {value!r} doesn't fit {type_name(datatype)}") from e
    if datatype == BOOLEAN:
        if not isinstance(value, bool):
            raise CodecError(f"{what} is Boolean, so its value must be true or false, got {value!r}")
        return _key(FIELD_NUMBERS["boolean"], VARINT) + (b"\x01" if value else b"\x00")
    if datatype in TEXTS:
        if not isinstance(value, str):
            raise CodecError(f"{what} is {type_name(datatype)}, so its value must be text, got {value!r}")
        return _chunk(FIELD_NUMBERS["string"], value.encode("utf-8"))
    if datatype is None and value is None:
        return b""                           # a metric with no value field
    raise CodecError(f"{what}: can't encode a {type_name(datatype)} value")
