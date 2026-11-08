// A screen for pipeline leaks and worn pumps, from the raw readings, for when the backend's
// detection is not there. It is causal: an event is only "seen" at the reading that showed
// it, and its onset is fitted from readings up to that moment, so time travel shows what an
// operator could have known then. Pure functions, no DOM.
//
// How the problems show up (SPEC "Pressure formulas" and the labelled week):
//   leak      pump flow minus what arrives at disposal, while the pump runs. Healthy is 0
//             with about 0.9 % noise. A leak opens with a step of 1.5 to 2 % of the flow and
//             grows about 0.35 % an hour until it is fixed, when it drops straight back to 0.
//   worn pump discharge pressure minus (disposal + 150 psi), while the pump runs. Healthy is
//             0 with a few psi of noise. Wear pulls it down steadily; the repair puts it back,
//             or the pump fails (status 2), which ends the wear event (SPEC: a degrading pump
//             that later fails is two events).
// Readings are only used while the pump runs, so an onset or end inside a pump-off gap is
// placed at the first reading that shows it, and the event says so.

const STEP_S = 300;
const W = 12; // one hour of running readings
const BACK_S = 36 * 3600; // how far back the onset fit looks
export const LEAK = { enter: 1.2, leave: 0.5, stepMin: 0.8 }; // % of pump flow
// Wear is slow, so it is judged over two hours: 7 psi down over 24 running readings shows
// 2 to 6 hours sooner than 12 psi over one hour, with no false alarm in the history folders.
export const WEAR = { enter: -7, leave: -4, healthy: 150, win: 24 }; // psi against disposal + 150

const mean = (a, i, n) => { let s = 0; for (let k = i - n + 1; k <= i; k++) s += a[k][1]; return s / n; };

// Least-squares onset. step: the value jumps at t0 then ramps (leak); else a ramp from 0
// (wear). Returns the reading time that best splits "healthy" from "event".
export function fitOnset(x, iDet, step, stepMin = 0) {
  const tDet = x[iDet][0];
  let i0 = iDet;
  while (i0 > 0 && x[i0 - 1][0] >= tDet - BACK_S) i0--;
  // A step sits at a reading (it shows at once). A ramp starts from 0, so it can start
  // inside a pump-off gap: try every 5-minute step there too.
  const cands = [];
  if (step) for (let j = i0; j <= iDet - 2; j++) cands.push([x[j][0], j]);
  else for (let t = x[i0][0], j = i0; t <= x[iDet - 2][0]; t += STEP_S) { while (x[j][0] < t) j++; cands.push([t, j]); }
  let best = null;
  for (const [t0, j] of cands) {
    let pre = 0;
    for (let k = i0; k < j; k++) pre += x[k][1] ** 2;
    let a0 = 0, b;
    const n = iDet - j + 1;
    if (step) {
      let mx = 0, my = 0;
      for (let k = j; k <= iDet; k++) { mx += x[k][0] - t0; my += x[k][1]; }
      mx /= n; my /= n;
      let sxx = 0, sxy = 0;
      for (let k = j; k <= iDet; k++) { const a = x[k][0] - t0 - mx; sxx += a * a; sxy += a * (x[k][1] - my); }
      b = sxx ? sxy / sxx : 0;
      a0 = my - b * mx;
      if (a0 < stepMin) continue; // a leak opens with a step; no step means noise drifting
    } else {
      let saa = 0, sav = 0;
      for (let k = j; k <= iDet; k++) { const a = x[k][0] - t0; saa += a * a; sav += a * x[k][1]; }
      b = saa ? sav / saa : 0;
    }
    let sse = pre;
    for (let k = j; k <= iDet; k++) sse += (x[k][1] - a0 - b * (x[k][0] - t0)) ** 2;
    if (!best || sse < best.sse - 1e-9) best = { sse, t0, j };
  }
  if (!best) return { onset: x[iDet][0], afterGap: false, from: null };
  // A step right after a pump-off gap could have opened any time in the gap; say so.
  const afterGap = step && best.j > 0 && best.t0 - x[best.j - 1][0] > 2 * STEP_S;
  return { onset: best.t0, afterGap, from: afterGap ? x[best.j - 1][0] : null };
}

// x: [[ts, value]] while the pump runs, in time order. Returns
// [{ onset, seen, end, endSeen, afterGap, from, peak }] where seen/endSeen are the reading
// times that showed the start and the end (null while it goes on).
export function scan(x, { enter, leave, step, stepMin, faults = [], win = W }) {
  const out = [];
  let on = null;
  let i = win - 1;
  while (i < x.length) {
    const t = x[i][0];
    if (!on) {
      if (enter(mean(x, i, win))) {
        const f = fitOnset(x, i, step, stepMin);
        on = { onset: f.onset, seen: t, end: null, endSeen: null, afterGap: f.afterGap, from: f.from, peak: 0, atSeen: mean(x, i, win) };
      }
      i++;
      continue;
    }
    const v = x[i][1];
    if (Math.abs(v) > Math.abs(on.peak)) on.peak = v;
    const fault = faults.find((ft) => ft >= on.seen - win * STEP_S && ft <= t);
    if (fault !== undefined) {
      on.end = on.endSeen = fault;
      out.push(on); on = null; i += win; continue;
    }
    if (i >= 2 && leave(mean(x, i, 3))) {
      const j = i - 2; // the first of the three healthy readings
      on.end = x[j][0] - x[j - 1][0] <= STEP_S ? x[j - 1][0] + STEP_S : x[j][0];
      on.endAfterGap = x[j][0] - x[j - 1][0] > 2 * STEP_S;
      on.endSeen = t;
      out.push(on); on = null; i += win; continue;
    }
    i++;
  }
  if (on) {
    // The pump can fail after its last running reading: that ends the wear too.
    const fault = faults.find((ft) => ft >= on.seen - win * STEP_S);
    if (fault !== undefined) on.end = on.endSeen = fault;
    out.push(on);
  }
  return out;
}

// Running readings of one facility from the store (time order, pump status 1, flow > 0).
function running(store, f) {
  const { times, series } = store;
  const col = (a, m) => series.get(a + "|" + m);
  const st = col(f.pump.id, "status"), fl = col(f.pump.id, "flow_bpd"), rc = col(f.pipeline.id, "receive_flow_bpd"), ps = col(f.pump.id, "discharge_psi");
  const lost = [], press = [], faults = [];
  if (!st || !fl) return { lost, press, faults, flow: new Map() };
  const flow = new Map();
  for (let i = 0; i < times.length; i++) {
    if (st[i] === 2 && i > 0 && st[i - 1] !== 2) faults.push(times[i]);
    if (st[i] !== 1 || !(fl[i] > 0)) continue;
    flow.set(times[i], fl[i]);
    if (rc && Number.isFinite(rc[i])) lost.push([times[i], ((fl[i] - rc[i]) / fl[i]) * 100]);
    if (ps && Number.isFinite(ps[i])) press.push([times[i], ps[i]]);
  }
  return { lost, press, faults, flow };
}

// Every leak and worn pump in the dataset, as the page's own screen saw them.
// Shape is close to the backend's Event so alerts.js can show both.
export function screenDataset(network, store) {
  const out = [];
  for (const s of network.systems || []) {
    const disp = Number(s.disposal_site?.pressure_psi);
    for (const f of s.facilities || []) {
      if (!f.pump || !f.pipeline) continue;
      const r = running(store, f);
      for (const e of scan(r.lost, { enter: (m) => m > LEAK.enter, leave: (m) => m < LEAK.leave, step: true, stepMin: LEAK.stepMin })) {
        out.push({ ...e, category: "pipeline_leak", system: s.id, facility: f.id, asset: f.pipeline.id, flowAtSeen: r.flow.get(e.seen) ?? null });
      }
      if (Number.isFinite(disp)) {
        const exc = r.press.map(([t, v]) => [t, v - disp - WEAR.healthy]);
        for (const e of scan(exc, { enter: (m) => m < WEAR.enter, leave: (m) => m > WEAR.leave, step: false, faults: r.faults, win: WEAR.win })) {
          out.push({ ...e, category: "pump_degradation", system: s.id, facility: f.id, asset: f.pump.id, healthyPsi: disp + WEAR.healthy, endedByFault: r.faults.includes(e.end) });
        }
      }
    }
  }
  return out.sort((a, b) => a.onset - b.onset);
}

// The screen's events as they stood at time `at`: only what had been seen by then, with
// the ones that had not ended yet marked ongoing.
export function screenAt(events, at) {
  return events.filter((e) => e.seen <= at).map((e) => {
    const ended = e.endSeen !== null && e.endSeen <= at;
    return { ...e, status: ended ? "resolved" : "ongoing", end: ended ? e.end : null };
  });
}

// Mean of the last hour of running readings at or before t (null if fewer than 3).
export function recentMean(x, t, n = W) {
  let hi = x.length - 1;
  while (hi >= 0 && x[hi][0] > t) hi--;
  const k = Math.min(n, hi + 1);
  if (k < 3) return null;
  return mean(x, hi, k);
}

// Pump failures from the status signal: every run of status 2, with its first and last
// reading. A run still going at the end of the data has end null.
export function faultRuns(store, pumpId) {
  const col = store.series.get(pumpId + "|status");
  const out = [];
  if (!col) return out;
  const { times } = store;
  for (let i = 0; i < times.length; i++) {
    if (col[i] !== 2 || (i > 0 && col[i - 1] === 2)) continue;
    let j = i;
    while (j + 1 < times.length && col[j + 1] === 2) j++;
    out.push({ onset: times[i], last: times[j], end: j + 1 < times.length && Number.isFinite(col[j + 1]) ? times[j + 1] : null });
    i = j;
  }
  return out;
}

// The series the screen reads, for evidence lines and charts.
export function screenSeries(store, f, disposalPsi) {
  const r = running(store, f);
  return { lost: r.lost, excess: Number.isFinite(disposalPsi) ? r.press.map(([t, v]) => [t, v - disposalPsi - WEAR.healthy]) : [], flow: r.flow };
}

// Healthy noise of one signal: a robust standard deviation (1.4826 x median absolute
// deviation) of the running readings before `before`, so a problem never widens its own
// band. null with fewer than 24 readings.
export function noiseSd(x, before, n = 2000) {
  let hi = x.length - 1;
  while (hi >= 0 && x[hi][0] >= before) hi--;
  const v = [];
  for (let k = hi; k >= 0 && v.length < n; k--) v.push(x[k][1]);
  if (v.length < 24) return null;
  const med = (a) => { const b = [...a].sort((p, q) => p - q); const m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };
  const m = med(v);
  return 1.4826 * med(v.map((y) => Math.abs(y - m))) || null;
}

// Confidence that something is really wrong, in words an operator can check: the share of
// the last n running readings at or before t that sit outside normal noise (2 sd) on the
// problem's side. A problem that is just starting reads low and climbs as it grows.
export function confidenceAt(x, t, sd, sign, n = 24, since = -Infinity) {
  if (!sd) return null;
  let hi = x.length - 1;
  while (hi >= 0 && x[hi][0] > t) hi--;
  let k = 0, m = 0;
  // Only readings since the onset count: the healthy ones before it say nothing either way.
  for (let j = hi; j >= 0 && m < n && x[j][0] >= since; j--, m++) if (sign * x[j][1] > 2 * sd) k++;
  return m < 3 ? null : { share: k / m, k, n: m, band: 2 * sd };
}
