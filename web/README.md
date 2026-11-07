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

## Data

`src/api.js` reads the API in the `docs/CONTRACT.md` shape. Without `VITE_API`, or if the server is down, it uses `public/fake/*.json` and shows a **FAKE DATA** badge. The fake files were generated from the Last Call placeholder scenario (PR #10), so their numbers match it: Tank 3 shuts down at 2:50 AM.

## Debug

`window.__freeboard` in the browser console has `assets` (ID to meshes), `select(id)`, `viewAt(t)`, and `missing` (contract IDs not found in the model).

## Next

1. What-if: clicking a valve, pump, or truck posts `/api/forecast` with that action and redraws the timeline.
2. Plans panel from `/api/plans`, and the approve walk-through, where the camera moves to each step's asset.
3. Last Call fuse rings from `/api/lastcall` `assets[id].useful_until_min`.
