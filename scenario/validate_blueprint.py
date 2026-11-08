"""Check a blueprint against docs/blueprint.schema.json and the cross-references a schema can't express.

    python3 scenario/validate_blueprint.py scenario/field.json [more.json ...]
    python3 scenario/validate_blueprint.py --draft upload.json   # ingest draft: list what a person still has to fill

Exit 0 when every file is a complete blueprint, 1 when any file has problems, 2 when
jsonschema isn't installed (pip install jsonschema). --draft always exits 0 and prints
the missing numbers as a to-do list, because a draft is allowed to have gaps.
"""
import json
import sys
from pathlib import Path

SCHEMA = Path(__file__).resolve().parent.parent / "docs" / "blueprint.schema.json"
WELLS_PER_TANK, PUMPS_PER_TANK, XO_PER_TANK = 8, 2, 4   # generator limits (blender/blueprint.py)


def _ids(bp):
    out = {}
    for key in ("tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards", "equipment"):
        for a in bp.get(key) or []:
            if isinstance(a, dict) and isinstance(a.get("id"), str):
                out.setdefault(a["id"], []).append(key)
    return out


def cross_checks(bp):
    """Problems the schema can't see: duplicate IDs, references to missing assets, per-tank limits."""
    out = []
    ids = _ids(bp)
    for aid, keys in ids.items():
        if len(keys) > 1:
            out.append(f"ID {aid} is used {len(keys)} times ({', '.join(keys)}).")
    kind = {aid: keys[0] for aid, keys in ids.items()}

    def must(where, ref, expected):
        if ref is None:
            return
        if ref not in kind:
            out.append(f"{where} names {ref}, which isn't in this blueprint.")
        elif expected and kind[ref] != expected:
            out.append(f"{where} names {ref}, which is in {kind[ref]}, not {expected}.")

    per_tank = {t["id"]: {"wells": 0, "pumps": 0, "xo": 0} for t in bp.get("tanks") or [] if isinstance(t, dict) and "id" in t}
    for p in bp.get("pumps") or []:
        must(f"{p.get('id')} tank_id", p.get("tank_id"), "tanks")
        must(f"{p.get('id')} valve_id", p.get("valve_id"), "valves")
        must(f"{p.get('id')} swd_id", p.get("swd_id"), "disposal_wells")
        if p.get("tank_id") in per_tank:
            per_tank[p["tank_id"]]["pumps"] += 1
    for w in bp.get("wells") or []:
        must(f"{w.get('id')} tank_id", w.get("tank_id"), "tanks")
        if w.get("tank_id") in per_tank:
            per_tank[w["tank_id"]]["wells"] += 1
    owner = {}
    for v in bp.get("valves") or []:
        ends = v.get("connects") or []
        for e in ends:
            must(f"{v.get('id')} connects", e, None)
        if v.get("kind") == "CROSSOVER":
            if len(ends) == 2 and ends[0] == ends[1]:
                out.append(f"{v.get('id')} connects {ends[0]} to itself.")
            for e in ends:
                if e in per_tank:
                    per_tank[e]["xo"] += 1
        elif v.get("kind") == "DISCHARGE":
            pump = v.get("pump_id") or (ends[0] if ends else None)
            must(f"{v.get('id')} pump_id", v.get("pump_id"), "pumps")
            if v.get("pump_id") and ends and ends[0] != v["pump_id"]:
                out.append(f"{v.get('id')} pump_id {v['pump_id']} doesn't match connects[0] {ends[0]}.")
            if pump in owner:
                out.append(f"{pump} has two discharge valves, {owner[pump]} and {v.get('id')}.")
            owner[pump] = v.get("id")
    for p in bp.get("pumps") or []:
        vid = p.get("valve_id")
        if vid and owner.get(p.get("id")) not in (None, vid):
            out.append(f"{p.get('id')} valve_id is {vid}, but {owner[p['id']]} names this pump.")
    for e in bp.get("equipment") or []:
        must(f"{e.get('id')} tank_id", e.get("tank_id"), "tanks")
        for c in e.get("connects") or []:
            must(f"{e.get('id')} connects", c, None)
    for tr in bp.get("trucks") or []:
        must(f"{tr.get('id')} home_id", tr.get("home_id"), "yards")
        for t in (tr.get("minutes_to") or {}):
            must(f"{tr.get('id')} minutes_to", t, "tanks")
    for t in ((bp.get("initial") or {}).get("tank_pct") or {}):
        must("initial.tank_pct", t, "tanks")
    for f in bp.get("faults") or []:
        must("faults", f.get("asset_id"), None)
    for j, link in enumerate(bp.get("connections") or []):
        ends = link if isinstance(link, list) else [link.get("from"), link.get("to")]
        for e in ends:
            must(f"connections[{j}]", e, None)
    for c in bp.get("connectors") or []:
        for s in c.get("signals") or []:
            must(f"{c.get('id')} signal", s.get("asset_id"), None)
        for cmd in c.get("commands") or []:
            must(f"{c.get('id')} command", cmd.get("asset_id"), None)
    for tid, n in per_tank.items():
        if n["wells"] > WELLS_PER_TANK:
            out.append(f"{tid} has {n['wells']} wells; the generator builds 0 to {WELLS_PER_TANK} per tank.")
        if n["pumps"] > PUMPS_PER_TANK:
            out.append(f"{tid} has {n['pumps']} pumps; the generator builds 0 to {PUMPS_PER_TANK} per tank.")
        if n["xo"] > XO_PER_TANK:
            out.append(f"{tid} is on {n['xo']} crossovers; the generator builds up to {XO_PER_TANK} per tank.")
    return out


def schema_problems(bp, validator):
    out = []
    for err in sorted(validator.iter_errors(bp), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "(top level)"
        msg = err.message
        if err.validator == "oneOf" and isinstance(err.instance, dict):
            # a valve that fits neither kind: report the errors for the kind it says it is
            branch = {"DISCHARGE": 0, "CROSSOVER": 1}.get(err.instance.get("kind"))
            mine = [e for e in err.context if e.relative_schema_path and e.relative_schema_path[0] == branch]
            if mine:
                msg = "; ".join(sorted({e.message for e in mine}))
        elif err.validator == "not" and "config" in err.absolute_path:
            msg = f"{err.instance!r} looks like a secret. Name an environment variable instead, e.g. password_env."
        out.append(f"{where}: {msg}")
    return out


def check(bp, validator):
    """All problems for one blueprint, empty when it's complete."""
    if not isinstance(bp, dict):
        return ["A blueprint must be a JSON object."]
    return schema_problems(bp, validator) + cross_checks(bp)


def main(argv):
    draft = "--draft" in argv
    paths = [a for a in argv if a != "--draft"]
    if not paths:
        print(__doc__.strip())
        return 1
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print("SKIP blueprint check: jsonschema isn't installed (pip install jsonschema)")
        return 2
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))
    failed = False
    for path in paths:
        try:
            bp = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"FAIL {path}: {e}")
            failed = True
            continue
        problems = check(bp, validator)
        if not problems:
            print(f"PASS {path}")
        elif draft:
            print(f"DRAFT {path}: {len(problems)} item(s) to fill before it's complete")
        else:
            print(f"FAIL {path}: {len(problems)} problem(s)")
            failed = True
        for p in problems:
            print(f"  - {p}")
    return 0 if draft or not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
