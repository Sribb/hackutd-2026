# Freeboard v2 roadmap: blueprint to 3D to Jarvis to self-healing field

Owner of this document: `@mounishmavuduru`, lead engineer for the backend and database. Mounish's agent **builds** the backend and database cards (section 6) itself. It doesn't hand out tasks. Sriyan (`@teameuphoriainv-dot`) is the team lead and assigns work (AGENTS.md section 2). Any lane change below takes effect when he confirms it on the board.

Credits: the vision picks and the base for the sensor, alert, and Jarvis code come from Vinny (#21 and his board post). The 3D generator, asset library, and ingest are Sriyan's (#28, #32, #33, #35). Last Call and the console are Ronak's (#10, #16, #26).

## 1. What we're building

A client uploads a blueprint of their oil field. Claude takes it apart into `blueprint.json`, and a human confirms anything uncertain. The generator assembles their field in 3D from our CC0 asset library. Then the field comes alive:

- **Jarvis console:** a fully interactive 3D field driven by hand gestures (computer vision in the browser), with a Claude assistant that explains and answers.
- **Signal and IoT layer:** connectors stream every sensor. Detectors catch failures, drift, stale or tampered signals, and capacity limits.
- **Agentic fixes:** the AI proposes a fix built from forecast and plan numbers. A human approves it, and the system drives the field's machines to carry it out, then confirms the result.

The track's three questions stay at the center: what's failing, how long do we have, and what's the cheapest fix. The upload step gets 45 seconds of the pitch at most.

## 2. The non-negotiable rules (every agent, every PR)

1. **The AI never moves a machine by itself.** Each actuation is a `command` row that moves through `PROPOSED → APPROVED → SENT → ACKED / FAILED / REJECTED`. Agents can only propose. Approving takes a human hold-to-commit of at least 1.5 s plus an on-screen confirm. A single gesture or a single click never approves.
2. **Every command is checked before approval.** The backend re-runs the forecast with the command added. It rejects the command if that creates a new shutdown or overflow, goes over a disposal-well limit, targets tripped or locked-out equipment, or uses a value outside the allowlist for that asset type.
3. **Simulated machines by default.** Real protocol adapters (MQTT, Modbus TCP, OPC UA) are read-only. Writing needs `ALLOW_ACTUATION=1`, an allowlist for each asset, and a human approval, and real writes are out of scope for the demo. We never connect to real field equipment (README).
4. **Kill switch.** `POST /api/commands/halt` and a STOP control in the UI (a button and an open-palm hold) freeze the queue at once. Every command, approval, and halt goes in an audit log, with the forecast before and after.
5. **Numbers come from the solver, never from a model.** Jarvis answers numeric questions only through tools that call `/api/forecast`, `/api/plans`, and `/api/lastcall`.
6. **No data is never safe.** A missing, stale, or tampered signal is an alert, never "OK".
7. **Keys stay on the server.** `ANTHROPIC_API_KEY` and `SPACETIME_TOKEN` live in `.env`, which is gitignored. The browser never sees them. With no key, an endpoint returns a clean 503.
8. **CC0 or our own work only.** Asset licenses and code reuse are credited in the README.
9. **Stay in your lane.** Change shared contracts only in `docs/CONTRACT.md`, in the same PR, and post on the board. Never edit another lane's files without a handoff.

## 3. Architecture

```text
 upload (img/pdf/csv/json)
        │
        ▼
 POST /api/blueprints ──► ingest/ (Claude reads it) ──► blueprint.json + review[] + errors[]
        │                                                    │ human confirms in web/
        ▼                                                    ▼
 backend validate.py ──► blender/build_field.py (headless) ──► field.glb + build_report.json
        │
        ▼ activate
 ACTIVE FIELD ──► forecast / plans / lastcall / approve  (backend/app)
        ▲                │
        │                ▼
 backend/connectors ──► SpacetimeDB (live bus) ◄──► web/ console (3D + gestures + Jarvis panel)
  sim field              signal_latest, alert,            ▲
  MQTT/Modbus/OPC UA     command, audit, plan_step        │ proposes only
  (read-only)                     ▲                        │
        ▲                         │                 backend/agent (Jarvis, Claude tool use)
        │ ACK                     │
 backend/actuators ◄── approved command (after the safety check and human approval)
  sim actuators (default)
```

## 4. Lanes (proposed by Mounish; Sriyan confirms)

| Lane | Owner | Folders |
|---|---|---|
| Backend core, database (SpacetimeDB), command bus, safety gate, actuators, blueprint lifecycle, integration | `@mounishmavuduru` | `backend/app/`, `backend/spacetime/`, `backend/actuators/`, `tools/e2e` |
| Connectors, signals, detectors, simulated field (from #21) | `@vinnycodes67` | `backend/connectors/` |
| Jarvis agent: Claude tool loop, tools, UI directives (from #21) | `@vinnycodes67`, handed to `@ronakkasaram` if Vinny runs out of time | `backend/agent/` |
| Web console: gestures, upload and review, Jarvis panel, command approval, Last Call | `@ronakkasaram` | `web/`, `lastcall/` |
| 3D generator, asset library, scenery, ingest, maincloud library, team lead | `@teameuphoriainv-dot` | `blender/`, `ingest/`, `library/` |
| Scenario values, sample blueprints, Devpost, pitch | `@vinnycodes67` | `scenario/` |

## 5. Contracts to freeze first (by M0)

All of these go in `docs/CONTRACT.md` (v2 section). Mounish writes the draft PR, and each owner signs off on the board.

### 5.1 Blueprint lifecycle (Mounish)

| Endpoint | Does |
|---|---|
| `POST /api/blueprints` (multipart `file`) | Runs ingest. Returns `{id, status: "REVIEW" or "INVALID", blueprint, review[], errors[]}` |
| `GET /api/blueprints` / `GET /api/blueprints/{id}` | List, or one with its status and reports |
| `PUT /api/blueprints/{id}` | The human's edits after review. Validates again (`backend/app/validate.py`) |
| `POST /api/blueprints/{id}/build` | Runs `build_field.py --blueprint --out data/blueprints/{id}/`. Status `BUILDING`, then `READY` or `BUILD_FAILED`, with `build_report` |
| `GET /api/blueprints/{id}/field.glb` | The built model |
| `POST /api/blueprints/{id}/activate` | Makes it the active field. `/api/field`, `/api/state`, forecast, plans, and lastcall switch over, SpacetimeDB is reseeded, and `field_clock.blueprint_id` is set |

Statuses: `UPLOADED → REVIEW → VALID → BUILDING → READY → ACTIVE`, with `INVALID` or `BUILD_FAILED` on the way.

### 5.2 Signals (Vinny writes the data, Mounish owns the tables)

`Signal = {asset_id, metric, value, unit, ts_ms, quality: "GOOD" | "STALE" | "BAD" | "SUSPECT", source}`

Metrics v1:
- tank: `level_pct`
- pump: `flow_bbl_hr`, `running`, `motor_amps`, `vibration_mm_s`
- valve: `position_pct`
- well: `water_bbl_hr`
- SWD: `inject_bbl_hr`, `pressure_psi`
- truck: `gps`, `load_bbl`
- comms: `heartbeat`

- `POST /api/signals` takes a batch of up to 1,000 signals. Connectors also write to the SpacetimeDB table `signal_latest` (one row per asset and metric) through the reducer `ingest_signals`.
- Ring buffer: the backend keeps 30 min per signal in memory for detectors and charts.

### 5.3 Alerts (Vinny detects, Mounish stores)

`Alert = {id, asset_id, kind, severity: "WARNING" | "ALARM", detected_ms, evidence, status: "OPEN" | "ACKED" | "RESOLVED"}`

Kinds v1:
- `PUMP_TRIP`: flow 0 while running is expected
- `RISING_UNEXPLAINED`: the tank rises faster than its wells explain
- `STALE_SIGNAL`: no update for `stale_after_min`
- `OUT_OF_RANGE`
- `SUSPECT_SIGNAL`: a jump that isn't physically possible, a flatline, or a checksum or signature failure. This is the "vulnerability / tamper" signal.
- `SWD_AT_CAPACITY`
- `COMMS_LOSS`
- `HIGH_VIBRATION`

An `ALARM` opens an incident, and the incident feeds `/api/state.incident` and the forecast.

### 5.4 Commands and safety (Mounish)

`Command = {id, asset_id, action, value, target_id, proposed_by: "agent" | "human:<login>", reason, status, check: {ok, problems[], forecast_before, forecast_after}, approved_by, sent_ms, ack_ms, result}`

| Endpoint | Does |
|---|---|
| `POST /api/commands` | Propose. Runs the safety check right away and stores the result in `check` |
| `POST /api/commands/{id}/approve` | Human only (header `X-Operator`). Runs the check again, and refuses if it now fails |
| `POST /api/commands/{id}/reject` | Rejects the command |
| `POST /api/commands/halt` / `POST /api/commands/resume` | Kill switch |
| `GET /api/audit` | Every proposal, check, approval, halt, send, and ack |

Actions are the forecast's action types (`SET_VALVE`, `SET_PUMP`, `CHOKE_WELL`, `SEND_TRUCK`), plus `ACK_ALERT` and `RESET_PUMP` (simulated only). Approving a plan creates one command per step, executed in step order. Each step waits for the ACK of the step before it.

### 5.5 Jarvis (Vinny, or Ronak)

- `POST /api/agent/chat`: `{messages, view: {selected_ids, camera, screenshot_b64?}}` returns `{reply, ui: [directives], proposed_command_ids: []}`, streamed with SSE.
- **Tools:**
  - reading: `get_state`, `get_alerts`, `forecast(actions)`, `plans()`, `lastcall()`, `explain_asset(id)`
  - UI: `focus_camera(id)`, `highlight(ids)`
  - writing: `propose_command(...)`, the only write tool, which only proposes
  - **No execute tool exists.**
- Models: `claude-sonnet-5-5` for chat (speed), and `claude-opus-5-5` for ingest and screenshot questions. With no key the endpoint returns 503, and a fast rules-only path still answers "what's failing / how long / cheapest fix" from the solver.
- UI directives: `{type: "focus" | "highlight" | "open_panel" | "play_forecast", ...}`. They're sent through the chat response and the SpacetimeDB table `ui_directive`, so every open console follows.

### 5.6 Gestures (Ronak, from Vinny's pick)

MediaPipe Hand Landmarker (`@mediapipe/tasks-vision`, Apache-2.0), patterned on AirBench (MIT). The camera stays in the browser.

| Gesture | Effect |
|---|---|
| Point | Hover and highlight |
| Pinch | Select |
| Palm drag | Pan |
| Two-hand spread | Zoom |
| Twist | Orbit |
| Fist hold 1.5 s | Hold-to-commit, only while a confirm dialog is open |
| Open palm hold 1 s | STOP, which halts commands |

Every gesture has a mouse and keyboard equivalent. A gesture counts only after it's confirmed over at least 5 frames.

## 6. Task cards

Each card: **goal, files, depends on, done when (a check anyone can run)**.

### Mounish (`@mounishmavuduru`): backend core and database
- **M-1 Contract v2:** sections 5.1 to 5.4 in `docs/CONTRACT.md`. Done when: the PR is merged, and Vinny, Ronak, and Sriyan have all signed off on the board.
- **M-2 Blueprint lifecycle:** the 5.1 endpoints. Ingest goes through `ingest.ingest_bytes`, validation through `validate.prepare`, and the build through Sriyan's CLI (its own `--out` per blueprint, a 120 s timeout, Blender path from `BLENDER_BIN`). Done when: uploading `scenario/field.json` goes to `READY`, `field.glb` downloads, and `activate` switches `/api/field` and `/api/state`. A 12-tank sample builds and activates, and the forecast runs on it.
- **M-3 Active field:** a registry for the active field. Every endpoint and Last Call read through it, and switching is atomic. Done when: switching fields mid-session never mixes IDs (there's a test).
- **M-4 SpacetimeDB v2:** tables `signal_latest`, `alert`, `command`, `audit`, `ui_directive`, `connector`, plus `blueprint_id` on `field_clock`. Write reducers are locked to an admin identity list, using the pattern from `library/spacetime`. Publish to maincloud `hackutd-freeboard`. Done when: an anonymous `upsert_asset` call is rejected, and the backend identity can write.
- **M-5 Command bus and safety gate:** section 5.4, plus `backend/actuators/sim.py`. Simulated actuators change the live simulated field and ACK after a realistic delay (valve 20 s, pump 5 s). Done when: tests cover these cases:
  - an agent proposal can't execute without approval
  - approval with a failing check is refused
  - halt stops the queue
  - opening the crossover when Tank 4 would overflow is refused with a reason
- **M-6 Live loop:** the simulated field (Vinny's) and the actuators share one clock. `/api/state` comes from live signals, and the forecast starts from the latest levels. Done when: after approving a plan, the levels in `/api/state` follow the forecast within ±2%.
- **M-7 End-to-end harness:** `tools/e2e` runs headless: upload a blueprint, build, activate, inject `PUMP_3` trip, detect the alert, get plans, propose, approve, execute, recover. Done when: it exits 0 in under 90 s, and runs at every milestone check.
- **M-8 Reviews:** review the backend part of PRs that touch `backend/` (V-1, V-5), between M cards. Merges go through Sriyan.

### Vinny (`@vinnycodes67`): connectors, detectors, Jarvis brain, scenario, pitch
- **V-1 Split #21:** land it as separate PRs on top of `main`, in `backend/connectors/` and `backend/agent/`. Don't touch `backend/app/`, which Mounish wires up. Done when: each PR passes its own tests and `tools/check`.
- **V-2 Simulated field connector:** one tick per second, driven by the active blueprint. It emits section 5.2 signals with noise, can inject faults (`POST /api/connectors/sim/fault`), and applies actuator changes from M-5. Done when: a `PUMP_3` trip injected at 1:47 raises `PUMP_TRIP` within 3 ticks.
- **V-3 Protocol adapters, read-only:**
  - MQTT first (`paho-mqtt`; topic `field/<asset_id>/<metric>`, with a local broker for the demo)
  - then Modbus TCP (`pymodbus`, register map in the blueprint)
  - OPC UA last, if time allows (`asyncua`)
  - Done when: a publisher script on a second laptop moves a tank level in the 3D view.
- **V-4 Detectors:** every kind in 5.3, with thresholds set per blueprint. `SUSPECT_SIGNAL` catches spoofed data: an impossible rate of change, a flatline, or a sequence or signature gap. Done when: unit tests cover each kind with a recorded series, and a replayed "spoofed sensor" demo raises `SUSPECT_SIGNAL` without crying wolf on the normal series.
- **V-5 Jarvis brain:** section 5.5, porting #21's tools and rules fast path. Done when, all without inventing a number:
  - "what's failing?" names `PUMP_3` and the evidence
  - "how long?" quotes `/api/forecast`
  - "fix it" proposes commands that land as `PROPOSED`, never executed
  - "show me tank 3" focuses the camera
- **V-6 Sample blueprints and pitch:**
  - three samples: JSON, CSV, and a photo of a hand-drawn plan (12 tanks max)
  - the 3-minute script
  - Devpost text
  - real numbers from the mentors
  - Done when: each sample builds through M-2.

### Ronak (`@ronakkasaram`): web console, gestures, Last Call
- **R-1 Land the open PRs:** #10, #16, #26, and #9 (rebased). Done when: they're merged and `tools/check` passes with the web build included.
- **R-2 Upload and review screen:** drop a file, see the parsed equipment table with confidence, fix or confirm anything flagged, Build, watch progress, Activate, and the console opens on the new field. It uses only the 5.1 endpoints. Done when: sample 3 goes from upload to an active console without a page reload.
- **R-3 Gesture layer:** section 5.6 in `web/src/gestures/`. It runs at 30 fps or more on a laptop webcam, with a visible cursor and a live gesture label. Done when: a stranger can pan, zoom, select an asset, and hit STOP within 60 s, without a mouse.
- **R-4 Jarvis panel:** the chat stream, tool calls shown as chips, UI directives applied, and an optional screenshot. Done when: the V-5 checks work in the UI.
- **R-5 Command approval UI:** the proposed command queue, each with its safety check (before and after forecast), hold-to-commit approval (gesture or mouse), reject, STOP always visible, and the audit view. Done when: the M-5 safety tests can be reproduced by hand in the UI.
- **R-6 Live bus:** subscribe to SpacetimeDB (`asset_state`, `signal_latest`, `alert`, `command`, `ui_directive`) instead of polling, falling back to REST. Done when: two browsers show the same approval in under 1 s.

### Sriyan (`@teameuphoriainv-dot`): lead, 3D, library, ingest
- **S-1 Merge queue:** #36, #28, #33, #35, #32, then the R-1 PRs. Confirm the lanes in section 4.
- **S-2 Generator follow-ups:** status-light and gauge nodes for each asset, named in the contract, so the console can drive them from signals. Gesture-friendly hit boxes, at least 2 m for each actuatable asset.
- **S-3 Asset library:** curated equipment variants (tank sizes, pump types, SWD heads) that the generator chooses from based on blueprint fields. Licenses tracked.
- **S-4 Ingest v2:** pull positions (`x`, `y`) and connections from site plans, and add a confidence score for each connection. Done when: the photo sample from V-6 builds with fewer than 5 review items.
- **S-5 Rules check:** paste the 2026 rules on the board, and confirm that use of the Anthropic API and the demo hardware are allowed.

## 7. Milestones

H0 = when the lead confirms this roadmap. Every milestone ends with `tools/check` and `tools/e2e` on `main`, and each agent posts its result line.

| When | Milestone | Done means |
|---|---|---|
| H0+1 | **M0: contracts frozen** | The CONTRACT v2 PR is merged, and the merge queue (S-1) is clear |
| H0+4 | **M1: pieces live** | M-2 and M-4 done. V-2 streams. A gesture pans the field. Jarvis answers "what's failing" |
| H0+8 | **M2: the full loop** | Upload a sample, build, activate, trip, alert, countdown, Jarvis proposes, a human hold-approves, simulated actuators run, levels recover live in 3D. `tools/e2e` is green |
| H0+12 | **M3: harden** | MQTT adapter live from a second laptop. The spoofed-sensor demo works. Three samples build. A stranger passes the 60-second test |
| H0+15 | **Feature freeze** | Only bug fixes after this |
| H0+17 | **Ship** | Devpost submitted, repo public, rehearsed twice |

## 8. If we run late, cut in this order

1. OPC UA and Modbus (keep MQTT plus the simulator)
2. Optional camera vision on the field (Moondream, RF-DETR)
3. Phone alerts
4. Multi-blueprint library UI (keep upload and activate)
5. Scenery
6. Jarvis screenshot questions

**Never cut:**
- the failure detected from signals
- the countdown with its range
- the cheapest plan
- the human-approved fix executing on simulated machines, with the kill switch
- gesture navigation

## 9. How the agents coordinate

- **Nobody orchestrates except the lead.** Each agent works through its own cards in order. When a card is DONE, it takes its next card here. Sriyan reassigns cards as lead (AGENTS.md section 2).
- A lane with no answer for 45 minutes: any agent can post the blocked cards and tag the lead. Only the lead moves them.
- Each card becomes a GitHub issue assigned to its owner. The branch is `<issue#>-<slug>`, and the PR says `Closes #N`.
- Post on the board when you start a card, and post DONE when it merges. Tag only the people it affects.
- Blocked for more than 20 minutes? Post the blocker and tag the owner. Mounish's agent re-plans.
- Contract change: same PR, plus a `` `@all` `` post.
- After each merge to `main`, the merging agent runs `tools/e2e` (once M-7 lands) and posts any FAIL lines, tagging the owner.
