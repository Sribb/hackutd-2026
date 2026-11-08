# Devpost draft: Freeboard (#40)

**Status: draft.** Paste each section into the matching Devpost field. Before submitting:

- Fill every `{LIVE: ...}` from the backend on the demo laptop, the same way as [DEMO.md](DEMO.md). Never type a number from memory or from a slide.
- Fill every `{fill: ...}` at the end of the event.
- Re-check the **Status** column under "What it does". Several parts were still in open PRs when this was written (Sat Nov 7, evening CST). Anything not merged and working on the demo laptop comes out of the text.

---

## Inspiration

The track gave us one night in Avon: a pump fails, a tank nears capacity, and the system forecasts a shutdown. It asked for a console that tells a night-shift operator what's failing, how long they have before it gets worse, and the lowest-cost way to keep running.

Every barrel of oil brings up salt water that has to go somewhere: into tanks, through pipelines, and down disposal wells. When a transfer pump trips at 2 AM, one person has to work out which tank is filling, when it will shut in its wells, and which fix costs least. They have to do it before the cheap options run out.

We named it Freeboard. That is the room left at the top of a tank, and it's the margin the operator is running out of.

Two things shaped the design. First, most consoles for this are a dashboard of numbers, and some add a 3D model to look at. We wanted a 3D field the operator can fast-forward and change before anything goes wrong. Second, every field is different. A console built for one hard-coded field is a demo, not a product. So we made the first step "upload your field."

## What it does

Upload a site plan, review what was read, and get a 3D night-shift console for that field. It answers the track's three questions, and a side assistant, Jarvis, explains it and proposes fixes. A person confirms every equipment command.

1. **Upload your field.** Drop a site plan on the upload screen: a photo, a PDF, a CSV equipment list, or a blueprint JSON. Claude reads images and PDFs into a draft blueprint (CSV and JSON need no Claude call). It never guesses a number. Anything not on the plan, such as a tank's shutdown level or what a shut-in well costs per hour, goes on a review list.
2. **Review.** A person sees every asset found, its label on the plan, and how sure the reader was (under 80% shows red). They fill each missing number or tick it as checked. Only a complete blueprint gets forecast.
3. **Build.** A generator turns the blueprint into a 3D field (`field.glb`) with every asset named by its ID, so the console can find and animate it.
4. **What's failing?** The field stays gray. The failed equipment is the only thing in color, with the cause in plain English. A tank with no reading shows UNKNOWN / NO DATA, never OK.
5. **How long?** The forecast gives the shutdown time with a range: {LIVE: /api/forecast event text}, between {LIVE: t_min_low} and {LIVE: t_min_high} minutes. Drag the time slider and the tank rises and its wells go dark.
6. **What's the cheapest fix?** Click a valve, truck, or well to try a change, and the shutdown time and cost update. The planner ranks plans by dollars. It always returns a plan, and it marks whether each one still holds if inflow runs 15% high. Today: {LIVE: /api/plans robust plan summary and cost}, against doing nothing at {LIVE: do-nothing cost}.
7. **Last Call.** Every fix has a lead time, so the cheap, slow fixes expire first. Last Call shows when each plan stops working: {LIVE: /api/lastcall expiry}. Amber fuse rings around the assets that can still help burn down as the slider moves.
8. **Approve.** Approving takes a press and hold. The console walks the numbered steps, flies the camera to each asset, and writes a radio script. A shift log records every what-if and approval and exports a handoff note for the next crew.
9. **Jarvis.** A side panel, never the main interface. Ask "Why is Tank 3 rising?" and it answers from the forecast and plans and flies the camera to the asset. "What am I looking at" sends a snapshot of the 3D view. When Jarvis proposes a fix, nothing reaches equipment until a person holds Confirm, and the backend enforces that, not just the UI.
10. **Hand control.** Turn on the laptop camera: point to highlight, pinch to select and open Jarvis on that asset, open palm to orbit, two hands to zoom. Hand tracking runs in the browser, and the video never leaves the laptop. The mouse does everything too.
11. **Live signals (in progress).** A blueprint can name connectors (`sim`, `mqtt`, `modbus`, `opcua`, `webhook`) for where readings come from and where confirmed commands go. With none, the backend simulates every asset, so the demo never depends on real hardware. Alerts go to the console and to phones.

| Part | Where | Status at time of writing |
|---|---|---|
| Forecast, planner, approve, SpacetimeDB module | `backend/` | Merged |
| Validate on load, robust plan, mount ingest | `backend/`, PR #36 | Open |
| Blueprint format v1 + validator | `docs/BLUEPRINT.md`, `scenario/` | Merged (#48) |
| Ingest (site plan to blueprint with Claude) | `ingest/`, PR #28 | Merged |
| Blueprint to 3D generator | `blender/`, PR #33 | Merged |
| CC0 scenery kit | `blender/assets/`, PR #32 | Merged |
| Console: what-if, plans, approve, Last Call | `web/`, PR #16 | Open |
| Last Call | `lastcall/`, PR #10 | Open |
| Upload and review screen | `web/`, PR #49 | Open |
| Hand control | `web/`, PR #52 | Open; not yet tried with a live camera |
| Jarvis side panel | `web/`, PR #53 | Open; tested against a stand-in backend |
| Jarvis + connectors backend | issue #38 | In progress |
| `POST /api/blueprints` (build an uploaded field) | issue #41 | Queued |
| Shared asset library on Maincloud | `library/`, PR #35 | Merged |

## How we compare

Same text as [PITCH.md](PITCH.md). Every tag resolves in `docs/COMPETITIVE.md` (#110): letter tags in its Sources list, `[F]` and `[N]` tags in its feature matrix. Before submitting, re-check the Freeboard rows against what is merged, and paste the source links below with the paragraph.

At 1:47 AM, Ignition [I1], AVEVA [A2], Experion [H1], and eLynx [L2] tell you a pump tripped. Freeboard tells you when Tank 3 shuts in, the cheapest fix, and when that fix expires. Its forecast runs six hours ahead with a plus or minus 15% band [F1]. The closest commercial feature we found, Honeywell's Experion Operations Assistant, warns 5 to 10 minutes before an alarm [H3]. The planner ranks fixes by dollars and flags the one that still holds if inflow runs 15% high [F2], and Last Call says when each fix stops being possible [F3]. We found no public equivalent of the planner at Ignition, AVEVA, Honeywell, eLynx, or WellAware [N1], and no fix expiry at any of the six we compared [N2]. Where they lead is alarm management: Ignition and Experion shelve alarms on a timer [I7] [H1], and Honeywell's DynAMo suite claims up to 80% fewer alarms, by the vendor's own count [H2]. EOG already sends real-time alerts to a control center [E1], so Freeboard is built to sit on top of SCADA and turn an alert into a decision.

Sources ("not found" means we found nothing public, not that the product lacks it):
- [I1] Ignition Alarm Notification module docs, 8.3: https://docs.inductiveautomation.com/docs/8.3/ignition-modules/alarm-notification
- [I7] Ignition alarm shelving, 5 minutes to 4 hours: https://docs.inductiveautomation.com/docs/7.9/alarming/alarm-status/alarm-status-shelving
- [A2] Coterra well loading with AVEVA PI (event frame notifications): https://www.aveva.com/en/perspectives/presentations/2026/well-loading-analysis---maximizing-production-with-aveva-pi/
- [H1] Experion PKS alarm shelving configuration: https://process.honeywell.com/content/dam/process/en/documents/training-docs/control-monitoring-and-safety-systems/mt/EXP-7013-MT-Experion-PKS-Alarm-Shelving-Configuration.pdf
- [H2] Honeywell introduces DynAMo Alarm Suite: https://www.automation.com/article/honeywell-introduces-dynamo-alarm-suite
- [H3] Honeywell AI control room assistant, March 2026: https://www.honeywell.com/us/en/news/press-releases/2026/03/honeywell-unveils-commercial-launch-of-ai-powered-control-room-assistant-following-successful-pilot
- [L2] eLynx cry-out alarms: https://elynxtech.com/platform/alarm-cry-outs
- [E1] EOG Fact Sheet, March 2025: https://eogresources.com/documents/EOG_Fact_Sheet_2025.pdf
- [F1] to [F3], [N1], [N2]: our feature matrix, `docs/COMPETITIVE.md` section 2 (rows: forecast of what happens next, cheapest fix, how long until a fix expires)

## How we built it

**One contract first.** Before anyone wrote code, `docs/CONTRACT.md` fixed the asset IDs (`TANK_3`, `PUMP_3`, `VALVE_XO_34`, ...) and the API shapes. The 3D model, the console, and the backend all build against it, so four people could work in parallel with fake data and swap in the real pieces as they landed. `tools/check` confirms the model names, the scenario, and the API still match the contract.

**Blueprint format.** One JSON file describes a field: tanks, pumps, valves, wells, disposal wells, trucks, lead times, costs, and optional positions and connectors. A JSON Schema (2020-12) and `scenario/validate_blueprint.py` check it. A draft is expected to fail the schema, and its errors become the review list. Keys that would hold a secret (`password`, `token`, `api_key`) are rejected; a connector names an environment variable instead.

**Ingest.** `ingest/` sends images and PDFs to the Claude API with structured JSON output, then maps labels to contract IDs, converts positions to meters, and builds the review list. CSV and blueprint JSON are parsed without Claude. A refusal from the model becomes a 422, not a guess.

**Forecast.** `backend/app/forecast.py` is a minute-by-minute mass balance starting from the current level: wells fill their tank, pumps empty it toward a disposal well up to its limit, crossovers move water from the fuller tank to the emptier one, and trucks drive, load, and haul. At the shutdown level, every well on that tank goes offline. Each run is replayed at +15% and -15% inflow to give the range. Cost is trucking, disposal, lost production, and well restarts. We chose simple physics over machine learning because it is easier to explain and to trust at 2 AM.

**Planner.** `backend/app/planner.py` tries the crossover, zero to two trucks, and choking or shutting in the wells that lose the least money per barrel first. Each action waits for its lead time. A plan has to keep every tank below shutdown. Plans that only add steps to a cheaper plan are dropped, and each plan says whether it holds at +15% inflow.

**Last Call.** `lastcall/` tests candidate plans and binary-searches when each one expires, at normal and +15% inflow. That gives the do-by time for each plan and each asset.

**3D.** `blender/build_field.py` runs Blender headless: blueprint in, `field.glb` out. It lays out pads, wells, disposal wells, the yard, trucks, and crossover routes, then checks that every expected name is present with no `.001` suffixes. It drops to a lighter detail level when a field would go over the 300,000-triangle budget. Moving parts (tank levels, pump rotors, valve wheels, pumpjacks, truck axles) each have their pivot set so the console animates them with one property. Scenery comes from a CC0 kit with every license checked at its source.

**Console.** `web/` is three.js with Vite. Clicking any part walks up to the node named with the asset ID. Status colors come from the backend, and the model itself stays gray. Every endpoint falls back on its own to fake data recorded from the real backend, and a badge names any endpoint that's fake.

**Jarvis and hands.** Jarvis uses Claude tool use, with a rules fast path that answers the scripted questions instantly and keeps working if the API is down. Proposals come back as actions that wait on `POST /api/actions/{id}/confirm`. Hand control uses MediaPipe Hand Landmarker in the browser, with gesture rules adapted from AirBench: a gesture counts after it holds for 4 frames, pinch has separate on and off thresholds, and the cursor is smoothed.

**Shared state.** A SpacetimeDB module holds live asset state, the clock, operators' what-if actions, and plan steps, so every open console sees the same thing without polling. The REST API stays the source of every number.

**Team process.** Our coding agents coordinated on a pinned GitHub issue used as a message board. Every task was an issue with one owner, a branch per issue, and a PR with a check line.

## Challenges we ran into

- **Missing data that looked safe.** We first tested last year's open-source tank forecasts. With no data in the query window, one reported a fill rate of 0 for every tank, then "0 trucks needed, overflow prevented." It also forecast from an empty tank, not the current level, so tanks at 95% and 5% got the same time left. We made "no reading" a first-class state: a tank with no data is WARNING or UNKNOWN, the forecast returns `null` levels with a warning, and the planner lists `unknown_tanks`.
- **A planner that could not say "I can't save that one."** An OR-Tools router we tried crashed in our crisis test, and its deadlines made a near-full tank unsolvable. We wrote our own planner that always returns a plan and lists what it cannot save.
- **Three readers, one file.** The generator, the backend, and ingest each read the blueprint and each needed different fields. Writing the format down exposed gaps between them, such as a truck field the backend requires but ingest never asked for. We listed them in `docs/BLUEPRINT.md` and made the schema require the fields the forecast depends on.
- **Reading a plan without guessing.** A model reading a drawing will fill in plausible numbers. Ingest keeps anything not printed on the plan missing and sends it to review, and the console won't build until a person clears the list.
- **An AI near equipment.** Jarvis can propose a valve command. We put the confirm gate in the backend, so a bug in the UI can't skip it.
- **Hand tracking that flickers.** Raw landmarks jitter, and a pinch near the threshold toggles every frame. Frame confirmation, pinch hysteresis, and cursor smoothing fixed it, and every gesture has a mouse equivalent for bad lighting.
- **A 3D budget for any field.** A 20-tank field with full detail goes over the triangle budget, so the generator picks a lighter detail level and says so in its build report.
- **Free scenery for West Texas.** We found no CC0 barbed-wire fence, yucca, or prickly pear, so we used a post-and-rail fence and left the cactus out instead of using a saguaro.

## Accomplishments that we're proud of

- The console never shows a tank as safe because it has no data.
- Every number on screen and in the pitch comes from the running backend. The offline fake data is recorded from it, not typed by hand.
- Any field, not just ours: the generator builds a 3D field from any valid blueprint (tested from 2 to 20 tanks in PR #33), and the backend validates the same file before it forecasts (PR #36). Wiring the two together for uploads is #41.
- Plans are checked at +15% inflow, so we never present a plan that only works on a good night.
- Last Call turns the countdown to a shutdown into a countdown to each option.
- Nothing reaches equipment without a person confirming it.
- Every third-party asset is CC0 or team-made, with its license checked at the source.

## What we learned

- Simple physics with a range is easier to defend than a model nobody can explain, and it's fast enough to rerun on every click.
- Write down the shared IDs and API shapes first. It let four people and their agents build in parallel and plug together late.
- "Missing" has to be a value the whole stack carries, from the blueprint to the forecast to the color on screen.
- Lead time is part of cost. The cheapest fix is often the one that expires first.
- AI is useful at the edges: reading a messy site plan and explaining the field. The solver produces every number, and a person makes every decision.

## What's next

- Replace the placeholder costs, rates, and lead times with real values from operators. The disposal cost range comes from a 2018 JPT article. Trucking and lost-production costs are placeholders.
- Real connectors. The blueprint already names MQTT, Modbus, OPC-UA, and webhook sources, and the next step is running them against real SCADA without code changes.
- Failure detection from a live time series: flag a pump when its tank rises faster than its wells explain.
- A phone view with live alerts and hold-to-confirm.
- More equipment types in the generator, and a labeled placeholder for types it doesn't know yet.
- Lock down who may write to the shared state, so only the backend can.

## Built with

`python` · `fastapi` · `uvicorn` · `pydantic` · `pytest` · `javascript` · `three.js` · `vite` · `blender` · `gltf` · `claude` · `anthropic-api` · `mediapipe` · `spacetimedb` · `typescript` · `json-schema` · `websockets` · `ntfy`

## What we built during the event

{fill at the end: PR count, line counts, and what is live versus simulated on the demo laptop}

---

# Credits

Team: Sriyan (`@teameuphoriainv-dot`, lead, 3D, ingest, shared library), Mounish (`@mounishmavuduru`, backend), Ronak (`@ronakkasaram`, web console, Last Call), Vinny (`@vinnycodes67`, scenario, blueprint format, Jarvis and connectors, demo, Devpost).

Licenses below come from each package's own metadata (npm lockfile `license` field or PyPI metadata) or the source's license page, as noted. **Unconfirmed** means we could not confirm the license in this pass.

## Services and models

| What | Used for | Terms |
|---|---|---|
| Claude API (Anthropic), model `claude-opus-5-5` | Ingest reads site plans (#28); Jarvis answers and proposes fixes (#38, `JARVIS_MODEL`) | Anthropic API terms (commercial service, not an open-source license) |
| MediaPipe Hand Landmarker model (`hand_landmarker.task`, Google) | Hand tracking (#52). Loaded at runtime from Google's model storage, or from `web/public/mediapipe/` offline | **Unconfirmed.** The MediaPipe code is Apache-2.0; the model file's license was not checked |
| SpacetimeDB Maincloud (Clockwork Labs) | Hosted shared state and the asset library (#35) | Hosted service terms; **unconfirmed** |
| ntfy.sh | Phone push alerts (#21, #38) | ntfy is Apache-2.0 per its GitHub repo; the hosted ntfy.sh service terms are **unconfirmed** |
| EOG HackUTD 2025 API | Practice data for testing the forecast logic (`docs/BACKEND.md`) | **Unconfirmed**; used for testing only, no data shipped |

## Software

| Library | Version | License | Used in |
|---|---|---|---|
| three.js | 0.170.0 | MIT | `web/`; also vendored in `blender/preview/vendor/` with its LICENSE (#33) |
| Vite | 5.4.11 | MIT | `web/` build (dev) |
| `@mediapipe/tasks-vision` (Google MediaPipe) | 1.1.1 | Apache-2.0 | `web/` hand control (#52) |
| `@fontsource/gloock` (Gloock font) | 5.3.0 | OFL-1.1 | `web/` display type (#16) |
| `@fontsource/azeret-mono` (Azeret Mono font) | 5.3.0 | OFL-1.1 | `web/` numbers and labels (#16) |
| `spacetimedb` (TypeScript SDK) | 2.14.1 | ISC | `backend/spacetime/`, `library/spacetime/` |
| TypeScript | 5.6.3 | Apache-2.0 | Type-checking the SpacetimeDB modules (dev) |
| SpacetimeDB server and CLI | 2.x | BSL 1.1 (changes to AGPL-3.0 with a linking exception on 2031-11-05), per its LICENSE.txt | Running the modules locally and on Maincloud |
| FastAPI | 0.115.6 | MIT | `backend/` |
| Starlette (FastAPI dependency) | 0.41.x | BSD-3-Clause | `backend/` |
| Pydantic | 2.x | MIT | `backend/` |
| Uvicorn | 0.34.0 | BSD-3-Clause | `backend/` server |
| HTTPX | 0.28.1 | BSD-3-Clause | `backend/` tests |
| pytest | 8.3.4 | MIT | Tests |
| Anthropic Python SDK (`anthropic`) | 2.6.0 | MIT | `ingest/`, Jarvis |
| python-multipart | 0.0.33 | Apache-2.0 | File upload for `POST /api/ingest` |
| websockets | (#21 pins 17.4) | BSD-3-Clause | `/ws` hub (#21, #38) |
| jsonschema | not pinned | MIT | `scenario/validate_blueprint.py` |
| Blender (`bpy`, `bmesh`, `mathutils`) | not pinned | GNU GPL; **unconfirmed in this pass** (GitHub reports no SPDX id for its mirror). Used as a tool only; the scripts and `field.glb` are ours | `blender/` generator |

Build-only packages pulled in by Vite (esbuild, Rollup, PostCSS, nanoid, picocolors, source-map-js, `@types/estree`, fsevents) are MIT, ISC, or BSD-3-Clause per the lockfile. Runtime dependencies of the `spacetimedb` SDK (base64-js, headers-polyfill, object-inspect, prettier, pure-rand, safe-stable-stringify, statuses, url-polyfill) are MIT per its lockfile.

## Code and ideas from other projects

| Project | License | What we took |
|---|---|---|
| [AirBench](https://github.com/VARSHITH707/AirBench) | MIT (per GitHub) | Gesture rules for hand control: 4-frame confirmation, pinch hysteresis, cursor smoothing. Credited in `web/src/hands.js` and `web/README.md` (#52) |
| [hackathon-agent-board](https://github.com/azizu06/hackathon-agent-board) | **No license file found** | `tools/board` and `.claude/` copied unchanged; `AGENTS.md` adapted from its template |
| [EOG_HackUTD](https://github.com/Lord-Protector/EOG_HackUTD) (HackUTD VIII) | **No license file found** | Idea only: cut water where it loses the least revenue first (our planner's well-choke order) |
| [AquaLink](https://github.com/bilal-feroz/AquaLink) | **No license file found** | Idea only: replay any future from a list of timed actions (our what-if shape). No code, 3D, or visuals |
| [CauldronWatch](https://github.com/anhnguyen0101/CauldronWatch) (HackUTD 2025) | **No license file found** | Reviewed and tested as a baseline (`docs/BACKEND.md`). We found no commit importing its code; the forecast was written new |
| [SnitchTheWitch](https://github.com/Abhijith-utla/SnitchTheWitch) (HackUTD 2025) | **No license file found** | Reviewed and tested as a baseline. No code taken |

"No license file found" means GitHub reports no license for the repo. For the two we copied from or adapted (hackathon-agent-board, and AirBench, which is MIT), credit is kept in the files. Before the repo goes public, confirm with hackathon-agent-board's author that reuse is fine.

## 3D assets

Every scenery asset is CC0 1.0, from the `blender/assets/` kit (PR #32). CC0 doesn't require credit; we credit every author anyway. The full record, with source URLs and the changes we made, is in `blender/assets/manifest.json` and `blender/assets/CREDITS.md`. Each license was checked at its source: [polyhaven.com/license](https://polyhaven.com/license), [docs.ambientcg.com/license](https://docs.ambientcg.com/license/), and the `License.txt` inside each Kenney zip.

| Source | Assets | Authors | License |
|---|---|---|---|
| [Poly Haven](https://polyhaven.com) | Mesquite, creosote (2), sage (2), dry grass, dead branches (2), rocks (2), boulders (2), oil drum, tire; ground textures dry soil, caliche, gravel pad; moonlit night HDRI (`rogland_moonlit_night`) | James Ray Cock, Jenelle van Heerden, Rico Cilliers, Greg Zaal, Serhii Khromov, MP, Rob Tuytel, Charlotte Baglioni, Matterfield | CC0 1.0 |
| [ambientCG](https://ambientcg.com) | Dirt road tracks texture (`Ground017`) | Lennart Demes | CC0 1.0 |
| [Kenney](https://kenney.nl) Nature Kit 2.1 | Dry grass, fence rail, fence gate, lease sign | Kenney | CC0 1.0 |
| Kenney Car Kit 3.1 | Pickup | Kenney | CC0 1.0 |
| Kenney City Kit Industrial 2.0 | Shed | Kenney | CC0 1.0 |

The field itself (tanks, pumps, valves, wells, disposal wells, trucks, pads, pipes) is modeled by our own scripts in `blender/`. No 3D models or visuals come from other hackathon projects.

## Data and figures

- Saltwater disposal cost range ($0.60 to $1.50 per barrel, 2018 figures): [JPT](https://jpt.spe.org/twa/saltwater-disposal-key-permian-productivity). Recorded in `scenario/field.json` under `costs._source`.
- All other costs, rates, and lead times in `scenario/field.json` and the samples are placeholders until the mentors confirm them. The field is simulated. Nothing connects to real equipment.
