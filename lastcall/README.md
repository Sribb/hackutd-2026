# Last Call

**Every console counts down to the failure. Last Call counts down to your options.**

The forecast says Tank 3 shuts down at 2:50 AM. That is the wrong clock for a night-shift operator. The real question is how long they can think before each fix stops working.

Every fix has a lead time:

- The crossover valve is manual. The rover needs 20 minutes to drive out and open it.
- A truck needs to be called, drive out, and load.
- Choking a well is remote and almost instant, but it burns production.

So the cheap, slow fixes die first, and the operator is left with fast, expensive ones. At 1:47 AM the cheapest plan costs $1,408. At 2:21 AM it costs $1,962. After 2:45 AM nothing keeps every well online and the bill is $8,834. **The operator never sees those cliffs on a normal dashboard, because each one happens 20 to 30 minutes before anything looks wrong.**

Last Call finds every cliff by replaying the forecast with each plan started later and later.

## What it shows

| Output | What the operator reads |
|---|---|
| Headline | "Your cheapest plan expires at 2:20 AM. Wait past it and the bill goes from $1,408 to $1,962." |
| The ladder | Each plan that is the cheapest for some stretch of time, with a burning fuse to its expiry. Drag the decision slider and fuses burn down and rungs go dark. |
| Price of waiting | A step chart of the cheapest working plan's cost, by the minute the operator commits. Each step is an option that just expired. |
| Point of no return | The last minute any plan keeps every well online. |
| Fragility | Whether a plan still works if inflow runs 15% hot. The cheapest plan tonight does not, and the console says so. |
| Do-by times | Each step gets a deadline, longest lead first: "Send the rover to open the crossover by 2:20 AM." |
| Asset windows | For each valve, truck, and well: when it's part of the cheapest plan, and the last minute it can help at all. |

All numbers come from the forecast, never from a chatbot.

## In the 3D field

This follows the README rule: if the operator can't act on it, it doesn't go in the scene. Every Last Call element is something to act on.

- **A fuse ring around each actionable asset** (`VALVE_XO_34`, `TRUCK_1`, `WELL_9`...). It shrinks as the clock runs, using `assets[id].useful_until_min`. When it closes, the asset goes dark: it can no longer help tonight.
- **The time slider gets a second meaning.** Dragging it already plays the future. With Last Call, it also shows the operator which buttons disappear while they wait.
- **Approved plan steps carry do-by times** from `steps[].do_by_clock`, and the radio script reads them out.

## The demo moment

1. Pump 3 trips. The countdown says 63 minutes to shutdown. The judge relaxes.
2. The hero number says **33 minutes**. That's how long the cheapest plan lives. The judge sees that 63 was the wrong number.
3. Hand the judge the slider. As they drag it, rungs go dark and the price climbs in steps. At 2:45 AM the screen goes red: the point of no return.
4. Pitch line: *"The forecast tells you when the tank fills. Last Call tells you when your options run out, and what each minute of hesitation costs."*

## Why it matters past tonight

Control rooms already know response time matters. Alarm management practice (ISA-18.2) has teams write down, for each alarm, how long an operator has to respond. That number is written once and filed in a binder. Last Call computes it live, for each fix, from the current tank levels. **Check the exact ISA-18.2 wording with a mentor before saying it on stage.**

The same idea works anywhere fixes have lead times and the cheap ones are slow: produced-water hauling, pipeline pigging, grid dispatch, and hospital bed transfers. The engine only needs a forecast and a list of actions with lead times.

## How it works

1. Run the forecast with no actions. Find the tanks that reach shutdown and when.
2. Build candidate plans from the levers around those tanks: each crossover, zero to two trucks (fastest first), and chokes on the tank's wells, cheapest dollars per barrel first. Tonight that's 54 plans.
3. For each plan, binary-search the latest decision minute where it still keeps every tank below shutdown. Do it again at +15% inflow.
4. Re-price the surviving plans along the timeline, and record where the cheapest one changes. Those are the cliffs.

It runs in about 0.7 seconds in plain Python. It doesn't need to run on every click, only when an incident opens or a plan is approved.

A plan only counts if **every** tank stays safe. A crossover that saves Tank 3 by drowning Tank 4 is rejected. There's a test for it.

## Files

```text
lastcall/
  core.py           the engine: candidate plans, expiry search, cliffs, ladder
  sim.py            reference forecast in the CONTRACT.md shape, so this runs before the backend lands
  router.py         FastAPI route: POST /api/lastcall
  scenario.json     placeholder field with contract IDs, lead times, and costs
  test_lastcall.py  6 tests
  demo/index.html   standalone judge-facing page (loads demo/data.js)
```

## Run it

```bash
python3 -m lastcall                              # print the /api/lastcall JSON
python3 -m lastcall --demo lastcall/demo/data.js # rebuild the demo data
python3 -m lastcall.test_lastcall                # tests, no pytest needed
cd lastcall/demo && python3 -m http.server 8765 --bind 127.0.0.1
# open http://127.0.0.1:8765
```

No dependencies besides FastAPI for `router.py`. Works on Python 3.9 and up.

## Plug into the backend

```python
from lastcall.router import router as lastcall_router, set_source
app.include_router(lastcall_router)

# When the backend has its own scenario and forecast:
set_source(get_scenario=load_live_scenario,
           forecast_fn=lambda actions, inflow_scale: my_forecast(actions, inflow_scale))
```

Any forecast that returns the contract's `events` and `cost` works. Last Call only needs two extra things from the field data: `lead_min` per action type and `manual` on valves. Both are in [docs/CONTRACT.md](../docs/CONTRACT.md).

## Placeholders to replace

All costs and lead times in `scenario.json` are placeholders. The ones that matter most for Last Call:

- How long the rover takes to reach each pad at night. This sets when the crossover expires.
- Truck call-out time and hourly rate.
- Lost revenue per well-hour, and the cost to restart a shut-in well.
