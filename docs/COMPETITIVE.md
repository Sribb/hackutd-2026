# Competitive map: Freeboard vs the systems operators use today

Issue #107. Researched Sat Nov 7, 2026 for Sriyan (lead). The task list built from this file is [AGENT_NOTE.md](AGENT_NOTE.md).

**How to read this file**
- Every claim about another company has a source tag like `[I1]`. The tags are listed in [Sources](#sources) at the bottom, with what each one says.
- "Not found" means we found nothing public in the time we had. It does not prove the product lacks the feature.
- Vendor pages are marketing. Numbers from them (like "80% fewer alarms") are the vendor's own claims.
- Freeboard cells say where the feature lives: **main** means it is merged on `origin/main` at commit `a71a481`, and **PR #N** means it is only on an open pull request.

## 1. What we know about EOG (the likely sponsor)

The track text names no sponsor. EOG ran the HackUTD 2025 challenge, and its API is still up [E8]. Everything below is public. Nothing here comes from a login or a paywall.

**Verified (EOG's own documents)**
- EOG runs **continuous leak detection that sends real-time alerts to a control center**: "Methane sensing technology continuously monitors facilities and provides actionable real-time alerts of potential leaks to a control center." The photo shows iSense methane sensors on a tank battery [E1].
- **iSense covers 99% of the Delaware Basin**, measured as the share of gross oil production handled at central tank batteries covered by iSense at year end 2025 [E2].
- **More than 99% of the water EOG used in the Delaware Basin in 2024 came from reuse or nonfresh sources** [E1]. Produced water is a core business input for EOG, not just waste.
- EOG lists "Superior In-House Technical Expertise, Proprietary Information Technology" and "Technology Leadership and Real-Time, Data-Driven Decision Making" as company strengths [E1].
- EOG's employer profile says its "100+ proprietary, in-house developed, mobile and desktop apps give everyone at EOG a virtual control room", backed by "massive proprietary integrated data warehouses and predictive analytics" [E3].

**Verified (secondary sources)**
- A 2018 Houston Chronicle article describes EOG's in-house "app store", including iSteer, which sends steering instructions from the office to the rig [E4].
- A 2017 World Oil column reported more than 65 proprietary apps at the time [E5]. We read only the search summary, because the full article is paywalled.
- EOG sold 23 saltwater disposal wells and about 300 miles of water gathering pipeline in southeastern New Mexico to a water midstream company, and signed a long-term disposal contract with the buyer [E11].
- Job postings attributed to EOG on job boards describe a Control Room Operator who monitors "production facilities and well site locations through SCADA" and manages "incoming alarms" by calling field staff [E6], an Automation Tech who builds PLC logic "for the control of valves, pumps, and alarm monitoring" [E7], and a contract SCADA admin role in Corpus Christi that administers "multiple CygNet SCADA systems" and asks for CygNet or Ignition experience [E9]. These are job board copies, some expired, so details may be stale.
- The 2025 HackUTD challenge API had three endpoints: historical cauldron levels, transport tickets, and the cauldron list [E8]. Teams compared tank levels against hauling tickets to find mismatches [E10].

**Inference (not stated by EOG)**
- EOG builds its own tools and owns its data, so judges from EOG will likely respect software that is data-first, explains every number, and could be built in-house. A flashy wrapper over a vendor product will impress them less.
- EOG already has a control center that gets real-time alerts [E1], and control room operators who triage alarms by phone [E6]. Freeboard should present itself as the layer that turns an alarm into a decision, not as a replacement for SCADA.
- Because some of EOG's water goes to third-party disposal under contract [E11], the cost of trucking, disposal, and shut-in production is a real lever. Our dollar-ranked plans speak to that.

**Nothing public:** we found no public description of how EOG monitors produced water tanks or SWDs at night, what its control room screens look like, which alarm priorities it uses, or what its field mobile apps show for water.

## 2. Feature matrix

Legend: **Yes** (documented), **Partial**, **No**, **Not found** (no public evidence found). Freeboard cells cite the file or PR.

| Feature | EOG (public) | Ignition | AVEVA | Honeywell | Cognite | eLynx / WellAware | Freeboard today |
|---|---|---|---|---|---|---|---|
| Alarm priorities (ISA-18.2) | Not found. Operators "manage incoming alarms" [E6] | Not checked | Event frames trigger notifications [A2] | Yes. DynAMo is built to ISA 18.2 and EEMUA 191 [H2] | Not found | Escalations and an alarm dashboard [L2] | **No.** PR #57 alerts have a type, no priority |
| Shelving with a timer | Not found | Yes, 5 min to 4 h [I6] [I7] | Not checked | Yes [H1] [H5] | Not found | Not found | **No** |
| Flood handling and suppression | Not found | Not found | Not found | Yes. Claims up to 80% fewer alarms and prevents floods and chattering [H2]. Dynamic suppression [H5] | Not found | eLynx: "alarm for multiple conditions to aggregate symptoms of a root cause" [L5] | **Partial.** The field is gray and only the failed asset is in color (README, `web/src/main.js` `paint`) |
| Root cause first | Not found | Not found | Not found | AI assistant warns 5 to 10 min before alarms [H3] | Root cause analysis after the fact on a canvas [C2] | Aggregate symptoms [L5] | **Yes.** `incident.root_asset_id` and a plain-English reason in `/api/state` (main) |
| Acknowledge from anywhere | Not found | SMS with an ack link, voice ack [I1] | Not found | Not found | Not found | Web, mobile, text, voice, email [L3] | **Partial.** `POST /api/alerts/{id}/ack` (PR #57) |
| Trends and historian | Data warehouses and analytics [E3] | Historian, rebuilt in 8.3 [I8] | PI System is the core product [A5] | Not checked | Time series in Data Fusion [C2] | Not found | **No history.** Forecast timeline only (main) |
| ISA-101 high performance HMI | Not found | Documented gray HMI techniques [I3] | Not checked | Hosts ASM Consortium guidelines [X6] | Not found | Values in alarm show red [L4] | **Yes.** Gray field, color only for status (CONTRACT status colors, main) |
| Maps and geo views | Not found | Not checked | A "command-centric operations facility" [A1] | Not checked | Not checked | eLynx map and tank views [L4] | **Partial.** Plan meters, no geo basemap |
| 3D or digital twin view | Not found | **No native 3D.** Users embed three.js in an iframe [I4] | Not checked | Not checked | 3D in Industrial Canvas [C2] | Not found | **Yes.** Clickable 3D generated from a blueprint, animated pumps, valves, levels (main) |
| KPIs and dashboards | Not found | Not checked | KPIs per process and equipment [A1] | Not checked | Not checked | Alarm dashboard [L2] | **Partial.** Plan cost, cost of waiting, wells online (main) |
| Shift handover | Not found | Not found | Not found | Structured logbook study at Engen [H4] | Not found | Voice-to-text field notes [L2] | **Yes.** Auto shift log and handoff export (`web/src/shiftlog.js`, main) |
| Mobile alerts and on-call | 100+ mobile and desktop apps [E3] | On-call rosters, native app [I1] [I2] | Not checked | Not checked | Not found | Mobile app, callouts, groups by route [L1] [L2]; WellAware phone control [W1] [W2] | **Partial.** `/phone` with hold to confirm (PR #58), ntfy push (PR #57). No roster |
| Work orders | Not found | Not found | Not found | Not found | Work orders in the canvas [C2] | Not found | **Partial.** Numbered plan steps and a radio script (main) |
| Control with confirmation | PLC logic for valves and pumps [E7] | Not checked | Not checked | Not checked | Not found | WellAware remote pump control [W2] | **Yes, on PRs.** Propose, then a person confirms, then the connector sends (PR #57, #74) |
| Users, roles, audit trail | Not found | Audit log [I5] | AI answers follow user permissions [A3] | Not checked | Admins control who builds agents [C1] | Not found | **No.** `/phone` has no login (PR #58). Audit log is planned in #66 |
| Texas RRC reporting | Not found | Not found | Not found | Not found | Not found | Not found | **No.** RRC now wants daily SWD pressure and volume for new Permian permits [X9] |
| Forecast of what happens next | "Predictive analytics" [E3] | Not found | Analytics on PI data [A2] | Predicts 5 to 10 min before an alarm [H3] | Not found | WellAware empty-tank prediction [W1] | **Yes.** 6 h physics forecast with a plus or minus 15% band (`backend/app/forecast.py`, main) |
| Cheapest fix (optimization) | Not found | Not found | Not found | Not found | Agents can act on workflows [C1] | Not found | **Yes.** Tries lever combinations, ranks by dollars, flags the robust plan (`backend/app/planner.py`, main) |
| How long until a fix expires | Not found | Not found | Not found | Not found | Not found | Not found | **Yes.** Last Call (`lastcall/`, main) |
| AI assistant | Not found | Not found | Industrial AI Assistant, generally available July 2025 [A3] | Experion Operations Assistant, March 2026 [H3] | Atlas AI agents [C1] | Not found | **Yes, on PR.** Jarvis rules path and Claude tools, never sends without a person (PR #57) |
| Protocols (OPC UA, MQTT, Modbus) | A posting under EOG's name maintains CygNet SCADA [E9] | MQTT modules, OPC UA [I9] | Not checked | Not checked | Not checked | Own hardware kit [L2] | **Partial.** MQTT and Modbus with a fake SCADA and PLC (PR #74). OPC UA is a stub |
| New site to working console | Not found | You build the screens | "Industry templates" [A1] | Sells alarm rationalization software [H2] | Works on contextualized data [C2] | Sensor hardware per tank [W1] | **Yes.** Site plan photo, PDF, or CSV, then review, then a 3D field (`ingest/`, generator, `web/src/upload.js`, main; build API PR #64) |
| Missing data never shown as safe | Not found | Not found | Not found | Not found | Not found | Not found | **Yes.** No-reading tanks get a warning and are treated as at risk (`forecast.py`, `main.js` `blindTanks`, main) |

## 3. Where each competitor is weak

**EOG's own tools (public view).** Everything public is about leak detection, drilling, and data [E1] [E3] [E4]. We found nothing on a night-shift produced water console that forecasts and prices fixes. *Inference:* EOG's control room gets alerts [E1] [E6], and the operator still has to work out what the alert means and what to do.

**Ignition (Inductive Automation).** It is a toolkit. You get alarm pipelines, rosters, shelving, and a historian [I1] [I6] [I8], but your engineers build every screen. It has no native 3D: forum answers say to embed a three.js page in an iframe [I4]. We found no built-in forecast or cost optimizer.

**AVEVA (PI System, Unified Operations Center).** It is strong on history and enterprise KPIs [A1] [A5]. Its AI assistant uses retrieval over CONNECT data to answer questions [A3]. *Inference from those pages:* it explains what the data says, but we found nothing about simulating a fix and pricing it. It is sold through a credits subscription [A1].

**Honeywell (Experion, DynAMo).** It has the best alarm management we found: shelving, suppression, and an alarm suite built to ISA 18.2 that claims up to 80% fewer alarms [H1] [H2] [H5]. Its new Operations Assistant predicts trouble 5 to 10 minutes before an alarm [H3]. It is built for process plants with a DCS. *Inference:* a lease of tanks, wells, and SWDs on cellular RTUs is not its home turf, and 5 to 10 minutes of warning is shorter than the hour a tank gives you.

**Cognite (Data Fusion, Atlas AI).** It contextualizes data and runs AI agents, with 3D, time series, and work orders on one canvas [C1] [C2]. Its showcased use is root cause analysis on past events [C2]. Getting there needs a data platform project first.

**eLynx and WellAware (pumper tools).** They are lean and field-first: mobile apps, multi-channel callouts, ack by text or voice, tank levels, and cheap sensors (WellAware lists $420 per tank per year) [L1] [L2] [L3] [W1]. eLynx itself says oil and gas operators get "over 50 alarm notifications in just one day" and suffer alarm fatigue [L5]. We found no forecast of shutdown time and no cost-ranked fixes.

**Produced water specialists.** Select Water's AquaView runs a 24/7 remote operations center with SCADA and a mobile app [S1]. WaterBridge and Aris hire control room operators for remote SWD and pipeline control [WB1] [AR1]. B3 Insight forecasts disposal capacity and reservoir pressure across the Permian, which is basin planning, not a shift console [B1] [B2].

## 4. Where Freeboard wins (true today, checked against our code)

1. **It answers the next question, not just the first one.** A competitor tells you a pump tripped. Freeboard tells you when the tank shuts in (with a range), what each fix costs, and when each fix stops being possible. Code: `backend/app/forecast.py` (`RANGE = 0.15` gives `t_min_low` and `t_min_high`), `backend/app/planner.py` (tries combinations with `itertools.product`, keeps a robust plan that survives 15% more inflow), and `lastcall/core.py`. The closest commercial feature we found is Honeywell's assistant, which warns 5 to 10 minutes ahead [H3].
2. **It never calls a blind tank safe.** A tank with no reading gets "its future is unknown. Treat it as at risk." (`forecast.py`), the console lists blind tanks (`main.js` `blindTanks`), and the planner tracks `unknown` tanks. At Texas City, operators "were misled by the malfunctioning level indicator" and a high-level alarm "failed to activate" [X4]. We found no competitor page that states this rule.
3. **A site plan becomes a working 3D console.** Upload a photo, PDF, or CSV, review the gaps, and get a clickable 3D field with the same IDs as the API (`ingest/`, `blender/build_field.py`, `web/src/upload.js`, all on main). Ignition has no native 3D [I4], and the others start with a data or screen-building project [A1] [C2].
4. **One cause in color, the rest gray.** That is the ISA-101 idea of color only for abnormal states [I3], applied to a whole field. After Milford Haven, the UK HSE found an "excessive number of alarms in emergency situation reduced effectiveness of operator response" and that graphics "did not provide necessary process overviews" [X3].
5. **The handoff writes itself.** Every what-if, approval, and finished step goes into a timestamped shift log that exports as a handoff note (`web/src/shiftlog.js`, `main.js` `handoffText`). Honeywell's study with Engen found that structured handovers improved the next shift's picture of the plant [H4].
6. **On open PRs, not main yet:** Jarvis proposes and a person confirms every command, over real MQTT and Modbus, and tests prove nothing reaches the fake PLC without a confirm (PR #57, PR #74).

## 5. Gaps to close

Ranked by judge impact first, then by effort. S is under 1 hour, M is 1 to 3 hours, L is more than 3 hours. Task IDs point into [AGENT_NOTE.md](AGENT_NOTE.md).

| Rank | Gap | Who has it | Judge impact | Effort | Task |
|---|---|---|---|---|---|
| 1 | Alarm priorities computed from the forecast (minutes and dollars to consequence), with grouping under the root cause | Honeywell [H2], Ignition pipelines [I1] (static priorities) | High | M | CF-7, CF-1 |
| 2 | Alarm flood shown as one cause, in the demo | Honeywell [H2], eLynx [L5]; EEMUA flood rule [X2] | High | S | CF-8 |
| 3 | 3D alarm markers by priority, and the consequence chain in 3D | Nobody we found; Ignition has no native 3D [I4] | High | M | CF-17, CF-19 |
| 4 | Blind sensor shown as unknown in 3D (hide the level we don't have) | Texas City lesson [X4] | High | S | CF-18 |
| 5 | Shelving with a reason and a timer that expires | Ignition [I7], Experion [H1] | Medium | S (with CF-7) | CF-7, CF-1 |
| 6 | Lock write actions behind an operator PIN | Every platform has users and roles [I5] [A3] [C1] | Medium (judges ask) | S | CF-15, CF-5 |
| 7 | Audit log of every proposal, confirm, ack, and shelve | Ignition [I5] | Medium | S | CF-10 |
| 8 | Past plus future on one trend | PI System history [A5]; nobody joins it to a forecast | Medium | M | CF-3, CF-14 |
| 9 | Structured handoff sections | Honeywell and Engen [H4] | Medium | S | CF-4 |
| 10 | On-call escalation for an unacked P1 | Ignition rosters [I1], eLynx escalations [L2] | Medium | M | CF-9 |
| 11 | Alarm rate meter with the EEMUA flood threshold | Emerson and EEMUA metrics [X1] [X2] | Low | S | CF-2 |
| 12 | Every live number shows its age | eLynx red values [L4]; #56 covers stale | Low | S | CF-6 |
| 13 | ISA-101 display levels mapped to 3D zoom | ISA-101 four levels [X7] | Low | S | CF-20 |
| 14 | Alarm setpoints asked for at ingest | Rationalization projects [H2] [X5] | Low | S | CF-21 |
| 15 | Real SWD capacity limit from permit pressure | RRC daily pressure rules [X9], rising Permian pressure [B2] | Medium if real numbers exist | S | CF-13 |
| 16 | Daily SWD report in the RRC parameter list | RRC [X9] [X10] | Low | M | CF-16 |

**Not worth building tonight:** a full historian, real work orders, OPC UA, a geo basemap, and user accounts. Each is a large project that competitors already do well, and none moves the demo.

## Sources

**EOG**
- [E1] EOG Fact Sheet, March 2025 (continuous leak detection with alerts to a control center, iSense sensors, more than 99% reuse or nonfresh water in the Delaware Basin in 2024, proprietary IT, real-time decisions): https://eogresources.com/documents/EOG_Fact_Sheet_2025.pdf
- [E2] EOG Sustainability page (iSense continuous leak detection coverage 99% in the Delaware Basin, measured at central tank batteries): https://www.eogresources.com/sustainability/
- [E3] EOG employer profile (100+ in-house apps, "virtual control room", data warehouses and predictive analytics): https://employer.circaworks.com/company/profile/eog-resources-inc/4a32e03d05d3445e9ab362c64eec6911
- [E4] Houston Chronicle, June 17, 2018, via PressReader (EOG's in-house "app store" and iSteer; excerpt only): https://www.pressreader.com/usa/houston-chronicle-sunday/20180617/283287458361074
- [E5] World Oil, June 2017, "Drilling advances" (65+ apps; paywalled, search summary only): https://www.worldoil.com/magazine/2017/june-2017/columns/drilling-advances
- [E6] EOG Control Room Operator I postings on job boards (monitor facilities and well sites through SCADA, manage incoming alarms; search summaries): https://apllogistics.dejobs.org/midland-tx/control-room-operator-i/5DF599CD04554AC39019648059913B13/job/ and https://app.energyhire.com/job-board/control-room-operator-i-san-antonio-tx-48ac7611-099f-4596-bbee-6e9dc0c1bde3
- [E7] EOG Automation Tech I posting (PLC and EFM logic for valves, pumps, and alarm monitoring; search summary): https://talents.vaia.com/companies/eog-resources/automation-tech-i-38378424/
- [E8] HackUTD 2025 EOG API (Swagger: `/api/Data`, `/api/Tickets`, `/api/Information/cauldrons`): https://hackutd2025.eog.systems/swagger/index.html
- [E9] EOG SCADA Administrator, Corpus Christi, contract, expired (administers multiple CygNet SCADA systems; 2 years of CygNet or Ignition, CygNet preferred): https://www.career.com/job/eog-resources/scada-administrator/j202406121259595462569
- [E10] Brew67, a HackUTD 2025 EOG challenge entry (compared cauldron levels with transport tickets): https://devpost.com/software/brew67
- [E11] Oil and Gas 360, EOG sells saltwater disposal wells in New Mexico (23 SWDs, about 300 miles of pipeline, long-term disposal contract): https://www.oilandgas360.com/eog-resources-sells-saltwater-disposal-wells-in-new-mexico/

**Ignition (Inductive Automation)**
- [I1] Alarm Notification module docs, 8.3 (pipelines, on-call rosters, SMS with an ack link, voice ack): https://docs.inductiveautomation.com/docs/8.3/ignition-modules/alarm-notification
- [I2] Perspective module (native iOS and Android app): https://inductiveautomation.com/ignition/modules/perspective
- [I3] High performance HMI techniques, 8.3 docs (gray screens, Alarm Indicator shows color only for a problem): https://docs.inductiveautomation.com/docs/8.3/ignition-modules/vision/common-tasks-in-vision/high-performance-hmitechniques
- [I4] Inductive Automation forum, 3D in Perspective (no native 3D, embed a web viewer in an iframe): https://forum.inductiveautomation.com/t/support-for-3d-model-or-panoramic-image-view-in-ignition-perspective/107506
- [I5] Auditing actions reference, 8.3 docs: https://www.docs.inductiveautomation.com/docs/8.3/platform/audit-log-and-profiles/auditing-action-reference
- [I6] `system.alarm.shelve` docs (shelve for a set number of minutes, 0 unshelves): https://docs.inductiveautomation.com/docs/8.1/appendix/scripting-functions/system-alarm/system-alarm-shelve
- [I7] Alarm status shelving, 7.9 docs (5 minutes to 4 hours, returns when the time is up): https://docs.inductiveautomation.com/docs/7.9/alarming/alarm-status/alarm-status-shelving
- [I8] DMC, Ignition 8.3 what's new (new Historian): https://www.dmcinfo.com/blog/15670/ignition-8-3-whats-new-and-reasons-to-upgrade-your-project/
- [I9] Industrial Monitor Direct, Ignition technical reference (MQTT modules, OPC UA; third-party blog): https://industrialmonitordirect.com/blogs/knowledgebase/ignition-scada-technical-reference-licensing-development-and-architecture

**AVEVA**
- [A1] AVEVA Unified Operations Center (KPIs per process and equipment, a "command-centric operations facility", industry templates, a credits-based subscription): https://www.aveva.com/en/products/unified-operations-center/
- [A2] Coterra well loading with AVEVA PI (event frame notifications to operators, PI Vision dashboard): https://www.aveva.com/en/perspectives/presentations/2026/well-loading-analysis---maximizing-production-with-aveva-pi/
- [A3] AVEVA, Industrial AI Assistant in CONNECT is now available (July 2025, retrieval over CONNECT data, follows user permissions): https://www.aveva.com/en/perspectives/blog/industrial-ai-assistant-in-connect-is-now-available-here-is-how-you-can-get-started/
- [A5] AVEVA, What is PI Asset Framework: https://www.aveva.com/en/perspectives/blog/easy-as-pi-asset-framework/

**Honeywell**
- [H1] Experion PKS alarm shelving configuration (training module): https://process.honeywell.com/content/dam/process/en/documents/training-docs/control-monitoring-and-safety-systems/mt/EXP-7013-MT-Experion-PKS-Alarm-Shelving-Configuration.pdf
- [H2] Automation.com, Honeywell introduces DynAMo Alarm Suite (up to 80% fewer alarms, floods and chattering, ISA 18.2, EEMUA 191): https://www.automation.com/article/honeywell-introduces-dynamo-alarm-suite
- [H3] Honeywell press release, March 2026, AI control room assistant (predictions 5 to 10 minutes before alarms in the pilot): https://www.honeywell.com/us/en/news/press-releases/2026/03/honeywell-unveils-commercial-launch-of-ai-powered-control-room-assistant-following-successful-pilot
- [H4] ASM Consortium and Engen Oil, structured shift handover (2011): https://process.honeywell.com/content/dam/process/en/documents/document-lists/doc_asm-consortium/presentations/June%2030%202011%20-%20Structured%20Approach%20to%20Shift%20Handover%20Improves%20Situation%20Awareness%20at%20Engen%20Oil.pdf
- [H5] Experion alarming product note (shelving and dynamic suppression, 2012): https://ges.yodify.com/Documentation/Document/bz2xVZ/Experion-Alarming-PIN.pdf

**Cognite**
- [C1] Cognite, Introducing Cognite Atlas AI (agent workbench, templates, admin control): https://www.cognite.com/en/press-release/introducing-cognite-atlas
- [C2] Cognite, Extensible AI agents (Industrial Canvas with time series, work orders, documents, 3D; root cause analysis): https://cognite.com/en/resources/blog/streamline-industrial-workflows-and-collaboration-with-extensible-ai-agents

**eLynx, WellAware, Emerson, Kelvin**
- [L1] eLynx mobile app (tank level entry, field values): https://www.elynxtech.com/solutions/mobile
- [L2] eLynx cry-out alarms (text, email, voice callouts, groups by route, escalations, alarm dashboard, voice-to-text notes, hardware pricing): https://elynxtech.com/platform/alarm-cry-outs
- [L3] eLynx support, acknowledging alarms (web, mobile, text, voice, email): https://support.elynxtech.com/hc/en-us/articles/4405517927067-Acknowledging-Alarms
- [L4] eLynx water SCADA (tabular, map, and tank views, values in alarm in red): https://water.elynxtech.com/solutions/water-utility-scada
- [L5] eLynx, Minimize alarm fatigue with 6 steps ("over 50 alarm notifications in just one day", aggregate symptoms of a root cause): https://elynxtech.com/post/minimize-alarm-fatigue-with-6-steps
- [W1] WellAware tank monitoring on AWS Marketplace ($420 per tank, hazardous area rated): https://aws.amazon.com/marketplace/pp/prodview-cbiscubymcub4
- [W2] SitePro on WellAware (remote control of well sites, tank levels from a phone; secondary): https://www.sitepro.com/?p=1453
- [Z1] Emerson Zedi tank monitoring brochure (alarms, mobile app): https://s1-live.emerson.com/documents/automation/brochure-tank-monitoring-control-solutions-en-7199000.pdf
- [K1] Kelvin and AWS (closed-loop control, about 800 plunger lift wells): https://kelvin.ai/partners/aws

**Produced water**
- [S1] Select Water AquaView (24/7 remote operations center, SCADA oversight, mobile app, alerts): https://www.selectwater.com/technology/aquaview/
- [B1] B3 Insight launches Permian produced water forecasting tool (InjectIQ): https://www.watertechonline.com/produced-water/news/55368403/b3-insight-launches-permian-basin-produced-water-forecasting-tool
- [B2] JPT, The growing pressures of produced water disposal: https://jpt.spe.org/the-growing-pressures-of-produced-water-disposal
- [WB1] WaterBridge Resources Control Room Operator posting, removed June 2025 (remote control of multiple SWD facilities and pipelines, manages active alarms): https://builtin.com/job/control-room-operator-range-i-iv/4702682
- [AR1] Aris Water Control Room Operator, Carlsbad NM (central dispatch, real-time dashboards, SCADA; search summary): https://app.energyhire.com/job-board/control-room-operator-carlsbad-nm-1b890929-64e6-4bba-a27e-729f04ed6746

**Standards, incidents, regulation**
- [X1] Emerson, Alarm management by numbers (1 to 2 alarms per 10 minutes target, under 1% of 10-minute periods above 10 alarms): https://d1-live.emerson.com/documents/automation/article-alarm-management-by-numbers-deltav-en-38292.pdf
- [X2] IChemE, process safety metrics for alarm rationalisation (EEMUA 191 flood: more than 10 new alarms in 10 minutes): https://www.icheme.org/media/17392/lc-0151_21-lead-process-safety-metrics-alarm-rationalisation-final.pdf
- [X3] UK HSE, Texaco Milford Haven 1994 case study (excessive alarms reduced operator response; graphics lacked overviews): https://www.hse.gov.uk/comah/sragtech/casetexaco94.htm
- [X4] US CSB, preliminary findings on BP Texas City (misled by a malfunctioning level indicator, high-level alarm failed): https://www.csb.gov/csb-issues-preliminary-findings-in-bp-texas-city-refinery-accident-investigators-present-data-in-public-meeting/
- [X5] Control Engineering, ASM Consortium releases alarm management guidelines: https://www.controleng.com/asm-consortium-releases-alarm-management-guidelines/
- [X6] Automation.com, ASM publishes 2nd edition of HMI design guidelines: https://www.automation.com/en-us/articles/2013-2/asm-publishes-2nd-edition-of-hmi-design-guidelines
- [X7] Tatsoft, ISA-101 HMI compliance how-to (Level 1 overview to Level 4 support, grayscale by default, color only for abnormal; vendor guide): https://docs.tatsoft.com/display/FX/ISA-101+HMI+Compliance+How-to
- [X9] Texas RRC, Notice to Operators, Dec 19, 2023 (new and amended Permian SWD permits: daily max and average surface injection pressure, volume, and rate, reported monthly): https://www.rrc.texas.gov/announcements/121923-nto-disposal-well-monitoring-and-reporting-requirements-in-the-permian-basin
- [X10] Texas RRC, Form H-10 instructions (monthly average and maximum injection pressure): https://rrc.texas.gov/media/ellhjemv/h-10ins.pdf
