# ingest

Turns an uploaded blueprint of an oil field into `blueprint.json`, the same shape as `scenario/field.json`, plus a review list a human confirms before the 3D field is built.

| Upload | How it's read |
|---|---|
| JSON that already has `tanks` | Parsed directly and validated |
| CSV equipment list with `kind` and `id` (or `label`) columns | Parsed directly |
| PNG, JPEG, WebP, GIF, PDF site plan, or any other text | Read by Claude (`claude-opus-5-5`, structured outputs) |

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

## Tests

`.venv/bin/python -m pytest -q ingest/tests` runs offline with a fake Claude client.
