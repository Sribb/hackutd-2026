# Demo kit: sample blueprints (#39)

Three ways a customer hands us their field. Each one goes through `POST /api/ingest`, then review, then the generator. Format: [docs/BLUEPRINT.md](../../docs/BLUEPRINT.md).

| # | File | What it shows | After ingest |
|---|---|---|---|
| 1 | `sample1_pecos_8tank.json` | A complete blueprint for a different, bigger lease: 8 tanks, 24 wells, 3 disposal wells, 3 trucks, and a simulated SCADA connector | 0 review items. Pump 6 trips at 1:00 AM, and Tank 6 shuts down at **2:03 AM** (±11 min). The cheapest plan that survives +15% inflow is **$287** (5-to-6 crossover + Truck 2). Choking a well instead costs $746 and fails at +15%. |
| 2 | `sample2_delaware_4tank.csv` | A spreadsheet equipment list, the way most operators keep it | 24 items parsed with no Claude call. 14 review items: shutdown level on 4 tanks, lost $/hr on 8 wells, crossover manual, and the truck's `minutes_to`. The upload screen's *Fill all* clears them with a few entries. |
| 3 | `sample3_handdrawn.jpg` (to add) | A photo of a site plan drawn by hand on paper | Claude reads it. Expect review items for anything not written on the paper. |

`tools/check` validates every `.json` here against the schema.

## Drawing sample 3 (about 5 minutes)

Use a plain white sheet and a thick black marker, and take the photo straight down in good light. Write labels in capitals, beside each symbol.

- **4 tanks** as circles in a row across the middle: `TANK 1` to `TANK 4`, each with its size, for example `500 BBL`. Write `SHUTDOWN 90%` once in a corner.
- **A pump** as a small square under each tank: `P-1` to `P-4`, with `150 BBL/HR`.
- **1 disposal well** at the bottom: a triangle labeled `SWD 1  450 BBL/HR`. Draw a line from each pump to it, with a small bowtie on each line for its discharge valve.
- **1 crossover** between Tank 2 and Tank 3: a line with a bowtie, labeled `XO 2-3  MANUAL  150 BBL/HR`.
- **8 wells** as small crosses above the tanks, two per tank: `WELL 1` to `WELL 8`, each with `20 BBL/HR` and an arrow to its tank.
- A **north arrow** and a **scale bar** (`100 m`) in a corner, so positions come out in meters.
- Leave the wells' lost $/hr off on purpose. It shows the review step catching a missing number instead of guessing.

Save it as `scenario/samples/sample3_handdrawn.jpg`, under 5 MB.
