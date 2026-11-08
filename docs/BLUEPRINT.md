# Blueprint format v1

One file describes a customer's field. The 3D generator builds the model from it, the backend forecasts and plans from it, and the console finds every asset by its ID. Issue #37.

- Schema: [blueprint.schema.json](blueprint.schema.json) (JSON Schema 2020-12)
- Check a file: `python3 scenario/validate_blueprint.py my_field.json` (needs `pip install jsonschema`)
- Example: [scenario/field.json](../scenario/field.json) is a complete v1 blueprint with no positions.

A blueprint is `scenario/field.json` plus four optional additions: `x` and `y` per asset, `connections`, `site`, and `connectors`. Nothing already in `field.json` changed, so every existing file is still valid.

## The life of a blueprint

| Stage | Who | What happens |
|---|---|---|
| 1. Upload | the console (#43) | A person drops a site plan (image, PDF, CSV, or JSON). |
| 2. Draft | `ingest/` (#28) | Claude reads it into a **draft** blueprint. Numbers that aren't on the plan stay missing and go on the `review` list. Nothing is guessed silently. |
| 3. Review | a person, in the console | They fill every review item. `validate_blueprint.py --draft` lists the same gaps. |
| 4. Complete | | The file passes `validate_blueprint.py`. Only a complete blueprint gets forecast. |
| 5. Build | `blender/build_field.py` (#33) | It builds `field.glb`. The generator accepts drafts too, since it only needs IDs and links. |
| 6. Run | `backend/` (#41) | The forecast, plans, and Last Call load it. |

**Draft vs complete:** the schema describes a *complete* blueprint. A draft is expected to fail it, and its errors are the to-do list. The backend refuses to forecast a tank whose numbers are missing, and it never calls an unknown tank safe.

## Units and coordinates

- Volumes are in barrels (`bbl`), rates in `bbl_hr`, times in minutes, money in USD, and levels in percent (0 to 100).
- `clock_start` is `HH:MM`, 24-hour, local site time.
- `x` and `y` are **meters on the plan**. `x` runs east along the lease road, `y` runs north, and the road is at `y = 0`. Give both or neither, each within ±5000 m. In three.js, the point (x, y) lands at `(x, height, -y)`.
- An asset with no `x` and `y` is laid out automatically. Valves ignore positions because they sit on the pipe they control.
- Keys that start with `_` (`_note`, `_source`, `_ingest`) are comments or metadata. Every reader ignores them.

## IDs

IDs follow [CONTRACT.md](CONTRACT.md): capital letters, digits, and underscores, unique across the whole file. The prefix gives the type:

`TANK_` · `PUMP_` · `VALVE_D_` (discharge) · `VALVE_XO_` (crossover) · `WELL_` · `SWD_` · `TRUCK_` · `YARD_` · `CONN_` (connector) · equipment: `SEP_` `HT_` `FLARE_` `COMP_` `LACT_` `METER_` `EQ_`

The generator names 3D nodes after these IDs, and derived names (`<tank>_LEVEL`, `STOP_<tank>`, and so on) follow CONTRACT.md. The console never invents an ID. It takes the list from `/api/field`.

## Assets

**Bold** fields are required in a complete blueprint. Any asset may also have `label` (the name printed on the plan), `x`, and `y`.

| List | Fields | Notes |
|---|---|---|
| `tanks` (1 to 20) | **`id`**, **`capacity_bbl`** > 0, **`shutdown_pct`** (1 to 100), `warning_pct`, `pump_low_pct`, `setpoint_pct` | The level at which the tank shuts its wells in. |
| `pumps` (0 to 2 per tank) | **`id`**, **`tank_id`**, **`rate_bbl_hr`** ≥ 0, `valve_id`, `swd_id` | Empties its tank toward a disposal well. |
| `valves`, `kind: "DISCHARGE"` | **`id`**, **`kind`**, **`connects: [PUMP, SWD]`**, `pump_id`, `default` (OPEN/CLOSED), `remote` | Both ends are required. The backend can't route a pump with no disposal well. |
| `valves`, `kind: "CROSSOVER"` (up to 4 per tank) | **`id`**, **`kind`**, **`connects: [TANK, TANK]`**, **`max_bbl_hr`** > 0, **`manual`** (true/false), `bbl_hr_per_pct`, `default` | `manual: true` means the night rover has to drive out (`lead_min.SET_VALVE_manual`). |
| `wells` (0 to 8 per tank) | **`id`**, **`tank_id`**, **`water_bbl_hr`** ≥ 0, **`lost_usd_hr`** ≥ 0 | What a shut-in costs per hour. |
| `disposal_wells` (1 to 4) | **`id`**, **`max_bbl_hr`** > 0 | |
| `trucks` (0 to 6) | **`id`**, **`capacity_bbl`** > 0, **`minutes_to`** `{TANK: minutes}` (at least one), `home_id`, `load_minutes`, `minutes_to_disposal`, `unload_minutes`, `usd_hr` | |
| `yards` (0 or 1) | **`id`** | Where trucks park. |

## Scenario fields (unchanged from field.json)

| Key | Meaning |
|---|---|
| **`name`** | The field's display name. |
| `clock_start` | The demo clock when the file loads. |
| `initial.tank_pct` | `{TANK: level %}`. Leave a tank out, or set it to `null`, if its level is unknown. |
| `faults` | `[{asset_id, type: TRIP / STUCK_VALVE / SENSOR_STALE / LEAK, at_min, reason}]`. Scripted failures for the demo. |
| `lead_min` | Minutes from a decision until an action works: `SET_VALVE_manual`, `SET_VALVE_remote`, `CHOKE_WELL`, `SEND_TRUCK`, `SET_PUMP`. |
| `costs` | `disposal_usd_bbl`, `truck_usd_hr`, `well_restart_usd`. |
| `sensors` | Simulator noise: `level_noise_pct`, `flow_noise_bbl_hr`, `stale_after_min`. |

## New in v1

**`site`** (optional): `{name, operator, lat, lon, width_m, timezone}`. Context for Jarvis and the pitch. `width_m` tells ingest the drawing's scale. Nothing else reads it yet.

**`connections`** (optional): pipes or links as `["FROM", "TO"]` or `{"from", "to", "kind": WATER/OIL/GAS/POWER/SIGNAL}`. The generator checks that both ends exist but doesn't draw them, because piping follows `tank_id`, pumps, and valves. Use it for anything the plan shows that those fields don't.

**`equipment`** (optional, used by #47): other equipment the generator draws, like separators, heater treaters, flares, compressors, LACT units, and meter runs. It's display and context only, so the forecast ignores it. `{id, type, label?, tank_id?, connects?, x?, y?}`. `type` is `SEPARATOR`, `HEATER_TREATER`, `FLARE`, `COMPRESSOR`, `LACT`, `METER_RUN`, or `OTHER`. IDs start `SEP_`, `HT_`, `FLARE_`, `COMP_`, `LACT_`, `METER_`, or `EQ_`. A type with no model yet builds as a labeled placeholder, so a blueprint never fails over a missing model.

**`connectors`** (optional, used by #38 and #62): where live signals come from and where confirmed commands go. **With no connectors, the backend simulates every asset**, so the demo never depends on real hardware. With some, each declared signal is read from its connector and the simulator keeps the rest.

MQTT and Modbus TCP are real drivers (paho-mqtt and pymodbus). `backend/tools/fake_field.py` serves both on one laptop, and `scenario/samples/sample1_pecos_mqtt_modbus.json` is a full example. An MQTT SCADA:

```json
"connectors": [
  {
    "id": "CONN_SCADA",
    "kind": "mqtt",
    "config": { "host": "10.0.0.5", "port": 1883, "topic_prefix": "avon/", "username": "night",
                "password_env": "FREEBOARD_CONN_SCADA_PASSWORD" },
    "signals": [
      { "asset_id": "TANK_3", "signal": "level_pct", "address": "tank3/level" },
      { "asset_id": "PUMP_3", "signal": "running", "address": "pump3/run" }
    ],
    "commands": [
      { "asset_id": "VALVE_XO_34", "command": "SET_VALVE", "address": "xo34/cmd" }
    ]
  }
]
```

A Modbus TCP PLC. A register holds an unsigned 16-bit integer, and the reading is `raw * scale`, so `"scale": 0.1` turns a register of `723` into 72.3%:

```json
{
  "id": "CONN_PLC",
  "kind": "modbus",
  "config": { "host": "10.0.0.6", "port": 502, "unit_id": 1, "poll_s": 1 },
  "signals": [
    { "asset_id": "TANK_5", "signal": "level_pct", "address": "ir:0", "scale": 0.1 },
    { "asset_id": "PUMP_5", "signal": "running", "address": "di:0" },
    { "asset_id": "PUMP_5", "signal": "flow_bbl_hr", "address": "ir:10", "scale": 0.1 },
    { "asset_id": "VALVE_XO_56", "signal": "position", "address": "coil:0" }
  ],
  "commands": [
    { "asset_id": "VALVE_XO_56", "command": "SET_VALVE", "address": "hr:10" },
    { "asset_id": "PUMP_5", "command": "SET_PUMP", "address": "hr:20" },
    { "asset_id": "WELL_1", "command": "CHOKE_WELL", "address": "hr:100" }
  ]
}
```

- `kind` is one of `sim`, `mqtt`, `modbus`, `opcua`, or `webhook`. `mqtt` and `modbus` are real drivers. `webhook` takes readings over HTTP and POSTs commands. `opcua` is a stub that reports `unavailable`.
- `signal` is one of `level_pct`, `flow_bbl_hr`, `running`, `position`, `pressure_psi`, or `motor_amps`. `scale` (optional, default 1) multiplies a raw Modbus register; MQTT and webhook values are used as sent.
- `address` for **mqtt** is a topic after `topic_prefix` (default `ASSET_ID/signal`, or `ASSET_ID/set` for a command). It must be one exact topic: `+` and `#` wildcards are rejected when the blueprint loads. For **modbus** it is `hr:N`, `ir:N`, `coil:N`, or `di:N`, 0-based (a bare `N` is a holding register). Commands need `hr` or `coil`. `SET_VALVE` writes 1 for OPEN and 0 for CLOSED, `SET_PUMP` writes 1 for ON and 0 for OFF, and `CHOKE_WELL` writes the percent kept, from 0 to 100.
- `command` is one of `SET_VALVE`, `SET_PUMP`, or `CHOKE_WELL`. A command runs **only after a person confirms it in the console**. An asset with any signal or command on a real connector is never handed to the simulator, so declare every command you want for it. Anything else is refused ("no command path").
- **Never put a password or token in a blueprint.** Name an environment variable (`password_env`, `token_env`) instead. The name must start with `FREEBOARD_CONN_`. Any other name makes the connector `unavailable`, so a blueprint can't send an unrelated secret (like `ANTHROPIC_API_KEY`) to its host. The schema rejects config keys named `password`, `token`, `secret`, `api_key`, or `key`.
- Full config keys and statuses are in [CONTRACT.md](CONTRACT.md#connector-protocols-62).

## What each reader needs

| | Generator (#33) | Backend (#36, #41) | Ingest (#28) writes |
|---|---|---|---|
| IDs and links | required | required | always |
| Forecast numbers | ignored | required (`backend/app/validate.py`) | only if printed on the plan, otherwise a review item |
| `x`, `y` | used if present | ignored | from the drawing, scaled by `site.width_m` (default 450 m) |
| `connectors` | ignored | #38 | never |

## Known gaps between the readers (found while writing this)

1. **Truck `minutes_to`** (fixed by #65): the backend requires it, but ingest didn't put it on the review list (`REQUIRED_NUMBERS` only has `capacity_bbl` for trucks). A plan with trucks passes review, then fails on load. Fix in `ingest/schema.py`.
2. **Discharge valve ends:** the backend needs `connects: [PUMP, SWD]`. The generator accepts the pump alone, and ingest writes the pump alone when the plan doesn't show the disposal well. Ingest already flags it for review. The schema makes it required.
3. **Crossover `manual`:** the forecast's lead time depends on it, and ingest asks about it in a review note. The schema makes it required, so the review can't skip it.
