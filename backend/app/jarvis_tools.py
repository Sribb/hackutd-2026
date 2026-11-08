"""Jarvis console tools (docs/JARVIS_CONSOLE.md section 5): the deterministic tools both
engines share, the screen directives, and the `brief` feed.

Hooked into jarvis.py with a few lines:
    TOOLS += jarvis_tools.TOOLS                  Claude sees every tool below
    run_tool() asks jarvis_tools.run() first     PASS means "not mine, use jarvis.py's"
    _focus / _note_ui go through focus()         so every directive has an id
    the system prompt ends with PROMPT

Nothing here moves equipment. `propose_action` (jarvis.py) and `execute_plan` (here)
store PENDING proposals; a person confirms each one with a full hold on the console.
`offer_ack` and `offer_shelve` only put a card on screen; the person's tap or hold
sends the request. Every number in an answer comes from one of these results.

Directives (the reply's `ui[]`, also broadcast on /ws as type `ui`):
    {"type": "ui", "id": "ui_7f3a", "action": "explode", "asset_id": "PUMP_3", "args": {},
     "source": "jarvis", "reason": "open pump 3"}
check_directive() is the one whitelist, for Jarvis's own directives and for the ones a
tokened client sends over /ws. At most MAX_UI directives per answer.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
import secrets
from typing import Any, Optional

from . import parts_catalog
from .fieldmap import name
from .forecast import clock_at
from .live import ActionError, LiveField, describe as describe_action

log = logging.getLogger("freeboard.jarvis_tools")

PASS = object()          # run(): not a tool this module owns
MAX_UI = 3               # directives per answer
MAX_HIGHLIGHT = 24
HORIZON_MIN = 360
BRIEF_EVERY_MIN = 5.0    # live minutes between brief recomputes with nothing new
NODE = re.compile(r"^(SENSOR|RTU|PIPE)_[A-Z0-9_]+$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PANELS = ("alerts", "plans", "walk", "whatif", "asset", "log", "tanks")
DIRECTIONS = ("downstream", "upstream", "both")
ACTIONS = ("focus", "highlight", "explode", "collapse", "isolate", "show_sensors", "trace_pipes",
           "set_time", "reset_view", "open_panel", "try_whatif")
NEEDS_ASSET = ("focus", "explode", "isolate", "trace_pipes")


# ---------------------------------------------------------------- directives ----

def _set_time_args(live: LiveField, args: dict) -> tuple[Optional[dict], Optional[str]]:
    t_min, clock = args.get("t_min"), args.get("clock")
    if t_min is None and clock:
        m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$", str(clock).lower())
        if not m:
            return None, f"Can't read the clock {clock!r}. Use '3:00 AM' or '03:00'."
        h, mm, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
        if h > 23 or mm > 59 or (ap and not 1 <= h <= 12):
            return None, f"Can't read the clock {clock!r}."
        if ap:
            h = h % 12 + (12 if ap == "pm" else 0)
        now_h, now_m = (int(x) for x in live.fmap.clock(live.t).split(":"))
        t_min = (h * 60 + mm - (now_h * 60 + now_m)) % (24 * 60)
    try:
        t = float(t_min)
    except (TypeError, ValueError):
        return None, "set_time needs t_min (0 to 360 minutes ahead) or a clock time."
    if not 0 <= t <= HORIZON_MIN:
        return None, "I can only show the next 6 hours."
    t = min(HORIZON_MIN, int(5 * round(t / 5)))
    return {"t_min": t, "clock": clock_at(live.fmap.clock(live.t), t)}, None


def _clean_actions(live: LiveField, actions: Any) -> tuple[Optional[list], Optional[str]]:
    """Forecast actions checked against the field (as live.clean_action does), keeping at_min."""
    if not isinstance(actions, list) or not actions:
        return None, "Give at least one action."
    if len(actions) > 6:
        return None, "At most 6 actions in a what-if."
    out = []
    for a in actions:
        if not isinstance(a, dict):
            return None, "Each action is an object."
        try:
            clean = live.clean_action(a)
        except ActionError as e:
            return None, e.detail
        at = a.get("at_min")
        try:
            at = None if at is None else max(0.0, min(float(at), float(HORIZON_MIN)))
        except (TypeError, ValueError):
            return None, f"at_min must be minutes from now, got {at!r}."
        clean["at_min"] = lead_min(live, clean) if at is None else at
        out.append(clean)
    return out, None


DATASET_ID = re.compile(r"^S\d+_[A-Z0-9]+(?:_[A-Z0-9]+)*$")


def _dataset_active() -> bool:
    """True while an EOG sponsor dataset drives the console (backend/app/eog/gaia.py)."""
    try:
        from .eog import gaia
        return bool(gaia.active())
    except Exception:  # noqa: BLE001 - no dataset package or it failed to load: live field only
        return False


def check_directive(live: LiveField, d: Any) -> tuple[Optional[dict], Optional[str]]:
    """(clean directive, None) or (None, why not). The whitelist in JARVIS_CONSOLE.md section 5.
    Accepts {action, asset_id?, args?} with args also allowed at the top level (as a
    client might send them). `id`, `source`, and `reason` are left to the caller."""
    if not isinstance(d, dict):
        return None, "A directive is an object."
    action = str(d.get("action", ""))
    if action not in ACTIONS:
        return None, f"Unknown screen action {action!r}."
    raw = d.get("args") if isinstance(d.get("args"), dict) else {}
    raw = {**{k: v for k, v in d.items() if k not in ("type", "id", "seq", "action", "asset_id", "args",
                                                        "source", "reason")}, **raw}
    aid = str(d.get("asset_id") or raw.get("asset_id") or "").upper()
    f = live.fmap
    args: dict = {}

    def known(x: str) -> bool:
        # The live field's ids, plus a sponsor dataset's ids (S1_PUMP_C, S4_TANK_A) while the
        # EOG dataset drives the console. Screen directives only: nothing here reaches equipment.
        return bool(f.kind_of(x)) or (_dataset_active() and bool(DATASET_ID.match(x)))

    if action in NEEDS_ASSET or (action == "show_sensors" and aid != "ALL"):
        if not known(aid):
            return None, f"Unknown asset {aid or '(none)'}."
    if action == "highlight":
        ids = raw.get("ids")
        if not isinstance(ids, list) or not ids:
            return None, "highlight needs ids."
        ids = [str(x).upper() for x in ids][:MAX_HIGHLIGHT]
        bad = [x for x in ids if not (known(x) or NODE.match(x))]
        if bad:
            return None, f"Unknown ids {', '.join(bad[:3])}."
        try:
            secs = float(raw.get("seconds") or 20)
        except (TypeError, ValueError):
            secs = 20.0
        args = {"ids": list(dict.fromkeys(ids)), "seconds": max(1.0, min(secs, 120.0))}
        aid = ""
    elif action == "trace_pipes":
        direction = str(raw.get("direction") or "both").lower()
        if direction not in DIRECTIONS:
            return None, "direction is downstream, upstream, or both."
        args = {"direction": direction}
    elif action == "set_time":
        args, why = _set_time_args(live, raw)
        if why:
            return None, why
        aid = ""
    elif action == "open_panel":
        panel = str(raw.get("panel") or "").lower()
        if panel not in PANELS:
            return None, f"panel is one of {', '.join(PANELS)}."
        args = {"panel": panel}
        if aid and not f.kind_of(aid):
            return None, f"Unknown asset {aid}."
    elif action == "try_whatif":
        acts, why = _clean_actions(live, raw.get("actions"))
        if why:
            return None, why
        args = {"actions": acts}
        aid = acts[0]["asset_id"]
    elif action in ("collapse", "reset_view"):
        aid = ""
    out = {"type": "ui", "action": action}
    if aid:
        out["asset_id"] = aid
    out["args"] = args
    return out, None


def new_id() -> str:
    return "ui_" + secrets.token_hex(3)


async def emit(live: LiveField, d: dict, ctx: dict, reason: str = "", source: str = "jarvis",
               broadcast: bool = True) -> dict:
    """Check, number, record in ctx["ui"], and broadcast one directive. Returns {ok, id} or
    {error}. The same directive twice in a row is recorded once."""
    clean, why = check_directive(live, d)
    if why:
        return {"error": why}
    ui = ctx.setdefault("ui", [])
    if ui and all(ui[-1].get(k) == clean.get(k) for k in ("action", "asset_id", "args")):
        return {"ok": True, "id": ui[-1]["id"]}
    if len(ui) >= MAX_UI:
        return {"error": f"At most {MAX_UI} screen changes per answer."}
    clean.update(id=new_id(), source=source, reason=str(reason or "")[:200])
    ui.append(clean)
    if broadcast:
        await live.ui(clean)
    return {"ok": True, "id": clean["id"]}


async def focus(jarvis, aid: str, ctx: dict) -> None:
    """jarvis.py _focus: a focus directive, broadcast."""
    await emit(jarvis.live, {"action": "focus", "asset_id": aid}, ctx)


def note_focus(live: LiveField, aid: str, ctx: dict) -> None:
    """jarvis.py _note_ui: a focus directive in the reply only (the proposal already moved
    every screen through live.focus)."""
    clean, why = check_directive(live, {"action": "focus", "asset_id": aid})
    ui = ctx.setdefault("ui", [])
    if why or (ui and ui[-1].get("action") == "focus" and ui[-1].get("asset_id") == aid) or len(ui) >= MAX_UI:
        return
    clean.update(id=new_id(), source="jarvis", reason="")
    ui.append(clean)


def extras(ctx: dict) -> dict:
    """Extra reply fields: the ack and shelve cards Jarvis offered."""
    return {"offers": ctx.get("offers", [])}


# ---------------------------------------------------------------- helpers ----

def lead_min(live: LiveField, a: dict) -> float:
    """Minutes before an action takes effect, from the field's lead_min (as planner.py)."""
    leads = live.scenario.get("field", {}).get("lead_min", {}) or {}
    if a["type"] == "SET_VALVE":
        manual = bool(live.fmap.valves.get(a["asset_id"], {}).get("manual"))
        return float(leads.get("SET_VALVE_manual" if manual else "SET_VALVE_remote", 0))
    return float(leads.get(a["type"], 0))


def names_of(ids: list) -> str:
    n = [name(t) for t in ids]
    return n[0] if len(n) == 1 else ", ".join(n[:-1]) + " and " + n[-1] if n else ""


def unknown_tanks(live: LiveField) -> list[str]:
    st = live.state()["assets"]
    return sorted(t for t in live.fmap.tanks if st.get(t, {}).get("status") == "UNKNOWN")


def lost_feeds(live: LiveField) -> list[dict]:
    """Connectors that are down (CONN_SIM never is)."""
    return [c for c in live.registry.status() if c["status"] != "connected"]


def blind_text(unknown: list) -> str:
    """CF-11's sentence for tanks Jarvis can't see (jarvis.no_current_reading), first in any answer."""
    if not unknown:
        return ""
    from .jarvis import no_current_reading   # late: jarvis.py imports this module
    many = len(unknown) > 1
    return (no_current_reading(names_of(unknown), "tank", many=many)
            + f" Treat {'them' if many else 'it'} as at risk and check in person. ")


def _first_shutdown(fc: dict, tank: Optional[str] = None) -> Optional[dict]:
    return next((e for e in fc.get("events", []) if e.get("type") == "SHUTDOWN_LEVEL"
                 and (tank is None or e.get("asset_id") == tank)), None)


def countdown_of(live: LiveField, fc: dict, unknown: list, tank: Optional[str] = None) -> dict:
    """get_countdown from a baseline forecast."""
    now = live.fmap.clock(live.t)
    ev = _first_shutdown(fc, tank)
    cd = None
    if ev:
        lo, hi = ev.get("t_min_low"), ev.get("t_min_high")
        cd = {"asset_id": ev["asset_id"], "name": name(ev["asset_id"]), "minutes": int(round(ev["t_min"])),
              "t_min": ev["t_min"], "clock": clock_at(now, ev["t_min"]),
              "t_min_low": lo, "low_clock": None if lo is None else clock_at(now, lo),
              "t_min_high": hi, "high_clock": None if hi is None else clock_at(now, hi),
              "wells_lost": list(ev.get("wells_lost") or []), "consequence_t": round(live.t + ev["t_min"], 2)}
    others = [w for w in fc.get("warnings", []) if not any(w.upper().startswith(t + " ") for t in unknown)]
    warn = (" It also warns: " + others[0]) if others else ""
    if cd:
        says = forecast_says(live, ev["text"], ev["t_min"])
    elif tank and tank in unknown:
        says = ""
    elif tank:
        says = forecast_says(live, f"{name(tank)} doesn't reach shutdown in the next 6 hours if nothing changes.")
    elif unknown:
        says = forecast_says(live, "no tank with a fresh reading reaches shutdown in the next 6 hours if nothing changes.")
    else:
        says = forecast_says(live, "no tank reaches shutdown in the next 6 hours if nothing changes.")
    text = blind_text(unknown) + says + (warn if says else "")
    return {"clock": now, "as_of": live.fmap.clock_text(live.t), "countdown": cd, "unknown_tanks": list(unknown),
            "text": text.strip()}


def forecast_says(live: LiveField, text: str, t_min: Optional[float] = None) -> str:
    """CF-11's wording (jarvis._forecast_says): 'The forecast at 1:52 AM says ... That's 55 min from now.'"""
    line = f"The forecast at {live.fmap.clock_text(live.t)} says {text.strip()}"
    if not line.endswith("."):
        line += "."
    if t_min is not None:
        mins = int(round(t_min))
        line += " That's now." if mins <= 0 else f" That's {mins} min from now."
    return line


def _plan_row(p: Optional[dict]) -> Optional[dict]:
    if not p:
        return None
    return {"id": p["id"], "summary": p["summary"], "actions": p["actions"],
            "total_usd": (p.get("cost") or {}).get("total_usd", p.get("total_usd")),
            "robust": bool(p.get("robust")), "keeps_running": p.get("keeps_running"),
            "cannot_save": p.get("cannot_save", [])}


def plan_pair(plans: list) -> dict:
    """{cheapest, robust} plans that do something. A do-nothing plan is no fix."""
    acting = [p for p in plans if p.get("actions")]
    cheapest = acting[0] if acting else None
    robust = next((p for p in acting if p.get("robust")), None)
    return {"cheapest": _plan_row(cheapest), "robust": _plan_row(robust)}


async def baseline(jarvis) -> tuple[dict, dict]:
    sc = jarvis.live.live_scenario()
    fc = await asyncio.to_thread(jarvis.forecast_fn, sc, [])
    return sc, fc


def _unknown_from(sc: dict, fc: dict) -> list:
    unknown = fc.get("unknown_tanks")
    if unknown is None:
        unknown = sorted(t for t, lv in (sc.get("initial_levels_pct") or {}).items() if lv is None)
    return list(unknown)


# ---------------------------------------------------------------- Last Call ----

def _lastcall_scenario(live: LiveField, sc: dict) -> dict:
    """The flat field shape lastcall/core.py reads (main.py lastcall_scenario), from live readings."""
    f, st = sc["field"], live.state()["assets"]
    levels = sc.get("initial_levels_pct") or {}
    return {
        "clock": live.fmap.clock(live.t),
        "tanks": [{**t, "level_pct": levels.get(t["id"])} for t in f["tanks"]],
        "pumps": [{**p, "status": st.get(p["id"], {}).get("status", "OK"),
                   **({"reason": st[p["id"]]["reason"]} if st.get(p["id"], {}).get("reason") else {})}
                  for p in f["pumps"]],
        "valves": [{**v, "position": v.get("position", "CLOSED")} for v in f["valves"]],
        "wells": f["wells"], "disposal_wells": f["disposal_wells"], "trucks": f["trucks"],
        "lead_min": f.get("lead_min", {}), "costs": f.get("costs", {}),
    }


def last_call_now(live: LiveField) -> dict:
    """get_last_call: lazy lastcall.core on the live field, or {error}. Synchronous (run it
    in a thread): it replays the forecast many times."""
    try:
        from lastcall.core import last_call  # type: ignore
    except ImportError:
        return {"error": "Last Call isn't installed on this server (lastcall/ is not on this branch)."}
    from .forecast import build_plant, forecast
    sc = live.live_scenario()
    try:
        plant = build_plant(sc)
        res = last_call(_lastcall_scenario(live, sc), forecast_fn=lambda actions, inflow_scale=1.0: forecast(
            sc, actions, inflow_scale=inflow_scale, with_range=False, plant=plant))
    except Exception as e:  # noqa: BLE001 - Last Call must never break Jarvis
        log.exception("last call failed")
        return {"error": f"Last Call failed: {e}"}
    now = live.fmap.clock(live.t)
    options = res.get("options") or []
    cheap = next((o for o in options if o.get("expires_min") is not None), None)
    pnr = res.get("point_of_no_return")
    return {
        "status": res.get("status"), "clock": now, "computed_t": round(live.t, 2),
        "headline": res.get("headline") or res.get("text"),
        "cheapest": None if not cheap else {
            "plan_id": cheap.get("plan_id"), "summary": cheap.get("summary"),
            "total_usd": (cheap.get("cost") or {}).get("total_usd") if isinstance(cheap.get("cost"), dict) else cheap.get("cost"),
            "expires_min": cheap.get("expires_min"), "expires_clock": cheap.get("expires_clock") or clock_at(now, cheap["expires_min"]),
            "minutes_left": int(round(cheap["expires_min"])), "robust": cheap.get("robust")},
        "point_of_no_return": None if not pnr else {
            "t_min": pnr.get("t_min"),
            "clock": pnr.get("clock") or (clock_at(now, pnr["t_min"]) if pnr.get("t_min") is not None else None),
            "minutes_left": None if pnr.get("t_min") is None else int(round(pnr["t_min"]))},
        "useful_until": {aid: {"t_min": a.get("useful_until_min"),
                               "clock": None if a.get("useful_until_min") in (None, -1)
                               else clock_at(now, a["useful_until_min"])}
                         for aid, a in (res.get("assets") or {}).items()},
        "do_nothing_usd": res.get("do_nothing_usd"),
        "unknown_tanks": res.get("unknown_tanks", []),
    }


LAST_CALL_FRESH_MIN = 10.0   # live minutes a brief's Last Call is reused for get_last_call


def aged_last_call(live: LiveField) -> Optional[dict]:
    """The brief feed's Last Call (it takes seconds to compute), with minutes_left counted
    down to now, if it is from the open incident and under LAST_CALL_FRESH_MIN old."""
    feed = getattr(live, "brief_feed", None)
    lc = getattr(feed, "_last_call", None)
    if not lc or "error" in lc or lc.get("computed_t") is None or not live.detector.active:
        return None
    age = live.t - float(lc["computed_t"])
    if not 0 <= age <= LAST_CALL_FRESH_MIN:
        return None
    out = copy.deepcopy(lc)
    for key in ("cheapest", "point_of_no_return"):
        part = out.get(key)
        if part and part.get("minutes_left") is not None:
            part["minutes_left"] = max(0, int(round(part["minutes_left"] - age)))
    out["age_min"] = round(age, 2)
    return out


def last_call_text(lc: dict) -> str:
    if "error" in lc:
        return lc["error"]
    blind = blind_text(lc.get("unknown_tanks") or [])
    c = lc.get("cheapest")
    if not c:
        return (blind + (lc.get("headline") or "No plan has a deadline right now.")).strip()
    if c["minutes_left"] <= 0:
        line = f"The cheapest plan expired at {c['expires_clock']}: {c['summary']} Ask for the plans again."
    else:
        line = f"The cheapest plan expires at {c['expires_clock']}, in {c['minutes_left']} minutes: {c['summary']}"
    pnr = lc.get("point_of_no_return")
    if pnr and pnr.get("clock"):
        line += f" After {pnr['clock']} no plan keeps every well online."
    return (blind + line).strip()


# ---------------------------------------------------------------- tools ----

def get_state(live: LiveField) -> dict:
    st = live.state()
    counts: dict = {}
    for a in st["assets"].values():
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    active = [a for a in live.detector.active.values()]
    roots = sorted((a for a in active if not a.get("root_id")),
                   key=lambda a: (a.get("priority") or "P3", a["severity"] != "ALARM"))
    inc = roots[0] if roots else None
    lost = lost_feeds(live)
    return {"clock": st["clock"], "clock_text": live.fmap.clock_text(live.t), "t": st["t"],
            "connectors": st["connectors"], "feed_ok": not lost,
            "lost_feeds": [{"id": c["id"], "detail": c["detail"]} for c in lost],
            "status_counts": counts, "unknown_tanks": unknown_tanks(live),
            "active_alerts": len(active),
            "incident": None if not inc else {"alert_id": inc["id"], "asset_id": inc["asset_id"],
                                              "priority": inc.get("priority"), "message": inc["message"]},
            "speed": st["speed"], "paused": st["paused"]}


PRIO_ORDER = {"P1": 0, "P2": 1, "P3": 2}


def _derived_priority(a: dict) -> str:
    m = a.get("minutes_to_consequence")
    return "P1" if m is not None and m < 60 else "P2" if m is not None and m < 180 else "P3"


def list_alerts(live: LiveField, active_only: bool = True, priority: Optional[str] = None,
                group: bool = True) -> dict:
    alerts = [a for a in live.alerts() if a["active"] or not active_only]
    derived = any("priority" not in a for a in alerts)
    related: dict = {}
    for a in alerts:
        if a.get("root_id") and a["active"]:
            related.setdefault(a["root_id"], []).append(a["id"])
    rows = []
    for a in alerts:
        if group and a.get("root_id") and any(x["id"] == a["root_id"] for x in alerts):
            continue
        prio = a.get("priority") or _derived_priority(a)
        if priority and prio != priority.upper():
            continue
        rows.append({"id": a["id"], "asset_id": a["asset_id"], "kind": a["kind"], "severity": a["severity"],
                     "message": a["message"], "clock": a.get("clock"), "active": a["active"],
                     "priority": prio, "root_id": a.get("root_id"),
                     "related_count": len(related.get(a["id"], [])), "related_ids": related.get(a["id"], []),
                     "minutes_to_consequence": a.get("minutes_to_consequence"),
                     "consequence": a.get("consequence"), "usd_at_risk": a.get("usd_at_risk", 0),
                     "acked": bool(a.get("acked")), "shelved_until": a.get("shelved_until"),
                     "shelved_until_t": a.get("shelved_until_t")})
    rows.sort(key=lambda r: (r["shelved_until_t"] is not None, PRIO_ORDER.get(r["priority"], 3),
                             r["severity"] != "ALARM",
                             r["minutes_to_consequence"] if r["minutes_to_consequence"] is not None else 1e9))
    return {"clock": live.fmap.clock(live.t), "alerts": rows,
            "priority_source": "derived" if derived else "forecast",
            "unknown_tanks": unknown_tanks(live), "feed_ok": not lost_feeds(live)}


def sensor_nodes(live: LiveField, aid: str) -> list[str]:
    """Model nodes that show this asset's readings (names.py: tanks and pumps today)."""
    kind = live.fmap.kind_of(aid)
    if kind == "tank":
        return [f"SENSOR_{aid}", f"{aid}_lt", f"{aid}_lshh"]
    if kind == "pump":
        return [f"SENSOR_{aid}"]
    return []


def list_sensors(live: LiveField, asset_id: Optional[str] = None) -> dict:
    f, d = live.fmap, live.detector
    st = live.state()["assets"]
    ids = [asset_id.upper()] if asset_id else [a for a in f.all_ids() if f.kind_of(a) != "truck"]
    if asset_id and not f.kind_of(ids[0]):
        return {"error": f"Unknown asset {asset_id}."}
    keys = sorted(set(live.latest) | set(d.expected_keys))
    rows = []
    for aid in ids:
        sigs = []
        for (a, s) in keys:
            if a != aid:
                continue
            r = live.latest.get((a, s))
            conn = live.registry.owner.get((a, s), live.registry.sim)
            sigs.append({"signal": s, "value": None if r is None else r["value"],
                         "age_min": None if r is None else round(live.t - r["t"], 2),
                         "stale": (a, s) in d.stale_keys or r is None, "connector_id": conn.id})
        conns = sorted({x["connector_id"] for x in sigs})
        rows.append({"asset_id": aid, "name": name(aid), "kind": f.kind_of(aid),
                     "status": st.get(aid, {}).get("status"), "stale": any(x["stale"] for x in sigs),
                     "signals": sigs, "connectors": conns, "nodes": sensor_nodes(live, aid)})
    status = {c["id"]: c["status"] for c in live.registry.status()}
    for r in rows:
        r["connector_status"] = {c: status.get(c) for c in r["connectors"]}
    return {"clock": live.fmap.clock(live.t), "stale_after_min": d.stale_after, "sensors": rows,
            "stale": [r["asset_id"] for r in rows if r["stale"]]}


SIGNAL_WORDS = {"level_pct": "level", "flow_bbl_hr": "flow", "running": "run status", "position": "position",
                "pressure_psi": "pressure", "motor_amps": "motor current"}


def reading_text(signal: str, value: Any) -> str:
    if value is None:
        return f"{SIGNAL_WORDS.get(signal, signal)} unknown"
    if signal == "level_pct":
        return f"level {float(value):.1f}%"
    if signal == "flow_bbl_hr":
        return f"flow {float(value):.0f} bbl/hr"
    if signal == "running":
        return "running" if value else "stopped"
    if signal == "pressure_psi":
        return f"pressure {float(value):.0f} psi"
    if signal == "motor_amps":
        return f"motor current {float(value):.0f} A"
    return f"{SIGNAL_WORDS.get(signal, signal)} {str(value).lower()}"


def _ago(age_min: Optional[float]) -> str:
    return "just now" if age_min is not None and age_min < 1 else f"{age_min:.0f} min ago"


def sensors_text(res: dict, one: bool) -> str:
    if "error" in res:
        return res["error"]
    rows = res["sensors"]
    if one:
        r = rows[0]
        parts = []
        for s in r["signals"]:
            word = SIGNAL_WORDS.get(s["signal"], s["signal"])
            if s["stale"]:
                age = "ever" if s["age_min"] is None else f"{s['age_min']:.0f} min"
                parts.append(f"no {word} reading for {age}, so treat it as unknown")
            else:
                parts.append(f"{reading_text(s['signal'], s['value'])}, {_ago(s['age_min'])}")
        nm = r["name"][0].upper() + r["name"][1:]
        if not parts:
            return f"{nm} has no sensors this console reads."
        return f"{nm}: " + "; ".join(parts) + "."
    stale = [r for r in rows if r["stale"]]
    n = len(rows)
    if not stale:
        oldest = max((s["age_min"] or 0 for r in rows for s in r["signals"]), default=0)
        return f"{n} assets with sensors, all reporting. The oldest reading is from {_ago(oldest)}."

    def age(r: dict) -> str:
        ages = [s["age_min"] for s in r["signals"] if s["stale"] and s["age_min"] is not None]
        return f"{max(ages):.0f} min" if ages else "since startup"
    listed = "; ".join(f"{r['name']} no reading for {age(r)}" for r in stale[:4])
    more = f" and {len(stale) - 4} more" if len(stale) > 4 else ""
    return f"{n} assets with sensors: {n - len(stale)} reading, {listed}{more}. Treat those as unknown, not safe."


def describe_parts(live: LiveField, asset_id: str) -> dict:
    aid = str(asset_id or "").upper()
    kind = live.fmap.kind_of(aid)
    if not kind:
        return {"error": f"Unknown asset {aid}."}
    entry = live.state()["assets"].get(aid, {})
    shut = live.fmap.tanks[aid]["shutdown_pct"] if kind == "tank" else None
    return parts_catalog.parts_for(kind, aid, entry, shut)


def get_audit(live: LiveField, since: int = 0, limit: int = 20, asset_id: Optional[str] = None) -> dict:
    rows = live.audit.entries(int(since or 0))
    if asset_id:
        rows = [e for e in rows if e.get("asset_id") == str(asset_id).upper()]
    limit = max(1, min(int(limit or 20), 200))
    return {"entries": rows[-limit:], "total": len(rows)}


def audit_text(res: dict, asset_id: Optional[str] = None) -> str:
    rows = [e for e in res["entries"] if e["event"] in ("proposed", "confirmed", "result", "rejected",
                                                        "acked", "shelved", "unshelved", "approved")]
    if not rows:
        return f"Nothing has been done to {name(asset_id)} yet." if asset_id else "Nothing has been done yet this shift."
    out = []
    for e in rows[-3:]:
        who = e.get("by") or e.get("source") or "someone"
        what = e.get("text") or e.get("message") or name(e.get("asset_id") or "")
        verb = {"proposed": "proposed", "confirmed": "confirmed", "rejected": "rejected", "acked": "acknowledged",
                "shelved": "shelved", "unshelved": "unshelved", "approved": "approved",
                "result": "got the result for"}[e["event"]]
        if e["event"] == "proposed":
            who = e.get("source") or who
        tail = f" ({e.get('status')})" if e["event"] == "result" else ""
        out.append(f"{e.get('clock') or ''} {who} {verb}: {what.rstrip('.')}{tail}.".strip())
    return " ".join(out)


async def compare_whatif(jarvis, actions: Any) -> dict:
    live = jarvis.live
    acts, why = _clean_actions(live, actions)
    if why:
        return {"error": why}
    sc = live.live_scenario()
    before = await asyncio.to_thread(jarvis.forecast_fn, sc, [])
    after = await asyncio.to_thread(jarvis.forecast_fn, copy.deepcopy(sc), acts)
    unknown = _unknown_from(sc, after)
    now = live.fmap.clock(live.t)

    def side(fc: dict) -> dict:
        ev = _first_shutdown(fc)
        return {"shutdown": None if not ev else {"asset_id": ev["asset_id"], "t_min": ev["t_min"],
                                                  "clock": clock_at(now, ev["t_min"]),
                                                  "wells_lost": len(ev.get("wells_lost") or [])},
                "shutdowns": [e["asset_id"] for e in fc.get("events", []) if e.get("type") == "SHUTDOWN_LEVEL"],
                "cost_usd": (fc.get("cost") or {}).get("total_usd")}

    b, a = side(before), side(after)
    if b["shutdown"] and a["shutdown"]:
        gained = round(a["shutdown"]["t_min"] - b["shutdown"]["t_min"], 1)
    elif b["shutdown"]:
        gained = round(HORIZON_MIN - b["shutdown"]["t_min"], 1)
    else:
        gained = 0.0
    what = " ".join(describe_action(x) for x in acts)
    what = what[0].lower() + what[1:]
    if b["shutdown"] and not a["shutdown"]:
        effect = (f"{'no tank with a fresh reading' if unknown else 'no tank'} reaches shutdown in the next 6 hours "
                  f"({name(b['shutdown']['asset_id'])} would at {b['shutdown']['clock']}).")
    elif a["shutdown"] and b["shutdown"] and a["shutdown"]["asset_id"] == b["shutdown"]["asset_id"]:
        diff = int(round(gained))
        same = a["shutdown"]["clock"] == b["shutdown"]["clock"]
        effect = (f"{name(a['shutdown']['asset_id'])} still shuts down at {a['shutdown']['clock']}"
                  + ("" if same else f" instead of {b['shutdown']['clock']}")
                  + (f" ({diff} min more)." if diff > 0 else "."))
    elif a["shutdown"]:
        effect = f"{name(a['shutdown']['asset_id'])} shuts down at {a['shutdown']['clock']}."
    else:
        effect = "nothing reaches shutdown either way."
    cost = ""
    if isinstance(a["cost_usd"], (int, float)) and isinstance(b["cost_usd"], (int, float)):
        cost = f" ${a['cost_usd']:,.0f} vs ${b['cost_usd']:,.0f} doing nothing."
    text = blind_text(unknown) + f"If you {what.rstrip('.')}: {effect}{cost}"
    return {"clock": now, "actions": acts, "before": b, "after": a, "gained_min": gained,
            "unknown_tanks": unknown, "warnings": after.get("warnings", []), "text": text.strip()}


async def execute_plan(jarvis, plan_id: str, ctx: dict) -> dict:
    """Propose the plan's next step (longest lead first), one at a time. Never confirms."""
    live = jarvis.live
    res = await asyncio.to_thread(jarvis.plans_fn, live.live_scenario())
    plan = next((p for p in res.get("plans", []) if p["id"] == plan_id), None)
    if plan is None:
        return {"error": "That plan is no longer offered. Ask for the plans again."}
    if not plan.get("actions"):
        return {"error": "That plan has no steps: nothing needs doing."}
    steps = sorted(plan["actions"], key=lambda a: -float(a.get("at_min") or 0))
    mine = [p for p in live.proposals.values() if p.get("plan_id") == plan_id]
    waiting = next((p for p in mine if p["status"] in ("PENDING", "SENDING")), None)
    if waiting:
        return {"status": "waiting", "proposal_id": waiting["id"], "step": waiting.get("plan_step"),
                "steps": len(steps), "text": waiting["text"],
                "note": "That step is still waiting for a person to confirm it."}
    done = {(p["action"]["type"], p["action"]["asset_id"], str(p["action"].get("value")),
             p["action"].get("target_id")) for p in mine if p["status"] in ("CONFIRMED", "UNCERTAIN")}
    for n, step in enumerate(steps, start=1):
        try:
            clean = live.clean_action(step)
        except ActionError as e:
            return {"error": e.detail}
        key = (clean["type"], clean["asset_id"], str(clean.get("value")), clean.get("target_id"))
        if key in done:
            continue
        reason = f"Plan step {n} of {len(steps)}: {plan['summary']}"
        try:
            p = await live.propose(clean, reason=reason, source="jarvis", role=ctx.get("role"))
        except ActionError as e:
            return {"error": e.detail}
        p.update(plan_id=plan_id, plan_step=n, plan_steps=len(steps))
        ctx.setdefault("proposals", []).append(p)
        note_focus(live, clean["asset_id"], ctx)
        return {"status": p["status"], "proposal_id": p["id"], "step": n, "steps": len(steps), "text": p["text"],
                "total_usd": (plan.get("cost") or {}).get("total_usd"),
                "note": "Waiting for a person to confirm it on the console." if p["status"] == "PENDING"
                else p.get("result")}
    return {"status": "done", "steps": len(steps), "note": "Every step of this plan has been sent."}


def _alert(live: LiveField, alert_id: str) -> Optional[dict]:
    return next((a for a in live.alerts() if a["id"] == alert_id), None)


def offer_ack(live: LiveField, alert_id: str, ctx: dict) -> dict:
    a = _alert(live, str(alert_id))
    if a is None:
        return {"error": f"No alert {alert_id}."}
    if a.get("acked"):
        return {"error": f"That alert is already acknowledged ({a['message']})."}
    offer = {"type": "ack", "id": "offer_" + secrets.token_hex(3), "alert_id": a["id"], "asset_id": a["asset_id"],
             "text": f"Acknowledge: {a['message']}", "press": "tap",
             "request": {"method": "POST", "path": f"/api/alerts/{a['id']}/ack", "body": {}}}
    ctx.setdefault("offers", []).append(offer)
    return {"ok": True, "offer_id": offer["id"], "note": "A card is on screen. The operator taps it to send."}


def offer_shelve(live: LiveField, alert_id: str, minutes: Any, reason: Any, ctx: dict) -> dict:
    from .live import SHELVE_MAX, SHELVE_MIN
    a = _alert(live, str(alert_id))
    if a is None:
        return {"error": f"No alert {alert_id}."}
    why = str(reason or "").strip()
    if not why:
        return {"error": "Why? A shelve needs a reason."}
    try:
        mins = float(minutes)
    except (TypeError, ValueError):
        mins = -1.0
    if not SHELVE_MIN <= mins <= SHELVE_MAX:
        return {"error": f"Shelve for {SHELVE_MIN} to {SHELVE_MAX} minutes."}
    if not a["active"]:
        return {"error": "That alert has already cleared, so there is nothing to shelve."}
    mins = int(mins) if mins == int(mins) else mins
    offer = {"type": "shelve", "id": "offer_" + secrets.token_hex(3), "alert_id": a["id"], "asset_id": a["asset_id"],
             "minutes": mins, "reason": why, "press": "hold",
             "text": f"Shelve for {mins} min: {a['message']} Reason: {why}",
             "request": {"method": "POST", "path": f"/api/alerts/{a['id']}/shelve",
                         "body": {"minutes": mins, "reason": why}}}
    ctx.setdefault("offers", []).append(offer)
    return {"ok": True, "offer_id": offer["id"], "note": "A card is on screen. The operator holds it to send."}


# ---------------------------------------------------------------- schemas ----

_NULL_STR = {"type": ["string", "null"]}
_ACTION = {
    "type": "object", "additionalProperties": False, "required": ["type", "asset_id", "value", "target_id", "at_min"],
    "properties": {
        "type": {"type": "string", "enum": ["SET_VALVE", "SET_PUMP", "CHOKE_WELL", "SEND_TRUCK"]},
        "asset_id": {"type": "string"},
        "value": {"type": ["string", "null"], "description": "OPEN/CLOSED, ON/OFF, a fraction like '0.5', or null"},
        "target_id": {"type": ["string", "null"], "description": "Tank ID for SEND_TRUCK"},
        "at_min": {"type": ["number", "null"], "description": "Minutes from now; null uses the action's lead time"}},
}


def _tool(name_: str, description: str, props: Optional[dict] = None) -> dict:
    props = props or {}
    return {"name": name_, "description": description, "strict": True,
            "input_schema": {"type": "object", "additionalProperties": False, "required": list(props),
                             "properties": props}}


_ASSET = {"asset_id": {"type": "string"}}
TOOLS = [
    # list_alerts is jarvis.py's (CF-11, root-cause groups); list_alerts() below is the rules' flat view.
    _tool("get_state", "The live clock, connector status, status counts, unknown tanks, and the open incident."),
    _tool("get_countdown", "The first tank shutdown if nobody acts (asset, clock, low/high, wells lost), the "
          "unknown tanks, and a sentence to say. Unknown tanks come first. tank_id narrows it to one tank.",
          {"tank_id": _NULL_STR}),
    _tool("compare_whatif", "Before and after a list of hypothetical actions: first shutdown, cost, minutes "
          "gained, unknown tanks. Changes nothing.", {"actions": {"type": "array", "items": _ACTION}}),
    _tool("get_last_call", "When the cheapest plan stops working, the point of no return, and until when each "
          "asset can still help. May return an error when Last Call is not installed."),
    _tool("get_audit", "Who proposed, confirmed, rejected, acknowledged, or shelved what, oldest first.",
          {"since": {"type": ["integer", "null"]}, "limit": {"type": ["integer", "null"]}, "asset_id": _NULL_STR}),
    _tool("list_sensors", "Per asset: each signal's value, age in minutes, stale flag, connector, and the model's "
          "sensor nodes. asset_id null lists every asset.", {"asset_id": _NULL_STR}),
    _tool("describe_parts", "The parts of one asset in plain words, with the live value a part shows.", _ASSET),
    _tool("execute_plan", "Start a plan from get_plans: proposes its next step only (PENDING). A person confirms "
          "each step on the console; call again after it is done for the next one.", {"plan_id": {"type": "string"}}),
    _tool("offer_ack", "Put an Acknowledge card on the operator's screen for one alert. The operator taps it.",
          {"alert_id": {"type": "string"}}),
    _tool("offer_shelve", "Put a Shelve card on the operator's screen: 15 to 240 minutes and the operator's own "
          "reason (never invent one). The operator holds it to send.",
          {"alert_id": {"type": "string"}, "minutes": {"type": "number"}, "reason": {"type": "string"}}),
    _tool("ui_focus", "Fly every console's camera to one asset (same as focus_camera).", _ASSET),
    _tool("ui_highlight", "Outline up to 24 assets or SENSOR_/RTU_/PIPE_ nodes for a few seconds.",
          {"ids": {"type": "array", "items": {"type": "string"}}, "seconds": {"type": ["number", "null"]}}),
    _tool("ui_explode", "Take one asset apart on screen to show its parts.", _ASSET),
    _tool("ui_collapse", "Put an exploded asset back together."),
    _tool("ui_isolate", "Hide everything but one asset, its pad, and its pipes.", _ASSET),
    _tool("ui_show_sensors", "Show the sensor nodes of one asset, or every one with asset_id 'ALL'.", _ASSET),
    _tool("ui_trace_pipes", "Light the pipes from or to one asset.",
          {"asset_id": {"type": "string"}, "direction": {"type": ["string", "null"], "enum": [*DIRECTIONS, None]}}),
    _tool("ui_set_time", "Move the time slider: t_min 0 to 360 minutes ahead, or a clock like '3:00 AM'.",
          {"t_min": {"type": ["number", "null"]}, "clock": _NULL_STR}),
    _tool("ui_reset_view", "Back to the overview: collapse, show everything, time to now."),
    _tool("ui_open_panel", "Open a console panel.",
          {"panel": {"type": "string", "enum": list(PANELS)}, "asset_id": _NULL_STR}),
    _tool("ui_try_whatif", "Show a what-if on screen (data only, never sent to equipment).",
          {"actions": {"type": "array", "items": _ACTION}}),
]
_GET_PLANS_PROPS = {"robust_only": {"type": ["boolean", "null"],
                                    "description": "true keeps only plans that hold with inflow 15% high"}}
UI_TOOLS = {"ui_focus": "focus", "ui_highlight": "highlight", "ui_explode": "explode", "ui_collapse": "collapse",
            "ui_isolate": "isolate", "ui_show_sensors": "show_sensors", "ui_trace_pipes": "trace_pipes",
            "ui_set_time": "set_time", "ui_reset_view": "reset_view", "ui_open_panel": "open_panel",
            "ui_try_whatif": "try_whatif"}

PROMPT = (
    "\n\nMore tools:\n"
    "- Data (change nothing): get_state, list_alerts, get_countdown, compare_whatif, get_last_call, get_audit, "
    "list_sensors, describe_parts. Prefer them over guessing; say a tank with no fresh reading is unknown, "
    "never safe, and never say 'all clear' while one is unknown or a feed is lost.\n"
    "- Screen (change only what the operator sees): ui_focus, ui_highlight, ui_explode, ui_collapse, ui_isolate, "
    "ui_show_sensors, ui_trace_pipes, ui_set_time, ui_reset_view, ui_open_panel, ui_try_whatif. Use at most 3 "
    "per answer.\n"
    "- execute_plan proposes a plan's next step only; offer_ack and offer_shelve only put a card on screen. A "
    "person confirms every step and sends every card. A shelve needs 15 to 240 minutes and the operator's reason."
)


def extend_tools(tools: list) -> list:
    """jarvis.py: TOOLS += ... Adds the new tools and the get_plans robust_only argument."""
    for t in tools:
        if t["name"] == "get_plans":
            t["input_schema"] = {"type": "object", "additionalProperties": False, "required": list(_GET_PLANS_PROPS),
                                 "properties": dict(_GET_PLANS_PROPS)}
    have = {t["name"] for t in tools}
    return [t for t in TOOLS if t["name"] not in have]


# ---------------------------------------------------------------- dispatch ----

async def run(jarvis, tool: str, args: dict, ctx: dict) -> Any:
    """jarvis.run_tool asks this first. PASS: jarvis.py's own tool (or unknown)."""
    live = jarvis.live
    args = args or {}
    if tool == "get_plans":
        if not args.get("robust_only"):
            return PASS
        res = await jarvis.run_tool("get_plans", {}, ctx)
        return {**res, "plans": [p for p in res.get("plans", []) if p.get("robust")]}
    if tool in UI_TOOLS:
        d = {**{k: v for k, v in args.items() if v is not None}, "action": UI_TOOLS[tool]}
        return await emit(live, d, ctx, reason=str(args.get("reason") or ""))
    if tool == "get_state":
        return get_state(live)
    if tool == "get_countdown":
        sc, fc = await baseline(jarvis)
        tank = str(args.get("tank_id") or "").upper() or None
        return countdown_of(live, fc, _unknown_from(sc, fc), tank if tank in live.fmap.tanks else None)
    if tool == "compare_whatif":
        return await compare_whatif(jarvis, args.get("actions"))
    if tool == "get_last_call":
        cached = aged_last_call(live)
        return cached if cached is not None else await asyncio.to_thread(last_call_now, live)
    if tool == "get_audit":
        return get_audit(live, args.get("since") or 0, args.get("limit") or 20, args.get("asset_id"))
    if tool == "list_sensors":
        return list_sensors(live, args.get("asset_id"))
    if tool == "describe_parts":
        return describe_parts(live, args.get("asset_id"))
    if tool == "execute_plan":
        return await execute_plan(jarvis, str(args.get("plan_id") or ""), ctx)
    if tool == "offer_ack":
        return offer_ack(live, str(args.get("alert_id") or ""), ctx)
    if tool == "offer_shelve":
        return offer_shelve(live, str(args.get("alert_id") or ""), args.get("minutes"), args.get("reason"), ctx)
    return PASS


# ---------------------------------------------------------------- brief feed ----

class BriefFeed:
    """The ws `brief` feed and GET /api/jarvis/brief:
        {clock, t, countdown, unknown_tanks, plans: {cheapest, robust}, do_nothing_usd, last_call}
    Recomputed after an alert is raised or resolved, after a confirmed proposal, and every
    BRIEF_EVERY_MIN live minutes. Last Call only when an incident opens (a new root ALARM)
    or plan_approved() is called; otherwise its last result is kept. It takes seconds, so
    it runs on its own: the brief goes out at once with `last_call_pending` true, then
    again with `last_call` filled."""

    def __init__(self, live: LiveField, jarvis) -> None:
        self.live, self.jarvis = live, jarvis
        self.latest: Optional[dict] = None
        self._t: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._again = False
        self._last_call: Optional[dict] = None
        self._lc_due = False
        self._lc_task: Optional[asyncio.Task] = None
        self._roots: set = set()

    @classmethod
    def attach(cls, live: LiveField, jarvis) -> "BriefFeed":
        feed = getattr(live, "brief_feed", None)
        if feed is None:
            feed = cls(live, jarvis)
            live.brief_feed = feed
            live.brief_hook = feed.poke
        return feed

    def _note_incidents(self, changed) -> None:
        for a in changed or []:
            if a.get("active") and a.get("severity") == "ALARM" and not a.get("root_id") and a["id"] not in self._roots:
                self._roots.add(a["id"])
                self._lc_due = True

    def plan_approved(self) -> None:
        """Another lane approved a plan: recompute Last Call with the next brief."""
        self._lc_due = True
        self.poke(force=True)

    def poke(self, changed=(), force: bool = False) -> None:
        """live.py calls this after each publish and confirm. Schedules a recompute when due."""
        self._note_incidents(changed)
        due = force or bool(changed) or self._t is None or self.live.t - self._t >= BRIEF_EVERY_MIN - 1e-9
        if not due or not self.live.hub.clients:
            return   # nobody listening: GET /api/jarvis/brief computes fresh anyway
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = loop.create_task(self._run())

    async def _run(self) -> None:
        while True:
            self._again = False
            try:
                b = await self.compute()
                await self.live.hub.broadcast("brief", {"brief": b})
            except Exception:  # noqa: BLE001 - the feed must never stop the live loop
                log.exception("brief failed")
                return
            if not self._again:
                return

    def _lc_running(self) -> bool:
        return self._lc_task is not None and not self._lc_task.done()

    def _start_last_call(self) -> None:
        """Last Call replays the forecast many times (seconds), so it never holds up a brief:
        it runs on its own and sends the brief again, with last_call filled, when it is done."""
        if self._lc_running():
            self._lc_due = True   # run again after this one
            return
        self._lc_due = False

        async def go() -> None:
            while True:
                try:
                    res = await asyncio.to_thread(last_call_now, self.live)
                except Exception as e:  # noqa: BLE001
                    log.exception("last call failed")
                    res = {"error": f"Last Call failed: {e}"}
                self._last_call = res
                if self.latest is not None:
                    self.latest = {**self.latest, "last_call": res, "last_call_pending": False}
                    await self.live.hub.broadcast("brief", {"brief": self.latest})
                if not self._lc_due:
                    return
                self._lc_due = False

        self._lc_task = asyncio.get_running_loop().create_task(go())

    async def compute(self, start_last_call: bool = True) -> dict:
        live, jarvis = self.live, self.jarvis
        t = live.t
        sc = live.live_scenario()
        fc = await asyncio.to_thread(jarvis.forecast_fn, sc, [])
        plans = await asyncio.to_thread(jarvis.plans_fn, copy.deepcopy(sc))
        unknown = _unknown_from(sc, fc)
        cd = countdown_of(live, fc, unknown)
        if not live.detector.active:
            self._last_call, self._lc_due = None, False
        elif start_last_call and (self._lc_due or (self._last_call is None and not self._lc_running() and any(
                a["severity"] == "ALARM" and not a.get("root_id") for a in live.detector.active.values()))):
            self._start_last_call()
        self._t = t
        self.latest = {"clock": live.fmap.clock(t), "t": round(t, 2), "countdown": cd["countdown"],
                       "countdown_text": cd["text"], "unknown_tanks": unknown,
                       "plans": plan_pair(plans.get("plans", [])),
                       "do_nothing_usd": (fc.get("cost") or {}).get("total_usd"),
                       "last_call": self._last_call, "last_call_pending": self._lc_running(),
                       "feed_ok": not lost_feeds(live)}
        return self.latest

    async def get(self) -> dict:
        """GET /api/jarvis/brief: computed fresh. Last Call is started only on the live loop
        (a request-scoped loop would drop it); until it is done, last_call_pending is true."""
        here = asyncio.get_running_loop()
        return await self.compute(start_last_call=self.live.loop is here or bool(self.live.hub.clients))
