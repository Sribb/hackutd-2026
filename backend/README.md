# Backend

Owner: `@mounishmavuduru`. The API is defined in [docs/CONTRACT.md](../docs/CONTRACT.md).

## Run the API

From the repo root:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements.txt   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

| Endpoint | What it does |
|---|---|
| `GET /api/field` | Field definition, plus `lead_min`, `costs`, and `yards` when the scenario has them |
| `GET /api/state` | Current readings and the incident. A tank with no reading is WARNING, never OK |
| `POST /api/forecast` | Minute-by-minute replay of the action list. About 4 ms per run, 3 runs per call for the ±15% range |
| `POST /api/plans` | Cheapest plans first (default 3), always at least one. About 0.2 s |
| `POST /api/plans/{id}/approve` | Numbered steps (longest lead first) and a radio script. Pushes the steps to SpacetimeDB when it's on |
| `POST /api/lastcall` | Mounted automatically when `lastcall/` is in the checkout. Uses this forecast. About 3 s |
| `GET /api/health` | Extra: SpacetimeDB sync and Last Call status |
| `POST /api/spacetime/sync` | Extra: re-push the state into SpacetimeDB |

Tests: `.venv/Scripts/python -m pytest -q backend/tests`. They're pinned to `backend/data/scenario.json` (see `backend/tests/conftest.py`).

### Which scenario

`FREEBOARD_SCENARIO` if set, else `scenario/field.json` (Vinny's) once it's on main, else `backend/data/scenario.json`. Both file shapes load: `backend/app/scenario.py` converts the `scenario/` shape on load and never edits it.

### The forecast model

`backend/app/forecast.py`, one-minute steps from the current level:

- **Wells** feed their tank at `water_bbl_hr` times the fraction kept by `CHOKE_WELL` (`1` untouched, `0` shut in).
- **Pumps** hold their tank near `setpoint_pct`, up to `rate_bbl_hr`. A tripped pump can't be turned on. A closed discharge valve stops its pump.
- **Disposal wells** cap the pumps that feed them at `max_bbl_hr`. Going over slows every pump on that well and adds a `SWD_AT_CAPACITY` event.
- **Crossovers** move water from the fuller tank to the emptier one, up to `max_bbl_hr` (and `bbl_hr_per_pct` × level gap when given).
- **Trucks** leave at `at_min`, drive `minutes_to`, load `capacity_bbl` over `load_min`, and haul for `haul_to_swd_min`. A busy truck starts late, with a warning.
- **Shutdown**: at `shutdown_pct` every well on that tank goes offline. That adds a `SHUTDOWN_LEVEL` event with `wells_lost`, `t_min_low` (inflow +15%), and `t_min_high` (inflow -15%; `null` means later than the horizon).
- **Cost**: trucking (whole trip at `usd_hr`), disposal of trucked barrels, lost production of choked and shut-in wells, plus `well_restart_usd` for each well a shutdown takes down.
- **Bad actions** are dropped with a message in `warnings`, never a crash. Missing tank readings give `null` levels and a warning, never "safe".

### The planner

`backend/app/planner.py` looks at each tank that reaches shutdown, or would at +15% inflow. It tries the crossover (closed or open), zero to two trucks, and choking or shutting in the wells that lose the least money per barrel first. Each action starts after its `lead_min`. A plan has to keep every tank below shutdown. Plans that only add steps to a cheaper plan are dropped. `robust` says whether the plan still holds at +15% inflow. `unknown_tanks` lists tanks with no reading.

## SpacetimeDB (live shared state)

The REST API is still the source of every number. SpacetimeDB holds the live state so every open console sees the same thing at once, without polling:

| Table | What it holds |
|---|---|
| `asset_state` | Status, level, valve position, and reason per asset ID. `level_pct = -1` means not a tank, or no reading. |
| `field_clock` | One row: the clock and the active incident |
| `operator_action` | What-if actions operators are trying, with who sent them |
| `plan_step` | Steps of the approved plan, and which are done |

Reducers: `upsert_asset`, `set_clock`, `propose_action`, `remove_action`, `clear_actions`, `set_plan`, `mark_step_done`. They reject IDs, statuses, and action types outside the contract. The module is in `backend/spacetime/src/index.ts`.

### Run it

1. Install the CLI once (Windows PowerShell): `iwr https://windows.spacetimedb.com -useb | iex`. macOS/Linux: `curl -sSf https://install.spacetimedb.com | sh`.
2. Start a local server: `spacetime start` (listens on port 3000).
3. Publish the module:
   ```bash
   cd backend/spacetime && npm install
   spacetime publish --server local freeboard
   ```
4. Start the API with sync on. It pushes the full state at startup:
   ```bash
   SPACETIME_DB=freeboard .venv/Scripts/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
   ```
5. Check it: `spacetime sql --server local freeboard "SELECT * FROM asset_state"`

If SpacetimeDB is down, the API keeps working and `/api/health` shows the error.

### For the web console

Generate typed bindings and subscribe instead of polling `/api/state`:

```bash
spacetime generate --lang typescript --out-dir web/src/module_bindings --module-path backend/spacetime
```

Then subscribe to `SELECT * FROM asset_state`, `field_clock`, `operator_action`, and `plan_step`. Call `propose_action` when an operator clicks a fix, and `mark_step_done` as they walk the plan.

## Live layer: connectors, alerts, Jarvis (#38, `@vinnycodes67`)

Additive: the endpoints above are unchanged. Shapes are in [docs/CONTRACT.md](../docs/CONTRACT.md#live-layer-38).

| File | What it does |
|---|---|
| `app/connectors/` | One interface: `read()` returns `{asset_id, signal, value, t}` readings, `send(command)` returns `{ok, detail}`. `sim` (the default, a live digital twin), `webhook` (readings pushed in, commands POSTed out), `mqtt` (paho-mqtt) and `modbus` (pymodbus TCP) real drivers (#62), and a stub for `opcua` that reports "unavailable" |
| `app/detect.py` | Pump trip (stopped, no flow, or tank rising faster than its wells explain), stale sensor (UNKNOWN, never OK), valve mismatch. Confirmed over 2 readings |
| `app/live.py` | The live loop, readings, statuses, and proposals that only a confirm sends |
| `app/jarvis.py` | Rules fast path (no key needed) plus Claude tool use: focus camera, explain asset, forecast, plans, propose action |
| `app/hub.py`, `app/notify.py` | `/ws` broadcast and optional ntfy phone push |
| `app/live_api.py` | The routes, mounted by one guarded block in `main.py` |

The loop starts with the app (`uvicorn ...`). Try it: `curl localhost:8000/api/live/state`, then
`curl -X POST localhost:8000/api/jarvis -H 'content-type: application/json' -d '{"message":"what is failing?"}'`.
Set `ANTHROPIC_API_KEY` for open questions and screenshots. Env vars are listed in CONTRACT.md.

### Real protocols on one laptop: MQTT + Modbus (#62)

`app/connectors/mqtt.py` and `app/connectors/modbus.py` talk to real brokers and PLCs. `backend/tools/` has the field side, so the whole protocol path runs with no hardware:

| Tool | What it is |
|---|---|
| `tools/fake_field.py` | **Use this for the demo.** One simulated field behind both protocols: an MQTT broker (SCADA) and a Modbus TCP server (PLC) |
| `tools/fake_scada_mqtt.py` | Just the MQTT side: an embedded broker (`tools/mqtt_broker.py`, standard library) plus a publisher. `--broker host:port` publishes to mosquitto instead |
| `tools/fake_plc_modbus.py` | Just the PLC: a pymodbus server whose registers follow the simulator |

The fakes read the same blueprint as the backend, so topics and registers always match its `connectors` block, and they run the backend's own simulator (its scripted `faults` fire on the fake's clock). Type `trip PUMP_5`, `stale TANK_6`, `leak TANK_7`, `stuck VALVE_D_5`, or `repair PUMP_5` and Enter in a fake's terminal to break the field.

```bash
# terminal 1: the field (MQTT on 127.0.0.1:1883, Modbus on 127.0.0.1:5020)
.venv/bin/python -m backend.tools.fake_field
# terminal 2: the backend on the matching blueprint
FREEBOARD_SCENARIO=scenario/samples/sample1_pecos_mqtt_modbus.json .venv/bin/python -m uvicorn backend.app.main:app --port 8000
# terminal 3
curl -s localhost:8000/api/live/state | python -m json.tool | grep -A3 CONN_   # CONN_SCADA and CONN_PLC "connected"
```

Pump 6 trips 2 minutes in on the PLC and raises `PUMP_TRIP`. Propose and confirm `SET_VALVE VALVE_XO_56 OPEN`, and the fake PLC prints the holding-register write (`hr:10 = 1`). Stop the fake field and both connectors go `unavailable` and their assets `UNKNOWN` at once.

- **MQTT**: readings are plain values (`72.4`, `1`, `OPEN`) or JSON `{"value": ...}` on `topic_prefix + address`. Commands are published at QoS 1 as `OPEN`/`CLOSED`, `ON`/`OFF`, or the choke fraction.
- **Modbus**: addresses are `hr:N`, `ir:N`, `coil:N`, `di:N` (0-based; a bare number is a holding register). A register reads `raw * scale`; 65535 means bad quality (no reading). Commands write 1/0 for valves and pumps, and the percent kept (0 to 100) for `CHOKE_WELL`.
- Missing library, unreachable endpoint, or unset `*_env` variable: the connector says why in `/api/live/state`, never crashes, and its assets are `UNKNOWN`. Commands to it fail cleanly (`FAILED`).
- Commands reach a broker or PLC **only after a person confirms**. `JARVIS_CONFIRM=0` never applies to them.

Tests: `backend/tests/test_connectors_real.py` starts the fake broker and PLC in-process on free ports.
