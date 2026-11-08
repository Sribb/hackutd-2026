"""Build web/public/fake/*.json: contract-shaped responses so the console runs with no backend.

These files are data, not a second forecast. Every number comes from the Last Call
reference forecast (lastcall/sim.py) on the team's scenario/field.json (Vinny's file).
Run it again whenever that scenario changes:

    python3 web/scripts/make_fakes.py                 # needs lastcall/ in the repo (PR #10)
    python3 web/scripts/make_fakes.py --lastcall DIR  # or a folder that contains lastcall/
    python3 web/scripts/make_fakes.py --from-api http://127.0.0.1:8000   # record the real backend (preferred)

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
ap.add_argument("--scenario", default=None, help="field file (default: scenario/field.json, else lastcall/scenario.json)")
ap.add_argument("--from-api", default=None, metavar="URL",
                help="record every response from a running backend instead (e.g. http://127.0.0.1:8000)")
args = ap.parse_args()


def _from_api(base: str) -> None:
    """Record the real backend's answers, so the offline console shows the product's numbers."""
    import urllib.request

    def call(path, body=None):
        req = urllib.request.Request(base.rstrip("/") + path, method="POST" if body is not None else "GET",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    def out(name, obj):
        (OUT / name).write_text(json.dumps(obj, separators=(",", ":")) + "\n")

    note = f"Recorded from the backend at {base}. Not live."
    field, state = call("/api/field"), call("/api/state")
    out("field.json", field)
    out("state.json", state)
    fc = call("/api/forecast", {"horizon_minutes": 360, "step_minutes": 5, "actions": []})
    fc["warnings"] = [note] + list(fc.get("warnings") or [])
    out("forecast.json", fc)
    lead = {"SET_VALVE_manual": 20, "SEND_TRUCK": 5, "CHOKE_WELL": 2, **(field.get("lead_min") or {})}
    ev = next((e for e in fc["events"] if e["type"] == "SHUTDOWN_LEVEL"), None)
    tank = ev["asset_id"] if ev else "TANK_3"
    xos = [v for v in field["valves"] if tank in v.get("connects", []) and all(c.startswith("TANK_") for c in v["connects"])]
    changing = sorted({tank, *[c for v in xos for c in v["connects"]]})
    lead.setdefault("SET_VALVE_remote", 1)
    # One list of choices per lever, None meaning "leave it alone". Matches options() in main.js.
    flat = [[None, {"type": "SET_VALVE", "asset_id": v["id"], "value": "OPEN",
                    "at_min": lead["SET_VALVE_remote" if v.get("manual") is False else "SET_VALVE_manual"]}] for v in xos]
    flat += [[None, {"type": "SEND_TRUCK", "asset_id": tr["id"], "target_id": tank, "at_min": lead["SEND_TRUCK"]}]
             for tr in field["trucks"] if tank in (tr.get("minutes_to") or {})]
    flat += [[None] + [{"type": "CHOKE_WELL", "asset_id": w["id"], "value": v, "at_min": lead["CHOKE_WELL"]} for v in (0.5, 0.0)]
             for w in field["wells"] if w["tank_id"] == tank]
    whatif = {}
    for combo in itertools.product(*flat):
        acts = [a for a in combo if a]
        if not acts:
            continue
        r = call("/api/forecast", {"horizon_minutes": 360, "step_minutes": 5, "actions": acts})
        k = "|".join(sorted(f"{a['type']}:{a['asset_id']}:{a.get('value', '')}:{a.get('target_id', '')}" for a in acts))
        whatif[k] = {"tanks": {t: [row["tanks"][t] for row in r["timeline"]] for t in changing},
                     "wells_online": [row.get("wells_online") for row in r["timeline"]],
                     "events": r["events"], "cost": r["cost"]}
    out("whatif.json", {"step_minutes": 5, "horizon_minutes": 360, "changing": changing, "results": whatif})
    plans = call("/api/plans", {"horizon_minutes": 360})
    out("plans.json", plans)
    out("approve.json", {p["id"]: call(f"/api/plans/{p['id']}/approve", {}) for p in plans.get("plans", [])})
    try:
        out("lastcall.json", call("/api/lastcall", {}))
    except Exception as e:  # Last Call not mounted on that backend
        print("lastcall skipped:", e)
    print(f"wrote {OUT}: {len(whatif)} what-if results, {len(plans.get('plans', []))} plans, from {base}")


if args.from_api:
    _from_api(args.from_api)
    sys.exit(0)
sys.path.insert(0, args.lastcall)
from lastcall import core, sim  # noqa: E402

_team = ROOT / "scenario" / "field.json"
SCENARIO = args.scenario or str(_team if _team.exists() else Path(args.lastcall) / "lastcall" / "scenario.json")
S = core.load_scenario(SCENARIO)
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
