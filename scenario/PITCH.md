# Pitch: the competitive slide (CF-12)

**Rule:** every claim carries a source tag. Letter-and-number tags like `[H3]` are in the Sources list of [docs/COMPETITIVE.md](../docs/COMPETITIVE.md) (PR #110). `[F1]` to `[F3]` and `[N1]` to `[N2]` point at rows of its feature matrix (section 2), listed under the slide. "Not found" means we found nothing public, not that the product lacks the feature. Freeboard cells were checked against `origin/main` at `a71a481`, so re-check them before the pitch.

---

## The slide

**Title:** They tell you a pump tripped. Freeboard tells you what happens next.

**Subtitle:** At 1:47 AM, Ignition [I1], AVEVA [A2], Experion [H1], and eLynx [L2] tell you a pump tripped. Freeboard tells you when Tank 3 shuts in, the cheapest fix, and when that fix expires.

- **When Tank 3 shuts in, with a range.** A 6-hour physics forecast with a plus or minus 15% band [F1]. The closest feature we found, Honeywell's Experion Operations Assistant, warns 5 to 10 minutes before an alarm [H3].
- **The cheapest fix, in dollars.** The planner tries combinations of fixes, ranks them by cost, and flags the plan that still holds if inflow runs 15% high [F2]. We found no public equivalent at Ignition, AVEVA, Honeywell, eLynx, or WellAware [N1].
- **When that fix expires.** Last Call gives each plan's start-by time [F3]. We found it at none of the six we compared [N2].

**Footnote (small type on the slide):**
[I1] Ignition Alarm Notification docs, 8.3 · [A2] Coterra well loading with AVEVA PI, event frame notifications · [H1] Experion PKS alarm shelving training module · [L2] eLynx cry-out alarms · [H3] Honeywell press release, March 2026, AI control room assistant · [F1] [F2] [F3] [N1] [N2] Freeboard feature matrix, docs/COMPETITIVE.md section 2. "Not found" means no public evidence found, not proof of absence.

### Matrix rows behind the F and N tags

| Tag | Row in docs/COMPETITIVE.md section 2 | What the row says |
|---|---|---|
| [F1] | Forecast of what happens next, Freeboard column | "Yes. 6 h physics forecast with a plus or minus 15% band (`backend/app/forecast.py`, main)" |
| [F2] | Cheapest fix (optimization), Freeboard column | "Yes. Tries lever combinations, ranks by dollars, flags the robust plan (`backend/app/planner.py`, main)" |
| [F3] | How long until a fix expires, Freeboard column | "Yes. Last Call (`lastcall/`, main)" |
| [N1] | Cheapest fix (optimization), competitor columns | Not found for EOG, Ignition, AVEVA, Honeywell, and eLynx / WellAware. Cognite: "Agents can act on workflows" [C1] |
| [N2] | How long until a fix expires, competitor columns | Not found in all six columns: EOG, Ignition, AVEVA, Honeywell, Cognite, eLynx / WellAware |

### Speaker notes (not on the slide)

- **Where they lead, if a judge asks.** Alarm management. Ignition shelves alarms for 5 minutes to 4 hours [I7], Experion shelves too [H1], and Honeywell's DynAMo suite claims up to 80% fewer alarms [H2]. That 80% is the vendor's own figure. Our version is CF-7 and CF-1. Say it is shipped only if it is merged and working on the demo laptop.
- **EOG.** EOG's control center already gets real-time alerts [E1], and its control room operators manage incoming alarms over SCADA [E6]. Pitch Freeboard as the layer that turns that alert into a decision, not as a replacement for SCADA.
- **No numbers on this slide.** The times and dollars come from the live demo (DEMO.md), never from a slide.

---

## Devpost paragraph

This also appears in [DEVPOST.md](DEVPOST.md) under "How we compare".

At 1:47 AM, Ignition [I1], AVEVA [A2], Experion [H1], and eLynx [L2] tell you a pump tripped. Freeboard tells you when Tank 3 shuts in, the cheapest fix, and when that fix expires. Its forecast runs six hours ahead with a plus or minus 15% band [F1]. The closest commercial feature we found, Honeywell's Experion Operations Assistant, warns 5 to 10 minutes before an alarm [H3]. The planner ranks fixes by dollars and flags the one that still holds if inflow runs 15% high [F2], and Last Call says when each fix stops being possible [F3]. We found no public equivalent of the planner at Ignition, AVEVA, Honeywell, eLynx, or WellAware [N1], and no fix expiry at any of the six we compared [N2]. Where they lead is alarm management: Ignition and Experion shelve alarms on a timer [I7] [H1], and Honeywell's DynAMo suite claims up to 80% fewer alarms, by the vendor's own count [H2]. EOG already sends real-time alerts to a control center [E1], so Freeboard is built to sit on top of SCADA and turn an alert into a decision.
