# Backend

The forecast, planner, live IoT field, Jarvis command interface, and phone alerts. Everything the web console and phones show comes from here, over REST and one WebSocket.

## Run it

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cp backend/env.example .env          # optional: API key, phone push, demo speed

uvicorn backend.app.main:app --port 8000                  # laptop only
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000   # phones on the same Wi-Fi
```

- Phone remote: open `http://<laptop-ip>:8000/phone` on a phone.
- API docs: `http://127.0.0.1:8000/docs`
- Tests: `python -m pytest backend/tests -q`

The night starts at 1:44 AM. Pump 3 trips at 1:47 AM. The clock runs at `FREEBOARD_SPEED` simulated minutes per real second (default 0.25, so an hour takes 4 minutes).

## How it fits together

```text
scenario/field.json ─► Plant (sim.py) ─► sensors (live.py) ─► detector ─► incident + alerts ─► /ws ─► web console
                          ▲                    ▲                    │                              └─► phones (+ ntfy push)
                          │                    └─ POST /api/telemetry (real devices)
                          └── commands, approved plans ◄── Jarvis (jarvis.py) ◄── text / voice from any screen
```

- **One physics model.** The live field and every what-if forecast step the same `Plant`, so the 3D scene, the countdown and the planner never disagree.
- **Detection uses sensor readings only.** Pump 3 is flagged because its flow meter reads 0 while its tank says it should be running, not because the code reads the scenario file.
- **No data never looks safe.** A tank whose sensor drops out or freezes is forecast as if its pump had stopped, and its status is at least `WARNING`.
- **The solver produces every number.** Jarvis, whether Claude or the rules engine, only calls tools and repeats what they return.
- **People confirm changes.** Equipment changes and plan approvals from Jarvis wait until someone taps Confirm on any screen.

| File | What it does |
|---|---|
| [app/sim.py](app/sim.py) | Plant physics, actions, the 6-hour forecast with a ±15% range |
| [app/planner.py](app/planner.py) | Tries valve, truck and choke combinations, ranks them by dollars, writes the steps and radio script |
| [app/live.py](app/live.py) | Live clock, simulated sensors, failure detection, alerts, commands, plan execution |
| [app/jarvis.py](app/jarvis.py) | Command interface: Claude tool use, with an offline rules fallback |
| [app/bus.py](app/bus.py) | WebSocket hub with client roles and presence |
| [app/alerts.py](app/alerts.py) | Alert log, plus phone push through ntfy |
| [app/main.py](app/main.py) | FastAPI routes |
| [app/phone.html](app/phone.html) | Minimal phone remote. The real web app can replace it |

## For the web console: the WebSocket

Connect to `ws://<host>:8000/ws?role=web&name=console`. Phones use `role=phone`, and sensors or microcontrollers use `role=device`. Every message has the form `{type, seq, data, clock, t}`.

**On connect** you get `hello`, which holds everything needed to draw the screen: `field`, `state`, `telemetry`, `forecast`, `alerts`, `plans`, `execution`, `pending`, `commands`, `presence` and `jarvis`. After a `reset`, redraw from the snapshot it carries.

| Server sends | When | What the console does |
|---|---|---|
| `telemetry` | Every tick (about 2 per second) | Tank fill meshes (`tanks[id].level_pct`), pump spin (`pumps[id].running`), valve positions, truck positions |
| `forecast` | Every simulated minute and after any change | Time slider timeline (`timeline[]`), countdown (`countdown.text`), event markers |
| `incident` | A failure is detected | Turn the field gray and color the root asset (`root_asset_id`), show `summary`, `reason` and `evidence` |
| `alert` / `alert_ack` | Alerts are raised or acknowledged | Toast, alert list |
| `ui` | Jarvis, a phone, the detector or a plan wants the screen to do something | See the next table |
| `pending_command` / `pending_cleared` | Something waits for a human to confirm | Show a Confirm card |
| `command` | Equipment changed | Animate the asset, add to the log |
| `plans` / `plan_approved` / `plan_step` | Plan search, approval, each step executing | Plan cards, numbered steps, radio script |
| `jarvis_heard` / `jarvis` | Someone spoke to Jarvis, and Jarvis answered | Caption bar. Speak `reply` with `speechSynthesis` |
| `presence` | A screen connects or disconnects | "2 phones connected" |
| `field_event` | A truck arrives or finishes loading | Ticker |

**`ui` directives.** These are how Jarvis and phones drive the 3D scene. Apply each one on every screen.

| `action` | Fields | Do this |
|---|---|---|
| `focus` | `asset_id`, optional `highlight[]`, `reason` | Fly the camera to the object named `asset_id` and show `reason` |
| `highlight` | `asset_ids[]` | Outline those objects |
| `set_time` | `t_min` | Move the time slider to `t_min` minutes from now |
| `preview` | `actions[]`, `forecast` | Show this what-if on the time slider without changing the field |
| `show_panel` | `panel` (`plans`, `alerts`, `forecast`, `asset`, `radio`) | Open that panel |
| `walkthrough` | `asset_ids[]` | Visit each step's asset in order |
| `clear` | | Return to live, drop highlights |

**Client sends** (each can carry `rid`, and the server answers with `{type: "reply", data: {rid, ...}}`):

```json
{"type": "jarvis", "text": "what's the cheapest fix"}
{"type": "command", "action": {"type": "SET_VALVE", "asset_id": "VALVE_XO_34", "value": "OPEN"}, "confirm": false}
{"type": "confirm", "id": "p12", "approve": true}
{"type": "forecast", "actions": [...]}
{"type": "plans"}
{"type": "approve", "plan_id": "plan_1"}
{"type": "ui", "data": {"action": "focus", "asset_id": "TANK_3"}}
{"type": "ack_alert", "id": "a3"}
```

Clicking a valve in 3D for a what-if: send `forecast` with that action and draw the reply on the time slider. To actually operate the valve: send `command`.

## REST

The [contract](../docs/CONTRACT.md) endpoints, plus:

| Endpoint | Purpose |
|---|---|
| `POST /api/lastcall` | Ronak's Last Call (`lastcall/core.py`) run on the live field and this forecast. Returns 503 until `lastcall/` is on `main`. About 5 s cold, then cached until the field changes |
| `POST /api/commands` | Operate equipment now (`confirm: true` makes it wait for a human) |
| `POST /api/commands/{id}/confirm` | Confirm or cancel (`{"approve": false}`) |
| `POST /api/jarvis` | `{"text": "...", "by": "Vinay", "source": "phone"}` |
| `POST /api/ui` | Drive every screen from anywhere |
| `POST /api/telemetry` | Real device readings (below) |
| `GET /api/alerts`, `POST /api/alerts/{id}/ack` | Alert log |
| `GET /api/snapshot` | Same as `hello` |
| `POST /api/sim` | `{"speed": 1, "paused": false}` |
| `POST /api/sim/advance` | `{"minutes": 10}` jumps ahead |
| `POST /api/demo/reset` | Back to 1:44 AM |
| `POST /api/demo/fault` | `{"asset_id": "TANK_5", "type": "SENSOR_DROPOUT"}`. Types: `TRIP`, `REPAIR`, `SENSOR_DROPOUT`, `SENSOR_STUCK`, `CLEAR_SENSOR` |

## Real devices

Anything that can make an HTTP request becomes a field sensor. Its readings override the simulated sensor for `hold_min` simulated minutes:

```bash
curl -X POST localhost:8000/api/telemetry -H 'content-type: application/json' -d '{
  "source": "esp32-pump1",
  "readings": [{"asset_id": "PUMP_1", "metric": "flow_bbl_hr", "value": 0, "hold_min": 30}]
}'
```

About a minute of simulated time later, the detector flags Pump 1. Metrics: `level_pct` (tanks), `flow_bbl_hr` (pumps), `rate_bbl_hr` (wells, disposal wells).

## Phone push alerts

1. Install the ntfy app and subscribe to a long random topic name.
2. Set `NTFY_TOPIC` to that name and `PUBLIC_URL` to `http://<laptop-ip>:8000` in `.env`.
3. Critical and warning alerts then arrive as real notifications, with buttons that open the phone page or acknowledge the alert.

Push is off until `NTFY_TOPIC` is set, so nothing leaves the laptop by default.

## Demo controls

```bash
curl -X POST localhost:8000/api/demo/reset
curl -X POST localhost:8000/api/sim -d '{"speed": 0.25}' -H 'content-type: application/json'
curl -X POST localhost:8000/api/sim/advance -d '{"minutes": 5}' -H 'content-type: application/json'
```

## Where this came from

We kept the shape of CauldronWatch's server (FastAPI, one WebSocket manager, typed broadcast messages) and rebuilt the parts our tests showed were broken. No CauldronWatch code was copied. [bus.py](app/bus.py) follows the pattern of its `backend/api/websocket.py`.

| Baseline finding (see [BACKEND.md](../docs/BACKEND.md)) | What we did |
|---|---|
| CauldronWatch forecasts from empty, not the current level | `sim.py` steps from the current level. `test_full_tank_and_empty_tank_differ` covers it |
| CauldronWatch reports 0 fill rate as "overflow prevented" | Missing or stale data raises warnings and treats the tank as at risk. `test_sensor_dropout_is_never_safe` |
| SnitchTheWitch's OR-Tools planner crashed, and its deadlines hit 0 | `planner.py` enumerates combinations through the same forecast and always returns plans, listing `cannot_save` |
| Neither planner used dollars | Cost = trucking + disposal + lost production, ranked cheapest first |
| CauldronWatch detects drains, not failures | `live.py` detects pump failures, unexplained rises, and stale or stuck sensors |
| EOG 2021 optimizer: cut water where it loses the least revenue | Planner chokes wells in order of lost $ per barrel of water |
| AquaLink replays the future from timed actions | `forecast()` replays `actions[]` by `at_min` |
