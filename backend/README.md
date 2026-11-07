# Backend

Owner: `@mounishmavuduru`. The API is defined in [docs/CONTRACT.md](../docs/CONTRACT.md).

## Run the API

From the repo root:

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements.txt   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

| Endpoint | Status |
|---|---|
| `GET /api/field` | Done, from `backend/data/scenario.json` |
| `GET /api/state` | Done. Tonight's failure: `PUMP_3` at 1:47 AM |
| `POST /api/forecast` | Next |
| `POST /api/plans`, `POST /api/plans/{id}/approve` | Later |
| `GET /api/health` | Extra: server and SpacetimeDB sync status |
| `POST /api/spacetime/sync` | Extra: re-push the state into SpacetimeDB |

Tests: `.venv/Scripts/python -m pytest -q backend/tests`

Set `FREEBOARD_SCENARIO` to load a different scenario file with the same shape.

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
