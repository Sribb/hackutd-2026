"""Jarvis: the command interface. Text (or speech-to-text from the browser) in,
an answer plus UI directives out, broadcast to every screen.

Two engines with the same tools:
  - "claude": Claude (claude-opus-5-5) picks tools via tool use. Needs ANTHROPIC_API_KEY.
  - "rules":  a regex parser for the demo-critical phrases. Instant, works offline.
JARVIS_MODE=auto (default) uses Claude when a key is present and falls back to rules
on any API error, so the demo never dies on bad Wi-Fi.

The README rule holds: the solver produces every number. Claude only chooses tools
and phrases their results. Equipment changes and plan approvals become pending
commands that a human confirms on the console or a phone (JARVIS_CONFIRM=1, default).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .field import nice_name
from .live import LiveField

log = logging.getLogger("freeboard.jarvis")

MODEL = os.environ.get("JARVIS_MODEL", "claude-opus-5-5")
MAX_TOOL_ROUNDS = 6
# Questions the regex engine shouldn't answer even if a keyword matches.
COMPLEX = re.compile(r"\b(what if|why|instead|compare|versus|vs\.?|explain|should we|better|cheaper than|unless|and then|but)\b")
MAX_HISTORY_MESSAGES = 40

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "first": 1, "second": 2, "third": 3}

ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "asset_id", "value", "target_id", "at_min"],
    "properties": {
        "type": {"type": "string", "enum": ["SET_VALVE", "SET_PUMP", "CHOKE_WELL", "SEND_TRUCK"]},
        "asset_id": {"type": "string", "description": "Asset ID, e.g. VALVE_XO_34, PUMP_2, WELL_11, TRUCK_1"},
        "value": {"type": ["string", "null"], "description": "OPEN/CLOSED for valves, ON/OFF for pumps, a fraction like '0.5' for CHOKE_WELL (0 = shut in), null for trucks"},
        "target_id": {"type": ["string", "null"], "description": "Tank ID for SEND_TRUCK, otherwise null"},
        "at_min": {"type": ["number", "null"], "description": "Minutes from now; null means now"},
    },
}

TOOLS = [
    {
        "name": "get_field_status",
        "description": "Current clock, active incident, countdown to the next shutdown, every asset not in OK status, open alerts, pending confirmations, and plan progress. Call this before answering any question about the field.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
    },
    {
        "name": "explain_asset",
        "description": "Live readings, status, and reason for one asset (tank level and fill rate vs expected, pump flow, valve position, well rate, truck location).",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["asset_id"],
                         "properties": {"asset_id": {"type": "string"}}},
    },
    {
        "name": "forecast_what_if",
        "description": "Replay the next 6 hours with a list of hypothetical actions (empty list = do nothing). Returns shutdown times, wells lost, and dollar cost. Also previews the result on the 3D time slider. Does not change the field.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["actions"],
                         "properties": {"actions": {"type": "array", "items": ACTION_SCHEMA}}},
    },
    {
        "name": "find_plans",
        "description": "Search valve, truck, and choke combinations and return the cheapest plans that keep tanks running, ranked by dollars. Shows them on every screen.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
    },
    {
        "name": "approve_plan",
        "description": "Ask the operator to approve a plan from find_plans. Creates a confirmation on every screen; the plan runs only after a human confirms.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["plan_id"],
                         "properties": {"plan_id": {"type": "string", "description": "e.g. plan_1"}}},
    },
    {
        "name": "control_equipment",
        "description": "Operate one piece of equipment now: open/close a valve, turn a pump on/off, choke or shut in a well, or send a truck to a tank. Creates a confirmation on every screen; it runs only after a human confirms.",
        "strict": True,
        "input_schema": ACTION_SCHEMA,
    },
    {
        "name": "show_on_screen",
        "description": "Drive the 3D console on every screen: focus the camera on an asset, highlight assets, scrub the time slider to minutes from now, open a panel, or clear.",
        "strict": True,
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "required": ["action", "asset_ids", "t_min", "panel"],
            "properties": {
                "action": {"type": "string", "enum": ["focus", "highlight", "set_time", "show_panel", "clear"]},
                "asset_ids": {"type": "array", "items": {"type": "string"}},
                "t_min": {"type": ["number", "null"]},
                "panel": {"anyOf": [{"type": "string", "enum": ["plans", "alerts", "forecast", "asset", "radio"]},
                                    {"type": "null"}]},
            },
        },
    },
]


def _system_prompt(live: LiveField) -> str:
    f = live.f
    ids = {
        "tanks": sorted(f.tanks), "pumps (one per tank)": sorted(f.pumps),
        "valves": sorted(f.valves), "wells": f"WELL_1..WELL_{len(f.wells)} (4 per tank)",
        "disposal wells": sorted(f.swds), "trucks": sorted(f.trucks),
    }
    return (
        "You are Jarvis, the voice of a 3D night-shift console for a salt water system in an oil field. "
        "Operators talk to you from the control room screen or their phones, often at 2 AM under pressure.\n\n"
        "How to answer:\n"
        "- Replies are read aloud. Use one to three short plain sentences. No markdown, no lists.\n"
        "- Every number you say (times, levels, dollars, barrels) must come from a tool result in this turn. "
        "Never estimate. If a tool returns warnings about missing or stale data, say so first.\n"
        "- When the operator asks about the field, call get_field_status or explain_asset first.\n"
        "- When the operator says 'show', 'zoom', 'look at', 'go to', or 'fast forward', call show_on_screen. "
        "Also focus the asset you're talking about so the screen follows the conversation.\n"
        "- For 'what if' questions use forecast_what_if. For 'what's the cheapest fix' use find_plans.\n"
        "- To change equipment use control_equipment; to approve a plan use approve_plan. Both wait for a human "
        "to tap Confirm, so tell the operator it's waiting for their confirmation.\n"
        "- Say asset names like a person ('Tank 3', 'the Tank 3 to 4 crossover'), not IDs.\n\n"
        f"Asset IDs: {json.dumps(ids)}. Crossover VALVE_XO_34 connects TANK_3 and TANK_4."
    )


class Jarvis:
    def __init__(self, live: LiveField) -> None:
        self.live = live
        self.mode = os.environ.get("JARVIS_MODE", "auto").lower()
        self.require_confirm = os.environ.get("JARVIS_CONFIRM", "1") != "0"
        self.fast_path = os.environ.get("JARVIS_FAST_PATH", "1") != "0"
        self.sessions: dict[str, list] = {}
        self._client = None
        self._client_error: str | None = None
        if self.mode in ("auto", "llm") and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            try:
                from anthropic import AsyncAnthropic
                self._client = AsyncAnthropic()
            except Exception as exc:  # missing package or bad config: fall back to rules
                self._client_error = str(exc)

    @property
    def engine(self) -> str:
        return "claude" if self._client and self.mode != "rules" else "rules"

    def status(self) -> dict:
        return {"engine": self.engine, "model": MODEL if self.engine == "claude" else None,
                "require_confirm": self.require_confirm, "error": self._client_error}

    # ------------------------------------------------------------------ entry point
    async def handle(self, text: str, session: str = "default", source: str = "web", by: str = "") -> dict:
        text = (text or "").strip()
        ctx = {"ui": [], "pending": [], "data": {}, "source": source, "by": by}
        await self.live.broadcast("jarvis_heard", {"text": text, "source": source, "by": by, "session": session})
        engine = self.engine
        reply = None
        # Fast path: demo phrases ("what's failing", "send truck 1 to tank 3") answer instantly.
        # Anything open-ended, or not matched, goes to Claude (3-10 s).
        if engine == "claude" and self.fast_path and not COMPLEX.search(text.lower()):
            reply = await self._rules(text, ctx, help_fallback=False)
            if reply is not None:
                engine = "rules"
        if engine == "claude":
            try:
                reply = await self._claude(text, session, ctx)
            except Exception as exc:
                log.warning("Claude failed, using rules: %s", exc)
                engine = "rules"
                ctx["data"]["fallback_reason"] = type(exc).__name__
        if reply is None:
            engine = "rules"
            reply = await self._rules(text, ctx)
        out = {"text": text, "reply": reply, "engine": engine, "source": source, "by": by,
               "session": session, "ui": ctx["ui"], "pending": ctx["pending"], "data": ctx["data"]}
        await self.live.broadcast("jarvis", out)
        return out

    # ------------------------------------------------------------------ tools (shared by both engines)
    async def run_tool(self, name: str, args: dict, ctx: dict) -> Any:
        live = self.live
        if name == "get_field_status":
            st = live.state()
            not_ok = {k: v for k, v in st["assets"].items() if v.get("status") != "OK"}
            return {"clock": st["clock"], "incident": st["incident"], "countdown": st["countdown"],
                    "assets_not_ok": not_ok, "warnings": st["warnings"],
                    "open_alerts": [{k: a[k] for k in ("severity", "title", "body")} for a in live.alerts.open_items()][-5:],
                    "pending_confirmations": [p["text"] for p in live.pending.values()],
                    "plan_in_progress": live.execution and {
                        "plan_id": live.execution["plan_id"],
                        "steps": [{"n": s["n"], "text": s["text"], "status": s["status"]} for s in live.execution["steps"]]}}
        if name == "explain_asset":
            aid = str(args.get("asset_id", "")).upper()
            if aid.endswith("_LEVEL"):
                aid = aid[:-6]
            st = live.state()["assets"].get(aid)
            if st is None:
                return {"error": f"Unknown asset {aid}"}
            await self._ui(ctx, {"action": "focus", "asset_id": aid})
            tele = live.telemetry()
            extra = {}
            for group in ("tanks", "pumps", "wells", "trucks", "disposal_wells"):
                if aid in tele.get(group, {}):
                    extra = tele[group][aid]
            return {"asset_id": aid, "name": nice_name(aid), **st, "live": extra}
        if name == "forecast_what_if":
            actions = [self._clean_action(a) for a in args.get("actions", [])]
            fc = await live.forecast(actions)
            await self._ui(ctx, {"action": "preview", "actions": actions, "forecast": fc})
            ctx["data"]["forecast"] = {"summary": fc["summary"], "cost": fc["cost"]}
            return {"summary": fc["summary"], "cost": fc["cost"], "warnings": fc["warnings"],
                    "key_events": [e["text"] for e in fc["events"] if e["type"] in ("SHUTDOWN_LEVEL", "SHUTDOWN_POSSIBLE", "WELLS_SHUT_IN")]}
        if name == "find_plans":
            res = await live.make_plans()
            await self._ui(ctx, {"action": "show_panel", "panel": "plans"})
            ctx["data"]["plans"] = res
            return {"at_risk": res["at_risk"], "warnings": res.get("warnings", []),
                    "plans": [{k: p[k] for k in ("id", "summary", "keeps_running", "robust", "cannot_save", "notes")}
                              | {"total_usd": p["cost"]["total_usd"]} for p in res["plans"]]}
        if name == "approve_plan":
            pid = str(args.get("plan_id", ""))
            if pid not in live.plans:
                return {"error": f"No plan {pid}. Call find_plans first."}
            if self.require_confirm:
                p = await live.propose_plan(pid, source=ctx["source"], by=ctx["by"])
                ctx["pending"].append(p)
                return {"status": "WAITING_FOR_CONFIRMATION", "plan": live.plans[pid]["summary"]}
            res = await live.approve(pid, by=ctx["by"] or "Jarvis")
            return {"status": "APPROVED", "steps": [s["text"] for s in res["steps"]]}
        if name == "control_equipment":
            a = self._clean_action(args)
            res = await live.command(a, source=ctx["source"], by=ctx["by"], confirm=self.require_confirm)
            if res.get("status") == "PENDING":
                ctx["pending"].append(res)
                return {"status": "WAITING_FOR_CONFIRMATION", "text": res["text"],
                        "if_confirmed": {"keeps_running": res["preview"]["summary"]["keeps_running"],
                                         "first_shutdown": (res["preview"]["summary"]["first_shutdown"] or {}).get("text"),
                                         "total_usd": res["preview"]["cost"]["total_usd"]}}
            return res
        if name == "show_on_screen":
            d = {"action": args.get("action", "focus")}
            ids = [str(x).upper() for x in args.get("asset_ids") or []]
            if ids:
                d["asset_id"], d["asset_ids"] = ids[0], ids
            if args.get("t_min") is not None:
                d["t_min"] = float(args["t_min"])
            if args.get("panel"):
                d["panel"] = args["panel"]
            await self._ui(ctx, d)
            return {"ok": True}
        return {"error": f"Unknown tool {name}"}

    @staticmethod
    def _clean_action(a: dict) -> dict:
        out = {"type": str(a.get("type", "")).upper(), "asset_id": str(a.get("asset_id", "")).upper()}
        if a.get("value") is not None:
            out["value"] = a["value"]
        if a.get("target_id"):
            out["target_id"] = str(a["target_id"]).upper()
        out["at_min"] = float(a.get("at_min") or 0)
        return out

    async def _ui(self, ctx: dict, directive: dict) -> None:
        directive = {**directive, "source": "jarvis", "by": ctx.get("by")}
        ctx["ui"].append({k: v for k, v in directive.items() if k != "forecast"})
        await self.live.broadcast("ui", directive)

    # ------------------------------------------------------------------ Claude engine
    async def _claude(self, text: str, session: str, ctx: dict) -> str:
        history = self.sessions.setdefault(session, [])
        if len(history) > MAX_HISTORY_MESSAGES:
            history.clear()  # start fresh rather than editing old turns
        clock = self.live.f.clock_at(self.live.plant.t)
        messages = history + [{"role": "user", "content": f"[{clock}, from {ctx['source']}{' ' + ctx['by'] if ctx['by'] else ''}] {text}"}]
        system = [{"type": "text", "text": _system_prompt(self.live), "cache_control": {"type": "ephemeral"}}]

        final_text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self._client.beta.messages.create(
                model=MODEL,
                max_tokens=4000,
                system=system,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if resp.stop_reason == "refusal":
                final_text = "I can't help with that one. Try asking about the field."
                break
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            final_text = " ".join(b.text for b in resp.content if b.type == "text").strip() or final_text
            if resp.stop_reason != "tool_use" or not tool_uses:
                break
            results = []
            for tu in tool_uses:
                try:
                    out = await self.run_tool(tu.name, dict(tu.input), ctx)
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(out, default=str)})
                except Exception as exc:
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"Error: {exc}", "is_error": True})
            messages.append({"role": "user", "content": results})
        self.sessions[session] = messages
        return final_text or "Done."

    # ------------------------------------------------------------------ rules engine
    async def _rules(self, text: str, ctx: dict, help_fallback: bool = True) -> str | None:
        t = " " + text.lower().replace("-", " ") + " "
        for w, n in NUM_WORDS.items():
            t = re.sub(rf"\b{w}\b", str(n), t)
        f = self.live.f

        def asset(kind: str, n: str) -> str:
            return {"tank": "TANK_", "pump": "PUMP_", "well": "WELL_", "truck": "TRUCK_"}[kind] + n

        # approve plan N
        m = re.search(r"approve( plan)? ?(\d)?", t)
        if m and "plan" in t:
            pid = f"plan_{m.group(2) or 1}"
            if not self.live.plans:
                await self.run_tool("find_plans", {}, ctx)
            r = await self.run_tool("approve_plan", {"plan_id": pid}, ctx)
            if "error" in r:
                return r["error"]
            return f"{r['plan']} Waiting for your confirmation." if r["status"].startswith("WAITING") else "Plan approved."

        # equipment: send truck / open|close crossover|valve / choke|shut in well / pump on|off
        m = re.search(r"send truck (\d)( to tank (\d))?", t)
        if m:
            target = m.group(3) or (self.live.incident or {}).get("tank_id", "TANK_3").split("_")[-1]
            return await self._rules_command({"type": "SEND_TRUCK", "asset_id": asset("truck", m.group(1)),
                                              "target_id": asset("tank", target)}, ctx)
        m = re.search(r"\b(open|close)\b.*?(crossover|valve)( d)? ?(\d+)?", t)
        if m:
            vid = "VALVE_XO_34" if m.group(2) == "crossover" or not m.group(4) else f"VALVE_D_{m.group(4)}"
            if m.group(2) == "valve" and m.group(4) and len(m.group(4)) == 2:
                vid = f"VALVE_XO_{m.group(4)}"
            return await self._rules_command({"type": "SET_VALVE", "asset_id": vid,
                                              "value": "OPEN" if m.group(1) == "open" else "CLOSED"}, ctx)
        m = re.search(r"(choke|shut in|shut) well (\d+)(?:.*?(\d+) ?(?:%|percent))?", t)
        if m:
            frac = 0.0 if m.group(1).startswith("shut") else (int(m.group(3)) / 100 if m.group(3) else 0.5)
            return await self._rules_command({"type": "CHOKE_WELL", "asset_id": asset("well", m.group(2)), "value": frac}, ctx)
        m = re.search(r"turn (on|off) pump (\d)|pump (\d) (on|off)", t)
        if m:
            on, n = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
            return await self._rules_command({"type": "SET_PUMP", "asset_id": asset("pump", n), "value": on.upper()}, ctx)

        # time slider
        m = re.search(r"(fast forward|skip|jump|go|show|in)\D*(\d+(?:\.\d+)?) ?(hours?|hrs?|minutes?|mins?)", t)
        if m:
            mins = float(m.group(2)) * (60 if m.group(3).startswith("h") else 1)
            await self._ui(ctx, {"action": "set_time", "t_min": mins})
            fc = self.live.last_forecast
            snap = None
            if fc:
                snap = min(fc["timeline"], key=lambda s: abs(s["t_min"] - mins))
            if snap:
                hot = max(snap["tanks"].items(), key=lambda kv: kv[1])
                return (f"Showing {f.clock_text(self.live.plant.t + mins)}. {nice_name(hot[0])} would be at {hot[1]:.0f}%, "
                        f"with {snap['wells_online']} of {len(f.wells)} wells online if nothing changes.")
            return f"Showing {f.clock_text(self.live.plant.t + mins)}."
        if re.search(r"\b(back to now|reset view|clear|now)\b", t) and re.search(r"show|go|back|clear|reset", t):
            await self._ui(ctx, {"action": "clear"})
            return "Back to live."

        # look at an asset
        m = re.search(r"(tank|pump|well|truck) (\d+)", t)
        if m and re.search(r"show|focus|zoom|look|go to|where|what about|how is|how's|status of|check", t):
            aid = asset(m.group(1), m.group(2))
            info = await self.run_tool("explain_asset", {"asset_id": aid}, ctx)
            if "error" in info:
                return info["error"]
            return self._describe_asset(info)
        if "crossover" in t and re.search(r"show|focus|zoom|look|where", t):
            await self._ui(ctx, {"action": "focus", "asset_id": "VALVE_XO_34"})
            pos = self.live.telemetry()["valves"]["VALVE_XO_34"]
            return f"That's the Tank 3 to 4 crossover. It's {pos.lower()}."

        # plans / cheapest fix
        if re.search(r"plan|cheapest|fix|options|what should|what do (we|i) do|recommend|save", t):
            res = await self.run_tool("find_plans", {}, ctx)
            if not res["plans"]:
                return "Nothing is at risk right now. No plan needed."
            p = res["plans"][0]
            line = f"Cheapest plan: {p['summary']} ${p['total_usd']:,.0f}."
            if not p["keeps_running"]:
                line += f" It can't save {', '.join(nice_name(x) for x in p['cannot_save'])}."
            robust = next((x for x in res["plans"] if x["robust"]), None)
            if robust and robust is not p:
                line += f" For margin if inflow runs high, {robust['id'].replace('_', ' ')} costs ${robust['total_usd']:,.0f}."
            return line + " Say approve plan 1 to run it."

        # countdown / status
        if re.search(r"how long|time left|when|countdown|until", t):
            cd = self.live.countdown()
            return cd["text"] if cd else "No tank reaches shutdown in the next 6 hours."
        if re.search(r"status|what'?s (failing|wrong|happening|going on)|report|sitrep|update|alarm", t):
            st = await self.run_tool("get_field_status", {}, ctx)
            parts = []
            if st["incident"]:
                inc = st["incident"]
                parts.append(f"{inc['summary']} {inc.get('reason', '')}")
                await self._ui(ctx, {"action": "focus", "asset_id": inc["root_asset_id"]})
            if st["countdown"]:
                parts.append(st["countdown"]["text"])
            parts += st["warnings"][:1]
            if not parts:
                parts.append(f"All quiet at {f.clock_text(self.live.plant.t)}. Every tank is safe for the next 6 hours.")
            return " ".join(p.strip() for p in parts)

        if not help_fallback:
            return None
        return ("I can show assets, report status, give the countdown, find the cheapest plan, "
                "and open valves or send trucks. Try: what's failing, or send truck 1 to tank 3.")

    async def _rules_command(self, action: dict, ctx: dict) -> str:
        r = await self.run_tool("control_equipment", {**action, "value": action.get("value"),
                                                      "target_id": action.get("target_id"), "at_min": None}, ctx)
        if r.get("status") == "REJECTED":
            return f"Can't do that: {r.get('error')}"
        if r.get("status") == "WAITING_FOR_CONFIRMATION":
            fx = r["if_confirmed"]
            effect = "Every tank stays running." if fx["keeps_running"] else (f"Even so, {fx['first_shutdown'].lower()}." if fx["first_shutdown"] else "")
            return f"{r['text']} {effect} Waiting for your confirmation."
        return f"Done. {r.get('text', '')}"

    @staticmethod
    def _describe_asset(info: dict) -> str:
        name = info["name"]
        live = info.get("live", {})
        if "level_pct" in info and info["level_pct"] is not None:
            line = f"{name} is at {info['level_pct']:.0f}%"
            obs = info.get("observed_bbl_hr")
            if obs is not None:
                line += f", changing {obs:+.0f} barrels an hour"
            line += "."
        elif "flow_bbl_hr" in info:
            line = f"{name} is moving {info['flow_bbl_hr'] or 0:.0f} barrels an hour."
        elif "position" in info:
            line = f"{name} is {str(info['position']).lower()}."
        elif "phase" in info:
            line = f"{name} is {str(info['phase']).lower().replace('_', ' ')}" + (
                f" to {nice_name(info['target_id'])}, ETA {info['eta_clock']}." if info.get("target_id") else ".")
        elif "rate_bbl_hr" in info:
            line = f"{name} is making {info['rate_bbl_hr'] or 0:.0f} barrels of water an hour."
        else:
            line = f"{name}."
        if info.get("reason"):
            line += " " + info["reason"]
        return line
