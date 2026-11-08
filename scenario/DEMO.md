# Demo script: 3 minutes + 2 minutes of questions (#39)

**Rule:** every number on screen and in our mouths comes from the live backend on the demo laptop. Before each run, fill each `{LIVE: ...}` from the endpoint named, and rehearse with those values.

**Setup:** backend on 127.0.0.1:8000 with `scenario/field.json` (Avon), the console open full screen, the webcam on, the sample 3 photo on the desktop, and phone alerts on (ntfy).

**Fallbacks:** if the upload or build fails, skip beat 1 and say "here's a field we built earlier." If hand tracking misbehaves, use the mouse, since every gesture has one. If the Claude API is down, Jarvis's rules path still answers the scripted questions.

| Time | Beat | Say | Do |
|---|---|---|---|
| 0:00–0:15 | **Hook** | "Every barrel of oil brings up salt water. Tonight in Avon a pump fails, and in an hour a tank shuts in 14 wells. One night-shift operator has to see it, size it, and fix it cheaply." | Console on the gray, calm field at 1:44 AM. |
| 0:15–0:45 | **1. Your field, in 30 seconds** | "Any operator can start. This is a site plan someone drew on paper." | Drop `sample3_handdrawn.jpg` on the upload screen. The review table appears: "Claude read 21 items, and here's what the paper didn't say: lost dollars per hour on the wells. It never guesses." Fill one value with *Fill all*, confirm, and the 3D field builds. |
| 0:45–1:05 | **2. What's failing** | "1:47 AM. Pump 3 trips." | Pump 3 turns red, and it's the only color in the field. The phone buzzes with the alert. Point at the panel: the cause in plain English. |
| 1:05–1:25 | **3. How long** | "{LIVE: /api/forecast event text}", for example "Tank 3 reaches shutdown at 2:47 AM, give or take 11 minutes." | Drag the time slider. The tank rises and its wells go dark. Drag it back. |
| 1:25–1:55 | **4. Jarvis, hands-free** | "You don't even need the mouse." | Raise a hand. Point at Tank 3 and it highlights. Pinch, and Jarvis opens on Tank 3. Ask: "Why is Tank 3 rising, and what's my cheapest fix?" Jarvis flies the camera along the crossover and answers with the shutdown time and plan cost. |
| 1:55–2:25 | **5. The fix** | "Judge, your turn." | Hand the judge the mouse. They click the Tank 3 to 4 crossover. The clock and cost update instantly: {LIVE: shutdown before and after}. |
| 2:25–2:45 | **6. The plan** | "The cheapest plan that still holds if inflow runs 15% high: {LIVE: /api/plans robust plan summary and cost}. Doing nothing costs {LIVE: do-nothing cost}." Last Call: "This plan expires at {LIVE: /api/lastcall expiry}." | Approve. Jarvis proposes the valve command. **It waits for a person to confirm**, then the numbered steps and radio script appear. |
| 2:45–3:00 | **Close** | "Upload your field, get your own night-shift console, and it tells you what's failing, how long you have, and the cheapest fix. Avon stays lit." | Freeze on the approved plan and the countdown. |

## Likely questions

- **Where does the data come from?** In the demo, a simulator driven by the blueprint. In production, the same `connectors` block points at SCADA over MQTT, Modbus, or OPC-UA (docs/BLUEPRINT.md), with no code change.
- **Can the AI break something?** No. Jarvis only *proposes*. Every equipment command waits for a person to confirm, and that rule is enforced in the backend, not the UI.
- **What if a sensor dies?** The tank shows UNKNOWN, never OK, and the forecast says it can't vouch for that tank.
- **How accurate is the forecast?** It gives a range (±11 min here) and re-checks every plan at +15% inflow, so we never sell a plan that only works on a good night.
- **Why 3D?** Only things the operator can act on are in the scene, like valves, pumps, wells, and trucks. Exact numbers stay in 2D panels.
- **What did you build in 24 hours?** {fill at the end: line counts, PR count, what's live vs simulated}.

## Before going on stage

- [ ] `tools/check` PASS on the demo laptop
- [ ] Every `{LIVE: ...}` above filled from today's backend: `python scenario/rehearse.py --api http://127.0.0.1:8000 --fill` runs every beat, prints PASS/FAIL/SKIP and each number, and writes `scenario/DEMO.filled.md`. Restart the backend after it, because it trips Pump 3 and runs the live clock.
- [ ] Run the upload with sample 3 twice; time it under 30 s
- [ ] Hand tracking tested under the expo lighting
- [ ] Phone alerts on, volume up
- [ ] `.env` has `ANTHROPIC_API_KEY`, and it's never on screen
