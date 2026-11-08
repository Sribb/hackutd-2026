"""Jarvis: the console's side panel. A question in; an answer, camera moves, and
proposed fixes out.

Two engines share the same tools:
  - rules:  instant regex answers for the common questions ("what's failing", "how long",
            "show pump 3", "what am I looking at", "send truck 1 to tank 3"). Works with
            no API key. Ported from draft PR #21.
  - claude: Claude picks tools for anything else, and can see a screenshot of the 3D view.
            Needs ANTHROPIC_API_KEY. Model from JARVIS_MODEL (default claude-sonnet-5-5).

JARVIS_MODE: auto (default: rules first, Claude for the rest), rules, or llm.

Jarvis never changes equipment. propose_action stores a PENDING proposal; a person
confirms it with POST /api/actions/{id}/confirm. Every number comes from a tool result.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, Optional

from . import jarvis_intents, jarvis_tools   # console tools and intents (docs/JARVIS_CONSOLE.md section 5)
from .fieldmap import name
from .live import ActionError, LiveField

log = logging.getLogger("freeboard.jarvis")

DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_TOOL_ROUNDS = 6
NO_KEY = "Jarvis needs ANTHROPIC_API_KEY on the server."
# Models that take server-side refusal fallback ("default" form, Claude API).
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}
# Open-ended questions skip the rules fast path when Claude is available.
COMPLEX = re.compile(r"\b(what if|why|instead|compare|versus|vs\.?|explain|should (we|i)|better|cheaper than|unless|and then)\b")
NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
# A number after one of these words is not a tank ("well 9", "valve d 3", "truck 1").
NOT_TANK_WORDS = {"well", "wells", "pump", "pumps", "truck", "trucks", "valve", "valves", "d", "swd", "plan", "step"}
# A tank question that also asks for a fix gets the plans line too.
ASKS_FIX = re.compile(r"\bplans?\b|cheapest|\bfix(es)?\b|what should|what do (we|i) do|recommend")
HELP = ("I can show and explain assets, report what's failing, give the time to shutdown, find the "
        "cheapest plan, and propose fixes like 'open the crossover' or 'send truck 1 to tank 3'.")


class JarvisUnavailable(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


ACTION_PROPS = {
    "type": {"type": "string", "enum": ["SET_VALVE", "SET_PUMP", "CHOKE_WELL", "SEND_TRUCK"]},
    "asset_id": {"type": "string", "description": "Asset ID, e.g. VALVE_XO_34, PUMP_2, WELL_11, TRUCK_1"},
    "value": {"type": ["string", "null"],
              "description": "OPEN or CLOSED for valves, ON or OFF for pumps, a fraction like '0.5' for "
                             "CHOKE_WELL (0 shuts the well in), null for trucks"},
    "target_id": {"type": ["string", "null"], "description": "Tank ID for SEND_TRUCK, otherwise null"},
}

TOOLS = [
    {
        "name": "focus_camera",
        "description": "Move the 3D camera on every console to one asset. Call it whenever you talk about a "
                       "specific asset so the screen follows the conversation.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["asset_id"],
                         "properties": {"asset_id": {"type": "string"}}},
    },
    {
        "name": "explain_asset",
        "description": "Live readings, status, active alerts, and design numbers for one asset (tank level and "
                       "fill rate, pump flow and run state, valve position, well flow, truck job).",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["asset_id"],
                         "properties": {"asset_id": {"type": "string"}}},
    },
    {
        "name": "get_forecast",
        "description": "Replay the next 6 hours from the live readings with a list of hypothetical actions "
                       "(empty list = do nothing). Returns shutdown times, wells lost, cost, and data warnings. "
                       "Changes nothing.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["actions"], "properties": {
            "actions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["type", "asset_id", "value", "target_id", "at_min"],
                "properties": {**ACTION_PROPS, "at_min": {"type": ["number", "null"],
                                                          "description": "Minutes from now; null means now"}}}}}},
    },
    {
        "name": "get_plans",
        "description": "The cheapest plans that keep every tank below shutdown, from the live readings, ranked "
                       "by dollars. Changes nothing.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
    },
    {
        "name": "propose_action",
        "description": "Propose one equipment command (open or close a valve, turn a pump on or off, choke or "
                       "shut in a well, send a truck to a tank). It is only stored as a proposal: nothing "
                       "happens until a person taps Confirm on the console. Tell the operator it is waiting "
                       "for their confirmation.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False,
                         "required": ["type", "asset_id", "value", "target_id", "reason"],
                         "properties": {**ACTION_PROPS, "reason": {
                             "type": "string", "description": "One sentence: why, citing the tool result"}}},
    },
]
TOOLS += jarvis_tools.extend_tools(TOOLS)


class Jarvis:
    def __init__(self, live: LiveField, forecast_fn: Callable[[dict, list], dict],
                 plans_fn: Callable[[dict], dict]) -> None:
        self.live = live
        self.forecast_fn = forecast_fn
        self.plans_fn = plans_fn
        self.model = os.environ.get("JARVIS_MODEL", DEFAULT_MODEL)
        self.effort = os.environ.get("JARVIS_EFFORT", "low")
        self._client = None
        self.system = self._system_prompt()

    # ---------- entry point ----------
    @staticmethod
    def has_key() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def status(self) -> dict:
        mode = os.environ.get("JARVIS_MODE", "auto").lower()
        return {"mode": mode, "model": self.model, "llm_available": self.has_key() and mode != "rules"}

    def refresh(self) -> None:
        """Rebuild the asset list in Claude's prompt after the live field reloads."""
        self.system = self._system_prompt()

    async def handle(self, message: str, selected: Optional[list] = None,
                     image_png_base64: Optional[str] = None, role: Optional[str] = None) -> dict:
        """`role` is the asker's control-token role (auth.py), recorded on Jarvis's proposals
        in the audit log. It never confirms anything."""
        text = (message or "").strip()
        selected = [s.upper() for s in selected or [] if isinstance(s, str)]
        mode = os.environ.get("JARVIS_MODE", "auto").lower()
        key = self.has_key()
        ctx: dict = {"ui": [], "proposals": [], "role": role}
        rules_first = mode == "rules" or (mode != "llm" and not (key and (COMPLEX.search(text.lower()) or
                                                                           (image_png_base64 and not selected))))
        # Deterministic intents first, even before COMPLEX sends a question to Claude.
        reply, source = (None if mode == "llm" else await jarvis_intents.match(self, text, selected, ctx)), "rules"
        if reply is None and rules_first:
            reply = await self._rules(text, selected, ctx)
        if reply is None:
            if mode == "rules":
                reply = HELP
            elif not key:
                raise JarvisUnavailable(NO_KEY)
            else:
                reply, source = await self._claude(text, selected, image_png_base64, ctx), "claude"
        return {"reply": reply, "source": source, "ui": ctx["ui"], "proposals": ctx["proposals"],
                **jarvis_tools.extras(ctx)}

    # ---------- tools (both engines) ----------
    async def run_tool(self, tool: str, args: dict, ctx: dict) -> Any:
        out = await jarvis_tools.run(self, tool, args, ctx)
        if out is not jarvis_tools.PASS:
            return out
        live = self.live
        if tool == "focus_camera":
            aid = str(args.get("asset_id", "")).upper()
            if not live.fmap.kind_of(aid):
                return {"error": f"Unknown asset {aid}."}
            await self._focus(aid, ctx)
            return {"ok": True, "asset_id": aid}
        if tool == "explain_asset":
            aid = str(args.get("asset_id", "")).upper()
            kind = live.fmap.kind_of(aid)
            if not kind:
                return {"error": f"Unknown asset {aid}."}
            spec = getattr(live.fmap, {"swd": "swds"}.get(kind, kind + "s"))[aid]
            return {"asset_id": aid, "name": name(aid), "kind": kind, "clock": live.fmap.clock(live.t),
                    "live": live.state()["assets"].get(aid, {}), "design": spec,
                    "alerts": [{k: a[k] for k in ("kind", "severity", "message")} for a in live.detector.active_for(aid)]}
        if tool == "get_forecast":
            actions = [self._forecast_action(a) for a in args.get("actions") or []]
            sc = live.live_scenario()
            fc = await asyncio.to_thread(self.forecast_fn, sc, actions)
            # Tanks with no fresh level: the forecast can't say they're safe, so neither can Jarvis.
            unknown = fc.get("unknown_tanks")
            if unknown is None:
                unknown = sorted(t for t, lv in (sc.get("initial_levels_pct") or {}).items() if lv is None)
            return {"now": live.fmap.clock(live.t),
                    "events": [{"type": e["type"], "asset_id": e.get("asset_id"), "t_min": e.get("t_min"), "text": e.get("text")}
                               for e in fc.get("events", [])][:12],
                    "cost": fc.get("cost"), "warnings": fc.get("warnings", []), "unknown_tanks": list(unknown)}
        if tool == "get_plans":
            res = await asyncio.to_thread(self.plans_fn, live.live_scenario())
            return {"plans": [{"id": p["id"], "summary": p["summary"], "actions": p["actions"],
                               "total_usd": p.get("cost", {}).get("total_usd"),
                               "keeps_running": p.get("keeps_running"), "robust": p.get("robust"),
                               "cannot_save": p.get("cannot_save", [])} for p in res.get("plans", [])]}
        if tool == "propose_action":
            try:
                p = await live.propose(args, reason=str(args.get("reason") or ""), source="jarvis",
                                       role=ctx.get("role"))
            except ActionError as e:
                return {"error": e.detail}
            ctx["proposals"].append(p)
            self._note_ui(p["action"]["asset_id"], ctx)
            return {"proposal_id": p["id"], "status": p["status"], "text": p["text"],
                    "note": "Waiting for a person to confirm on the console." if p["status"] == "PENDING" else p.get("result")}
        return {"error": f"Unknown tool {tool}."}

    def _note_ui(self, aid: str, ctx: dict) -> None:
        jarvis_tools.note_focus(self.live, aid, ctx)

    async def _focus(self, aid: str, ctx: dict) -> None:
        await jarvis_tools.focus(self, aid, ctx)

    @staticmethod
    def _forecast_action(a: dict) -> dict:
        out = {"type": str(a.get("type", "")).upper(), "asset_id": str(a.get("asset_id", "")).upper(),
               "at_min": float(a.get("at_min") or 0)}
        val = a.get("value")
        if out["type"] == "CHOKE_WELL":
            try:
                out["value"] = float(val)
            except (TypeError, ValueError):
                out["value"] = val
        elif val is not None:
            out["value"] = str(val).upper()
        if a.get("target_id"):
            out["target_id"] = str(a["target_id"]).upper()
        return out

    # ---------- Claude ----------
    def _system_prompt(self) -> str:
        f = self.live.fmap
        ids = {"tanks": sorted(f.tanks), "pumps": sorted(f.pumps), "valves": sorted(f.valves),
               "wells": sorted(f.wells), "disposal wells": sorted(f.swds), "trucks": sorted(f.trucks)}
        tanks = {tid: {"capacity_bbl": t["capacity_bbl"], "shutdown_pct": t["shutdown_pct"]} for tid, t in sorted(f.tanks.items())}
        return (
            "You are Jarvis, the assistant in the side panel of Freeboard, a night-shift console for an oil "
            "field's produced-water tanks. Wells fill tanks with water; transfer pumps empty them to disposal "
            "wells; if a tank reaches its shutdown level, its wells shut in and production is lost. The operator "
            "sees a 3D model of the field and may send a screenshot of it with selected assets.\n\n"
            "How to answer:\n"
            "- One to three short, plain sentences. No markdown or lists.\n"
            "- Every number you state (levels, times, rates, dollars) must come from a tool result in this turn. "
            "If a tool reports missing or stale data, say so first; an unknown tank is never safe.\n"
            "- Look up an asset with explain_asset before describing it, and call focus_camera on the asset you "
            "are talking about.\n"
            "- For 'what if' use get_forecast; for the cheapest fix use get_plans.\n"
            "- To fix something, call propose_action. It only creates a proposal; tell the operator it is "
            "waiting for their confirmation on the console. Never claim equipment has changed.\n"
            "- Name assets like a person would ('Tank 3', 'the Tank 3 to 4 crossover'), not by ID.\n\n"
            f"Asset IDs: {json.dumps(ids)}\nTanks: {json.dumps(tanks)}"
            + jarvis_tools.PROMPT
        )

    def _anthropic(self):
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as e:
                raise JarvisUnavailable(f"Jarvis needs the anthropic package on the server (pip install anthropic): {e}")
            self._client = AsyncAnthropic()
        return self._client

    async def _claude(self, text: str, selected: list, image_b64: Optional[str], ctx: dict) -> str:
        import anthropic

        client = self._anthropic()
        content: list[dict] = []
        if image_b64:
            data = image_b64.split(",", 1)[1] if image_b64.startswith("data:") else image_b64
            media = "image/jpeg" if data.startswith("/9j/") else "image/png"
            content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
        live = self.live
        note = (f"[Live clock {live.fmap.clock(live.t)}. Selected in the 3D view: {', '.join(selected) or 'nothing'}."
                f"{' The image is a screenshot of the 3D view.' if image_b64 else ''}]")
        content.append({"type": "text", "text": f"{note}\n{text or 'What am I looking at?'}"})
        messages: list[dict] = [{"role": "user", "content": content}]
        kwargs: dict = {"model": self.model, "max_tokens": 16000, "tools": TOOLS,
                        "system": [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                        "output_config": {"effort": self.effort}}
        if self.model in FALLBACK_MODELS:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        last_text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                resp = await client.beta.messages.create(messages=messages, **kwargs)
            except anthropic.AuthenticationError:
                raise JarvisUnavailable("Jarvis's ANTHROPIC_API_KEY was rejected. Check the key on the server.")
            except anthropic.RateLimitError:
                raise JarvisUnavailable("Claude is rate limited right now. Try again in a minute.")
            except anthropic.APIStatusError as e:
                raise JarvisUnavailable(f"Claude returned an error ({e.status_code}). Try again.")
            except anthropic.APIConnectionError:
                raise JarvisUnavailable("Can't reach Claude from the server. Check the network.")
            if resp.stop_reason == "refusal":
                return "I can't help with that one. Ask me about the field."
            messages.append({"role": "assistant", "content": resp.content})  # thinking blocks kept unchanged
            last_text = " ".join(b.text for b in resp.content if b.type == "text").strip() or last_text
            if resp.stop_reason == "pause_turn":
                continue
            uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                break
            results = []
            for tu in uses:
                try:
                    out = await self.run_tool(tu.name, dict(tu.input), ctx)
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(out, default=str)})
                except Exception as e:  # noqa: BLE001 - report to Claude, never crash the request
                    log.warning("Jarvis tool %s failed: %s", tu.name, e)
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"Error: {e}", "is_error": True})
            messages.append({"role": "user", "content": results})
        return last_text or "Done."

    # ---------- rules ----------
    def _asset_in(self, t: str) -> Optional[str]:
        f = self.live.fmap
        m = re.search(r"\b(tank|pump|well|truck|swd|disposal well) (\d+)\b", t)
        if m:
            prefix = {"tank": "TANK_", "pump": "PUMP_", "well": "WELL_", "truck": "TRUCK_", "swd": "SWD_",
                      "disposal well": "SWD_"}[m.group(1)]
            aid = prefix + m.group(2)
            return aid if f.kind_of(aid) else None
        m = re.search(r"\b(?:discharge )?valve (?:d ?)?(\d+)\b", t)
        if m and f.kind_of(f"VALVE_D_{m.group(1)}"):
            return f"VALVE_D_{m.group(1)}"
        m = re.search(r"\b((?:tank|pump|well|truck|swd|valve_d|valve_xo)_\d+)\b", t)
        if m and f.kind_of(m.group(1).upper()):
            return m.group(1).upper()
        return None

    def _tanks_named(self, t: str) -> list[str]:
        """Tanks a sentence names, in order: 'tank 3 to 4', '3 and 4', 'between 5 and 6'.
        Numbers that belong to another asset ('well 9', 'valve d 3') or a unit don't count."""
        out: list[str] = []
        for m in re.finditer(r"(?:\b([a-z]+) )?\b(\d+)\b(?!:| ?(?:%|percent|min|minutes?|hours?|hrs?|bbl|barrels?)\b)", t):
            if m.group(1) in NOT_TANK_WORDS:
                continue
            tid = f"TANK_{m.group(2)}"
            if tid in self.live.fmap.tanks and tid not in out:
                out.append(tid)
        return out

    def _headroom_bbl(self, tid: str) -> float:
        """Barrels a tank can take before shutdown; an unknown level counts as none."""
        tank = self.live.fmap.tanks[tid]
        lvl = self.live.value(tid, "level_pct")
        if lvl is None or self.live.detector.is_stale(tid, "level_pct"):
            return float("-inf")
        return (float(tank["shutdown_pct"]) - float(lvl)) / 100.0 * float(tank["capacity_bbl"])

    def _crossover_next_to(self, tid: str, closing: bool = False) -> Optional[str]:
        """The crossover on `tid` whose other tank has the most room (open ones first when closing)."""
        xos = [xo for xo in self.live.fmap.crossovers if tid in (xo.get("connects") or [])[:2]]
        if closing:
            xos = [xo for xo in xos if self.live.value(xo["id"], "position") == "OPEN"] or xos
        if not xos:
            return None
        other = lambda xo: next((e for e in xo["connects"][:2] if e != tid), tid)  # noqa: E731
        return max(xos, key=lambda xo: self._headroom_bbl(other(xo)))["id"]

    async def _tanks_in_trouble(self) -> list[str]:
        """Tanks with an active pump trip, then by earliest forecast shutdown."""
        f, out = self.live.fmap, []
        for a in self.live.detector.active.values():
            if a["kind"] == "PUMP_TRIP" and a["asset_id"] in f.pumps:
                tid = f.pumps[a["asset_id"]]["tank_id"]
                if tid not in out:
                    out.append(tid)
        if any(self._crossover_next_to(tid) for tid in out):
            return out
        fc = await self.run_tool("get_forecast", {"actions": []}, {"ui": [], "proposals": []})
        sds = sorted((e for e in fc.get("events", []) if e.get("type") == "SHUTDOWN_LEVEL" and e.get("asset_id") in f.tanks),
                     key=lambda e: e.get("t_min") if e.get("t_min") is not None else float("inf"))
        return out + [e["asset_id"] for e in sds if e["asset_id"] not in out]

    async def _crossover_in(self, t: str, closing: bool = False) -> tuple[Optional[str], Optional[str]]:
        """(crossover ID, None), or (None, why not). Named tanks first, then the tank in
        trouble, preferring the neighbor with the most headroom, then the first crossover."""
        f = self.live.fmap
        m = re.search(r"\bvalve_xo_\w+\b", t)
        if m and m.group(0).upper() in f.valves:
            return m.group(0).upper(), None
        if not f.crossovers:
            return None, "This field has no crossovers."
        named = self._tanks_named(t)
        if len(named) >= 2:
            pair = set(named[:2])
            xo = next((x for x in f.crossovers if set((x.get("connects") or [])[:2]) == pair), None)
            if xo:
                return xo["id"], None
            return None, f"There's no crossover between {name(named[0])} and {name(named[1])}."
        if named:
            aid = self._crossover_next_to(named[0], closing)
            return (aid, None) if aid else (None, f"{name(named[0])} has no crossover.")
        if closing:
            open_ = [x["id"] for x in f.crossovers if self.live.value(x["id"], "position") == "OPEN"]
            if open_:
                return open_[0], None
        for tid in await self._tanks_in_trouble():
            aid = self._crossover_next_to(tid, closing)
            if aid:
                return aid, None
        return f.crossovers[0]["id"], None

    async def _plans_line(self, ctx: dict) -> str:
        """The cheapest plan, and the cheapest robust one if that's a different plan.
        Focuses the camera on the cheapest plan's first step."""
        res = await self.run_tool("get_plans", {}, ctx)
        if not res["plans"]:
            return "No plan needed: nothing reaches shutdown in the next 6 hours."
        p = res["plans"][0]
        if p["actions"]:
            await self._focus(p["actions"][0]["asset_id"], ctx)

        def usd(x: dict) -> str:
            return f", ${x['total_usd']:,.0f}" if isinstance(x.get("total_usd"), (int, float)) else ""

        line = f"Cheapest plan{usd(p)}: {p['summary']}"
        if p["cannot_save"]:
            line += f" It can't save {', '.join(name(x) for x in p['cannot_save'])}."
        robust = next((x for x in res["plans"] if x.get("robust")), None)
        if robust and robust["id"] != p["id"]:
            line += f" Cheapest robust plan{usd(robust)}: {robust['summary']}"
        return line + " Ask me to propose any step and confirm it on the console."

    async def _rules(self, text: str, selected: list, ctx: dict) -> Optional[str]:
        t = " " + text.lower().replace("-", " ").replace("?", " ") + " "
        for w, n in NUM_WORDS.items():
            t = re.sub(rf"\b{w}\b", str(n), t)
        live = self.live

        # Fixes (proposals only).
        m = re.search(r"send truck (\d+)(?: to tank (\d+))?", t)
        if m:
            target = m.group(2)
            if not target:
                trip = next((a["asset_id"] for a in live.detector.active.values() if a["kind"] == "PUMP_TRIP"), None)
                target = live.fmap.pumps[trip]["tank_id"].split("_")[-1] if trip else None
            if not target:
                return "Which tank should the truck go to?"
            return await self._propose({"type": "SEND_TRUCK", "asset_id": f"TRUCK_{m.group(1)}",
                                        "target_id": f"TANK_{target}"}, text, ctx)
        m = re.search(r"^\s*(?:please |can you |could you |jarvis,? )?(open|close)\b", t)
        if m and re.search(r"crossover|valve", t):
            if "crossover" in t:
                aid, why_not = await self._crossover_in(t, closing=m.group(1) == "close")
                if why_not:
                    return why_not
            else:
                aid = self._asset_in(t)
            if aid and live.fmap.kind_of(aid) == "valve":
                return await self._propose({"type": "SET_VALVE", "asset_id": aid,
                                            "value": "OPEN" if m.group(1) == "open" else "CLOSED"}, text, ctx)
        m = re.search(r"\b(choke|shut in|shut) well (\d+)(?:\D+(\d+) ?(?:%|percent))?", t)
        if m:
            frac = 0.0 if m.group(1).startswith("shut") else (int(m.group(3)) / 100 if m.group(3) else 0.5)
            return await self._propose({"type": "CHOKE_WELL", "asset_id": f"WELL_{m.group(2)}", "value": frac}, text, ctx)
        m = re.search(r"turn (on|off) pump (\d+)|pump (\d+) (on|off)\b|(start|stop) pump (\d+)", t)
        if m:
            on, n = ((m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3)) if m.group(3)
                     else ("on" if m.group(5) == "start" else "off", m.group(6)))
            return await self._propose({"type": "SET_PUMP", "asset_id": f"PUMP_{n}", "value": on.upper()}, text, ctx)

        # What am I looking at.
        if re.search(r"looking at|what is this|what's this|whats this|this (asset|thing|one)|selected|what is that", t):
            ids = [a for a in selected if live.fmap.kind_of(a)][:3]
            if not ids:
                return None  # needs the screenshot, so Claude
            await self._focus(ids[0], ctx)
            return " ".join([await self._describe(a) for a in ids])

        # Show or check an asset ("Why is Tank 3 rising, and what's my cheapest fix?" adds the plans).
        if re.search(r"show|focus|zoom|look|go to|where|\bis\b|how's|status of|check|explain|tell me about|about", t):
            aid = (await self._crossover_in(t))[0] if "crossover" in t else self._asset_in(t)
            if aid:
                await self._focus(aid, ctx)
                line = await self._describe(aid)
                if ASKS_FIX.search(t):
                    line += " " + await self._plans_line(ctx)
                return line

        # Plans.
        if ASKS_FIX.search(t) or re.search(r"\boptions\b", t):
            return await self._plans_line(ctx)

        # Countdown.
        if re.search(r"how long|time left|countdown|until|when (does|will)|shutdown", t):
            fc = await self.run_tool("get_forecast", {"actions": []}, ctx)
            sd = next((e for e in fc["events"] if e["type"] == "SHUTDOWN_LEVEL"), None)
            unknown = fc.get("unknown_tanks") or []
            # The blind-tank warnings are said below in plain words, so skip their raw form here.
            others = [w for w in fc["warnings"] if not any(w.upper().startswith(t + " ") for t in unknown)]
            warn = (others[0] + " ") if others else ""
            blind = ""
            if unknown:
                names = [name(t) for t in unknown]
                who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
                blind = (f"I can't vouch for {who}: no fresh level reading, so treat "
                         f"{'it' if len(names) == 1 else 'them'} as at risk and check in person. ")
            if sd:
                await self._focus(sd["asset_id"], ctx)
                return (blind + warn + sd["text"]).strip()
            if unknown:
                await self._focus(unknown[0], ctx)
                return (blind + warn + "No tank with a fresh reading reaches shutdown in the next 6 hours "
                        "if nothing changes.").strip()
            return warn + "No tank reaches shutdown in the next 6 hours if nothing changes."

        # Status.
        if re.search(r"status|what'?s (failing|wrong|happening|going on|broken)|report|sitrep|alarms?|alerts?|problem|update", t):
            # Root causes first, then by priority (CF-7), then alarms before warnings.
            active = sorted(live.detector.active.values(),
                            key=lambda a: (a.get("root_id") is not None, a.get("priority") or "P3",
                                           a["severity"] != "ALARM"))
            if not active:
                return f"All quiet at {live.fmap.clock_text(live.t)}. No active alerts."
            await self._focus(active[0]["asset_id"], ctx)
            more = f" {len(active) - 2} more alerts are open." if len(active) > 2 else ""
            return " ".join(a["message"] for a in active[:2]) + more
        return None

    async def _propose(self, action: dict, text: str, ctx: dict) -> str:
        res = await self.run_tool("propose_action", {**action, "reason": f"Asked in Jarvis: \"{text}\""}, ctx)
        if "error" in res:
            return f"I can't propose that: {res['error']}"
        if res["status"] == "PENDING":
            return f"Proposed: {res['text']} It's waiting for your confirmation on the console."
        return f"{res['text']} {res.get('note') or ''}".strip()

    async def _describe(self, aid: str) -> str:
        info = await self.run_tool("explain_asset", {"asset_id": aid}, {"ui": [], "proposals": []})
        if "error" in info:
            return info["error"]
        lv, nm, kind = info["live"], info["name"], info["kind"]
        nm = nm[0].upper() + nm[1:]
        if lv.get("status") == "UNKNOWN":
            line = f"{nm} has no fresh reading, so treat it as unknown, not safe."
        elif kind == "tank":
            line = f"{nm} is at {lv['level_pct']:.0f}% (shutdown at {info['design']['shutdown_pct']}%)"
            obs, exp = lv.get("observed_bbl_hr"), lv.get("expected_bbl_hr")
            # Right after a pump changes state the fit can still lag; a rate whose sign
            # disagrees with what the pumps and wells explain would only mislead.
            if obs is not None and not (exp is not None and abs(exp) >= 1 and obs * exp < 0):
                line += f", changing {obs:+.0f} barrels an hour"
            line += "."
            # Why: the tank's own pumps' active alerts, then its forecast shutdown time.
            causes = [a["message"] for pid in self.live.fmap.pumps_by_tank.get(aid, [])
                      for a in self.live.detector.active_for(pid)]
            fc = await self.run_tool("get_forecast", {"actions": []}, {"ui": [], "proposals": []})
            sd = next((e for e in fc.get("events", [])
                       if e.get("type") == "SHUTDOWN_LEVEL" and e.get("asset_id") == aid), None)
            extra = causes[:1] + ([sd["text"]] if sd else [])
            if extra:
                line += " " + " ".join(extra)
        elif kind == "pump":
            run = "running" if lv.get("running") else "stopped"
            line = f"{nm} is {run}, moving {lv.get('flow_bbl_hr') or 0:.0f} barrels an hour."
        elif kind == "valve":
            line = f"{nm} is {str(lv.get('position')).lower()}."
        elif kind == "truck":
            phase = str(lv.get("phase", "IDLE")).lower().replace("_", " ")
            line = f"{nm} is {phase}" + (f" for {name(lv['target_id'])}." if lv.get("target_id") else ".")
        else:
            line = f"{nm} is flowing {lv.get('flow_bbl_hr') or 0:.0f} barrels an hour."
        alerts = [a["message"] for a in info["alerts"] if a["message"] not in line]
        return " ".join([line, *alerts[:1]])
