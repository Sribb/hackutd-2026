# The contract

This is what lets us work in parallel. The Blender model, the web console, and the backend all use the same IDs and the same API.

If you need to change something here, change this file in the same commit and tell the team.

## Asset IDs

The field below is a placeholder until we have sponsor data. Change the numbers if needed, but keep the naming pattern.

| Asset | IDs | Notes |
|---|---|---|
| Tanks | `TANK_1` to `TANK_6` | |
| Tank fill meshes | `TANK_1_LEVEL` to `TANK_6_LEVEL` | Scaled by the web console to show the level |
| Transfer pumps | `PUMP_1` to `PUMP_6` | One per tank |
| Discharge valves | `VALVE_D_1` to `VALVE_D_6` | One per pump |
| Crossover valve | `VALVE_XO_34` | Connects Tank 3 and Tank 4 |
| Wells | `WELL_1` to `WELL_24` | 4 per tank |
| Disposal wells | `SWD_1`, `SWD_2` | |
| Trucks | `TRUCK_1`, `TRUCK_2` | |
| Truck yard | `YARD_1` | |

**Tonight's failure:** `PUMP_3` trips at 1:47 AM.

## Blender rules

The web console finds objects by name, so names matter more than anything else here.

1. **The object name is the asset ID**, spelled exactly as above.
2. **No `.001` suffixes.** Blender adds them to duplicates, and they break name matching. Check before every export.
3. **Assets with many parts:** Put them under one parent Empty named with the ID, like `PUMP_3`. Clicking a child will select the parent. Children can use names like `PUMP_3_motor`.
4. **Tank fill meshes:** Set the origin at the tank floor and model them at full height, so scaling Y from 0 to 1 goes from empty to full. The tank shell has to let the fill be seen, either with a cutaway or a see-through material.
5. **Scale:** 1 Blender unit is 1 meter. Apply transforms before exporting, except where an origin has to stay put for rule 4.
6. **Color:** Model everything in neutral gray and steel. The web console adds status color. Don't paint status into the model.
7. **Materials:** Use Principled BSDF, because other node setups may not export correctly. Keep textures at 2K or below.
8. **Budget:** Aim for under 300,000 triangles and under 30 MB so it runs smoothly on a laptop at the expo.

**Exporting:** File, Export, glTF 2.0. Use the glTF Binary (`.glb`) format, keep the default +Y Up, and turn Apply Modifiers on. Leave Draco compression off at first.

Save the source as `blender/field.blend` and the export as `blender/field.glb`.

**Push a rough version first.** Plain boxes with the correct names are enough for the web console to start. Then improve the quality without changing the names.

## What's in field.glb

A generator builds the field from a blueprint, so anyone can rebuild it, and a client's own field comes out of the same code. Run this from the repo root:

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup \
  --python blender/build_field.py -- --blueprint path/to/blueprint.json --out path/to/dir
```

- With no `--blueprint`, it builds `scenario/field.json` into `blender/`. That is the committed `blender/field.glb`, with the IDs in the table above.
- With `--blueprint` and no `--out`, it writes to `blender/out/<blueprint name>/`, which git ignores.
- It writes `field.glb`, `field.blend`, and `build_report.json`. A blueprint with errors exits with code 1 and writes only the report, never a glb. A crash or a failed self-check exits with code 2.
- `--detail full|lite|bare` forces a detail level. `--render-dir DIR` saves Workbench check renders.
- `python3 blender/test_generator.py` builds the samples in `blender/samples/` and checks every glb. `blender/preview/index.html` opens any `field.glb` in a browser; how to serve it is at the top of the file.

Change the model in the scripts, not by hand in the `.blend`, or the next rebuild will overwrite your edits:
- `blender/blueprint.py` reads and checks the blueprint.
- `blender/layout.py` decides where every asset goes.
- `blender/field_build.py` and `blender/piping.py` build the field and its pipes.
- `blender/field_parts.py` builds each core asset.
- `blender/detail_equipment.py` adds gauges, sensors, panels, wellhead trees, labels, and load-outs.
- `blender/detail_site.py` adds power, light towers, containment, disposal well and yard equipment, and truck detail.
- `blender/names.py` lists the names every build must contain. Each build checks itself against it before it exports.

### The blueprint

A blueprint is `scenario/field.json` with optional positions. The generator reads only these fields. The backend reads the rest.

| Field | What the generator does with it |
|---|---|
| `tanks[]`: `id`, `capacity_bbl` | One pad per tank. The painted number is the ID without `TANK_`, and the shell reads `<capacity_bbl> BBL`. |
| `pumps[]`: `id`, `tank_id`, `valve_id` | Up to 2 pumps per tank, each with its discharge valve. |
| `valves[]`: `id`, `kind`, `pump_id`, `connects` | A `DISCHARGE` valve sits on its pump, found from the pump's `valve_id`, the valve's `pump_id`, or `connects[0]`. A `CROSSOVER` joins the two tanks in `connects`. |
| `wells[]`: `id`, `tank_id` | Up to 8 wells per tank, each piped to its tank. |
| `disposal_wells[]`: `id` | 1 to 4, fed by the trunk line. |
| `trucks[]`: `id`, `home_id` | Up to 6, parked in the yard. |
| `yards[]`: `id` | One yard, or none. |
| `x`, `y` on any asset | Optional position in meters. Assets without one are placed automatically. Valves always sit on their pipe, so their `x` and `y` are ignored with a warning. |
| `connections` | Checked for unknown IDs, not drawn yet. |

The generator never invents an asset ID: every asset node in the glb is a blueprint ID. These stop the build with exit code 1: an ID that doesn't match `^[A-Z][A-Z0-9_]*$`, a duplicate ID, a reference to an unknown ID (`tank_id`, `valve_id`, `swd_id`, `pump_id`, `connects`, `home_id`, or a `minutes_to` key), a crossover that doesn't join two tanks, a discharge valve that no pump uses, and a size outside 1 to 20 tanks, 0 to 8 wells per tank, 0 to 2 pumps per tank, 4 crossovers per tank, 1 to 4 disposal wells, 0 to 6 trucks, or 0 to 1 yards.

### Coordinates and auto-layout

Positions are plan meters. The lease road runs along x on y = 0, and y points north. Blender uses the same X and Y with Z up, so three.js gets `(x, height, -y)`. Assets without a position are laid out like today's field:

- Pads sit in a row north of the road, 55 m apart and centered on x = 0, and the road grows with the row. With more than 8 tanks they go in two rows. The second row sits south of the road, turned to face it, with the yard in the middle slot.
- Each pad has its tank 46 m from the road, its pumps between the tank and the road, and the load-out stand and `STOP_<tank>` beside the driveway. Its wells sit behind the tank, in one arc for up to 4 wells and two staggered rows for 5 to 8.
- Disposal wells sit at the road ends. The first goes at the west end north of the road, the second at the east end, then the south side of the west and east ends.
- The yard and its trucks sit south of the road at the center.
- The trunk line runs along the road's north shoulder through every pump's tie-in and dips under each driveway. Tie-ins from south pads cross under the road. A disposal well at a trunk end feeds in as today, and any other disposal well gets a branch.
- A crossover between neighbors in a row is a straight run between the tanks, like `VALVE_XO_34`. Any other pair runs on a pipe rack behind the tanks, with its valve beside the first tank in `connects`. When the tanks face each other across the road, the rack crosses under it in a gap between pads.
- A tank with `x` and `y` has its center there, and its pad faces the road. Keep `|y|` at 30 m or more so the pad clears the road.

### The report

`build_report.json` says what was built, or why nothing was:

```json
{
  "ok": true, "errors": [], "warnings": [],
  "blueprint": "scenario/field.json",
  "counts": { "tanks": 6, "pumps": 6, "discharge_valves": 6, "crossover_valves": 1, "wells": 24, "disposal_wells": 2, "trucks": 2, "yards": 1 },
  "kinds": { "disposal_well": 2, "pump": 6, "tank": 6, "truck": 2, "valve": 7, "well": 24, "yard": 1 },
  "placed": { "given": [], "auto": ["PUMP_1", "..."] },
  "layout": { "rows": 1, "road_m": 450.0, "crossovers": { "VALVE_XO_34": "straight" } },
  "detail": { "level": "full", "dropped": [] },
  "triangles": 144230, "glb_mb": 6.0, "nodes": 776, "seconds": 1.9
}
```

`counts` comes from the blueprint and `kinds` from the built scene. `placed` lists which assets had positions in the blueprint and which were placed automatically. When `ok` is false, `errors` says what to fix.

**Names in this section:** `<tank>`, `<pump>`, `<valve>`, `<well>`, `<swd>`, `<truck>`, and `<yard>` are blueprint IDs, and `n` is a tank ID without its `TANK_` prefix, so `TANK_3` gives `PAD_3`. For today's field they are the same names as before.

**Finding what was clicked:** Every asset is an empty node named with its ID. Static parts are child meshes, like `TANK_3_tank` and `TANK_3_steel`. Walk up from the clicked mesh until a node has `userData.asset_id`. Three.js puts glTF extras in `userData`, and `userData.kind` is `tank`, `pump`, `valve`, `well`, `disposal_well`, `truck`, or `yard`.

**Moving parts:** Each one has its origin at its pivot, with no rotation in the file. Animate it with one property:

| Node | What it does | How to animate it in three.js |
|---|---|---|
| `<tank>_LEVEL` | Brine level inside the cutaway tank | `scale.y` from 0 (empty) to 1 (full). The file ships at 0.55 for preview. |
| `<pump>_rotor` | Motor fan, so you can see the pump running | Spin `rotation.x` while running, stop when failed |
| `<valve>_wheel` | Handwheel on every valve | Turn `rotation.y` while the valve opens or closes |
| `<well>_beam` | Pumpjack walking beam | Rock `rotation.x` by about ±0.25 rad while the well produces |
| `<well>_crank` | Pumpjack crank and counterweights | Spin `rotation.x` while the well produces, stop when shut in |
| `<tank>_gauge` | Float marker on the outside level board | `position.y = 0.3 + 4.3 × level` (0 to 1), in the tank's local space |
| `<truck>_axle_1` to `_3` | Wheel pairs | Spin `rotation.z` while the truck drives |
| `<swd>_gate` | Swinging gate in the fence | `rotation.y` 0 is closed, 1.31 rad is open. It ships open. |

Assets on the south side of the road are turned 180 degrees about Y. Their parts still animate the same way, because each property is in the part's local space.

**Lights:** These meshes use the `M_LAMP` material. Set the emissive color to light them by status:
- `<pump>_light` and `<well>_light` are the status lights on each pump panel and well controller.
- `<truck>_beacon` is the amber beacon for a truck on a job, and `<truck>_headlights` are its head and tail lights.
- `PAD_n_lamps` and `<swd>_lamps` are the light tower heads.

**Sensors:** `<tank>_lt` (radar level) and `<tank>_lshh` (high-level switch) are separate meshes, so the console can highlight the sensor behind an alarm. Clicking them selects the tank.

**Pipes:** Every pipe is its own mesh named for what it connects, with `userData.pipe_id`:

| Pipes | Connects |
|---|---|
| `PIPE_<well>_<tank>` | A well's flowline to its tank's inlet header |
| `PIPE_INLET_n`, `PIPE_INLET_n_RISER` | The inlet header and the riser into the top of the tank |
| `PIPE_<tank>_<pump>` | The tank's outlet to a transfer pump |
| `PIPE_<pump>_<valve>`, `PIPE_<valve>_TRUNK` | The pump through its discharge valve to the trunk. A pump with no valve has `PIPE_<pump>_TRUNK` instead. |
| `PIPE_TRUNK_0` to `PIPE_TRUNK_k` | The trunk line, west to east, between the end disposal wells and each pump's tie-in. Today that is `PIPE_TRUNK_0` to `PIPE_TRUNK_6` between `SWD_1` and `SWD_2`. |
| `PIPE_TRUNK_<swd>` | A branch from the trunk to a disposal well that is not at a trunk end |
| `PIPE_<xo>_W`, `PIPE_<xo>_E` | A crossover on each side of its valve, where `<xo>` is the valve ID without `VALVE_`. `_W` joins the more westerly tank. Today: `PIPE_XO_34_W` and `PIPE_XO_34_E`. |
| `PIPE_<tank>_LOADOUT_n` | The tank's load-out line to the truck hookup |

**Truck routes:** Empty nodes mark the road, so the console can drive a truck along it:
- `STOP_<tank>`, `STOP_<swd>`, and `STOP_<yard>` are where a truck parks. `STOP_<tank>` sits beside `LOADOUT_n`, the stand where a truck hooks up to drain the tank.
- `ROAD_JCT_<tank>`, `ROAD_JCT_<swd>`, and `ROAD_JCT_<yard>` are where each of those driveways meets the lease road. The road runs straight along X on plan y = 0 between `ROAD_END_W` and `ROAD_END_E`.
- A route is the start stop, its junction, along the road, the target's junction, then the target stop.

**Other names:** None of these are clickable assets:
- `PAD_n` holds each tank pad's gravel, steel containment, liner, step-over, sign, and light tower.
- `LOADOUT_n` is the truck hookup stand.
- `SITE_*` is the ground, roads, pipe supports, and the power line.
- `CAM_OVERVIEW` is a camera framing the whole field.
- Names ending in `_label`, `_sign`, or `_tag` are painted numbers and signs.

**Size:** Today's field is about 144,000 triangles and 6 MB, and the 12-tank sample is about 279,000 triangles and 11.5 MB, inside the 300,000 triangle and 30 MB budget. The generator estimates the size before it builds. If a field has more than 12 tanks, or full detail would pass the budget, it drops small detail: bolts, small labels, tags, signs, and containment and fence posts, then wellhead trim if that is still too much. `detail` in the report says what was dropped. Asset nodes, tank numbers (`<tank>_label`), lights, sensors, moving parts, pipes, and waypoints are never dropped.

**Scale and axes:** Today's field runs 450 m along X, and the report gives `layout.road_m` for any field. In three.js, Y is up and the lease road runs along X near Z = 0, with the first row of tank pads toward negative Z and a second row, if any, toward positive Z.

## Status colors

The backend sends the status. The web console picks the color.

| Status | Meaning | Look |
|---|---|---|
| `OK` | Normal | Gray, no color |
| `WARNING` | Needs attention soon | Amber |
| `ALARM` | Failed or at risk now | Red |
| `OFFLINE` | Stopped or shut in | Dark |
| `UNKNOWN` | No fresh reading (live layer only). Never treat as safe | Hatched or gray with a "?" |

## API

The default server address is `http://127.0.0.1:8000`. All numbers below are examples. Times are minutes from now.

### `GET /api/field`

Everything that doesn't change during the night.

```json
{
  "tanks": [{ "id": "TANK_3", "capacity_bbl": 1000, "shutdown_pct": 90 }],
  "pumps": [{ "id": "PUMP_3", "tank_id": "TANK_3", "rate_bbl_hr": 150 }],
  "valves": [{ "id": "VALVE_XO_34", "connects": ["TANK_3", "TANK_4"] }],
  "wells": [{ "id": "WELL_9", "tank_id": "TANK_3", "water_bbl_hr": 20, "lost_usd_hr": 300 }],
  "disposal_wells": [{ "id": "SWD_1", "max_bbl_hr": 600 }],
  "trucks": [{ "id": "TRUCK_1", "capacity_bbl": 130, "minutes_to": { "TANK_3": 35 } }]
}
```

### `GET /api/state`

The current readings and any active incident.

```json
{
  "clock": "01:47",
  "assets": {
    "TANK_3": { "status": "ALARM", "level_pct": 71.2 },
    "PUMP_3": { "status": "ALARM", "reason": "Pump tripped. No flow since 1:47 AM." },
    "VALVE_XO_34": { "status": "OK", "position": "CLOSED" }
  },
  "incident": { "root_asset_id": "PUMP_3", "summary": "Pump 3 failed. Tank 3 is filling." }
}
```

### `POST /api/forecast`

**This is the most important endpoint.** Send a list of actions and get back the future. An empty list means "do nothing." It must be fast enough to feel instant when clicking.

Request:

```json
{
  "horizon_minutes": 360,
  "step_minutes": 5,
  "actions": [
    { "type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "at_min": 5 },
    { "type": "CHOKE_WELL", "asset_id": "WELL_9", "value": 0.5, "at_min": 10 },
    { "type": "SEND_TRUCK", "asset_id": "TRUCK_1", "target_id": "TANK_3", "at_min": 0 }
  ]
}
```

Action types: `SET_VALVE`, `SET_PUMP`, `CHOKE_WELL`, `SEND_TRUCK`.

Response:

```json
{
  "timeline": [
    { "t_min": 0, "tanks": { "TANK_3": 71.2 }, "wells_online": 24 },
    { "t_min": 5, "tanks": { "TANK_3": 72.8 }, "wells_online": 24 }
  ],
  "events": [
    {
      "type": "SHUTDOWN_LEVEL",
      "asset_id": "TANK_3",
      "t_min": 60,
      "t_min_low": 45,
      "t_min_high": 75,
      "text": "Tank 3 reaches shutdown at 2:47 AM"
    }
  ],
  "cost": { "total_usd": 4200, "trucking_usd": 1500, "disposal_usd": 700, "lost_production_usd": 2000 },
  "warnings": []
}
```

If data is missing or stale, say so in `warnings`. **Never report a tank as safe because there's no data.**

### `POST /api/plans`

Returns the best plans, cheapest first. It must always return something, even if not every tank can be saved.

```json
{
  "plans": [
    {
      "id": "plan_1",
      "summary": "Open the Tank 3 to 4 crossover and send one truck.",
      "actions": [{ "type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN", "at_min": 5 }],
      "cost": { "total_usd": 4200 },
      "keeps_running": true,
      "cannot_save": []
    }
  ]
}
```

### `POST /api/plans/{id}/approve`

Turns a plan into the steps the 3D scene walks through.

```json
{
  "steps": [
    { "n": 1, "asset_id": "VALVE_XO_34", "text": "Open the Tank 3 to 4 crossover." },
    { "n": 2, "asset_id": "TRUCK_1", "text": "Send Truck 1 to Tank 3. Arrives in 35 minutes." }
  ],
  "radio_script": "Field, this is control. Pump 3 is down..."
}
```

## Live layer (#38)

Additive. The endpoints above don't change. The live layer reads signals from **connectors** (the blueprint's optional `connectors` list, see [BLUEPRINT.md](BLUEPRINT.md); with none, the simulator drives every asset), flags problems from the readings, and lets Jarvis and the console **propose** commands that do nothing until a person confirms them.

The live clock starts at the blueprint's `clock_start` and runs at `FREEBOARD_SPEED` live minutes per real second (default 0.25). Scripted `faults` fire at their `at_min`.

### `GET /api/live/state`

```json
{
  "clock": "01:52",
  "t": 8.0,
  "assets": {
    "TANK_3": { "status": "ALARM", "level_pct": 72.6, "stale": false, "age_min": 0, "last_reading_t": "2026-11-07T07:12:03+00:00", "reason": null, "observed_bbl_hr": 182.0, "expected_bbl_hr": -40.0 },
    "TANK_5": { "status": "UNKNOWN", "level_pct": 60.1, "stale": true, "age_min": 6.5, "last_reading_t": "2026-11-07T07:11:01+00:00", "reason": "No level_pct reading from Tank 5 for 7 min. Treat it as unknown, not safe." },
    "PUMP_3": { "status": "ALARM", "running": false, "flow_bbl_hr": 0, "stale": false, "age_min": 0, "last_reading_t": "...", "reason": "Pump 3 tripped: it reports stopped. Tank 3 is no longer being emptied." },
    "VALVE_XO_34": { "status": "OK", "position": "CLOSED", "stale": false, "age_min": 0, "last_reading_t": "..." },
    "WELL_9": { "status": "OK", "flow_bbl_hr": 50, "stale": false, "age_min": 0, "last_reading_t": "..." },
    "TRUCK_1": { "status": "OK", "phase": "EN_ROUTE", "target_id": "TANK_3", "stale": false, "age_min": null, "last_reading_t": null }
  },
  "connectors": [
    { "id": "CONN_SIM", "kind": "sim", "status": "connected", "detail": "simulating 47 assets" },
    { "id": "CONN_SCADA", "kind": "mqtt", "status": "unavailable", "detail": "paho-mqtt is not installed (pip install paho-mqtt)" }
  ],
  "speed": 0.25,
  "paused": false
}
```

- `status` is `OK`, `WARNING`, `ALARM`, `OFFLINE`, or `UNKNOWN`. A tank with no fresh level reading is `UNKNOWN`, never `OK`.
- `stale` is true when the asset's readings are older than `sensors.stale_after_min` (default 5). `age_min` is live minutes since its newest reading (null if never). `last_reading_t` is that reading's ISO time (null if never). Trucks have no sensors, so their age is null.
- Readings per kind: tanks `level_pct`; pumps `running`, `flow_bbl_hr`; valves `position`; wells and disposal wells `flow_bbl_hr`; trucks `phase` (`IDLE`, `QUEUED`, `EN_ROUTE`, `LOADING`, `TO_DISPOSAL`) and `target_id`. A value is null when nothing has been read.
- `connectors` always starts with the simulator, `CONN_SIM`. Connector `kind` is `sim`, `webhook`, `mqtt`, `modbus`, or `opcua`; `status` is `connected` or `unavailable`.

### Alerts

`GET /api/alerts` → `{"alerts": [alert]}`, oldest first. `POST /api/alerts/{id}/ack` marks one acknowledged (the phone notification button calls it).

```json
{ "id": "alert_1", "asset_id": "PUMP_3", "kind": "PUMP_TRIP", "severity": "ALARM",
  "message": "Pump 3 tripped: flow reads 0 bbl/hr, it should be moving 150. Tank 3 is filling.",
  "t": "2026-11-07T07:12:03+00:00", "clock": "01:48", "active": true }
```

| `kind` | `severity` | Raised when |
|---|---|---|
| `PUMP_TRIP` | `ALARM` | The pump reports `running` false, or its flow is under 10% of its rate while it should run, or its tank rises faster than its wells explain. Confirmed over 2 readings, so it alerts within 3. |
| `SENSOR_STALE` | `WARNING` | No reading of a signal for `stale_after_min`. |
| `VALVE_MISMATCH` | `WARNING` | A valve's reported position differs from the position a person confirmed, for 2 readings after a 1 minute grace. |
| `RUN_PAUSED` | `ALARM` | A plan run paused itself: a step's effect wasn't seen, a tank is rising faster than forecast, or a step was rejected or failed. `asset_id` is that step's asset or tank. The message names the re-plan run once Jarvis has one. Resolves when the run resumes, is replaced, or is cancelled. See [Plan runs](#plan-runs-63). |
| `RUN_FAILED` | `ALARM` | A tank reached shutdown during a plan run. `asset_id` is the tank. |

`t` is when it was raised (ISO). When the condition clears, the same alert is sent again with `"active": false` and `resolved_t`.

### Actions (human-confirmed)

A proposal changes nothing. Only confirm sends it, through the connector that owns the asset (the simulator when none does).

```json
{ "id": "act_1",
  "action": { "type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN" },
  "reason": "Tank 3 reaches shutdown at 2:47 AM; the crossover moves water to Tank 4.",
  "status": "PENDING", "created_t": "2026-11-07T07:12:10+00:00",
  "text": "Open the Tank 3 to 4 crossover.", "source": "jarvis", "connector_id": "CONN_SIM" }
```

- `action.type`: `SET_VALVE` (value `OPEN`/`CLOSED`), `SET_PUMP` (`ON`/`OFF`), `CHOKE_WELL` (0 shut in to 1 untouched), `SEND_TRUCK` (value null, plus `target_id`, a tank).
- `status`: `PENDING`, then `CONFIRMED`, `REJECTED`, or `FAILED` (the connector refused or was unreachable). After a decision the proposal also has `result` (what the connector said), `decided_t`, and `decided_by`.
- `source` is `console`, `jarvis`, or `run`. A plan run's step also has `run_id` and `step` (its `n`). It is confirmed and rejected like any other proposal.
- `GET /api/actions` → `{"proposals": [proposal]}`.
- `POST /api/actions` with `{type, asset_id, value, target_id?, reason?}` → a PENDING proposal (422 if the action is invalid).
- `POST /api/actions/{id}/confirm` and `POST /api/actions/{id}/reject` (optional body `{"by": "name"}`) → the updated proposal. 404 for an unknown id, 409 if it isn't PENDING.

### `POST /api/jarvis`

Request: `{"message": "What am I looking at?", "selected_asset_ids": ["PUMP_3"], "image_png_base64": "<optional screenshot of the 3D view>"}`

Response 200:

```json
{ "reply": "Pump 3 is stopped, moving 0 barrels an hour. Pump 3 tripped: it reports stopped.",
  "source": "rules",
  "ui": [{ "type": "ui", "action": "focus", "asset_id": "PUMP_3" }],
  "proposals": [],
  "runs": [] }
```

- `source` is `rules` (instant answers that need no API key: what's failing, how long until shutdown, show or explain an asset, what am I looking at with a selection, the cheapest plan, "fix it", and fixes like "open the crossover", "send truck 1 to tank 3", "shut in well 9", "turn off pump 2") or `claude`.
- Jarvis's fixes are proposals (PENDING) in `proposals`. It never changes equipment itself.
- "Fix it" (also "run the plan", "make it so") starts a [plan run](#plan-runs-63) of the cheapest plan that keeps every tank below shutdown, preferring one that holds with inflow 15% high. The run is in `runs`; each of its steps still waits for a person. With a key, Claude does the same through its `execute_plan` tool. If a run is already RUNNING, the reply says so and nothing new starts.
- With no `ANTHROPIC_API_KEY` and no rules match: **503** `{"detail": "Jarvis needs ANTHROPIC_API_KEY on the server."}`. Other Claude failures are also 503 with a `detail` saying why.

### Connectors and demo controls

- `GET /api/live/connectors` → `{"connectors": [...], "jarvis": {"mode", "model", "llm_available"}, "push": {"enabled", "last_error"}}`.
- `POST /api/live/readings/{connector_id}` (webhook connectors only) with `{"readings": [{"asset_id", "signal", "value"} | {"address", "value"}]}` → `{"accepted": [...], "rejected": [...]}`. Only signals the blueprint declares for that connector are accepted. If the connector has `token_env`, send the token in `X-Freeboard-Token`.
- `POST /api/live/sim` with `{speed?, paused?, advance_min?}` → the live clock.
- `POST /api/live/faults` with `{asset_id, type: TRIP | STUCK_VALVE | SENSOR_STALE | LEAK | REPAIR, reason?, rate_bbl_hr?}` injects a fault into the simulator.

### Plan runs (#63)

A run executes a plan from `POST /api/plans` (or from Jarvis) step by step. **Every step is an ordinary proposal that waits for a person to confirm it**; the run never confirms anything. `JARVIS_CONFIRM=0` auto-confirms only steps whose command goes to the simulator.

1. The plan's actions are sorted by `at_min`. Each step is released as a PENDING proposal (`source: "run"`, with `run_id` and `step`) when the live clock is `at_min` minutes past the run's start. The simulator applies a command the moment it's confirmed, so releasing at the plan's lead time keeps the field on the plan's timing.
2. A person confirms (or rejects) it with `POST /api/actions/{id}/confirm`. It goes through the asset's connector.
3. The run then looks for the effect in the live readings: the valve's `position` reads the commanded value (within 2 live min), the pump's `running` matches ON/OFF (2 min), the well's flow is within 10% of design × choke (3 min), the truck is no longer `IDLE` (1 min). It also replays exactly what was confirmed, at the times it was confirmed, and watches every tank with a fresh level.
4. If an effect isn't seen in time (for example a stuck valve still reads CLOSED), a tank runs more than 3 points above the replay for 1 live minute (rising faster than forecast), or a step is rejected or fails at its connector, the run **pauses**, raises a `RUN_PAUSED` alert, and asks Jarvis for a **re-plan** from the current state. The re-plan avoids the failed asset (and any valve with an active `VALVE_MISMATCH`), counts wells at their live flow, and doesn't resend trucks already on the road (it counts their loads). With no API key Jarvis uses the rules pick (cheapest plan that keeps every tank running and holds with inflow 15% high); with one, Claude chooses among the same planner plans, and `replan_source` says which. The re-plan is offered as a **new run** that is PAUSED and not started (`started: false`). `POST /api/runs/{new}/resume` starts it and cancels the paused run.
5. When every step is finished the run is `DONE`. A tank reaching shutdown during a run makes it `FAILED` (alert `RUN_FAILED`).

Only one run is RUNNING at a time. Starting a run cancels any other PAUSED run.

```json
{ "id": "run_1", "plan_id": "plan_867eb029",
  "summary": "Open the Tank 3 to 4 crossover, send Truck 1 to Tank 3, and send Truck 2 to Tank 3. Every tank stays below shutdown through 7:48 AM. Not enough if inflow runs 15% high.",
  "cost_usd": 664, "robust": false, "keeps_running": true,
  "status": "PAUSED", "source": "console", "created_t": "2026-11-07T07:12:10+00:00", "created_by": "Sriyan",
  "started": true, "start_t": 4.0, "start_clock": "01:48",
  "steps": [
    { "n": 1, "action": { "type": "SEND_TRUCK", "asset_id": "TRUCK_1", "value": null, "target_id": "TANK_3" },
      "at_min": 5.0, "release_clock": "1:53 AM", "text": "Send Truck 1 to Tank 3.",
      "proposal_id": "act_1", "status": "DONE", "confirmed_t": 9.0,
      "result": "TRUCK_1 dispatched to TANK_3, arriving in 35 min.", "verify": "Truck 1 is en route." },
    { "n": 3, "action": { "type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN" },
      "at_min": 20.0, "release_clock": "2:08 AM", "text": "Open the Tank 3 to 4 crossover.",
      "proposal_id": "act_3", "status": "MISMATCH", "confirmed_t": 24.0,
      "result": "Sent to VALVE_XO_34. (Simulated fault: the valve is stuck and won't move.)",
      "verify": "The Tank 3 to 4 crossover still reads CLOSED 2 minutes after it was commanded OPEN." }
  ],
  "reason": "The Tank 3 to 4 crossover still reads CLOSED 2 minutes after it was commanded OPEN.",
  "note": null, "replaces": null, "replan_run_id": "run_2", "replan_source": "rules",
  "excluded": ["VALVE_XO_34"], "alert_id": "alert_3", "ended_t": null }
```

- Run `status`: `RUNNING`, `PAUSED`, `DONE`, `FAILED`, `CANCELLED`. `reason` says why it's paused or ended. `start_t` is live minutes (null until started); `note` is Jarvis's one-line reason for a re-plan chosen by Claude.
- Step `status`: `WAITING` (not released yet), `PENDING` (waiting for a person), `VERIFYING` (confirmed, effect not seen yet), `DONE`, `MISMATCH` (effect not seen in time; `verify` says what the readings show), `REJECTED`, `FAILED` (the connector refused), `CANCELLED`.
- `POST /api/plans/{id}/execute` (optional body `{"by": "name"}`) → the new run, RUNNING. Plan ids come from `POST /api/plans` or from Jarvis's plans (ids are deterministic, so an unknown id is looked up again from the live state). 404 for an unknown plan, 409 if another run is RUNNING, 422 if a step can't apply to this field.
- `GET /api/runs` → `{"runs": [run]}`, oldest first. `GET /api/runs/{id}` → one run (404 if unknown).
- `POST /api/runs/{id}/pause` (RUNNING → PAUSED: nothing new is released; steps already PENDING stay confirmable). `POST /api/runs/{id}/resume` (PAUSED → RUNNING: starts an offered re-plan, or continues a paused run, releasing overdue steps now and treating a failed step as skipped). `POST /api/runs/{id}/cancel` (any active run → CANCELLED: waiting steps are cancelled and pending proposals rejected, so nothing more from it can reach equipment). Optional body `{"by": "name"}`. 404 for an unknown run, 409 if the run isn't in the right status.

### `WS /ws`

Connect to `ws://127.0.0.1:8000/ws?role=web` (`role` is `web`, `phone`, `device`, or `other`). Every message is one JSON object with a `type` and a `seq` counter:

| `type` | Payload | When |
|---|---|---|
| `state` | `{"type": "state", "seq": 1, "state": <GET /api/live/state body>}` | On connect, every tick, and after each confirm |
| `alert` | `{"type": "alert", "seq": 2, "alert": <alert>}` | Raised or resolved |
| `proposal` | `{"type": "proposal", "seq": 3, "proposal": <proposal>}` | Created, confirmed, rejected, or failed |
| `ui` | `{"type": "ui", "seq": 4, "action": "focus", "asset_id": "PUMP_3", "reason": "...", "source": "jarvis"}` | Jarvis, a new ALARM, a proposal, or a phone wants every screen on an asset |
| `run` | `{"type": "run", "seq": 5, "run": <run>}` (`run.steps[]` has `proposal_id`, `action`, `status`) | A [plan run](#plan-runs-63) is created, or its status, a step's status, or its re-plan changes |

Clients may send `{"type": "ping"}` (answered with `pong`) and `{"type": "ui", "action": "focus", "asset_id": "TANK_3"}` to point every console at an asset.

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables Claude in Jarvis. Without it, only the rules answer. |
| `JARVIS_MODEL` | `claude-sonnet-5-5` | Claude model for Jarvis. |
| `JARVIS_MODE` | `auto` | `auto` (rules first, Claude for the rest), `rules`, or `llm`. |
| `JARVIS_CONFIRM` | `1` | `0` lets Jarvis's proposals and plan-run steps run without a person, **only** for the simulator (rehearsals). Real equipment always waits for confirm. |
| `FREEBOARD_SPEED` | `0.25` | Live minutes per real second. |
| `NTFY_TOPIC` | unset | Phone push of ALARM and WARNING alerts through ntfy. Off when unset. |
| `NTFY_SERVER` | `https://ntfy.sh` | ntfy server. |
| `PUBLIC_URL` | unset | Where a phone can reach this server, for the notification's buttons. |
| `CORS_ORIGINS` | unset | Extra allowed origins, comma separated. Localhost is always allowed. |
| `*_env` in connector config | | Names the variable holding a connector secret, e.g. `"token_env": "HOOK_TOKEN"`. |
