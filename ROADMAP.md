# Roadmap v2: the revamp

**Orchestrator:** Vinny's agent (`@vinnycodes67`), standing in for Mounish while his laptop is down. Sriyan (`@teameuphoriainv-dot`) is still the lead and can overrule any row. **All times are CST** (board timestamps are UTC, which is CST + 6 h).
Last updated: Sat Nov 7, 6:10 PM CST. Live status lives in the issues. This file holds the plan and the order.

> Mounish's [backend/ROADMAP.md](backend/ROADMAP.md) is still the backend's detailed checklist. This file is the cross-team plan on top of it.

## The product (one sentence)

**Upload your field, get your own night-shift console, then watch it answer what's failing, how long you have, and the cheapest fix, with Jarvis you can talk to and point at.**

```
site plan (photo / PDF / CSV / JSON)
   │  POST /api/ingest            ingest/ (#28)       Claude reads it, never guesses, flags gaps
   ▼
draft blueprint ──► review screen (#43/#49) ──► complete blueprint   docs/BLUEPRINT.md (#37)
   │  POST /api/blueprints        backend/ (#41)
   ▼
generator (#33) ──► field.glb ──► web console (#16): 3D field, slider, what-if, plans, Last Call (#10)
                                     │
connectors (#38: sim/mqtt/modbus/opcua/webhook) ─► detection ─► alerts ─► /ws ─► console + phones
                                     │
Jarvis (#38 backend, #44 panel, #45 hand control) ─► proposes a fix ─► a PERSON confirms ─► connector sends it
```

## How the orchestration works

1. Every task is a GitHub issue with one owner. Branch `<issue#>-<slug>` from fresh `main`.
2. When it's done, the agent posts on the board: `DONE #N | PR #M | tools/check: PASS (…)`.
3. The orchestrator checks the PR (runs `tools/check`, reads the diff), then posts that person's next row from this file.
4. Blocked? Post `BLOCKED #N: <why> | needs <who>`. The orchestrator reroutes within one board cycle.
5. Every agent keeps `tools/board wait` running. A silent agent with an open task gets pinged after 30 minutes, then its human gets a text.
6. Authors merge their own PRs once `tools/check` passes. If an agent isn't allowed to merge, its **human presses the button**.

## Milestones

| By (CST) | Milestone | Done when |
|---|---|---|
| **7:00 PM Sat** | **M1: merge queue cleared** | Every PR in the queue below is merged, and `tools/check` PASSes on main |
| **10:00 PM Sat** | **M2: upload to console, end to end** | Sample 2 (CSV) → review → build → the console opens on the new field |
| **2:00 AM Sun** | **M3: Jarvis + IoT live** | Pump trip → alert on the console and phone → ask Jarvis → it proposes a fix → confirm → the sim reflects it. Hand control works. |
| **6:00 AM Sun** | **M4: feature freeze** | Only bug fixes after this. Demo laptop set up from a fresh clone. |
| **10:00 AM Sun** | **M5: rehearsed + Devpost drafted** | 3 timed run-throughs of [scenario/DEMO.md](scenario/DEMO.md), every `{LIVE}` number filled |
| **11:00 AM Sun** | **Submit** | Devpost submitted, repo public, 1 hour of buffer. **Confirm the real deadline** (last year's was noon Sunday). |

## Step 1: merge queue (M1)

| PR | Owner (who presses merge) | What it is | Unblocks |
|---|---|---|---|
| #32 → #33 → #28 → #35 | Sriyan | Scenery kit → generator → ingest → Maincloud library | #41, #43/#49 live, #46, #47 |
| #36 | Mounish (the GitHub web UI is fine) | Validate on load, robust plan, mount ingest | #41, #42, #38's rebase |
| #10 → #16 → #49 → #52 → #53, and #9 anytime | **Ronak himself** (his agent can't merge) | Last Call → console → upload screen → hand control → Jarvis panel; lane fence | M2, M3, and the demo |
| #21 | Vinny | Close it, unmerged, once #38 lands (#38 replaces it) | nothing |

**If Sriyan is away:** with his OK (asked on the board), Vinny's agent merges #32, #33, #28, and #35 after `tools/check` passes, with a board post. Without his OK, everyone keeps building against his branches, and nothing on the critical path waits on 3D.

## Step 2: tasks by lane

Status: ✅ done · 🔨 in progress · ⏳ queued · ⛔ blocked

### Vinny (`@vinnycodes67`): scenario, Jarvis/IoT backend, demo, Devpost
| # | Task | Status | Depends on | Check |
|---|---|---|---|---|
| #37 | Blueprint format v1 (doc, schema, validator) | ✅ PR #48 | none | field.json + samples pass in `tools/check` |
| #39 | Demo kit: samples + 3-minute script | 🔨 PR #50 merged; **sample 3 photo still to draw** | none | each sample ingests with no errors |
| #38 | Jarvis + IoT connectors + human-confirmed fixes | 🔨 background agent; API shape posted for #44 | #36 (rebase only) | pump trip alerts in ≤3 readings; nothing moves without confirm |
| #40 | Devpost, pitch, credits | ⏳ after #38 | everything | slide numbers match the live backend |

### Mounish (`@mounishmavuduru`): backend + DB lead (light load while his laptop is down)
| # | Task | Status | Depends on | Check |
|---|---|---|---|---|
| #36 | Merge it | ⏳ | none | main has `validate.py` |
| #41 | `POST /api/blueprints` + serve the built glb, falling back to main's field.glb | ⏳ | #33, #36, #37 (use `scenario/validate_blueprint.py`) | posting `field.json` returns ok and a glb URL that loads |
| #42 | Live clock + applied plans (B5, B6) | ⏳ | #36 | approve the crossover, then state shows it OPEN and the forecast drops the shutdown |
| | Review #38's backend part | ⏳ | #38 PR | |
| **Fallback** | **No #41 PR by 7:30 PM → #41 moves to Vinny's agent** | | | |

### Ronak (`@ronakkasaram`): web console
| # | Task | Status | Depends on | Check |
|---|---|---|---|---|
| #15/#25 | Console: what-if, plans, Last Call, Night Survey look | ✅ in PR #16 (READY) | none | |
| #43 | Upload screen, with *Fill all* in review | ✅ PR #49 (READY) | #16, #28, #41 shape | sample 2 clears 13 review items with 3 entries |
| #45 | Hand control (MediaPipe, AirBench-based) | ✅ PR #52 (READY, stacked on #49); needs a live-hand test on the demo laptop | `web/` only | point → highlight, pinch → Jarvis opens on that asset, ≥30 fps (8.7 ms/frame measured) |
| #44 | Jarvis side panel | ✅ PR #53 (READY, stacked on #52); rerun against the real #38 when it lands | #38's API (shape is fixed and on the board) | "why is Tank 3 rising?" flies the camera and answers |
| #54 | Live IoT layer: live status, a stale sensor shows UNKNOWN (never OK), connector badge, 'live feed lost' | ⏳ next | #38 | stop a sensor, and its tank turns UNKNOWN within `stale_after_min` |
| #55 | Phone view: `/phone` with live alerts and hold-to-confirm | ⏳ | #38 | a Pump 3 trip shows on the phone in under 2 s; confirming on the phone updates the console |

### Sriyan (`@teameuphoriainv-dot`): 3D only, **never on the critical path**
| # | Task | Status | Depends on | Check |
|---|---|---|---|---|
| #32 #33 #28 #35 | Merge his PRs | ⏳ **the one place he blocks us** | none | |
| #46 | IoT sensor + RTU models with status lights | ⏳ | #33 | `tools/check` blender PASS, in budget |
| #47 | More equipment types + a labeled placeholder for unknown types | ⏳ | #33 | a blueprint with every new type builds |
| | Scenery pass on generator output | ⏳ | #32, #33 | under 300k triangles, 30 MB |
| | Ingest fixes: truck `minutes_to` on review; crossover flow wrongly re-asked; CSV `shutdown_pct`/`lost_usd_hr` columns | ⏳ optional | | |

**Rule:** the console, backend, and demo always run on main's `blender/field.glb`. Sriyan's work makes it better. It never gates a milestone.

## Critical path

`#36 + #33 + #28 merged` → `#41` → `#49 live` → **M2**
`#38` → `#44` + `#45` → **M3**
Everything else runs in parallel or is polish.

## Risks

| Risk | Plan |
|---|---|
| Sriyan's PRs sit unmerged | With his OK, Vinny's agent merges them after `tools/check`; until then we build against his branches |
| Mounish's laptop | His tasks are light; #41 falls back to Vinny at 7:30 PM |
| Blender isn't on the demo laptop, so the build fails | #41 falls back to main's field.glb; the upload beat still shows the review step |
| Claude API is down or out of credit | Jarvis's rules path answers the scripted questions; ingest of JSON/CSV needs no Claude |
| Hand tracking under expo lights | Mouse parity for every gesture; test it at the venue |
| A second track (Ronak's CBRE "firebreak" repo) splits focus | **Team decision needed.** Freeboard milestones come first. |

## Open decisions (humans)

1. The real submission deadline and rules for 2026: someone paste https://hackutd-2026.devpost.com/rules.
2. Are we also entering the CBRE track with firebreak, and who works on it?
3. Sriyan: OK for Vinny's agent to merge his PRs if he's away?
4. Sriyan: update AGENTS.md section 2 so the orchestrator can assign work.

---

# v2.1: iterate, test, and make it better (added Sat 6:50 PM CST)

Everything above stays: it's the plan we iterate on. This section adds what's left now that most features exist as code.

**Where we are:** almost every step of the product is built and tested on its own branch. What's not done: (1) it isn't all merged on main, (2) it's never run on real hardware, with a real API key, a real hand, a real phone, and Blender on the demo laptop, (3) four features are still in progress, and (4) nothing has been through a stranger test or a quality pass. "Built" means it passes tests on a branch. "Done" means a judge can do it on the demo laptop without us touching anything.

## Phase A: merge and integrate (now → 8:00 PM)
| # | Task | Owner | Done when |
|---|---|---|---|
| A1 | Merge #57 → #64 | Mounish | on main, tools/check PASS |
| A2 | Merge #10 → #16 → #49 → #52 → #53 → #56 → #58 → #70 → #9, with "Delete branch" each time | Ronak | on main, `npm run build` OK |
| A3 | Merge #69, #71, #76 | Sriyan | on main |
| A4 | Integration run #2 **on main** (rehearse.py + pytest + web build + both samples end to end), with every defect filed to its owner | Vinny's agent | 0 blockers open |

## Phase B: prove it on real hardware (8:00 → 11:00 PM), which is **M2**
| # | Task | Owner | Done when |
|---|---|---|---|
| B1 | Demo laptop from a fresh clone: Python 3.12, Blender, `npm run demo`, `.env` with the API key (never committed) | Ronak + Vinny | `tools/check` PASS with nothing skipped |
| B2 | Real Claude, real key: ingest the sample 3 **photo**, plus 10 free-form Jarvis questions | Vinny | the photo gives the right equipment with confidence; Jarvis answers with real numbers, never invented ones |
| B3 | Real Blender build of samples 1–3 through `/api/blueprints` | Sriyan + Mounish | each builds in under 30 s, and the console opens on it |
| B4 | Real hand control under expo-like lighting | Ronak | point, pinch, and palm each work 9 out of 10 times |
| B5 | Real phone over Wi-Fi via the QR pager: alert → confirm | Ronak | alert in under 2 s, confirm shows on the console |
| B6 | Draw sample 3, the hand-drawn site plan photo | Vinny (human) | `scenario/samples/sample3_handdrawn.jpg` exists |

## Phase C: finish the features (by 2:00 AM), which is **M3**
| # | Task | Owner | Done when |
|---|---|---|---|
| C1 | #42 one clock + applied actions: confirmed fixes and approved plans change `/api/state` and every forecast | Mounish | approve the crossover, and the console, live layer, forecast, and Last Call all agree |
| C2 | #62 real MQTT + Modbus with a fake SCADA/PLC | Vinny's agent (PR #74) | both connectors `connected`, trip on the PLC → alert, confirm → register write |
| C3 | #63 agentic plan runs + Ronak's plan-run panel | Vinny's agent + Ronak | "fix it" runs the cheapest robust plan step by step, each step confirmed, and a stuck valve pauses and re-plans |
| C4 | DB: publish SpacetimeDB, push every live change in under 1 s, lock writes (B2, B8, D2) | Mounish | two browsers see a confirm in under 1 s |
| C5 | #46 IoT sensor/RTU models, #47 equipment types, scenery under `SCENERY` | Sriyan | in budget, built from blueprints |
| C6 | Last Call under 1.5 s (B9) | Mounish | timed on the demo laptop |

## Phase D: make it better (iteration loops, 2:00 → 6:00 AM)
Each loop: **build → test (`scenario/rehearse.py` + Ronak's Playwright E2E) → file defects → fix → retest**, until **two clean runs in a row**.

| Loop | What "better" means | Owner | Exit |
|---|---|---|---|
| D1 Stranger test | Someone new goes from failure to an approved plan in under 60 s without touching the camera (README) | everyone, with Vinny running it | 3 strangers pass |
| D2 Jarvis quality | A 20-question eval (scripted + free-form, with and without a key): right numbers, focuses the right asset, never claims an unknown tank is safe, never sends a command without a confirm | Vinny | 20/20 |
| D3 Forecast truth | Hand-check shutdown times and plan costs on Avon, Pecos, and a 1-tank and a 20-tank field | Mounish | every number matches hand math |
| D4 Failure modes | Backend down, /ws drop, no key, blurry photo, wrong CSV, a 20 MB file, a stale sensor, Blender missing: each shows a clear message and never a fake "safe" | Ronak + Mounish | each case scripted in E2E |
| D5 Visual polish | Design review of the console, Jarvis panel, upload, and phone: one visual language, readable at expo distance, color only for state | Ronak | `/design-review` has no high findings |
| D6 Performance | 60 fps console, upload under 30 s, alert to phone under 2 s, Last Call under 1.5 s, Jarvis rules answer under 1 s | Ronak + Mounish | all measured on the demo laptop |
| D7 3D quality | Generated fields look like a real West Texas lease. Sensors and RTUs show where the IoT data comes from. | Sriyan | Sriyan signs off |

## Phase E: freeze and ship (6:00 → 11:00 AM), which is **M4 → submit**
| # | Task | Owner | Done when |
|---|---|---|---|
| E1 | Feature freeze at 6:00 AM: only fixes for defects found in rehearsal | everyone | no feature PRs after 6:00 |
| E2 | 3 timed rehearsals of DEMO.md, with `rehearse.py --fill` writing every LIVE number | Vinny + all | under 3:00 each, numbers match the slides |
| E3 | Devpost: final text (#60 draft), screenshots, the E2E video as backup, credits | Vinny | submitted by 11:00 AM |
| E4 | Pitch deck: 6 slides that follow DEMO.md's beats | Vinny | rehearsed with the live demo |
| E5 | Repo public and clean: no keys, README run steps, credits | Sriyan | judges can clone and run |

## Definition of a "perfect demo"
- [ ] Upload → review → 3D field in under 45 s, from a hand-drawn photo
- [ ] Pump trip → red on the console + phone buzz in under 2 s → plain-English cause
- [ ] Countdown and time slider → judge clicks a fix → time and cost update immediately
- [ ] Jarvis by voice or text, plus hand gestures: answers with real numbers and flies the camera
- [ ] "Fix it" → a plan run, each step confirmed by a person, and the sim shows the tank saved
- [ ] Real protocol on screen (MQTT/Modbus badge `connected`), plus a dead sensor shown as UNKNOWN
- [ ] Every number on stage comes from the live backend
