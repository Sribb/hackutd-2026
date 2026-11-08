"""Ingest: turn an uploaded oil field blueprint into blueprint.json.

    from ingest import ingest_bytes
    result = ingest_bytes(file_bytes, "site_plan.png")

The result holds the blueprint, a review list for the human to confirm, and hard
errors that block a build. JSON and CSV are parsed directly. Images, PDFs, and other
text are read by Claude.
"""
import json
import struct

from .assemble import assemble, missing_numbers, validate
from .claude_reader import IMAGE_TYPES, IngestError, read_plan
from .tabular import csv_extraction, looks_like_blueprint

__all__ = ["ingest_bytes", "IngestError"]

EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".webp": "image/webp", ".pdf": "application/pdf", ".csv": "text/csv", ".json": "application/json",
              ".txt": "text/plain"}


def _media_type(filename, given):
    for ext, mt in EXTENSIONS.items():
        if (filename or "").lower().endswith(ext):
            return mt
    return given or "application/octet-stream"


def _aspect(data, media_type):
    """Height over width of an image, so positions keep the drawing's shape."""
    try:
        if media_type == "image/png" and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return h / w
        if media_type == "image/jpeg":
            i = 2
            while i < len(data) - 9:
                marker, size = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return h / w
                i += 2 + size
    except (struct.error, ZeroDivisionError, IndexError):
        pass
    return 0.77 if media_type == "application/pdf" else 0.5


def _result(bp, review, source):
    errors = validate(bp)
    counts = {k: len(bp.get(k, []) or []) for k in ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards")}
    return {"ok": not errors, "errors": errors, "review": review, "counts": counts, "source": source, "blueprint": bp}


def ingest_bytes(data, filename="upload", media_type=None, client=None):
    mt = _media_type(filename, media_type)
    source = {"file": filename, "media_type": mt, "read_by": "parser"}
    text = None
    if not (mt in IMAGE_TYPES or mt == "application/pdf"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise IngestError("Upload a PNG, JPEG, WebP, GIF or PDF site plan, or a CSV or JSON equipment list.", 415)

    if mt == "application/json" or (text and text.lstrip().startswith("{")):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = None
        if looks_like_blueprint(obj):
            obj.setdefault("_ingest", {"source": source, "unclear": []})
            return _result(obj, missing_numbers(obj), source)

    if mt == "text/csv" or (text and filename.lower().endswith(".csv")):
        extraction = csv_extraction(text)
        if extraction is not None:
            bp, review = assemble(extraction, positions="meters", source=source)
            return _result(bp, review, source)

    extraction, meta = read_plan(data, mt, text=text, client=client)
    source.update({"read_by": "claude", **meta})
    bp, review = assemble(extraction, aspect=_aspect(data, mt), positions="fraction", source=source)
    return _result(bp, review, source)
