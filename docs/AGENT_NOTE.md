# Agent note: close the gaps with real monitoring systems

**For:** every teammate's coding agent. **From:** Sriyan (lead), written by Sriyan's Claude. Issue #107.
**Read first:** [COMPETITIVE.md](COMPETITIVE.md). Source tags like `[H2]` resolve in its Sources list.

## What this is

We compared Freeboard with what operators use today: EOG's public tools, Ignition, AVEVA, Honeywell, Cognite, eLynx, WellAware, and produced water operators. Freeboard already beats all of them on three things: it forecasts when a tank shuts in, it prices every fix and says when each one expires, and it never calls a tank with no reading safe. Where we lose is **alarm handling**: priorities, shelving, flood handling, an audit trail, and locked write actions. Those are the first questions a control room judge will ask.

Each task below is a `CF-N` row. Pick yours by owner. Do not start another lane's row.

## Rules for every agent

1. **Lanes from AGENTS.md hold.** Nobody edits another lane's files without the owner's or the lead's OK on the board. Rows that touch two lanes say who writes and who merges.
2. **Sriyan's 3D control panel rows touch `web/`.** They follow the #101 pattern: Sriyan's agent writes new modules under `web/src/` plus small hooks, and **Ronak merges**. They must fit the default screen the lead picked, Vinny's "Freeboard Remote" layout ported beside the 3D field (see the board), and `web/src/highlight.js` (#101).
3. **The orchestrator (Vinny's agent) folds the accepted rows into ROADMAP.md** and can reorder or reassign them. If Vinny's agent is saturated, CF-7 and CF-10 can move to Mounish, since both live in `backend/`.
4. **After M4 (6:00 AM CST) only bug fixes land.** A row not merged by its milestone gets cut, not rushed.
5. A row that changes an API updates `docs/CONTRACT.md` in the same commit and posts on the board.
6. Effort: **S** is under 1 hour, **M** is 1 to 3 hours, **L** is more than 3 hours.

## Top 10 for tonight

In build order. M3 is 2:00 AM CST, M4 (feature freeze) is 6:00 AM CST.

| # | ID | Owner | What | Milestone | Effort |
|---|---|---|---|---|---|
| 1 | CF-7 | Vinny | Alerts API: priority from the forecast, root-cause grouping, shelve with a timer | M3 | M |
| 2 | CF-1 | Ronak | Alarm list: priority, ack, shelve with a reason, grouped under the root cause | M3 | M |
| 3 | CF-18 | Sriyan | Blind sensor in 3D: hide the level we don't have | M3 | S |
| 4 | CF-10 | Vinny | Audit log of every proposal, confirm, ack, and shelve | M3 | S |
| 5 | CF-17 | Sriyan | 3D alarm markers by priority | M4 | M |
| 6 | CF-19 | Sriyan | Consequence chain in 3D: what fails next, and when | M4 | M |
| 7 | CF-8 | Vinny | Demo beat: the alarm flood becomes one cause and one clock | M4 | S |
| 8 | CF-4 | Ronak | Structured handoff sections | M4 | S |
| 9 | CF-3 | Ronak | Past plus future on one trend line | M4 | M |
| 10 | CF-15 + CF-5 | Mounish + Ronak | Operator PIN on every write action | M4 | S + S |

---

## Ronak (`@ronakkasaram`): `web/`

### CF-1: Alarm list with live priority, ack, and shelve (TOP 10)
- **Build:** an alarm list fed by `/api/alerts` and `/ws` (PR #57, extended by CF-7). In the Freeboard Remote layout it is the alerts feed beside the Jarvis chat, and in Classic it is a panel. Each row shows the priority (P1, P2, P3), the asset, the plain-English cause, and the consequence with a clock time, like "Pump 3 tripped. Tank 3 shuts in at 2:47 AM (60 min)". Consequential alarms fold under their root cause as a count. **Ack** is one click. **Shelve** asks for a reason and a duration (15 min to 4 h), shows the time left, and the alarm comes back when the timer ends. Every ack and shelve writes a shift log line.
- **Why it beats them:** Ignition and Experion let operators shelve alarms [I7] [H1], but their priorities are set by an engineer ahead of time. Ours come from the live forecast, so the alarm with the least time left is always on top. The eLynx team itself says operators get over 50 alarm notifications a day [L5].
- **Acceptance:** trip Pump 3 in the sim. Within 3 readings, one P1 row appears with the shutdown time. The tank high-level and well alarms that follow sit under it as "+N related", not as separate rows. Shelving without a reason is refused. A shelved alarm returns when its timer ends. The shift log shows every ack and shelve with the clock time.
- **Effort:** M. **Milestone:** M3. **Depends on:** CF-7 (build against a fake JSON until it lands).

### CF-2: Alarm rate meter and flood mode
- **Build:** a small counter, "alarms in the last 10 min: N". Above 10, show "Alarm flood" and collapse the list to root causes only.
- **Why:** EEMUA 191 defines a flood as more than 10 new alarms in 10 minutes [X2]. Honeywell sells a whole suite to prevent floods [H2]. We show the flood rule live on screen.
- **Acceptance:** fire 12 alarms in the sim inside 10 minutes. The meter turns amber and the list shows only root causes, each with its count.
- **Effort:** S. **Milestone:** M4.

### CF-3: Past plus future on one trend (TOP 10)
- **Build:** for the selected tank, one small chart: the last 60 minutes of live level as a solid line, then the forecast as a dashed line with its low and high band, and the shutdown level as a horizontal line. Keep the history in the browser from `/ws` readings, and use CF-14 when it exists.
- **Why:** historians like the PI System show the past [A5]. Honeywell's new assistant looks 5 to 10 minutes ahead [H3]. Nobody we found puts the past and an hour-long forecast with a range on one line.
- **Acceptance:** select Tank 3 after the trip. The chart shows the rise so far, the dashed forecast crossing the shutdown line at the same time as the countdown, and the band. A what-if moves only the dashed part.
- **Effort:** M. **Milestone:** M4.

### CF-4: Structured handoff (TOP 10)
- **Build:** `handoffText()` in `web/src/main.js` gets fixed sections in this order: open alarms by priority, shelved alarms with the reason and the time left, blind sensors, the approved plan with each step done or pending, the next deadline from Last Call, and the what-ifs tried.
- **Why:** Honeywell and Engen found that a structured handover gave the next shift a better picture of the plant than a free-form log [H4]. Ours is filled in automatically.
- **Acceptance:** after the demo path (trip, what-if, approve, two steps done, one alarm shelved), the saved handoff has all six sections, and nothing in it was typed by hand.
- **Effort:** S. **Milestone:** M4.

### CF-5: Operator PIN prompt (TOP 10, with CF-15)
- **Build:** when a write call returns 401, ask for the operator PIN once per session on the console and on `/phone`, then retry.
- **Why:** see CF-15.
- **Acceptance:** with `OPERATOR_PIN` set on the backend, confirming a fix asks for the PIN once, and a wrong PIN shows "Wrong PIN. Nothing was sent."
- **Effort:** S. **Milestone:** M4.

### CF-6: Every live number shows its age
- **Build:** each live value in the panels shows "12 s ago". Past `stale_after_min` it shows UNKNOWN. This extends #56.
- **Why:** at Texas City, operators trusted a level reading that was wrong [X4].
- **Acceptance:** stop one sensor in the sim. Its age counts up, then it turns UNKNOWN at `stale_after_min`.
- **Effort:** S. **Milestone:** M3.

---

## Vinny (`@vinnycodes67`): Jarvis and IoT backend, scenario, demo, Devpost

### CF-7: Alerts API with priority, grouping, and shelving (TOP 10)
- **Build:** extend the alerts in PR #57 (`backend/app/detect.py`, `live.py`):
  - `priority`: `P1` if the alarm leads to a shutdown or lost production within 60 minutes in the baseline forecast, `P2` within 180 minutes, `P3` otherwise. Add `minutes_to_consequence` and `usd_at_risk`.
  - `root_id`: alarms caused by another alarm point at it. A tank high level after its pump tripped points at the pump trip.
  - `shelved_until` and `shelve_reason`, with `POST /api/alerts/{id}/shelve {minutes, reason}`. Allow 15 to 240 minutes, require a reason, and unshelve on expiry with a `/ws` message.
  - Document all of it in `docs/CONTRACT.md` in the same commit.
- **Why:** ISA 18.2 tools like DynAMo exist to rank and thin out alarms [H2], and Ignition shelves for 5 minutes to 4 hours [I7]. Our priority comes from the forecast, so it changes as the situation changes.
- **Acceptance:** a test trips Pump 3 and gets one P1 alert with `minutes_to_consequence` within 5 minutes of the forecast's shutdown event. The Tank 3 high alert has `root_id` set to the pump trip. Shelve with no reason returns 422, and a shelved alert comes back when its time is up.
- **Effort:** M. **Milestone:** M3. **Files:** `backend/` (your #57 code). Mounish can take it if you hand it off on the board.

### CF-8: Demo beat: the flood becomes one cause (TOP 10)
- **Build:** in `scenario/`, make the pump trip cascade as it would in the field: tank high, high-high, wells shutting in, and one stale sensor, which is 8 or more alarms. In `scenario/DEMO.md`, add a 15-second beat: "A normal alarm list just showed you nine rows. Freeboard shows one cause, one clock, and the cheapest fix."
- **Why:** after Milford Haven, the UK regulator found that too many alarms in an emergency made the operators less effective [X3]. EEMUA calls more than 10 alarms in 10 minutes a flood [X2].
- **Acceptance:** `scenario/rehearse.py` runs the beat and checks that the console shows exactly one P1 root cause with the rest grouped under it.
- **Effort:** S. **Milestone:** M4.

### CF-9: On-call escalation
- **Build:** if a P1 is not acknowledged in N minutes, push it to the next person on a roster (from `site.on_call` in the blueprint or an env var) through `notify.py`. An ack from any device stops the chain.
- **Why:** Ignition has on-call rosters [I1], and eLynx sells escalations [L2]. A missed 2 AM alert is the failure that matters.
- **Acceptance:** with N set to 1 minute in the sim, an unacked trip reaches the second phone 1 minute after the first, and acking on either phone stops it.
- **Effort:** M. **Milestone:** M4.

### CF-10: Audit log (TOP 10)
- **Build:** append every proposal, confirm, reject, approve, ack, and shelve to an append-only log with the time, the device or PIN holder, the source (console, phone, or Jarvis), and the asset. Serve it at `GET /api/audit`. This is the audit part of #66, so agree with that issue's owner who writes it.
- **Why:** Ignition keeps an audit log of user actions [I5]. A judge will ask who opened the valve, and we should show the answer.
- **Acceptance:** run the demo path. `GET /api/audit` lists every step in order, including the Jarvis proposal and the person who confirmed it.
- **Effort:** S. **Milestone:** M3.

### CF-11: Jarvis says where each number came from
- **Build:** every number in a Jarvis answer names its source and age, like "Tank 3 is at 71% (reading 20 s old). The forecast at 1:52 AM says shutdown at 2:47 AM." A stale asset gets "I have no current reading for Tank 5, so I can't say it's safe." Add both cases to the D2 eval.
- **Why:** AVEVA's assistant answers from retrieved data [A3]. Ours also says how old the data is, and it refuses to vouch for missing data.
- **Acceptance:** the D2 eval passes 20 of 20 with the two new cases.
- **Effort:** S. **Milestone:** M4.

### CF-12: Competitive slide and Devpost section
- **Build:** one slide and one Devpost paragraph: "At 1:47 AM, Ignition, AVEVA, Experion, and eLynx tell you a pump tripped. Freeboard tells you when Tank 3 shuts in, the cheapest fix, and when that fix expires." Use only rows from COMPETITIVE.md, with its sources.
- **Why:** judges from EOG know these tools. Showing that we know them too makes our claims believable.
- **Acceptance:** every claim on the slide has a source tag in COMPETITIVE.md.
- **Effort:** S. **Milestone:** M5 (pitch).

### CF-13: A real disposal limit in the scenario
- **Build:** ask the mentors for a realistic SWD limit. If they give one, set `max_bbl_hr` on the disposal wells to match it, and note in the scenario that it stands for the permit's pressure cap. This makes "send everything to SWD_1" a bad fix, which is open question 4 in the README.
- **Why:** the Texas RRC now requires daily maximum surface injection pressure for new and amended Permian SWD permits [X9], and Permian disposal pressure is rising [B2].
- **Acceptance:** the planner's cheapest plan changes because of the cap, and the demo can say why in one sentence. Skip this row if there are no real numbers.
- **Effort:** S. **Milestone:** M4.

---

## Mounish (`@mounishmavuduru`): `backend/`

### CF-14: Readings history
- **Build:** `GET /api/history?asset_id=TANK_3&minutes=120` from an in-memory ring buffer with one point per asset per sim minute. It backs CF-3. It hooks into the live loop in PR #57, so tell Vinny on the board before touching `live.py`.
- **Why:** every SCADA product has a historian [A5] [I8]. We only need two hours of it for the trend.
- **Acceptance:** after 30 sim minutes, the endpoint returns 30 points for Tank 3, and a stale period comes back as nulls, never a repeated last value.
- **Effort:** S. **Milestone:** M4.

### CF-15: Lock the write actions (TOP 10, with CF-5)
- **Build:** confirm, reject, approve, ack, and shelve require an operator PIN from the `OPERATOR_PIN` env var (never committed). Reads stay open. Without it, return 401 `{"detail": "Enter the operator PIN to send commands."}`. With no `OPERATOR_PIN` set, behave as today, so the demo can't break.
- **Why:** every platform we checked has users and permissions [I5] [A3] [C1]. Today `/phone` has no login (PR #58), and a judge will ask whether anyone on the Wi-Fi can open a valve.
- **Acceptance:** with the PIN set, a confirm without it returns 401 and the fake PLC gets no write. With the PIN, it works. A test covers both.
- **Effort:** S. **Milestone:** M4.

### CF-16: Daily SWD report (only if everything else is done before M4)
- **Build:** `GET /api/reports/swd-daily.csv` with each disposal well's daily volume, maximum rate, and maximum and average surface pressure (simulated), in the order the RRC lists them.
- **Why:** the RRC requires these daily values for new Permian SWD permits [X9], and the annual Form H-10 asks for monthly average and maximum injection pressure [X10]. No competitor page we read mentions this report.
- **Acceptance:** the CSV opens in a spreadsheet with one row per SWD per day, and the columns match the RRC notice.
- **Effort:** M. **Milestone:** M4 at the latest, or cut it.

---

## Sriyan (`@teameuphoriainv-dot`): 3D model, ingest, library, and the 3D control panel

The rows below that change `web/` are new modules plus small hooks, and **Ronak merges them**, the same as #101.

### CF-17: 3D alarm markers by priority (TOP 10)
- **Build:** each alarmed asset gets a marker above it. P1 is a pulsing red ring, P2 is amber, and P3 is small and steady. A shelved alarm is a hollow gray ring with a clock. At overview zoom, each pad shows one badge with its alarm count. Drive it through the `alarm` source in `highlight.js` (#101), and let #102's freeboard tag take over on tanks so no tank gets two markers.
- **Why:** Ignition has no native 3D, and its users embed a web viewer in an iframe [I4]. ISA-101 practice keeps color for abnormal states only [I3] [X7], which is how our gray field already works.
- **Acceptance:** after the trip, Pump 3 has the only pulsing red ring, and its pad badge says "1 + 3 related". Shelving the alarm turns the ring hollow. The console holds 30 fps or better on the demo laptop.
- **Effort:** M. **Milestone:** M4. **Files:** a new `web/src/` module and a hook; Ronak merges.

### CF-18: Blind sensor in 3D (TOP 10)
- **Build:** when a tank's level is UNKNOWN, hide its `<tank>_LEVEL` fill and the `<tank>_gauge` float, give the shell a gray hatch with a "?" tag, and show the `SENSOR_<tank>_light` as lost. Never draw a level we don't have. This extends `web/src/iot3d.js` (#93).
- **Why:** at Texas City, operators were "misled by the malfunctioning level indicator" [X4]. A 3D tank that keeps showing an old fill level does the same thing.
- **Acceptance:** stop Tank 5's sensor in the sim. At `stale_after_min`, its fill disappears, the "?" shows, and it comes back when readings resume.
- **Effort:** S. **Milestone:** M3. **Files:** `web/src/iot3d.js` (your #93); Ronak merges.

### CF-19: Consequence chain in 3D (TOP 10)
- **Build:** selecting an alarm lights its chain in order: the tripped pump, then the filling tank, then each well that shuts in. Each gets a numbered tag with the clock time it fails, taken from the forecast events. The time slider scrubs the chain. Applying a fix in a what-if moves or clears the tags.
- **Why:** competitors list alarms. Cognite shows root cause analysis after the fact [C2]. Nobody we found shows what fails next, and when, on the model itself.
- **Acceptance:** select the Pump 3 alarm. Pump 3, Tank 3, and Tank 3's wells light in order with times that match `/api/forecast`. Opening the crossover in a what-if clears the well tags.
- **Effort:** M. **Milestone:** M4. **Files:** a new `web/src/` module; Ronak merges.

### CF-20: ISA-101 levels as 3D zoom levels
- **Build:** keys 1 to 4 jump between Level 1 (field overview, `CAM_OVERVIEW`), Level 2 (the selected pad), Level 3 (the selected asset), and Level 4 (the part or sensor behind the alarm). A small breadcrumb shows the level.
- **Why:** ISA-101 organizes displays from Level 1 overview to Level 4 detail [X7]. This maps it onto the 3D field, so operators who know the standard know where they are.
- **Acceptance:** from any view, pressing 1 frames the whole field, and pressing 4 with an alarm selected frames the sensor behind it.
- **Effort:** S. **Milestone:** M4.

### CF-21: Ingest asks for alarm setpoints
- **Build:** in `ingest/`, make `warning_pct` and `shutdown_pct` for every tank, and the sensor that measures its level, review items when they are missing from the plan. The upload screen (#49) already shows review items.
- **Why:** in plants, alarm rationalization is a separate project with its own software [H2] [X5]. Freeboard asks for the setpoints at upload, so a new field starts with its alarms defined.
- **Acceptance:** sample 2 (the CSV) without `warning_pct` gets one review item per tank, and the blueprint passes `validate_blueprint.py` once they are filled.
- **Effort:** S. **Milestone:** M4.

---

## How to report

When a row is done, post the usual DONE line on the board with its CF ID, like `DONE CF-18 (#N)`. If a row is blocked, post `BLOCKED CF-N: <why> | needs <who>`.
