#!/usr/bin/env python3
"""Rehearse scenario/DEMO.md against a running backend, beat by beat (#40).

    python scenario/rehearse.py --api http://127.0.0.1:8000
    python scenario/rehearse.py --api http://127.0.0.1:8000 --fill     # also write scenario/DEMO.filled.md
    python scenario/rehearse.py --api http://127.0.0.1:8000 --json     # machine-readable output
    python scenario/rehearse.py --api http://127.0.0.1:8000 --skip-live

It prints a PASS/FAIL/SKIP row for every beat and every {LIVE: ...} value, so
nobody on stage says a number from memory. A part of the backend that isn't on
the server yet (404) is a SKIP. A scripted Jarvis question that gets a 503, or an
answer without the numbers DEMO.md promises, is a FAIL with the reply quoted.

What it changes on the server, and puts back:
  - live sim: trips PUMP_3 (the demo's own scripted fault, so the field ends up the
    way the demo starts). It opens the Tank 3 to 4 crossover through a confirmed
    proposal, then closes it the same way, and rejects any proposal it left PENDING.
    The live clock can't rewind: restart the backend before going on stage.
  - live sim, flood beat (CF-8): only when /api/alerts carries priority and root_id
    (CF-7), it advances the live clock in 5-minute steps until Tank 3 has shut in,
    so the whole cascade rings, then reads /api/alerts. Without CF-7 it SKIPs and
    changes nothing.
  - blueprints: activates sample 1, then always activates "default" again.

Exit code: 0 no FAIL, 1 at least one FAIL, 2 the backend can't be reached.
Needs only the standard library, httpx, and websockets (for the /ws timing).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

try:
    import websockets
except ImportError:  # the /ws timing row SKIPs without it
    websockets = None

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "scenario" / "DEMO.md"
FILLED = ROOT / "scenario" / "DEMO.filled.md"
SAMPLE1 = ROOT / "scenario" / "samples" / "sample1_pecos_8tank.json"
SAMPLE2 = ROOT / "scenario" / "samples" / "sample2_delaware_4tank.csv"

# The {LIVE: ...} placeholders in DEMO.md, keyed by their exact text.
LIVE_EVENT = "/api/forecast event text"
LIVE_BEFORE_AFTER = "shutdown before and after"
LIVE_ROBUST = "/api/plans robust plan summary and cost"
LIVE_NOTHING = "do-nothing cost"
LIVE_EXPIRY = "/api/lastcall expiry"
LIVE_FLOOD = "alarm flood row count"
LIVE_TWO_NIGHTS = "Two nights end card"

Q_FAILING = "What's failing?"
Q_WHY = "Why is Tank 3 rising, and what's my cheapest fix?"
Q_OPEN = "open the Tank 3 to 4 crossover"

CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\s?(?:AM|PM)\b", re.I)
DOLLAR_RE = re.compile(r"\$\s?\d[\d,]*")
TRIP_ALERT_MAX_S = 5.0  # beat 2: the phone has to buzz while the presenter is still on the line
SAMPLE2_REVIEW_EXPECTED = 14  # scenario/samples/README.md
FLOOD_MIN_ROWS = 8      # CF-8: trip, tank high, high-high, 4 wells shut in, 1 stale sensor
FLOOD_STEP_MIN = 5      # each POST /api/live/sim advance is one detector evaluation
FLOOD_MAX_MIN = 150     # Tank 3 shuts in about 60 live minutes after the trip
FLOOD_SETTLE_STEPS = 2  # evaluations after the shutdown, so the shut-in alarms can confirm


def usd(n: Any) -> str:
    try:
        return f"${float(n):,.0f}"
    except (TypeError, ValueError):
        return str(n)


def nice(asset_id: str) -> str:
    """TANK_3 -> Tank 3. Matches the backend's names for the assets the demo uses."""
    if asset_id.startswith("VALVE_XO_") and len(asset_id) == len("VALVE_XO_") + 2:
        a, b = asset_id[-2], asset_id[-1]
        return f"the Tank {a} to {b} crossover"
    head, _, num = asset_id.rpartition("_")
    return f"{head.replace('_', ' ').title()} {num}" if num.isdigit() else asset_id


def quote(text: Any, n: int = 400) -> str:
    s = text if isinstance(text, str) else json.dumps(text)
    s = " ".join(s.split())
    return '"' + (s if len(s) <= n else s[: n - 3] + "...") + '"'


class Rehearsal:
    def __init__(self, api: str, skip_live: bool = False, timeout: float = 60.0,
                 log: Callable[[str], None] = lambda s: None) -> None:
        self.api = api.rstrip("/")
        self.skip_live = skip_live
        self.http = httpx.Client(base_url=self.api, timeout=timeout)
        self.log = log
        self.results: list[dict] = []
        self.live: dict[str, str] = {}      # DEMO.md placeholder -> value
        self.extra: dict[str, str] = {}     # other numbers worth knowing on stage
        self.changes: list[str] = []        # what this run left changed on the server
        self.notes: list[str] = []
        # Filled by the beats and read by later ones.
        self.field: dict = {}
        self.state: dict = {}
        self.root = "PUMP_3"
        self.tank = "TANK_3"
        self.crossover = "VALVE_XO_34"
        self.base_event: Optional[dict] = None
        self.base_fc: Optional[dict] = None
        self.base_cost: Optional[float] = None
        self.robust_plan: Optional[dict] = None
        self.our_proposals: list[str] = []

    # ---------- plumbing ----------
    def add(self, beat: str, result: str, detail: str) -> None:
        self.results.append({"beat": beat, "result": result, "detail": detail})
        self.log(f"{result:4}  {beat}: {detail}")

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        return self.http.request(method, path, **kw)

    def run(self) -> dict:
        steps = [self.beat_state, self.beat_forecast, self.beat_whatif, self.beat_plans, self.beat_lastcall,
                 self.beat_two_nights, self.beats_live, self.beat_flood, self.beat_ingest, self.beat_blueprints]
        for step in steps:
            try:
                step()
            except httpx.HTTPError as e:
                self.add(step.__name__, "FAIL", f"HTTP error: {type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001 - one broken beat must not hide the others
                self.add(step.__name__, "FAIL", f"rehearse.py crashed: {type(e).__name__}: {e}")
        return self.report()

    def report(self) -> dict:
        missing = [k for k in placeholders(DEMO.read_text(encoding="utf-8")) if k not in self.live] \
            if DEMO.exists() else []
        return {"api": self.api, "results": self.results, "live": self.live, "extra": self.extra,
                "unfilled": missing, "server_changes": self.changes, "notes": self.notes,
                "ok": not any(r["result"] == "FAIL" for r in self.results)}

    # ---------- core beats ----------
    def beat_state(self) -> None:
        name = "Hook: GET /api/state clock and incident"
        r = self.call("GET", "/api/state")
        if r.status_code != 200:
            return self.add(name, "FAIL", f"HTTP {r.status_code}: {quote(r.text)}")
        self.state = st = r.json()
        f = self.call("GET", "/api/field")
        self.field = f.json() if f.status_code == 200 else {}
        inc = st.get("incident") or {}
        self.root = inc.get("root_asset_id") or self.root
        pump = next((p for p in self.field.get("pumps", []) if p.get("id") == self.root), None)
        if pump and pump.get("tank_id"):
            self.tank = pump["tank_id"]
        xo = next((v for v in self.field.get("valves", [])
                   if v.get("kind") == "CROSSOVER" and self.tank in (v.get("connects") or [])), None)
        if xo:
            self.crossover = xo["id"]
        clock = st.get("clock")
        self.extra["state clock"] = str(clock)
        self.extra["incident"] = inc.get("summary", "none")
        if not clock or not inc:
            return self.add(name, "FAIL", f"clock={clock!r}, incident={inc!r}. DEMO.md needs a live incident.")
        root_status = (st.get("assets", {}).get(self.root) or {}).get("status")
        detail = f"clock {clock}; incident: {inc.get('summary')} (root {self.root}, status {root_status})"
        if root_status != "ALARM":
            return self.add(name, "FAIL", detail + f". {self.root} should be ALARM, the only color in the field.")
        self.add(name, "PASS", detail)

    def _shutdown(self, fc: dict, tank: Optional[str] = None) -> Optional[dict]:
        return next((e for e in fc.get("events", []) if e.get("type") == "SHUTDOWN_LEVEL"
                     and (tank is None or e.get("asset_id") == tank)), None)

    def beat_forecast(self) -> None:
        name = "Beat 3 How long: POST /api/forecast"
        r = self.call("POST", "/api/forecast", json={"actions": []})
        if r.status_code != 200:
            return self.add(name, "FAIL", f"HTTP {r.status_code}: {quote(r.text)}")
        fc = self.base_fc = r.json()
        self.base_cost = (fc.get("cost") or {}).get("total_usd")
        if self.base_cost is not None:
            self.live[LIVE_NOTHING] = usd(self.base_cost)
        ev = self._shutdown(fc, self.tank) or self._shutdown(fc)
        if ev is None:
            return self.add(name, "FAIL", f"no SHUTDOWN_LEVEL event; warnings {fc.get('warnings')}")
        self.base_event = ev
        self.tank = ev.get("asset_id", self.tank)
        text = ev.get("text", "")
        self.live[LIVE_EVENT] = text
        self.extra["t_min_low"] = str(ev.get("t_min_low"))
        self.extra["t_min_high"] = str(ev.get("t_min_high"))
        problems = []
        if not CLOCK_RE.search(text):
            problems.append("no clock time in the text")
        if "give or take" not in text and "minutes" not in text:
            problems.append("no range in the text")
        if fc.get("warnings"):
            self.notes.append(f"Forecast warnings: {fc['warnings']}")
        self.add(name, "FAIL" if problems else "PASS",
                 (", ".join(problems) + ": " if problems else "") + quote(text))

    def lead_min(self, valve_id: str) -> float:
        leads = self.field.get("lead_min") or {}
        v = next((v for v in self.field.get("valves", []) if v.get("id") == valve_id), {})
        key = "SET_VALVE_manual" if v.get("manual") else "SET_VALVE_remote"
        try:
            return float(leads.get(key, 20 if v.get("manual") else 1))
        except (TypeError, ValueError):
            return 20.0

    def beat_whatif(self) -> None:
        name = "Beat 5 The fix: forecast with the crossover open"
        if self.base_event is None:
            return self.add(name, "FAIL", "no do-nothing shutdown from beat 3 to compare against")
        lead = self.lead_min(self.crossover)
        act = {"type": "SET_VALVE", "asset_id": self.crossover, "value": "OPEN", "at_min": lead}
        t0 = time.perf_counter()
        r = self.call("POST", "/api/forecast", json={"actions": [act]})
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            return self.add(name, "FAIL", f"HTTP {r.status_code}: {quote(r.text)}")
        fc = r.json()
        after = self._shutdown(fc, self.tank)
        before_clock = clock_of(self.base_event.get("text", ""))
        after_cost = (fc.get("cost") or {}).get("total_usd")
        if after is None:
            after_txt = "no shutdown in the next 6 hours"
        else:
            after_txt = clock_of(after.get("text", "")) or f"+{after.get('t_min')} min"
        value = (f"{nice(self.tank)} shutdown moves from {before_clock} to {after_txt}, "
                 f"and the bill from {usd(self.base_cost)} to {usd(after_cost)}")
        self.live[LIVE_BEFORE_AFTER] = value
        self.extra["what-if forecast ms"] = f"{ms:.0f}"
        later = after is None or float(after.get("t_min", 0)) > float(self.base_event.get("t_min", 0))
        detail = f"{value} (opened at +{lead:g} min lead time; {ms:.0f} ms)"
        if ms > 1000:
            detail += ". Slower than 1 s, so it won't feel instant on a click"
        self.add(name, "PASS" if later and ms <= 1000 else "FAIL",
                 detail if later else detail + ". Opening the crossover didn't move the shutdown later")

    def beat_plans(self) -> None:
        name = "Beat 6 The plan: POST /api/plans"
        r = self.call("POST", "/api/plans", json={})
        if r.status_code != 200:
            return self.add(name, "FAIL", f"HTTP {r.status_code}: {quote(r.text)}")
        plans = r.json().get("plans") or []
        if not plans:
            return self.add(name, "FAIL", "no plans; the contract says it always returns one")
        robust = sorted((p for p in plans if p.get("robust")),
                        key=lambda p: (p.get("cost") or {}).get("total_usd", 1e12))
        cheapest = plans[0]
        self.extra["cheapest plan"] = (f"{first_sentence(cheapest.get('summary', ''))} "
                                       f"{usd((cheapest.get('cost') or {}).get('total_usd'))}"
                                       f"{'' if cheapest.get('robust') else ' (fails at +15% inflow)'}")
        if not robust:
            return self.add(name, "FAIL", f"no plan is robust at +15% inflow; cheapest: {quote(cheapest.get('summary'))}")
        p = self.robust_plan = robust[0]
        value = f"{first_sentence(p.get('summary', '')).rstrip('.')}, for {usd((p.get('cost') or {}).get('total_usd'))}"
        self.live[LIVE_ROBUST] = value
        self.add(name, "PASS", f"robust: {value}; do nothing: {self.live.get(LIVE_NOTHING, '?')}; "
                               f"{len(plans)} plans, cheapest {self.extra['cheapest plan']}")

    def beat_lastcall(self) -> None:
        name = "Beat 6 Last Call: POST /api/lastcall"
        # top_n 20: Last Call's default 3 options are the cheapest, which may not include the robust plan.
        r = self.call("POST", "/api/lastcall", json={"top_n": 20})
        if r.status_code == 404:
            why = ""
            h = self.call("GET", "/api/health")
            if h.status_code == 200:
                why = f" (/api/health lastcall: {h.json().get('lastcall')})"
            return self.add(name, "FAIL", "not mounted on this server" + why +
                            ". DEMO.md beat 6 says the expiry out loud; this backend lacks lastcall/ (#10).")
        if r.status_code != 200:
            return self.add(name, "FAIL", f"HTTP {r.status_code}: {quote(r.text)}")
        lc = r.json()
        opts = lc.get("options") or []
        def key(acts: list) -> list:  # Last Call's steps carry type and asset_id, not target_id
            return sorted((a.get("type"), a.get("asset_id")) for a in acts)

        want = key((self.robust_plan or {}).get("actions", []))
        plan_cost = ((self.robust_plan or {}).get("cost") or {}).get("total_usd")
        same = [o for o in opts if want and key(o.get("actions") or o.get("steps") or []) == want]
        # Steps don't carry a choke's value (half vs shut in), so the price picks between look-alikes.
        match = next((o for o in same if (o.get("cost") or {}).get("total_usd") == plan_cost),
                     same[0] if same else None)
        pnr = (lc.get("point_of_no_return") or {}).get("clock")
        self.extra["point of no return"] = str(pnr)
        self.extra["Last Call headline"] = lc.get("headline", "")
        if match is None or not match.get("expires_clock"):
            robust = next((o for o in opts if o.get("robust")), None)
            return self.add(name, "FAIL",
                            f"none of Last Call's {len(opts)} options is the /api/plans robust plan, so DEMO.md's "
                            f"\"this plan expires at\" has no number. Headline {quote(lc.get('headline'))}"
                            + (f"; its first robust option is {quote(robust.get('summary'))}" if robust else ""))
        value = match["expires_clock"]
        if match.get("expires_clock_hot"):
            value += f" ({match['expires_clock_hot']} if inflow runs 15% high)"
        self.live[LIVE_EXPIRY] = value
        lc_cost = (match.get("cost") or {}).get("total_usd")
        detail = f"the robust plan expires at {value}; point of no return {pnr}"
        if lc_cost != plan_cost:
            return self.add(name, "FAIL", detail + f"; but Last Call prices it {usd(lc_cost)} and /api/plans {usd(plan_cost)}")
        self.add(name, "PASS", detail)

    def beat_two_nights(self) -> None:
        """The close's end card, counted the way web/src/twonights.js (PR #70) counts it:
        the do-nothing forecast against the forecast with the plan beat 6 approves (the
        robust one), as "$X and N wells apart"."""
        name = "Close Two nights: end card from both forecasts"
        if self.base_fc is None or self.robust_plan is None:
            return self.add(name, "FAIL", "needs the do-nothing forecast (beat 3) and the robust plan (beat 6)")
        r = self.call("POST", "/api/forecast", json={"actions": self.robust_plan.get("actions") or []})
        if r.status_code != 200:
            return self.add(name, "FAIL", f"HTTP {r.status_code}: {quote(r.text)}")
        none_usd = (self.base_fc.get("cost") or {}).get("total_usd")
        with_usd = (r.json().get("cost") or {}).get("total_usd")
        if none_usd is None or with_usd is None:
            return self.add(name, "FAIL", f"a forecast has no cost.total_usd (nobody acts: {none_usd}, "
                                          f"with the plan: {with_usd})")
        lost = wells_lost(self.base_fc, len(self.field.get("wells") or []))
        card = usd(max(0.0, float(none_usd) - float(with_usd)))
        if lost:
            card += f" and {lost} well{'' if lost == 1 else 's'}"
        self.live[LIVE_TWO_NIGHTS] = card + " apart"
        self.add(name, "PASS", f"{self.live[LIVE_TWO_NIGHTS]}: nobody acts {usd(none_usd)}, "
                               f"with the robust plan {usd(with_usd)}")

    # ---------- the alarm flood (CF-8) ----------
    def beat_flood(self) -> None:
        """Beat 2b. A normal list shows every alarm the trip sets off. Freeboard has to show
        exactly one P1 root cause, with every other alarm pointing at it by root_id (directly
        or through another alarm). A stale sensor is its own fault, not a symptom, so it may
        stand alone, but never as a P1."""
        name = "Beat 2b The flood: GET /api/alerts, one P1 root cause"
        if self.skip_live:
            return self.add(name, "SKIP", "--skip-live")
        r = self.call("GET", "/api/alerts")
        if r.status_code == 404:
            return self.add(name, "SKIP", "GET /api/alerts is 404: #38 isn't on this server")
        if r.status_code != 200:
            return self.add(name, "FAIL", f"GET /api/alerts HTTP {r.status_code}: {quote(r.text)}")
        alerts = r.json().get("alerts") or []
        if not any("priority" in a and "root_id" in a for a in alerts):
            self.notes.append("DEMO.md beat 2b (the flood) needs priority and root_id on /api/alerts (CF-7). "
                              "Until CF-7 is on this server, cut that beat and give its 15 s to beat 3.")
            why = "no alerts to read" if not alerts else "the alerts have no priority or root_id"
            return self.add(name, "SKIP", f"{why}, so CF-7 isn't on this server and there is no grouping "
                                          "to check. The live clock was not advanced.")
        before = next((a for a in alerts if a.get("active") and a.get("asset_id") == self.root
                       and a.get("priority") == "P1" and not a.get("root_id")), None)
        if before:
            self.extra["P1 before the flood"] = (f"{quote(before.get('message', ''), 120)}, "
                                                 f"{before.get('minutes_to_consequence')} min to consequence, "
                                                 f"{usd(before.get('usd_at_risk'))} at risk")
        advanced, clock, reached = self._advance_to_shutdown()
        if advanced is None:
            return self.add(name, "FAIL", clock)

        alerts = self.call("GET", "/api/alerts").json().get("alerts") or []
        rows = [a for a in alerts if a.get("active")]
        self.live[LIVE_FLOOD] = str(len(rows))
        kinds: dict[str, int] = {}
        for a in rows:
            kinds[str(a.get("kind"))] = kinds.get(str(a.get("kind")), 0) + 1
        kinds_txt = ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items())) or "none"
        self.extra["flood alarms by kind"] = kinds_txt
        by_id = {a.get("id"): a for a in rows}

        def top(a: dict) -> Optional[str]:
            """The id at the top of a's root_id chain, or None if the chain breaks."""
            seen: set = set()
            while a.get("root_id"):
                if a.get("id") in seen or a["root_id"] not in by_id:
                    return None
                seen.add(a.get("id"))
                a = by_id[a["root_id"]]
            return a.get("id")

        def label(a: dict) -> str:
            return f"{a.get('id')} {a.get('asset_id')} {a.get('kind')} {a.get('priority')}"

        problems = []
        if len(rows) < FLOOD_MIN_ROWS:
            problems.append(f"only {len(rows)} active alarms; the beat needs {FLOOD_MIN_ROWS} or more: the trip, "
                            f"{nice(self.tank)} high and high-high, its wells shutting in, and one stale sensor")
        p1_roots = [a for a in rows if a.get("priority") == "P1" and not a.get("root_id")]
        if len(p1_roots) != 1:
            problems.append(f"{len(p1_roots)} P1 root causes, expected exactly one"
                            + (": " + ", ".join(label(a) for a in p1_roots) if p1_roots else ""))
        root = p1_roots[0] if len(p1_roots) == 1 else None
        alone, stray = [], []
        if root:
            if root.get("asset_id") != self.root:
                problems.append(f"the P1 root is {label(root)}, not the {self.root} trip")
            for a in rows:
                if a is root or top(a) == root.get("id"):
                    continue
                if a.get("kind") == "SENSOR_STALE" and not a.get("root_id") and a.get("priority") != "P1":
                    alone.append(a)
                else:
                    stray.append(a)
            if stray:
                problems.append(f"{len(stray)} alarms don't point at {root.get('id')} by root_id: "
                                + ", ".join(f"{label(a)} root_id={a.get('root_id')!r}" for a in stray))
        detail = f"{len(rows)} active alarms after {advanced} live min ({clock}): {kinds_txt}. "
        if root:
            under = len(rows) - 1 - len(alone) - len(stray)
            detail += f"P1 root {label(root)}, {under} grouped under it"
            if alone:
                detail += f"; stale sensor on its own: {', '.join(str(a.get('asset_id')) for a in alone)}"
        if not reached:
            detail += f". {nice(self.tank)} never reached its shutdown level in {advanced} live min"
        if problems:
            return self.add(name, "FAIL", "; ".join(problems) + ". " + detail)
        self.add(name, "PASS", detail)

    def _advance_to_shutdown(self) -> tuple[Optional[int], str, bool]:
        """Advance the live clock in FLOOD_STEP_MIN steps until the tank reads its shutdown
        level, then FLOOD_SETTLE_STEPS more. Returns (minutes, clock, reached), or
        (None, why, False) when the sim control fails."""
        shut = next((t.get("shutdown_pct") for t in self.field.get("tanks", []) if t.get("id") == self.tank), None)
        advanced, settle, clock = 0, None, "?"
        while advanced < FLOOD_MAX_MIN and settle != 0:
            s = self.call("POST", "/api/live/sim", json={"advance_min": FLOOD_STEP_MIN})
            if s.status_code != 200:
                return None, f"POST /api/live/sim advance_min HTTP {s.status_code}: {quote(s.text)}", False
            advanced += FLOOD_STEP_MIN
            clock = s.json().get("clock", clock)
            if settle is not None:
                settle -= 1
                continue
            level = ((self.call("GET", "/api/live/state").json().get("assets") or {}).get(self.tank) or {}) \
                .get("level_pct")
            if shut is not None and level is not None and float(level) >= float(shut) - 0.05:
                settle = FLOOD_SETTLE_STEPS
        self.changes.append(f"Advanced the live clock {advanced} min to {clock} (POST /api/live/sim advance_min) "
                            f"so {nice(self.tank)} shut in and the whole flood rang. Restart the backend before the demo.")
        return advanced, clock, settle is not None

    # ---------- live layer (#38) ----------
    def beats_live(self) -> None:
        if self.skip_live:
            return self.add("Live layer (beats 2, 4, 6)", "SKIP", "--skip-live")
        r = self.call("GET", "/api/live/state")
        if r.status_code == 404:
            self.add("Live layer (beats 2, 4, 6)", "SKIP", "GET /api/live/state is 404: #38 isn't on this server")
            return
        if r.status_code != 200:
            self.add("Live layer (beats 2, 4, 6)", "FAIL", f"GET /api/live/state HTTP {r.status_code}: {quote(r.text)}")
            return
        ls = r.json()
        self.extra["live clock"] = str(ls.get("clock"))
        if ls.get("paused"):
            self.notes.append("The live sim was paused, so this run unpaused it (POST /api/live/sim {\"paused\": false}).")
            self.call("POST", "/api/live/sim", json={"paused": False})
            self.changes.append("Unpaused the live sim.")
        alerts = self.call("GET", "/api/alerts").json().get("alerts", [])
        already = any(a.get("asset_id") == self.root and a.get("kind") == "PUMP_TRIP" and a.get("active")
                      for a in alerts)
        if already:
            # Ask before the trip timing, which repairs the pump for a while and so moves the live numbers.
            self.beat_jarvis_failing()
            self.beat_jarvis_why()
            self.beat_trip_alert()
        else:
            self.beat_trip_alert()
            self.beat_jarvis_failing()
            self.beat_jarvis_why()
        self.beat_jarvis_open()
        self.cleanup_live()

    def beat_trip_alert(self) -> None:
        name = f"Beat 2 What's failing: trip {self.root}, PUMP_TRIP over /ws"
        if websockets is None:
            return self.add(name, "SKIP", "pip install websockets to time /ws")
        try:
            secs, detail = asyncio.run(self._time_trip())
        except Exception as e:  # noqa: BLE001
            return self.add(name, "FAIL", f"/ws: {type(e).__name__}: {e}")
        self.changes.append(f"Tripped {self.root} in the live sim (the demo's own fault). "
                            f"Undo: POST /api/live/faults {{\"asset_id\": \"{self.root}\", \"type\": \"REPAIR\"}}, "
                            "or restart the backend.")
        if secs is None:
            return self.add(name, "FAIL", detail)
        self.extra["trip to alert seconds"] = f"{secs:.1f}"
        ok = secs <= TRIP_ALERT_MAX_S
        self.add(name, "PASS" if ok else "FAIL",
                 f"{secs:.1f} s from the trip to the alert{'' if ok else f' (over {TRIP_ALERT_MAX_S:g} s)'}; {detail}")

    async def _time_trip(self) -> tuple[Optional[float], str]:
        ws_url = re.sub(r"^http", "ws", self.api) + "/ws?role=other&name=rehearse"
        pump = self.root
        async with httpx.AsyncClient(base_url=self.api, timeout=30) as http, \
                websockets.connect(ws_url, open_timeout=10, max_size=None) as ws:
            async def next_msg(deadline: float) -> Optional[dict]:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                try:
                    return json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
                except asyncio.TimeoutError:
                    return None

            def trip_active(alerts: list) -> bool:
                return any(a.get("asset_id") == pump and a.get("kind") == "PUMP_TRIP" and a.get("active")
                           for a in alerts)

            async def trip() -> httpx.Response:
                return await http.post("/api/live/faults", json={
                    "asset_id": pump, "type": "TRIP", "reason": "Rehearsal: tripped by scenario/rehearse.py"})

            note, tripped = "", False
            try:
                alerts = (await http.get("/api/alerts")).json().get("alerts", [])
                if trip_active(alerts):
                    # Already tripped (the scripted fault). Repair it and let the alert clear, so the
                    # timing below is a fresh trip. The tank-rise check looks back RATE_WINDOW_MIN (10)
                    # live minutes, so advance the clock past it instead of waiting ~40 real seconds.
                    r = await http.post("/api/live/faults", json={"asset_id": pump, "type": "REPAIR"})
                    if r.status_code != 200:
                        tripped = True  # nothing changed
                        return None, f"REPAIR {pump} got HTTP {r.status_code}: {quote(r.text)}"
                    await http.post("/api/live/sim", json={"advance_min": 12})
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        if not trip_active((await http.get("/api/alerts")).json().get("alerts", [])):
                            break
                        await next_msg(min(deadline, time.monotonic() + 1))
                    if trip_active((await http.get("/api/alerts")).json().get("alerts", [])):
                        return None, (f"{pump} was already tripped, and its PUMP_TRIP alert stayed active 30 s "
                                      "after a REPAIR and a 12-minute advance, so a fresh trip can't be timed")
                    note = f"{pump} was already tripped; repaired it and advanced the live clock 12 min first. "
                    self.changes.append("Advanced the live clock 12 minutes (POST /api/live/sim advance_min) "
                                        "so the repaired pump's alert could clear.")
                # Drop anything queued so the timer only sees messages after the trip.
                while True:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=0.05)
                    except asyncio.TimeoutError:
                        break
                t0 = time.monotonic()
                r = await trip()
                tripped = r.status_code == 200
                if not tripped:
                    return None, f"POST /api/live/faults TRIP got HTTP {r.status_code}: {quote(r.text)}"
                deadline = t0 + 30
                while True:
                    msg = await next_msg(deadline)
                    if msg is None:
                        return None, note + f"no PUMP_TRIP alert for {pump} on /ws within 30 s"
                    a = msg.get("alert") or {}
                    if msg.get("type") == "alert" and a.get("asset_id") == pump and a.get("kind") == "PUMP_TRIP" \
                            and a.get("active"):
                        return time.monotonic() - t0, note + quote(a.get("message", ""))
            finally:
                if not tripped:  # never leave the demo's failed pump repaired
                    await trip()

    def ask(self, message: str) -> tuple[int, dict, str]:
        r = self.call("POST", "/api/jarvis", json={"message": message, "selected_asset_ids": []})
        try:
            body = r.json()
        except ValueError:
            body = {}
        for p in body.get("proposals") or []:
            if p.get("status") == "PENDING":
                self.our_proposals.append(p["id"])
        return r.status_code, body, r.text

    def _jarvis_fail(self, name: str, message: str, code: int, body: dict, raw: str) -> bool:
        if code == 404:
            self.add(name, "SKIP", "POST /api/jarvis is 404")
            return True
        if code != 200:
            self.add(name, "FAIL", f"HTTP {code} with no API key: {quote(body.get('detail') or raw)}")
            return True
        return False

    def beat_jarvis_failing(self) -> None:
        name = f"Beat 2 Jarvis: \"{Q_FAILING}\""
        code, body, raw = self.ask(Q_FAILING)
        if self._jarvis_fail(name, Q_FAILING, code, body, raw):
            return
        reply = body.get("reply", "")
        want = nice(self.root)
        ok = want.lower() in reply.lower()
        self.add(name, "PASS" if ok else "FAIL",
                 (f"[{body.get('source')}] " if ok else f"doesn't name {want}: ") + quote(reply))

    def beat_jarvis_why(self) -> None:
        name = f"Beat 4 Jarvis: \"{Q_WHY}\""
        code, body, raw = self.ask(Q_WHY)
        if self._jarvis_fail(name, Q_WHY, code, body, raw):
            return
        reply = body.get("reply", "")
        missing = []
        if not CLOCK_RE.search(reply):
            missing.append("the shutdown time")
        if not DOLLAR_RE.search(reply):
            missing.append("the plan cost")
        detail = f"[{body.get('source')}] " + quote(reply)
        if missing:
            return self.add(name, "FAIL", "DEMO.md says it answers with the shutdown time and plan cost; "
                                          f"missing {' and '.join(missing)}: " + quote(reply))
        self.add(name, "PASS", detail)

    def beat_jarvis_open(self) -> None:
        name = f"Beat 6 Jarvis: \"{Q_OPEN}\" -> PENDING proposal"
        code, body, raw = self.ask(Q_OPEN)
        prop = None
        if not self._jarvis_fail(name, Q_OPEN, code, body, raw):
            prop = next((p for p in body.get("proposals") or []
                         if (p.get("action") or {}).get("asset_id") == self.crossover
                         and (p.get("action") or {}).get("value") == "OPEN"), None)
            if prop is None or prop.get("status") != "PENDING":
                self.add(name, "FAIL", f"no PENDING SET_VALVE {self.crossover} OPEN proposal; "
                                       f"reply {quote(body.get('reply', ''))}, proposals {quote(body.get('proposals'))}")
                prop = None
            else:
                self.add(name, "PASS", f"{prop['id']} PENDING: {quote(prop.get('text', ''))}; reply {quote(body.get('reply', ''))}")
        via = "Jarvis's proposal"
        if prop is None:
            # Still prove the confirm path, with the console's own proposal.
            r = self.call("POST", "/api/actions", json={"type": "SET_VALVE", "asset_id": self.crossover,
                                                        "value": "OPEN", "reason": "Rehearsal (scenario/rehearse.py)"})
            if r.status_code != 200:
                return self.add("Beat 6 Confirm -> crossover OPEN", "FAIL",
                                f"POST /api/actions HTTP {r.status_code}: {quote(r.text)}")
            prop = r.json()
            self.our_proposals.append(prop["id"])
            via = "a console proposal (POST /api/actions), since Jarvis made none"
        self.confirm_and_check(prop["id"], "OPEN", f"Beat 6 Confirm -> {self.crossover} OPEN in /api/live/state", via)

    def confirm_and_check(self, pid: str, want: str, name: Optional[str], via: str = "") -> bool:
        before = (self.call("GET", "/api/live/state").json().get("assets", {}).get(self.crossover) or {}).get("position")
        if before == want and name:
            self.notes.append(f"{self.crossover} already read {want} before the confirm.")
        r = self.call("POST", f"/api/actions/{pid}/confirm", json={"by": "rehearse.py"})
        if pid in self.our_proposals:
            self.our_proposals.remove(pid)
        if r.status_code != 200 or r.json().get("status") != "CONFIRMED":
            if name:
                self.add(name, "FAIL", f"confirm {pid}: HTTP {r.status_code} {quote(r.text)}")
            return False
        pos, deadline = None, time.monotonic() + 10
        while time.monotonic() < deadline:
            pos = (self.call("GET", "/api/live/state").json().get("assets", {}).get(self.crossover) or {}).get("position")
            if pos == want:
                break
            time.sleep(0.5)
        if name:
            self.changes.append(f"Opened {self.crossover} in the live sim through a confirmed proposal.")
            self.add(name, "PASS" if pos == want else "FAIL",
                     f"via {via}: {self.crossover} reads {pos}" + ("" if pos == want else f", expected {want} within 10 s"))
        return pos == want

    def cleanup_live(self) -> None:
        for pid in list(self.our_proposals):
            self.call("POST", f"/api/actions/{pid}/reject", json={"by": "rehearse.py cleanup"})
            self.notes.append(f"Rejected leftover proposal {pid}.")
        self.our_proposals.clear()
        pos = (self.call("GET", "/api/live/state").json().get("assets", {}).get(self.crossover) or {}).get("position")
        if pos == "OPEN":
            r = self.call("POST", "/api/actions", json={"type": "SET_VALVE", "asset_id": self.crossover,
                                                        "value": "CLOSED", "reason": "Rehearsal cleanup"})
            if r.status_code == 200 and self.confirm_and_check(r.json()["id"], "CLOSED", None):
                self.changes.append(f"Closed {self.crossover} again (confirmed proposal), as the demo starts.")
            else:
                self.changes.append(f"Could NOT close {self.crossover} again. Restart the backend before the demo.")
        self.changes.append("The live clock kept running during the rehearsal and can't rewind. Pause it with "
                            "POST /api/live/sim {\"paused\": true}; restart the backend for a fresh 1:44 AM.")

    # ---------- ingest and blueprints ----------
    def beat_ingest(self) -> None:
        name = "Beat 1 Upload: POST /api/ingest sample 2 (CSV)"
        if not SAMPLE2.exists():
            return self.add(name, "SKIP", f"{SAMPLE2.relative_to(ROOT)} is missing")
        with SAMPLE2.open("rb") as fh:
            r = self.call("POST", "/api/ingest", files={"file": (SAMPLE2.name, fh, "text/csv")})
        if r.status_code in (404, 405):
            return self.add(name, "SKIP", f"HTTP {r.status_code}: ingest isn't mounted on this server")
        if r.status_code != 200:
            return self.add(name, "FAIL", f"HTTP {r.status_code}: {quote(r.text)}")
        out = r.json()
        review = out.get("review") or []
        items = sum(int(v) for v in (out.get("counts") or {}).values() if isinstance(v, (int, float)))
        self.extra["sample 2 review items"] = str(len(review))
        self.extra["sample 2 items parsed"] = str(items)
        detail = f"{items} items parsed, {len(review)} review items, read_by {(out.get('source') or {}).get('read_by')}"
        if len(review) != SAMPLE2_REVIEW_EXPECTED:
            detail += f" (samples/README.md says {SAMPLE2_REVIEW_EXPECTED})"
        if not out.get("ok") or out.get("errors"):
            return self.add(name, "FAIL", f"ok={out.get('ok')}, errors {out.get('errors')}; {detail}")
        self.add(name, "PASS", detail)

    def beat_blueprints(self) -> None:
        name = "Beat 1 Build: POST /api/blueprints sample 1, activate"
        r = self.call("GET", "/api/blueprints")
        if r.status_code in (404, 405):
            return self.add(name, "SKIP", f"HTTP {r.status_code}: #41 isn't on this server")
        if r.status_code == 200 and r.json().get("active") not in (None, "default"):
            self.notes.append(f"Blueprint {r.json().get('active')} was active before the run; "
                              "the core beats above ran on it, not on Avon.")
        bp = json.loads(SAMPLE1.read_text(encoding="utf-8"))
        try:
            r = self.call("POST", "/api/blueprints", json={"blueprint": bp, "activate": True})
            if r.status_code == 404:
                return self.add(name, "SKIP", "POST /api/blueprints is 404")
            rec = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            self.changes.append("Activated sample 1 (Pecos Ridge) through /api/blueprints.")
            if r.status_code != 200:
                return self.add(name, "FAIL", f"HTTP {r.status_code}: {quote(rec.get('detail') or r.text)}")
            status = rec.get("status")
            self.extra["sample 1 blueprint status"] = f"{status} ({rec.get('note', '')})"
            fc = self.call("POST", "/api/forecast", json={"actions": []}).json()
            ev = self._shutdown(fc, "TANK_6")
            plans = self.call("POST", "/api/plans", json={}).json().get("plans") or []
            robust = sorted((p for p in plans if p.get("robust")),
                            key=lambda p: (p.get("cost") or {}).get("total_usd", 1e12))
            robust_txt = (f"; robust plan {usd((robust[0].get('cost') or {}).get('total_usd'))}: "
                          f"{first_sentence(robust[0].get('summary', ''))}") if robust else "; no robust plan"
            if ev is None:
                self.add(name, "FAIL", f"status {status}, but the forecast has no TANK_6 shutdown: "
                                       f"{quote([e.get('text') for e in fc.get('events', [])])}")
            else:
                self.extra["sample 1 shutdown"] = ev.get("text", "")
                self.add(name, "PASS" if status in ("ready", "fallback", "building") else "FAIL",
                         f"status {status}, glb {rec.get('glb_url')}; forecast {quote(ev.get('text'))}{robust_txt}")
            self._live_follows(bp)
        finally:
            back = self.call("POST", "/api/blueprints/default/activate")
            tanks = len((self.call("GET", "/api/field").json() or {}).get("tanks", []))
            if back.status_code == 200:
                self.changes.append(f"Activated \"default\" again (field has {tanks} tanks).")
            else:
                self.changes.append(f"Could NOT re-activate \"default\" (HTTP {back.status_code}). Restart the backend.")
                self.add("Restore default blueprint", "FAIL", f"HTTP {back.status_code}: {quote(back.text)}")

    def _live_follows(self, bp: dict) -> None:
        name = "Beat 1 to 2: the live layer follows the uploaded field"
        if self.skip_live:
            return self.add(name, "SKIP", "--skip-live")
        r = self.call("GET", "/api/live/state")
        if r.status_code == 404:
            return self.add(name, "SKIP", "no live layer on this server")
        assets = r.json().get("assets", {})
        new_ids = sorted({t["id"] for t in bp.get("tanks", [])} - {t["id"] for t in self.field.get("tanks", [])})
        if not new_ids:
            return self.add(name, "SKIP", "sample 1 has no tank IDs the default field lacks")
        seen = [i for i in new_ids if i in assets]
        self.add(name, "PASS" if seen else "FAIL",
                 f"/api/live/state has {seen}" if seen else
                 f"/api/live/state still shows the old field (no {', '.join(new_ids)}) after activating sample 1, "
                 "so alerts, /ws, and Jarvis would describe Avon while the console shows Pecos")


# ---------- helpers ----------

def wells_lost(fc: dict, n_wells: int) -> int:
    """Wells offline at the end of a forecast: the timeline's first wells_online minus its
    last, as web/src/twonights.js counts it, else the wells_lost on its shutdown events."""
    tl = [r for r in fc.get("timeline") or [] if r.get("wells_online") is not None]
    if tl:
        return max(0, int(tl[0]["wells_online"]) - int(tl[-1]["wells_online"]))
    return len({w for e in fc.get("events") or [] for w in e.get("wells_lost") or []})


def first_sentence(s: str) -> str:
    m = re.match(r"(.+?\.)(\s|$)", s.strip())
    return m.group(1) if m else s.strip()


def clock_of(text: str) -> str:
    m = CLOCK_RE.search(text or "")
    return m.group(0) if m else "?"


PLACEHOLDER_RE = re.compile(r"\{LIVE:\s*([^}]*?[^}.\s][^}]*?)\s*\}")  # not the "{LIVE: ...}" in the prose


def placeholders(text: str) -> list[str]:
    return list(dict.fromkeys(PLACEHOLDER_RE.findall(text)))


def fill(text: str, live: dict[str, str]) -> str:
    """Replace each {LIVE: key} that has a value. Unfilled ones stay, so they still stand out."""
    return PLACEHOLDER_RE.sub(lambda m: live.get(m.group(1), m.group(0)), text)


def table(results: list[dict]) -> str:
    w = max([len(r["beat"]) for r in results] + [4])
    lines = [f"{'BEAT'.ljust(w)}  RESULT  DETAIL", f"{'-' * w}  ------  ------"]
    lines += [f"{r['beat'].ljust(w)}  {r['result'].ljust(6)}  {r['detail']}" for r in results]
    return "\n".join(lines)


def render(out: dict) -> str:
    parts = [f"Rehearsal against {out['api']}", "", table(out["results"]), "", "LIVE values for scenario/DEMO.md:"]
    keys = placeholders(DEMO.read_text(encoding="utf-8")) if DEMO.exists() else list(out["live"])
    for k in keys:
        parts.append(f"  {{LIVE: {k}}} = {out['live'].get(k, '(not available: see the FAIL/SKIP rows)')}")
    if out["extra"]:
        parts += ["", "Other numbers from this run:"] + [f"  {k}: {v}" for k, v in out["extra"].items()]
    if out["notes"]:
        parts += ["", "Notes:"] + [f"  - {n}" for n in out["notes"]]
    if out["server_changes"]:
        parts += ["", "Server state this run changed (and how to undo it):"] + [f"  - {c}" for c in out["server_changes"]]
    n = {k: sum(r["result"] == k for r in out["results"]) for k in ("PASS", "FAIL", "SKIP")}
    parts += ["", f"{n['PASS']} PASS, {n['FAIL']} FAIL, {n['SKIP']} SKIP"]
    return "\n".join(parts)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Run every beat of scenario/DEMO.md against a backend.")
    ap.add_argument("--api", default="http://127.0.0.1:8000", help="backend base URL")
    ap.add_argument("--skip-live", action="store_true", help="skip the live layer (no faults, no Jarvis)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the table")
    ap.add_argument("--fill", action="store_true", help=f"write {FILLED.relative_to(ROOT)} with every LIVE value filled")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds per HTTP call")
    args = ap.parse_args(argv)

    try:
        httpx.get(args.api.rstrip("/") + "/api/state", timeout=10)
    except httpx.HTTPError as e:
        msg = f"Can't reach the backend at {args.api}: {e}. Start it with: uvicorn backend.app.main:app --port 8000"
        print(json.dumps({"ok": False, "error": msg}) if args.json else msg, file=sys.stdout if args.json else sys.stderr)
        return 2

    reh = Rehearsal(args.api, skip_live=args.skip_live, timeout=args.timeout,
                    log=(lambda s: None) if args.json else (lambda s: print(s, file=sys.stderr, flush=True)))
    out = reh.run()
    if args.fill and DEMO.exists():
        FILLED.write_text(fill(DEMO.read_text(encoding="utf-8"), out["live"]), encoding="utf-8")
        out["filled"] = str(FILLED.relative_to(ROOT))
        out["notes"].append(f"Wrote {out['filled']}." + (f" Still unfilled: {out['unfilled']}." if out["unfilled"] else ""))
    print(json.dumps(out, indent=2) if args.json else render(out))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
