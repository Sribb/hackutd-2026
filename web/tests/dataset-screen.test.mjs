// The page's own leak and worn-pump screen (src/dataset/screen.js), on synthetic readings
// shaped like the sponsor data: 5-minute grid, noise, pump on/off cycles.
import assert from "node:assert/strict";
import { makeStore } from "../src/dataset/data.js";
import { screenDataset, screenAt, faultRuns, scan, LEAK, screenSeries, plainAlarmAt, PLAIN } from "../src/dataset/screen.js";

const STEP = 300, N = 3 * 288; // three days
const t0 = 1_790_000_000 - (1_790_000_000 % STEP);
let seed = 7;
const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
const noise = (sd) => { let s = 0; for (let i = 0; i < 6; i++) s += rnd(); return (s - 3) * sd * Math.SQRT2; };

// One system, one facility. pump(i) -> status; leakPct(i) and wearPsi(i) shape the problem.
function dataset({ pump = () => 1, leakPct = () => 0, wearPsi = () => 0 }) {
  const times = new Float64Array(N);
  const cols = { st: new Float64Array(N), fl: new Float64Array(N), rc: new Float64Array(N), ps: new Float64Array(N) };
  for (let i = 0; i < N; i++) {
    times[i] = t0 + i * STEP;
    const st = pump(i);
    const run = st === 1;
    const wear = wearPsi(i);
    const flow = run ? 2700 * (1 + wear / 500) + noise(15) : 0;
    cols.st[i] = st;
    cols.fl[i] = flow;
    cols.rc[i] = run ? flow * (1 - leakPct(i) / 100) + noise(15) : 0;
    cols.ps[i] = run ? 1000 + 150 + wear + noise(3) : 1000 + noise(3);
  }
  const series = new Map([["P|status", cols.st], ["P|flow_bpd", cols.fl], ["L|receive_flow_bpd", cols.rc], ["P|discharge_psi", cols.ps]]);
  const network = { systems: [{ id: "S1", disposal_site: { id: "D", pressure_psi: 1000 }, facilities: [{ id: "F", tank: { id: "T" }, pump: { id: "P" }, pipeline: { id: "L" } }] }] };
  return { network, store: makeStore(times, series) };
}

// Pure noise, with the pump cycling: nothing found.
{
  const { network, store } = dataset({ pump: (i) => (Math.floor(i / 60) % 3 === 2 ? 0 : 1) });
  assert.deepEqual(screenDataset(network, store), []);
}

// A leak: a 1.8% step at reading 300 that grows 0.35%/h, fixed at reading 600.
{
  const on = 300, off = 600;
  const { network, store } = dataset({ leakPct: (i) => (i >= on && i < off ? 1.8 + 0.35 * ((i - on) / 12) : 0) });
  const ev = screenDataset(network, store);
  assert.equal(ev.length, 1);
  const e = ev[0];
  assert.equal(e.category, "pipeline_leak");
  assert.equal(e.asset, "L");
  assert.ok(Math.abs(e.onset - (t0 + on * STEP)) <= 30 * 60, `leak onset within 30 min (off by ${(e.onset - t0) / STEP - on} readings)`);
  assert.ok(e.seen - e.onset <= 2 * 3600, "seen within 2 h");
  assert.ok(Math.abs(e.end - (t0 + off * STEP)) <= 15 * 60, "end within 15 min");
  // Causal: before it was seen, time travel shows nothing; between seen and end, ongoing.
  assert.equal(screenAt(ev, e.seen - STEP).length, 0);
  const mid = screenAt(ev, e.seen + 3600);
  assert.equal(mid[0].status, "ongoing");
  assert.equal(mid[0].end, null);
  assert.equal(screenAt(ev, store.now)[0].status, "resolved");
}

// A leak that opens while the pump is off shows at the first reading after, and says so.
{
  const on = 300;
  const pump = (i) => (i >= 280 && i < 320 ? 0 : 1);
  const { network, store } = dataset({ pump, leakPct: (i) => (i >= on ? 1.8 + 0.35 * ((i - on) / 12) : 0) });
  const [e] = screenDataset(network, store);
  assert.equal(e.onset, t0 + 320 * STEP);
  assert.equal(e.afterGap, true);
  assert.equal(e.from, t0 + 279 * STEP);
  assert.equal(e.status ?? null, null); // screenDataset gives raw events; screenAt adds status
  assert.equal(screenAt([e], store.now)[0].status, "ongoing");
}

// A worn pump: discharge sags 1.5 psi an hour from reading 200, then the pump fails at 700.
{
  const on = 200, fail = 700;
  const { network, store } = dataset({
    pump: (i) => (i >= fail ? 2 : 1),
    wearPsi: (i) => (i >= on && i < fail ? -1.5 * ((i - on) / 12) : 0),
  });
  const ev = screenDataset(network, store);
  assert.equal(ev.length, 1);
  const e = ev[0];
  assert.equal(e.category, "pump_degradation");
  assert.ok(Math.abs(e.onset - (t0 + on * STEP)) <= 60 * 60, `wear onset within 1 h (off by ${(e.onset - t0) / STEP - on} readings)`);
  assert.equal(e.end, t0 + fail * STEP, "a pump failure ends the wear event");
  assert.equal(e.endedByFault, true);
  assert.deepEqual(faultRuns(store, "P").map((r) => [r.onset, r.end]), [[t0 + fail * STEP, null]]);
}

// The scan never reports a step smaller than the floor: a slow drift with no step is not a leak.
{
  const x = [];
  for (let i = 0; i < 400; i++) x.push([t0 + i * STEP, i > 100 ? (i - 100) * 0.01 : 0]);
  const ev = scan(x, { enter: (m) => m > LEAK.enter, leave: (m) => m < LEAK.leave, step: true, stepMin: LEAK.stepMin });
  assert.equal(ev.length, 1);
  assert.ok(ev[0].onset > t0 + 100 * STEP, "onset is never placed before the drift began");
}

// Review fixes.
// A second leak on the same line gets its own onset, not one dragged into the first.
{
  const leak = (i) => (i >= 120 && i < 240 ? 1.8 + 0.35 * ((i - 120) / 12) : i >= 360 ? 1.8 + 0.35 * ((i - 360) / 12) : 0);
  const { network, store } = dataset({ leakPct: leak });
  const ev = screenDataset(network, store);
  assert.equal(ev.length, 2);
  assert.ok(Math.abs(ev[1].onset - (t0 + 360 * STEP)) <= 30 * 60, `second leak onset near reading 360 (got ${(ev[1].onset - t0) / STEP})`);
}
// A one-reading fault glitch before the wear was seen does not end it.
{
  const { network, store } = dataset({
    pump: (i) => (i === 250 ? 2 : 1), // the wear shows at about reading 268
    wearPsi: (i) => (i >= 200 ? -1.5 * ((i - 200) / 12) : 0),
  });
  const ev = screenDataset(network, store).filter((e) => e.category === "pump_degradation");
  assert.equal(ev.length, 1);
  assert.ok(ev[0].end === null || ev[0].end > ev[0].seen, "never ends before it is seen");
}
// A fault run followed by missing readings ends at the next real reading; one across a
// gap with a fault on both sides is one run.
{
  const times = new Float64Array([0, 300, 600, 900, 1200, 1500, 1800]);
  const runs = (vals) => faultRuns(makeStore(times, new Map([["P|status", new Float64Array(vals)]])), "P").map((r) => [r.onset, r.end]);
  assert.deepEqual(runs([1, 2, 2, NaN, 1, 1, 1]), [[300, 1200]]);
  assert.deepEqual(runs([1, 2, NaN, 2, 1, 1, 1]), [[300, 1200]]);
  assert.deepEqual(runs([1, 2, 2, NaN, NaN, NaN, NaN]), [[300, null]]);
}

// How early: the screen sees a leak hours before a fixed 5% alarm on the same signal, and
// the fixed alarm is judged only up to the moment asked.
{
  const on = 300;
  const { network, store } = dataset({ leakPct: (i) => (i >= on ? 1.8 + 0.35 * ((i - on) / 12) : 0) });
  const [e] = screenDataset(network, store);
  const f = network.systems[0].facilities[0];
  const lost = screenSeries(store, f, 1000).lost;
  const plain = plainAlarmAt(lost, e.onset, store.now, PLAIN.leak);
  assert.ok(plain - e.seen > 6 * 3600, `fixed alarm ${(plain - e.seen) / 3600} h after the screen`);
  assert.equal(plainAlarmAt(lost, e.onset, plain - STEP, PLAIN.leak), null, "not before it happens");
}
console.log("dataset-screen: all pass");
