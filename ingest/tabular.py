"""Uploads that are already structured: a blueprint JSON, or a CSV equipment list. No Claude call."""
import csv
import io
import re

from .schema import KINDS

COLUMN_ALIASES = {
    "type": "kind", "equipment": "kind", "asset": "kind",
    "name": "label", "tag": "label", "id": "label",
    "tank": "feeds_label", "tank_id": "feeds_label", "feeds": "feeds_label", "flows_to": "feeds_label",
    "capacity": "capacity_bbl", "rate": "rate_bbl_hr", "connects": "connects_labels",
}
KIND_ALIASES = {
    "swd": "disposal_well", "disposal": "disposal_well", "injection_well": "disposal_well",
    "pumpjack": "well", "crossover": "crossover_valve", "xo": "crossover_valve",
    "discharge": "discharge_valve", "vacuum_truck": "truck", "office": "yard",
}


def looks_like_blueprint(obj):
    return isinstance(obj, dict) and isinstance(obj.get("tanks"), list)


def _num(value):
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def csv_extraction(text):
    """Return an extraction dict, or None if the columns aren't recognizable (then Claude reads it)."""
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        return None
    header = [COLUMN_ALIASES.get(h.strip().lower().replace(" ", "_"), h.strip().lower().replace(" ", "_"))
              for h in rows[0]]
    if "kind" not in header or "label" not in header:
        return None
    items, unclear = [], []
    for i, row in enumerate(rows[1:], start=2):
        rec = dict(zip(header, row))
        kind = rec.get("kind", "").strip().lower().replace(" ", "_")
        kind = KIND_ALIASES.get(kind, kind)
        if kind not in KINDS:
            unclear.append(f"Row {i}: unknown kind {rec.get('kind')!r}")
            continue
        connects = [p.strip() for p in re.split(r"[;|]", rec.get("connects_labels", "") or "") if p.strip()]
        items.append({
            "kind": kind, "label": rec.get("label", "").strip(), "feeds_label": (rec.get("feeds_label") or "").strip() or None,
            "connects_labels": connects, "capacity_bbl": _num(rec.get("capacity_bbl")), "rate_bbl_hr": _num(rec.get("rate_bbl_hr")),
            "x": _num(rec.get("x")), "y": _num(rec.get("y")), "confidence": 1.0, "evidence": f"CSV row {i}",
        })
    return {"site_name": None, "meters_per_drawing_width": None, "items": items, "unclear": unclear}
