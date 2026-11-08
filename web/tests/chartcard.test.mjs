// gaia chart cards (#159): the chart intent, the one-sentence explanation, and the SVG's
// gaps (src/console/chartcard.js, through intents.match). Run: npm test
import assert from "node:assert/strict";
import { match, normalize } from "../src/console/intents.js";
import { chartIntent, explain, chartSvg, linePath, slopePerHour, clockText, canPlot } from "../src/console/chartcard.js";

const IDS = ["TANK_1", "TANK_2", "TANK_3", "TANK_4", "TANK_5", "TANK_6", "PUMP_3", "PUMP_4", "WELL_9", "VALVE_XO_34", "VALVE_D_3", "SWD_1", "TRUCK_1"];
const ctx = (sel = null) => ({ hasId: (id) => IDS.includes(id), ids: () => IDS, selected: () => sel, opened: () => false });
const ask = (text, sel) => match(text, ctx(sel));
const chart = (text, sel) => { const m = ask(text, sel); assert.ok(m && m.chart, `"${text}" should be a chart, got ${JSON.stringify(m)}`); return m.chart; };
const ids = (c) => c.items.map((i) => i.id);

// ------------------------------------------------------------ the intent ----
{
  const c = chart("plot tank 3 level");
  assert.equal(c.source, "live");
  assert.deepEqual(ids(c), ["TANK_3"]);
  assert.equal(c.items[0].metric, "level_pct");
  assert.equal(c.items[0].unit, "% full");
  assert.equal(c.forecast, true, "a tank chart shows the forecast with the past");
  assert.equal(c.past, null);
  assert.equal(c.ahead, null);
}
assert.deepEqual(ids(chart("chart tank 3")), ["TANK_3"]);
assert.deepEqual(ids(chart("Chart tank three.")), ["TANK_3"], "number words and punctuation");
{
  const c = chart("graph pump 3 flow");
  assert.deepEqual(ids(c), ["PUMP_3"]);
  assert.equal(c.items[0].metric, "flow_bbl_hr");
  assert.equal(c.items[0].unit, "bbl/hr");
  assert.equal(c.forecast, false, "the forecast is tanks only");
}
{
  const c = chart("show tank 3 level for the next 6 hours");
  assert.deepEqual(ids(c), ["TANK_3"]);
  assert.equal(c.ahead, 360);
  assert.equal(c.forecast, true);
}
assert.equal(chart("show me tank 3 level over the next hour").ahead, 60);
assert.equal(chart("plot tank 3 for the next 12 hours").ahead, 360, "capped at the forecast's 6 h horizon");
{
  const c = chart("compare tank 3 and tank 4");
  assert.deepEqual(ids(c), ["TANK_3", "TANK_4"]);
  assert.equal(c.forecast, true);
}
assert.deepEqual(ids(chart("compare tanks 3 and 4")), ["TANK_3", "TANK_4"]);
{
  const c = chart("compare tank 1, tank 2, tank 3, tank 4, tank 5 and tank 6");
  assert.equal(c.items.length, 4, "at most four lines");
  assert.equal(c.cut, 2);
}
{
  const c = chart("S4 pump C discharge pressure since midnight");
  assert.equal(c.source, "dataset");
  assert.deepEqual(ids(c), ["S4_PUMP_C"]);
  assert.equal(c.items[0].metric, "discharge_psi");
  assert.equal(c.items[0].unit, "psi");
  assert.equal(c.since, 0);
  assert.equal(c.sinceText, "midnight");
}
assert.equal(chart("plot S2_TANK_B").items[0].metric, "level_pct", "sponsor ids as written");
assert.equal(chart("chart s1 well a1").items[0].id, "S1_WELL_A1");
assert.equal(chart("graph s4 pipeline c flow over the last 2 hours").items[0].metric, "receive_flow_bpd");
assert.equal(chart("graph s4 pipeline c flow over the last 2 hours").past, 120);
assert.equal(chart("plot pump 3 flow for the last 30 minutes").past, 30);
{
  const c = chart("plot tank 3 since 1 am");
  assert.equal(c.since, 60);
  assert.equal(c.forecast, false, "a past window is the past only");
}
assert.deepEqual(ids(chart("plot it", "TANK_5")), ["TANK_5"], "the selection");
assert.deepEqual(ids(chart("graph this", "PUMP_4")), ["PUMP_4"]);

// Unknown asset or metric: said plainly, with what can be plotted. Never a guess.
{
  const e = chart("plot tank 9");
  assert.match(e.error, /no tank 9 on this field/i);
  assert.match(e.error, /I can plot tank 1 to 6 \(level and forecast\)/);
}
assert.match(chart("plot tank 3 pressure").error, /tank 3 has no pressure reading on this field\. I can plot its level \(% full\)/);
assert.match(chart("chart the crossover").error, /not a number over time/);
assert.match(chart("plot something").error, /Name what to plot/);
assert.match(chart("compare tank 3 and pump 3").error, /can't share one axis/);
assert.match(chart("plot pump 3 flow for the next 2 hours").error, /forecast covers tank levels only/);
assert.match(chart("S4 pump C pressure for the next hour").error, /no forecast line/);
assert.match(chart("plot pump 3 status").error, /Status is a code/);
assert.match(chart("plot S4 tank A pressure").error, /S4_TANK_A has no pressure reading in the sponsor data\. I can plot its level/);
assert.match(chart("plot S4 pump C temperature").error, /S4_PUMP_C has no temperature reading in the sponsor data\. I can plot its flow \(bbl\/day\) or pressure \(psi\)/, "never swaps in another reading");
assert.match(chart("S4 pump C vibration since midnight").error, /no vibration reading/);
assert.match(chart("graph pump 3 temperature").error, /pump 3 has no temperature reading on this field\. I can plot its flow/);
{
  const ds = { hasId: (id) => ["S2_TANK_A", "S2_PUMP_A"].includes(id), ids: () => ["S2_TANK_A", "S2_PUMP_A"], selected: () => null };
  assert.match(chartIntent(normalize("plot tank 3"), ds).error, /I can plot the sponsor tanks.*"plot S2 tank A level since midnight"/, "dataset mode suggests sponsor ids");
  assert.equal(chartIntent(normalize("plot S2 tank A"), ds).items[0].id, "S2_TANK_A");
}
assert.match(canPlot(ctx()), /pump 3 to 4 \(flow\)/);

// Not charts: these keep their old routes.
assert.equal(ask("compare the plans"), null, "a compare with no asset goes to the backend");
assert.equal(ask("what is tank 3 level"), null, "a question in words goes to the backend");
assert.equal(ask("when does tank 3 hit shutdown"), null);
assert.deepEqual(ask("show tank 3"), { action: "focus", asset_id: "TANK_3", ask: true }, "show without a metric still flies there");
assert.deepEqual(ask("open pump 3"), { action: "explode", asset_id: "PUMP_3" });
assert.equal(ask("open the crossover"), null, "equipment stays route R");
assert.deepEqual(ask("in 2 hours"), { action: "set_time", args: { t_min: 120 } });
assert.equal(ask("trace pipes from tank 3").action, "trace_pipes");
assert.equal(chartIntent(normalize("hello there"), ctx()), null);

// ------------------------------------------------------- the sentence ----
const ramp = (from, to, a, b, step = 2) => { const out = []; for (let x = from; x <= to; x += step) out.push({ x, v: a + ((b - a) * (x - from)) / (to - from) }); return out; };
const tankModel = (over = {}) => ({
  unit: "% full", x0: -60, x1: 120, nowMin: 107, step: 1, fcShown: true, windowText: "over the last hour",
  refs: [{ y: 90, label: "shutdown 90%", tone: "alarm", name: "shutdown" }],
  series: [{ id: "TANK_3", label: "tank 3", metric: "level_pct", past: ramp(-60, 0, 62, 71, 1), fc: ramp(0, 120, 71, 89 + 9, 2), band: null, cross: { x: 60, low: 45, high: 75 }, staleMin: null }],
  ...over,
});
assert.equal(clockText(107 + 60), "2:47 am");
assert.equal(Math.round(slopePerHour(ramp(0, 60, 71, 80))), 9);
{
  const m = tankModel();
  m.series[0].fc = ramp(0, 120, 71, 89, 2); // 9 points an hour
  m.series[0].cross = { x: 60, low: null, high: null };
  assert.equal(explain(m), "Tank 3 rises about 9 points an hour and crosses shutdown at 2:47 am if nobody acts.");
}
{
  const m = tankModel();
  m.series[0].fc = ramp(0, 120, 71, 89, 2);
  assert.equal(explain(m), "Tank 3 rises about 9 points an hour and crosses shutdown at 2:47 am (give or take: 2:32 am to 3:02 am) if nobody acts.");
}
{
  const m = tankModel();
  m.series[0].fc = ramp(0, 120, 50, 54, 2);
  m.series[0].cross = null;
  assert.equal(explain(m), "Tank 3 rises about 2 points an hour, to about 54% by 3:47 am, and stays under shutdown in that forecast.");
}
{
  // Past the crossing: the forecast sits on the shutdown line with no event ahead. Never "under".
  const m = tankModel();
  m.series[0].fc = ramp(0, 120, 90.1, 90.1, 2);
  m.series[0].cross = null;
  assert.equal(explain(m), "Tank 3 is at shutdown now, about 90.1% against the 90% line, and the forecast keeps it there through 3:47 am if nobody acts.");
  assert.doesNotMatch(explain(m), /under/);
  m.series.push({ id: "TANK_4", label: "tank 4", metric: "level_pct", past: [], fc: ramp(0, 120, 40, 44), band: null, cross: null, staleMin: null });
  assert.equal(explain(m), "Tank 3 is at shutdown now, and tank 4 stays under it through 3:47 am.");
}
{
  const m = tankModel();
  m.series[0].fc = ramp(0, 120, 80, 95, 2);
  m.series[0].cross = null; // no event in the forecast, but the line still reaches shutdown
  assert.match(explain(m), /reaches shutdown about 3:07 am if nobody acts\.$/);
}
{
  const m = tankModel();
  m.series[0].staleMin = 7;
  m.series[0].cross = null;
  assert.match(explain(m), /no fresh reading for 7 min.*unknown, not safe/, "a stale tank is never called safe");
}
{
  const m = tankModel();
  m.series.push({ id: "TANK_4", label: "tank 4", metric: "level_pct", past: [], fc: ramp(0, 120, 40, 44), band: null, cross: null, staleMin: null });
  assert.equal(explain(m), "Tank 3 crosses shutdown first, at 2:47 am, if nobody acts, and tank 4 stays under it through 3:47 am.");
}
const pumpModel = (past) => ({ unit: "bbl/hr", x0: -60, x1: 0, nowMin: 120, step: 1, fcShown: false, windowText: "over the last hour", refs: [],
  series: [{ id: "PUMP_3", label: "pump 3", metric: "flow_bbl_hr", past, fc: null, band: null, cross: null, staleMin: null }] });
assert.equal(explain(pumpModel([...ramp(-60, -14, 150, 150, 1), ...ramp(-13, 0, 0, 0, 1)])), "Pump 3 flow fell from 150 to 0 bbl/hr over the last hour.");
assert.equal(explain(pumpModel(ramp(-60, 0, 150, 150.4, 1))), "Pump 3 flow held near 150 bbl/hr over the last hour.");
{
  const past = [...ramp(-60, -20, 150, 150, 1), ...ramp(-19, 0, 0, 0, 1).map((p) => ({ x: p.x, v: null }))];
  assert.equal(explain(pumpModel(past)), "Pump 3 flow held near 150 bbl/hr over the last hour, with no reading for the last 20 min, so treat it as unknown, not safe.");
}
assert.match(explain(pumpModel(ramp(-60, 0, 0, 0, 1).map((p) => ({ x: p.x, v: null })))), /has no readings over the last hour, so there is no line; treat it as unknown, not safe/);
{
  const m = { unit: "psi", x0: -930, x1: 0, nowMin: 930, step: 5, fcShown: false, windowText: "since midnight utc", refs: [],
    series: [{ id: "S4_PUMP_C", label: "S4_PUMP_C", metric: "discharge_psi", past: ramp(-930, 0, 1140, 1162, 5), fc: null, band: null, cross: null, staleMin: null }] };
  assert.equal(explain(m), "S4_PUMP_C discharge pressure stayed between 1,140 and 1,162 psi since midnight utc.");
}
{
  const m = { unit: "% full", x0: -360, x1: 0, nowMin: 720, step: 5, fcShown: false, windowText: "over the last 6 hours",
    refs: [{ y: 85, label: "warning 85%", tone: "warn", name: "warning" }, { y: 92, label: "shutdown 92%", tone: "alarm", name: "shutdown" }],
    series: [{ id: "S2_TANK_B", label: "S2_TANK_B", metric: "level_pct", past: ramp(-360, 0, 30, 78.5, 5), fc: null, band: null, cross: null, staleMin: null }] };
  assert.equal(explain(m), "S2_TANK_B rose from 30% to 78.5% over the last 6 hours and is under the 85% warning line.");
}
for (const s of [explain(tankModel()), canPlot(ctx()), chart("plot tank 9").error]) assert.doesNotMatch(s, /[\u2013\u2014]/, "no em or en dashes");

// --------------------------------------------------------- the chart ----
{
  const sx = (x) => x, sy = (v) => 100 - v;
  const d = linePath([{ x: 0, v: 10 }, { x: 1, v: null }, { x: 2, v: 12 }, { x: 3, v: 13 }], sx, sy);
  assert.equal((d.match(/M/g) || []).length, 2, "a null lifts the pen");
  assert.ok(!/,100\.0/.test(d), "a missing reading is never drawn at zero");
  assert.match(d, /^M0\.0,90\.0l0\.01,0M2\.0,88\.0L3\.0,87\.0$/, "a lone reading is a dot");
}
{
  const svg = chartSvg(tankModel());
  assert.match(svg, /<path class="cc-past s1" d="M/);
  assert.match(svg, /<path class="cc-fc s1" d="M/);
  assert.match(svg, /class="cc-ref alarm"/);
  assert.match(svg, /shutdown 90%/);
  assert.match(svg, />2:47 am</, "the crossing is marked");
  assert.match(svg, />now</);
  assert.doesNotMatch(svg, /NaN|undefined/);
}
{
  const svg = chartSvg(pumpModel(ramp(-60, 0, 0, 0, 1).map((p) => ({ x: p.x, v: null }))));
  assert.match(svg, /no readings in this window/);
  assert.doesNotMatch(svg, /class="cc-past[^"]*" d="M/, "no line through missing readings");
}

console.log("chartcard.js: all checks pass");
