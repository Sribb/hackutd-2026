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
8. **Budget:** For a field of up to 12 tanks: under 300,000 triangles, under 30 MB, and under 1,600 draw calls, so it runs smoothly on a laptop at the expo. Draw calls cost the most: in the console each one costs about 12 µs of frame time, and every 100,000 triangles cost about 0.7 ms (measured in Chrome on an M5, #97). The build reports `draw_calls`, and `build_field.py` and `blender/test_generator.py` hold the numbers.

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
- `--compress` (#97, off by default) writes Draco meshes and WebP textures: the default field goes from 16.3 MB to 4.7 MB with the same frame time, but the export takes much longer (2.6 s to 15.8 s at load average 20, mostly WebP encoding), so API builds leave it off. The console can only open such a file with a Draco decoder. To turn it on for the console, copy `web/node_modules/three/examples/jsm/libs/draco/gltf/` (`draco_decoder.wasm`, `draco_wasm_wrapper.js`, `draco_decoder.js`) to `web/public/draco/`, and give every `GLTFLoader` in `web/src/main.js` the decoder: `import { DRACOLoader } from "three/addons/loaders/DRACOLoader.js";` then `const loader = new GLTFLoader().setDRACOLoader(new DRACOLoader().setDecoderPath("/draco/"));`. WebP needs nothing: GLTFLoader reads `EXT_texture_webp` itself.
- `python3 blender/test_generator.py` builds the samples in `blender/samples/` and checks every glb. `blender/preview/index.html` opens any `field.glb` in a browser; how to serve it is at the top of the file.

Change the model in the scripts, not by hand in the `.blend`, or the next rebuild will overwrite your edits:
- `blender/blueprint.py` reads and checks the blueprint.
- `blender/layout.py` decides where every asset goes.
- `blender/field_build.py` and `blender/piping.py` build the field and its pipes. The civil works are a caliche lease road with a crown, two lanes of darker wheel tracks and sloped shoulders, driveways with their own tracks, caliche pads with soft edges and a low berm along their sides and back, and black HDPE flowlines on timber sleepers (#98).
- `blender/field_parts.py` builds each core asset; `blender/well_parts.py` (pumpjack and wellhead), `blender/truck_parts.py` (vacuum truck) and `blender/swd_parts.py` (disposal facility with its H-pumps) hold the bigger ones. At `--detail full` they build the hero detail; `lite` costs about what full did before #100.
- `blender/detail_equipment.py` adds gauges, sensors, panels, wellhead trees, labels, and load-outs.
- `blender/detail_site.py` adds power, light towers, corrugated steel containment walls with a black liner, disposal well equipment (a skim tank in lined containment and a truck offload header), yard equipment, and truck detail.
- `blender/siting.py` finds free spots for anything added after the core layout, and `blender/equipment_parts.py` builds the blueprint's `equipment`.
- `blender/scenery_parts.py` generates the scenery the CC0 kit lacks: barbed wire fence, yucca, prickly pear, a cattle guard, the lease sign, H2S warning plates, and pipeline markers. `blender/parts_preview.py` builds every generated model in a row to look at.
- `blender/terrain.py` plans the land around the lease: the terrain out to the horizon, its color, and the mesas on it (#98). Plain Python, so the tests check it without Blender.
- `blender/scenery.py` dresses the built field as desert with the kit in `blender/assets/` and those parts, and `siting.plan_scenery()` and `siting.plan_outside()` decide where each piece goes. `blender/beyond.py` builds what stands past the lease on the terrain: the road and power line out, other operators' pads, and the mesas.
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
  "scenery": { "on": true, "textures": ["ground_dry_soil", "ground_caliche"], "fence_m": 1216.0, "cattle_guards": 2,
               "terrain_m": 5400.0, "mesas": 24, "other_leases": 10, "power_spans": 18, "brush_past_fence": 50,
               "items": { "boulder": 4, "branch": 4, "creosote": 1, "grass": 102, "lease_sign": 1,
                          "prickly_pear": 23, "rock": 11, "yucca": 65 },
               "dropped": [], "triangles": 103782, "field_triangles": 154587, "images_mb": 4.36 },
  "triangles": 258369, "glb_mb": 16.33, "nodes": 864, "draw_calls": 750, "compressed": false,
  "optimize": { "merged": 39, "meshes_before": 750, "meshes_after": 318, "unshared": 0, "no_shadow": 32, "purged": 520,
                "instanced": { "SCENERY_dry_grass_2_instances": 102, "SCENERY_rock_1_instances": 6, "...": 5 },
                "instances": 121, "glb_meshes": 318, "instanced_nodes": 7 },
  "budget": { "triangles": 300000, "mb": 30.0, "draw_calls": 1600 },
  "timing": { "plan": 0.0, "field": 3.42, "scenery": 2.61, "checks": 0.0, "look": 0.51, "optimize": 1.77, "export": 2.58, "blend": 0.11, "report": 0.03 },
  "seconds": 11.04, "cpu_seconds": 15.35
}
```

(That build ran with a load average of about 10 on the Mac, with other agents' jobs running.)

`blender/optimize.py` runs right before export. It joins an asset's `<ID>_detail_steel`, `_detail_steel_dark`, `_detail_concrete`, `_detail_gravel` and `_detail_rubber` Kit meshes into their `<ID>_steel`-style sibling, which `web/src/parts.js` already names the same ("pumpjack frame"), so every console label stays and each asset draws one mesh fewer per material. Pipe and tank parts, contract names, and anything with custom properties (like a `userData.part` split for explode) never join. It then points identical meshes at one copy (24 pumpjacks store each part once), turns repeated scenery kit models into GPU-instanced nodes, drops unused data, and marks small scenery `cast_shadow: false` in extras. Every other node keeps its own node, name, parent and transform. `triangles` counts every instance.

**Shadows:** the console casts a shadow from every mesh, which draws it a second time. Grass, rocks, boulders, branches, yucca, prickly pear, the fence, cattle guards and road stubs carry `userData.cast_shadow === false`; only the mesquite, creosote and lease sign keep theirs. Honoring it took the default field's frame from about 13.4 ms to 12.7 ms (#97). In `indexModel()`, after `o.castShadow = o.receiveShadow = true;`, add: `for (let n = o; n; n = n.parent) if (n.userData && n.userData.cast_shadow !== undefined) { o.castShadow = n.userData.cast_shadow !== false; break; }`. `draw_calls` is one per primitive of each mesh node, which is what three.js draws without shadows (shadows draw each mesh again). `timing` gives the wall seconds of each step, and `seconds` the whole build.

`counts` comes from the blueprint and `kinds` from the built scene. `scenery` says what the desert layer added: its triangles on top of `field_triangles`, how far the terrain runs past the lease (`terrain_m`), the mesas and other operators' pads it built, the MB of its textures, and anything it `dropped` to stay in the budget. With `--no-scenery` it is `{"on": false}`. `placed` lists which assets had positions in the blueprint and which were placed automatically. When `ok` is false, `errors` says what to fix.

**Names in this section:** `<tank>`, `<pump>`, `<valve>`, `<well>`, `<swd>`, `<truck>`, and `<yard>` are blueprint IDs, and `n` is a tank ID without its `TANK_` prefix, so `TANK_3` gives `PAD_3`. For today's field they are the same names as before.

**Finding what was clicked:** Every asset is an empty node named with its ID. Static parts are child meshes, like `TANK_3_tank` and `TANK_3_steel`. Walk up from the clicked mesh until a node has `userData.asset_id`. Three.js puts glTF extras in `userData`, and `userData.kind` is `tank`, `pump`, `valve`, `well`, `disposal_well`, `truck`, `yard`, or `equipment`.

**Equipment:** each `equipment` item is an asset node named with its ID, with `userData.kind` `equipment`, `userData.equipment_type` (like `SEPARATOR`), `userData.model` (`generic` or `placeholder`), and `userData.label` when the blueprint gives one. A placeholder has its type and ID painted on top as `<id>_label`. Equipment is display and context only: it is not in `/api/field`, so the console can show its ID and type but has nothing to forecast.

**Moving parts:** Each one has its origin at its pivot, with no rotation in the file. Animate it with one property:

| Node | What it does | How to animate it in three.js |
|---|---|---|
| `<tank>_LEVEL` | Brine level inside the cutaway tank | `scale.y` from 0 (empty) to 1 (full). The file ships at 0.55 for preview. |
| `<pump>_rotor` | Motor fan, so you can see the pump running | Spin `rotation.x` while running, stop when failed |
| `<valve>_wheel` | Handwheel on every valve: the big wheel of a manual gate valve, or the override wheel under a remote valve's actuator. Its pivot is the same either way. Each valve's `userData.actuation` is `manual` or `remote`, from the blueprint's `manual` / `remote` | Turn `rotation.y` while the valve opens or closes |
| `<well>_beam` | Pumpjack walking beam | Rock `rotation.x` by about ±0.25 rad while the well produces |
| `<well>_crank` | Pumpjack crank and counterweights | Spin `rotation.x` while the well produces, stop when shut in |
| `<well>_pitman` | Both pitman arms, a child of `<well>_beam` with its pivot on the equalizer | Optional. As shipped it rocks with the beam. To keep its foot on the wrist pins, each frame set `rotation.x = atan2(dy, -dz) - beam.rotation.x`, where `(dy, dz)` runs from the equalizer to the wrist pin in the well's frame, in Blender axes (y along the beam toward the motor, z up; three.js local `-z` and `y`). The equalizer is the point `(y 3.55, z -0.24)` in the beam's frame, so in the well's frame it is that point turned by the beam's angle plus `(y 0, z 4.1)`; the pin is `(y 2.7 + 0.85 cos c, z 1.6 + 0.85 sin c)`, with `c` the crank's `rotation.x` |
| `<well>_bridle` | Wireline bridle, carrier bar and polished rod, a child of `<well>_beam` with its pivot where the line leaves the horse head | Optional. As shipped it rocks with the beam. Set `rotation.x = -beam.rotation.x` each frame to keep it plumb over the wellhead, as a real bridle hangs |
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

**IoT sensor nodes (#46):** `SENSOR_<asset>` is a field instrument with a wireless adapter and the `SENSOR_<asset>_light` LED, mounted on an asset and parented to it, so clicking it selects that asset. Its `userData.device` says which instrument it is (#100): `radar_level` on a tank roof, `pressure` (a transmitter on a manifold) on pumps, wellheads, disposal wells and equipment, `position` on a valve, `telematics` on a truck, `gateway` on the yard. The assets named in the blueprint's `connectors[].signals` get one each, and with no connectors every tank and pump does. Its `userData` has `kind` `sensor`, `for_asset` (the asset ID), `signals`, and `connectors`, the last two as comma-separated text like `"running,flow_bbl_hr"` and `"CONN_SIM"` (empty with no connectors). Today's field has `SENSOR_TANK_1` to `SENSOR_TANK_6` and `SENSOR_PUMP_1` to `SENSOR_PUMP_6`.

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
- `PAD_n` holds each tank pad's caliche (`PAD_n_gravel`) and berm (`PAD_n_berm`), steel containment, liner, step-over, sign, light tower, and `RTU_PAD_n`.
- `LOADOUT_n` is the truck hookup stand.
- `SITE_*` is the ground, roads, pipe supports, and the power line.
- `CAM_OVERVIEW` is a camera framing the whole field.
- Names ending in `_label`, `_sign`, or `_tag` are painted numbers and signs.
- `SCENERY` is the desert layer (#88, #98): one root Empty with `userData.kind` `scenery`, and every node under it is named `SCENERY_*`. Everything past the lease ground (`SITE_ground`) is under it, so hiding it leaves the plain lease. It holds `SCENERY_terrain`, the ground from the lease's edge out about 5.4 km to the horizon, sharing its edge with `SITE_ground` so there is no seam; `SCENERY_mesas`, a ring of caprock mesas 3 to 5 km out; `SCENERY_road`, the lease road running out of the lease both ways; `SCENERY_power_*`, the lease's power line continued 1.6 km west on wood poles; `SCENERY_neighbors_*`, other operators' caliche pads with far pumpjacks, small tanks and spur roads, 300 m to 2.4 km past the lease's ends; the barbed wire fence along the lease edge (`SCENERY_fence_*`) and the cattle guards where the road leaves (`SCENERY_cattle_guard_*`); `SCENERY_signs_*`, an RRC style lease sign at the west entrance (a made-up operator and lease number), a generic H2S warning on each pad's sign post, and yellow pipeline markers where the trunk runs under a driveway; and a seeded scatter of plants and rocks, thickest along the fence and thinning out in a 45 m band past it: kit models placed more than once are GPU-instanced (`EXT_mesh_gpu_instancing`, #97), one node per model like `SCENERY_dry_grass_2_instances` and `SCENERY_rock_1_instances`, which three.js loads as one `InstancedMesh` and one draw call for every copy; a model placed once stays a plain node, like `SCENERY_mesquite_1`; and one merged mesh per material for the generated plants (`SCENERY_yucca_*`, `SCENERY_prickly_pear_*`). Nothing under it has an asset ID, so it is never clickable. Hide the whole layer with `scene.getObjectByName("SCENERY").visible = false`. It stays off pads, roads, pipe runs, and truck routes, and it is the same on every build of the same blueprint.
- Surfaces outside `SCENERY` that the scenery pass dresses: the ground (`M_GROUND`) gets the kit's dry soil with its broad patches filtered out, and the road (`M_ROAD`), pads and yard (`M_GRAVEL`), and berms (`M_BERM`) get the kit's caliche. Each multiplies a vertex color layer (`COLOR_0` in the glb) into its texture: patches and caliche dust on the ground, wheel tracks and shoulders on the road. Well flowlines (`PIPE_<well>_<tank>`) are `M_HDPE`, the containment liner is `M_LINER`, signs are `M_SIGN` and `M_HAZARD`, and the mesas are `M_MESA` (vertex colors only).

**Size:** Today's field is about 258,000 triangles, 16.3 MB and 750 draw calls with its scenery, or 155,000 triangles with `--no-scenery`. The 12-tank sample is about 299,000 triangles, 13.6 MB and 1,411 draw calls: its field alone is 298,000 triangles, so its scenery is only the coarse terrain. All are inside the 300,000 triangle, 30 MB and 1,500 draw call budget. Scenery only uses what the field leaves: it fills up to 95% of the triangle budget. The terrain always comes, coarser when little is left; then, while they fit, the mesas, the road out of the lease, the signs, the power line out, other operators' pads, and the brush past the fence; the fence and the plants get the rest, and the fence is dropped when it can't fit. The generator estimates the size before it builds. If a field has more than 12 tanks, or full detail would pass the budget, it drops small detail: bolts, small labels, tags, signs, and containment and fence posts, then wellhead trim if that is still too much. `detail` in the report says what was dropped. Asset nodes, tank numbers (`<tank>_label`), lights, sensors, moving parts, pipes, and waypoints are never dropped.

**Scale and axes:** Today's field runs 450 m along X, and the report gives `layout.road_m` for any field. In three.js, Y is up and the lease road runs along X near Z = 0, with the first row of tank pads toward negative Z and a second row, if any, toward positive Z.

## Status colors

The backend sends the status. The web console picks the color.

| Status | Meaning | Look |
|---|---|---|
| `OK` | Normal | Gray, no color |
| `WARNING` | Needs attention soon | Amber |
| `ALARM` | Failed or at risk now | Red |
| `OFFLINE` | Stopped or shut in | Dark |

## Dataset mode (EOG sponsor data)

Everything under `/api/eog` serves the EOG track's sponsor datasets: the real network, its week of telemetry, and the events, plans and alerts built from them. Code: `backend/app/eog/`, where `api.py` is a thin layer over `service.py`, and detection, plans and the 3D layout come from `detect.py`, `plan.py` and `blueprint.py`. The other routes in this file are unchanged.

**Turning it on.** Start the backend with `FREEBOARD_EOG_DIR` set to a folder of datasets, such as a checkout of the sponsor repo. Every folder under it that holds `network.json`, `ops.json` and `telemetry.csv` is a dataset, and its id is its path under that folder, like `history/full_week`. `FREEBOARD_EOG_DATASET` picks the dataset shown at startup (an id, or a folder name such as `full_week`). Without it the backend picks `test`, then `history/full_week`, then the first dataset it finds. With `FREEBOARD_EOG_DIR` unset, every route answers 503 with one sentence that says how to turn it on.

**Rules for every route**

- **Times** are data time, never the server's clock. Each one comes as a pair: `onset` is ts_unix seconds (an integer) and `onset_ts` is the same moment in ISO 8601 UTC, like `2026-10-08T09:00:00Z`. Text such as "13:25" is UTC too, with the weekday added when it isn't on the day of `now`.
- **`?at=`** Every GET takes an optional `at`: ts_unix seconds, or ISO 8601 (UTC unless it carries an offset). Numbers of 10^11 or more are read as milliseconds, which is what JavaScript's `Date.now()` gives. `at` is clamped into the dataset's range, and anything unreadable is a 422. Without `at`, a GET reads the data at the clock's cursor (`GET /api/eog/clock`). `datasets` and `clock` check `at` but always describe the real cursor.
- **Every view starts with** `dataset`, `at`, `at_ts`, `now` and `now_ts`. `now` is the last telemetry row at or before `at`, and nothing after it is read, so time travel shows exactly what was known then.
- **Errors** are `{"detail": "<one sentence>"}`. 503: dataset mode is off, the dataset can't be loaded, or a module the view needs (`detect.py`, `plan.py`) isn't merged yet. 404: an unknown dataset, event, facility, asset or metric. 422: bad input, and the sentence says what to fix.
- **POST routes** need the operator token in `X-Freeboard-Token` when `backend/app/auth.py` is present and a token is configured: 401 without it, 403 for the phone token, 429 after too many wrong PINs. With no token configured they are open. If `auth.py` is present but can't be used, POST routes answer 503 and say why instead of opening. GET routes and the WebSocket only read, so they are always open. A POST body is a JSON object, an empty body counts as `{}`, and an unknown field is a 422.
- **JSON** never holds NaN or Infinity. A number that isn't known is `null`, and a missing reading never makes an asset `OK`.
- **Event ids** (`EV-0001`, ...) number the events in onset order within a view, so they hold as the cursor moves forward unless detection re-dates an onset. When you approve, send back the `at` of the payload the id came from.
- **Statuses** are `OK`, `INSPECT`, `WARNING` and `ALARM`, least to most severe. `INSPECT` is new in dataset mode: a suspected leak or a worn pump that needs a person on site. The console picks its look. `OFFLINE` isn't used here, and a shut-in well is `ALARM`.

### `GET /api/eog/datasets`

```json
{
  "enabled": true,
  "active": "history/snapshot_2026-10-08T1200",
  "datasets": [{ "id": "history/full_week" }, { "id": "history/snapshot_2026-10-08T1200" }],
  "clock": { "dataset": "history/snapshot_2026-10-08T1200", "at": 1791460800, "playing": false },
  "warnings": []
}
```

`clock` is the whole `GET /api/eog/clock` answer. `warnings` says when `FREEBOARD_EOG_DATASET` names no dataset. If the active dataset can't be loaded, `clock` is `null` and `error` says why.

### `POST /api/eog/datasets/activate`

Request: `{ "id": "history/full_week" }`. It switches the dataset for everyone and answers like `GET /api/eog/datasets`. The cursor moves to the new dataset's last row, paused, at the same speed. 404 for an unknown id. 422 if the folder can't be loaded, and the old dataset stays active.

### `GET /api/eog/clock`

```json
{
  "dataset": "history/snapshot_2026-10-08T1200",
  "start": 1791158400, "start_ts": "2026-10-05T00:00:00Z",
  "last": 1791460800, "last_ts": "2026-10-08T12:00:00Z",
  "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
  "now": 1791460800, "now_ts": "2026-10-08T12:00:00Z",
  "step_s": 300,
  "playing": false,
  "speed": 5.0,
  "live": true
}
```

- `at` is the cursor, and `now` is the last row at or before it. `live` is true while the cursor is on the last row.
- `step_s` is the telemetry's row spacing, read from the file.
- `speed` is data-minutes per real second, so 5 plays one 5-minute row a second. Autoplay pauses by itself at `last`.

### `POST /api/eog/clock`

Request: any of `{ "at": "2026-10-08T09:00:00Z", "play": true, "speed": 30 }`. `at` moves the cursor (clamped), `play` starts or pauses autoplay, and `speed` must be above 0 and at most 1440 (a day per second). It answers like `GET /api/eog/clock`, plus a `note` when `play` is asked for at the end of the data.

### `GET /api/eog/network`

Everything that doesn't change during the week, from `network.json` and `ops.json`.

```json
{
  "dataset": "history/snapshot_2026-10-08T1200", "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
  "now": 1791460800, "now_ts": "2026-10-08T12:00:00Z",
  "limits": { "warning_pct": 85.0, "shutdown_pct": 92.0 },
  "prices": { "oil_per_bbl": 60.0, "gas_per_mcf": 3.0 },
  "repair_hours": 6.0,
  "trucking": { "truck_capacity_bbl": 150.0, "cost_per_trip": 905.0 },
  "systems": [{
    "id": "S1", "center": [31.85, -102.3], "truck_yard": [31.65, -102.6],
    "disposal": { "id": "S1_DISPOSAL", "lat": 31.87, "lon": -102.295, "pressure_psi": 1050.0 },
    "fleet_size": 2, "facility_ids": ["S1_FACILITY_A", "S1_FACILITY_B", "S1_FACILITY_C"]
  }],
  "facilities": [{
    "id": "S1_FACILITY_A", "system": "S1", "lat": 31.912, "lon": -102.34,
    "tank": { "id": "S1_TANK_A", "capacity_bbl": 1200.0, "warning_pct": 85.0, "shutdown_pct": 92.0,
              "warning_bbl": 1020.0, "shutdown_bbl": 1104.0 },
    "pump": { "id": "S1_PUMP_A", "max_flow_bpd": 3600.0, "start_level_pct": 70.0, "stop_level_pct": 30.0 },
    "pipeline": { "id": "S1_PIPELINE_A", "max_bpd": 4000.0, "max_pressure_psi": 1700.0, "disposal_id": "S1_DISPOSAL" },
    "response_min": 75.0, "cycle_min": 240.0,
    "well_ids": ["S1_WELL_A1", "S1_WELL_A2", "S1_WELL_A3"], "baseline_water_bpd": 2400.0, "revenue_per_hour": 4125.0
  }],
  "wells": [{
    "id": "S1_WELL_C2", "facility": "S1_FACILITY_C", "system": "S1", "baseline_water_bpd": 700.0,
    "oil_bpd": 170.0, "gas_mcfd": 600.0, "revenue_per_day": 12000.0, "revenue_per_hour": 500.0,
    "cost_per_bbl_avoided": 17.142857142857142
  }],
  "links": [
    { "from": "S1_WELL_A1", "to": "S1_TANK_A", "kind": "water" },
    { "from": "S1_TANK_A", "to": "S1_PUMP_A", "kind": "tank_outlet" },
    { "from": "S1_PUMP_A", "to": "S1_PIPELINE_A", "kind": "discharge" },
    { "from": "S1_PIPELINE_A", "to": "S1_DISPOSAL", "kind": "pipeline" }
  ],
  "layout": [{
    "id": "S1_TANK_A", "kind": "tank", "system": "S1", "facility": "S1_FACILITY_A", "label": "S1 Tank A",
    "lat": 31.912, "lon": -102.34, "latlon_from": "facility", "x_m": -42.67, "y_m": 123.86,
    "links": ["S1_PUMP_A"], "fed_by": ["S1_WELL_A1", "S1_WELL_A2", "S1_WELL_A3"],
    "blueprint_id": "TANK_S1_TANK_A", "node": "TANK_S1_TANK_A", "metrics": ["level_pct"], "synthetic": false
  }],
  "warnings": []
}
```

- A facility's `revenue_per_hour` is what its wells earn in an hour, which is what a shut-in loses. A well's `cost_per_bbl_avoided` is its revenue per barrel of water it makes (`null` if it makes none).
- `response_min` is the drive from the truck yard to the facility, and `cycle_min` one round trip after that.
- `layout` is `blueprint.py`'s `field_assets`, as that module documents it: every asset (systems, facilities, tanks, pumps, pipelines, wells, disposal sites, truck yards and trucks) with its label, position (`lat`, `lon`, and `x_m`, `y_m` in meters for the 3D field), links and glb node. It is `null` until `blueprint.py` is merged. If it fails, it is `null` and `warnings` says why.

### `GET /api/eog/state`

Every asset's readings and status at `now`, keyed by asset id.

```json
{
  "dataset": "history/snapshot_2026-10-08T1200", "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
  "now": 1791460800, "now_ts": "2026-10-08T12:00:00Z",
  "limits": { "warning_pct": 85.0, "shutdown_pct": 92.0 },
  "assets": {
    "S2_PUMP_B": { "kind": "pump", "system": "S2", "facility": "S2_FACILITY_B", "status": "ALARM",
                   "readings": { "discharge_psi": 1000.6, "flow_bpd": 0.0, "status": 2.0 },
                   "events": ["EV-0011"], "reasons": ["Pump failure: the pump has stopped moving water."] },
    "S2_TANK_B": { "kind": "tank", "system": "S2", "facility": "S2_FACILITY_B", "status": "ALARM",
                   "readings": { "level_pct": 78.49 },
                   "events": ["EV-0011"], "reasons": ["Its pump has failed, so the tank is filling."] },
    "S5_PIPELINE_B": { "kind": "pipeline", "system": "S5", "facility": "S5_FACILITY_B", "status": "INSPECT",
                       "readings": { "pressure_psi": 1038.7, "receive_flow_bpd": 2446.6 }, "events": ["EV-0010"],
                       "reasons": ["Possible leak between the pump and the disposal site: send an inspector now."] }
  },
  "systems": { "S1": "OK", "S2": "ALARM", "S3": "INSPECT", "S4": "INSPECT", "S5": "INSPECT" },
  "counts": { "OK": 95, "INSPECT": 6, "WARNING": 0, "ALARM": 9 },
  "events": {
    "EV-0011": { "category": "pump_failure", "system": "S2", "facility": "S2_FACILITY_B", "asset": "S2_PUMP_B",
                 "onset": 1791450000, "onset_ts": "2026-10-08T09:00:00Z" }
  },
  "warnings": []
}
```

- `kind` is `pump`, `tank`, `pipeline`, `well`, `facility` or `disposal`. `readings` holds every metric the telemetry has for the asset at `now`, `null` where that row is missing. `events` lists the ongoing events that touch the asset (a failed pump also touches its tank, wells and facility). `reasons` has a plain sentence for each status other than `OK`.
- Pump: `ALARM` on a pump failure or a fault (status 2), `INSPECT` when it is wearing out, `WARNING` with no status reading.
- Tank: `ALARM` at or above shutdown or while its pump is down, `WARNING` at or above the warning limit or with no level reading.
- Pipeline: `INSPECT` on a suspected leak, `WARNING` when none of its readings came in.
- Well: `ALARM` when shut in (status 0), `WARNING` with no status reading.
- A facility is the worst of its parts, a system the worst of its facilities, and a disposal site `OK`.
- The top-level `events` lists every ongoing event. The state view never fails because detection does: if `detect.py` is missing or raises, the statuses come from the readings alone and `warnings` says so.

### `GET /api/eog/alerts`

The ongoing events in SPEC's alerts-view order: pump failures first, soonest shutdown first (ties go to the higher lost revenue per hour), then leaks and worn pumps.

```json
{
  "dataset": "history/snapshot_2026-10-08T1200", "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
  "now": 1791460800, "now_ts": "2026-10-08T12:00:00Z",
  "count": 6,
  "counts": { "pump_failure": 3, "pipeline_leak": 1, "pump_degradation": 2, "other": 0 },
  "alerts": [{
    "order": 1, "event_id": "EV-0011", "category": "pump_failure", "status": "ongoing",
    "system": "S2", "facility": "S2_FACILITY_B", "asset": "S2_PUMP_B",
    "onset": 1791450000, "onset_ts": "2026-10-08T09:00:00Z", "detected_at": 1791450000,
    "confidence": 0.9, "evidence": ["Pump status 2 and zero flow since 09:00."], "urgent": true,
    "title": "S2_PUMP_B failed: S2_TANK_B reaches shutdown at 13:25 (in 1 h 25 min)",
    "countdown": {
      "tank_id": "S2_TANK_B", "level_pct": 78.6, "hours_to_limit": { "p50": 1.42, "p10": 1.42, "p90": 1.42 },
      "trips_at": 1791465900, "trips_at_ts": "2026-10-08T13:25:00Z",
      "repair_end": 1791471600, "repair_end_ts": "2026-10-08T15:00:00Z",
      "tripped": false, "lost_revenue_per_hour": 2450.0
    },
    "approval": null,
    "action": {
      "type": "plan", "summary": "1 truck, 1 trip (~$905)",
      "actions": [{ "type": "dispatch_truck", "facility_id": "S2_FACILITY_B", "trucks": 1, "trips_per_truck": 1 }],
      "cost": 905.0, "truck_cost": 905.0, "shut_in_cost": 0.0, "auto_shut_in_cost": 0.0,
      "trips": false, "trip_at": null, "trip_at_ts": null, "peak_level_pct": 89.711,
      "expires_at": 1791461640, "expires_at_ts": "2026-10-08T12:14:00Z", "shares_with": ["EV-0012", "EV-0013"]
    }
  }]
}
```

- `countdown` is only on pump failures (`null` otherwise). `trips_at` is when the tank reaches shutdown if nobody acts, `null` when the repair comes first. `tripped` is true once the tank has reached shutdown and its wells are shut in.
- `action` for a pump failure is its recommended plan, with `summary` "No plan is available yet." and no actions while there is none. `expires_at` is Last Call: the last start time at which the plan still works. For a leak or a worn pump, `action` is `{ "type": "field_inspection", "asset_id": "S5_PIPELINE_B", "summary": "Send an inspector to S5_PIPELINE_B." }`.
- `urgent` is true for pump failures and leaks. `approval` is the recorded approval (see approve) or `null`.

### `GET /api/eog/events`

Every event up to `now` in SPEC's events.json shape, the file the track asks for. Ongoing pump failures carry their plan from `now`, and resolved ones the plan made at their onset.

```json
{
  "dataset": "history/snapshot_2026-10-08T1200", "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
  "now": 1791460800, "now_ts": "2026-10-08T12:00:00Z",
  "count": 13,
  "events": [{
    "event_id": "EV-0011", "system_id": "S2", "facility_id": "S2_FACILITY_B", "asset_id": "S2_PUMP_B",
    "category": "pump_failure", "status": "ongoing",
    "onset_ts": "2026-10-08T09:00:00Z", "onset_unix": 1791450000, "end_ts": null, "end_unix": null,
    "confidence": 0.9, "evidence": ["Pump status 2 and zero flow since 09:00."],
    "level_pct": 78.6, "hours_to_limit": { "p50": 1.42, "p10": 1.42, "p90": 1.42 },
    "recommended_actions": [{ "type": "dispatch_truck", "facility_id": "S2_FACILITY_B", "trucks": 1, "trips_per_truck": 1 }],
    "est_cost": 905.0
  }]
}
```

The field names are SPEC's (`system_id`, `onset_unix`), kept as SPEC writes them. `?download=1` sends only the list, as an `events.json` attachment.

### `GET /api/eog/plans`

`?mode=now`, the default: a plan from `now` for every ongoing pump failure, planned together per system so they share its trucks.

```json
{
  "dataset": "history/snapshot_2026-10-08T1200", "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
  "now": 1791460800, "now_ts": "2026-10-08T12:00:00Z",
  "mode": "now",
  "plans": [{
    "event": { "id": "EV-0011", "category": "pump_failure", "asset": "S2_PUMP_B", "level_pct": 78.6 },
    "plan": {
      "event_id": "EV-0011", "facility_id": "S2_FACILITY_B", "system": "S2",
      "start": 1791460800, "start_ts": "2026-10-08T12:00:00Z",
      "actions": [{ "type": "dispatch_truck", "facility_id": "S2_FACILITY_B", "trucks": 1, "trips_per_truck": 1 }],
      "summary": "1 truck, 1 trip (~$905)",
      "cost": 905.0, "truck_cost": 905.0, "shut_in_cost": 0.0, "auto_shut_in_cost": 0.0,
      "trips": false, "trip_at": null, "trip_at_ts": null, "peak_level_pct": 89.711,
      "trucks_free": 2, "fleet_size": 2, "shares_with": ["EV-0012", "EV-0013"],
      "arrivals": [1791465000], "arrivals_ts": ["2026-10-08T13:10:00Z"], "busy_until": [1791479700],
      "expires_at": 1791461640, "expires_at_ts": "2026-10-08T12:14:00Z",
      "notes": ["shares S2's 2 trucks with EV-0012, EV-0013"]
    },
    "trucks_free": 1,
    "alternatives": [{ "summary": "1 truck, 1 trip (~$905)", "cost": 905.0, "trips": false }],
    "wait_curve": [{ "delay_min": 0, "start": 1791460800, "start_ts": "2026-10-08T12:00:00Z", "cost": 905.0,
                     "summary": "1 truck, 1 trip (~$905)", "trucks": 1, "system_cost": 3108.33 }],
    "approval": null
  }],
  "systems": [{ "system": "S2", "fleet_size": 2, "events": ["EV-0011", "EV-0012", "EV-0013"], "trucks_used": 2, "cost": 3108.33 }]
}
```

- `event` is the whole Event from `backend/app/eog/model.py`: `id`, `category`, `system`, `facility`, `asset`, `onset`, `end`, `status`, `confidence`, `evidence`, `detected_at`, `level_at_onset_pct`, `level_pct`, `hours_to_limit`, `trips_at`, `repair_end`, `tripped`, `lost_revenue_per_hour`, plus `onset_ts`, `end_ts`, `trips_at_ts` and `repair_end_ts`. `plan` is the whole Plan from the same file.
- The item's `trucks_free` is the system's fleet minus the trucks the joint plan gives the system's other failures. `alternatives` are plans in the same shape for this failure on its own with those trucks, cheapest first, and they include doing nothing.
- `wait_curve` is Last Call, the price of waiting: for each 5-minute delay until the repair, what it costs to do nothing until then and follow the best plan from there (`cost` for this event, `system_cost` for its whole system).
- `?mode=reference` is history mode: every pump failure up to `now`, each planned alone at its onset with the trucks still out on earlier plans, as the answer keys' `reference_action`. Its items are `{ "event", "plan", "approval" }`, and `trucks_used` is `null`.

### `POST /api/eog/whatif`

Replays one facility's ongoing pump failure from `now` until the repair, with the actions you give and with no action.

Request:

```json
{
  "facility_id": "S2_FACILITY_B",
  "actions": [
    { "type": "dispatch_truck", "trucks": 1, "trips_per_truck": 2 },
    { "type": "shut_in_wells", "wells": ["S2_WELL_B1"], "start_after_min": 30 }
  ],
  "at": "2026-10-08T12:00:00Z"
}
```

`actions` take the events.json action shapes (`dispatch_truck`, `shut_in_wells`, `field_inspection`, `none`): at most 20, all for this facility, and no more trucks in all than the system's fleet. A truck action's `facility_id` defaults to the request's. `at` is optional.

Response:

```json
{
  "dataset": "history/snapshot_2026-10-08T1200", "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
  "now": 1791460800, "now_ts": "2026-10-08T12:00:00Z",
  "facility_id": "S2_FACILITY_B",
  "event": { "id": "EV-0011", "category": "pump_failure", "asset": "S2_PUMP_B" },
  "start": 1791460800, "level_pct": 78.6,
  "repair_end": 1791471600, "repair_end_ts": "2026-10-08T15:00:00Z",
  "actions": [
    { "type": "dispatch_truck", "facility_id": "S2_FACILITY_B", "trucks": 1, "trips_per_truck": 2 },
    { "type": "shut_in_wells", "wells": ["S2_WELL_B1"], "start_after_min": 30 }
  ],
  "result": {
    "points": [[1791460800, 78.6], [1791461100, 79.3937]],
    "trip_at": null, "peak_level_pct": 88.1238,
    "truck_cost": 905.0, "shut_in_cost": 3093.75, "auto_shut_in_cost": 0.0, "cost": 3998.75,
    "arrivals": [1791465000], "removed_bbl": 150.0,
    "errors": ["Trip 2 of 2 would arrive after the repair ends, so it is left out."]
  },
  "baseline": {
    "points": [[1791460800, 78.6], [1791461100, 79.3937]],
    "trip_at": 1791465840, "peak_level_pct": 92.0,
    "truck_cost": 0.0, "shut_in_cost": 0.0, "auto_shut_in_cost": 3902.85, "cost": 3902.85,
    "arrivals": [], "removed_bbl": 0.0, "errors": []
  },
  "savings": -95.9
}
```

- `level_pct` is where the replay starts: the planner's own de-noised level, so replaying the recommended actions gives the recommended plan's cost.
- `result` and `baseline` come from `plan.py`'s replay. `points` are `[ts_unix, level_pct]` pairs, `trip_at` is when the tank reaches shutdown (`null` if it doesn't), and `errors` lists anything left out, such as a trip that would land after the repair.
- `savings` is the baseline cost minus the result's, and it is negative when the actions cost more than doing nothing.
- 422 when the facility has no ongoing pump failure at `now`, or no level to start from.

### `GET /api/eog/series`

A reading history for charts: `?asset_id=S2_TANK_B&metric=level_pct&from=...&to=...&max_points=500`.

```json
{
  "dataset": "history/snapshot_2026-10-08T1200", "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
  "now": 1791460800, "now_ts": "2026-10-08T12:00:00Z",
  "asset_id": "S2_TANK_B",
  "from": 1791158400, "from_ts": "2026-10-05T00:00:00Z", "to": 1791460800, "to_ts": "2026-10-08T12:00:00Z",
  "max_points": 6,
  "metric": "level_pct",
  "raw_points": 1009,
  "points": [[1791159000, 70.76], [1791235800, 29.56], [1791327600, 29.68], [1791342900, 70.24], [1791373500, 29.74], [1791460800, 78.49]]
}
```

- `from` and `to` take the same forms as `at`. They default to the dataset's start and to `now`, and nothing after `now` is shown.
- `max_points` (2 to 20000, default 500) caps the points. Past it, the series is cut into buckets and each keeps its lowest and highest reading, so spikes survive. `raw_points` is the count before that. Missing rows are skipped.
- Without `metric`, you get every metric of the asset: `metrics` maps each one to its points, and `raw_points` maps each one to its count.
- 404 for an unknown asset or a metric it doesn't have.

### `GET /api/eog/handoff`

`?shift_hours=12` (1 to 168). A plain-English shift handoff that reads in about 30 seconds, built from the alerts, plans and events (no AI).

```json
{
  "dataset": "history/snapshot_2026-10-08T1200", "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
  "now": 1791460800, "now_ts": "2026-10-08T12:00:00Z",
  "shift_hours": 12.0, "shift_start": 1791417600, "shift_start_ts": "2026-10-08T00:00:00Z",
  "lines": [
    "Handoff Thu 2026-10-08 12:00 UTC: 3 pumps down, 3 inspections open.",
    "1. S2_PUMP_B down since 09:00: S2_TANK_B 79%, shuts in at 13:25, 1 h 25 min left. Do: 1 truck, 1 trip (~$905), start by 12:14.",
    "2. S2_PUMP_C down since 09:30: S2_TANK_C 69%, shuts in at 14:20, 2 h 20 min left. Do: shut in S2_WELL_C1 at +46 min, ~94 min before the trip (~$1,298), start by 12:46.",
    "3. S2_PUMP_A down since 10:00: S2_TANK_A 68%, shuts in at 14:35, 2 h 35 min left. Do: 1 truck, 1 trip (~$905), start by 13:10.",
    "Inspect: leak on S5_PIPELINE_B since 06:00, worn S3_PUMP_A since Wed 08:00 and 1 more.",
    "Resolved this shift: S1_PUMP_C failure (ended 06:20) and 1 more.",
    "Plans total $3,108."
  ],
  "text": "Handoff Thu 2026-10-08 12:00 UTC: 3 pumps down, 3 inspections open.\n1. S2_PUMP_B down since 09:00: ...",
  "words": 120,
  "read_s": 30,
  "counts": { "failing": 3, "inspect": 3, "resolved": 2 },
  "total_cost": 3108.33
}
```

- `text` is the `lines` joined with newlines. `read_s` is the reading time at 240 words a minute.
- Every failed pump is named: the first three in full, the rest on one "Also down:" line. The inspect and resolved lists end in "and N more" when they would pass the 30-second budget (120 words). The alerts view has the rest.
- A plan someone approved reads "approved by Dana" instead of "start by 12:14". With nothing going on, the first line says "all clear".

### `POST /api/eog/plans/{event_id}/approve`

Request, both fields optional: `{ "by": "Dana", "at": 1791460800 }`. Send the `at` of the alerts or plans payload the id came from. With a token configured, the approver is the device the token belongs to, not the body's `by`.

```json
{
  "dataset": "history/snapshot_2026-10-08T1200", "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
  "now": 1791460800, "now_ts": "2026-10-08T12:00:00Z",
  "event_id": "EV-0011", "category": "pump_failure", "facility_id": "S2_FACILITY_B",
  "summary": "1 truck, 1 trip (~$905)",
  "actions": [{ "type": "dispatch_truck", "facility_id": "S2_FACILITY_B", "trucks": 1, "trips_per_truck": 1 }],
  "cost": 905.0,
  "expires_at": 1791461640, "expires_at_ts": "2026-10-08T12:14:00Z",
  "steps": [
    { "n": 1, "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z", "asset_id": "S2_FACILITY_B",
      "text": "Call 1 truck from the S2 truck yard to S2_FACILITY_B now (12:00), 1 trip per truck." },
    { "n": 2, "at": 1791465000, "at_ts": "2026-10-08T13:10:00Z", "asset_id": "S2_FACILITY_B",
      "text": "First load: 1 truck at S2_FACILITY_B at 13:10, hauling 150 bbl." },
    { "n": 3, "at": 1791471600, "at_ts": "2026-10-08T15:00:00Z", "asset_id": "S2_PUMP_B",
      "text": "The S2_PUMP_B repair is due at 15:00." }
  ],
  "radio_script": "Field, this is control. S2_PUMP_B at S2_FACILITY_B has been down since 09:00. S2_TANK_B is at 79 percent. With no action it shuts in at 13:25. Plan: 1 truck, 1 trip (~$905). ... This plan only works if it starts by 12:14. Read back when ready. Control out.",
  "approval": {
    "event_id": "EV-0011", "dataset": "history/snapshot_2026-10-08T1200", "category": "pump_failure",
    "facility_id": "S2_FACILITY_B", "asset_id": "S2_PUMP_B", "onset": 1791450000,
    "by": "Dana", "at": 1791460800, "at_ts": "2026-10-08T12:00:00Z",
    "summary": "1 truck, 1 trip (~$905)", "actions": [{ "type": "dispatch_truck", "facility_id": "S2_FACILITY_B", "trucks": 1, "trips_per_truck": 1 }],
    "cost": 905.0, "seq": 1
  }
}
```

- A pump failure approves its current plan. The steps call the trucks (each load arrives `response_min` after the call, then one every `cycle_min`), time each shut-in, and end with the repair. A leak or a worn pump approves an inspection, with one step.
- The approval is kept in memory: it shows as `approval` on the event's alert and plan and in the handoff, it stays with its dataset when you switch, and a restart forgets it. It never changes the data, the events or the plans. Approving the same event again replaces it. `by` defaults to "operator".
- 404 for an id that isn't in the view. 422 when the event has already resolved, or has no plan yet.

### WebSocket `/api/eog/ws`

A read-only push, so it needs no token.

```json
{ "type": "tick", "dataset": "history/full_week", "clock": { "at": 1791460800, "playing": true, "speed": 5.0 }, "alerts": [] }
```

- `clock` is the whole `GET /api/eog/clock` answer and `alerts` is the `alerts` list of `GET /api/eog/alerts`, both at the cursor.
- A tick comes on connect and whenever something changes: a new data row while playing, any cursor move while paused, play, pause or speed, a dataset switch, a change to the dataset's files, or an approval. The server checks four times a second.
- When the alerts can't be built, the tick still carries the clock, with `"alerts": null` and `"error": { "status": 503, "detail": "..." }`. A problem with the dataset itself arrives once as `{ "type": "error", "status": 503, "detail": "..." }` (500 for an internal error), and ticks resume when it clears. With dataset mode off, the socket sends that error and closes with code 1013.
- Messages from the client are ignored.

## API

The default server address is `http://127.0.0.1:8000`. All numbers below are examples. Times are minutes from now.

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
