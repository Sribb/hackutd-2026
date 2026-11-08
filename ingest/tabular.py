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
    "shutdown": "shutdown_pct", "shutdown_%": "shutdown_pct", "lost": "lost_usd_hr", "lost_$/hr": "lost_usd_hr",
    "lost_usd": "lost_usd_hr", "mode": "manual", "operation": "manual", "drive_minutes": "minutes_to",
}
MANUAL_WORDS = {"manual": True, "yes": True, "true": True, "y": True, "1": True, "hand": True,
                "remote": False, "no": False, "false": False, "n": False, "0": False, "auto": False, "automated": False}
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


def _manual(value):
    """'manual'/'yes' -> True, 'remote'/'no' -> False, blank or anything else -> None (asked on review)."""
    return MANUAL_WORDS.get((value or "").strip().lower())


def _minutes_to(value, row, unclear):
    """'Tank 1:20; Tank 2:25' -> {"Tank 1": 20.0, "Tank 2": 25.0}. Tank labels are resolved later."""
    out = {}
    for part in re.split(r"[;|]", value or ""):
        if not part.strip():
            continue
        label, _, minutes = part.rpartition(":")
        if not label.strip() or _num(minutes.strip()) is None:
            unclear.append(f"Row {row}: can't read drive time {part.strip()!r}. Write it as 'Tank 1:20'.")
            continue
        out[label.strip()] = _num(minutes.strip())
    return out


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
            "shutdown_pct": _num(rec.get("shutdown_pct")), "lost_usd_hr": _num(rec.get("lost_usd_hr")),
            "manual": _manual(rec.get("manual")), "minutes_to_labels": _minutes_to(rec.get("minutes_to"), i, unclear),
            "x": _num(rec.get("x")), "y": _num(rec.get("y")), "confidence": 1.0, "evidence": f"CSV row {i}",
        })
    return {"site_name": None, "meters_per_drawing_width": None, "items": items, "unclear": unclear}
