"""CF-12: every claim in scenario/PITCH.md and DEVPOST.md's "How we compare" carries a
source tag that docs/COMPETITIVE.md defines, and every link matches its Sources list.

    python -m pytest -q scenario/tests/test_pitch.py
    COMPETITIVE_MD=/path/to/COMPETITIVE.md python -m pytest -q scenario/tests/test_pitch.py

SKIPs the cross-check until docs/COMPETITIVE.md is in the checkout (PR #110).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PITCH = ROOT / "scenario" / "PITCH.md"
DEVPOST = ROOT / "scenario" / "DEVPOST.md"
COMPETITIVE = Path(os.environ.get("COMPETITIVE_MD", ROOT / "docs" / "COMPETITIVE.md"))

TAG_RE = re.compile(r"\[([A-Z]{1,2}\d{1,2})\]")
URL_RE = re.compile(r"https?://\S+")
MATRIX_ROWS = {  # PITCH.md's F and N tags -> the COMPETITIVE.md matrix row each one cites
    "F1": "Forecast of what happens next", "F2": "Cheapest fix (optimization)",
    "F3": "How long until a fix expires", "N1": "Cheapest fix (optimization)",
    "N2": "How long until a fix expires",
}


def compare_section() -> str:
    text = DEVPOST.read_text(encoding="utf-8")
    m = re.search(r"^## How we compare\s*$(.*?)^## ", text, re.S | re.M)
    assert m, "DEVPOST.md has no '## How we compare' section"
    return m.group(1)


def slide_and_paragraph() -> str:
    text = PITCH.read_text(encoding="utf-8")
    return text.split("## The slide", 1)[1]


def claims(text: str) -> list[str]:
    """Bullets and paragraph sentences that make a claim (skip headings, tables, links, and
    the footnote line, which lists the sources themselves)."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "|", "---", "[", "**Footnote", "**Title")) or URL_RE.search(s):
            continue
        out += [x for x in re.split(r"(?<=[.!?])\s+", s) if len(x.split()) > 4]
    return out


def test_the_paragraph_is_the_same_in_pitch_and_devpost():
    para = PITCH.read_text(encoding="utf-8").rsplit("\n\n", 1)[-1].strip()
    assert para.startswith("At 1:47 AM") and para in compare_section()


def test_every_competitor_claim_has_a_tag():
    """Each sentence that names a competitor, or says "found", carries a tag."""
    who = re.compile(r"Ignition|AVEVA|Experion|Honeywell|eLynx|WellAware|Cognite|EOG|found", re.I)
    text = slide_and_paragraph().split("### Matrix rows", 1)[0] + slide_and_paragraph().split("## Devpost", 1)[1]
    untagged = [c for c in claims(text) if who.search(c) and not TAG_RE.search(c)]
    assert not untagged, untagged


@pytest.mark.skipif(not COMPETITIVE.exists(), reason="docs/COMPETITIVE.md not in this checkout yet (PR #110)")
def test_every_tag_and_link_resolves_in_competitive_md():
    comp = COMPETITIVE.read_text(encoding="utf-8")
    sources = comp.split("## Sources", 1)[1]
    defined = set(re.findall(r"^- \[([A-Z]{1,2}\d{1,2})\]", sources, re.M))
    for name, text in (("PITCH.md", PITCH.read_text(encoding="utf-8")), ("DEVPOST.md", compare_section())):
        used = set(TAG_RE.findall(text))
        unknown = used - defined - set(MATRIX_ROWS)
        assert not unknown, f"{name} uses tags COMPETITIVE.md doesn't define: {sorted(unknown)}"
    links = {u.rstrip(").,") for u in URL_RE.findall(compare_section())}
    assert links and all(u in sources for u in links), links - {u for u in links if u in sources}
    matrix = comp.split("## 2. Feature matrix", 1)[1].split("## 3.", 1)[0]
    rows = {r.split("|")[1].strip() for r in matrix.splitlines() if r.startswith("| ")}
    assert set(MATRIX_ROWS.values()) <= rows, set(MATRIX_ROWS.values()) - rows
