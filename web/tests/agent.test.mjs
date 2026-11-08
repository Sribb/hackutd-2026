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
assert.deepEqual(w.steps.map((s) => s.action), ["focus", "trace_pipes", "focus", "set_time", "open_panel"]);
assert.equal(w.steps[0].asset_id, "PUMP_3");
assert.equal(w.steps[2].asset_id, "TANK_3");
assert.deepEqual(w.steps[3].args, { clock: "2:47 AM" });
assert.deepEqual(w.steps.map((s) => s.step), ["flying to pump 3", "tracing pipes to tank 3", "flying to tank 3", "jumping to 2:47 am", "opening the plans"]);
assert.match(w.reply, /^Pump 3 tripped at 1:52 am \(live alert, P1\)\./);
assert.match(w.reply, /The shift forecast \(every 30 s\) says tank 3 reaches shutdown at 2:47 am; the cheapest safe plan is \$1,200/);
assert.match(w.reply, /holds to confirm/);
assert.ok(![...w.reply].some((c) => c.charCodeAt(0) === 0x2013 || c.charCodeAt(0) === 0x2014));

// A blind tank is said first and never called safe.
const blind = walkthrough({ ...base, unknown: () => ["TANK_5"] });
assert.match(blind.reply, /^No fresh reading from Tank 5, so I can't call it safe\./);
assert.ok(!/\bsafe\b(?! )/.test(blind.reply.replace("can't call it safe", "")));

// Nothing open: no steps that pretend, and no all-clear.
const quiet = walkthrough({ ...base, alerts: () => [], brief: () => ({ plans: {} }) });
assert.deepEqual(quiet.steps, []);
assert.equal(quiet.reply, "No open alarm to walk through.");

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
