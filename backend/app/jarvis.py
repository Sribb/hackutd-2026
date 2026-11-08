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
confirms it with POST /api/actions/{id}/confirm. execute_plan ("fix it") starts a plan
run (runs.py) whose every step is such a proposal. When a run hits a problem, Jarvis
picks the re-plan (choose_plan): the rules pick, or Claude's pick among the planner's
plans when a key is set. Every number comes from a tool result.

CF-11: every number says where it came from and how old it is. A reading carries its age
("Tank 3 is at 71% (reading 20 s old)"), a forecast or plan its run time ("The forecast at
1:52 AM says ..."). An asset with no current reading gets "I have no current reading for
Tank 5, so I can't say it's safe." and nothing that sounds like a level. "What's failing?"
leads with the P1 root cause and its clock (CF-7's priority and root_id), then "N related
alarms" instead of every row.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable, Optional

from .fieldmap import name
from .forecast import clock_at
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
        "cheapest plan, run it step by step when you say 'fix it', and propose fixes like 'open the "
        "crossover' or 'send truck 1 to tank 3'.")
FIX_IT = re.compile(r"\b(fix (it|this|that|the field|everything|tank \d+)|make it so|"
                    r"(run|execute|start) (the |a )?(cheapest |best )?plan)\b")
# "Don't fix it", "do not run the plan yet": a negation in the clause before the match.
# "Fix it, actually no don't", "fix it. wait.", "make it so... never mind": a negation or a
# retraction anywhere after it. Either starts nothing: a person can always say it again.
FIX_NEGATION = re.compile(r"\b(don'?t|dont|do not|never|not|no|nope|nah|stop|cancel|abort|belay|hold off|"
                          r"hold on|wait|without|rather not|forget (it|that)|scratch that|nevermind|"
                          r"(should|would|must|wo|ca)n'?t|cannot)\b")
# A question isn't an order ("should I fix it?", "how would you run the plan?"), except a
# polite request ("can you fix it?", "could you please run the plan?").
QUESTION_START = re.compile(r"^\s*(how|what|why|when|which|who|where|should|shall|would|could|can|will|"
                            r"is|are|was|does|do|did|if|whether|any idea)\b")
POLITE_REQUEST = re.compile(r"^\s*(jarvis,? )?(please |(can|could|will|would) you\b( please)?)")
# Asking, not ordering, in the middle of a sentence: "I wonder if we should fix it".
MID_QUESTION = re.compile(r"\b(wonder(ing)? (if|whether)|any idea (if|whether|how)|is it (safe|ok|okay) to)\b")
REPLAN_TIMEOUT_S = 20.0


def fix_it_request(text: str) -> Optional[str]:
    """'run' when the text orders a plan run ("fix it", "make it so", "please run the
    plan"), 'negated' for "don't fix it" or "fix it, actually no don't", 'question' for
    "should I fix it?" or "fix it, right?", else None.

    Starting a run is the one thing here that leads to commands, so anything short of a
    plain order starts nothing: a negation in the clause before the match or anywhere
    after it, and a question about it anywhere in the message from its sentence on."""
    t = text.lower().replace("-", " ").replace("’", "'")
    m = FIX_IT.search(t)
    if not m:
        return None
    # The clause before the match: from the last sentence or clause break before it.
    start = max(t.rfind(c, 0, m.start()) for c in ".!?;:,") + 1
    if FIX_NEGATION.search(t[start:m.start()]) or FIX_NEGATION.search(t[m.end():]):
        return "negated"
    sentence_start = max(t.rfind(c, 0, m.start()) for c in ".!?;") + 1
    sentence = t[sentence_start:m.start()]
    jarvis_name = re.sub(r"^\s*(hey |ok |okay )?jarvis[,:]?\s*", "", sentence)
    ends = [i for i in (t.find(c, m.end()) for c in ".!?;") if i >= 0]
    end = min(ends) if ends else len(t)
    if "?" in t[end + 1:] or MID_QUESTION.search(t[sentence_start:]):
        return "question"   # "fix it. is that safe?", "can you fix it? will it hold?"
    if POLITE_REQUEST.search(jarvis_name):
        return "run"
    if t[end:end + 1] == "?" or QUESTION_START.search(jarvis_name):
        return "question"
    return "run"
PRIORITY_RANK = {"P1": 0, "P2": 1, "P3": 2}
KIND_WORDS = {"PUMP_TRIP": "trip", "TANK_HIGH": "high level", "TANK_HIGH_HIGH": "high-high",
              "VALVE_MISMATCH": "position mismatch", "SENSOR_STALE": "stale reading"}


def age_text(age_min: Optional[float]) -> str:
    """Live minutes -> '20 s', '7 min', '2.5 h'."""
    if age_min is None:
        return "unknown"
    s = max(0.0, float(age_min) * 60.0)
    if s < 59.5:
        return f"{s:.0f} s"
    if age_min < 119.5:
        return f"{age_min:.0f} min"
    return f"{age_min / 60.0:.1f} h"


def no_current_reading(nm: str, kind: str = "tank", many: bool = False) -> str:
    """The one sentence for an asset Jarvis can't see (CF-11). Never a level, never 'safe'."""
    it = "they're" if many else "it's"
    if kind == "tank":
        return f"I have no current reading for {nm}, so I can't say {it} safe."
    return f"I have no current reading for {nm}, so I can't say what {it} doing."


def and_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def n_of(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


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
        "name": "list_alerts",
        "description": "Open alerts grouped under their root cause, most urgent first: each group's root alert "
                       "with its priority (P1 = shutdown within 60 min), the clock it was raised, the forecast "
                       "consequence and when, and how many related alarms fold under it. When a group's root is "
                       "shelved, its most urgent unshelved alarm heads the group and `shelved_root` names the "
                       "shelved one. Also the tanks with no current reading. Changes nothing.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}, "required": []},
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
    {
        "name": "execute_plan",
        "description": "Run a whole plan to fix the field. Each step becomes a proposal at its time and waits "
                       "for a person to confirm it on the console; after each one the effect is checked in the "
                       "live readings, and the run pauses and re-plans if it isn't seen. plan_id comes from "
                       "get_plans; null runs the cheapest plan that keeps every tank below shutdown (robust if "
                       "one is). Tell the operator every step waits for their confirmation.",
        "strict": True,
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["plan_id"],
                         "properties": {"plan_id": {"type": ["string", "null"]}}},
    },
]


class Jarvis:
    def __init__(self, live: LiveField, forecast_fn: Callable[[dict, list], dict],
                 plans_fn: Callable[..., dict],
                 now_fn: Optional[Callable[[], Optional[tuple]]] = None) -> None:
        """`now_fn()` -> (scenario, actions already under way) or None: the one clock the rest
        of the backend plans from (live_api.one_now_for). With it, forecasts run the under-way
        actions first and plans are called `plans_fn(scenario, under_way)`; without it (or when
        it returns None), live.live_scenario() with nothing under way."""
        self.live = live
        self.forecast_fn = forecast_fn
        self.plans_fn = plans_fn
        self.now_fn = now_fn
        self.model = os.environ.get("JARVIS_MODEL", DEFAULT_MODEL)
        self.effort = os.environ.get("JARVIS_EFFORT", "low")
        self._client = None
        self.runs = None  # runs.RunManager, set by live_api.install
        self.system = self._system_prompt()

    # ---------- entry point ----------
    @staticmethod
    def has_key() -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def llm_ready(self) -> bool:
        return self.has_key() and os.environ.get("JARVIS_MODE", "auto").lower() != "rules"

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
        ctx: dict = {"ui": [], "proposals": [], "runs": [], "role": role}
        rules_first = mode == "rules" or (mode != "llm" and not (key and (COMPLEX.search(text.lower()) or
                                                                           (image_png_base64 and not selected))))
        reply, source = None, "rules"
        if rules_first:
            reply = await self._rules(text, selected, ctx)
        if reply is None:
            if mode == "rules":
                reply = HELP
            elif not key:
                raise JarvisUnavailable(NO_KEY)
            else:
                reply, source = await self._claude(text, selected, image_png_base64, ctx), "claude"
        return {"reply": reply, "source": source, "ui": ctx["ui"], "proposals": ctx["proposals"],
                "runs": ctx["runs"]}

    # ---------- tools (both engines) ----------
    async def run_tool(self, tool: str, args: dict, ctx: dict) -> Any:
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
            lv = live.state()["assets"].get(aid, {})
            out = {"asset_id": aid, "name": name(aid), "kind": kind, "clock": live.fmap.clock(live.t),
                   "as_of": live.fmap.clock_text(live.t), "live": lv, "reading": self._reading(aid), "design": spec,
                   "alerts": [{k: a.get(k) for k in ("id", "kind", "severity", "priority", "root_id", "message")}
                              for a in live.detector.active_for(aid)]}
            if lv.get("status") == "UNKNOWN":
                out["say"] = no_current_reading(name(aid), kind)  # never a level, never safe
            return out
        if tool == "list_alerts":
            return self._alert_groups()
        if tool == "get_forecast":
            actions = [self._forecast_action(a) for a in args.get("actions") or []]
            sc, base = self._now()   # on the loop, so it never reads half a tick
            fc = await asyncio.to_thread(self.forecast_fn, sc, base + actions)
            # Tanks with no fresh level: the forecast can't say they're safe, so neither can Jarvis.
            unknown = fc.get("unknown_tanks")
            if unknown is None:
                unknown = sorted(t for t, lv in (sc.get("initial_levels_pct") or {}).items() if lv is None)
            return {"now": live.fmap.clock(live.t), "as_of": live.fmap.clock_text(live.t),
                    "source": f"forecast run at {live.fmap.clock_text(live.t)} on the live readings",
                    "events": [{"type": e["type"], "asset_id": e.get("asset_id"), "t_min": e.get("t_min"), "text": e.get("text")}
                               for e in fc.get("events", [])][:12],
                    "cost": fc.get("cost"), "warnings": fc.get("warnings", []), "unknown_tanks": list(unknown)}
        if tool == "get_plans":
            sc, base = self._now()
            res = await (asyncio.to_thread(self.plans_fn, sc, base) if base else
                         asyncio.to_thread(self.plans_fn, sc))
            if self.runs is not None:  # so POST /api/plans/{id}/execute finds them
                self.runs.remember(res.get("plans", []))
            return {"as_of": live.fmap.clock_text(live.t),
                    "source": f"planner run at {live.fmap.clock_text(live.t)} on the live readings",
                    "unknown_tanks": self._blind_tanks(),
                    "plans": [{"id": p["id"], "summary": p["summary"], "actions": p["actions"],
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
        if tool == "execute_plan":
            if self.runs is None:
                return {"error": "Plan runs aren't available on this server."}
            try:
                pid = args.get("plan_id")
                if pid:
                    run, plan = await self.runs.execute_id(str(pid), source="jarvis"), None
                else:
                    run, choice = await self.runs.execute_best(source="jarvis")
                    plan = choice["plan"]
            except ActionError as e:
                return {"error": e.detail}
            if run is None:
                return {"run_id": None, "note": (plan or {}).get("summary") or "No plan needed right now."}
            ctx["runs"].append(run)
            await self._focus(run["steps"][0]["action"]["asset_id"], ctx)
            return {"run_id": run["id"], "plan_id": run["plan_id"], "summary": run["summary"],
                    "total_usd": run["cost_usd"], "robust": run["robust"], "status": run["status"],
                    "steps": [{"n": s["n"], "text": s["text"], "at": s["release_clock"], "status": s["status"]}
                              for s in run["steps"]],
                    "note": "Each step goes to the console at its time and waits for a person to confirm it."}
        return {"error": f"Unknown tool {tool}."}

    # ---------- re-plan for a paused run ----------
    async def choose_plan(self, plans: list[dict], exclude: set, why: str) -> dict:
        """Pick the re-plan: {plan, source, reason}. The rules pick unless Claude is
        available; Claude may only choose among the planner's plans, never invent one."""
        from .runs import pick_plan, touches

        rules = pick_plan(plans, exclude)
        out = {"plan": rules, "source": "rules", "reason": ""}
        usable = [p for p in plans if p.get("actions") and not touches(p, exclude)]
        if rules is None or not self.llm_ready() or len(usable) < 2:
            return out
        try:
            chosen = await asyncio.wait_for(self._claude_choose(usable, exclude, why), REPLAN_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - the rules pick stands
            log.warning("Jarvis re-plan fell back to rules: %s", e)
            return out
        plan = next((p for p in usable if p["id"] == chosen.get("plan_id")), None)
        if plan is None:
            return out
        return {"plan": plan, "source": "claude", "reason": str(chosen.get("reason") or "")[:400]}

    async def _claude_choose(self, plans: list[dict], exclude: set, why: str) -> dict:
        client = self._anthropic()
        live = self.live
        brief = [{"id": p["id"], "summary": p["summary"], "actions": p["actions"],
                  "total_usd": p.get("cost", {}).get("total_usd"), "keeps_running": p.get("keeps_running"),
                  "robust": p.get("robust"), "cannot_save": p.get("cannot_save", [])} for p in plans]
        text = (f"[Live clock {live.fmap.clock(live.t)}.] A plan run was paused: {why} "
                f"Don't use: {', '.join(sorted(exclude)) or 'nothing'}. These plans were simulated from the current "
                f"readings: {json.dumps(brief)}. Choose the one to offer the operator: keep every tank running "
                "first, then hold if inflow runs high, then cheapest. Answer with only JSON: "
                '{"plan_id": "<id from the list>", "reason": "<one plain sentence for the operator>"}')
        kwargs: dict = {"model": self.model, "max_tokens": 4000,
                        "system": [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                        "output_config": {"effort": self.effort}}
        if self.model in FALLBACK_MODELS:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        resp = await client.beta.messages.create(messages=[{"role": "user", "content": text}], **kwargs)
        out = " ".join(b.text for b in resp.content if b.type == "text")
        m = re.search(r"\{.*\}", out, re.S)
        return json.loads(m.group(0)) if m else {}
    def _now(self) -> tuple[dict, list]:
        """(scenario, actions under way) from the one clock, else the live field as is."""
        if self.now_fn is not None:
            try:
                got = self.now_fn()
            except Exception:  # noqa: BLE001 - a clock problem falls back, never breaks Jarvis
                log.exception("one-clock now failed; using the live field")
                got = None
            if got is not None:
                sc, base = got
                return sc, [dict(a) for a in base]
        return self.live.live_scenario(), []

    def _note_ui(self, aid: str, ctx: dict) -> None:
        d = {"type": "ui", "action": "focus", "asset_id": aid}
        if not ctx["ui"] or ctx["ui"][-1] != d:
            ctx["ui"].append(d)

    async def _focus(self, aid: str, ctx: dict) -> None:
        await self.live.focus(aid, source="jarvis")
        self._note_ui(aid, ctx)

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

    # ---------- where each number came from (CF-11) ----------
    def _reading(self, aid: str, signal: Optional[str] = None) -> Optional[dict]:
        """The asset's newest reading (of `signal`, if given): its age in live minutes and
        seconds, the connector it came from, and whether it is current. None if it never sent one."""
        live = self.live
        rows = [(s, r) for (a, s), r in live.latest.items() if a == aid and (signal is None or s == signal)]
        if not rows:
            return None
        sig, r = max(rows, key=lambda x: x[1]["t"])
        age = max(0.0, live.t - float(r["t"]))
        return {"signal": sig, "age_min": round(age, 2), "age_s": int(round(age * 60)), "age": age_text(age),
                "connector_id": r.get("source"), "current": not live.detector.is_stale(aid, signal),
                "wall_t": r.get("wall")}

    def _age_note(self, aid: str, signal: Optional[str] = None) -> str:
        """' (reading 20 s old)', or ' (reading from CONN_HOOK, 20 s old)' off the simulator."""
        r = self._reading(aid, signal)
        if r is None:
            return ""
        sim = getattr(getattr(self.live.registry, "sim", None), "id", "CONN_SIM")
        src = "reading" if r["connector_id"] in (None, sim) else f"reading from {r['connector_id']},"
        return f" ({src} {r['age']} old)"

    def _no_reading(self, aid: str) -> str:
        """The no-current-reading sentence plus how old the last one is, if there was one."""
        kind = self.live.fmap.kind_of(aid) or "tank"
        r = self._reading(aid, "level_pct" if kind == "tank" else None)
        last = f" Its last reading is {r['age']} old." if r else " It has sent no reading since startup."
        return no_current_reading(name(aid), kind) + last

    def _blind_tanks(self) -> list[str]:
        """Tanks with no current level: UNKNOWN, never safe."""
        assets = self.live.state()["assets"]
        return [tid for tid in self.live.fmap.tanks if assets.get(tid, {}).get("status") == "UNKNOWN"]

    def _blind_line(self, tids: list[str], skip: tuple = ()) -> str:
        tids = [t for t in tids if t not in skip]
        if not tids:
            return ""
        if len(tids) == 1:
            return self._no_reading(tids[0])
        return no_current_reading(and_list([name(t) for t in tids]), "tank", many=True)

    def _forecast_says(self, text: str, t_min: Optional[float] = None, at_t: Optional[float] = None) -> str:
        """'The forecast at 1:52 AM says Tank 3 reaches shutdown at 2:47 AM ... That's 55 min from now.'
        `at_t` is the live minute the forecast ran (default now); `t_min` minutes from now."""
        live = self.live
        at = live.fmap.clock_text(live.t if at_t is None else at_t)
        line = f"The forecast at {at} says {text.strip()}"
        if not line.endswith("."):
            line += "."
        if t_min is not None:
            mins = int(round(t_min))
            line += " That's now." if mins <= 0 else f" That's {mins} min from now."
        return line

    def _alert_groups(self) -> dict:
        """Active alerts grouped under their root cause (CF-7's root_id, followed to the top
        active alert), most urgent first: P1 before P2 before P3, then the soonest consequence,
        then alarms before warnings. Shelved roots go last. Changes nothing."""
        live = self.live
        active = list(live.detector.active.values())
        by_id = {a["id"]: a for a in active}

        def top(a: dict) -> dict:
            seen = {a["id"]}
            while a.get("root_id") in by_id and a["root_id"] not in seen:
                a = by_id[a["root_id"]]
                seen.add(a["id"])
            return a

        groups: dict[str, list[dict]] = {}
        for a in active:
            groups.setdefault(top(a)["id"], []).append(a)
        prio_t = getattr(live, "_prio_t", None)

        def shelved(a: dict) -> bool:
            return a.get("shelved_until_t") is not None

        def urgency(a: dict) -> tuple:
            mins = a.get("consequence_t")
            return (PRIORITY_RANK.get(a.get("priority") or "P3", 2),
                    float("inf") if mins is None else mins, a["severity"] != "ALARM", a["id"])

        # A shelve hides only the alert that was shelved. When a group's root is shelved, its
        # most urgent unshelved member heads the group instead, so a P1 that came after the
        # shelve is never folded out of sight under it.
        heads: dict[str, tuple[dict, Optional[dict]]] = {}
        for rid, members in groups.items():
            root = by_id[rid]
            open_ = [m for m in members if not shelved(m)]
            if shelved(root) and open_:
                heads[rid] = (min(open_, key=urgency), root)
            else:
                heads[rid] = (root, None)

        def row(a: dict) -> dict:
            at = a.get("consequence_t")
            return {"id": a["id"], "asset_id": a["asset_id"], "name": name(a["asset_id"]), "kind": a["kind"],
                    "severity": a["severity"], "priority": a.get("priority") or "P3", "message": a["message"],
                    "raised_at": clock_at(a["clock"], 0) if a.get("clock") else None,
                    "consequence": a.get("consequence"), "consequence_clock": a.get("consequence_clock"),
                    "minutes_to_consequence": a.get("minutes_to_consequence"),
                    "minutes_from_now": None if at is None else int(round(at - live.t)),
                    "usd_at_risk": a.get("usd_at_risk", 0),
                    "forecast_at": None if prio_t is None else live.fmap.clock_text(prio_t),
                    "reading": self._reading(a["asset_id"]), "root_id": a.get("root_id"),
                    "acked": bool(a.get("acked")), "shelved": a.get("shelved_until_t") is not None,
                    "shelved_until": a.get("shelved_until"), "shelve_reason": a.get("shelve_reason")}

        def rank(rid: str) -> tuple:
            head = heads[rid][0]
            return (shelved(head), *urgency(head))

        out = []
        for rid, members in sorted(groups.items(), key=lambda kv: rank(kv[0])):
            head, shelved_root = heads[rid]
            skip = {head["id"]} | ({shelved_root["id"]} if shelved_root else set())
            related = [m["id"] for m in members if m["id"] not in skip]
            out.append({"root": row(head), "related": len(related), "related_count": len(related),
                        "related_ids": related, "shelved_root": row(shelved_root) if shelved_root else None})
        return {"as_of": live.fmap.clock_text(live.t), "open": len(active), "groups": out,
                "unknown_tanks": self._blind_tanks()}

    def _root_line(self, g: dict) -> str:
        """One root cause: priority, when it was raised, what it says (with its reading's age),
        then the forecast's consequence and its clock, then the count folded under it."""
        r = g["root"]
        if r["kind"] == "SENSOR_STALE":
            line = self._no_reading(r["asset_id"])
        else:
            msg = r["message"].rstrip()
            if msg.endswith("."):
                msg = msg[:-1]
            line = f"{r['priority']}{' since ' + r['raised_at'] if r['raised_at'] else ''}: " \
                   f"{msg}{self._age_note(r['asset_id'])}."
            if r["consequence"] and r.get("consequence_clock") is None:
                line += " " + r["consequence"]  # no forecast time behind it (an unknown tank)
            elif r["consequence"]:
                at_t = getattr(self.live, "_prio_t", None)
                line += " " + self._forecast_says(r["consequence"], r["minutes_from_now"], at_t)
        sr = g.get("shelved_root")
        if sr:
            why = f" ({sr['shelve_reason']})" if sr.get("shelve_reason") else ""
            until = f" until {sr['shelved_until']}" if sr.get("shelved_until") else ""
            line = (f"{sr['name'][0].upper() + sr['name'][1:]} {KIND_WORDS.get(sr['kind'], 'alert')} is shelved"
                    f"{why}{until}; {r['name']} {KIND_WORDS.get(r['kind'], 'alert')} is still active. {line}")
        if g["related"]:
            line += f" {n_of(g['related'], 'related alarm')}."
        return line

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
            "- Every number you state (levels, times, rates, dollars) must come from a tool result in this turn, "
            "and must say where it came from and how old it is: a reading with its age from the tool's `reading` "
            "('Tank 3 is at 71% (reading 20 s old).'), a forecast or plan with its `as_of` time ('The forecast at "
            "1:52 AM says shutdown at 2:47 AM.').\n"
            "- If a tool reports missing or stale data, say so first; an unknown tank is never safe. For an asset "
            "with status UNKNOWN, or a tank in `unknown_tanks`, say exactly 'I have no current reading for Tank 5, "
            "so I can't say it's safe.' (the tool's `say` field) and never state a level for it.\n"
            "- For what's failing, call list_alerts: lead with the first group's root cause, its priority and its "
            "consequence clock, then say how many related alarms fold under it. Don't list every alarm. If a "
            "group has a `shelved_root`, say it is shelved and why, then the active alarm that heads the group.\n"
            "- Look up an asset with explain_asset before describing it, and call focus_camera on the asset you "
            "are talking about.\n"
            "- For 'what if' use get_forecast; for the cheapest fix use get_plans.\n"
            "- To fix something, call propose_action. It only creates a proposal; tell the operator it is "
            "waiting for their confirmation on the console. Never claim equipment has changed.\n"
            "- When the operator asks you to fix the whole problem ('fix it', 'run the plan'), call execute_plan. "
            "Every step of the run still waits for their confirmation.\n"
            "- Name assets like a person would ('Tank 3', 'the Tank 3 to 4 crossover'), not by ID.\n\n"
            f"Asset IDs: {json.dumps(ids)}\nTanks: {json.dumps(tanks)}"
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

    def _level_now(self, tid: str) -> Optional[float]:
        """A tank's current level, or None when it has no fresh reading."""
        lvl = self.live.value(tid, "level_pct")
        if lvl is None or self.live.detector.is_stale(tid, "level_pct"):
            return None
        return float(lvl)

    def _xo_open(self, vid: str) -> bool:
        """Reads OPEN, or last read OPEN (a stale OPEN still counts as open)."""
        return str(self.live.value(vid, "position") or "").upper() == "OPEN"

    def _none_open(self, vids: list[str], where: str = "") -> str:
        """'No crossover is open.', unless one of `vids` has no current position: then it
        says so, since a valve nobody can see might be open."""
        blind = [v for v in vids if self.live.detector.is_stale(v, "position")
                 or self.live.value(v, "position") is None]
        if not blind:
            return f"No crossover{where} is open."
        return (f"No crossover{where} reads open. " +
                no_current_reading(and_list([name(v) for v in blind]), "valve", many=len(blind) > 1))

    def _crossover_next_to(self, tid: str, closing: bool = False,
                           helps: bool = True) -> tuple[Optional[str], Optional[str]]:
        """(crossover ID, None) or (None, why not), for a crossover on `tid`.

        Opening (`helps`): only a crossover whose other tank is emptier than `tid` now, the
        emptiest first. Water only runs to the lower tank, so opening onto a fuller one, or
        one nobody can see, is no help. Closing: only an open one. `helps=False` (showing
        one) takes the neighbor with the most room."""
        xos = [xo for xo in self.live.fmap.crossovers if tid in (xo.get("connects") or [])[:2]]
        if not xos:
            return None, f"{name(tid)} has no crossover."
        other = lambda xo: next((e for e in xo["connects"][:2] if e != tid), tid)  # noqa: E731
        if closing:
            open_ = [xo for xo in xos if self._xo_open(xo["id"])]
            if not open_:
                return None, self._none_open([xo["id"] for xo in xos], f" on {name(tid)}")
            return open_[0]["id"], None
        if not helps:
            return max(xos, key=lambda xo: self._headroom_bbl(other(xo)))["id"], None
        here = self._level_now(tid)
        if here is None:
            return None, (f"I have no current reading for {name(tid)}, so I can't tell whether a crossover "
                          f"would help it.")
        emptier = [(lv, xo["id"]) for xo in xos
                   if (lv := self._level_now(other(xo))) is not None and lv < here]
        if not emptier:
            return None, (f"No crossover helps {name(tid)}: every tank it connects to is as full or fuller, "
                          f"or has no current reading.")
        return min(emptier)[1], None

    async def _tanks_in_trouble(self) -> list[str]:
        """Tanks with an active pump trip, then by earliest forecast shutdown."""
        f, out = self.live.fmap, []
        for a in self.live.detector.active.values():
            if a["kind"] == "PUMP_TRIP" and a["asset_id"] in f.pumps:
                tid = f.pumps[a["asset_id"]]["tank_id"]
                if tid not in out:
                    out.append(tid)
        if any(self._crossover_next_to(tid)[0] for tid in out):
            return out
        fc = await self.run_tool("get_forecast", {"actions": []}, {"ui": [], "proposals": []})
        sds = sorted((e for e in fc.get("events", []) if e.get("type") == "SHUTDOWN_LEVEL" and e.get("asset_id") in f.tanks),
                     key=lambda e: e.get("t_min") if e.get("t_min") is not None else float("inf"))
        return out + [e["asset_id"] for e in sds if e["asset_id"] not in out]

    async def _crossover_in(self, t: str, closing: bool = False,
                            helps: bool = True) -> tuple[Optional[str], Optional[str]]:
        """(crossover ID, None), or (None, why not). Named tanks first. Closing with none
        named: an open crossover, else "No crossover is open." Opening: the tank in trouble's
        emptier neighbor (see _crossover_next_to), else why no crossover helps; with no tank
        in trouble, the first crossover. `helps=False` (showing one) skips the emptier rule."""
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
            return self._crossover_next_to(named[0], closing, helps)
        if closing:
            open_ = [x["id"] for x in f.crossovers if self._xo_open(x["id"])]
            if open_:
                return open_[0], None
            return None, self._none_open([x["id"] for x in f.crossovers])
        why = None
        for tid in await self._tanks_in_trouble():
            aid, why_not = self._crossover_next_to(tid, helps=helps)
            if aid:
                return aid, None
            why = why or why_not
        return (None, why) if why and helps else (f.crossovers[0]["id"], None)

    async def _plans_line(self, ctx: dict) -> str:
        """The cheapest plan, and the cheapest robust one if that's a different plan.
        Focuses the camera on the cheapest plan's first step."""
        res = await self.run_tool("get_plans", {}, ctx)
        blind = self._blind_line(res.get("unknown_tanks") or [])
        # The planner's "nothing to do" is one plan with no steps; that is not a $0 plan.
        nothing = not res["plans"] or all(not p["actions"] and not p["cannot_save"] for p in res["plans"])
        if nothing:
            if blind:
                return (f"{blind} The planner at {res['as_of']} has no plan to make for the tanks with a "
                        f"current reading: none reaches shutdown in the next 6 hours.")
            return (f"No plan needed: nothing reaches shutdown in the next 6 hours. The planner ran at "
                    f"{res['as_of']} on the live readings.")
        p = res["plans"][0]
        if p["actions"]:
            await self._focus(p["actions"][0]["asset_id"], ctx)

        def usd(x: dict) -> str:
            return f", ${x['total_usd']:,.0f}" if isinstance(x.get("total_usd"), (int, float)) else ""

        line = f"The planner ran at {res['as_of']} on the live readings. Cheapest plan{usd(p)}: {p['summary']}"
        if p["cannot_save"]:
            line += f" It can't save {', '.join(name(x) for x in p['cannot_save'])}."
        robust = next((x for x in res["plans"] if x.get("robust")), None)
        if robust and robust["id"] != p["id"]:
            line += f" Cheapest robust plan{usd(robust)}: {robust['summary']}"
        if blind:
            line = blind + " " + line  # said first: a plan can't vouch for a tank nobody can see
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

        # "Fix it": run the cheapest robust plan, one confirmed step at a time.
        # A negation ("don't fix it") or a question ("should I run the plan?") starts nothing.
        fix = fix_it_request(text)
        if fix == "run":
            res = await self.run_tool("execute_plan", {"plan_id": None}, ctx)
            if "error" in res:
                return f"I can't start a run: {res['error']}"
            if not res.get("run_id"):
                return f"Nothing to run. {res['note']}"
            first = res["steps"][0]
            kind = "cheapest plan that holds" if res.get("robust") else "cheapest plan that keeps the wells running"
            return (f"Started {res['run_id']}, the {kind} (${res['total_usd']:,.0f}): {res['summary']} "
                    f"Step 1 of {len(res['steps'])}, {first['text'][0].lower() + first['text'][1:-1]}, comes to the "
                    f"console at {first['at']}. Every step waits for your confirmation.")

        # What am I looking at.
        if re.search(r"looking at|what is this|what's this|whats this|this (asset|thing|one)|selected|what is that", t):
            ids = [a for a in selected if live.fmap.kind_of(a)][:3]
            if not ids:
                return None  # needs the screenshot, so Claude
            await self._focus(ids[0], ctx)
            return " ".join([await self._describe(a) for a in ids])

        # Show or check an asset ("Why is Tank 3 rising, and what's my cheapest fix?" adds the plans).
        if re.search(r"show|focus|zoom|look|go to|where|\bis\b|how's|status of|check|explain|tell me about|about", t):
            aid = (await self._crossover_in(t, helps=False))[0] if "crossover" in t else self._asset_in(t)
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
            warn = (" It also warns: " + others[0]) if others else ""
            blind = ""
            if unknown:
                blind = (self._blind_line(unknown) + f" Treat {'it' if len(unknown) == 1 else 'them'} "
                         f"as at risk and check in person. ")
            if sd:
                await self._focus(sd["asset_id"], ctx)
                return (blind + self._forecast_says(sd["text"], sd.get("t_min")) + warn).strip()
            if unknown:
                await self._focus(unknown[0], ctx)
                return (blind + self._forecast_says("no tank with a fresh reading reaches shutdown in the next "
                                                    "6 hours if nothing changes.") + warn).strip()
            return self._forecast_says("no tank reaches shutdown in the next 6 hours if nothing changes.") + warn

        # Status.
        if re.search(r"status|what'?s (failing|wrong|happening|going on|broken)|report|sitrep|alarms?|alerts?|problem|update", t):
            return await self._status(ctx)
        if fix == "negated":
            return "Okay, I won't start a run. Say \"fix it\" when you want one."
        return None

    async def _status(self, ctx: dict) -> str:
        """What's failing: every P1 root cause with its clock (at least the most urgent root),
        then 'N related alarms' folded under each, the tanks Jarvis can't see, and a count of
        the rest. Never one row per alarm (CF-7, CF-11)."""
        res = await self.run_tool("list_alerts", {}, ctx)
        if not res["groups"]:
            if res["unknown_tanks"]:  # never "all quiet" while a tank is blind
                await self._focus(res["unknown_tanks"][0], ctx)
                return f"No active alarms at {res['as_of']}. " + self._blind_line(res["unknown_tanks"])
            return f"All quiet at {res['as_of']}. No active alerts."
        groups = [g for g in res["groups"] if not g["root"]["shelved"]]
        shelved = len(res["groups"]) - len(groups)
        lead = [g for g in groups if g["root"]["priority"] == "P1"][:2] or groups[:1]
        if lead:
            await self._focus(lead[0]["root"]["asset_id"], ctx)
        parts = [self._root_line(g) for g in lead]
        said = tuple(g["root"]["asset_id"] for g in lead if g["root"]["kind"] == "SENSOR_STALE")
        blind = self._blind_line(res["unknown_tanks"], skip=said)
        if blind:
            parts.append(blind)
        rest = [g for g in groups if g not in lead and not (g["root"]["kind"] == "SENSOR_STALE"
                                                            and g["root"]["asset_id"] in res["unknown_tanks"])]
        if rest:
            n = sum(1 + g["related"] for g in rest)
            nxt = rest[0]["root"]
            parts.append(f"{n_of(n, 'other alert')} open; the next is {nxt['priority']} on {nxt['name']}.")
        if shelved:
            parts.append(f"{n_of(shelved, 'shelved alert')} not shown.")
        return " ".join(parts)

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
            # Nothing that sounds like a level: the sentence, and how old the last reading is.
            line = self._no_reading(aid)
            info["alerts"] = [a for a in info["alerts"] if a["kind"] != "SENSOR_STALE"]
        elif kind == "tank":
            line = (f"{nm} is at {lv['level_pct']:.0f}%{self._age_note(aid, 'level_pct')}, "
                    f"with shutdown at {info['design']['shutdown_pct']}%")
            obs, exp = lv.get("observed_bbl_hr"), lv.get("expected_bbl_hr")
            # Right after a pump changes state the fit can still lag; a rate whose sign
            # disagrees with what the pumps and wells explain would only mislead.
            if obs is not None and not (exp is not None and abs(exp) >= 1 and obs * exp < 0):
                line += f", changing {obs:+.0f} barrels an hour over its recent readings"
            line += "."
            # Why: the tank's own pumps' active alerts, then its forecast shutdown time.
            causes = [a["message"] for pid in self.live.fmap.pumps_by_tank.get(aid, [])
                      for a in self.live.detector.active_for(pid)]
            fc = await self.run_tool("get_forecast", {"actions": []}, {"ui": [], "proposals": []})
            sd = next((e for e in fc.get("events", [])
                       if e.get("type") == "SHUTDOWN_LEVEL" and e.get("asset_id") == aid), None)
            extra = causes[:1] + ([self._forecast_says(sd["text"], sd.get("t_min"))] if sd else [])
            # Its own high-level alerts only repeat the level and shutdown just said.
            info["alerts"] = [a for a in info["alerts"] if a["kind"] not in ("TANK_HIGH", "TANK_HIGH_HIGH")]
            if extra:
                line += " " + " ".join(extra)
        elif kind == "pump":
            run = "running" if lv.get("running") else "stopped"
            line = f"{nm} is {run}, moving {lv.get('flow_bbl_hr') or 0:.0f} barrels an hour{self._age_note(aid)}."
        elif kind == "valve":
            line = f"{nm} is {str(lv.get('position')).lower()}{self._age_note(aid, 'position')}."
        elif kind == "truck":
            phase = str(lv.get("phase", "IDLE")).lower().replace("_", " ")
            line = f"{nm} is {phase}" + (f" for {name(lv['target_id'])}." if lv.get("target_id") else ".")
        else:
            line = f"{nm} is flowing {lv.get('flow_bbl_hr') or 0:.0f} barrels an hour{self._age_note(aid, 'flow_bbl_hr')}."
        alerts = [a["message"] for a in info["alerts"] if a["message"] not in line]
        return " ".join([line, *alerts[:1]])
