# ingest

Turns an uploaded blueprint of an oil field into `blueprint.json`, the same shape as `scenario/field.json`, plus a review list a human confirms before the 3D field is built.

| Upload | How it's read |
|---|---|
| JSON that already has `tanks` | Parsed directly and validated |
| CSV equipment list with `kind` and `id` (or `label`) columns | Parsed directly |
| PNG, JPEG, WebP, GIF, PDF site plan, or any other text | Read by Claude (`claude-opus-5-5`, structured outputs) |

**CSV columns** (any order, case doesn't matter): `kind` and `label` (or `id`) are required. The optional columns are:
- `tank`: the tank a pump or well feeds
- `capacity` and `rate`
- `connects`: labels separated by `;`
- `shutdown_pct` and `lost_usd_hr`
- `manual`: `manual` or `remote`, or yes or no
- `minutes_to`: a truck's drive times, written like `Tank 1:20; Tank 2:25`
- `x` and `y` in meters

A blank cell is never guessed. It goes on the review list instead.

## The rules it follows

- **Nothing is invented.** Claude only returns equipment drawn or written on the plan, and a number only when the plan states it. A missing number goes on the review list, because the backend refuses to forecast a tank without its numbers.
- **Guesses are always flagged.** If a well or pump isn't clearly tied to a tank, ingest links it to the nearest tank and lists that for review.
- **Low confidence is flagged.** Anything Claude is less than 80% sure of goes on the review list.
- **IDs follow the contract.** Examples: `TANK_3`, `PUMP_3`, `VALVE_D_3`, `VALVE_XO_34`, `WELL_9`, `SWD_1`, `TRUCK_1`, `YARD_1`. A label's number is kept when possible, so "Tank 3" becomes `TANK_3`.
- **Positions keep the drawing's shape.** They come back in meters with the site's center at 0,0. The site width defaults to 450 m when the plan has no scale bar.

## Setup

```bash
.venv/bin/pip install -r ingest/requirements.txt
export ANTHROPIC_API_KEY=...        # server only; never commit it or send it to the browser
```

Mount it in the backend:

```python
from ingest.router import router as ingest_router
app.include_router(ingest_router)
```

Or run it from the command line: `.venv/bin/python -m ingest.cli plan.png --out blueprint.json`

## EOG sponsor datasets (#131)

`ingest/eog_network.py` turns any sponsor dataset folder (nihaal419/HackUTD-ZeroDay: `history/*`, the mock test, and the `test/` folder added before judging) into one complete blueprint and one 3D field per system. It reads only `network.json` and `ops.json`, and plain `python3` runs it.

```bash
python3 -m ingest.eog_network <folder> --out <dir> [--system S1] [--glb kit|blender]
```

Each system gets `<dir>/<system>/blueprint.json` and `ids.json`. With `--glb`, it also gets `field.glb` and `build_report.json`. `kit` (the default) is `blender/assemble_glb.py`, about 0.05 s a system once the kit is loaded. `blender` is `build_field.py`, headless with `--factory-startup`, about 9 s and with scenery. From code, `network_to_blueprints(network, ops)` returns `{system_id: blueprint}`, and `field_glb(folder, system, cache_dir)` builds one system's glb on the first call and caches it by the bytes of the two files.

| Sponsor | Blueprint |
|---|---|
| facility `S1_FACILITY_A` | its tag `A` names the pad, `PAD_A` |
| tank | `TANK_A`: `capacity_bbl`, `shutdown_pct` and `warning_pct` from `defaults.tank_limits_pct`, `setpoint_pct` and `pump_low_pct` from the pump's start and stop levels |
| pump | `PUMP_A`: `rate_bbl_hr` = `max_flow_bpd` / 24 |
| pipeline | `VALVE_D_A`, a `DISCHARGE` valve with `connects: [PUMP_A, SWD_1]` |
| well `S1_WELL_A1` | `WELL_A1` on `TANK_A`: `water_bbl_hr` = `baseline_water_bpd` / 24, `lost_usd_hr` = (oil x oil price + gas x gas price) / 24 |
| disposal site | `SWD_1`: `max_bbl_hr` = the sum of its pipelines' `max_bpd` / 24, since the site never goes down |
| truck yard | `YARD_1` |
| `emergency_fleet_size` | `TRUCK_1..n` (up to 6): `capacity_bbl` = `truck_capacity_bbl`, `minutes_to` = each facility's `response_min`, plus `_cycle_min` and `_cost_per_trip` |

Every asset keeps the sponsor ID as `label` and `_source_id`, and the glb carries it as `extras.source_id`. `ids.json` has `assets` (sponsor ID to node name), `nodes` (the reverse), `pipes` (each pipeline's own pipes, then the trunk pieces to the disposal well, ready to highlight a leak), `truck_yard`, `trucks`, and `review`. A gap in the data goes on `review` instead of being guessed, for example `lost_usd_hr` when there is no `ops.json`.

Positions are compressed, because facilities sit 5 to 10 km apart and the yard 30 km out, while a pad is 50 m wide. Facilities, the disposal site and the yard each get their own column along the lease road, in true west to east order. Neighbors are 60 m apart, plus 10 m per real km between them (at most 80 m more). A facility north of the disposal site goes north of the road, otherwise south, set back 6 m per real km (at most 40 m) beyond the usual 46 m. No two columns can overlap, so the generator needs no change. Each pump runs straight to the trunk on the road, and the trunk runs to the disposal well.

## Tests

`.venv/bin/python -m pytest -q ingest/tests` runs offline with a fake Claude client. The EOG tests use `ingest/tests/fixtures/eog/` (the sponsor's `network.json` and `ops.json`, the same in every `history/` folder) and a reshaped S6 to S11 network. Set `EOG_DATA` to a checkout of the sponsor repo to convert every folder in it, `test/` included.
