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
