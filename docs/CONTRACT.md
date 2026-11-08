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
- `--detail full|lite|bare` forces a detail level. `--render-dir DIR` saves Workbench check renders. `--no-scenery` builds the plain field, with no `SCENERY` layer and flat ground and road colors.
- `python3 blender/test_generator.py` builds the samples in `blender/samples/` and checks every glb. `blender/preview/index.html` opens any `field.glb` in a browser; how to serve it is at the top of the file.

Change the model in the scripts, not by hand in the `.blend`, or the next rebuild will overwrite your edits:
- `blender/blueprint.py` reads and checks the blueprint.
- `blender/layout.py` decides where every asset goes.
- `blender/field_build.py` and `blender/piping.py` build the field and its pipes.
- `blender/field_parts.py` builds each core asset.
- `blender/detail_equipment.py` adds gauges, sensors, panels, wellhead trees, labels, and load-outs.
- `blender/detail_site.py` adds power, light towers, containment, disposal well and yard equipment, and truck detail.
- `blender/siting.py` finds free spots for anything added after the core layout, and `blender/equipment_parts.py` builds the blueprint's `equipment`.
- `blender/scenery_parts.py` generates the scenery the CC0 kit lacks: barbed wire fence, yucca, prickly pear, and a cattle guard. `blender/parts_preview.py` builds every generated model in a row to look at.
- `blender/scenery.py` dresses the built field as desert with the kit in `blender/assets/` and those parts, and `siting.plan_scenery()` decides where each piece goes.
- `blender/names.py` lists the names every build must contain. Each build checks itself against it before it exports.

### The blueprint

A blueprint follows [BLUEPRINT.md](BLUEPRINT.md), format v1. The generator reads only these fields, and the backend reads the rest. The generator needs only IDs and links, so an ingest draft with missing numbers builds too. Keys that start with `_` are ignored everywhere. `site` and `connectors` never stop a build: a malformed one is a warning.

| Field | What the generator does with it |
|---|---|
| `tanks[]`: `id`, `capacity_bbl` | One pad per tank. The painted number is the ID without `TANK_`, and the shell reads `<capacity_bbl> BBL`. |
| `pumps[]`: `id`, `tank_id`, `valve_id` | Up to 2 pumps per tank, each with its discharge valve. |
| `valves[]`: `id`, `kind`, `pump_id`, `connects` | A `DISCHARGE` valve sits on its pump, found from the pump's `valve_id`, the valve's `pump_id`, or `connects[0]`. A `CROSSOVER` joins the two tanks in `connects`. |
| `wells[]`: `id`, `tank_id` | Up to 8 wells per tank, each piped to its tank. |
| `disposal_wells[]`: `id` | 1 to 4, fed by the trunk line. |
| `trucks[]`: `id`, `home_id` | Up to 6, parked in the yard. |
| `yards[]`: `id` | One yard, or none. |
| `equipment[]`: `id`, `type`, `label`, `tank_id`, `connects` | Up to 30. `SEPARATOR`, `HEATER_TREATER`, `FLARE`, `COMPRESSOR`, `LACT`, and `METER_RUN` get a generated model. Any other type, or none, builds as a labeled placeholder box, so a blueprint never fails over a missing model. Without `x` and `y`, an item with a `tank_id` goes beside that tank's pad, one with `connects` goes near the first asset it names, and the rest go near the middle of the field, always off pads, roads, pipes, and truck routes. |
| `x`, `y` on any asset | Optional position in meters. Assets without one, or with a null `x` and `y`, are placed automatically. Valves always sit on their pipe, so their `x` and `y` are ignored with a warning. |
| `connections` | Checked for unknown IDs, not drawn yet. An end may be a connector ID. |
| `site`, `connectors` | Read for the report: `site` and the connector IDs appear in `build_report.json`. |

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
  "counts": { "tanks": 6, "pumps": 6, "discharge_valves": 6, "crossover_valves": 1, "wells": 24, "disposal_wells": 2, "trucks": 2, "yards": 1, "equipment": 0 },
  "kinds": { "disposal_well": 2, "pump": 6, "tank": 6, "truck": 2, "valve": 7, "well": 24, "yard": 1 },
  "placed": { "given": [], "auto": ["PUMP_1", "..."] },
  "layout": { "rows": 1, "road_m": 450.0, "crossovers": { "VALVE_XO_34": "straight" } },
  "detail": { "level": "full", "dropped": [] },
  "iot": { "sensors": 12, "rtus": 6 },
  "scenery": { "on": true, "textures": ["ground_dry_soil", "dirt_road_tracks"], "fence_m": 1216.0, "cattle_guards": 2,
               "items": { "boulder": 6, "branch": 6, "creosote": 2, "grass": 110, "lease_sign": 1, "mesquite": 1,
                          "prickly_pear": 28, "rock": 15, "yucca": 65 },
               "dropped": [], "triangles": 114650, "field_triangles": 148435, "images_mb": 9.43 },
  "triangles": 263085, "glb_mb": 19.0, "nodes": 1013, "seconds": 7.8
}
```

`counts` comes from the blueprint and `kinds` from the built scene. `scenery` says what the desert layer added: its triangles on top of `field_triangles`, the MB of its textures, and anything it `dropped` to stay in the budget. With `--no-scenery` it is `{"on": false}`. `placed` lists which assets had positions in the blueprint and which were placed automatically. When `ok` is false, `errors` says what to fix.

**Names in this section:** `<tank>`, `<pump>`, `<valve>`, `<well>`, `<swd>`, `<truck>`, and `<yard>` are blueprint IDs, and `n` is a tank ID without its `TANK_` prefix, so `TANK_3` gives `PAD_3`. For today's field they are the same names as before.

**Finding what was clicked:** Every asset is an empty node named with its ID. Static parts are child meshes, like `TANK_3_tank` and `TANK_3_steel`. Walk up from the clicked mesh until a node has `userData.asset_id`. Three.js puts glTF extras in `userData`, and `userData.kind` is `tank`, `pump`, `valve`, `well`, `disposal_well`, `truck`, `yard`, or `equipment`.

**Equipment:** each `equipment` item is an asset node named with its ID, with `userData.kind` `equipment`, `userData.equipment_type` (like `SEPARATOR`), `userData.model` (`generic` or `placeholder`), and `userData.label` when the blueprint gives one. A placeholder has its type and ID painted on top as `<id>_label`. Equipment is display and context only: it is not in `/api/field`, so the console can show its ID and type but has nothing to forecast.

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
- `SENSOR_<asset>_light` is the LED on each IoT sensor node, and `RTU_PAD_n_light` is the status light on each pad's RTU (#46). Light them for a live, stale, or lost feed.

**Sensors:** `<tank>_lt` (radar level) and `<tank>_lshh` (high-level switch) are separate meshes, so the console can highlight the sensor behind an alarm. Clicking them selects the tank.

**IoT sensor nodes (#46):** `SENSOR_<asset>` is a wireless sensor node (an enclosure, a stub antenna, and the `SENSOR_<asset>_light` LED) mounted on an asset and parented to it, so clicking it selects that asset. The assets named in the blueprint's `connectors[].signals` get one each, and with no connectors every tank and pump does. Its `userData` has `kind` `sensor`, `for_asset` (the asset ID), `signals`, and `connectors`, the last two as comma-separated text like `"running,flow_bbl_hr"` and `"CONN_SIM"` (empty with no connectors). Today's field has `SENSOR_TANK_1` to `SENSOR_TANK_6` and `SENSOR_PUMP_1` to `SENSOR_PUMP_6`.

**RTUs (#46):** `RTU_PAD_n` is the remote terminal unit on each tank pad: a cabinet on a stand with a solar panel and battery box, the `RTU_PAD_n_light` status light, and `RTU_PAD_n_antenna`, the radio mast with an omni whip and a Yagi toward the road. It is parented to `PAD_n`, and its `userData` has `kind` `rtu` and `pad`. It is not a clickable asset. Today's field has `RTU_PAD_1` to `RTU_PAD_6`.

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
- `PAD_n` holds each tank pad's gravel, steel containment, liner, step-over, sign, light tower, and `RTU_PAD_n`.
- `LOADOUT_n` is the truck hookup stand.
- `SITE_*` is the ground, roads, pipe supports, and the power line.
- `CAM_OVERVIEW` is a camera framing the whole field.
- Names ending in `_label`, `_sign`, or `_tag` are painted numbers and signs.
- `SCENERY` is the desert layer (#88): one root Empty with `userData.kind` `scenery`, and every node under it is named `SCENERY_*`. It holds the barbed wire fence along the lease edge (`SCENERY_fence_*`), the cattle guards and road stubs where the lease road leaves (`SCENERY_cattle_guard_*`, `SCENERY_road_*`), a lease sign, and a seeded scatter of plants and rocks: one node per kit model copy, like `SCENERY_grass_12`, `SCENERY_rock_3`, and `SCENERY_mesquite_1`, and one merged mesh per material for the generated plants (`SCENERY_yucca_*`, `SCENERY_prickly_pear_*`). Nothing under it has an asset ID, so it is never clickable. Hide the whole layer with `scene.getObjectByName("SCENERY").visible = false`. It stays off pads, roads, pipe runs, and truck routes, and it is the same on every build of the same blueprint. The ground (`M_GROUND`) and the road (`M_ROAD`) also get the kit's dry soil and dirt road textures; those are not under `SCENERY`.

**Size:** Today's field is about 263,000 triangles and 19 MB with its scenery, or 148,000 triangles and 6.2 MB with `--no-scenery`. The 12-tank sample is about 288,000 triangles and 17 MB. All are inside the 300,000 triangle and 30 MB budget. Scenery only uses what the field leaves: it fills up to 95% of the triangle budget, drops the fence when it can't fit, and builds only the textures when nothing is left. The generator estimates the size before it builds. If a field has more than 12 tanks, or full detail would pass the budget, it drops small detail: bolts, small labels, tags, signs, and containment and fence posts, then wellhead trim if that is still too much. `detail` in the report says what was dropped. Asset nodes, tank numbers (`<tank>_label`), lights, sensors, moving parts, pipes, and waypoints are never dropped.

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

### One clock (#42)

Every backend endpoint starts from the same moment (`backend/app/now.py`):

- **With the live layer** (#38): that moment is the live field. `GET /api/state` has the live `clock` and the live
  statuses (the same as `/api/live/state`, including `UNKNOWN`), in this contract's shape. A stale reading shows
  `"stale": true`, `"level_pct": null`, and `"last_level_pct"`, never a number as if it were current.
  `POST /api/forecast`, `/api/plans`, and `/api/lastcall` start from the live clock and readings, and count what's
  already under way: confirmed chokes (wells at their live flow), pumps that read off, and trucks on the road on
  their real timetable. A plan never sends a truck that's already out.
- **No fresh reading means the worst case, said out loud.** A pump with neither a fresh run nor a fresh flow reading
  counts as off. A valve with no fresh `OPEN`/`CLOSED` reading counts as closed (for a crossover that gives the
  earliest shutdown of the two tanks; the warning says the lower tank could fill faster). Each assumption is a line
  in `warnings` on `POST /api/forecast`, `POST /api/plans` (new top-level `warnings`), and Last Call.
- **Without it** (or `FREEBOARD_ONE_CLOCK=0`): the active scenario, plus the plans approved on it.
- `GET /api/state`, `POST /api/forecast`, and `POST /api/plans` add `"clock_source": "live" | "static"`. Forecast and
  plans also add `"clock"`, the moment they start from. `GET /api/health` has `clock_source` too.
- `POST /api/plans/{id}/approve` (operator) still returns `plan_id`, `steps`, `radio_script`, and then:
  - live: `"proposals": ["act_7", ...]`, one PENDING proposal per step in step order (`source: "plan"`). Nothing
    moves until a person confirms each (`POST /api/actions/{id}/confirm`, phone or console). Approving the same plan
    again returns the proposals still pending instead of new ones. `proposal_errors` lists any step it couldn't propose.
  - static: `"recorded_actions": N`. `/api/state` shows them (`applied_actions`), and later forecasts and plans count them.
- Activating a blueprint moves the live layer to it (`app.state.live_reload`), so the clock never follows the old field.
- Audit (`GET /api/audit`): approving a plan logs `"approved"` (`plan_id`, `proposals`), and activating a blueprint
  logs `"blueprint_activated"` (`blueprint_id`), each with the caller's `source` and `role`.
- SpacetimeDB follows the live field: every change in status, level (0.1 %), position, or reason is pushed within
  `SPACETIME_MIRROR_S` (1 s). `asset_state.status` may be `UNKNOWN`. With #126 the mirror also sends alerts,
  proposals, and connector status in one `apply_batch` call (see SpacetimeDB v2 below).

### `GET /api/history` (CF-14)

Readings on a regular grid that ends now, on the same clock as `/api/state` and the forecast. Read only.

`?minutes=60&step_min=1&asset_id=TANK_3,TANK_4&signal=level_pct` (all optional: 60, 1, every asset, `level_pct`).
`asset_id` and `signal` take comma lists; `signal=all` is every signal recorded. At most 2000 points (422 otherwise);
an unknown asset is 404.

```json
{
  "clock": "02:10", "clock_source": "live", "t": 26.0,
  "minutes": 60, "step_min": 1, "stale_after_min": 5, "since_t_min": -26,
  "timeline": [{"t_min": -60, "clock": "01:10", "tanks": {"TANK_3": null, "TANK_4": null}},
               {"t_min": 0, "clock": "02:10", "tanks": {"TANK_3": 80.4, "TANK_4": 61.2}}],
  "series": [{"asset_id": "TANK_3", "signal": "level_pct", "points": [[-60, null], [0, 80.4]]}],
  "gaps": [{"asset_id": "TANK_4", "signal": "level_pct", "from_t_min": -12, "to_t_min": 0}]
}
```

- A point is what the console showed at that moment: the latest reading at or before it, while it was fresh (the live
  layer's rule, stale after `stale_after_min`). **A stale period is `null`, never the last value carried forward**,
  and so is any time before the first reading (`since_t_min` is where recording starts).
- `gaps` lists the stale runs after recording started, for shading. Time before `since_t_min` is not a gap.
- `timeline` is the tanks' `level_pct` in the forecast's timeline shape with `t_min <= 0`, so the past and the future
  are one trend (CF-3): history's timeline, then the forecast's (its `t_min: 0` is the same moment). A blind tank is
  still listed, all `null`.
- Values are as read (numbers rounded to 0.01; `running` and `position` as the connector sends them).
- Kept for `FREEBOARD_HISTORY_MIN` live minutes (720). Activating a blueprint starts it over.
- No live layer (or `FREEBOARD_ONE_CLOCK=0`): `clock_source: "static"`, empty lists, and a `note`.

### Control token (#105, #103, CF-15)

Reads (GET) are always open. One mechanism, `backend/app/auth.py`, checked per route:

- None of `FREEBOARD_TOKEN`, `OPERATOR_PIN`, `FREEBOARD_PHONE_TOKEN` set: everything is open, as before.
- Any of them set: protected writes need the header `X-Freeboard-Token`. Missing or wrong gets
  401 `{"detail": "Enter the operator PIN to send commands."}`. `FREEBOARD_TOKEN` or `OPERATOR_PIN` is the
  operator; `FREEBOARD_PHONE_TOKEN` is the phone, which may only confirm, reject, and ack (403 elsewhere).
- Operator only on the backend core: `POST /api/plans/{id}/approve`, `POST /api/spacetime/sync`,
  `POST /api/blueprints`, `POST /api/blueprints/{id}/activate`. `POST /api/forecast`, `/api/plans`, and
  `/api/lastcall` stay open, since they change nothing. The live layer's routes are listed in its section.
- Always, tokens or not (`backend/app/origin_guard.py`): a write whose `Origin` names a different host than the
  server gets 403. Same host on another port (console :5173, API :8000) and localhost pass. No Origin (curl,
  devices, webhooks) passes. Extra hosts: `FREEBOARD_ALLOWED_HOSTS=a,b`.
- `/api/blueprints` takes only `application/json` or `multipart/form-data` (415 otherwise), and the 5 MB cap is
  enforced while the body streams in.

### `GET /api/field`

Everything that doesn't change during the night.

```json
{
  "tanks": [{ "id": "TANK_3", "capacity_bbl": 1000, "shutdown_pct": 90 }],
  "pumps": [{ "id": "PUMP_3", "tank_id": "TANK_3", "rate_bbl_hr": 150 }],
  "valves": [{ "id": "VALVE_XO_34", "connects": ["TANK_3", "TANK_4"], "max_bbl_hr": 90, "manual": true }],
  "wells": [{ "id": "WELL_9", "tank_id": "TANK_3", "water_bbl_hr": 20, "lost_usd_hr": 300 }],
  "disposal_wells": [{ "id": "SWD_1", "max_bbl_hr": 600 }],
  "trucks": [{ "id": "TRUCK_1", "capacity_bbl": 130, "minutes_to": { "TANK_3": 35 } }],
  "lead_min": { "SET_VALVE_manual": 20, "SET_VALVE_remote": 1, "CHOKE_WELL": 2, "SEND_TRUCK": 5, "SET_PUMP": 1 }
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

`at_min` is when the action takes effect. For `SEND_TRUCK` it's the dispatch time, and the forecast adds the drive. For `CHOKE_WELL`, `value` is the fraction of normal flow the well keeps: `1.0` is untouched and `0.0` is shut in.

`lead_min` in `/api/field` is how long an action takes to start working after the operator decides, such as the rover's drive to a manual valve. Last Call uses it. The forecast doesn't.

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

### `POST /api/lastcall`

How long each fix stays possible, and what waiting costs. See [lastcall/README.md](../lastcall/README.md). Run it when an incident opens and after a plan is approved, not on every click. It takes about a second.

Request: `{ "horizon_minutes": 360, "step_minutes": 5, "top_n": 3 }`, all optional.

```json
{
  "status": "INCIDENT",
  "headline": "Your cheapest plan expires at 2:20 AM. Wait past it and the bill goes from $1,408 to $1,962.",
  "shutdown": { "asset_id": "TANK_3", "t_min": 62.7, "clock": "2:50 AM" },
  "point_of_no_return": { "t_min": 57.8, "clock": "2:45 AM" },
  "do_nothing_usd": 8834,
  "options": [
    {
      "plan_id": "lc_36",
      "summary": "Open the Tank 3 to 4 crossover and send one truck to Tank 3.",
      "cost": { "total_usd": 1408 },
      "expires_min": 33.3, "expires_clock": "2:20 AM",
      "expires_min_hot": null, "robust": false,
      "steps": [{ "n": 1, "asset_id": "VALVE_XO_34", "do_by_clock": "2:20 AM", "text": "Send the rover to open the Tank 3 to 4 crossover by 2:20 AM." }]
    }
  ],
  "ladder": [{ "plan_id": "lc_36", "from_min": 0, "expires_min": 33.3, "usd_at_start": 1408, "asset_ids": ["VALVE_XO_34", "TRUCK_1"] }],
  "cliffs": [{ "t_min": 33.3, "clock": "2:20 AM", "from_usd": 1324, "to_usd": 1962, "text": "..." }],
  "curve": [{ "start_min": 0, "total_usd": 1408, "plan_id": "lc_36", "keeps_running": true }],
  "assets": { "VALVE_XO_34": { "useful_until_min": 57.8, "in_cheapest_from_min": 0, "in_cheapest_until_min": 54.9 } }
}
```

`expires_min_hot` is the same expiry with inflow 15% higher. `null` means the plan already fails at that inflow. When no tank reaches shutdown, `status` is `CALM`.

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
- Tanks also carry `observed_bbl_hr` (the fitted fill rate, positive while filling) and `expected_bbl_hr` (wells in minus the running pumps, sharing their disposal well's `max_bbl_hr`). `observed_bbl_hr` uses only readings since the tank's pumps or valves last changed state (at most the last 10 minutes), so it is null for about 2 live minutes after a trip instead of still showing the draining. `expected_bbl_hr` is null when it can't be explained that simply (a crossover on the tank not known to be closed, a pump with no fresh reading).
- `connectors` always starts with the simulator, `CONN_SIM`. Connector `kind` is `sim`, `webhook`, `mqtt`, `modbus`, or `opcua`; `status` is `connected` or `unavailable`.

### Alerts

`GET /api/alerts` → `{"alerts": [alert]}`, oldest first. `POST /api/alerts/{id}/ack` (optional body `{"by": "name"}`, phone or operator token, see [Control tokens](#control-tokens-103-cf-15)) marks one acknowledged and adds `acked_t` and `acked_by` (from the token's role when tokens are on). A second ack changes nothing. The phone notification's Acknowledge button calls it, but only while no token is set: a token never goes into a notification.

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

`t` is when it was raised (ISO). When the condition clears, the same alert is sent again with `"active": false` and `resolved_t`. An alert closed because the field was reloaded (see below) also has `resolved_reason`.

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
- `status`: `PENDING`, then `REJECTED`, or `SENDING` (a person confirmed it and the command is on its way to the connector), then `CONFIRMED` or `FAILED` (the connector refused or was unreachable). A proposal is sent at most once: the first confirm claims it as `SENDING`, so a second confirm (another screen) or a late reject gets 409. After a decision the proposal also has `decided_t` and `decided_by`, and once sent, `result` (what the connector said).
- `decided_by`: with control tokens on, the server sets it from the token's role, `"console"` (operator token or PIN) or `"phone"`, and **never** from the body's `by`. Automatic paths set `"jarvis"` (`JARVIS_CONFIRM=0`, simulator only) and `"blueprint"` (closed by a reload). With no token configured it is the body's `by` (or null), as before.
- `GET /api/actions` → `{"proposals": [proposal]}`.
- `POST /api/actions` with `{type, asset_id, value, target_id?, reason?}` → a PENDING proposal (422 if the action is invalid). Operator token.
- `POST /api/actions/{id}/confirm` and `POST /api/actions/{id}/reject` (optional body `{"by": "name"}`) → the updated proposal. 404 for an unknown id, 409 if it isn't PENDING (including while it is SENDING). Phone or operator token.

### `POST /api/jarvis`

Request: `{"message": "What am I looking at?", "selected_asset_ids": ["PUMP_3"], "image_png_base64": "<optional screenshot of the 3D view>"}`

Response 200:

```json
{ "reply": "Pump 3 is stopped, moving 0 barrels an hour. Pump 3 tripped: it reports stopped.",
  "source": "rules",
  "ui": [{ "type": "ui", "action": "focus", "asset_id": "PUMP_3" }],
  "proposals": [] }
```

- `source` is `rules` (instant answers that need no API key: what's failing, how long until shutdown, show or explain an asset, what am I looking at with a selection, the cheapest plan, and fixes like "open the crossover", "send truck 1 to tank 3", "shut in well 9", "turn off pump 2") or `claude`.
- Jarvis's fixes are proposals (PENDING) in `proposals`. It never changes equipment itself. With control tokens on, asking Jarvis needs the operator token.
- Jarvis never vouches for a tank with no fresh level. "How long until shutdown?" with Tank 2 UNKNOWN says "I can't vouch for Tank 2: no fresh level reading, so treat it as at risk and check in person." first, then the earliest shutdown, or "No tank with a fresh reading reaches shutdown in the next 6 hours if nothing changes." Its `get_forecast` tool result carries `unknown_tanks`.
- "Open the crossover" picks: the crossover joining the tanks the text names ("the Tank 3 to 4 crossover", "3 and 4", "between 5 and 6"); else the one on the tank in trouble (an active `PUMP_TRIP`'s tank, then the tank with the earliest forecast shutdown), toward the neighbor with the most room before shutdown; only then the first crossover. Named tanks with no crossover between them get a reply saying so and no proposal. "Close the crossover" prefers one that is open.
- A question about an asset that also asks for a fix ("Why is Tank 3 rising, and what's my cheapest fix?") answers the question, then adds "Cheapest plan, $X: ..." and, when it is a different plan, "Cheapest robust plan, $Y: ...". The last `ui` focus is the cheapest plan's first step.
- With no `ANTHROPIC_API_KEY` and no rules match: **503** `{"detail": "Jarvis needs ANTHROPIC_API_KEY on the server."}`. Other Claude failures are also 503 with a `detail` saying why.

### Control tokens (#103, CF-15)

One mechanism for the console link, the phone QR, and the typed PIN. Code: `backend/app/auth.py`.

| Variable | Role |
|---|---|
| `FREEBOARD_TOKEN` | operator: everything |
| `OPERATOR_PIN` | operator too: a short PIN a person can type (CF-5's prompt fills the same header) |
| `FREEBOARD_PHONE_TOKEN` | phone: confirm, reject, and ack only |

- **None set:** every route behaves exactly as before.
- **Any set:** every `POST` in the live layer needs the header `X-Freeboard-Token`: `/api/actions` (create), `/api/actions/{id}/confirm` and `/reject`, `/api/alerts/{id}/ack`, `/api/live/faults`, `/api/live/sim`, `/api/jarvis`. Missing or wrong → **401** `{"detail": "Enter the operator PIN to send commands."}`. The phone token on an operator-only route → **403**. `POST /api/live/readings/{connector_id}` keeps its own connector token (`token_env`). Reads (`GET`) stay open. Tokens are compared with `hmac.compare_digest` and never logged.
- `WS /ws?token=<token>`: a wrong token is closed with code **1008**. No token connects read-only: it gets every message, but its `ui` messages are ignored. Operator and phone tokens may send `ui`.
- **Other lanes** put the same check on their routes (`/api/blueprints/*`, approve, spacetime sync #105; `/api/ingest` #106):

```python
from fastapi import Depends
from backend.app.auth import Caller, require_role

@app.post("/api/plans/{plan_id}/approve")
def post_approve(plan_id: str, caller: Caller = Depends(require_role("operator"))) -> dict:
    ...  # caller.role: "operator" | "phone" | None (no token configured); caller.device: "console" | "phone" | None
```

`require_role("operator")` is operator only; `require_role("phone")` is phone or operator. `caller.decided_by(body_by)` gives the value to store as `decided_by`.

### Audit log (CF-10)

`GET /api/audit` (optional `?since=<seq>`) → `{"entries": [entry]}`, oldest first. Append-only, kept in memory for the server's life. Set `FREEBOARD_AUDIT_FILE` to also append each entry as a JSON line (a relative path is under `backend/data/`, where `*.jsonl` is gitignored).

```json
{ "seq": 2, "t": "2026-11-07T07:12:10+00:00", "clock": "01:52", "live_t": 8.0,
  "event": "confirmed", "source": "phone", "role": "phone", "by": "phone",
  "asset_id": "VALVE_XO_34", "proposal_id": "act_1", "status": "SENDING",
  "text": "Open the Tank 3 to 4 crossover.", "connector_id": "CONN_SIM" }
```

| `event` | When | Extra fields |
|---|---|---|
| `proposed` | A proposal is created (console or Jarvis) | `proposal_id`, `status` `PENDING`, `text`, `reason`, `connector_id`, `target_id?` |
| `confirmed` | A person (or `JARVIS_CONFIRM=0`) confirmed it; it is now `SENDING` | same, plus `auto` `"JARVIS_CONFIRM=0"` on the automatic path |
| `result` | The connector answered | `status` `CONFIRMED` or `FAILED` (or `UNCERTAIN` once that state exists), `result` |
| `rejected` | A person rejected it, or a reload closed it (`source` `blueprint`) | `result?` |
| `acked` | An alert was acknowledged (the first time only) | `alert_id`, `kind`, `severity`, `message` |

- `t` is the wall time (ISO), `clock` and `live_t` the live clock. `role` is the token role (`operator`, `phone`, or null when no token is configured or nobody asked). `source` is `console`, `phone`, `jarvis`, or `blueprint`; for a Jarvis proposal, `role` is the operator who asked. `by` is the stored `decided_by`.
- **Other lanes** log their own steps (approve, activate, shelve) with `app.state.audit.append("approved", source=caller.device, role=caller.role, plan_id=...)`. Any extra keyword is kept on the entry. It is safe to call from a sync endpoint's worker thread.

### Following the active blueprint

The live layer follows `POST /api/blueprints/{id}/activate` once #64's `_use_scenario` calls `app.state.live_reload(scenario)` (installed by `backend/app/live_api.py`; until then the live layer stays on the startup scenario). The hook is a plain function and is safe to call from a sync endpoint's worker thread: it runs the reload on the live loop and waits. A reload:

- stops the old connectors and builds the new field, connectors, and detector, keeping the live clock (`t` and the clock shown), speed, and pause. Readings start over from the new connectors, and the new blueprint's `faults` fire at their `at_min` after the reload;
- resolves every active alert with a `resolved_reason` (`"Pump 6 is not in the activated blueprint."`, or that the field reloaded and is checked again; the new detector raises it again if it still holds). Alert ids keep counting;
- closes a PENDING proposal as `REJECTED` with `decided_by` `"blueprint"` and a `result` saying why, when its asset or target is gone, it is no longer valid, or it would now go to a different connector. A person never confirms a command routed somewhere other than where it was proposed. A proposal already `SENDING` is left to finish;
- broadcasts each closed proposal, each resolved alert, and a fresh `state`.

### Connectors and demo controls

- `GET /api/live/connectors` → `{"connectors": [...], "jarvis": {"mode", "model", "llm_available"}, "push": {"enabled", "last_error"}}`.
- `POST /api/live/readings/{connector_id}` (webhook connectors only) with `{"readings": [{"asset_id", "signal", "value"} | {"address", "value"}]}` → `{"accepted": [...], "rejected": [...]}`. Only signals the blueprint declares for that connector are accepted. Each value goes through the connector's signal rules (`signalmap`, with `config.signal_map`), the same as the other field connectors: numeric text becomes a number, run words become `true`/`false` (TRIPPED, STOPPED, OFF and 0 are stopped), and a value that isn't good (text that isn't a number, NaN, inf, out of range, a word that isn't on or off, a flow below -1 bbl/hr with no range) goes to `rejected` with its quality and why, without the raw value. A rejected signal shows `UNKNOWN` in `/api/state` at once, until a good value comes. If the connector has `token_env`, send the token in `X-Freeboard-Token`.
- `POST /api/live/sim` with `{speed?, paused?, advance_min?}` → the live clock.
- `POST /api/live/faults` with `{asset_id, type: TRIP | STUCK_VALVE | SENSOR_STALE | LEAK | REPAIR, reason?, rate_bbl_hr?}` injects a fault into the simulator.

### `WS /ws`

Connect to `ws://127.0.0.1:8000/ws?role=web` (`role` is `web`, `phone`, `device`, or `other`; add `&token=<token>` when control tokens are on, see above). Every message is one JSON object with a `type` and a `seq` counter:

| `type` | Payload | When |
|---|---|---|
| `state` | `{"type": "state", "seq": 1, "state": <GET /api/live/state body>}` | On connect, every tick, and after each confirm |
| `alert` | `{"type": "alert", "seq": 2, "alert": <alert>}` | Raised or resolved |
| `proposal` | `{"type": "proposal", "seq": 3, "proposal": <proposal>}` | Created, sending, confirmed, rejected, or failed |
| `ui` | `{"type": "ui", "seq": 4, "action": "focus", "asset_id": "PUMP_3", "reason": "...", "source": "jarvis"}` | Jarvis, a new ALARM, a proposal, or a phone wants every screen on an asset |

Clients may send `{"type": "ping"}` (answered with `pong`) and `{"type": "ui", "action": "focus", "asset_id": "TANK_3"}` to point every console at an asset (with control tokens on, only a client that connected with a valid token).

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Enables Claude in Jarvis. Without it, only the rules answer. |
| `JARVIS_MODEL` | `claude-sonnet-5-5` | Claude model for Jarvis. |
| `JARVIS_MODE` | `auto` | `auto` (rules first, Claude for the rest), `rules`, or `llm`. |
| `JARVIS_CONFIRM` | `1` | `0` lets Jarvis's proposals run without a person, **only** for the simulator. Real equipment always waits for confirm. |
| `FREEBOARD_SPEED` | `0.25` | Live minutes per real second. |
| `NTFY_TOPIC` | unset | Phone push of ALARM and WARNING alerts through ntfy. Off when unset. |
| `NTFY_SERVER` | `https://ntfy.sh` | ntfy server. |
| `PUBLIC_URL` | unset | Where a phone can reach this server, for the notification's buttons. |
| `CORS_ORIGINS` | unset | Extra allowed origins, comma separated. Localhost is always allowed. |
| `FREEBOARD_TOKEN` | unset | Operator control token (#103). Any of these three turns the write lock on. |
| `OPERATOR_PIN` | unset | Operator PIN, same role as `FREEBOARD_TOKEN` (CF-15). Never commit it. |
| `FREEBOARD_PHONE_TOKEN` | unset | Phone control token: confirm, reject, and ack only. |
| `FREEBOARD_AUDIT_FILE` | unset | Also append the audit log here as JSON lines (relative = under `backend/data/`). |
| `*_env` in connector config | | Names the variable holding a connector secret, e.g. `"token_env": "HOOK_TOKEN"`. For the #126 connectors (opcua, sparkplug) the name must start with `FREEBOARD_CONN_`. |
| `FREEBOARD_SECURITY_DEDUPE_S` | `60` | Seconds in which repeats of one security event add to its count (#126). |
| `SPACETIME_MIRROR_S` | `1` | Seconds between SpacetimeDB mirror batches. |
| `SPACETIME_RESYNC_S` | `60` | Seconds between full SpacetimeDB batches (#126). |

### `POST /api/ingest`

Upload a blueprint as multipart form field `file`. That can be a PNG, JPEG, WebP, GIF, or PDF site plan, a CSV equipment list, or a blueprint JSON. The response is the parsed blueprint plus what a human must confirm before the field is built. Code: `ingest/`. Mount it with `app.include_router(ingest.router.router)`.

```json
{
  "ok": true,
  "errors": [],
  "review": [
    { "id": "TANK_3", "label": "Tank 3", "issue": "Missing shutdown_pct. The backend won't forecast it until these are set." },
    { "id": "WELL_10", "label": "unlabeled pumpjack", "issue": "Claude is only 60% sure about this one. Check it." }
  ],
  "counts": { "tanks": 2, "pumps": 1, "valves": 1, "wells": 2, "disposal_wells": 1, "trucks": 0, "yards": 0 },
  "source": { "file": "plan.png", "read_by": "claude", "model": "claude-opus-5-5" },
  "blueprint": { "name": "North Lease", "tanks": [{ "id": "TANK_3", "capacity_bbl": 500, "x": -40.0, "y": 0.0 }] }
}
```

- `blueprint` has the same shape as `scenario/field.json`, plus `x` and `y` in meters where the plan shows a position. Each asset also carries `_ingest` with its original label, Claude's confidence, and the evidence.
- `errors` block a build: duplicate IDs, bad IDs, or references to missing IDs. `review` items don't block a build, but the console should show them for a human to confirm or fill in.
- Error statuses: 400 for a bad file, 413 for a file too large, 415 for an unsupported type, 422 when Claude declines or can't read it, 429 when rate limited, and 502 or 503 for server and API problems. The `detail` says what to do.

### Blueprints (#41): `POST /api/blueprints` and friends

Upload a customer's field ([BLUEPRINT.md](BLUEPRINT.md)), get its 3D model, and run the console on it. The upload screen (`web/src/upload.js`, #43) reads exactly this shape.

**`POST /api/blueprints`** takes any of:

- `{"blueprint": {...}, "activate": true}`, where `blueprint` is the one from `/api/ingest` after a person fills the review items. `activate` is optional and defaults to `true`.
- a raw blueprint as the JSON body.
- `multipart/form-data` with a `.json` file in the field `file` (and an optional `activate` field). Drawings and CSVs go to `/api/ingest` first, so they get a 415.

The response, and **`GET /api/blueprints/{id}`**, is one record:

```json
{
  "id": "bp_7efcfbee8eff",
  "status": "fallback",
  "ok": true,
  "errors": [],
  "warnings": [],
  "counts": { "tanks": 8, "pumps": 8, "valves": 13, "wells": 24, "disposal_wells": 3, "trucks": 3, "yards": 1 },
  "glb_url": "/api/blueprints/bp_7efcfbee8eff/field.glb",
  "note": "Showing the standard field model: Blender isn't installed on this server. The forecast and plans use your blueprint. 12 asset(s) have no 3D shape in it: PUMP_7, ...",
  "active": true,
  "name": "Pecos Ridge lease - night shift",
  "model": "fallback",
  "placed": null,
  "build_report": null,
  "build_seconds": null,
  "source": "body",
  "created_at": "2026-11-07T18:20:55"
}
```

- `status`: `building` (poll GET about once a second), `ready` (built by Blender), `fallback` (the console shows main's `blender/field.glb`), or `failed` (the blueprint has problems).
- **The demo never waits on a build.** If Blender is missing, the generator can't take `--blueprint` yet, or the build fails or takes over `FREEBOARD_BUILD_TIMEOUT` seconds (default 180), the status is `fallback` and the reason is in `note`. Generator errors become `warnings` that start with `Generator error:`.
- `failed` comes back as **HTTP 422** with the same record plus `detail` (the problems in one sentence). `errors` lists every problem from `scenario/validate_blueprint.py`, then the backend's own checks. Nothing is built or activated.
- The `id` is a hash of the blueprint, so uploading the same file again returns the same record without rebuilding.
- A valid upload is activated straight away unless `activate` is `false`. The console then reloads on `/api/field` and `glb_url`.
- Blender is found from `BLENDER`, then `blender` on the PATH, then `/Applications/Blender.app/Contents/MacOS/Blender`. It runs `blender --background --factory-startup --python blender/build_field.py -- --blueprint <file> --out <dir>` and reads `<dir>/build_report.json` (`ok`, `errors`, `warnings`, `counts`, `placed`).
- Files live in `backend/data/blueprints/<id>/` (gitignored), or `FREEBOARD_BLUEPRINTS_DIR`.

**`GET /api/blueprints/{id}/field.glb`** serves the built model when the status is `ready`, and main's `blender/field.glb` otherwise. The header `X-Freeboard-Model` says `built` or `fallback`. A `failed` blueprint gets a 409.

**`POST /api/blueprints/{id}/activate`** makes `/api/field`, `/api/state`, `/api/forecast`, `/api/plans`, and Last Call use this blueprint. It returns the record with `"active": true`. A `failed` blueprint gets a 409, and an unknown id gets a 404. **`POST /api/blueprints/default/activate`** goes back to the server's starting scenario. The active blueprint is kept in memory, so a restart goes back to the default.

**`GET /api/blueprints`** returns `{"active": "<id or default>", "blueprints": [record, ...]}`, newest first.

While a blueprint with new IDs is active (such as `TANK_7`), `/api/field` lists IDs the Asset IDs table above doesn't have, and `tools/check` against that server reports them. Activate `default` before running `tools/check`.

## Field connectors, security events, SpacetimeDB v2 (#126)

### What the backend wires up

- `backend/app/connectors/extra.py` registers the field connector kinds built for #126 into the registry's `KINDS`: `opcua` (the real client, replacing the stub) and `sparkplug`. main.py imports it before the live layer is installed. A protocol library that isn't installed makes that connector `unavailable` with the pip hint in its `detail`; the backend still starts. The libraries are pinned in `backend/requirements-connectors.txt`.
- Connector quality counts at once. Every #126 connector keeps `quality[(asset_id, signal)] = {quality, detail, t}`. When a signal's quality is anything but `good`, `/api/state`, the forecast, plans, and Last Call treat it as having no fresh reading, even if its last good value is younger than `sensors.stale_after_min`:
  - a tank has no level (`level_pct: null`, `last_level_pct` keeps the old one);
  - a valve counts as closed and a pump as off in the forecast, with the usual `live_assumptions` warnings;
  - a well keeps its design rate (never assume less water than we know);
  - the asset shows `UNKNOWN` (an asset already in `ALARM` stays in `ALARM`), with `stale: true`, `quality: {"<signal>": "<quality>"}`, and a `reason` like `"CONN_OPCUA reports level_pct as bad (StatusCode BadSensorFailure). Treat as unknown, not safe."`
- A pump with neither a fresh run nor a fresh flow reading shows `UNKNOWN` in `/api/state` with `reason` `"No fresh run or flow reading. Treat as unknown, not safe."`, never `OK`. A pump in `ALARM` stays in `ALARM`.
- `security_log.install(app)` runs right after `origin_guard.install(app)`, and the SpacetimeDB mirror is `spacetime_mirror.run` (both below).

**Demo.** `python -m backend.fakes` starts a fake OPC UA server (127.0.0.1:4840) and a fake Sparkplug B edge node with its own MQTT broker (127.0.0.1:1886), and prints the variables to export: the `FREEBOARD_CONN_*` ones and `FREEBOARD_SCENARIO=backend/data/samples/connectors_demo.json`. Start the backend in that shell, so it starts on the demo blueprint at 01:44 and the story starts calm. Don't start it on the default field and activate the demo blueprint later: the default field (`scenario/field.json`) trips PUMP_3 at 01:47 on its own, and activation keeps the live clock, so the console would show two PUMP_3 trips at the wrong times. That blueprint is the team field plus two connectors: OPC UA reads TANK_6, PUMP_6, VALVE_D_6 and WELL_1, and Sparkplug B reads TANK_3, PUMP_3 and VALVE_D_3. The simulator keeps every other asset. The fakes follow the backend's live clock (`t` from `GET /api/live/state`, `--backend http://127.0.0.1:8000` by default), so `POST /api/live/sim` speed, pause and advance move the fake tanks as far as the detector expects; while the backend can't be reached the story waits. Until the backend's Sparkplug host comes online the OPC UA fake answers BadWaitingForInitialData, so its line shows `UNKNOWN`, never a running pump over a frozen tank. PUMP_3 trips 3 live minutes after the host comes online. It needs the blueprint schema with the `sparkplug` kind (on main).

### Connector foundation

`backend/app/connectors/signalmap.py` is the shared code for every field connector built for #126 (OPC UA, Sparkplug B, and the rest). It doesn't change the API.

**Secrets.** A config key ending in `_env` names an environment variable, and the name must match `FREEBOARD_CONN_[A-Za-z0-9_]+`. Any other name (say `ANTHROPIC_API_KEY`) makes the connector `unavailable` with `bad config: config password_env must name an environment variable that starts with FREEBOARD_CONN_ ...`. The message names the key and never repeats the value. Nested config objects are checked too. A secret's value is never logged, returned in a status, or put in an error.

**Signal rules.** A signal's own `scale` multiplies the raw number first. Then the rule in `config.signal_map["ASSET_ID.signal"]` runs:

```json
"config": {
  "host": "10.0.0.7",
  "password_env": "FREEBOARD_CONN_PLANT_PASSWORD",
  "max_age_s": 60,
  "signal_map": {
    "TANK_3.level_pct":     { "ma420": true, "eng": [0, 100], "range": [0, 100] },
    "PUMP_3.flow_bbl_hr":   { "raw": [0, 27648], "eng": [0, 300], "unit": "gpm" },
    "TANK_4.level_pct":     { "unit": "ft", "height_ft": 16 },
    "SWD_1.pressure_psi":   { "unit": "bar", "range": [0, 150] },
    "PUMP_3.running":       { "invert": true },
    "VALVE_XO_34.position": { "open_values": [1], "closed_values": [0] }
  }
}
```

| Key | Signals | Meaning |
|---|---|---|
| `raw`, `eng` | analog | `[lo, hi]` each: linear scaling from the raw number to engineering units. One needs the other. |
| `ma420` | analog | `true` for a 4-20 mA input. Needs `eng`, the values at 4 and 20 mA. NAMUR NE43: at or below 3.6 mA, or at or above 21 mA, is `fault`. 3.6-3.8 and 20.5-21 mA is saturated (`uncertain`). 3.8-20.5 mA reads linearly. |
| `unit` | analog | flow `bbl_hr` (default), `gpm`, `bbl_day`, `m3_h`, `l_s`; pressure `psi` (default), `kpa`, `bar`, `mpa`; level `pct` (default), or `ft`, `in`, `m` with `height_ft` or `height_m` (the liquid height that reads 100%); motor_amps `a`. |
| `range` | analog | `[lo, hi]` in the live layer's unit (after conversion), inclusive. Outside it is `out_of_range`. |
| `invert` | running, position | Flips contacts: booleans, numbers, `1`/`0`, `true`/`false`. Words like `STOPPED` are taken as written. |
| `open_values`, `closed_values` | position | Both or neither. Then only the listed values count; anything else is `bad`. |
| `max_age_s` | any | The oldest device timestamp accepted. Defaults to the connector's `config.max_age_s`, else 60 s. |

A rule for a signal the connector doesn't declare, an unknown rule key, or a bad value in a rule makes the connector `unavailable` with the reason. Keys that start with `_` are comments. Factors: 1 bbl = 42 US gal (1 gpm = 60/42 bbl/hr), 1 m3 = 6.28981 bbl, 1 L/s = 3.6 m3/h, 1 kPa = 0.145038 psi, 1 bar = 14.5038 psi, 1 MPa = 145.038 psi, 1 ft = 0.3048 m.

**Quality.** Every mapped value has a quality: `good`, `uncertain`, `bad`, `fault`, `out_of_range`, `comm_lost`, `stale`, or `null`. Only a `good` value is used. A retained, replayed, or historical message is `stale`, and so is a device timestamp older than `max_age_s` (or that far ahead). A value that isn't good is never replaced by the last good one. It is no reading at all, and the connector's `quality` entry makes the asset `UNKNOWN` in `/api/state` at once (see above).

**Commands.** A connector writes only from `send()`, and the live layer calls that only from confirm. `SET_VALVE` OPEN/CLOSED and `SET_PUMP` ON/OFF become true/false. `CHOKE_WELL` is a fraction from 0 (shut in) to 1 (full flow), so 50 is refused, not read as 50%. Anything else is refused before it is sent. A write whose acknowledgement never comes is `{"ok": false, "uncertain": true, "detail": "sent; delivery not confirmed - check the field before retrying"}` and is never sent again. Reconnects back off from 1 s, doubling up to 30 s, with up to 20% jitter either way.

### OPC UA connector (`kind: "opcua"`)

A real OPC UA client (asyncua) on its own thread. It reads declared signals through subscriptions and writes declared commands only after a person confirms them.

```json
{"id": "CONN_OPCUA", "kind": "opcua",
 "config": {"endpoint": "opc.tcp://10.0.0.5:4840",
            "security_policy": "Basic256Sha256", "security_mode": "SignAndEncrypt",
            "cert_path_env": "FREEBOARD_CONN_OPCUA_CERT", "key_path_env": "FREEBOARD_CONN_OPCUA_KEY",
            "server_cert_path_env": "FREEBOARD_CONN_OPCUA_SERVER_CERT",
            "username": "freeboard", "password_env": "FREEBOARD_CONN_OPCUA_PASSWORD",
            "publish_ms": 500, "timeout_s": 5, "max_age_s": 300},
 "signals":  [{"asset_id": "TANK_6", "signal": "level_pct", "address": "ns=2;s=TANK_6.Level"}],
 "commands": [{"asset_id": "VALVE_D_6", "command": "SET_VALVE", "address": "ns=2;s=VALVE_D_6.Command"}]}
```

- `address` is a NodeId string. Commands can be SET_VALVE (OPEN/CLOSED), SET_PUMP (ON/OFF) or CHOKE_WELL (0..1). Each is written once to the command's NodeId using the variable's own VariantType: Boolean takes True/False, numbers take the number, String takes OPEN/CLOSED or ON/OFF.
- Security: `security_policy` is Basic256Sha256 (default), Aes128Sha256RsaOaep or Aes256Sha256RsaPss. `security_mode` is SignAndEncrypt (default) or Sign. `None` is allowed only with `allow_insecure: true` and an endpoint on 127.0.0.1 or localhost. Certificate and key are file paths held in FREEBOARD_CONN_ variables. `server_cert_path_env` pins the server certificate. An endpoint off this machine needs it (the connector is unavailable otherwise), unless config says `allow_unpinned_server_cert: true`; without a pin the client takes whatever certificate the endpoint shows.
- Every `*_env` value must name a FREEBOARD_CONN_ variable. Otherwise the connector shows `unavailable` with the reason. Secret values never appear in status or results.
- Values: a reading is made only when the StatusCode is Good and the SourceTimestamp is no older than `max_age_s`. Bad, Uncertain, old, or a value its `config.signal_map` rule rejects gives no reading, and the connector's `quality[(asset_id, signal)]` says why: good, bad, uncertain, comm_lost, stale, out_of_range, fault.
- Session loss: status turns `unavailable` with the reason, every declared signal becomes `comm_lost`, and the connector reconnects with backoff and subscribes again.
- `send()` results: `{"ok": true, "detail"}`, `{"ok": false, "detail"}` (nothing was sent: no session, refused, wrong type), or `{"ok": false, "uncertain": true, "detail": "sent; delivery not confirmed - check the field before retrying"}` when no answer came within `timeout_s`. Nothing is retried.
- Fake for demos: `python -m backend.fakes.opcua --port 4840 --workdir <gitignored dir>` (127.0.0.1, SignAndEncrypt, username `freeboard`, password from FREEBOARD_CONN_OPCUA_PASSWORD). It prints the endpoint and the certificate paths to export, never the password.

### Sparkplug B connector (`kind: "sparkplug"`)

Freeboard acts as a Sparkplug 3.0 **host application** on an MQTT 3.1.1 broker (paho-mqtt).

**Config:** `host` (required), `port` (1883), `group_id` (required), `host_id` (required; no `/ + #`), `client_id` (default `freeboard-<host_id>-<id>`), `username`, `password_env` (must name a `FREEBOARD_CONN_*` variable), `tls` (false), `keepalive_s` (30), `timeout_s` (5), `rebirth_min_interval_s` (10), `max_age_s` (60), `signal_map` (rules keyed `ASSET_ID.signal`, see above). Each signal's `scale` is applied too.

**Addresses** are exact metric paths. MQTT wildcards are refused, and the group must equal `group_id`:
- `group/edge_node/device/metric` for a device metric, e.g. `FREEBOARD/EDGE_1/PLC_1/TANK_3/Level`
- `group/edge_node//metric` for an edge node metric

The metric is everything after the third slash, so it may contain `/`.

**Host behaviour:**
- **STATE:** on connect the host publishes `spBv1.0/STATE/<host_id>` = `{"online": true, "timestamp": ms}`, retained at QoS 1. Its will is `{"online": false}` with the same timestamp. On a clean close it publishes offline.
- **Tracking:** it subscribes to `spBv1.0/<group_id>/#` and keeps bdSeq, alias maps and datatypes from NBIRTH/DBIRTH, plus the last seq for each edge node it maps.
- **Rebirth:** data before a birth, a retained message, a seq gap (mod 256) or an unreadable payload is ignored, and the host sends NCMD `Node Control/Rebirth` = true. This is a protocol message and moves no equipment. It is limited to one per node per `rebirth_min_interval_s`. After a seq gap the node's signals are `uncertain` until the next NBIRTH.
- **Deaths:** an NDEATH with the node's bdSeq, or a DDEATH, sets every mapped signal of that node or device to quality `comm_lost` at once. Nothing from it counts until the next birth. An NDEATH with an older bdSeq is ignored.
- **Values:** `is_historical` metrics are never live. `is_null`, a value that doesn't fit its datatype, a metric timestamp older than `max_age_s`, or a SignalMap fault produces no reading, and `quality` says why. Every signal starts as `comm_lost` until its device is born, and losing the broker sets every signal to `comm_lost`.

**Commands:** only from `send()`, which is called only from confirm. The host publishes a DCMD (NCMD for a node metric) with the metric by name and its birth datatype, QoS 0, never retained.
- SET_VALVE and SET_PUMP write true/false to a Boolean metric, 1/0 to an integer or float metric, or OPEN/CLOSED and ON/OFF to a String metric.
- CHOKE_WELL writes the 0..1 fraction to a Float or Double metric.
- The command is refused, and nothing is sent, unless the device is born and online with that metric in its birth.
- `ok: true` means it was published. The next DDATA shows the change.
- If the publish doesn't complete within `timeout_s`, the result is `{"ok": false, "uncertain": true, ...}`. A QoS 0 command is never re-sent after a reconnect.

**Status:** available only while connected to the broker. The detail lists which edge nodes are online and which are still waiting for a birth.

Fake for demos: `python -m backend.fakes.sparkplug --port 1886` starts its own MQTT broker on 127.0.0.1:1886 plus edge node `FREEBOARD/EDGE_1` with device `PLC_1`. `--broker-port N` attaches to a broker already running (`python -m backend.fakes.mqtt_broker --port 1885`).

### Security events

`backend/app/security_log.py` watches what the server answers and records refused requests. It never changes a response. There is no SMS and no outside service: alerts go only to the audit log and the console.

| Seen on | Answer | kind |
|---|---|---|
| `/api/*` | 401 | `auth_failed` |
| `/api/*` | 403 from origin_guard ("Refused: this request came from another site.") | `cross_site` |
| `/api/*` | any other 403 (for example the phone token on an operator route) | `forbidden_role` |
| `/api/*` | 429 (the PIN lockout from auth.py, or a rate limit) | `lockout` |
| `/api/*` | 413 or 415 | `upload_rejected` |
| `/ws` | close with code 1008 | `ws_auth_failed` |

An event:

```json
{"id": 3, "kind": "auth_failed", "method": "POST", "path": "/api/actions/act_1/confirm",
 "client": "192.168.1.x", "first_t": "2026-11-07T07:12:03+00:00",
 "last_t": "2026-11-07T07:12:41+00:00", "count": 4, "detail": "401: missing or wrong token."}
```

- `path` never includes a query string, so a `?token=` is never recorded. uvicorn's own request log (`uvicorn.access`, and `uvicorn.error` for /ws handshakes) shows `token=***`: `backend/app/log_redact.py` masks every secret-looking query parameter there. `client` shows the last IPv4 octet as `x`, and an IPv6 address is cut to its /48. `method` is `WS` for /ws. Headers, bodies, tokens and PINs are never recorded.
- The same (kind, path, client) within `FREEBOARD_SECURITY_DEDUPE_S` seconds (default 60) of the event's `first_t` adds to `count` and moves `last_t` instead of creating a new event.
- Only the first occurrence is an alert. It writes an audit entry `security` with `source="security"`, `kind`, `path` and `client`, and sends `{"type": "security", "event": {...}}` to every screen on `/ws`. At most 30 alerts go out per minute; events beyond that are still kept.
- The last 500 events are kept in memory.

`GET /api/security/events?since=<id>` returns `{"events": [...]}`, oldest first, with ids greater than `since`. It is open like every read. An event's `count` and `last_t` keep changing during its window, so read without `since` to see the current values.

### SpacetimeDB v2: alerts, proposals, connectors, apply_batch

Module: `backend/spacetime/src/index.ts`. v2 only adds to v1. Every v1 table, column and reducer is unchanged, so a database published with v1 (hackutd-freeboard-live) takes v2 with `spacetime publish` and no `--delete-data`.

New public tables (consoles subscribe; only the backend writes):

| table | columns |
|---|---|
| `alert` | `id` (pk, e.g. `alert_7`), `asset_id`, `kind` (ID-style, e.g. `PUMP_TRIP`), `severity` (`WARNING`/`ALARM`), `message`, `active` (bool), `raised_clock`, `resolved_clock` ("" while active) |
| `proposal` | `id` (pk, e.g. `act_3`), `type` (`SET_VALVE`/`SET_PUMP`/`CHOKE_WELL`/`SEND_TRUCK`), `asset_id`, `value` (text: `OPEN`, `ON`, `0.5`, or ""), `target_id`, `status` (`PENDING`/`SENDING`/`CONFIRMED`/`FAILED`/`REJECTED`/`UNCERTAIN`), `source`, `reason`, `decided_by`, `result` |
| `connector_status` | `id` (pk, connector ID), `kind` (lowercase, e.g. `mqtt`), `status` (`connected`/`unavailable`), `detail`, `updated_clock` (live clock when status or detail last changed) |

The `proposal` table only shows proposals. A command moves only when a person confirms it through the backend (`POST /api/actions/{id}/confirm`).

New reducer `apply_batch(json: string)`, writer only. `json` is one object, and every key is optional: `clock` {clock, incident_root, incident_summary}; `assets` [{asset_id, status, level_pct, position, reason}]; `alerts` [...]; `proposals` [...]; `connectors` [...]; `removed_assets`, `removed_alerts`, `removed_proposals`, `removed_connectors` [id, ...]; `full` (bool). Missing text is "". Removals run first, then upserts, all in one transaction. One bad row (an unknown status or enum, an ID that is not a contract ID, a level outside 0-100 that is not -1, an unknown key) refuses the whole batch and writes nothing. A `full` batch must carry `clock` and all four lists (empty is fine), and it deletes every row it does not list. Asset `status` still allows `UNKNOWN` (no fresh reading, never shown as safe).

Backend: `spacetime_sync.push_batch(payload)` sends one `apply_batch` call (it claims the writer role once, as before, and never logs `SPACETIME_TOKEN`). `spacetime_mirror.run(app, period_s)` runs every `SPACETIME_MIRROR_S` seconds (default 1). It reads `now.state(app)`, the live alerts, the proposals and `registry.status()`, and sends only what changed since SpacetimeDB last accepted a batch. It sends a full batch on the first push, after any `push_state()`, and every `SPACETIME_RESYNC_S` seconds (default 60). Failures keep the changes for the next try. `upsert_asset` and `set_clock` keep working for older callers.
