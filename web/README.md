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
