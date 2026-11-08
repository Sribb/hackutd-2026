"""Every tool schema gaia sends to Claude must pass the API's schema check.

The API rejects an enum on a type list (type ["string", "null"] with None in the enum) with a
400 for the whole request, so one bad tool silently broke every Claude answer (2026-11-08).
"""
from __future__ import annotations

from backend.app import jarvis


def _enum_problems(node, path=""):
    out = []
    if isinstance(node, dict):
        if "enum" in node and (isinstance(node.get("type"), list) or None in node["enum"]):
            out.append(f"{path}: type={node.get('type')!r} enum={node['enum']!r}")
        for k, v in node.items():
            out += _enum_problems(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _enum_problems(v, f"{path}[{i}]")
    return out


def test_no_enum_on_a_type_list_or_null_in_an_enum():
    tools = list(jarvis.TOOLS) + [jarvis._eog_gaia().HISTORY_TOOL]
    problems = [p for t in tools for p in _enum_problems(t.get("input_schema", {}), t["name"])]
    assert not problems, "use anyOf [{type: string, enum: [...]}, {type: null}] instead:\n" + "\n".join(problems)


def test_at_most_20_strict_tools_in_either_mode():
    """The API allows at most 20 strict tools in one request; over that, every answer is a 400."""
    base = list(jarvis.TOOLS)
    for tools in (base, base + [jarvis._eog_gaia().HISTORY_TOOL]):
        assert sum(1 for t in tools if t.get("strict")) <= 20
