"""gaia on a dataset: jarvis.py's read tools answered from the active sponsor dataset.

With a dataset on screen, gaia's tools in backend/app/jarvis.py (list_alerts, explain_asset,
get_forecast, get_plans) would read the live demo field and answer about the wrong field. One
guarded line at the top of Jarvis.run_tool hands those tools (and propose_action) here while
active() is true. Each answer has the same top-level keys as jarvis.py's own, filled from the
active dataset as of the service's clock cursor (service.SERVICE.view): sponsor ids, the dataset's
clock in UTC, and a "source" naming the folder and the cut. Every number is the service's cached
detect and plan output, the same /api/eog and /api/dataset serve.

active() is true while dataset mode serves a dataset: once one has been activated in this process
by either route (POST /api/eog/datasets/activate or POST /api/dataset/load, which both set
Service.activated), or while the console's legacy routes serve one (eog/legacy_api.py, #153).
With no dataset ever activated and no legacy routes serving one, gaia reads the live field
exactly as before.

The dataset is a recording, so propose_action never makes an equipment action here: it answers
with a sentence saying a plan can be approved for its steps and a radio script instead.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from typing import Any, Optional

from . import service
from .dataset import Dataset
from .model import Event, money

log = logging.getLogger("freeboard.eog")

TOOLS = ("list_alerts", "explain_asset", "get_forecast", "get_plans", "propose_action", "get_week_history")
# The week-history tool Claude gets on top of jarvis.TOOLS while a dataset is on screen.
HISTORY_TOOL = {
    "name": "get_week_history",
    "description": "Every event the dataset on screen recorded from its first row up to its clock (pump failures, "
                   "pipeline leaks, worn pumps), oldest first, with each pump failure's response cost "
                   "(est_cost_usd: the plan made at its onset, or from now while it is still open), the total, "
                   "and the costliest failure. Call it for 'what happened this week', 'which failure cost the "
                   "most', 'list the failures'.",
    "strict": True,
    "input_schema": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
}
RECORDING = ("The dataset on screen is a recording, so gaia can't send equipment commands for it. "
             "A plan can be approved for its numbered steps and a radio script instead: "
             "POST /api/eog/plans/{event_id}/approve.")

def _legacy_on() -> bool:
    """The console's legacy routes serve the dataset (eog/legacy_api.py, #153, once it is mounted)."""
    mod = sys.modules.get(f"{__package__}.legacy_api")
    on = getattr(mod, "on", None)
    return bool(on()) if callable(on) else False


def active() -> bool:
    """True while dataset mode serves a dataset to someone: one was activated in this process,
    by any route (Service.activated: POST /api/eog/datasets/activate or POST /api/dataset/load),
    or the console's legacy routes serve one. False while dataset mode is off. Never raises."""
    try:
        svc = service.SERVICE
        if getattr(svc, "activated", None) is None and not _legacy_on():
            return False
        svc.require_enabled()
    except Exception:  # noqa: BLE001 - dataset mode off or broken: gaia answers from the live field
        return False
    return True


# ---------- shared pieces ----------

def _clock(ts: Optional[int]) -> Optional[str]:
    """'2026-10-10 09:00 UTC'."""
    if ts is None:
        return None
    return service._utc(ts).strftime("%Y-%m-%d %H:%M UTC")


def _hm(ts: Optional[int]) -> Optional[str]:
    return None if ts is None else service._utc(ts).strftime("%H:%M")


def _view() -> tuple[Dataset, Dataset, int]:
    return service.SERVICE.view(None)


def _head(ds: Dataset, view: Dataset, at: int) -> dict:
    return {"dataset": ds.id, "now": view.now, "clock": _hm(view.now), "as_of": _clock(view.now),
            "source": f"dataset {ds.id}, cut at {_clock(at)} (the recording's own clock, UTC)"}


def _kinds(ds: Dataset) -> dict[str, tuple[str, Any]]:
    out: dict[str, tuple[str, Any]] = {}
    for s in ds.systems.values():
        out[s.disposal_id] = ("disposal", s)
    for f in ds.facilities.values():
        out[f.id] = ("facility", f)
        out[f.tank_id] = ("tank", f)
        out[f.pump_id] = ("pump", f)
        out[f.pipeline_id] = ("pipeline", f)
        for w in f.wells:
            out[w.id] = ("well", (f, w))
    return out


def _bbl_hr(bpd: Optional[float]) -> Optional[float]:
    """A flow in barrels a day as barrels an hour, one decimal. Never below zero: a meter's
    noise around a stopped pump reads a hair under 0, which rounds to -0.0 and was spoken as
    "-0 barrels an hour"."""
    if bpd is None:
        return None
    return max(0.0, round(float(bpd) / 24.0, 1)) + 0.0


def _blind_tanks(ds: Dataset, view: Dataset) -> list[str]:
    """Tanks with no level reading at the view's now: unknown, never safe."""
    return [f.tank_id for f in ds.facilities.values() if view.value_at(f.tank_id, "level_pct") is None]


def _priority(ev: Event) -> str:
    if ev.category == "pump_failure":
        h = service.p50(ev)
        return "P1" if ev.tripped or (h is not None and h <= 1.0) else "P2"
    return "P2" if ev.category == "pipeline_leak" else "P3"


def _ongoing(ds: Dataset, view: Dataset) -> tuple[list[Event], dict]:
    return service.SERVICE.ordered(view)


def _row(ds: Dataset, view: Dataset, ev: Event, plans: dict) -> dict:
    svc = service.SERVICE
    p = plans.get(ev.id)
    fac = ds.facilities.get(ev.facility)
    return {"id": ev.id, "asset_id": ev.asset, "name": ev.asset, "kind": ev.category.upper(),
            "severity": "ALARM" if ev.category == "pump_failure" else "WARNING", "priority": _priority(ev),
            "message": svc._title(ds, view, ev), "raised_at": _hm(ev.onset), "raised_at_utc": _clock(ev.onset),
            # the message already says when the tank trips; jarvis's forecast sentence would use the live clock
            "consequence": None, "consequence_clock": None, "minutes_to_consequence": None,
            "minutes_from_now": None if ev.trips_at is None else int(round((ev.trips_at - view.now) / 60)),
            "usd_at_risk": 0 if p is None else round(float(p.cost), 2), "forecast_at": _clock(view.now),
            "reading": None, "root_id": None, "acked": False, "shelved": False, "shelved_until": None,
            "shelve_reason": None,
            "facility_id": ev.facility, "system_id": ev.system, "tank_id": fac.tank_id if fac else None,
            "evidence": list(ev.evidence or []), "hours_to_limit": ev.hours_to_limit,
            "trips_at_utc": _clock(ev.trips_at), "plan": None if p is None else p.summary}


# ---------- the tools ----------

def list_alerts() -> dict:
    ds, view, at = _view()
    order, plans = _ongoing(ds, view)
    groups = [{"root": _row(ds, view, ev, plans), "related": 0, "related_count": 0, "related_ids": [],
               "shelved_root": None} for ev in order]
    return {**_head(ds, view, at), "open": len(order), "groups": groups, "unknown_tanks": _blind_tanks(ds, view)}


def explain_asset(asset_id: str) -> dict:
    ds, view, at = _view()
    aid = str(asset_id or "").upper()
    kinds = _kinds(ds)
    if aid not in kinds:
        return {"error": f"Unknown asset {aid}."}
    kind, ref = kinds[aid]
    order, plans = _ongoing(ds, view)
    mine = [ev for ev in order if ev.asset == aid or ev.facility == aid
            or (kind in ("tank", "well") and ev.facility == (ref[0].id if kind == "well" else ref.id))]
    status = "OK"
    if any(ev.category == "pump_failure" for ev in mine):
        status = "ALARM"
    elif mine:
        status = "WARNING"
    metrics = {m: view.value_at(aid, m) for m in ds.metrics(aid)}
    live: dict = {"status": status, **metrics}
    design: dict = {}
    if kind == "tank":
        f = ref
        design = {"capacity_bbl": f.capacity_bbl, "shutdown_pct": ds.shutdown_pct, "warning_pct": ds.warning_pct,
                  "facility_id": f.id, "pump_id": f.pump_id}
        lvl = metrics.get("level_pct")
        if lvl is None:
            live["status"] = "UNKNOWN"
        elif status == "OK" and lvl >= ds.warning_pct:
            live["status"] = "WARNING"
    elif kind == "pump":
        f = ref
        design = {"max_flow_bpd": f.max_flow_bpd, "start_level_pct": f.start_level_pct,
                  "stop_level_pct": f.stop_level_pct, "tank_id": f.tank_id, "facility_id": f.id}
        flow = metrics.get("flow_bpd")
        live["running"] = metrics.get("status") == 1
        live["flow_bbl_hr"] = _bbl_hr(flow)
    elif kind == "well":
        f, w = ref
        design = {"baseline_water_bpd": w.baseline_water_bpd, "oil_bpd": w.oil_bpd, "gas_mcfd": w.gas_mcfd,
                  "facility_id": f.id, "lost_usd_per_hour_if_shut_in": round(ds.revenue_per_hour(w.id), 2)}
        water = metrics.get("water_bpd")
        live["flow_bbl_hr"] = _bbl_hr(water)
    elif kind == "pipeline":
        f = ref
        design = {"max_bpd": f.pipeline_max_bpd, "max_pressure_psi": f.pipeline_max_pressure_psi, "facility_id": f.id}
        flow = metrics.get("receive_flow_bpd")
        live["flow_bbl_hr"] = _bbl_hr(flow)
    elif kind == "facility":
        f = ref
        design = {"tank_id": f.tank_id, "pump_id": f.pump_id, "pipeline_id": f.pipeline_id,
                  "wells": list(f.well_ids), "system_id": f.system}
    else:
        s = ref
        design = {"system_id": s.id, "pressure_psi": s.disposal_pressure_psi}
    if all(v is None for v in metrics.values()) and kind not in ("facility", "disposal"):
        live["status"] = "UNKNOWN"
    out = {**_head(ds, view, at), "asset_id": aid, "name": aid, "kind": kind, "live": live,
           "reading": {"as_of": _clock(view.now), "metrics": metrics} if metrics else None, "design": design,
           "alerts": [{"id": ev.id, "kind": ev.category.upper(), "severity": "ALARM" if ev.category == "pump_failure"
                       else "WARNING", "priority": _priority(ev), "root_id": None,
                       "message": service.SERVICE._title(ds, view, ev)} for ev in mine]}
    if live["status"] == "UNKNOWN":
        out["say"] = (f"{aid} has no reading at {_clock(view.now)} in this dataset, so gaia can't say it is safe. "
                      f"Treat it as at risk and check in person.")
    return out


def get_forecast(actions: Any) -> dict:
    """If nobody acts: each ongoing pump failure's tank countdown (plan.countdown) and the cost of
    the automatic shut-ins. A recording can't replay hypothetical commands, so actions only warn."""
    from .dataset_api import do_nothing_cost

    ds, view, at = _view()
    order, plans = _ongoing(ds, view)
    events = service.SERVICE.events(view)
    ongoing = service.SERVICE.ongoing_failures(events)
    out_events, lost = [], 0.0
    for ev in order:
        if ev.category != "pump_failure":
            continue
        fac = ds.facilities.get(ev.facility)
        tank = fac.tank_id if fac else ev.facility
        repair_end = service.SERVICE.repair_end_of(ds, ev)
        if ev.tripped:
            text = f"{tank} is at shutdown; {ev.facility}'s wells stay shut in until the repair ends at {_clock(repair_end)}."
            out_events.append({"type": "SHUTDOWN_LEVEL", "asset_id": tank, "t_min": 0, "text": text})
        elif ev.trips_at is not None:
            text = (f"{tank} reaches shutdown at {_clock(ev.trips_at)} and {ev.facility}'s wells shut in "
                    f"until the repair ends at {_clock(repair_end)}.")
            out_events.append({"type": "SHUTDOWN_LEVEL", "asset_id": tank,
                               "t_min": round((ev.trips_at - view.now) / 60.0, 1), "text": text})
        cost = do_nothing_cost(ds, view, ev, ongoing, plans)
        lost += cost or 0.0
    warnings = []
    if actions:
        warnings.append("This dataset is a recording, so the actions asked about were not replayed: the forecast "
                        "is the do-nothing case. GET /api/eog/plans has every plan and POST /api/eog/whatif "
                        "replays a tank with sponsor actions.")
    blind = _blind_tanks(ds, view)
    warnings += [f"{t} has no level reading at {_clock(view.now)}." for t in blind]
    return {**_head(ds, view, at), "events": out_events[:12],
            "cost": {"total_usd": round(lost, 2), "trucking_usd": 0.0, "lost_production_usd": round(lost, 2),
                     "disposal_usd": 0.0},
            "warnings": warnings, "unknown_tanks": blind}


def _with_asset(a: dict, ev: Event, tank: str) -> dict:
    """Sponsor action plus an asset_id, which jarvis.py's plan sentence focuses the camera on."""
    a = dict(a)
    if a.get("type") == "shut_in_wells" and a.get("wells"):
        a.setdefault("asset_id", a["wells"][0])
    else:
        a.setdefault("asset_id", a.get("facility_id") or tank)
    return a


def get_plans() -> dict:
    ds, view, at = _view()
    order, plans = _ongoing(ds, view)
    rows = []
    for ev in order:
        p = plans.get(ev.id)
        if ev.category != "pump_failure" or p is None:
            continue
        fac = ds.facilities.get(ev.facility)
        tank = fac.tank_id if fac else ev.facility
        acts = [_with_asset(a, ev, tank) for a in p.actions or [] if a.get("type") != "none"]
        rows.append({"id": ev.id, "event_id": ev.id, "facility_id": ev.facility,
                     "summary": f"{ev.asset} failure at {ev.facility}: {p.summary}.",
                     "actions": acts, "total_usd": round(float(p.cost), 2), "keeps_running": not p.trips,
                     "robust": None, "cannot_save": [tank] if p.trips else [],
                     "expires_at_utc": _clock(p.expires_at)})
    return {**_head(ds, view, at), "unknown_tanks": _blind_tanks(ds, view), "plans": rows}


# ---------- the week's history (get_week_history, and the rules path's answers) ----------

def _usd(x: Optional[float]) -> str:
    return "an unknown cost" if x is None else money(x)


def _day(ts: Optional[int]) -> Optional[str]:
    """'Oct 08 10:00 UTC'."""
    return None if ts is None else service._utc(ts).strftime("%b %d %H:%M UTC")


def _n(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def week_history() -> dict:
    """Every event the active dataset recorded up to its clock, oldest first, from the service's
    cached detect and plan output (what /api/eog/events serves): each pump failure carries its
    response cost, the plan made at its onset once resolved, the plan from now while open."""
    svc = service.SERVICE
    ds, view, at = _view()
    events = svc.events(view)
    now = svc.plans_now(view, events)
    ref = (svc.plans_reference(view, events)
           if any(e.category == "pump_failure" and e.status != "ongoing" for e in events) else {})
    rows = []
    for ev in events:
        p = None
        if ev.category == "pump_failure":
            p = now.get(ev.id) if ev.status == "ongoing" else ref.get(ev.id)
        fac = ds.facilities.get(ev.facility)
        rows.append({"event_id": ev.id, "category": ev.category, "system_id": ev.system,
                     "facility_id": ev.facility, "asset_id": ev.asset, "tank_id": fac.tank_id if fac else None,
                     "status": ev.status, "onset": ev.onset, "onset_utc": _clock(ev.onset), "end_utc": _clock(ev.end),
                     "est_cost_usd": None if p is None else round(float(p.cost), 2),
                     "response": None if p is None else re.sub(r"\s*\(~\$[\d,.]+\)", "", str(p.summary)).strip()})
    failures = [r for r in rows if r["category"] == "pump_failure"]
    costed = [r for r in failures if r["est_cost_usd"] is not None]
    top = max(costed, key=lambda r: (r["est_cost_usd"], -r["onset"])) if costed else None
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    return {**_head(ds, view, at), "from": _clock(ds.start), "to": _clock(view.now), "events": rows,
            "counts": counts, "open": sum(r["status"] == "ongoing" for r in rows),
            "pump_failures": len(failures), "total_cost_usd": round(sum(r["est_cost_usd"] for r in costed), 2),
            "costliest": top,
            "cost_note": "est_cost_usd is each pump failure's best response (trucks plus any shut-in), "
                         "planned at its onset; an open one's is planned from now."}


COSTLIEST = re.compile(r"\b(cost(ed|s)? (us )?the most|most (expensive|costly)|costliest|biggest (cost|loss|bill)|"
                       r"worst (failure|event|one)|(highest|largest|top) cost|expensive(st)? (failure|one|event))\b")
LIST_FAILURES = re.compile(r"\b(list|name|show me) (all |every |the |this week'?s )*(failures|events|incidents)\b|"
                           r"\b(all|every) (the )?(pump )?(failures|events|incidents)\b|"
                           r"\bhow many (pump )?(failures|events|incidents)\b")
WEEK = re.compile(r"\b(what (happened|went wrong|has happened)|this week|past week|last week|the week|so far|"
                  r"recap|history|summar(y|ize|ise))\b")
CATEGORY_WORDS = {"pump_failure": "pump failure", "pipeline_leak": "pipeline leak",
                  "pump_degradation": "worn pump", "other": "other event"}


def history_answer(text: str) -> Optional[tuple[str, Optional[str]]]:
    """The rules path's answer to a week-history question while a dataset is on screen, as
    (sentence, asset id to fly to or None), or None when the question is not about the week's
    history. Every number is week_history()'s."""
    t = " " + re.sub(r"[?!.,]", " ", (text or "").lower()) + " "
    asks_top, asks_list, asks_week = COSTLIEST.search(t), LIST_FAILURES.search(t), WEEK.search(t)
    if not (asks_top or asks_list or asks_week):
        return None
    h = week_history()
    span = f"from {h['from']} to {h['to']} in {h['dataset']}"
    top = h["costliest"]
    if asks_top:
        if top is None:
            return f"No pump failure has a response cost {span}.", None
        return (f"The costliest failure {span} was {top['asset_id']} at {top['facility_id']}, which failed "
                f"{_day(top['onset'])}: its best response, {top['response']}, cost {_usd(top['est_cost_usd'])}. "
                f"All {_n(h['pump_failures'], 'pump failure')} came to {_usd(h['total_cost_usd'])}."), top["asset_id"]
    fails = [r for r in h["events"] if r["category"] == "pump_failure"]
    if asks_list:
        if not fails:
            return f"No pump failures {span}.", None
        items = "; ".join(f"{r['asset_id']} {_day(r['onset'])} ({_usd(r['est_cost_usd'])}"
                          f"{', still open' if r['status'] == 'ongoing' else ''})" for r in fails)
        return f"{_n(len(fails), 'pump failure')} {span}: {items}. Total {_usd(h['total_cost_usd'])}.", None
    if not h["events"]:
        return f"Nothing went wrong {span}: no failures, leaks or worn pumps.", None
    order = list(CATEGORY_WORDS)
    parts = [_n(n, CATEGORY_WORDS.get(c, c.replace("_", " "))) for c, n in
             sorted(h["counts"].items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order))]
    said = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
    line = (f"{span[0].upper()}{span[1:]}: {said}, {h['open']} still open. "
            f"The failures' responses cost {_usd(h['total_cost_usd'])} in all")
    if top is not None:
        line += f"; the costliest was {top['asset_id']} on {_day(top['onset'])} at {_usd(top['est_cost_usd'])}"
    return line + ".", (top or {}).get("asset_id")


# ---------- gaia's Claude prompt on a dataset ----------

def clock_note() -> Optional[str]:
    """'Dataset clock 2026-10-11 23:55 UTC (history/full_week, a recording)' for Claude's per-turn note."""
    try:
        ds, view, _ = _view()
    except Exception:  # noqa: BLE001 - no note beats a broken turn
        return None
    return f"Dataset clock {_clock(view.now)} ({ds.id}, a recording)"


def system_prompt() -> Optional[str]:
    """gaia's whole Claude system prompt for the dataset on screen: its systems, real asset ids,
    limits and prices, and its clock in UTC. None when no dataset is active, so jarvis.py keeps
    the demo field's prompt."""
    if not active():
        return None
    try:
        ds, view, _ = _view()
    except Exception:  # noqa: BLE001 - a broken dataset keeps the demo prompt, never breaks gaia
        log.exception("dataset prompt failed")
        return None
    systems = {}
    for s in ds.systems.values():
        facs = {}
        for fid in s.facility_ids:
            f = ds.facilities[fid]
            facs[fid] = {"tank": f.tank_id, "capacity_bbl": f.capacity_bbl, "pump": f.pump_id,
                         "pump_max_bbl_hr": round(f.max_flow_bpd / 24.0, 1), "pipeline": f.pipeline_id,
                         "wells": list(f.well_ids)}
        systems[s.id] = {"disposal_site": s.disposal_id, "emergency_trucks": s.fleet_size, "facilities": facs}
    n_fac = sum(len(s.facility_ids) for s in ds.systems.values())
    return (
        "You are gaia (always lowercase), the night-shift operator's assistant on a console for an oil "
        "field's produced-water network. Your name is gaia and nothing else. "
        f"The console is replaying the sponsor's recorded dataset {ds.id}, not a live field: its clock is the "
        f"recording's own, in UTC, from {_clock(ds.start)} to now, {_clock(view.now)}. "
        f"The field has {_n(len(ds.systems), 'system')}, {_n(n_fac, 'facility', 'facilities')} and "
        f"{_n(len(ds.wells), 'well')}. At each facility the wells fill one tank with produced water and one "
        "pump sends it down a pipeline to the system's disposal site. If a tank reaches "
        f"{ds.shutdown_pct:g}% (warning at {ds.warning_pct:g}%), the facility's wells shut in and production is "
        f"lost. A failed pump takes about {ds.repair_hours:g} hours to repair. Each system has its own emergency "
        f"trucks; a trip carries {ds.truck_capacity_bbl:g} bbl and costs {money(ds.truck_cost)}. The operator "
        "sees the field in 3D next to you and may send a screenshot with the selected asset.\n\n"
        "You act on the screen. Show, don't tell: when someone asks to see, understand or be walked through "
        "something, move the view with ui_action, one call per step, in the order a person would look. A "
        "walk-through of what's wrong: list_alerts; ui_action focus on the failed pump; ui_action trace_pipes "
        "from it to its tank; get_forecast; ui_action set_time to the shutdown clock the forecast gave; "
        "ui_action chart on that tank; get_plans; ui_action open_panel plans; then say the cheapest plan can be "
        "approved on its plan card. Give each step a short lowercase label with the real id ('flying to "
        "s4_pump_a', 'jumping to 11:57 utc', 'opening the plans').\n\n"
        "\"This\", \"it\" and \"that\" mean the asset selected on screen, named in the note before the "
        "question. If nothing is selected and the question needs one, ask which asset.\n\n"
        "How to answer:\n"
        "- Your reply is read aloud: at most two short, plain sentences. No markdown or lists. The screen "
        "carries the detail.\n"
        "- Every number you state (levels, times, rates, dollars) must come from a tool result in this turn and "
        "say it is the dataset's, with its UTC clock ('S4_TANK_A reads 71% at 2026-10-10 09:00 UTC in the "
        "dataset.'). Times are UTC; never say AM or PM.\n"
        "- If a tool reports a missing reading, say so first with the tool's `say` sentence; an unknown tank is "
        "never safe.\n"
        "- For what's failing now, call list_alerts and lead with the first group's root cause and its clock.\n"
        "- For the week's history ('what happened this week', 'which failure cost the most', 'list the "
        "failures'), call get_week_history. Its costs are each failure's best response (est_cost_usd); say so.\n"
        "- Look up an asset with explain_asset before describing it. To point the camera, use ui_action focus "
        "(focus_camera only knows the demo field).\n"
        "- For 'what if' use get_forecast (the recording's do-nothing case); for the cheapest fix use get_plans.\n"
        "- This is a recording: you never move equipment, and propose_action can't either. Say the plan can "
        "be approved on the plans card for its steps and a radio script.\n"
        "- Name assets by the ids the console shows (S4_TANK_A, S4_PUMP_A, S2_WELL_A1). This field has no "
        "'Tank 3' or 'Pump 3'; never use the demo field's names.\n\n"
        f"Oil {money(ds.oil_price)}/bbl, gas ${ds.gas_price:g}/mcf.\n"
        f"Systems and asset IDs: {json.dumps(systems, separators=(',', ':'))}"
    )


async def run_tool(tool: str, args: dict, ctx: dict) -> Any:
    """jarvis.py's tool on the active dataset (TOOLS). Blocking work runs off the event loop."""
    args = args or {}
    try:
        if tool == "list_alerts":
            return await asyncio.to_thread(list_alerts)
        if tool == "explain_asset":
            return await asyncio.to_thread(explain_asset, args.get("asset_id", ""))
        if tool == "get_forecast":
            return await asyncio.to_thread(get_forecast, args.get("actions") or [])
        if tool == "get_plans":
            return await asyncio.to_thread(get_plans)
        if tool == "get_week_history":
            return await asyncio.to_thread(week_history)
        if tool == "propose_action":
            return {"error": RECORDING}
    except service.ServiceError as e:
        return {"error": e.sentence}
    return {"error": f"Unknown tool {tool}."}
