"""Build web/public/fake/*.json: contract-shaped responses so the console runs with no backend.

These files are data, not a second forecast. Every number comes from the Last Call
reference forecast (lastcall/sim.py) on the Last Call placeholder scenario.
Run it again whenever that scenario changes:

    python3 web/scripts/make_fakes.py                 # needs lastcall/ in the repo (PR #10)
    python3 web/scripts/make_fakes.py --lastcall DIR  # or a folder that contains lastcall/

Writes:
  field.json, state.json, forecast.json   GET /api/field, GET /api/state, POST /api/forecast with no actions
  whatif.json                             POST /api/forecast for every combination the console offers
  plans.json, approve.json                POST /api/plans, POST /api/plans/{id}/approve
  lastcall.json                           POST /api/lastcall
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "web" / "public" / "fake"

ap = argparse.ArgumentParser()
ap.add_argument("--lastcall", default=str(ROOT), help="folder that contains the lastcall package")
args = ap.parse_args()
sys.path.insert(0, args.lastcall)
from lastcall import core, sim  # noqa: E402

S = core.load_scenario(str(Path(args.lastcall) / "lastcall" / "scenario.json"))
LEAD = S["lead_min"]
INCIDENT_TANK = "TANK_3"


def dump(name: str, obj) -> None:
    (OUT / name).write_text(json.dumps(obj, separators=(",", ":")) + "\n")


# ---- GET /api/field and /api/state ----
field = {
    "tanks": [{k: t[k] for k in ("id", "capacity_bbl", "shutdown_pct")} for t in S["tanks"]],
    "pumps": [{k: p[k] for k in ("id", "tank_id", "rate_bbl_hr")} for p in S["pumps"]],
    "valves": [{"id": f"VALVE_D_{i}", "connects": [f"PUMP_{i}", "SWD_1" if i <= 3 else "SWD_2"]} for i in range(1, 7)]
    + [{k: v[k] for k in ("id", "connects", "max_bbl_hr", "manual")} for v in S["valves"]],
    "wells": S["wells"],
    "disposal_wells": S["disposal_wells"],
    "trucks": [{k: t[k] for k in ("id", "capacity_bbl", "minutes_to")} for t in S["trucks"]],
    "yards": [{"id": "YARD_1"}],
    "lead_min": {k: v for k, v in LEAD.items() if not k.startswith("_")},
}
assets = {}
for t in S["tanks"]:
    assets[t["id"]] = {"status": "OK", "level_pct": t["level_pct"]}
assets[INCIDENT_TANK].update(status="WARNING", reason="Filling at 180 bbl/hr with Pump 3 down.")
for p in S["pumps"]:
    assets[p["id"]] = {"status": p["status"], **({"reason": p["reason"]} if p.get("reason") else {})}
for i in range(1, 7):
    assets[f"VALVE_D_{i}"] = {"status": "OK", "position": "OPEN"}
assets["VALVE_XO_34"] = {"status": "OK", "position": "CLOSED"}
for w in S["wells"]:
    assets[w["id"]] = {"status": "OK"}
for d in S["disposal_wells"]:
    assets[d["id"]] = {"status": "OK"}
for t in S["trucks"]:
    assets[t["id"]] = {"status": "OK", "location": "YARD_1"}
assets["YARD_1"] = {"status": "OK"}
state = {"clock": S["clock"], "assets": assets,
         "incident": {"root_asset_id": "PUMP_3", "summary": "Pump 3 failed. Tank 3 is filling."}}
dump("field.json", field)
dump("state.json", state)

FAKE_WARNING = "Fake data from the Last Call placeholder scenario. Not from the backend."


def forecast(actions):
    fc = sim.forecast(S, actions, 360, 5, with_range=True)
    fc["warnings"] = [FAKE_WARNING]
    return fc


dump("forecast.json", forecast([]))

# ---- what-if: every combination of the levers the console offers ----
# Keys must match actionKey() in web/src/api.js: TYPE:ASSET:VALUE:TARGET, sorted, joined by "|".
def key(a):
    return f"{a['type']}:{a['asset_id']}:{a.get('value', '')}:{a.get('target_id', '')}"


xo = [[], [{"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "at_min": LEAD["SET_VALVE_manual"]}]]
trucks = [[], [{"type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": INCIDENT_TANK, "at_min": LEAD["SEND_TRUCK"]}]]
trucks2 = [[], [{"type": "SEND_TRUCK", "asset_id": "TRUCK_2", "target_id": INCIDENT_TANK, "at_min": LEAD["SEND_TRUCK"]}]]
wells = [w["id"] for w in S["wells"] if w["tank_id"] == INCIDENT_TANK]
choke_opts = [[None, 0.5, 0.0]] * len(wells)

whatif = {}
changing = ["TANK_3", "TANK_4"]
for a, b, c in itertools.product(xo, trucks, trucks2):
    for keeps in itertools.product(*choke_opts):
        acts = a + b + c + [{"type": "CHOKE_WELL", "asset_id": w, "value": v, "at_min": LEAD["CHOKE_WELL"]}
                            for w, v in zip(wells, keeps) if v is not None]
        if not acts:
            continue
        fc = sim.forecast(S, acts, 360, 5, with_range=True)
        whatif["|".join(sorted(key(x) for x in acts))] = {
            # Compact: only the tanks that can change, as arrays on the 5-minute grid.
            "tanks": {t: [r["tanks"][t] for r in fc["timeline"]] for t in changing},
            "wells_online": [r["wells_online"] for r in fc["timeline"]],
            "events": fc["events"],
            "cost": fc["cost"],
        }
dump("whatif.json", {"step_minutes": 5, "horizon_minutes": 360, "changing": changing, "results": whatif})

# ---- plans, approve, lastcall ----
lc = core.last_call(S)
plans, approve = [], {}
for i, o in enumerate(lc["options"], 1):
    template = next(p for p in core.candidate_plans(S, [INCIDENT_TANK]) if p["id"] == o["plan_id"])
    acts = core.schedule(template, 0)
    pid = f"plan_{i}"
    plans.append({
        "id": pid, "summary": o["summary"], "actions": acts, "cost": o["cost"],
        "keeps_running": True, "cannot_save": [],
        "expires_clock": o["expires_clock"], "robust": o["robust"],
    })
    steps = [{"n": s["n"], "asset_id": s["asset_id"], "text": s["text"]} for s in o["steps"]]
    lines = [f"Field, this is control. Pump 3 is down and Tank 3 is filling. Shutdown at {lc['shutdown']['clock']} if we do nothing."]
    lines += [f"Step {s['n']}: {s['text']}" for s in steps]
    lines.append(f"This plan only works if we start by {o['expires_clock']}. Confirm when each step is done. Control out.")
    approve[pid] = {"steps": steps, "radio_script": " ".join(lines)}
dump("plans.json", {"plans": plans})
dump("approve.json", approve)
dump("lastcall.json", lc)

print(f"wrote {OUT}: {len(whatif)} what-if results, {len(plans)} plans")
