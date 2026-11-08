"""Score dataset mode against an answer key (a developer check, never part of the app).

    python -m backend.app.eog.score <dataset folder> --key <answer_key.json> [--at <unix s>]

It runs the same Service the /api/eog routes use, so the numbers are what the API serves.
The app itself never opens answer_key.json; this CLI reads it only because you pass it.

Prints a detection table (key event vs ours: category, asset, onset and end error in
minutes, status), a plan table for every ongoing pump failure (the key's reference action,
level and hours to limit next to ours), and a last line "SCORE PASS ..." or "SCORE FAIL: ...".

A key event is found when one of ours has the same asset and category and its onset is
within 3 h. PASS needs every key event found, no extra events, and for every ongoing pump
failure hours_to_limit within 0.02 h and the plan cost within $5 of the reference.
A status that differs is reported but does not fail the score (a leak that starts or ends
while its pump is off shows in no reading).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ONSET_TOL_S = 3 * 3600
HOURS_TOL = 0.02
COST_TOL = 5.0


def _ours(folder: Path, at: int | None) -> tuple[list[dict], dict]:
    """(events.json rows, {event_id: alert entry}) for the folder, as the API serves them."""
    os.environ["FREEBOARD_EOG_DIR"] = str(folder.parent)
    from .service import Service  # after the env var: the service reads it on first use

    svc = Service()
    svc.activate(folder.name)
    _, events = svc.events_export(at)
    alerts = {a["event_id"]: a for a in svc.alerts(at).get("alerts", [])}
    return events, alerts


def _min(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds / 60:+.0f}"


def _ref_cost(text: str) -> float | None:
    m = re.search(r"\$([\d,]+(?:\.\d+)?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def score(folder: Path, key_path: Path, at: int | None = None) -> bool:
    key = json.loads(key_path.read_text(encoding="utf-8"))
    ours, alerts = _ours(folder, at)
    used: set[int] = set()
    missed, status_diff, plan_bad = [], 0, []
    print(f"detection: {folder.name} ({len(key)} key events, {len(ours)} ours)")
    print(f"  {'key':10} {'category':17} {'asset':18} {'onset':>6} {'end':>6}  status")
    for k in key:
        best = None
        for i, e in enumerate(ours):
            if i in used or e.get("asset_id") != k["asset"] or e.get("category") != k["category"]:
                continue
            d = abs((e.get("onset_unix") or 0) - k["onset_unix"])
            if d <= ONSET_TOL_S and (best is None or d < best[1]):
                best = (i, d)
        if best is None:
            missed.append(k["id"])
            print(f"  {k['id']:10} {k['category']:17} {k['asset']:18} {'MISSED':>6}")
            continue
        used.add(best[0])
        e = ours[best[0]]
        onset_err = e["onset_unix"] - k["onset_unix"]
        end_err = (e["end_unix"] - k["end_unix"]) if e.get("end_unix") and k.get("end_unix") else None
        same = e.get("status") == k["status"]
        status_diff += 0 if same else 1
        print(f"  {k['id']:10} {k['category']:17} {k['asset']:18} {_min(onset_err):>6} {_min(end_err):>6}  "
              f"{e.get('status')}{'' if same else ' (key: ' + k['status'] + ')'}")
        if k["category"] == "pump_failure" and k["status"] == "ongoing":
            h = e.get("hours_to_limit")
            h50 = h.get("p50") if isinstance(h, dict) else h
            a = alerts.get(e.get("event_id"), {})
            action = a.get("action") or {}
            cost = e.get("est_cost") if e.get("est_cost") is not None else action.get("cost")
            ref = _ref_cost(k.get("reference_action", ""))
            ok_h = h50 is not None and k.get("hours_to_limit_final") is not None and abs(h50 - k["hours_to_limit_final"]) <= HOURS_TOL
            ok_c = cost is not None and ref is not None and abs(cost - ref) <= COST_TOL
            if not (ok_h and ok_c):
                plan_bad.append(k["id"])
            lvl = (a.get("countdown") or {}).get("level_pct")
            plan_bad_mark = "" if ok_h and ok_c else "  <- differs"
            print(f"      plan {k['id']}: key {k.get('reference_action')}, level {k.get('level_final_pct')}%, "
                  f"{k.get('hours_to_limit_final')} h, order {k.get('order')}")
            print(f"      ours: {action.get('summary') or '-'} (${cost:,.2f}), level "
                  f"{'-' if lvl is None else round(lvl, 1)}%, {h50} h, order {a.get('order')}{plan_bad_mark}"
                  if cost is not None else f"      ours: no plan{plan_bad_mark}")
    extra = [e for i, e in enumerate(ours) if i not in used]
    for e in extra:
        print(f"  EXTRA      {e.get('category'):17} {e.get('asset_id'):18} onset {e.get('onset_ts')}")
    found = len(key) - len(missed)
    counts = (f"{found}/{len(key)} found, {len(extra)} extra, {status_diff} status differs, "
              f"{len(plan_bad)} plan differs")
    ok = not missed and not extra and not plan_bad
    print(f"SCORE PASS ({counts})" if ok else f"SCORE FAIL: {counts}")
    return ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("folder", type=Path, help="a dataset folder (network.json, ops.json, telemetry.csv)")
    p.add_argument("--key", type=Path, help="answer_key.json (default: the one in the folder)")
    p.add_argument("--at", type=int, default=None, help="score as of this unix time (default: the last reading)")
    a = p.parse_args(argv)
    folder = a.folder.resolve()
    key = (a.key or folder / "answer_key.json").resolve()
    if not key.is_file():
        print(f"No answer key at {key}. Pass --key.", file=sys.stderr)
        return 2
    return 0 if score(folder, key, a.at) else 1


if __name__ == "__main__":
    sys.exit(main())
