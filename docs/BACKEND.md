# Backend handoff

**Your goal:** answer three questions for the console. What's failing? How long do we have? What's the cheapest way to keep running?

The API you're building is in [CONTRACT.md](CONTRACT.md). The most important endpoint is `POST /api/forecast`.

## Where to start

Fork the server from [CauldronWatch](https://github.com/anhnguyen0101/CauldronWatch), a HackUTD 2025 project for EOG's challenge. Its "cauldrons" are tanks and its "witches" are trucks. **Its server works, but its forecast has to be rewritten.**

```bash
git clone https://github.com/anhnguyen0101/CauldronWatch.git /tmp/cw
cp -R /tmp/cw/backend ./backend
cp /tmp/cw/requirements.txt ./backend/requirements.txt
find backend -name __pycache__ -type d -prune -exec rm -rf {} +
echo "networkx" >> backend/requirements.txt

python3.11 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
uvicorn backend.api.main:app --port 8000
```

Commit the untouched import first, with a message naming the source. That keeps our own changes clear in the git history.

Check it with last year's dates. The default query looks at the last 7 days, and this data ended in November 2025:

```bash
curl "http://127.0.0.1:8000/api/analysis/cauldrons?start=2025-11-02T00:00:00&end=2025-11-09T23:59:00"
```

## What we tested

We ran these on 2026-11-07 with Python 3.11 and OR-Tools 9.14, against EOG's live 2025 API.

| Test | Result |
|---|---|
| CauldronWatch server start | Works in about 4 seconds after adding `networkx`, which its requirements leave out. `openai` is optional. |
| Tanks, trucks, and latest levels | Work against the EOG API |
| Fill rates and drains with dates set | Real values for all 12 tanks in about 8 seconds |
| Same query with no dates | **Reports 0 fill rate for every tank, then "0 trucks needed, overflow prevented."** The data is simply outside the default window, but having no data must never look safe. |
| Forecast with tanks at 95% and 5% | **Both report 500 minutes.** The real time left is 25 and 475 minutes, because it [calculates from empty](https://github.com/anhnguyen0101/CauldronWatch/blob/HEAD/backend/api/forecast_service.py#L145) and never uses the current level. |
| Same forecast's safety check | **Says overflow is prevented with a truck arriving at minute 30, when that tank overflows at minute 25.** It also plans 1,920 volume units on a truck that holds 100. |
| SnitchTheWitch OR-Tools planner | **Crashed with a `SystemError` after 45 seconds, in both a calm and a crisis test.** We didn't pin down why. Build a new planner rather than debugging this one. |
| SnitchTheWitch crisis deadlines | **Tanks near overflow get a deadline of minute 0**, [from a 60-minute safety margin](https://github.com/Abhijith-utla/SnitchTheWitch/blob/HEAD/optimization_api.py#L270). The solver can't skip a tank, so a plan fails in exactly our scenario. |
| CauldronWatch fill rate accuracy | **Not validated.** Its rates ran about 0.05 L/min above a plain median of rising minutes, because it [ignores rises under 0.1](https://github.com/anhnguyen0101/CauldronWatch/blob/HEAD/backend/data_analysis/rate_calculator.py#L55). On slow tanks that's a big gap in time left. Check against a straight-line fit between drains. |

## Build order

1. **Serve fake data first.** Add `/api/field` and `/api/state` from a scenario file, using the IDs in the contract. The web console can start as soon as this exists.
2. **Rewrite the forecast.** For each step, `level = level + (inflow - outflow) * step`. Start from the current level. Stop the wells feeding a tank when it reaches its shutdown level. Add an event for each thing that happens.
3. **Give a range.** Re-run with inflow about 15% higher and lower to get `t_min_low` and `t_min_high`. Replace 15% if the data supports a better number.
4. **Add actions.** `POST /api/forecast` takes the action list and replays the future with those changes. Keep it fast, because it runs on every click.
5. **Add cost.** See the table below. Neither past planner uses dollars, and the track asks for the lowest cost.
6. **Build the planner.** Try combinations of valves, choked wells, and trucks. Run each one through the forecast, drop the plans that fail, and return the cheapest 3. Use OR-Tools for truck routing only if a simple nearest-truck rule isn't good enough.
7. **Detect failures.** Flag a pump when a tank rises faster than expected or flow stops. CauldronWatch detects drains, not failures, so this part is new.
8. **Approve a plan.** Return numbered steps with asset IDs so the 3D scene can walk through them.

If real sponsor data shows up, keep the same API and swap the data source underneath.

## If you use OR-Tools

We tested this on a tiny case with two tanks and one truck, where it solved instantly. Larger cases are untested.

```python
# A deadline is the real time left, with no extra margin subtracted.
clock.CumulVar(manager.NodeToIndex(node)).SetRange(0, minutes_until_shutdown)

# Let the solver skip a tank at a high cost, so it always returns a plan.
routing.AddDisjunction([manager.NodeToIndex(node)], 1_000_000)
```

| Case | Result |
|---|---|
| Deadline of 0, no skipping | No solution |
| Deadline of 0, skipping allowed | A plan that skips that tank |
| Tank has 45 minutes, truck is 35 away | Reached at minute 35 |
| Tank has 30 minutes, truck is 35 away, skipping allowed | A plan that skips that tank |

Any tank missing from the route was skipped, so report it in `cannot_save`.

**Keep callback values as whole numbers.** Fractional values produced arrival times of 0 in our test.

## Costs

These are placeholders. Ask the sponsor mentors for real numbers.

| Cost | Starting value | Source |
|---|---|---|
| Disposal | $0.60 to $1.50 per barrel, or $2+ on the spot market | [JPT](https://jpt.spe.org/twa/saltwater-disposal-key-permian-productivity), citing 2018 figures |
| Trucking | Ask the mentors | |
| Lost production per well-hour | Ask the mentors | |

## Other ideas worth taking

- **[The 2021 EOG optimizer](https://github.com/Lord-Protector/EOG_HackUTD/blob/HEAD/main.py#L74):** It cuts water where it loses the least revenue. Use the same idea to decide which wells to choke first. Take the idea only, because the code is tied to a 2021 server.
- **[AquaLink's replay](https://github.com/bilal-feroz/AquaLink/blob/HEAD/src/lib/simulation/engine.ts#L914):** It rebuilds any future from a list of timed actions, which is the right shape for what-if. It's TypeScript, so port the idea. Don't take any of its 3D or visuals.

## Don't copy

- **`hackutd-eog-project`:** It hard-codes its tanks and commits `.env` files.
- **SnitchTheWitch's random forest:** [It's given the answer as an input](https://github.com/Abhijith-utla/SnitchTheWitch/blob/HEAD/src/cauldron_flow_model.py#L228), so its accuracy means nothing.
- **SnitchTheWitch's server settings:** It [runs in debug mode, open to the network](https://github.com/Abhijith-utla/SnitchTheWitch/blob/HEAD/optimization_api.py#L792), which is unsafe on hackathon Wi-Fi. Keep our server on `127.0.0.1` unless a teammate needs to reach it.

## Practice data

[EOG's 2025 API](https://hackutd2025.eog.systems/swagger/index.html) has 12 tanks with readings every minute from 2025-10-30 to 2025-11-09, plus trucks, a travel-time network, and drain tickets. Volumes are in liters. Use it to test the fill-rate and forecast logic until we know what this year's data looks like.
