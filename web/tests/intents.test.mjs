// Browser intents on the sponsor network (dataset mode): assetIn() reads sponsor ids such as
// S2_TANK_B and S4_PUMP_C, and only ids the field has are used. Run: npm test
import assert from "node:assert/strict";
import { match, assetIn, normalize } from "../src/console/intents.js";

const DS = ["S2_TANK_B", "S2_PUMP_B", "S4_PUMP_C", "S4_TANK_C", "S1_WELL_A1", "S1_WELL_A2", "S2_PIPELINE_B", "S2_FACILITY_B", "S2_DISPOSAL"];
const SIM = ["TANK_3", "PUMP_3", "VALVE_XO_34", "VALVE_D_3", "SWD_1"];
const ctx = (ids, sel = null) => ({ hasId: (id) => ids.includes(id), ids: () => ids, selected: () => sel, opened: () => false });
const id = (text, ids = DS, sel) => assetIn(normalize(text), ctx(ids, sel));

// Spelled out, as written, with underscores, and in any case.
assert.equal(id("show s2 tank b"), "S2_TANK_B");
assert.equal(id("show S2 Tank B"), "S2_TANK_B");
assert.equal(id("show S2_TANK_B"), "S2_TANK_B");
assert.equal(id("open s2tank b"), "S2_TANK_B");
assert.equal(id("open s4 pump c"), "S4_PUMP_C");
assert.equal(id("where is s1 well a1"), "S1_WELL_A1");
assert.equal(id("trace s2 pipeline b"), "S2_PIPELINE_B");
assert.equal(id("isolate s2 facility b"), "S2_FACILITY_B");
assert.equal(id("show s2 disposal"), "S2_DISPOSAL");
assert.equal(id("plot s4 pump c pressure"), "S4_PUMP_C");
// The team lead's three: a question in words still names the asset for the backend.
assert.equal(id("what's wrong with s4 pump c"), "S4_PUMP_C");
assert.equal(id("isolate s1 well a2"), "S1_WELL_A2");
assert.equal(id("show s2 tank b?"), "S2_TANK_B");
// Only ids the field has: no guessing.
assert.equal(id("show s9 tank z"), null, "an id the field doesn't have");
assert.equal(id("show s2 tank b", SIM), null, "a sponsor id on the sim field");
assert.equal(id("show s2 tank and pump"), null, "\"and\" is not facility letter a");
// The sim field still works, and its ids win there.
assert.equal(id("show tank 3", SIM), "TANK_3");
assert.equal(id("close valve d 3", SIM), "VALVE_D_3");
assert.equal(id("it", DS, "S2_PUMP_B"), "S2_PUMP_B", "the selection");

// Through match(): the same verbs as on the sim field.
const m = (text, sel) => match(text, ctx(DS, sel));
assert.deepEqual(m("show s2 tank b"), { action: "focus", asset_id: "S2_TANK_B", ask: true });
assert.deepEqual(m("open s4 pump c"), { action: "explode", asset_id: "S4_PUMP_C" });
assert.deepEqual(m("isolate S2_TANK_B"), { action: "isolate", asset_id: "S2_TANK_B" });
assert.deepEqual(m("isolate s1 well a2"), { action: "isolate", asset_id: "S1_WELL_A2" });
assert.equal(m("what's wrong with s4 pump c"), null, "a question goes to the backend (rules, then Claude)");
assert.deepEqual(m("show sensors on s2 tank b"), { action: "show_sensors", asset_id: "S2_TANK_B" });
assert.equal(m("trace pipes from s2 tank b").action, "trace_pipes");
// Charts on sponsor ids, by name or by the selection.
const c = m("plot s4 pump c pressure").chart;
assert.equal(c.source, "dataset");
assert.deepEqual(c.items.map((i) => [i.id, i.metric]), [["S4_PUMP_C", "discharge_psi"]]);
const s = m("chart it", "S2_TANK_B").chart;
assert.equal(s.source, "dataset", "a selected sponsor asset charts from the sponsor data");
assert.equal(s.items[0].metric, "level_pct");

console.log("intents.js sponsor ids: all checks pass");
