// gaia as an agent (#171): the offline walk-through, the step labels and the spoken words.
import assert from "node:assert/strict";
import { walkthrough, WALK } from "../src/agent/playbook.js";
import { stepLabel } from "../src/agent/steps.js";
import { spoken } from "../src/agent/voice.js";
import { normalize } from "../src/console/intents.js";

const NICE = { PUMP_3: "Pump 3", TANK_3: "Tank 3", TANK_5: "Tank 5" };
const base = {
  nice: (id) => NICE[id] || id,
  hasId: (id) => id in NICE,
  tankOf: (id) => (id === "PUMP_3" ? "TANK_3" : null),
  selected: () => null,
  unknown: () => [],
  alerts: () => [{ id: "A1", asset_id: "PUMP_3", kind: "PUMP_TRIP", priority: "P1", severity: "ALARM", clock: "1:52 AM", consequence_asset_id: "TANK_3" },
    { id: "A2", asset_id: "TANK_3", kind: "TANK_HIGH", priority: "P2", severity: "WARNING", root_id: "A1" }],
  brief: () => ({ source: "fallback", from: "forecast", countdown: { asset_id: "TANK_3", at: 167 }, plans: { robust: { total_usd: 1200, actions: [] } } }),
};

for (const q of ["walk me through it", "walk me through what's wrong", "step by step", "show me what's wrong"]) assert.ok(WALK.test(normalize(q).trim()), q);
assert.ok(!WALK.test(normalize("show tank 3").trim()));

const w = walkthrough(base);
assert.deepEqual(w.steps.map((s) => s.action), ["focus", "trace_pipes", "focus", "set_time", "chart", "open_panel"]);
assert.equal(w.steps[4].text, "plot tank 3 level");
assert.equal(w.steps[0].asset_id, "PUMP_3");
assert.equal(w.steps[2].asset_id, "TANK_3");
assert.deepEqual(w.steps[3].args, { clock: "2:47 AM" });
assert.deepEqual(w.steps.map((s) => s.step), ["flying to pump 3", "tracing pump 3's pipes", "flying to tank 3", "jumping to 2:47 am", "drawing the level", "opening the plans"]);
assert.equal(w.reply, "Pump 3 tripped (live alert P1, raised 1:52 am). The shift forecast (every 30 s) says tank 3 reaches shutdown at 2:47 am; the best plan is $1,200, and nothing moves until a person holds to approve it.");
assert.ok(!/\bsafe\b/.test(w.reply), "never 'safe' from a plan");
assert.ok(![...w.reply].some((c) => c.charCodeAt(0) === 0x2013 || c.charCodeAt(0) === 0x2014));

// A blind tank is said first and never called safe.
const blind = walkthrough({ ...base, unknown: () => ["TANK_5"] });
assert.match(blind.reply, /^No fresh reading from Tank 5, so I can't call it safe\./);
assert.ok(!/\bsafe\b(?! )/.test(blind.reply.replace("can't call it safe", "")));

// Nothing open: no steps that pretend, and no all-clear.
const quiet = walkthrough({ ...base, alerts: () => [], brief: () => ({ plans: {} }) });
assert.deepEqual(quiet.steps, []);
assert.equal(quiet.reply, "No open alarm to walk through.");

// A dataset (#153): the recording's UTC clock, the alert's own words for an unknown kind, no
// jump to a shutdown that has passed, and a plan that can't save a tank says so.
const DS = { S4_PUMP_A: "S4 Pump A", S4_TANK_A: "S4 Tank A", S2_PIPELINE_C: "S2 Pipeline C", S4_TANK_B: "S4 Tank B" };
const utc = (m) => `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")} utc`;
const ds = {
  ...base, nice: (id) => DS[id] || id, hasId: (id) => id in DS, tankOf: () => null, dataset: () => true, clock: utc, now: () => 540,
  alerts: () => [{ id: "EV-21", asset_id: "S4_PUMP_A", kind: "PUMP_FAILURE", priority: "P2", severity: "ALARM", clock: "08:00", consequence_asset_id: "S4_TANK_A" }],
  brief: () => ({ source: "fallback", from: "alerts", countdown: { asset_id: "S4_TANK_A", at: 717 }, do_nothing_usd: 12028, plans: { robust: { total_usd: 905, cannot_save: ["S4_TANK_B"] } } }),
};
const d1 = walkthrough(ds);
assert.deepEqual(d1.steps.map((s) => s.step), ["flying to s4 pump a", "tracing s4 pump a's pipes", "flying to s4 tank a", "jumping to 11:57 utc", "drawing the level", "opening the plans"]);
assert.equal(d1.steps[4].text, "plot s4 tank a level");
assert.equal(d1.reply, "S4 Pump A failed (alert P2, raised 08:00 utc). The dataset's forecast at 09:00 utc says s4 tank a reaches shutdown at 11:57 utc; the best plan is $905 against $12,028 if nothing changes and can't save S4 Tank B, and nothing moves until a person holds to approve it.");
assert.ok(!/\blive\b/.test(d1.reply), "a recording is never called live");
const leak = walkthrough({ ...ds, alerts: () => [{ id: "EV-17", asset_id: "S2_PIPELINE_C", kind: "PIPELINE_LEAK", priority: "P3", clock: "10:00", message: "Leak on S2_PIPELINE_C: send an inspector now." }], brief: () => ({ plans: {} }) });
assert.equal(leak.reply, "Leak on S2_PIPELINE_C: send an inspector now (alert P3, raised 10:00 utc).");
const past = walkthrough({ ...ds, now: () => 730 });
assert.ok(!past.steps.some((s) => s.action === "set_time"), "no jump to a shutdown that has passed");
assert.match(past.reply, /s4 tank a is at shutdown now/);

// Labels: the backend's own step wins; else ours, lowercase.
assert.equal(stepLabel({ action: "focus", asset_id: "PUMP_3", step: "Flying To Pump 3" }), "flying to pump 3");
assert.equal(stepLabel({ action: "focus", asset_id: "TANK_3" }, base.nice), "flying to tank 3");
assert.equal(stepLabel({ action: "set_time", args: { clock: "2:47 AM" } }), "jumping to 2:47 am");
assert.equal(stepLabel({ action: "open_panel", args: { panel: "plans" } }), "opening the plans");
assert.equal(stepLabel({ action: "trace_pipes", asset_id: "PUMP_3" }, base.nice), "tracing pipes from pump 3");

// Spoken: two sentences, IDs read as names, dollars said.
assert.equal(spoken("TANK_3 is at 71%. Shutdown at 2:47 AM. Third sentence."), "tank 3 is at 71%. Shutdown at 2:47 AM.");
assert.equal(spoken("Plan costs $1,200."), "Plan costs 1,200 dollars.");
assert.equal(spoken(""), "");
console.log("agent: all checks pass");
