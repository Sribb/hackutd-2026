"""What Claude returns when it reads a site plan, and the rules for blueprint.json.

The extraction schema follows the structured outputs subset: every object sets
additionalProperties false, nullable fields use anyOf, and there are no numeric limits.
"""
import re

KINDS = ["tank", "pump", "discharge_valve", "crossover_valve", "well", "disposal_well", "truck", "yard"]

ID_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _nullable(json_type):
    return {"anyOf": [{"type": json_type}, {"type": "null"}]}


ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "label": {"type": "string"},
        "feeds_label": _nullable("string"),
        "connects_labels": {"type": "array", "items": {"type": "string"}},
        "capacity_bbl": _nullable("number"),
        "rate_bbl_hr": _nullable("number"),
        "shutdown_pct": _nullable("number"),
        "lost_usd_hr": _nullable("number"),
        "manual": _nullable("boolean"),
        "x": _nullable("number"),
        "y": _nullable("number"),
        "confidence": {"type": "number"},
        "evidence": {"type": "string"},
    },
    "required": ["kind", "label", "feeds_label", "connects_labels", "capacity_bbl", "rate_bbl_hr",
                 "shutdown_pct", "lost_usd_hr", "manual", "x", "y", "confidence", "evidence"],
    "additionalProperties": False,
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "site_name": _nullable("string"),
        "meters_per_drawing_width": _nullable("number"),
        "items": {"type": "array", "items": ITEM_SCHEMA},
        "unclear": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["site_name", "meters_per_drawing_width", "items", "unclear"],
    "additionalProperties": False,
}

# Numbers the backend forecast needs per asset (from Mounish's list on the board).
# Ingest never guesses these. A missing one goes on the review list instead.
REQUIRED_NUMBERS = {
    "tanks": ["capacity_bbl", "shutdown_pct"],
    "pumps": ["rate_bbl_hr"],
    "wells": ["water_bbl_hr", "lost_usd_hr"],
    "disposal_wells": ["max_bbl_hr"],
    "trucks": ["capacity_bbl", "minutes_to"],
}
# A crossover's numbers, asked for together so the console can show one row per valve.
CROSSOVER_NEEDS = ["max_bbl_hr", "manual"]

REVIEW_CONFIDENCE = 0.8   # items Claude is less sure about than this go on the review list
DEFAULT_SITE_WIDTH_M = 450.0
