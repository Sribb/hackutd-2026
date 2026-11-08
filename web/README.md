# Web console

Ronak's lane (`web/`). The 3D field, the side panels, and the time slider.

```bash
cd web
npm install
npm run dev                                      # http://127.0.0.1:5173, fake data
VITE_API=http://127.0.0.1:8000 npm run dev       # real backend
npm run build                                    # what tools/check runs
```

## What works

- Loads `blender/field.glb`, served from Sriyan's folder at `/field.glb`. `web/` never keeps a copy. Until the model lands, it draws a gray placeholder field built in code, following the CONTRACT.md Blender rules (`src/placeholder.js`).
- Click any part of an asset to select it. The click walks up to the node named with the asset ID, so `PUMP_3_motor` selects `PUMP_3`, and `TANK_3_LEVEL` selects `TANK_3`. A drag moves the camera instead of selecting. Escape clears the selection.
- Status colors come from `/api/state`. The model stays gray, and only non-OK assets get color. An asset with no reading shows **NO DATA** and is never drawn as safe.
- Tank fill meshes scale `y` with `level_pct`.
- The time slider plays the do-nothing forecast. Tank levels move, a tank turns red at shutdown, and its wells go dark. The big clock always shows the real time, and the forecast time is shown on the time bar.
- The camera frames the whole field on load, whatever size the model is.

## What-if, plans, approve, Last Call

- **Try a change:** select a valve, truck, or well on the incident tank and press a "Try a change" button. The console posts `/api/forecast` with the action list, and the verdict, cost, tank levels, and slider update. Each action uses the lead time from `field.lead_min` (the rover's 20 minutes to the crossover, for example). The "Trying" card lists every action, and you can remove any one.
- **Cheapest plans:** come from `/api/plans`, with Last Call's "Start by" time. "Try it" loads a plan as a what-if. "Approve" calls `/api/plans/{id}/approve` and walks the numbered steps: the camera flies to each step's asset, and the radio script is underneath. Escape exits and flies home.
- **Last Call:** the incident card says when the cheapest plan expires. An amber fuse ring around each asset that can still help burns down as the slider moves and disappears at that asset's `useful_until_min` from `/api/lastcall`. Rings hide while a what-if is active.

## Data

`src/api.js` reads the API in the `docs/CONTRACT.md` shape. Each endpoint falls back to `public/fake/*.json` on its own, so a backend that only has `/api/field` and `/api/state` still runs live for those two. The badge names any endpoint that's fake. A what-if the fake data doesn't cover is refused with a note. It's never guessed.

The fake files are generated, not hand-written: `python3 web/scripts/make_fakes.py`. Every number comes from the Last Call reference forecast (PR #10) on its placeholder scenario: Tank 3 shuts down at 2:50 AM. `whatif.json` covers every combination of crossover, two trucks, and the four Tank 3 wells (none, 50%, or shut in).

## Debug

`window.__freeboard` in the browser console has `assets` (ID to meshes), `select(id)`, `viewAt(t)`, `setActions([...])`, `actions`, and `missing` (contract IDs not found in the model).

## Next

1. Swap to the live `/api/forecast`, `/api/plans`, and `/api/lastcall` as the backend lands. No code change is needed.
2. Run the README's 60-second test on a stranger.

## Night shift tools

- **Hold to approve.** Approving a plan takes a 1.5 s press and hold (mouse, touch, or Space/Enter on the focused button). Letting go early approves nothing.
- **Shift log.** Every what-if result, approval, finished step, and typed note is stamped with the shift clock, which starts at `/api/state`'s clock and runs in real time. It survives a reload in the same browser. **Copy handoff** or **Download .txt** writes the note for the next crew: incident, Last Call deadline, approved plan with steps checked off, the forecast on screen, tank levels, and the log.
- **Model parts from field.glb:** tank float gauges follow the level, pump and well status lights take the status color, and a sent truck's beacon, lights, and axles run while it's on the job.

## Fake data from the real backend

`public/fake/` is recorded from the backend, so the offline demo shows the product's numbers:

    python -m uvicorn backend.app.main:app --port 8000      # from the repo root
    python3 web/scripts/make_fakes.py --from-api http://127.0.0.1:8000

The files say "Recorded from the backend" in their warnings, and the badge still reads FAKE DATA.

## Load a field (upload screen, #43)

**Load field** (top right), or open `/#upload`.

1. **Drop** a site plan: a photo or PDF (Claude reads it on the server), a CSV equipment list, or a blueprint JSON. It is sent to `POST /api/ingest` as multipart `file`.
2. **Check** every asset Claude or the parser found, with its label on the plan and how sure it is (under 80% shows red). Each `review` item needs a human: type the missing numbers, or tick "I checked this". Any `errors` block the build.
3. **Hold to build** sends `{"blueprint": ...}` to `POST /api/blueprints` and polls `GET /api/blueprints/{id}` while `status` is `building`. On `ready` or `fallback` the console reopens on `/api/field` and the returned `glb_url`. The clock in the corner counts from the drop, and the shift log records the total.

Without a backend, only a blueprint JSON can be shown, and nothing is built.

## Hand control (#45)

**Hands** (top right of the field) turns on the laptop camera. Everything also works with the mouse.

| Gesture | Does | Mouse |
|---|---|---|
| Point (index finger out) | Moves the cursor, highlights what's under it | Hover |
| Pinch (thumb to index) | Selects it, flies to it, and opens Jarvis on it (`freeboard:jarvis` event) | Click |
| Open palm, moving | Orbits the camera | Drag |
| Two hands apart or together | Zooms | Wheel |

MediaPipe Hand Landmarker (`@mediapipe/tasks-vision` 1.1.1, Apache-2.0) runs in the browser. The camera feed never leaves the laptop. The gesture rules follow AirBench (github.com/VARSHITH707/AirBench, MIT): a gesture counts after it holds for 4 frames, a pinch has on/off thresholds so it doesn't flicker, and the cursor is smoothed.

The WASM runtime is served from the npm package at `/mediapipe/wasm`. The hand model loads from Google's model storage. To run with no network, save `hand_landmarker.task` as `web/public/mediapipe/hand_landmarker.task`, and it is used instead.

On this MacBook Air, the model loads in 2.5 s and runs at 8.7 ms a frame, so the 30 fps camera is the limit.

## Jarvis panel (#44)

**Jarvis** (top right of the field) opens a side panel. It never replaces the console.

- **Ask** about the field. The selected asset goes along as `selected_asset_ids`. **What am I looking at** also sends a 640 px PNG of the 3D view. `POST /api/jarvis` replies with text, `ui` focus directives (the camera flies there), and proposed fixes.
- **Proposed fixes** need a press and hold to confirm (`POST /api/actions/{id}/confirm`), or Reject. Nothing reaches equipment without that. Confirmations, rejections, and new alerts go into the shift log.
- **Alerts** come from `GET /api/alerts` and live over `/ws` (`alert`, `proposal`, `ui` messages). The button blinks red on a new alert. The socket reconnects with backoff.
- A **pinch** in hand control opens Jarvis on that asset and asks what's going on with it.
- A 503 from Jarvis (no matching rule and no API key on the server) shows its `detail` in the chat. Without a backend, the panel says so.

## Live IoT layer (#54)

With a backend, `src/live.js` reads `GET /api/live/state` and keeps one `/ws` socket that the console, Jarvis, and the phone page share.

- A live reading beats the snapshot from `/api/state` for status, level, position, and running.
- **A stale sensor is UNKNOWN, never OK**, and never a stale alarm passed off as current. It shows hatched in the panel, dimmed in 3D with a slow gray lamp, and says "No reading for N min. Last known: ALARM at 71.2%." A tank with no reading gets "Can't vouch for Tank N" under the tank levels, because its forecast starts from an old value.
- The connector badge under the clock lists each connector (sim, mqtt, modbus, opcua, webhook) and marks any that are unavailable. It reads `connectors` from `/api/live/state`, then from `/api/health`. If neither has it, it shows `sim`.
- If the socket is down for 4 s, the badge turns into **Live feed lost, m:ss ago**, and every OK value becomes UNKNOWN until the feed is back. REST polling runs every 3 s while the socket is down, and the socket reconnects with backoff.
- A backend without `/api/live/state` (404) leaves the console as it was.

## Phone view (#55)

`/phone` is the page a night operator keeps in their pocket. It shows live alerts, newest first, in big type, and the screen flashes and buzzes on a new one. Below them are the pending fixes from `/api/actions`, with **hold to confirm** or reject. It follows the same rule as the console: nothing moves without a person confirming. A decision on the phone shows up in the console's Jarvis panel over `/ws`.

On a phone on the same Wi-Fi:

    VITE_API=http://<laptop-ip>:8000 npm run phone     # Vite listens on the LAN
    # open http://<laptop-ip>:5173/phone on the phone

The page picks its backend in this order: `?api=http://<laptop-ip>:8000` (remembered on that phone), then `VITE_API` with any `127.0.0.1` swapped for the host the page came from, then the page's own host on port 8000. The backend has to allow that origin (CORS / `PUBLIC_URL`).

**No login.** Anyone on the same network who opens the page can confirm a fix. That's fine for the demo, not for a real field.

## One-command demo (#67)

    cd web && npm install && npm run demo

It starts the backend on `127.0.0.1:8000` (with `.venv/bin/python` if the repo has one, else `$PYTHON`, else `python3`) and the console on `0.0.0.0:5173`. Then it prints the console URL, the phone URL, a QR code for the phone, and the curl lines that trip a pump or speed up the sim. Ctrl+C stops both.

The Vite server proxies `/api` and `/ws` to the backend (`VITE_API=same-origin`), so the laptop and phones on the same Wi-Fi use one origin, and the backend needs no CORS change. The **Pager** button in the console shows the same QR code: a judge scans it, and their phone becomes the night pager.

If the backend can't start, the console still runs on recorded data and says so. The backend needs Python 3.10 or newer, because `anthropic` 2.x doesn't install on 3.9.

## Two nights (counterfactual replay)

`[ Two nights ]` on the time bar replays tonight twice in about 35 seconds, side by side. The same pump trip and the same water run once with nobody acting and once with the plan.

- **The plan side** uses the approved plan, or else the cheapest plan that keeps every well online. Its "start by" minute comes from Last Call, matched by the plan's asset IDs.
- **The 3D tanks** show the plan's water. A red x-ray column and waterline show where the water goes if nobody acts. The waterline pulses once it reaches shutdown, and it only shows where the two nights differ.
- **The two ledgers** count dollars and wells online minute by minute, and log events as the clock passes them: the shutdown and wells lost on one side, each plan step, the Last Call minute and the closest call on the other.
- **Every final number** is the backend forecast's own `cost.total_usd`. The running counters follow the forecast's events (wells lost, chokes, first truck), scaled so they land on those totals.
- **Controls:** playback slows near each event. Click the bar to jump, press Space to pause, press Esc to close. Closing restores your what-ifs and time. The end card can approve the plan with hold-to-approve. With reduced motion, it opens on the end.
- **Code:** everything lives in `src/twonights.js`. main.js passes hooks to `mountTwoNights()` and adds nothing else.

## Scenery toggle and frame-rate guard (#88)

The desert layer (one `SCENERY` root, about 115k triangles) gets a **Scenery on/off** toggle in the model note. A click is remembered on this browser.

After the field loads, the console measures its own frame rate. Under 45 fps it steps quality down, cheapest visual loss first: pixel density 2x to 1.5x, then 1x, then scenery off. It stops as soon as the field is smooth, and the model note says what changed. A step that doesn't raise the frame rate by 10% is undone and the guard stops, because then the bottleneck is a busy CPU, not pixels or triangles. URL overrides: `?scenery=on|off` and `?quality=full` (skip the guard, for recording video).
