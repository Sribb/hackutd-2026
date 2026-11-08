#!/usr/bin/env python3
"""M3 end to end at the API level: pump trip -> alert -> Jarvis -> a person confirms -> the sim reflects it.

Run it against a backend that is already up (the live layer, PR #57; plan runs, PR #75):

  python3 tools/iot_e2e.py                                  # http://127.0.0.1:8000
  python3 tools/iot_e2e.py --api http://127.0.0.1:8125 --speed 2
  python3 tools/iot_e2e.py --expect-unavailable CONN_PLC    # then stop the fake field (#62) within --wait s

It prints PASS, FAIL or SKIP per step with timings, and exits 1 on any FAIL.

It changes the live sim, because that is the test: it trips a pump, stales a tank sensor, opens the
crossover and runs a plan, confirming every step as "phone" or "iot_e2e". Use a test backend, not the
one on stage. `--speed` sets live minutes per real second for the run and puts the old speed back.
Standard library only. The /ws checks need `websockets` (it's in backend/requirements.txt), so run it
with the backend's venv; without it they SKIP.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request

results: list[tuple[str, str, str, float]] = []  # (PASS|FAIL|SKIP, name, detail, seconds)


class Fail(Exception):
    pass


class Skip(Exception):
    pass


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body=None, ok=(200,)):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, OSError) as e:
            raise Fail(f"{method} {path}: can't reach {self.base} ({e})")
        try:
            out = json.loads(raw or b"null")
        except ValueError:
            out = raw.decode(errors="replace")
        if status not in ok:
            raise Fail(f"{method} {path} returned {status}: {str(out)[:200]}")
        return out

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None, ok=(200,)):
        return self.call("POST", path, {} if body is None else body, ok)

    def state(self) -> dict:
        return self.get("/api/live/state")

    def alerts(self) -> list:
        return self.get("/api/alerts")["alerts"]


class Ws:
    """Collects every /ws message in a thread. Disabled (ok False) without the websockets package."""

    def __init__(self, base: str) -> None:
        self.msgs: list[tuple[float, dict]] = []
        self.ok, self.why = False, ""
        try:
            from websockets.sync.client import connect
        except ImportError:
            self.why = "the websockets package isn't installed (use the backend's venv)"
            return
        url = base.replace("http", "ws", 1).rstrip("/") + "/ws?role=other&name=iot_e2e"
        try:
            self.conn = connect(url, open_timeout=5)
        except Exception as e:  # noqa: BLE001
            self.why = f"can't open {url}: {e}"
            return
        self.ok = True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for raw in self.conn:
                try:
                    self.msgs.append((time.monotonic(), json.loads(raw)))
                except ValueError:
                    pass
        except Exception:  # noqa: BLE001
            self.ok = False

    def wait(self, pred, since: float, timeout: float):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for t, m in list(self.msgs):
                if t >= since and pred(m):
                    return t, m
            time.sleep(0.05)
        return None

    def close(self) -> None:
        if self.ok:
            self.conn.close()


def step(name: str, fn) -> None:
    t0 = time.monotonic()
    try:
        detail = fn() or ""
        kind = "PASS"
    except Skip as e:
        kind, detail = "SKIP", str(e)
    except Fail as e:
        kind, detail = "FAIL", str(e)
    except Exception as e:  # noqa: BLE001  a crash in one step shouldn't hide the others
        kind, detail = "FAIL", f"{type(e).__name__}: {e}"
    secs = time.monotonic() - t0
    results.append((kind, name, detail, secs))
    print(f"{kind:4}  {secs:6.1f}s  {name}" + (f": {detail}" if detail else ""), flush=True)


def wait_for(fn, timeout: float, what: str, poll: float = 0.1):
    end = time.monotonic() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() > end:
            raise Fail(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(poll)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--pump", default="PUMP_3")
    ap.add_argument("--stale", default="TANK_5", help="tank whose level sensor goes stale")
    ap.add_argument("--valve", default="VALVE_XO_34", help="the crossover Jarvis should open")
    ap.add_argument("--speed", type=float, help="live minutes per real second during the run")
    ap.add_argument("--stale-after", type=float, default=5.0, help="the blueprint's sensors.stale_after_min")
    ap.add_argument("--expect-unavailable", metavar="CONN_ID", help="wait for this connector to go unavailable")
    ap.add_argument("--wait", type=float, default=60, help="seconds to wait for --expect-unavailable")
    ap.add_argument("--skip-run", action="store_true", help="skip the plan run step")
    ap.add_argument("--force", action="store_true", help="cancel a plan run that is already RUNNING")
    ap.add_argument("--run-timeout", type=float, default=180)
    args = ap.parse_args()
    api = Api(args.api)
    ctx: dict = {}
    print(f"iot_e2e against {api.base}")

    # 1. The live layer answers.
    def reach():
        s = api.state()
        for k in ("clock", "t", "assets", "connectors"):
            if k not in s:
                raise Fail(f"/api/live/state has no {k!r}")
        if args.pump not in s["assets"] or args.stale not in s["assets"]:
            raise Fail(f"{args.pump} or {args.stale} isn't in this field; pass --pump/--stale")
        ctx["speed0"], ctx["paused0"] = s.get("speed"), s.get("paused")
        if args.speed or s.get("paused"):
            api.post("/api/live/sim", {"speed": args.speed or s.get("speed"), "paused": False})
        ctx["speed"] = args.speed or s.get("speed") or 0.25
        conns = ", ".join(f"{c['id']} {c['kind']} {c['status']}" for c in s["connectors"])
        return f"clock {s['clock']}, speed {ctx['speed']} live min/s, connectors: {conns}"
    step("live API answers", reach)
    if results[-1][0] == "FAIL":
        return report()
    speed = ctx["speed"]
    tick_s = lambda live_min: live_min / speed + 3  # noqa: E731  real seconds for live_min, plus slack

    ws = Ws(api.base)

    def ws_open():
        if not ws.ok:
            raise Skip(ws.why)
        got = ws.wait(lambda m: m.get("type") == "state", 0, 5)
        if not got:
            raise Fail("/ws opened but sent no state message within 5 s")
        return "first message is state"
    step("/ws connects", ws_open)

    def active(aid, kind):
        return next((a for a in api.alerts() if a["asset_id"] == aid and a["kind"] == kind and a.get("active")), None)

    # 2. Pump trip -> PUMP_TRIP within 3 readings.
    def pump_trip():
        if active(args.pump, "PUMP_TRIP"):  # the scripted trip already fired: repair it and start clean
            api.post("/api/live/faults", {"asset_id": args.pump, "type": "REPAIR"})
            wait_for(lambda: not active(args.pump, "PUMP_TRIP"), tick_s(15), f"{args.pump}'s old trip to clear")
        before = {a["id"] for a in api.alerts()}
        t0, mono0 = api.state()["t"], time.monotonic()
        api.post("/api/live/faults", {"asset_id": args.pump, "type": "TRIP", "reason": "iot_e2e"})
        seen = set()

        def raised():
            s = api.state()
            if s["t"] != t0:
                seen.add(s["t"])
            a = active(args.pump, "PUMP_TRIP")
            return a if a and a["id"] not in before else None
        alert = wait_for(raised, tick_s(6), f"PUMP_TRIP on {args.pump}")
        ctx["trip_alert"], ctx["trip_mono"] = alert, mono0
        n = len(seen)
        st = api.state()["assets"][args.pump]["status"]
        if n > 3:
            raise Fail(f"{alert['id']} came after {n} readings, more than 3")
        if st != "ALARM":
            raise Fail(f"{alert['id']} raised but {args.pump} reads {st}, not ALARM")
        return f"{alert['id']} after {n} readings ({time.monotonic() - mono0:.1f}s): {alert['message']}"
    step(f"trip {args.pump} -> PUMP_TRIP within 3 readings", pump_trip)

    def ws_alert():
        if not ws.ok:
            raise Skip(ws.why)
        a = ctx.get("trip_alert")
        if not a:
            raise Skip("no trip alert to look for")
        got = ws.wait(lambda m: m.get("type") == "alert" and (m.get("alert") or {}).get("id") == a["id"], 0, 5)
        if not got:
            raise Fail(f"/ws never sent {a['id']}")
        return f"/ws sent {a['id']} {got[0] - ctx['trip_mono']:.1f}s after the fault"
    step("PUMP_TRIP reaches /ws (console and phone)", ws_alert)

    # 3. A stale sensor reads UNKNOWN, never OK.
    def stale():
        tank = args.stale
        if api.state()["assets"][tank].get("stale"):
            api.post("/api/live/faults", {"asset_id": tank, "type": "REPAIR"})
            wait_for(lambda: not api.state()["assets"][tank].get("stale"), tick_s(3), f"{tank} to read fresh")
        api.post("/api/live/faults", {"asset_id": tank, "type": "SENSOR_STALE", "reason": "iot_e2e"})
        mono0, bad = time.monotonic(), []

        def unknown():
            a = api.state()["assets"][tank]
            age = a.get("age_min")
            if (a.get("stale") or (age is not None and age > args.stale_after)) and a["status"] == "OK":
                bad.append(f"stale={a.get('stale')} age_min={age} but status OK")
            return a if a.get("stale") and a["status"] == "UNKNOWN" else None
        a = wait_for(unknown, tick_s(args.stale_after + 2), f"{tank} to read UNKNOWN")
        if bad:
            raise Fail(f"{tank} {bad[0]}")
        alert = active(tank, "SENSOR_STALE")
        if not alert:
            raise Fail(f"{tank} is UNKNOWN but no SENSOR_STALE alert is active")
        api.post("/api/live/faults", {"asset_id": tank, "type": "REPAIR"})
        return f"UNKNOWN after {time.monotonic() - mono0:.1f}s (age {a.get('age_min')} min), {alert['id']}; repaired"
    step(f"stale {args.stale} -> UNKNOWN, never OK", stale)

    # 4. A connector that goes unavailable turns its assets UNKNOWN.
    def connector():
        if args.expect_unavailable:
            cid = args.expect_unavailable
            print(f"      waiting up to {args.wait:.0f}s for {cid} to go unavailable...", flush=True)
            wait_for(lambda: any(c["id"] == cid and c["status"] == "unavailable" for c in api.state()["connectors"]),
                     args.wait, f"{cid} to read unavailable", poll=0.5)
        s = api.state()
        down = [c for c in s["connectors"] if c["status"] == "unavailable"]
        if not down:
            raise Skip("every connector is connected. Start the fake field (#62), run with "
                       "--expect-unavailable CONN_ID and stop it")
        out = []
        for c in down:
            mine = {k: v for k, v in s["assets"].items() if f"Lost the link to {c['id']}" in (v.get("reason") or "")}
            if not mine:
                raise Fail(f"{c['id']} is unavailable but no asset says it lost the link")
            wrong = [k for k, v in mine.items() if v["status"] not in ("UNKNOWN", "ALARM") or not v.get("stale")]
            if wrong:
                raise Fail(f"{c['id']} is unavailable but {', '.join(wrong[:5])} still read "
                           f"{s['assets'][wrong[0]]['status']}")
            out.append(f"{c['id']} unavailable, {len(mine)} assets UNKNOWN or ALARM")
        return "; ".join(out)
    step("connector unavailable -> its assets UNKNOWN", connector)

    # 5. Ask Jarvis. 6. Confirm the way the phone does. 7. The sim reflects it.
    def jarvis_why():
        tank = next((k for k in api.state()["assets"] if k.startswith("TANK_") and k[5:] == args.pump[5:]), None)
        if not tank:
            raise Skip(f"no tank pairs with {args.pump}")
        mono0 = time.monotonic()
        r = api.post("/api/jarvis", {"message": f"Why is Tank {tank[5:]} rising?", "selected_asset_ids": []})
        secs = time.monotonic() - mono0
        if not r.get("reply"):
            raise Fail("empty reply")
        if not re.search(r"\b\d{1,2}:\d\d\b", r["reply"]):  # the #44 check: it says when the tank shuts down
            raise Fail(f"no shutdown time in the reply: {r['reply'][:160]}")
        focus = [u.get("asset_id") for u in r.get("ui") or [] if u.get("action") == "focus"]
        return f"[{r.get('source')}] in {secs:.2f}s, focus {focus or 'none'}: {r['reply'][:140]}"
    step("ask Jarvis why the tank is rising", jarvis_why)

    def jarvis_propose():
        pos = lambda: api.state()["assets"].get(args.valve, {}).get("position")  # noqa: E731
        if args.valve not in api.state()["assets"]:
            raise Skip(f"{args.valve} isn't in this field; pass --valve")
        if str(pos()).upper() == "OPEN":  # a previous run opened it: close it first, through a person
            p = api.post("/api/actions", {"type": "SET_VALVE", "asset_id": args.valve, "value": "CLOSED",
                                          "reason": "iot_e2e reset"})
            api.post(f"/api/actions/{p['id']}/confirm", {"by": "iot_e2e"})
            wait_for(lambda: str(pos()).upper() == "CLOSED", tick_s(3), f"{args.valve} to close for the reset")
        mono0 = time.monotonic()
        r = api.post("/api/jarvis", {"message": "Open the crossover", "selected_asset_ids": []})
        secs = time.monotonic() - mono0
        props = [p for p in r.get("proposals", []) if p["action"]["type"] == "SET_VALVE"
                 and p["action"]["asset_id"] == args.valve and str(p["action"].get("value")).upper() == "OPEN"]
        if not props:
            raise Fail(f"no SET_VALVE {args.valve} OPEN proposal. Reply: {r.get('reply', '')[:160]}")
        p = props[0]
        if p["status"] != "PENDING":
            raise Fail(f"{p['id']} is {p['status']}, not PENDING: Jarvis must not act by itself")
        time.sleep(min(3.0, 2.0 / speed + 0.5))  # two ticks: nothing may move before a person confirms
        if str(pos()).upper() == "OPEN":
            raise Fail(f"{args.valve} opened before anyone confirmed {p['id']}")
        ctx["prop"] = p
        return f"{p['id']} PENDING in {secs:.2f}s, {args.valve} still {pos()} 2 ticks later: {p.get('text') or ''}"
    step("Jarvis proposes opening the crossover", jarvis_propose)

    def confirm():
        p = ctx.get("prop")
        if not p:
            raise Skip("no proposal to confirm")
        mono0 = time.monotonic()
        out = api.post(f"/api/actions/{p['id']}/confirm", {"by": "phone"})
        if out["status"] != "CONFIRMED":
            raise Fail(f"{p['id']} is {out['status']} after confirm: {out.get('result')}")
        if out.get("decided_by") != "phone":
            raise Fail(f"decided_by is {out.get('decided_by')!r}, not 'phone'")
        ctx["confirm_mono"] = mono0
        extra = ""
        if ws.ok:
            got = ws.wait(lambda m: m.get("type") == "proposal" and (m.get("proposal") or {}).get("id") == p["id"]
                          and m["proposal"].get("status") == "CONFIRMED", mono0 - 0.5, 5)
            if not got:
                raise Fail(f"confirmed, but /ws never broadcast {p['id']} CONFIRMED to the other screens")
            extra = f", /ws broadcast it in {got[0] - mono0:.2f}s"
        return f"{p['id']} CONFIRMED by phone{extra}"
    step("confirm it the way the phone does", confirm)

    def reflects():
        if "confirm_mono" not in ctx:
            raise Skip("nothing was confirmed")
        a = wait_for(lambda: (lambda v: v if str(v.get("position")).upper() == "OPEN" else None)(
            api.state()["assets"][args.valve]), tick_s(3), f"{args.valve} to read OPEN")
        return f"{args.valve} position OPEN, status {a['status']}, {time.monotonic() - ctx['confirm_mono']:.1f}s after confirm"
    step(f"/api/live/state shows {args.valve} OPEN", reflects)

    # 8. A plan run (#63): every step waits for a person, and each one is confirmed here.
    def plan_run():
        if args.skip_run:
            raise Skip("--skip-run")
        try:
            running = [r for r in api.get("/api/runs")["runs"] if r["status"] == "RUNNING"]
        except Fail as e:
            if "404" in str(e):
                raise Skip("this backend has no plan runs (PR #75)")
            raise
        if running and not args.force:
            raise Skip(f"{running[0]['id']} is already RUNNING; pass --force to cancel it")
        for r in running:
            api.post(f"/api/runs/{r['id']}/cancel", {"by": "iot_e2e"})
        plans = api.post("/api/plans", {})["plans"]
        plan = next((p for p in plans if p.get("keeps_running")), plans[0] if plans else None)
        if not plan:
            raise Fail("POST /api/plans returned no plans")
        mono0 = time.monotonic()
        run = api.post(f"/api/plans/{plan['id']}/execute", {"by": "iot_e2e"})
        if run["status"] != "RUNNING":
            raise Fail(f"{run['id']} started {run['status']}: {run.get('reason')}")
        rid, done, notes = run["id"], set(), []
        last_at = max([s.get("at_min") or 0 for s in run["steps"]] + [0])
        end = time.monotonic() + min(args.run_timeout, tick_s(last_at + 8) + 10)
        while time.monotonic() < end:
            run = api.get(f"/api/runs/{rid}")
            for s in run["steps"]:
                if s["status"] == "PENDING" and s.get("proposal_id") and s["proposal_id"] not in done:
                    out = api.post(f"/api/actions/{s['proposal_id']}/confirm", {"by": "iot_e2e"})
                    done.add(s["proposal_id"])
                    notes.append(f"step {s['n']} {s['action']['type']} {s['action']['asset_id']} "
                                 f"confirmed at {time.monotonic() - mono0:.1f}s ({out['status']})")
            if run["status"] != "RUNNING":
                break
            time.sleep(0.2)
        props = {p["id"]: p for p in api.get("/api/actions")["proposals"]}
        robot = [pid for pid in (s.get("proposal_id") for s in run["steps"]) if pid and pid in props
                 and props[pid]["status"] == "CONFIRMED" and props[pid].get("decided_by") != "iot_e2e"]
        if robot:
            raise Fail(f"{', '.join(robot)} confirmed without this script (decided_by "
                       f"{props[robot[0]].get('decided_by')!r}); a run must never confirm by itself")
        steps = ", ".join(f"{s['n']}:{s['status']}" for s in run["steps"])
        if run["status"] != "DONE":
            raise Fail(f"{rid} ended {run['status']} ({run.get('reason')}); steps {steps}")
        return f"{rid} DONE in {time.monotonic() - mono0:.1f}s, plan {plan['id']} ${plan.get('cost', {}).get('total_usd')}, steps {steps}. " + "; ".join(notes)
    step("plan run, each step confirmed by a person", plan_run)

    def ws_run():
        if not ws.ok:
            raise Skip(ws.why)
        got = [m for _, m in ws.msgs if m.get("type") == "run"]
        if args.skip_run or not got:
            raise Skip("no run messages") if args.skip_run else Fail("/ws sent no run messages during the plan run")
        return f"/ws sent {len(got)} run messages, last status {got[-1]['run']['status']}"
    step("plan run reaches /ws", ws_run)

    ws.close()
    if args.speed and ctx.get("speed0") is not None:
        api.post("/api/live/sim", {"speed": ctx["speed0"]})
    return report()


def report() -> int:
    n = {k: sum(1 for r in results if r[0] == k) for k in ("PASS", "FAIL", "SKIP")}
    total = sum(r[3] for r in results)
    print(f"\niot_e2e: {'FAIL' if n['FAIL'] else 'PASS'} ({n['PASS']} pass, {n['FAIL']} fail, {n['SKIP']} skip) in {total:.0f}s")
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
