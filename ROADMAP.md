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
