"""Deterministic Jarvis intents (docs/JARVIS_CONSOLE.md section 5, routes L and R).

jarvis.handle() calls match() first: before jarvis.py's _rules and before the COMPLEX
check that sends open-ended questions to Claude. So "why is tank 3 rising" is answered
from the tools even when an API key is set, "status of PUMP_3" (a pinch) answers about
Pump 3, and "open pump 3" takes it apart on screen instead of asking Claude.

match() returns the reply text, or None to fall through to _rules (the fixes it already
proposes: "open the crossover", "send truck 1 to tank 3", ...) and then Claude.

The phrases, with the result each must give, are in scenario/jarvis_phrases.json; pytest
(backend/tests/test_jarvis_phrases.py) and the web's Playwright suite both run it.
"it", "this" and "that" mean the selected asset. Number words count as digits.
"""
from __future__ import annotations

import re
from typing import Optional

from . import jarvis_tools as T
from .fieldmap import name

NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
       "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
       "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90}
OPEN_ENDED = re.compile(r"\b(instead|rather|compare|versus|vs\.?|better|cheaper than|unless|and then|explain)\b")
QUESTION = re.compile(r"^(should|can|could|would|will|do|does|did|is|are|what|why|how|when|which)\b")
NEGATION = re.compile(r"\b(don'?t|do not|never|not|no|cancel|stop|hold off|wait)\b")
IT = re.compile(r"\b(it|this|that|this one|that one|selected|selection)\b")


def normalize(text: str) -> str:
    t = " " + (text or "").lower().replace("-", " ").replace("?", " ").replace("!", " ").replace(",", " ") + " "
    t = t.replace("’", "'")
    t = re.sub(r"(\d)'s\b", r"\1", t)   # "tank 3's sensors"
    for w, n in sorted(NUM.items(), key=lambda kv: -len(kv[0])):
        t = re.sub(rf"\b{w}\b", str(n), t)
    t = re.sub(r"\s+", " ", t)
    return t


def _say(s: str) -> str:
    return s[0].upper() + s[1:] if s else s


# ---------------------------------------------------------------- assets ----

def asset_in(jarvis, t: str, selected: list, allow_it: bool = True) -> Optional[str]:
    """The asset a sentence names: an ID (PUMP_3), words (pump 3, valve d 3, valve xo 34,
    the pump 3 discharge valve, disposal well 1), or "it" for the selection. Crossovers
    by tank ("the crossover", "tank 3 to 4 crossover") are left to jarvis._crossover_in."""
    f = jarvis.live.fmap
    m = re.search(r"\b((?:tank|pump|well|truck|swd|valve_d|valve_xo|valve)_[a-z0-9_]+)\b", t)
    if m and f.kind_of(m.group(1).upper()):
        return m.group(1).upper()
    m = re.search(r"\b(?:valve )?xo(?: valve)? ?(\d{2})\b", t)
    if m and f.kind_of(f"VALVE_XO_{m.group(1)}"):
        return f"VALVE_XO_{m.group(1)}"
    m = re.search(r"\bpump (\d+) (?:discharge )?valve\b", t)
    if m and f.pump_valve.get(f"PUMP_{m.group(1)}"):
        return f.pump_valve[f"PUMP_{m.group(1)}"]
    m = re.search(r"\b(?:discharge valve|valve d|valve) ?(\d+)\b", t)
    if m and f.kind_of(f"VALVE_D_{m.group(1)}"):
        return f"VALVE_D_{m.group(1)}"
    m = re.search(r"\b(?:disposal well|swd) ?(\d+)\b", t)
    if m and f.kind_of(f"SWD_{m.group(1)}"):
        return f"SWD_{m.group(1)}"
    m = re.search(r"\b(tank|pump|well|truck) ?(\d+)\b", t)
    if m and f.kind_of(f"{m.group(1).upper()}_{m.group(2)}"):
        return f"{m.group(1).upper()}_{m.group(2)}"
    if allow_it and selected and IT.search(t):
        return next((s for s in selected if f.kind_of(s)), None)
    return None


async def asset_or_crossover(jarvis, t: str, selected: list) -> Optional[str]:
    if "crossover" in t:   # "the Tank 3 to 4 crossover" names tanks, but means the valve
        return (await jarvis._crossover_in(t))[0]
    return asset_in(jarvis, t, selected)


# ---------------------------------------------------------------- replies ----

async def _directive(jarvis, ctx: dict, action: str, reason: str, aid: Optional[str] = None, **args) -> dict:
    d = {"action": action, "args": args}
    if aid:
        d["asset_id"] = aid
    return await T.emit(jarvis.live, d, ctx, reason=reason)


async def _explain(jarvis, aid: str, t: str, ctx: dict, text: str) -> str:
    from .jarvis import ASKS_FIX
    await jarvis._focus(aid, ctx)
    line = await jarvis._describe(aid)
    if ASKS_FIX.search(t):
        line += " " + await jarvis._plans_line(ctx)
    return line


async def _status(jarvis, ctx: dict) -> str:
    """jarvis.py's "What's failing?" (CF-11: P1 roots, related counts, blind tanks), plus the
    lost feeds it doesn't name yet. Never "all quiet" while a feed is lost."""
    live = jarvis.live
    reply = await jarvis._status(ctx)
    lost = [c for c in T.lost_feeds(live) if c["id"] not in reply]
    if lost:
        clock = live.fmap.clock_text(live.t)
        reply = reply.replace(f"All quiet at {clock}. No active alerts.", f"No active alarms at {clock}.")
        reply += (f" Lost the link to {', '.join(c['id'] for c in lost)}, so I can't vouch for "
                  f"what it reads. Treat those assets as unknown, not safe.")
    return reply.strip()


def _minutes(t: str) -> Optional[float]:
    if re.search(r"\bhalf an? hour\b", t):
        return 30.0
    if re.search(r"\ban? hour\b", t):
        return 60.0
    m = re.search(r"(\d+(?:\.\d+)?) ?(hours?|hrs?|h|minutes?|mins?|m)\b", t)
    if not m:
        return None
    n = float(m.group(1))
    return n * 60 if m.group(2).startswith("h") else n


def _set_time_target(t: str) -> Optional[dict]:
    s = t.strip()
    if re.fullmatch(r"(go )?(back to )?(now|live|the present|current time)( please)?", s):
        return {"t_min": 0}
    m = re.fullmatch(r"(?:show (?:me )?|go to |jump to |skip to )?(?:in|\+|plus|forward|ahead) ?(an?|half an|\d+(?:\.\d+)?) ?"
                     r"(hours?|hrs?|h|minutes?|mins?|m)(?: from now| ahead)?", s)
    if m:
        n = {"a": 1.0, "an": 1.0, "half an": 0.5}.get(m.group(1))
        n = float(m.group(1)) if n is None else n
        return {"t_min": n * 60 if m.group(2).startswith("h") else n}
    m = re.fullmatch(r"(?:show (?:me )?|go to |jump to )?at (\d{1,2}(?::\d{2})? ?(?:am|pm)?)", s)
    if m:
        return {"clock": m.group(1)}
    return None


def _parse_action(jarvis, t: str) -> Optional[dict]:
    """A hypothetical action ("open the crossover", "send truck 1", "shut in well 9") with
    no proposal. Crossover choice is left to the caller (needs an await)."""
    f = jarvis.live.fmap
    m = re.search(r"send truck (\d+)(?: to tank (\d+))?", t)
    if m:
        target = f"TANK_{m.group(2)}" if m.group(2) else None
        if not target:
            trip = next((a["asset_id"] for a in jarvis.live.detector.active.values() if a["kind"] == "PUMP_TRIP"), None)
            target = f.pumps[trip]["tank_id"] if trip else None
        return {"type": "SEND_TRUCK", "asset_id": f"TRUCK_{m.group(1)}", "target_id": target}
    m = re.search(r"\b(choke|shut in|shut) well (\d+)(?:\D+(\d+) ?(?:%|percent))?", t)
    if m:
        frac = 0.0 if m.group(1).startswith("shut") else (int(m.group(3)) / 100 if m.group(3) else 0.5)
        return {"type": "CHOKE_WELL", "asset_id": f"WELL_{m.group(2)}", "value": frac}
    m = re.search(r"turn (on|off) pump (\d+)|pump (\d+) (on|off)\b|(start|stop) pump (\d+)", t)
    if m:
        on, n = ((m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3)) if m.group(3)
                 else ("on" if m.group(5) == "start" else "off", m.group(6)))
        return {"type": "SET_PUMP", "asset_id": f"PUMP_{n}", "value": on.upper()}
    m = re.search(r"\b(open|close)\b", t)
    if m and re.search(r"crossover|valve|\bxo\b", t):
        return {"type": "SET_VALVE", "asset_id": None, "value": "OPEN" if m.group(1) == "open" else "CLOSED",
                "_crossover": "crossover" in t}
    return None


async def _whatif(jarvis, t: str, selected: list, ctx: dict) -> Optional[str]:
    a = _parse_action(jarvis, t)
    if a is None:
        return None
    if a["type"] == "SET_VALVE":
        if a.pop("_crossover"):
            aid, why = await jarvis._crossover_in(t, closing=a["value"] == "CLOSED")
            if why:
                return why
        else:
            aid = asset_in(jarvis, t, selected)
        a["asset_id"] = aid
    if a["type"] == "SEND_TRUCK" and not a.get("target_id"):
        return "Which tank should the truck go to?"
    res = await T.compare_whatif(jarvis, [{**a, "at_min": None}])
    if "error" in res:
        return f"I can't run that what-if: {res['error']}"
    await _directive(jarvis, ctx, "try_whatif", "what if", actions=[{**a, "at_min": None}])
    return res["text"] + " Shown on screen as a what-if. Nothing was sent."


async def _execute(jarvis, t: str, raw: str, ctx: dict) -> str:
    if NEGATION.search(t):
        return "Okay. Nothing started."
    res = await jarvis.run_tool("get_plans", {}, ctx)
    pair = T.plan_pair(res.get("plans", []))
    wants_cheapest = "cheapest" in t
    plan = pair["cheapest"] if wants_cheapest else (pair["robust"] or pair["cheapest"])
    unknown = T.unknown_tanks(jarvis.live)
    if plan is None:
        return (T.blind_text(unknown) + "Nothing needs fixing: no tank "
                + ("with a fresh reading " if unknown else "") + "reaches shutdown in the next 6 hours.").strip()
    if raw.strip().endswith("?") or QUESTION.search(t.strip()):
        return (await jarvis._plans_line(ctx)) + " Say 'run the plan' to start it."
    out = await jarvis.run_tool("execute_plan", {"plan_id": plan["id"]}, ctx)
    if "error" in out:
        return f"I can't start that plan: {out['error']}"
    usd = f" (${plan['total_usd']:,.0f})" if isinstance(plan.get("total_usd"), (int, float)) else ""
    which = "cheapest" if plan is pair["cheapest"] else "cheapest robust"
    if out["status"] == "waiting":
        return f"Step {out['step'] or ''} is still waiting for your confirmation: {out['text']}".replace("Step  ", "Step ")
    if out["status"] == "done":
        return "Every step of the plan has been sent. Watch the tank levels."
    return (f"Step {out['step']} of {out['steps']} of the {which} plan{usd}: {out['text']} "
            f"It's waiting for your confirmation on the console.")


def _pick_alert(jarvis, t: str, selected: list, unacked: bool = False) -> Optional[dict]:
    live = jarvis.live
    rows = T.list_alerts(live, group=False)["alerts"]
    if unacked:
        rows = [r for r in rows if not r["acked"]] or rows
    aid = asset_in(jarvis, t, selected)
    if aid:
        mine = [r for r in rows if r["asset_id"] == aid]
        if mine:
            return mine[0]
    for word, test in (("pump", lambda r: r["asset_id"].startswith("PUMP_")),
                       ("tank", lambda r: r["asset_id"].startswith("TANK_")),
                       ("valve", lambda r: r["asset_id"].startswith("VALVE_")),
                       ("well", lambda r: r["asset_id"].startswith("WELL_")),
                       ("sensor|stale", lambda r: r["kind"] == "SENSOR_STALE")):
        if re.search(rf"\b({word})\b", t):
            hit = [r for r in rows if test(r)]
            if hit:
                return hit[0]
    if aid:
        return None
    roots = [r for r in rows if not r["root_id"]]
    return (roots or rows or [None])[0]


def _reason(raw: str) -> str:
    m = re.search(r"\b(?:because|since|reason:?|cause|as)\b\s*(.+)$", raw, re.I)
    return m.group(1).strip().rstrip(".") if m else ""


# ---------------------------------------------------------------- match ----

async def match(jarvis, text: str, selected: Optional[list], ctx: dict) -> Optional[str]:
    raw = (text or "").strip()
    t = normalize(raw)
    s = t.strip()
    selected = [x for x in (selected or []) if isinstance(x, str)]
    live = jarvis.live
    if not s:
        return None

    # Hands are a switch on this screen; the server can't turn a camera on.
    if re.fullmatch(r"(turn |switch )?(hands?|hand control|gestures?) (on|off)|(turn|switch) (on|off) "
                    r"(the )?(hands?|hand control|gestures?)|(enable|disable|start|stop) (hands?|hand control|gestures?)", s):
        return "Hands are the switch at the top of Jarvis on the console. The mouse always works too."

    if re.fullmatch(r"(help|what can you do|commands|what can i say)", s):
        from .jarvis import HELP
        return HELP + " On screen: 'open pump 3', 'show tank 3's sensors', 'trace tank 3's pipes', 'reset'."

    # Reset (L).
    if re.fullmatch(r"(please )?(reset( the)?( view| camera| screen)?|go home|home|overview|start over|"
                    r"show (me )?everything)( please)?", s):
        await _directive(jarvis, ctx, "reset_view", raw)
        return "Back to the overview. What-ifs and an approved plan walk stay on."

    # Time slider (L).
    tt = _set_time_target(s)
    if tt is not None:
        out = await _directive(jarvis, ctx, "set_time", raw, **tt)
        if "error" in out:
            return out["error"]
        d = ctx["ui"][-1]["args"]
        return "Back to now." if d["t_min"] == 0 else f"Showing {d['clock']}, {d['t_min']} min ahead."

    # Collapse (L).
    if re.fullmatch(r"(please )?(close (it|this|that)|put (it|this|that) back( together)?|collapse( it)?|"
                    r"close the (breakdown|parts)|reassemble( it)?)( please)?", s):
        await _directive(jarvis, ctx, "collapse", raw)
        return "Put back together."

    # Stale sensors (R) before "sensors on X".
    if re.search(r"which sensors (are|went) (stale|quiet|dead)|stale sensors|sensor ages|sensors? health|"
                 r"how old are the readings|any stale", s):
        res = T.list_sensors(live)
        await _directive(jarvis, ctx, "show_sensors", raw, aid="ALL")
        return T.sensors_text(res, one=False)

    # Show sensors (L).
    if re.search(r"\bsensors?\b", s) and not re.search(r"\b(why|instead)\b", s):
        if re.search(r"\b(all|every|each)\b", s) or re.fullmatch(r"(show )?(the )?sensors", s):
            res = T.list_sensors(live)
            await _directive(jarvis, ctx, "show_sensors", raw, aid="ALL")
            return T.sensors_text(res, one=False)
        aid = await asset_or_crossover(jarvis, t, selected)
        if aid:
            res = T.list_sensors(live, aid)
            await _directive(jarvis, ctx, "show_sensors", raw, aid=aid)
            return T.sensors_text(res, one=True)

    # Explode (L). open/close + valve or crossover is equipment: leave it to _rules (a proposal).
    m = re.match(r"^(?:please )?(open( up)?|break down|take apart|take (?:it|this|that) apart|disassemble|explode|"
                 r"show (?:me )?(?:the )?parts(?: of)?|what'?s inside)\b", s)
    if m:
        valve_words = re.search(r"\b(valve|crossover|xo)\b", s) or re.search(r"\bvalve_", s)
        if m.group(1).startswith("open") and valve_words:
            aid = asset_in(jarvis, t, selected, allow_it=False) if "crossover" not in s else None
            if aid and live.fmap.kind_of(aid) == "valve":   # "open the pump 3 discharge valve"
                return await jarvis._propose({"type": "SET_VALVE", "asset_id": aid, "value": "OPEN"}, raw, ctx)
            return None   # the crossover: _rules picks which one and proposes it
        aid = asset_in(jarvis, t, selected) if "crossover" not in s else None
        if aid:
            if m.group(1).startswith("open") and live.fmap.kind_of(aid) == "valve":
                # "open it" with a valve selected is the valve itself: a proposal, held to confirm.
                return await jarvis._propose({"type": "SET_VALVE", "asset_id": aid, "value": "OPEN"}, raw, ctx)
            await _directive(jarvis, ctx, "explode", raw, aid=aid)
            parts = T.describe_parts(live, aid)
            n = len([p for p in parts.get("parts", []) if not p.get("planned")])
            return f"{_say(name(aid))}, taken apart: {n} parts. Point at one to see what it does. Say 'close it' to put it back."

    # Isolate (L).
    m = re.match(r"^(?:please )?(isolate|show only|show just|only|just)\b", s)
    if m:
        aid = await asset_or_crossover(jarvis, t, selected)
        if aid:
            await _directive(jarvis, ctx, "isolate", raw, aid=aid)
            return f"Only {name(aid)}, its pad, and its pipes are showing. Say 'reset' to see everything."

    # Trace pipes (L).
    if re.search(r"\btrace\b|\bpipes?\b|\bfollow\b|where does .* (drain|go|flow|come from)|what feeds|\bdrains?\b|"
                 r"downstream|upstream", s):
        aid = await asset_or_crossover(jarvis, t, selected)
        if aid:
            direction = ("downstream" if re.search(r"drain|downstream|where does .* (go|flow)\b", s) else
                         "upstream" if re.search(r"upstream|come from|feeds", s) else "both")
            await _directive(jarvis, ctx, "trace_pipes", raw, aid=aid, direction=direction)
            way = {"downstream": "where its water goes", "upstream": "what feeds it", "both": "its pipes"}[direction]
            return f"Tracing {way} from {name(aid)}."

    # What if (R): compare and show, never send.
    if re.match(r"^(what if|what happens if|suppose|if (i|we))\b", s):
        out = await _whatif(jarvis, t, selected, ctx)
        if out is not None:
            return out

    # Run a plan (R): one proposal for its next step.
    if re.search(r"^(please )?(fix it|fix this|fix the problem|run (the |a )?plan|run it|execute (the )?plan|"
                 r"approve (the )?(cheapest |robust |safest |best )?plan|start the plan|go ahead with the plan)\b", s) \
            or re.search(r"\b(don'?t|do not|never) (fix it|run (the )?plan|start the plan|approve)\b", s):
        return await _execute(jarvis, t, raw, ctx)

    # Acknowledge (R): a card the operator taps.
    if re.match(r"^(please )?(ack|acknowledge)\b", s):
        a = _pick_alert(jarvis, t, selected, unacked=True)
        if not a:
            return "There's no active alert to acknowledge."
        out = await jarvis.run_tool("offer_ack", {"alert_id": a["id"]}, ctx)
        if "error" in out:
            return out["error"]
        return f"Tap the card to acknowledge: {a['message']}"

    # Shelve (R): a card the operator holds. Needs a reason.
    if re.match(r"^(please )?(shelve|snooze)\b", s):
        a = _pick_alert(jarvis, t, selected)
        if not a:
            return "There's no active alert to shelve."
        why = _reason(raw)
        if not why:
            return "Why? A shelve needs a reason."
        mins = _minutes(re.split(r"\b(?:because|since|reason|cause|as)\b", t)[0])
        out = await jarvis.run_tool("offer_shelve", {"alert_id": a["id"], "minutes": 30 if mins is None else mins,
                                                     "reason": why}, ctx)
        if "error" in out:
            return out["error"]
        offer = ctx["offers"][-1]
        return f"Hold the card to shelve it for {offer['minutes']} min: {a['message']}"

    # Audit (R).
    if re.search(r"^who (opened|closed|confirmed|sent|turned|shut|approved|rejected|acked|acknowledged|shelved|did)\b|"
                 r"what did (we|i|you) do|\baudit\b|what (was|has been) done|\bshift log\b|history", s):
        aid = await asset_or_crossover(jarvis, t, selected) if re.search(r"\bwho\b", s) else None
        res = T.get_audit(live, asset_id=aid, limit=50)
        return T.audit_text(res, aid)

    # Last Call (R).
    if re.search(r"last call|deadline|(plan|option)s? (expire|run out)|when does the plan (expire|stop working)|"
                 r"how long do (i|we) have to (decide|act)", s):
        lc = await jarvis.run_tool("get_last_call", {}, ctx)
        return T.last_call_text(lc)

    # Countdown (R). Unknown tanks first.
    if re.search(r"time left|how long|countdown|how much time|when (does|will) .*(shut|fill|overflow)|"
                 r"until shutdown|time to shutdown", s):
        aid = asset_in(jarvis, t, selected, allow_it=True)
        tank = aid if aid in live.fmap.tanks else None
        if tank is None:
            # CF-11's countdown, worded with its sources. A fixed question, so nothing else in the
            # sentence ("... if I send truck 1") can reach _rules' proposals.
            out = await jarvis._rules("time left", selected, ctx)
            if out is not None:
                return out
        res = await jarvis.run_tool("get_countdown", {"tank_id": tank}, ctx)
        target = (res["countdown"] or {}).get("asset_id") or (res["unknown_tanks"][0] if res["unknown_tanks"] else None)
        if target:
            await jarvis._focus(target, ctx)
        return res["text"]

    # Why is X rising / red / down (R): explain, cause, shutdown. Exempt from COMPLEX.
    if re.match(r"^why (is|are|does|did|has|'?s)\b", s) and not OPEN_ENDED.search(s):
        aid = await asset_or_crossover(jarvis, t, selected)
        if aid:
            return await _explain(jarvis, aid, t, ctx, raw)

    # Status of one asset (R), e.g. the pinch's "status of PUMP_3".
    if re.search(r"\bstatus of\b|\bhow'?s\b|\bhow is\b|tell me about|what'?s (going on|up|happening|wrong) with|"
                 r"what about|\bcheck( on)?\b|info on|details (on|for)|what'?s the (status|level) of|"
                 r"^(show( me)?|go to|fly to|focus( on)?|zoom( in)?( to| on)?|where is|where'?s|find|take me to|"
                 r"look at)\b", s) and not OPEN_ENDED.search(s):
        aid = await asset_or_crossover(jarvis, t, selected)
        if aid:
            return await _explain(jarvis, aid, t, ctx, raw)
    if re.fullmatch(r"(the )?((tank|pump|well|truck|swd|disposal well|valve( d| xo)?|xo) ?\d+|[a-z]+(_[a-z0-9]+)+)",
                    s):   # just a name: "pump 3", "PUMP_3"
        aid = asset_in(jarvis, t, selected, allow_it=False)
        if aid:
            return await _explain(jarvis, aid, t, ctx, raw)

    # Cheapest fix (R).
    if re.search(r"cheapest fix|what should (i|we) do|what do (i|we) do|how do (i|we) fix|\bplans?\b|\boptions\b|"
                 r"recommend|best fix", s) and not OPEN_ENDED.search(s):
        return await jarvis._plans_line(ctx)

    # Field status (R). Never "all quiet" while a tank is unknown or a feed is lost.
    if re.search(r"^(status|sitrep|report|alarms?|alerts?|update)$|what'?s (failing|wrong|happening|going on|broken)|"
                 r"any (alarms|alerts|problems|issues)|what needs (attention|me)|field status|status report", s):
        return await _status(jarvis, ctx)
    return None
