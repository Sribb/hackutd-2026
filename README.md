# Gaia

A 3D night-shift console for salt water operations in an oil field. Built for HackUTD 2026.

Its working name was Freeboard, the room left at the top of a tank: the margin an operator is running out of. That name still appears in code, such as `FREEBOARD_TOKEN`. How the gaia AI works: [AGENTS.md section 7](AGENTS.md#7-gaia-the-product-and-its-ai).

## Run it

You need Python 3.11 or newer and Node 18 or newer.

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cd web && npm ci
FREEBOARD_EOG_DIR=/path/to/HackUTD-ZeroDay npm run demo          # on this laptop
FREEBOARD_EOG_DIR=/path/to/HackUTD-ZeroDay npm run demo:tunnel   # plus a public https link
```

`FREEBOARD_EOG_DIR` is a checkout of EOG's sponsor repo. Every folder in it with `network.json`, `ops.json` and `telemetry.csv` shows up, including `test/`. Open the printed link:
- `/start` is the judge flow.
- `/` is the gaia console.
- `/dataset` is any EOG folder.

Text and voice alerts need `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_FROM` in the server's `.env`. Without them, the phone pager stands in.

**Start here, then go to your area:**
- Backend: [docs/BACKEND.md](docs/BACKEND.md)
- Everyone: [docs/CONTRACT.md](docs/CONTRACT.md) has the names and API we all build against
- Your coding agent: [set up the agent board](#the-agent-board), then have it read [AGENTS.md](AGENTS.md)

## The track

> Avon never sleeps - and neither do the oil fields beyond its walls that keep the city running. Every barrel of oil produced also brings salt water that must be managed: collected in tanks, transported through pipelines, and treated for reuse. Most nights, the system runs seamlessly in the background, but tonight... something breaks. A pump fails, a tank nears capacity, and the system forecasts an impending shutdown to prevent a spill.
>
> If no one intervenes, Avon goes dark.
>
> Your job is to change that outcome. Build a console that gives night-shift operators the insight they need to act fast: identify what's failing, predict the time before it worsens, and determine the lowest-cost path to keep operations running.

## The idea

Most teams will build a normal dashboard, and some will add a 3D model to look at. We are building **a 3D field the operator can fast-forward and change before anything goes wrong.**

| The operator asks | What the console does |
|---|---|
| What's failing? | The field is gray. The failed equipment is the only thing in color, with the cause in plain English. |
| How long do we have? | "Tank 3 reaches shutdown at 2:47 AM, give or take 15 minutes." A time slider plays the next 6 hours. |
| What's the cheapest fix? | The operator tries fixes directly on the 3D field. The console also tests combinations and ranks plans by dollars. |
| How long can I think? | **Last Call:** "Your cheapest plan expires at 2:20 AM." Each fix has a fuse, and the price of waiting climbs in steps. See [lastcall/](lastcall/README.md). |

### The one rule for the 3D

**If the operator can't act on it, it doesn't go in the scene.** The one exception is scenery: the desert plants, rocks, and fence live on the hideable `SCENERY` layer and are never clickable. Good-looking effects that only show what might happen are out. Exact numbers stay in 2D panels beside the scene.

| In the scene | What the operator does | What they get |
|---|---|---|
| Valves, pumps, and wells | Clicks one to try a change | Shutdown time and cost update immediately |
| An approved plan | Follows numbered steps | Each valve lights up in order and the camera moves to it |
| Tank pads and roads | Picks a truck | Arrival time and barrels removed feed into the forecast |

### The usability test

Someone who has never seen the console goes from the failure to an approved plan **in under 60 seconds, without touching the camera.** Try this on a stranger before judging.

## The demo

HackUTD's guide says to plan for a 3 minute pitch and 2 minutes of questions. Numbers below are examples.

1. **The failure:** The field is gray and calm at 1:47 AM. Pump 3 trips, and it's the only thing in color.
2. **The countdown:** "Tank 3 reaches shutdown at 2:47 AM, give or take 15 minutes. 14 wells go down with it."
3. **The future:** The judge drags the time slider. The tank rises and the wells go dark.
4. **The fix:** We hand the judge the mouse. They click a fix, like opening a crossover valve or sending a truck, and the clock and cost update right away.
5. **The plan:** The console shows the cheapest plan that keeps every tank safe. The operator approves it and gets numbered steps and a radio script.

## Who does what

| Area | Owner | First task |
|---|---|---|
| **Backend:** forecast, planner, API | `mounishmavuduru` | Follow [docs/BACKEND.md](docs/BACKEND.md) |
| **3D model quality in Blender** | Sriyan | Export a rough gray `blender/field.glb` with the correct names first, then improve it |
| **Web console:** 3D viewer, panels, time slider | `ronakkasaram` | Load the `.glb`, click an object, show its ID, then use the fake API data |
| **Scenario, costs, Devpost, pitch** | `vinnycodes67` | Ask the sponsor mentors the open questions below |
| **Last Call:** option expiry and the price of waiting | `ronakkasaram` | Mount `POST /api/lastcall` and draw fuse rings in the 3D field. See [lastcall/](lastcall/README.md) |

`vinnycodes67` and `ronakkasaram`: claim an open row, or tell Sriyan if you want to work on something else. Agents read the owner table in [AGENTS.md](AGENTS.md), so update it there too.

**Work in parallel.** Don't wait on each other. Build against [docs/CONTRACT.md](docs/CONTRACT.md) with fake data, and swap in the real pieces as they land. If the contract has to change, update the doc in the same commit and post on the board.

## The agent board

All of our coding agents talk on one pinned issue, the **[Message Board](https://github.com/teameuphoriainv-dot/hackutd-2026/issues/1)**. An agent posts when it finishes something or needs something, and tags the teammate it needs by GitHub login. That teammate's agent picks it up, so handoffs don't need a text.

**Everyone does this once:**

```bash
git pull
gh auth login
tools/board whoami      # should print your GitHub login
tools/board read -n 5   # should show recent posts
```

- **Claude Code:** Start it from this repo's folder and approve the project hooks if it asks. It then sees unread posts on its own and starts a background `tools/board wait`.
- **Any other agent:** Tell it to read [AGENTS.md](AGENTS.md) and run `tools/board unread` before each task.
- **Finishing:** An agent runs `tools/check` before it posts DONE. The check confirms the model names, the scenario, and the API still match the contract. See section 4 of [AGENTS.md](AGENTS.md).
- **Usage:** Every post wakes a waiting agent, which spends your usage. Set `BOARD_WAIT=off` if that gets expensive, and your agent will check the board before each commit instead.

Issue #1 has to stay open, so never write `Closes #1`. It's also locked to collaborators, because posts go straight into everyone's agents. That protects us when the repo goes public for judging, so leave it locked.

## Folders

```text
blender/    field.blend and the exported field.glb
web/        the console
backend/    server, forecast, planner
scenario/   the field definition and tonight's failure
lastcall/   option expiry and the price of waiting (runs standalone today)
docs/       this plan
```

Stay in your own folder and follow the workflow in [AGENTS.md](AGENTS.md). Never commit `.env` files or keys. GitHub rejects files over 100 MB, so keep `.blend` and `.glb` files well below that.

## Milestones

This assumes a 24-hour window. Confirm the real deadline first.

| Hour | Done means |
|---|---|
| 1 | Everyone has read this. A rough gray `field.glb` is pushed with the correct names. |
| 3 | The web console loads the model and clicking shows an asset ID. The backend serves `/api/field` and `/api/state`, even with fake data. |
| 8 | **First real loop:** Pump 3 fails, the forecast runs, and the time slider moves the tank levels. |
| 14 | **What-if works:** clicking a valve or truck returns a new timeline and cost. |
| 18 | Ranked plans and the approve step work. **Feature freeze.** |
| 21 | Model polish is done. Run the 60-second test on a stranger and rehearse the pitch. |
| 23 | Devpost is submitted, code is pushed, and judges can see the repo. |

**If we run late, cut in this order:** spill response, radio script, truck route detail.

**Never cut:** the failure, the countdown, one working what-if, and the cheapest plan.

## Not building

- A spill animation that only shows what might happen. A spill response tool, where the operator places a berm or stages a vacuum truck, is a stretch goal only.
- Any 3D models or visuals from other projects. Our models come from Blender.
- A chatbot as the main interface. AI can write the radio script and handoff notes, but the solver produces every number.
- Machine learning for the forecast. Simple physics is easier to explain and to trust at 2 AM.
- A connection to real field equipment. All data is simulated or from a provided dataset.

## Open questions

Whoever owns scenario and pitch should chase these first.

1. **Rules and deadline:** Read [this year's rules](https://hackutd-2026.devpost.com/rules). Our automated check was blocked, so they haven't been read yet. [Last year's rules](https://hackutd-2025.devpost.com/rules) had a hard Sunday noon deadline, a limit of 4 people, in-person expo judging, code linked on Devpost, and a limit of 2 sponsor challenges.
2. **Sponsor data:** Is there a dataset or API for this track? [EOG's 2025 API](https://hackutd2025.eog.systems/swagger/index.html) is still up and is good for practice, but the track text doesn't name a sponsor.
3. **Real numbers:** What level triggers a shutdown? What do trucking, disposal, and a shut-in well cost per hour? Ask the mentors.
4. **Real constraints:** What makes the obvious fix a bad one, such as a disposal well's pressure limit? This would make the best moment in the demo, but only if it's true.

## Words you'll need

| Term | Meaning |
|---|---|
| Produced water | The salt water that comes up with the oil |
| Disposal well (SWD) | A well that injects salt water underground |
| Crossover valve | A valve connecting two tanks or lines, so one can help the other |
| Choke a well | Reduce how much a well produces, which also reduces its salt water |
| Shut in | Stop a well entirely |
| Shutdown level | The tank level where the system stops things to prevent a spill |

## Credits

Sriyan confirmed with an organizer that working from downloaded code is allowed. Credit everything we reuse here and keep our own changes clear in the git history. Last year's rules disqualified projects that were "only copied code."

Backend logic reviewed for reuse:
- [CauldronWatch](https://github.com/anhnguyen0101/CauldronWatch) (HackUTD 2025)
- [SnitchTheWitch](https://github.com/Abhijith-utla/SnitchTheWitch) (HackUTD 2025)
- [EOG_HackUTD](https://github.com/Lord-Protector/EOG_HackUTD) (HackUTD VIII)
- [AquaLink](https://github.com/bilal-feroz/AquaLink) (simulation logic only, no 3D or visuals)

Agent board: [hackathon-agent-board](https://github.com/azizu06/hackathon-agent-board). `tools/board` and `.claude/` are copied unchanged, and `AGENTS.md` is adapted from its template.

When reused code lands, note the exact files here.
