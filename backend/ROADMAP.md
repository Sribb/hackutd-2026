# What's left, end to end

Written by the backend owner (`@mounishmavuduru`) on 2026-11-07. Rows owned by other people are suggestions for them to accept or change on the board. Nobody edits another lane's files without a handoff.

Every row ends with a check, so "done" can be verified.

## Status today

| Piece | State |
|---|---|
| `/api/field`, `/api/state` | Built. PR #14 |
| `/api/forecast`, `/api/plans`, `/api/plans/{id}/approve` | Built. PR for #19, stacked on #14 |
| Last Call mount | Coded. It turns on by itself once `lastcall/` (#10) is on main. Tested against #10's code in a scratch worktree: 200 OK, same cheapest plan as `/api/plans` |
| SpacetimeDB module | Written and typechecked. **Never published**, because the CLI install is blocked on Mounish's machine |
| Web console | On main (#13). What-if, plans, and Last Call are in #16 |
| 3D model | On main (#18) |
| `scenario/field.json` | PR #20. The backend reads it automatically once it's on main |

## Backend (`@mounishmavuduru`)

| # | Step | Depends on | Check |
|---|---|---|---|
| B1 | Merge #14, then the #19 PR (retarget it to `main` after #14 merges) | A reviewer, or Mounish merging it himself | `tools/check` PASS on main |
| B2 | Install the SpacetimeDB CLI, `spacetime start`, publish `backend/spacetime` | Mounish runs the installer | `spacetime sql --server local freeboard "SELECT * FROM asset_state"` returns 48 rows after the API starts with `SPACETIME_DB=freeboard` |
| B3 | After #20 merges: check the backend picks up `scenario/field.json` | #20 | `/api/state` clock is `01:47`, and `test_real_team_scenario_runs` passes, not skipped |
| B4 | After #10 merges: confirm `/api/health` says `lastcall: mounted` | #10 | `POST /api/lastcall` returns 200 and the same shutdown time as `/api/forecast` |
| B5 | Live clock: `/api/state` advances with the demo clock (a `?t_min=` parameter or a server clock), and `/api/forecast` starts from that moment | Decide with Ronak whether the console or the server owns the clock | Slider at 30 min plus approving a plan gives levels that match the forecast at t=30 |
| B6 | Applied plans: approving a plan records its actions, so `/api/state` shows the crossover OPEN and later forecasts include those actions | B5 | Approve, then `GET /api/state` shows `VALVE_XO_34` OPEN, and the forecast with no actions no longer shows a Tank 3 shutdown |
| B7 | Failure detection (BACKEND.md step 7): flag a pump when its tank rises faster than its wells explain, or flow stops. Only needed if readings arrive as a time series (Vinny's draft #21 simulates sensors) | Readings source | Unit test: a series with the pump stopped raises ALARM within 3 readings |
| B8 | Push live state to SpacetimeDB on every change (approve, clock tick), not only at startup | B2, B5 | A console subscribed to `asset_state` sees the change in under 1 s |
| B9 | Speed: Last Call takes about 3 s because it makes about 800 forecast calls. If that's too slow on stage, add numpy or a 2-minute step | Only if needed | `POST /api/lastcall` under 1.5 s |
| B10 | Real numbers: replace placeholder costs, rates, and lead times once the mentors answer (Vinny's lane owns the values) | Mentors | Values in `scenario/field.json` have a source note |

## Web console (`@ronakkasaram`), suggested

| # | Step | Check |
|---|---|---|
| W1 | Merge #16. Run it against the live backend: `cd web && VITE_API=http://127.0.0.1:8000 npm run dev` | The badge shows forecast, plans, and approve as live, not fake |
| W2 | Show `warnings` from every forecast, plus the `robust` and `unknown_tanks` plan fields | A tank with no reading shows a warning chip, never a green tank |
| W3 | Optional: subscribe to SpacetimeDB instead of polling `/api/state`, using `spacetime generate --lang typescript --out-dir web/src/module_bindings --module-path backend/spacetime` | Two browser tabs show the same approved plan steps live |
| W4 | Approve walk-through calls `mark_step_done` (SpacetimeDB) or keeps it local if SpacetimeDB is off | Steps tick off in both tabs |
| W5 | 60-second test on a stranger (README) | From failure to approved plan in under 60 s, without touching the camera |

## Database (SpacetimeDB, backend lane)

| # | Step | Check |
|---|---|---|
| D1 | Publish locally (B2) | `spacetime logs --server local freeboard` shows no errors after the API starts |
| D2 | Decide who may call the write reducers. Today anyone connected can. For the expo, either keep it local-only or check `ctx.sender` against the backend's identity | A browser call to `upsert_asset` is rejected if locked down |
| D3 | Optional: publish to maincloud so phones and other laptops can join | `spacetime publish --server maincloud freeboard` works, and the console connects over wss |

## Scenario and pitch (`@vinnycodes67`), suggested

| # | Step | Check |
|---|---|---|
| S1 | Merge #20 | `tools/check` scenario PASS |
| S2 | Get answers from the mentors: shutdown level, trucking and disposal $, lost $/well-hr, rover travel time | Placeholders in `scenario/field.json` replaced, with a source note |
| S3 | Pitch numbers come from the live backend, not slides: shutdown time, do-nothing cost, cheapest plan cost, Last Call expiry | Numbers on the slides match `/api/forecast`, `/api/plans`, and `/api/lastcall` on the demo laptop |

## Team rules (`@teameuphoriainv-dot`)

| # | Step | Check |
|---|---|---|
| R1 | Decide #4 (`tools/check`), #8, and #9 (lane fence), in that merge order | Owner table in AGENTS.md lists all four lanes |
| R2 | Decide what to do with Vinny's draft #21. It overlaps `backend/` (forecast, planner, Last Call route) and adds IoT sensors, a `/ws` hub, phone alerts, and a Claude-driven "Jarvis". The README says no chatbot as the main interface | A board decision on which pieces, if any, come into `backend/`, credited to Vinny |

## Demo-day checklist (everyone)

1. Fresh clone on the demo laptop: `pip install -r backend/requirements.txt`, then `cd web && npm install`.
2. Start the backend on 127.0.0.1:8000, start the web console with `VITE_API`, and optionally start SpacetimeDB.
3. Run `tools/check` and get PASS with nothing skipped except optional pieces.
4. Rehearse the 5 demo beats in README. The numbers on screen must match the pitch.
5. No `.env` or keys in the repo. The server binds to 127.0.0.1 unless a teammate needs to reach it.
