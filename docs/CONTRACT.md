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

`blender/field.glb` is built by a script, so anyone can rebuild it. Run this from the repo root:

```bash
/Applications/Blender.app/Contents/MacOS/Blender --background --factory-startup --python blender/build_field.py
```

It writes `blender/field.blend` and `blender/field.glb`. Change the model in `blender/build_field.py` (the layout) and `blender/field_parts.py` (each asset), not by hand in the `.blend`, or the next rebuild will overwrite your edits.

**Finding what was clicked:** Every asset is an empty node named with its ID. Static parts are child meshes, like `TANK_3_tank` and `TANK_3_steel`. Walk up from the clicked mesh until a node has `userData.asset_id`. Three.js puts glTF extras in `userData`.

**Moving parts:** Each one has its origin at its pivot, with no rotation in the file. Animate it with one property:

| Node | What it does | How to animate it in three.js |
|---|---|---|
| `TANK_n_LEVEL` | Brine level inside the cutaway tank | `scale.y` from 0 (empty) to 1 (full). The file ships at 0.55 for preview. |
| `PUMP_n_rotor` | Motor fan, so you can see the pump running | Spin `rotation.x` while running, stop when failed |
| `VALVE_*_wheel` | Handwheel on every valve | Turn `rotation.y` while the valve opens or closes |
| `WELL_k_beam` | Pumpjack walking beam | Rock `rotation.x` by about ±0.25 rad while the well produces |
| `WELL_k_crank` | Pumpjack crank and counterweights | Spin `rotation.x` while the well produces, stop when shut in |

**Pipes:** Every pipe is its own mesh named for what it connects, with `userData.pipe_id`:

| Pipes | Connects |
|---|---|
| `PIPE_WELL_k_TANK_n` | A well's flowline to its tank's inlet header |
| `PIPE_INLET_n`, `PIPE_INLET_n_RISER` | The inlet header and the riser into the top of Tank n |
| `PIPE_TANK_n_PUMP_n` | Tank n's outlet to its transfer pump |
| `PIPE_PUMP_n_VALVE_D_n`, `PIPE_VALVE_D_n_TRUNK` | The pump through its discharge valve to the trunk |
| `PIPE_TRUNK_0` to `PIPE_TRUNK_6` | The trunk line, west to east, between `SWD_1`, each pump's tie-in, and `SWD_2` |
| `PIPE_XO_34_W`, `PIPE_XO_34_E` | The crossover, on each side of `VALVE_XO_34` |

**Truck routes:** Empty nodes mark the road, so the console can drive a truck along it:
- `STOP_YARD_1`, `STOP_TANK_1` to `STOP_TANK_6`, `STOP_SWD_1`, and `STOP_SWD_2` are where a truck parks.
- `ROAD_JCT_*` is where each of those driveways meets the lease road, and the road runs straight along X between `ROAD_END_W` and `ROAD_END_E`.
- A route is the start stop, its junction, along the road, the target's junction, then the target stop.

**Other names:** `PAD_n` holds each tank pad's gravel and containment berm. `SITE_*` is the ground, roads, and pipe supports. `CAM_OVERVIEW` is a camera framing the whole field. None of these are clickable assets.

**Scale and axes:** The field runs about 450 m along X. In three.js, Y is up and the lease road runs along X near Z = 0, with the tank pads toward negative Z.

## Status colors

The backend sends the status. The web console picks the color.

| Status | Meaning | Look |
|---|---|---|
| `OK` | Normal | Gray, no color |
| `WARNING` | Needs attention soon | Amber |
| `ALARM` | Failed or at risk now | Red |
| `OFFLINE` | Stopped or shut in | Dark |

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
