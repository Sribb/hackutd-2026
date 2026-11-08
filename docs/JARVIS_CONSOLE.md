# Freeboard Jarvis console: UX and interaction spec

The operator's default screen. The 3D field fills the screen under one slim status line. A right-side sidebar holds Jarvis. Inside Jarvis, one toggle turns on hand control. Classic stays at `?layout=classic`.

**Read from:** `origin/67-demo-start` web/src (main, live, jarvis, hands, highlight, parts, inspect, sky, twonights), `origin/80-iot3d` iot3d.js, and `origin/38-jarvis-iot` jarvis.py, live_api.py and CONTRACT "Live layer". Also read: `origin/main` CONTRACT "What's in field.glb", AGENT_NOTE, DEMO.md, blender/field_parts.py, names.py; `origin/63-plan-runs` (plan runs); `origin/backend-handoff-vinny` phone.html (Remote layout). There is no `origin/109-remote-layout`; only `109-e2e-tokens` exists. phone.html's ws protocol (`hello`, `pending_command`, `jarvis`) is not the #38 contract, so port its layout, not its messages.

**Bugs this spec fixes:**
1. Pinch asks "What's going on with X?". That matches the field-wide status rule in `_rules`, not the asset.
2. When a key is set, `COMPLEX` sends "Why is Tank 3 rising…" (demo beat 4) to Claude.
3. jarvis.js and live.js send no `X-Freeboard-Token` or `?token=`, so with tokens on, every confirm returns 401.
4. hands.js clicks at the cursor of the pinch frame, so the pinch itself makes it jitter. It has no fist, idle throttle, or hint, and it never closes the landmarker.
5. main.js Escape also ends an approved plan walk. Reset must not.

**Missing in the backend today:**
- CF-7 alert fields (`priority`, `root_id`, `minutes_to_consequence`, shelve).
- `UNCERTAIN` proposal status. It is in #62's connectors only.

## 1. Wireframes

Jarvis closed (default):
```
┌───────────────────────────────────────────────────────────────────────────────────────┐
│FREEBOARD 1:52 AM ● Live·sim │WATCH ● 1 alarm +3 │FORECAST Tank 3 ▓▓▓▓▓░░ 55 min │PLAN $1,210 vs $8,940 │⚑ 1 fix waiting│ Jarvis ●│
├───────────────────────────────────────────────────────────────────────────────────────┤ 32 px, mono 12/13, tabular nums
│                                                                                       │
│                 3D field, full bleed (canvas = viewport − 32 px)                      │
│            [hover tag: "Pump 3 motor fan: still, ALARM"]                              │▐ Jarvis tab
│                                                                                       │▐ 28×96, right edge
│ (timebar: hidden 0 px; slides up 36 px when time ≠ now, bottom-edge hover, T, or a   │
│  click on FORECAST. It holds the slider, Two nights, and Sky. Hides 10 s after Now.) │
└───────────────────────────────────────────────────────────────────────────────────────┘
```
Jarvis open (380 px; min 340, max 28vw. The canvas narrows and the camera target stays put):
```
┌──────────────────────────────────────────────────────┬──────────────────────────────────┐
│FREEBOARD 1:52 AM ●Live │WATCH ●1 │FORECAST ▓▓▓░ 55m │ Jarvis   ● live    Hands [○]  ?  × │
├──────────────────────────────────────────────────────┤──────────────────────────────────│
│                                                      │ W  WATCH · 1:48                  │
│                                                      │ ● P1  Pump 3 tripped. Tank 3 is  │
│          3D field                                    │       filling.        [Pump 3]   │
│                                                      │ F  FORECAST · 1:49               │
│                                                      │ now ▓▓▓▓▓▓▓▓░░░░░░░░ 2:47 AM     │
│                                                      │ [Tank 3] shuts in in 58 min      │
│                                                      │ (2:36–2:58). 4 wells go down.    │
│                                                      │ P  PLANNER · 1:49                │
│                                                      │ fix  ██ $1,210                   │
│                                                      │ wait ████████████ $8,940         │
│                                                      │ Open the [Tank 3→4 crossover].   │
│                                                      │ Holds at +15% inflow. By 2:05 AM.│
│                                                      │ J  JARVIS  · · ·                 │
│                                                      │┌────────────────────────────────┐│
│                                                      ││FIX · from Jarvis · PENDING      ││
│                                                      ││Open the Tank 3 to 4 crossover.  ││
│                                                      ││Tank 3 holds through 7:48 AM.    ││
│                                                      ││$1,210 vs $8,940 doing nothing.  ││
│                                                      ││[Not now]  [▓▓▓▓ Hold to confirm]││
│                                                      │└────────────────────────────────┘│
│                                                      │[What's failing][Time left][Cheapest fix][Reset]│
│                                                      │[Tank 3 ×] Ask Jarvis…          ⏎ │
└──────────────────────────────────────────────────────┴──────────────────────────────────┘
```
- **Status line segments:** click any segment to open Jarvis at that agent's latest message.
  - Feed: `● Live · sim`, `◌ Polling`, `Live feed lost 0:42` (red until it is back), or `No backend` (amber).
  - Watch: dot plus count. The dot is red for P1 or ALARM, amber for P2 or WARNING, and gray for none. With no alarms but an UNKNOWN tank, it reads `No alarms · 1 tank unknown` with a gray hatched dot, never "all clear".
  - Forecast: a 120 px bar. See §2.
  - Plan: `$fix vs $wait`. It shows `No fix needed` only when nothing shuts down and no tank is unknown; otherwise `—`.
  - Pending pill: shown when any proposal is PENDING.
  - Jarvis tab: has an unread dot.
- **Keys:** `J` toggles Jarvis. `/` focuses the input. `Esc` resets the view (§3). `T` shows the timebar.
- **Auto-open:** Jarvis opens on its own only once per new root-cause P1. It never opens for anything else.
- **Quick chips:** with nothing selected, `What's failing · Time left · Cheapest fix · Reset`. With an asset selected, `Open · Sensors · Pipes · Isolate · Reset`, which is mouse parity for every 3D breakdown.
- **Old aside:** `#side` stays in the DOM (main.js still renders into it) but is hidden. `open_panel` moves one existing section (`#plans`, `#walk`, `#whatif`, `#selected`, `#log`, `#tanks`) into a sidebar drawer by id and puts it back on close. main.js keeps working untouched.

## 2. Agent message stream

Four agents in one chat-style stream. Watch, Forecast and Planner are templates over deterministic data and never call an LLM. Jarvis answers questions.

- **Bubble:** letter chip (W/F/P/J), name and clock, one or two plain sentences (140 chars or less), one visual, and asset chips.
- **Live vs. frozen:** each agent's latest bubble is live, so its visual updates in place. Older bubbles freeze and dim. The stream keeps 50; the shift log keeps everything.
- **Typing dots:** shown before each agent bubble for 350 ms, or 150 ms for an ALARM. For Jarvis they show until the reply arrives, minimum 250 ms, with "Asking Claude…" after 2 s when `source` will be claude. With `prefers-reduced-motion`, a static "…".

| Agent | Feeds (zero AI) | Posts when | One visual | Copy example |
|---|---|---|---|---|
| **Watch** | live.js `alert`, `feed`, `state` | Alert raised, resolved, acked or shelved; feed lost or back; a tank turns UNKNOWN | Priority dot + count (`● P1 · +3 related`); gray hatched for UNKNOWN | "Pump 3 tripped. Tank 3 is filling." / "No reading from Tank 5 for 7 min. Treat it as unknown, not safe." |
| **Forecast** | `brief.countdown` (new ws `brief`, §5) | First shutdown moves 5 min or more, appears or clears; the unknown set changes | Countdown bar: now → shutdown on a 0–6 h scale, with the low–high band hatched. With an unknown tank, a gray hatched bar and "Can't vouch for Tank 5" first. | "Tank 3 shuts in at 2:47 AM (2:36–2:58) if nobody acts. 4 wells go down." |
| **Planner** | `brief.plans` + `brief.last_call` | Cheapest robust plan or its cost changes; 15 min and 5 min before its Last Call expiry | Two bars: cheapest robust fix $ vs doing nothing $ (baseline `cost.total_usd`) | "Open the Tank 3 to 4 crossover: $1,210 vs $8,940 doing nothing. Start by 2:05 AM." |
| **Jarvis** | `POST /api/jarvis` replies, local intents | Every question; pinch on an asset | Asset chips + a small `rules`/`claude` tag | "Pump 3 is stopped, moving 0 barrels an hour." |

- **Rate:** at most one bubble per agent per 20 s, merged into the live bubble. An ALARM and a feed lost never wait.
- **No false all-clear:** no agent ever says "all clear", "all quiet", or "every tank is safe" while any tank is UNKNOWN or the feed is lost.
- **Asset chips:** made from known IDs and nice names in the text. Click or pinch flies to the asset and adds an 8 s `jarvis` highlight. A second click on the same chip opens it (explode).
- **Fix card (one at a time):** sticky above the input, showing exactly one proposal id.
  - **Contents:** text, why (`reason`), effect from `POST /api/jarvis/preview`, and source (`from Jarvis`, `from phone`, `plan step 1 of 3`).
  - **Buttons:** `Not now` rejects with a plain click. `Hold to confirm` uses fx.js `holdButton`, 1500 ms. It works with mouse, touch, or Space held, never by hand gesture.
  - **More than one waiting:** `+N more waiting` cycles through them.

  | Status | Card shows |
  |---|---|
  | PENDING | Buttons |
  | SENDING | "Sending to CONN_SIM…", no buttons |
  | CONFIRMED | "Done, confirmed by console 1:53 AM", folding into the stream after 5 s |
  | FAILED | "Failed: <result>. Nothing changed." |
  | UNCERTAIN | "Sent, not confirmed. The device may have moved. Check the field before trying again." No retry button. |
  | REJECTED | "Not done." |
  | 409 | "Already decided on another screen." |

- **Ack card:** tap to send. **Shelve card:** hold to send.
- **Tokens:** every POST sends `X-Freeboard-Token` and the socket uses `?token=`. A token comes from `?token=` once, then is stripped from the URL and kept in sessionStorage. On 401, ask for the PIN once per session and retry once. A wrong PIN shows "Wrong PIN. Nothing was sent." A 403 shows "This device can't do that."

## 3. 3D interaction (one visual language)

**State vs. attention:**
- **State** lives in body tint and lamps: main.js `paint()`, iot3d lights, and the CONTRACT status table. OK has no color, UNKNOWN is gray or hatched, and never green.
- **Attention** lives in outlines and labels, all cream `0xe8e2d6`. Pulse means "pointed at here". Outline color carries state only when nothing points at the mesh.

highlight.js sources, highest priority first:

| Source | Owner | Prio | Look | Change |
|---|---|---|---|---|
| `part` / `hover` | inspect.js | 100 / 90 | white fill / cream | — |
| `sensors` | console3d/sensors3d.js | 85 | cream, level 1.0 (fill, so small nodes show) | new, via `highlight.set(src, ids, style)` |
| `jarvis` / `phone` / `focus` | inspect.pointed, directives | 80 / 80 / 75 | cream + pulse 1.2 | was lavender `0xb9a7f0` |
| `open` | console3d/explode.js | 78 | cream, `dimOthers` | new |
| `selection` | inspect.js | 70 | cream, dimOthers | — |
| `run` | iot3d.js | 60 | cream + pulse 1.6 | was green `0x8fa36b` |
| `trying` | inspect.js | 50 | blue `0x8fb8c8` (what-if is a hypothetical state) | — |
| `alarm` / `alert` | inspect / iot3d | 40 | red pulse (amber when WARNING only) | — |
| `sensor` (quiet hw) | iot3d.js | 35 | gray `0x9a968e` = UNKNOWN | was amber `0xc9a14a` |
| `trace` | console3d/trace.js | 30 | cream level 0.5, staggered by hop | new |
| `related` | inspect.js | 20 | gray thin | — |

The three color changes are STYLES edits in highlight.js. That file is Sriyan's (#101), so ask for a handoff on the board.

All nodes below are resolved with parts.js `buildIndex()`. console3d builds its own index (same args as inspect.js), so inspect.js stays unchanged.

**Open (explode)** — `explode {asset_id}`; "open pump 3"; double-click; pinch on the selected asset; `Open` chip.
- **Order:** collapse any open asset (200 ms), then `select(id, true)` and fly to it, then move parts out over 450 ms (ease-out, 30 ms stagger).
- **Parts that move:** `index.parts(id)` nodes under the asset root. These stay put: `LEVEL`, `gauge` (main.js sets its `position.y` every frame), `label`, `label_cap`, `tag`, `/concrete|gravel/`, and `LOADOUT_n` (not under the root).
- **Offset:** `userData.explode` (local metres) if the glb has it. Otherwise radial in asset-local XZ, 0.35 R out plus 0.15 R × height rank up, capped at 1.8 R, where R is the bounding-sphere radius. Moving parts keep animating, because their rotation is local.
- **Labels:** one per moved FEATURE part (inspect's `FEATURE` regex) plus up to 3 largest material groups, 8 at most. HTML chips with a 1 px cream leader line.
  - Text is `partWords(kind,key).label`. When the part carries a reading, add ` · ` + `describe(id,key).live`: `lt` gives the level, `rotor` running/still, `light` status, `wheel` position.
  - A state dot only when the reading is not OK.
  - Update at 30 Hz. Stack greedily in screen y with a 4 px gap. Hide off-screen labels.
- **Hover or pinch on a part:** shows the existing tooltip. Jarvis posts the part's `describe().text` locally.
- **Collapse (300 ms):** on `collapse`, "close it", Esc, reset, opening another asset, a `focus` from a new ALARM, or a click on empty space.

**Show sensors** — `show_sensors {asset_id|"ALL"}`.
- **One asset:** `SENSOR_<asset>`, `<tank>_lt`, `<tank>_lshh`, and `RTU_PAD_n` for its pad (from `field` tank_id). Every one gets a label, like "Level radar · 71.2% · 8 s ago" or "Tank 5 level · no reading 7 min · UNKNOWN" (gray hatched chip).
- **"ALL":** every `SENSOR_*`, `*_lt`, `*_lshh` and `RTU_PAD_*`. Only non-OK ones get labels. Jarvis adds a summary: "12 sensor nodes: 11 reading, Tank 5 no reading for 7 min."
- **Sources:** status from `window.__iot3d.status.sensors` and RTUs; age from `age_min` and `last_reading_t` (live.js `staleMinutes`).

**Isolate** — `isolate {asset_id}`.
- **Hides:** every mesh except the asset, `PAD_n` of its tank, `SITE_*` ground and road, and its traced pipes. Also hides `SCENERY` (remember its value; the #88 fps guard may own it).
- **Shows:** the status line gets `Isolated: Tank 3 · Show all`.
- **Ends:** on reset, on isolate of another asset, or on a `focus` from a new ALARM. Exactly the old visibility comes back. Hidden meshes are never picked (inspect checks `visible`).

**Trace pipes** — `trace_pipes {asset_id, direction: downstream|upstream|both}`.
- **Graph:** a BFS over `index.pipes` (pipe → touched IDs, INLET/LOADOUT/XO already mapped) plus `linkGraph`. Valves pass flow through. 120 ms stagger per hop. Name-only labels on the assets along the path.
- **Example, Tank 3:** `PIPE_WELL_k_TANK_3` (×4), `PIPE_INLET_3`/`_RISER`, `PIPE_TANK_3_PUMP_3`, `PIPE_PUMP_3_VALVE_D_3`, `PIPE_VALVE_D_3_TRUNK`, `PIPE_XO_34_W`/`E`, and the trunk.
- **Limit today:** `PIPE_TRUNK_k` touches no ID, so the whole trunk and both SWDs light as the endpoint.

**Reset** — `reset_view`, "reset", fist, Esc, `Reset` chip.
- **Clears:** collapse, un-isolate, `sensors`, `trace`, `open`, `jarvis`, `phone`; selection set to null; time to 0; `flyHome()`; `SCENERY` restored.
- **Keeps:** what-ifs and an approved plan walk. If a what-if is on, Jarvis says "What-if still on: <list>. Say 'clear what-if' to remove it."

**What Sriyan's 3D agents must add to field.glb:**
1. **Functional parts.** `Kit.done()` merges static parts into one mesh per material (`<id>_steel`, `_steel_dark`, `_concrete`, `_detail`). So "open pump 3" today only splits material groups, and only tanks have real named parts (`_lt`, `_lshh`, `_gauge`, `_LEVEL`, `_label`). Split per kind with `userData.part` and `label`:
   - pump: `_motor`, `_casing`, `_coupling`, `_skid`, `_panel`
   - valve: `_body`, `_bonnet`, `_stem`
   - well: `_frame`, `_horsehead`, `_wellhead`, `_motor`, `_controller`
   - tank: `_shell`, `_stairs`, `_hatch`, `_inlet`, `_outlet`
   - truck: `_cab`, `_cargo_tank`, `_hose`

   That is about +4 draw calls per asset, so check #97's `draw_calls`.
2. **Explode offsets:** `userData.explode = [x,y,z]` (local metres) per part, so the layout is designed.
3. **Pipe topology:** `userData.from`/`to` on every `PIPE_*`, and named trunk tie-ins (`TRUNK_JCT_<valve|pump|swd>`), so the trace can follow the trunk to the nearest SWD.
4. **More sensor nodes.** `SENSOR_*` exists only for tanks and pumps with no connectors (names.py), but the sim reads valves, wells and SWDs too. Add `SENSOR_VALVE_*`, `SENSOR_WELL_*` and `SENSOR_SWD_*` for every simulated signal, or the console says "no sensor node in the model".
5. **Sensor signals:** `userData.signals` on `<tank>_lt` (`level_pct`) and `<tank>_lshh` (none: the label says "switch, not wired to the console").

## 4. Hand control (browser only, no server or AI compute)

MediaPipe Hand Landmarker (`@mediapipe/tasks-vision` 1.1.1) runs only while the Jarvis `Hands` toggle is on. Thresholds start from hands.js and are tuned here.

| Gesture | Detect (21 landmarks, mirrored x) | Enter / exit | Does | Mouse |
|---|---|---|---|---|
| Point | index extended (tip–wrist > 1.15 × PIP–wrist), middle and ring curled | 3 / 4 frames | Hover. The cursor is the index tip through a One-Euro filter (min cutoff 1.2 Hz, β 0.015). The hover target changes only after 3 stable frames. | move |
| Pinch | thumb–index gap / hand size (wrist→middle MCP) < 0.33 on, > 0.45 off | 3 / 3 frames | One click at the cursor latched 100 ms before pinch onset. Asset: select, then Jarvis asks `status of <ID>` (rules). Selected asset: open. Exploded part: Jarvis names it. A sidebar button with `data-hand-ok`: press it. Empty space: nothing. | click / dblclick |
| Open palm | 4 fingers extended and gap > 0.6 | 6 / 4 frames | Orbit with the palm centre. Dead zone 0.4 % of frame width per frame; gain 1.6π per frame width (as today). | left-drag |
| Two hands | both open or pinched | 4 / 4 frames | Zoom by span ratio, clamped to 0.95–1.05 per frame, dead zone 1.5 %. | wheel |
| Fist | 4 tips nearer the wrist than their PIPs, thumb tip within 0.5 hand size of index MCP | hold 15 frames (~0.5 s), with a ring filling on the cursor | Reset view, once. It re-arms only after another gesture. | Esc / Reset |
| (none) | no hand for 8 frames | — | Cursor fades out over 300 ms; hover clears. | — |

- **Switching:** a new gesture must win `enter` frames in a row. Leaving pinch needs its 3 release frames first, so pinch cannot jitter into a double click.
- **Hands never:** confirm, reject, hold, type, or ack.
- **Over the fix card:** the cursor shows "Use the mouse to confirm".
- **Cursor:** an 18 px cream ring. The hover tooltip is the same as the mouse's.
- **Lifecycle:**
  - **Start:** off → starting ("Starting camera…") → live.
  - **Idle:** no hand for 2 s; detection drops to 8 Hz.
  - **Errors:** a blocked or unavailable camera returns to off. Jarvis says "Camera blocked. Hands are off; the mouse does everything."
  - **What turns it off:** the toggle, closing Jarvis, tab hidden for 30 s, the camera track ending, or an error.
  - **Off means:** the rVFC/rAF loop is cancelled, every track is stopped, `video.srcObject = null`, and `landmarker.close()` frees WASM and GPU memory. The next start reloads the model from the HTTP cache in about 1 s.
- **First-use hint:** a centered card for 5 s on the first `live`: "Point to look · Pinch to open · Open palm to turn · Fist to reset". Any gesture or click dismisses it. It is remembered in localStorage (try/catch). `?` shows it again.
- **Perf budget:**
  - **Camera and detection:** 640×480 at 30 fps, GPU delegate (CPU fallback). Detect via `requestVideoFrameCallback`: 30 Hz with a hand, 8 Hz idle, 0 when off.
  - **Main-thread cost:** `detectForVideo` should average 8 ms or less. Above 12 ms for 3 s, drop to 15 Hz and the HUD says "Hands slowed to keep the 3D smooth".
  - **3D frame rate:** 45 fps or better with hands on (the existing #88 guard).

## 5. Jarvis tools and directives

**Routes, cheapest first:**
- **L (local):** browser intents, no network.
- **R (rules):** backend `jarvis_intents.py`, then the existing `_rules`, no AI.
- **C (Claude):** only when nothing matches. `COMPLEX` applies only after the deterministic intents fail.

Every number comes from a tool result. Nothing reaches equipment without a person's full hold on one proposal.

**UI directive** (the HTTP reply's `ui[]`, and broadcast on `/ws`):
```json
{"type":"ui","seq":41,"id":"ui_7f3a","action":"explode","asset_id":"PUMP_3","args":{},"source":"jarvis","reason":"open pump 3"}
```
- **Whitelist:**
  - `focus {asset_id}`
  - `highlight {ids ≤ 24, seconds = 20}` (IDs or `SENSOR_`/`RTU_`/`PIPE_` names)
  - `explode {asset_id}`, `collapse {}`
  - `isolate {asset_id}`
  - `show_sensors {asset_id | "ALL"}`
  - `trace_pipes {asset_id, direction}`
  - `set_time {t_min 0–360 step 5 | clock}`
  - `reset_view {}`
  - `open_panel {panel: alerts|plans|walk|whatif|asset|log|tanks, asset_id?}`
  - `try_whatif {actions[]}`: shows a what-if on screen; data only.
- **Server:** checks `fmap.kind_of` for IDs and `^(SENSOR|RTU|PIPE)_[A-Z0-9_]+$` for node names. It accepts client-sent `ui` only from tokened clients, extending today's focus-only rule.
- **Console:** applies directives from its own reply at once. It skips the ws echo by `id` (it remembers the last 200 ids). Phones ignore everything but `focus`.

| Backend tool (Claude + rules) | Args | Returns / does |
|---|---|---|
| `get_state` | — | clock, connectors, status counts, `unknown_tanks`, incident |
| `explain_asset` (exists) | asset_id | live reading, design, active alerts |
| `list_alerts` | active_only = true, priority?, group = true | alerts with priority, root_id, related_count, minutes_to_consequence, usd_at_risk, acked, shelved_until. Until CF-7 lands, derived from the baseline (P1 under 60 min, P2 under 180), with `priority_source: "derived"`. |
| `get_countdown` | — | first shutdown {asset, clock, low/high, wells_lost} or null, `unknown_tanks`, text |
| `get_forecast` (exists) | actions[] | events, cost, warnings, unknown_tanks |
| `compare_whatif` | actions[] | before/after shutdown and cost, gained_min, unknown_tanks, text |
| `get_plans` (exists, + arg) | robust_only = false | ranked plans with id, summary, actions, total_usd, robust, cannot_save |
| `get_last_call` | — | cheapest plan expiry and minutes left, point of no return, per-asset useful_until. Lazy `lastcall.core`, or `{error}`. |
| `get_audit` | since = 0, limit = 20, asset_id? | CF-10 entries |
| `list_sensors` | asset_id? | per node: signals [{signal, value, age_min, stale}], connector + status, status, stale_after_min |
| `describe_parts` | asset_id | kind, parts [{key, label, what, signal?, value?}] from `parts_catalog.py`, a copy of parts.js words |
| `propose_action` (exists) | type, asset_id, value, target_id, reason | PENDING proposal. Never moves equipment. |
| `execute_plan` | plan_id | #63 run. Each step is a proposal, released one at a time, so the card shows one. Before #63 lands, it proposes step 1 only. |
| `offer_ack` | alert_id | A card offer. A tap sends `POST /api/alerts/{id}/ack`. |
| `offer_shelve` | alert_id, minutes 15–240, reason (required) | A card offer. A hold sends `POST /api/alerts/{id}/shelve`. Bad args return an error to Claude. |
| `ui_focus` (=`focus_camera`), `ui_highlight`, `ui_explode`, `ui_collapse`, `ui_isolate`, `ui_show_sensors`, `ui_trace_pipes`, `ui_set_time`, `ui_reset_view`, `ui_open_panel`, `ui_try_whatif` | as the whitelist | Appends the directive to `ctx.ui` and broadcasts it. Returns `{ok, id}`. The prompt allows 3 or fewer per answer. |

- **Not exposed:** sim speed/pause/faults, connector config, blueprint activate, ingest, tokens, `JARVIS_CONFIRM`, confirm/reject, sky time, the scenery toggle.
- **New feed, `ws brief`:** `{countdown, unknown_tanks, plans: {cheapest, robust}, do_nothing_usd, last_call}` from the live scenario. Recomputed on a new or resolved alert, a confirmed proposal, and every 5 live minutes; Last Call only when an incident opens or a plan is approved. Also served at `GET /api/jarvis/brief`.
- **New endpoint:** `POST /api/jarvis/preview {action}` runs `compare_whatif` for the fix card.

**Deterministic phrases:** these go in `scenario/jarvis_phrases.json`, and both pytest and Playwright run it. "it/this/that" means the selection. Number words are normalized as `NUM_WORDS`.

| Phrase (examples) | Route | Result |
|---|---|---|
| open / break down / take apart / show parts of pump 3, well 9, tank 3 | L (+R for phone) | `explode`. **Exception:** `open`/`close` + valve, crossover or XO stays R `propose SET_VALVE` (equipment, hold). To explode a valve, say "break down valve d 3". |
| close it, put it back, collapse | L | `collapse` |
| show tank 3's sensors, sensors on pump 3, show all sensors | L | `show_sensors` |
| which sensors are stale, sensor ages | R | `list_sensors` + `show_sensors ALL` |
| isolate tank 3, only pump 3, just tank 3 | L | `isolate` |
| trace tank 3's pipes, where does tank 3 drain, follow well 9 | L | `trace_pipes` (drain = downstream) |
| show / go to / where is tank 3 | L focus + R | focus, then `explain_asset` text |
| reset, reset view, home, overview, start over | L | `reset_view` |
| in 1 hour, +30 min, at 3 am, back to now, live | L | `set_time` |
| hands on / hands off | L | toggle |
| what's failing, what's wrong, status, alarms, sitrep | R | `list_alerts` top 2 + focus. Never "All quiet" while a tank is UNKNOWN. |
| status of tank 3, how's pump 3, tell me about valve xo 34 | R | `explain_asset` |
| why is tank 3 rising / filling / red / down | R | explain + cause (pump alert) + shutdown. Exempt from `COMPLEX`. |
| time left, how long, countdown, when does tank 3 shut down | R | `get_countdown`. Unknown tanks come first. |
| cheapest fix, what should I do, plans, options | R | cheapest + cheapest robust + Planner bubble |
| last call, deadline, when does the plan expire | R | `get_last_call` |
| what if I open the crossover / send truck 1 / shut in well 9 | R | parse the action, `compare_whatif`, `try_whatif` |
| open the crossover, send truck 1 to tank 3, shut in well 9, choke well 9 to 50%, turn off pump 2 | R | `propose_action` (exists) gives one card |
| fix it, run the plan, approve the cheapest plan | R | `execute_plan` (questions and negations start nothing, as #63 says) |
| acknowledge it, ack the pump alarm | R | `offer_ack` |
| shelve the tank 5 alarm for 30 min because crew is on it | R | `offer_shelve`. With no reason: "Why? A shelve needs a reason." |
| who opened the crossover, what did we do, audit | R | `get_audit` |
| what am I looking at (with a selection) | R | explain (exists). With no selection: C with a screenshot. |
| anything else (why X instead of Y, compare, explain) | C | Claude with the tools above |

## 6. File plan: 3 parallel tracks

The tracks share two interfaces, frozen first in `docs/CONTRACT.md` "Jarvis console":
- the §5 directive schema, between A and B;
- the `view` API, between B and C: `view.{focus, highlight, explode, collapse, isolate, showSensors, tracePipes, reset, state}` and `hands.{start, stop, on}`.

B stubs `view` with focus only until C lands.

**Track A: backend Jarvis tools.** Vinny's agent (#38 lane). Tell Mounish on the board about the `backend/` hooks.
- **New:**
  - `backend/app/jarvis_tools.py`: schemas and handlers for the §5 tools, the directive builder and validator, and the `brief` compute.
  - `backend/app/jarvis_intents.py`: view intents and the new R phrases, run before `_rules`.
  - `backend/app/parts_catalog.py`.
  - `backend/tests/test_jarvis_tools.py`, `test_jarvis_phrases.py`.
- **Edits:**
  - `jarvis.py`: `TOOLS += jarvis_tools.TOOLS`; `run_tool` falls through to `jarvis_tools.run`; `_rules` calls `jarvis_intents.match` first; the prompt lists the `ui_*` tools.
  - `live_api.py`: `POST /api/alerts/{id}/shelve` (CF-7 shape), `GET /api/jarvis/brief`, `POST /api/jarvis/preview`, whitelisted client `ui`.
  - `live.py`: a `ui(directive)` broadcast next to `focus`; a `brief` broadcast; `UNCERTAIN` from a connector's `uncertain` result, if #62 isn't merged first.
  - `docs/CONTRACT.md`; `scenario/jarvis_phrases.json`.

**Track B: web console, agents and sidebar.** Vinny's agent (Remote layout owner). Ronak merges, per AGENT_NOTE.
- **New, all in `web/src/console/`:**
  - `index.js`: `mountConsole(hooks)`, the layout switch, and `window.__console`.
  - `statusline.js`, `sidebar.js` (header, Hands toggle, stream, chips, input), `stream.js` (bubbles, typing dots, asset chips), `agents.js` (Watch/Forecast/Planner templates from `brief`, `alert` and `feed`).
  - `fixcard.js` (proposal, ack and shelve cards, all statuses), `intents.js` (L route), `directives.js` (applies `ui`, dedupes ids, calls `view`).
  - `auth.js` (token, PIN, 401 retry; wraps fetch and `live.start`), `console.css`.
- **Tests:** `web/tests/console.spec.js`. Add `@playwright/test` and a `test:e2e` script.
- **main.js hooks (about 15 lines):**
  - import; `layout` from the URL;
  - mount `mountJarvis` and `mountHands` only for `classic`;
  - `const ui = mountConsole({...})` with stage, canvas, scene, camera, controls, renderer, liveFeed, api, select, flyHome, setTime/getTime (as the twonights hooks), setActions/getActions, view and field getters, data, assets, ids, idFor, nice, clockAt, usd, snapshot, and `logged: renderLog`;
  - `ui.ready(model.root)` after `inspect.ready`;
  - `ui.update(dt, t)` after `inspect.update()`;
  - Esc goes to `ui.reset()` in console layout.
- **live.js:** one line for `start(base, role, token)`.

**Track C: web 3D breakdown and hands.** Sriyan's 3D agents. Ronak merges.
- **New, all in `web/src/console3d/`:**
  - `view.js` (the `view` API and `window.__console3d`), `explode.js`, `sensors3d.js`, `trace.js`, `isolate.js`, `labels.js` (overlay, leader lines, collision).
  - `gestures.js`: pure `classify(landmarks, state)`, the One-Euro filter, hysteresis, and the pinch latch. Unit-testable in Node.
  - `handsctl.js`: lifecycle, rVFC throttle, cursor, hint, and `window.__hands` with `inject(frames)` for tests. Reuses `loadLandmarker` from hands.js.
- **Tests:** `web/tests/console3d.spec.js`.
- **Edits (handoff from #101):** the 3 STYLES colors in highlight.js. Plus the glb asks in §3, built in `blender/`.
- **No edits to:** inspect.js, parts.js (imported only), hands.js (classic only).

## 7. Acceptance checks (Playwright; backend at :8000, Vite at :5173)

**Track A** (Playwright `request` + `ws`; `JARVIS_MODE=auto`, no API key):
1. Every row of `jarvis_phrases.json` with route R or L returns 200 with `source: "rules"`, never a 503. Its `ui[].action` and its proposal type match the fixture.
2. "open pump 3" returns `ui[0]` `{action:"explode", asset_id:"PUMP_3"}` and `proposals: []`. "open the crossover" returns one `SET_VALVE` PENDING and no explode.
3. Fault `SENSOR_STALE TANK_5`, then `advance_min: 6`.
   - "time left" starts with "I can't vouch for Tank 5".
   - No reply to "time left" or "what's failing" contains "All quiet" or "every tank".
   - `list_sensors(TANK_5)` has `stale: true` and `age_min ≥ 5`.
4. "why is tank 3 rising" after `TRIP PUMP_3` is `source: "rules"`, even with a fake key set.
5. With `OPERATOR_PIN` set:
   - `/api/jarvis` with no header returns 401.
   - A Jarvis proposal confirmed with the phone token is accepted.
   - `/api/audit` shows `proposed` (source jarvis) then `confirmed` (role phone), in order.
   - An untokened ws `ui` explode is not broadcast.
6. Shelve:
   - With no reason: 422. With `minutes: 10`: 422.
   - A valid shelve sets `shelved_until`; `advance_min` past it sends the alert again, unshelved.
7. `ws brief` arrives within 2 s of `TRIP PUMP_3` (after the alert). `countdown.asset_id` is TANK_3, and its clock matches `get_countdown`.

**Track B:**
1. Default load:
   - The canvas box equals the viewport minus 32 px.
   - `#side` is hidden, and the status line shows Feed, Watch, Forecast and Plan.
   - Jarvis is closed. `J` opens it within 200 ms.
   - `?layout=classic` shows `#side`.
2. After `TRIP PUMP_3` and a 3-reading advance:
   - Jarvis auto-opens once.
   - A Watch bubble with a red dot and a `[Pump 3]` chip appears after a `.typing` element.
   - A click on the chip puts `__freeboardHL.state().sources.jarvis` at `["PUMP_3"]`.
3. The Forecast bar's clock is within 1 min of `brief.countdown`. With TANK_5 stale, the bar has `.hatched` and its text starts with "Can't vouch for Tank 5". No element contains "all clear".
4. The Planner bars show `brief.plans.robust.total_usd` and `do_nothing_usd`.
5. Fix card ("open the crossover"):
   - Exactly one card.
   - An 800 ms press sends no `/confirm` request.
   - A 1600 ms press sends exactly one; the card goes SENDING, then CONFIRMED.
   - The ws echo adds no second card.
6. A mocked `/confirm` returning `status: "UNCERTAIN"` shows "Sent, not confirmed" and no retry button.
7. With `OPERATOR_PIN`, hold → PIN prompt → wrong PIN gives "Wrong PIN. Nothing was sent.", and `/api/audit` has no `confirmed`.
8. With `/api/jarvis` aborted by `page.route`, "open pump 3", "show tank 3's sensors" and "reset" still work, and no `/api/jarvis` request is recorded.

**Track C** (through `window.__console3d` and `window.__hands`):
1. `view.explode("PUMP_3")`, 500 ms later:
   - Every moving part has moved 0.2 R or more from rest; `TANK_*_gauge` and `_LEVEL` have not moved.
   - 8 or fewer labels, all inside the viewport, with no two rects overlapping.
   - `collapse()` restores every position within 1e-3.
2. `showSensors("TANK_3")` puts `SENSOR_TANK_3`, `TANK_3_lt` and `TANK_3_lshh` in `sources.sensors`. Their labels contain an age ("s ago" or "min"). With TANK_3 stale, the label says "UNKNOWN".
3. `showSensors("ALL")` outlines every `SENSOR_*` and `*_lt`, and labels only non-OK nodes.
4. `tracePipes("TANK_3","both")` includes `PIPE_TANK_3_PUMP_3`, `PIPE_PUMP_3_VALVE_D_3`, `PIPE_VALVE_D_3_TRUNK`, `PIPE_INLET_3`, the 4 `PIPE_WELL_*_TANK_3` and `PIPE_XO_34_W`/`E`.
5. `isolate("TANK_3")`:
   - `SCENERY.visible` is false.
   - `__freeboardHL.pick` at `project("PUMP_5")` returns null.
   - `reset()` restores every mesh's prior `visible`, clears `jarvis`/`sensors`/`trace`/`open`, puts the camera within 0.01 of home, and the time at 0.
6. With Pump 3 in ALARM plus `view.focus("PUMP_3")`, `winner("PUMP_3_rotor").source === "jarvis"` (cream). After `reset()`, `alarm`.
7. Hands (`inject` with synthetic landmarks):
   - Point → hover id.
   - Pinch for 2 frames → no click; 4 frames → exactly 1 click at the latched point.
   - A ratio oscillating 0.34–0.44 for 60 frames → 0 extra clicks.
   - Fist for 15 frames → `reset` once.
   - A palm move changes the azimuth. A two-hand spread shortens the camera distance.
8. Lifecycle (`--use-fake-device-for-media-stream`):
   - Toggle on: `state().camera === "live"`, and the hint is visible, then gone at 5 s and not shown on the second toggle.
   - With no hand for 3 s, `detectHz` is 10 or less.
   - Toggle off, or closing Jarvis: every track is `ended`, `loopTicks` stays flat for 1 s, and `landmarkerClosed` is true.
   - The fix card's confirm button ignores `inject` pinches.
