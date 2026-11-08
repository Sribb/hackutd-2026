// gaia draws charts on request (#159): "plot tank 3 level", "compare tank 3 and tank 4",
// "show tank 3 level for the next 6 hours", "S4 pump C discharge pressure since midnight".
// Pure code, no DOM and no network, so node tests can run it (tests/chartcard.test.mjs):
//
//   chartIntent(t, ctx)  t: intents.normalize(text). null (not a chart question) or
//                        { items, past, ahead, since, ... } or { error } (unknown asset or metric:
//                        said plainly with what can be plotted; never a guess)
//   chartSvg(model)      the small line chart, as an SVG string
//   explain(model)       one plain sentence, computed from the same numbers the chart draws
//
// The model (chartask.js builds it): x is minutes from now (negative is the past), v is a
// reading or null. A null is a gap in the line, never a zero and never "safe".
//   { unit, x0, x1, nowMin, utc, step, windowText, refs: [{ y, label, tone }],
//     series: [{ id, label, metric, past: [{x, v}], fc: [{x, v}] | null, band: { lo, hi } | null,
//                cross: { x, low, high } | null, staleMin }] }

import { niceTicks } from "../dataset/chart.js";

export const MAX_SERIES = 4;

// What each kind of asset can be plotted as. Live field (simulator or connectors): GET
// /api/history signals. Sponsor data (dataset mode): GET /api/eog/series metrics.
const LIVE = {
  TANK: { level: ["level_pct", "% full"] },
  PUMP: { flow: ["flow_bbl_hr", "bbl/hr"] },
  WELL: { flow: ["flow_bbl_hr", "bbl/hr"] },
  SWD: { flow: ["flow_bbl_hr", "bbl/hr"] },
};
const SPONSOR = {
  TANK: { level: ["level_pct", "% full"] },
  PUMP: { flow: ["flow_bpd", "bbl/day"], pressure: ["discharge_psi", "psi"] },
  PIPELINE: { pressure: ["pressure_psi", "psi"], flow: ["receive_flow_bpd", "bbl/day"] },
  WELL: { flow: ["water_bpd", "bbl/day"] },
  FACILITY: { flow: ["inflow_bpd", "bbl/day"] },
};
const WORD = { level_pct: "level", flow_bbl_hr: "flow", flow_bpd: "flow", discharge_psi: "discharge pressure",
  pressure_psi: "pressure", receive_flow_bpd: "flow at the disposal end", water_bpd: "water", inflow_bpd: "inflow" };
export const metricWord = (m) => WORD[m] || String(m || "").replace(/_/g, " ");

const VERB = /\b(plot|chart|graph|trend|draw|sparkline|compare|history of)\b/;
const SHOW = /^(?:please |gaia )?(?:show|give|pull up|bring up|let me see|can i see)(?: me)?\b/;
const QUESTION = /^(?:what|whats|what's|when|why|how|is|are|will|would|should|can|could|does|do|did|which|who)\b/;
const METRIC = /\b(levels?|fill|full|flows?|flowing|rate|inflow|water|pressures?|psi|discharge|forecast|readings?|history|status|running)\b/;
const OTHER = /\b(temperature|temp|vibration|voltage|current|amps?|rpm|speed|ph|salinity|h2s|torque|density|viscosity|power|energy|humidity|wind|cost|price|oil|gas)\b/;
const UNIT = "(h|hr|hrs|hours?|m|min|mins|minutes?)";
const NEXT = new RegExp(`\\b(?:next|coming|upcoming)(?: (\\d+(?:\\.\\d+)?))? ?${UNIT}\\b`);
const LAST = new RegExp(`\\b(?:last|past|previous)(?: (\\d+(?:\\.\\d+)?))? ?${UNIT}\\b`);
const SINCE = /\bsince (midnight|noon|(\d{1,2})(?::(\d{2}))? ?(am|pm|a m|p m)?)\b|\b(today)\b/;
const toMinutes = (n, u) => (n == null || n === "" ? 1 : Number(n)) * (/^h/.test(u) ? 60 : 1);

// "tank 3", "tanks 3 and 4", "pump 3, 4 and 5", in the order the text names them.
function liveNamed(t) {
  const out = [];
  const re = /\b(tank|pump|well|swd|disposal well)s? ?(\d+)((?: ?(?:,|and|&|vs|versus|with)? ?\d+\b)*)/g;
  let m;
  while ((m = re.exec(t))) {
    const kind = m[1] === "disposal well" ? "SWD" : m[1].toUpperCase();
    for (const n of [m[2], ...(m[3].match(/\d+/g) || [])]) out.push(`${kind}_${n}`);
  }
  return [...new Set(out)];
}

// Sponsor ids: "s4 pump c", "S1_WELL_A1", "s2 tank b".
function sponsorNamed(t) {
  const out = [];
  const re = /\bs(\d{1,2})[ _]?(tank|pump|pipeline|pipe|well|facility)[ _]?([a-z])[ _]?(\d)?\b/g;
  let m;
  while ((m = re.exec(t))) out.push(`S${m[1]}_${m[2] === "pipe" ? "PIPELINE" : m[2].toUpperCase()}_${m[3].toUpperCase()}${m[4] || ""}`);
  return [...new Set(out)];
}

export const liveLabel = (id) => String(id).replace(/^SWD_/, "disposal well ").replace(/_/g, " ").toLowerCase();
const kindOf = (id, sponsor) => (sponsor ? String(id).split("_")[1] : String(id).split("_")[0]);

// What the person asked to see, by word: level, flow, pressure, status, or nothing named.
function wanted(t) {
  if (/\b(status|running|on or off)\b/.test(t)) return "status";
  if (/\b(pressures?|psi)\b/.test(t)) return "pressure";
  if (/\b(flows?|flowing|rate|inflow|water|bpd|throughput)\b/.test(t)) return "flow";
  if (/\bdischarge\b/.test(t)) return "discharge";
  if (/\b(levels?|fill|full|forecast)\b/.test(t)) return "level";
  return null;
}

// What this field can plot, in a few words, for "I can't plot that" answers.
export function canPlot(ctx) {
  const ids = (ctx && ctx.ids && ctx.ids()) || [];
  const span = (kind) => {
    const n = ids.filter((id) => new RegExp(`^${kind}_\\d+$`).test(id)).map((id) => Number(id.split("_")[1])).sort((a, b) => a - b);
    if (!n.length) return "";
    const w = kind === "SWD" ? "disposal well" : kind.toLowerCase();
    return n.length === 1 ? `${w} ${n[0]}` : `${w} ${n[0]} to ${n[n.length - 1]}`;
  };
  const parts = [span("TANK") && `${span("TANK")} (level and forecast)`, span("PUMP") && `${span("PUMP")} (flow)`, span("WELL") && `${span("WELL")} (flow)`].filter(Boolean);
  if (parts.length) return `I can plot ${parts.join(", ")}, like "plot tank 3 level" or "compare tank 3 and tank 4".`;
  // The sponsor network (dataset mode): its asset ids are the sponsor's.
  const tank = ids.find((id) => /^S\d+_TANK_[A-Z]$/.test(id));
  if (tank) { const [sys, , f] = tank.split("_"); return `I can plot the sponsor tanks, pumps, pipelines, wells and facilities by id, like "plot ${sys} tank ${f} level since midnight".`; }
  return `Try "plot tank 3 level".`;
}

function windowOf(t) {
  let m;
  const out = { past: null, ahead: null, since: null, sinceText: null };
  if ((m = NEXT.exec(t))) out.ahead = toMinutes(m[1], m[2]);
  if ((m = LAST.exec(t))) out.past = toMinutes(m[1], m[2]);
  if ((m = SINCE.exec(t))) {
    if (m[5] || m[1] === "midnight") { out.since = 0; out.sinceText = "midnight"; }
    else if (m[1] === "noon") { out.since = 720; out.sinceText = "noon"; }
    else {
      let h = Number(m[2]) % 24;
      const ap = m[4] ? m[4].replace(/ /g, "") : "";
      if (ap) { h %= 12; if (ap === "pm") h += 12; }
      out.since = h * 60 + Number(m[3] || 0);
      out.sinceText = clockText(out.since);
    }
  }
  return out;
}

// t: normalized text (intents.normalize). ctx: { hasId, ids, selected }.
export function chartIntent(t, ctx) {
  const raw = String(t || "").trim();
  if (!raw) return null;
  let sponsor = sponsorNamed(raw);
  let live = liveNamed(raw);
  const named = live.length || sponsor.length;
  const verb = VERB.test(raw);
  const show = SHOW.test(raw) && METRIC.test(raw);
  const win = windowOf(raw);
  const timed = win.past != null || win.ahead != null || win.since != null;
  const bare = !QUESTION.test(raw) && named && (METRIC.test(raw) || OTHER.test(raw)) && (timed || sponsor.length > 0);
  if (!verb && !show && !bare) return null;
  // "Compare the plans" and other compares without an asset are questions for the backend.
  if (!named && /\bcompare\b/.test(raw)) return null;
  if (!named && /\b(it|its|this|that|this one|that one|the selection|selected)\b/.test(raw) && ctx.selected && ctx.selected()) {
    const sel = ctx.selected();
    if (/^S\d+_[A-Z]+_[A-Z0-9]+$/.test(sel)) sponsor = [sel]; else live = [sel]; // dataset mode: the selection is a sponsor id
  }
  if (!live.length && !sponsor.length) {
    if (/\b(valve|crossover|xo|truck|yard)\b/.test(raw)) return { error: `Valves and trucks report a position or a trip, not a number over time, so there's no line to draw. ${canPlot(ctx)}` };
    return { error: `Name what to plot. ${canPlot(ctx)}` };
  }
  if (live.length && sponsor.length) return { error: "That mixes this field's assets with sponsor data, which run on different clocks. Ask for one at a time." };

  const isSponsor = sponsor.length > 0;
  const ids = isSponsor ? sponsor : live;
  if (!isSponsor) {
    const missing = ids.filter((id) => !ctx.hasId(id));
    if (missing.length) return { error: `There's no ${liveLabel(missing[0])} on this field. ${canPlot(ctx)}` };
  }
  const want = wanted(raw);
  const table = isSponsor ? SPONSOR : LIVE;
  // A reading nobody has ("temperature"): say so, never swap in another one.
  const other = !want && OTHER.exec(raw);
  if (other) {
    const it = ids[0], opts = table[kindOf(it, isSponsor)];
    return { error: `${isSponsor ? it : liveLabel(it)} has no ${other[1]} reading${isSponsor ? " in the sponsor data" : " on this field"}.${opts ? ` I can plot its ${Object.entries(opts).map(([k, v]) => `${k} (${v[1]})`).join(" or ")}.` : ` ${canPlot(ctx)}`}` };
  }
  const items = [];
  for (const id of ids) {
    const kind = kindOf(id, isSponsor);
    const opts = table[kind];
    const label = isSponsor ? id : liveLabel(id);
    if (!opts) return { error: `${isSponsor ? id : liveLabel(id)} has no reading I can draw as a line. ${isSponsor ? "Sponsor tanks, pumps, pipelines, wells and facilities have readings." : canPlot(ctx)}` };
    if (want === "status") return { error: `Status is a code (running, stopped, fault), not a reading I can draw as a line. Try ${Object.keys(opts).map((k) => `${label} ${k}`).join(" or ")}.` };
    let key = want === "discharge" ? (opts.pressure ? "pressure" : opts.flow ? "flow" : null) : want;
    if (key == null || (key === "level" && !opts.level)) key = Object.keys(opts)[0];
    if (!opts[key]) return { error: `${label} has no ${key} reading${isSponsor ? " in the sponsor data" : " on this field"}. I can plot its ${Object.entries(opts).map(([k, v]) => `${k} (${v[1]})`).join(" or ")}.` };
    items.push({ id, kind, metric: opts[key][0], unit: opts[key][1], label });
  }
  const units = [...new Set(items.map((i) => i.unit))];
  if (units.length > 1) return { error: `${items[0].label} is in ${items[0].unit} and ${items.find((i) => i.unit !== items[0].unit).label} in ${units.find((u) => u !== items[0].unit)}, so they can't share one axis. Ask for them one at a time.` };
  const cut = Math.max(0, items.length - MAX_SERIES);
  const kept = items.slice(0, MAX_SERIES);
  const tanks = kept.every((i) => i.kind === "TANK");
  if (win.ahead != null && (isSponsor || !tanks)) {
    return { error: isSponsor ? "The sponsor data is a record of what happened, so there is no forecast line in it. Ask for the past, like \"since midnight\"." : `The forecast covers tank levels only, so I can't draw ${kept.find((i) => i.kind !== "TANK").label} ahead. Try "plot ${kept[0].label} ${metricWord(kept[0].metric)}" for the last hour.` };
  }
  return {
    source: isSponsor ? "dataset" : "live",
    items: kept, cut,
    past: win.past != null ? Math.min(win.past, 720) : null,
    ahead: win.ahead != null ? Math.min(win.ahead, 360) : null,
    since: win.since, sinceText: win.sinceText,
    forecast: !isSponsor && tanks && win.past == null && win.since == null,
  };
}

// ------------------------------------------------------------- numbers ----
export function clockText(min) {
  if (min == null || !isFinite(min)) return "--:--";
  const x = ((Math.round(min) % 1440) + 1440) % 1440, hh = Math.floor(x / 60), mm = x % 60;
  return `${hh % 12 || 12}:${String(mm).padStart(2, "0")} ${hh < 12 ? "am" : "pm"}`;
}
export function fmt(v) {
  const a = Math.abs(v);
  if (a >= 1000) return Math.round(v).toLocaleString("en-US");
  if (a >= 100) return String(Math.round(v));
  if (a >= 10) return v.toFixed(1).replace(/\.0$/, "");
  return v.toFixed(1).replace(/\.0$/, "");
}
const valid = (pts) => (pts || []).filter((p) => p.v != null && isFinite(p.v));

// Least-squares slope, per hour, of the readings with x in [a, b].
export function slopePerHour(pts, a = -Infinity, b = Infinity) {
  const p = valid(pts).filter((q) => q.x >= a && q.x <= b);
  if (p.length < 2) return null;
  const mx = p.reduce((s, q) => s + q.x, 0) / p.length, my = p.reduce((s, q) => s + q.v, 0) / p.length;
  let num = 0, den = 0;
  for (const q of p) { num += (q.x - mx) * (q.v - my); den += (q.x - mx) ** 2; }
  return den ? (num / den) * 60 : null;
}

// Minutes since the last reading, when the line ends in a gap before now.
function trailingGap(s, step) {
  if (s.staleMin != null && s.staleMin >= 1) return Math.round(s.staleMin);
  const p = valid(s.past);
  if (!s.past || !s.past.length) return null;
  const end = s.past[s.past.length - 1].x;
  if (!p.length) return null;
  const gap = end - p[p.length - 1].x;
  return gap > Math.max(2 * (step || 1), 3) ? Math.round(gap) : null;
}

// ---------------------------------------------------------- the sentence ----
const pts = (r) => { const a = Math.abs(r); return a >= 3 ? String(Math.round(a)) : a.toFixed(1).replace(/\.0$/, ""); };
const withUnit = (v, unit) => (unit === "% full" ? `${fmt(v)}%` : `${fmt(v)} ${unit}`);

const shutOf = (m) => (m.refs || []).find((r) => r.tone === "alarm");
// When the forecast line is at or over the shutdown line: the event's time, else its first point there.
function reachX(s, m) {
  if (s.cross) return s.cross.x;
  const shut = shutOf(m);
  const hit = shut && valid(s.fc).find((p) => p.v >= shut.y);
  return hit ? hit.x : null;
}

function tankForecastPhrase(s, m) {
  const ahead = valid(s.fc);
  const shut = shutOf(m);
  const end = s.cross ? Math.min(s.cross.x, 60) : Math.min(60, m.x1);
  const r = slopePerHour(s.fc, 0, Math.max(end, 10));
  const move = r == null ? "" : Math.abs(r) < 0.5 ? "holds about steady" : `${r > 0 ? "rises" : "falls"} about ${pts(r)} points an hour`;
  const who = `${s.label} `;
  if (s.cross) {
    const lo = s.cross.low != null ? clockText(m.nowMin + s.cross.low) : null, hi = s.cross.high != null ? clockText(m.nowMin + s.cross.high) : null;
    const at = clockText(m.nowMin + s.cross.x);
    // One time when both ends print the same (EOG's p10 = p90), and nothing when that is the crossing itself.
    const range = lo != null && hi != null && !(lo === hi && lo === at) ? ` (give or take: ${lo === hi ? lo : `${lo} to ${hi}`})` : "";
    return `${who}${move ? `${move} and ` : ""}crosses shutdown at ${clockText(m.nowMin + s.cross.x)}${range} if nobody acts`;
  }
  if (!ahead.length) return `${who}has no forecast line`;
  const last = ahead[ahead.length - 1];
  const x = reachX(s, m);
  if (x != null) {
    const back = ahead.find((p) => p.x > x && p.v < shut.y);
    const after = back ? `, and the forecast has it back under the line by ${clockText(m.nowMin + back.x)}` : `, and the forecast keeps it there through ${clockText(m.nowMin + last.x)}`;
    if (x <= 1) return `${who}is at shutdown now, about ${fmt(ahead[0].v)}% against the ${fmt(shut.y)}% line${after} if nobody acts`;
    return `${who}${move ? `${move} and ` : ""}reaches shutdown about ${clockText(m.nowMin + x)} if nobody acts`;
  }
  return `${who}${move || "moves"}, to about ${fmt(last.v)}% by ${clockText(m.nowMin + last.x)}, and stays under shutdown in that forecast`;
}

function pastPhrase(s, m) {
  const p = valid(s.past);
  const what = `${s.label}${m.unit === "% full" ? "" : ` ${metricWord(s.metric)}`}`;
  if (!p.length) return `${what} has no readings ${m.windowText}, so there is no line; treat it as unknown, not safe`;
  const a = p[0].v, b = p[p.length - 1].v;
  const lo = Math.min(...p.map((q) => q.v)), hi = Math.max(...p.map((q) => q.v));
  const big = Math.max(Math.abs(a), Math.abs(b), Math.abs(hi), 1);
  const two = (x, y) => (m.unit === "% full" ? `${fmt(x)}% to ${fmt(y)}%` : `${fmt(x)} to ${fmt(y)} ${m.unit}`);
  let head;
  if (Math.abs(b - a) > Math.max(0.5, 0.03 * big)) head = `${what} ${b > a ? "rose" : "fell"} from ${two(a, b)} ${m.windowText}`;
  else if (hi - lo <= Math.max(0.5, 0.01 * big)) head = `${what} held near ${withUnit(b, m.unit)} ${m.windowText}`;
  else head = `${what} stayed between ${two(lo, hi).replace(" to ", " and ")} ${m.windowText}`;
  const gap = trailingGap(s, m.step);
  return gap ? `${head}, with no reading for the last ${gap} min, so treat it as unknown, not safe` : head;
}

function limitNote(s, m) {
  const p = valid(s.past);
  if (!p.length || !(m.refs || []).length || m.fcShown) return "";
  const b = p[p.length - 1].v;
  const refs = [...m.refs].sort((x, y) => y.y - x.y);
  const over = refs.find((r) => b >= r.y);
  if (over) return ` and is at or above the ${withUnit(over.y, m.unit)} ${over.name || "limit"} line`;
  const next = refs[refs.length - 1];
  return ` and is under the ${withUnit(next.y, m.unit)} ${next.name || "limit"} line`;
}

export function explain(m) {
  const S = (m.series || []).filter(Boolean);
  if (!S.length) return "There is nothing to plot.";
  const blind = S.filter((s) => trailingGap(s, m.step) && !s.cross);
  const done = (x) => `${x.charAt(0).toUpperCase()}${x.slice(1)}.`;
  if (m.fcShown) {
    if (S.length === 1) {
      const s = S[0];
      if (blind.length) return done(`${s.label} has had no fresh reading for ${trailingGap(s, m.step)} min, so the forecast starts from its last value; treat it as unknown, not safe`);
      return done(tankForecastPhrase(s, m));
    }
    const at = (s) => reachX(s, m) ?? Infinity;
    const order = [...S].sort((a, b) => at(a) - at(b));
    const first = order[0];
    const rest = order.slice(1).map((s) => (trailingGap(s, m.step) ? `${s.label} has no fresh reading (unknown, not safe)`
      : at(s) <= 1 ? `${s.label} is at shutdown too` : at(s) < Infinity ? `${s.label} follows at ${clockText(m.nowMin + at(s))}` : `${s.label} stays under it through ${clockText(m.nowMin + m.x1)}`));
    if (at(first) <= 1) return done(`${first.label} is at shutdown now, and ${rest.join(", and ")}`);
    if (at(first) < Infinity) return done(`${first.label} crosses shutdown first, at ${clockText(m.nowMin + at(first))}, if nobody acts, and ${rest.join(", and ")}`);
    return done(`${S.map((s) => tankForecastPhrase(s, m)).join(", and ")}`);
  }
  if (S.length === 1) return done(`${pastPhrase(S[0], m)}${trailingGap(S[0], m.step) ? "" : limitNote(S[0], m)}`);
  return done(S.map((s) => pastPhrase(s, m)).join(", and "));
}

// ------------------------------------------------------------- the chart ----
const W = 320, H = 150, L = 38, R = 10, T = 12, B = 22;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// A path that lifts the pen at every null: a missing reading is a gap, never a zero.
export function linePath(points, sx, sy) {
  let d = "", run = 0;
  for (const p of points || []) {
    if (p.v == null || !isFinite(p.v)) { if (run === 1) d += "l0.01,0"; run = 0; continue; }
    d += `${run ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.v).toFixed(1)}`;
    run++;
  }
  if (run === 1) d += "l0.01,0"; // a lone reading shows as a dot
  return d;
}

export function yRange(m) {
  const vals = [];
  for (const s of m.series) for (const k of ["past", "fc"]) for (const p of valid(s[k])) vals.push(p.v);
  for (const s of m.series) if (s.band) for (const p of valid([...s.band.lo, ...s.band.hi])) vals.push(p.v);
  for (const r of m.refs || []) vals.push(r.y);
  if (!vals.length) return m.unit === "% full" ? [0, 100] : [0, 1];
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (m.unit === "% full") return [Math.max(0, Math.floor((lo - 6) / 10) * 10), Math.min(100, Math.ceil((hi + 4) / 10) * 10)];
  if (lo > 0 && lo < (hi - lo) * 0.6) lo = 0;
  const pad = (hi - lo) * 0.08 || Math.max(1, Math.abs(hi) * 0.05);
  const ticks = niceTicks(lo === 0 ? 0 : lo - pad, hi + pad, 3);
  return [Math.min(lo === 0 ? 0 : ticks[0], lo), Math.max(ticks[ticks.length - 1], hi)];
}

export function chartSvg(m) {
  const [lo, hi] = yRange(m);
  const sx = (x) => L + ((x - m.x0) / (m.x1 - m.x0 || 1)) * (W - L - R);
  const sy = (v) => T + (1 - (v - lo) / (hi - lo || 1)) * (H - T - B);
  const out = [];
  const yt = m.unit === "% full" ? niceTicks(lo, hi, 4).filter((v) => v >= lo && v <= hi) : niceTicks(lo, hi, 3).filter((v) => v >= lo && v <= hi);
  for (const v of yt) out.push(`<line class="cc-grid" x1="${L}" x2="${W - R}" y1="${sy(v).toFixed(1)}" y2="${sy(v).toFixed(1)}"/><text class="cc-ax" x="${L - 4}" y="${(sy(v) + 3).toFixed(1)}" text-anchor="end">${m.unit === "% full" ? `${fmt(v)}%` : fmt(v)}</text>`);
  // Time ticks on round clock times, plus "now".
  const span = m.x1 - m.x0;
  const step = [15, 30, 60, 120, 180, 240, 360].find((s) => span / s <= 4) || 720;
  const first = Math.ceil((m.nowMin + m.x0) / step) * step - m.nowMin;
  const nowIn = m.x0 <= 0 && m.x1 >= 0;
  for (let x = first; x <= m.x1 + 1e-6; x += step) {
    if (nowIn && Math.abs(sx(x) - sx(0)) < 26) continue;
    out.push(`<text class="cc-ax" x="${sx(x).toFixed(1)}" y="${H - 6}" text-anchor="middle">${clockText(m.nowMin + x).replace(":00 ", " ")}</text>`);
  }
  if (nowIn) out.push(`<line class="cc-now" x1="${sx(0).toFixed(1)}" x2="${sx(0).toFixed(1)}" y1="${T}" y2="${H - B}"/><text class="cc-ax now" x="${sx(0).toFixed(1)}" y="${H - 6}" text-anchor="${sx(0) > W - R - 12 ? "end" : "middle"}">now</text>`);
  // Limits: the shutdown line in red (an alarm line), a warning line in amber.
  const refs = (m.refs || []).filter((r) => r.y >= lo && r.y <= hi).sort((a, b) => b.y - a.y);
  refs.forEach((r, i) => {
    const below = i > 0 && Math.abs(sy(r.y) - sy(refs[i - 1].y)) < 11;
    out.push(`<line class="cc-ref ${esc(r.tone || "")}" x1="${L}" x2="${W - R}" y1="${sy(r.y).toFixed(1)}" y2="${sy(r.y).toFixed(1)}"/><text class="cc-lbl ${esc(r.tone || "")}" x="${L + 3}" y="${(sy(r.y) + (below ? 9 : -3)).toFixed(1)}">${esc(r.label)}</text>`);
  });
  m.series.forEach((s, i) => {
    const c = `s${i + 1}`;
    if (s.band) out.push(`<path class="cc-band ${c}" d="${linePath(s.band.lo, sx, sy)}${linePath([...s.band.hi].reverse(), sx, sy).replace(/^M/, "L")}Z"/>`);
    if (s.past && s.past.length) out.push(`<path class="cc-past ${c}" d="${linePath(s.past, sx, sy)}"/>`);
    if (s.fc && s.fc.length) out.push(`<path class="cc-fc ${c}" d="${linePath(s.fc, sx, sy)}"/>`);
  });
  for (const s of m.series) {
    if (!s.cross || s.cross.x < m.x0 || s.cross.x > m.x1) continue;
    const ref = refs.find((r) => r.tone === "alarm");
    if (!ref) continue;
    const cx = sx(s.cross.x), cy = sy(ref.y);
    if (s.cross.low != null && s.cross.high != null) out.push(`<line class="cc-range" x1="${sx(Math.max(m.x0, s.cross.low)).toFixed(1)}" x2="${sx(Math.min(m.x1, s.cross.high)).toFixed(1)}" y1="${cy.toFixed(1)}" y2="${cy.toFixed(1)}"/>`);
    out.push(`<circle class="cc-cross" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="3.5"/><text class="cc-lbl alarm" x="${Math.min(cx, W - R - 2).toFixed(1)}" y="${(cy - 6).toFixed(1)}" text-anchor="${cx > W - 60 ? "end" : "middle"}">${clockText(m.nowMin + s.cross.x)}</text>`);
  }
  const any = m.series.some((s) => valid(s.past).length || valid(s.fc).length);
  if (!any) out.push(`<text class="cc-empty" x="${(L + W - R) / 2}" y="${(T + H - B) / 2}" text-anchor="middle">no readings in this window</text>`);
  const label = `${m.series.map((s) => s.label).join(", ")}: ${metricWord(m.series[0].metric)} in ${m.unit}${m.fcShown ? ", past and forecast" : ""}`;
  return `<svg class="cc-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">${out.join("")}</svg>`;
}
