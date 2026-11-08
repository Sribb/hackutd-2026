// Past plus future on one line (CF-3), for the selected tank.
//
//   solid   the last 60 live minutes of level, kept in this browser from /ws readings
//           and GET /api/history (CF-14) for what came before. A stale period is a gap,
//           never a repeated last value.
//   dashed  the forecast on screen (the what-if result when one is applied), from now on.
//   band    give or take: the forecast stretched in time so it crosses shutdown at the
//           event's t_min_low and t_min_high. Only drawn when the forecast has that event.
//   red     the shutdown level, with the crossing time and its range marked on it.
//
// A what-if changes only the dashed part and the band: the past is what happened.
// One y axis (level %), one x axis (clock). Hover reads any minute.

import * as liveFeed from "./live.js";

const PAST_MIN = 60;
const KEEP_MIN = 90;
const W = 300, H = 132, L = 30, R = 8, T = 14, B = 22;

const hist = new Map();       // tank id -> [{ t: live minute, v: level % or null }]
const asked = new Set();      // tanks we already asked /api/history about
let historyApi = true;        // false after a 404
let base = "";
let hoverAt = null;           // the pointer over a trend, kept across redraws

const clockMin = (c) => {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(c || ""));
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
};
const clockText = (min) => {
  const x = ((Math.round(min) % 1440) + 1440) % 1440, hh = Math.floor(x / 60), mm = x % 60;
  return `${hh % 12 || 12}:${String(mm).padStart(2, "0")} ${hh < 12 ? "AM" : "PM"}`;
};

function record() {
  const L0 = liveFeed.live;
  if (typeof L0.t !== "number") return;
  for (const id of Object.keys(L0.assets)) {
    if (!/^TANK_/.test(id)) continue;
    const r = liveFeed.reading(id);
    const v = r && !r.stale && typeof r.level_pct === "number" ? r.level_pct : null;
    let list = hist.get(id) || [];
    // The live clock went back (the backend restarted): this history is from another run.
    // Start over and ask /api/history again.
    if (list.length && L0.t < list[list.length - 1].t - 1) { list = []; asked.delete(id); }
    const last = list[list.length - 1];
    if (last && L0.t - last.t < 0.2 && (last.v == null) === (v == null)) continue;
    list.push({ t: L0.t, v });
    while (list.length && L0.t - list[0].t > KEEP_MIN) list.shift();
    hist.set(id, list);
  }
}

// Frozen gauge. A working level sensor jitters (the simulator's do too, about ±0.1 point a
// reading). One that reads exactly the same value, reading after reading, for 10 live minutes is
// the signature of a stuck gauge: at Buncefield (2005) the tank gauge flatlined while the tank
// kept filling, and overflowed hours later on the night shift. Returns { v, minutes } or null.
// It is a reason to check in person, not proof: the console stops vouching for that tank.
export const FROZEN_MIN = 10;
export function frozen(id) {
  const list = hist.get(id) || [];
  const L0 = liveFeed.live;
  if (!list.length || typeof L0.t !== "number") return null;
  const last = list[list.length - 1];
  if (last.v == null || L0.t - last.t > 2) return null; // no fresh reading: already blind, not frozen
  let since = last.t, n = 0;
  for (let i = list.length - 1; i >= 0; i--) {
    const p = list[i];
    if (p.v == null || Math.abs(p.v - last.v) > 0.005) break;
    since = p.t;
    n++;
  }
  const minutes = L0.t - since;
  return n >= 5 && minutes >= FROZEN_MIN ? { v: last.v, minutes: Math.floor(minutes) } : null;
}

// CF-14 GET /api/history (CONTRACT.md): timeline rows in the forecast's shape with
// t_min <= 0 relative to the response's live minute `t`. Null is a stale period (a gap);
// rows before `since_t_min` are before the live layer started, so they are left out.
async function backfill(id) {
  if (!base || !historyApi || asked.has(id)) return;
  asked.add(id);
  try {
    const r = await fetch(`${base}/api/history?minutes=${PAST_MIN}&step_min=1&asset_id=${encodeURIComponent(id)}&signal=level_pct`);
    if (r.status === 404) { historyApi = false; return; }
    if (!r.ok) return;
    const j = await r.json();
    if (typeof j.t !== "number" || !Array.isArray(j.timeline)) return;
    const since = typeof j.since_t_min === "number" ? j.since_t_min : -Infinity;
    const got = j.timeline
      .filter((row) => row.t_min >= since && row.tanks && id in row.tanks)
      .map((row) => ({ t: j.t + row.t_min, v: typeof row.tanks[id] === "number" ? row.tanks[id] : null }));
    if (!got.length) return;
    const mine = hist.get(id) || [];
    const first = mine.length ? mine[0].t : Infinity;
    hist.set(id, [...got.filter((p) => p.t < first), ...mine]);
  } catch { /* the browser's own buffer still works */ }
}

export function startTrend(apiBase) {
  base = apiBase || "";
  liveFeed.on("state", record);
}

// The forecast level for a tank at minute t (linear between timeline rows).
function fcAt(fc, id, t) {
  const rows = fc && fc.timeline;
  if (!rows || !rows.length) return null;
  if (t <= rows[0].t_min) return rows[0].tanks[id] ?? null;
  for (let i = 1; i < rows.length; i++) {
    if (rows[i].t_min >= t) {
      const a = rows[i - 1], b = rows[i];
      const va = a.tanks[id], vb = b.tanks[id];
      if (va == null || vb == null) return null;
      return va + ((vb - va) * (t - a.t_min)) / (b.t_min - a.t_min || 1);
    }
  }
  return rows[rows.length - 1].tanks[id] ?? null;
}

// o: { id, fc, shutdownPct, fcClock ("HH:MM" the forecast starts at), sliderMin, blind }
export function trendHtml(o) {
  const L0 = liveFeed.live;
  const liveNow = typeof L0.t === "number" && clockMin(L0.clock) != null;
  // "now" on the clock. With a live layer, the live clock; else the forecast's start.
  const nowClock = liveNow ? clockMin(L0.clock) + (L0.t - Math.floor(L0.t)) : clockMin(o.fcClock) ?? 0;
  // Minutes since the forecast started (it was run a little while ago). Small, never negative.
  let shift = liveNow && clockMin(o.fcClock) != null ? nowClock - clockMin(o.fcClock) : 0;
  if (shift < -720) shift += 1440;
  if (shift > 720) shift -= 1440;
  shift = Math.max(0, shift);

  const ev = o.fc && (o.fc.events || []).find((e) => e.type === "SHUTDOWN_LEVEL" && e.asset_id === o.id);
  const ahead = Math.min(240, Math.max(120, ev ? Math.ceil((ev.t_min_high ?? ev.t_min) - shift + 15) : 120));
  const x0 = -PAST_MIN, x1 = ahead;

  // Past points, relative to now.
  if (liveNow) backfill(o.id);
  const past = liveNow ? (hist.get(o.id) || []).map((p) => ({ x: p.t - L0.t, v: p.v })).filter((p) => p.x >= x0 && p.x <= 0) : [];
  // Forecast from now on.
  const fut = [];
  for (let x = 0; x <= x1; x += 2) fut.push({ x, v: fcAt(o.fc, o.id, x + shift) });
  // Band: stretch the forecast's rise in time so it crosses shutdown at t_min_low / _high.
  let band = null;
  if (ev && ev.t_min_low != null && ev.t_min_high != null && ev.t_min > 0) {
    const lo = [], hi = [];
    for (let x = 0; x <= x1; x += 2) {
      const t = x + shift;
      lo.push({ x, v: fcAt(o.fc, o.id, Math.min(t * (ev.t_min / ev.t_min_low), o.fc.timeline[o.fc.timeline.length - 1].t_min)) });
      hi.push({ x, v: fcAt(o.fc, o.id, t * (ev.t_min / ev.t_min_high)) });
    }
    band = { lo, hi };
  }

  const vals = [...past, ...fut].map((p) => p.v).filter((v) => v != null);
  const yMin = Math.max(0, Math.floor((Math.min(o.shutdownPct, ...vals) - 8) / 10) * 10);
  const yMax = Math.min(100, Math.ceil((Math.max(o.shutdownPct, ...vals) + 4) / 10) * 10);
  const sx = (x) => L + ((x - x0) / (x1 - x0)) * (W - L - R);
  const sy = (v) => T + (1 - (v - yMin) / (yMax - yMin || 1)) * (H - T - B);
  const path = (pts) => {
    let d = "", pen = false;
    for (const p of pts) {
      if (p.v == null) { pen = false; continue; }
      d += `${pen ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.v).toFixed(1)}`;
      pen = true;
    }
    return d;
  };

  const grid = [];
  for (let v = yMin; v <= yMax; v += yMax - yMin > 40 ? 20 : 10) {
    grid.push(`<line class="tr-grid" x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text class="tr-ax" x="${L - 4}" y="${sy(v) + 3}" text-anchor="end">${v}%</text>`);
  }
  const step = x1 > 150 ? 120 : 60;
  const ticks = [x0, 0, ...[1, 2, 3, 4].map((k) => k * step)].filter((x) => x >= x0 && x <= x1 - 25)
    .map((x) => `<text class="tr-ax" x="${sx(x)}" y="${H - 6}" text-anchor="${x === x0 ? "start" : "middle"}">${x === 0 ? "now" : x === x0 ? "1 h ago" : clockText(nowClock + x)}</text>`).join("");

  let cross = "";
  if (ev) {
    const cx = ev.t_min - shift;
    if (cx >= 0 && cx <= x1) {
      const lo = Math.max(0, (ev.t_min_low ?? ev.t_min) - shift), hi = Math.min(x1, (ev.t_min_high ?? ev.t_min) - shift);
      cross = `<line class="tr-range" x1="${sx(lo)}" x2="${sx(hi)}" y1="${sy(o.shutdownPct)}" y2="${sy(o.shutdownPct)}"/>
        <circle class="tr-cross" cx="${sx(cx)}" cy="${sy(o.shutdownPct)}" r="4"/>
        <text class="tr-lbl alarm" x="${Math.min(sx(cx), W - R - 2)}" y="${sy(o.shutdownPct) - 6}" text-anchor="${sx(cx) > W - 70 ? "end" : "middle"}">${clockText(nowClock + cx)}</text>`;
    }
  }
  const slider = o.sliderMin > 0 && o.sliderMin - shift <= x1
    ? `<line class="tr-slider" x1="${sx(o.sliderMin - shift)}" x2="${sx(o.sliderMin - shift)}" y1="${T}" y2="${H - B}"/>` : "";
  const bandPath = band
    ? `<path class="tr-band" d="${path(band.lo)}${path([...band.hi].reverse()).replace(/^M/, "L")}Z"/>` : "";

  let pastNote = !liveNow ? "No live feed, so no past line." : past.some((p) => p.v != null) ? "" : "Collecting live readings for the past line.";
  // The forecast starts from the readings it was run on. If the live level has moved since,
  // say so instead of bending either line to meet the other.
  const lastPast = [...past].reverse().find((p) => p.v != null);
  const fc0 = fut[0] && fut[0].v;
  if (lastPast && fc0 != null && Math.abs(lastPast.v - fc0) >= 2) {
    pastNote = `The forecast (run on ${clockText(clockMin(o.fcClock) ?? nowClock)} readings) puts it at ${fc0.toFixed(1)}% now; the live reading is ${lastPast.v.toFixed(1)}%.`;
  }
  const label = `Tank level trend for ${o.id.replace(/_/g, " ")}: the past hour and the forecast${ev ? `, crossing shutdown at ${clockText(nowClock + ev.t_min - shift)}` : ""}.`;
  return `<figure class="trend" data-trend="${o.id}" data-x0="${x0}" data-x1="${x1}" data-shift="${shift}" data-now="${nowClock}">
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${label}">
      ${grid.join("")}
      <line class="tr-shut" x1="${L}" x2="${W - R}" y1="${sy(o.shutdownPct)}" y2="${sy(o.shutdownPct)}"/>
      <text class="tr-lbl alarm" x="${L + 2}" y="${sy(o.shutdownPct) - 4}">shutdown ${o.shutdownPct}%</text>
      ${bandPath}
      <line class="tr-now" x1="${sx(0)}" x2="${sx(0)}" y1="${T}" y2="${H - B}"/>
      ${slider}
      <path class="tr-past" d="${path(past)}"/>
      <path class="tr-fc" d="${path(fut)}"/>
      ${cross}
      ${ticks}
      <g class="tr-hover" hidden><line y1="${T}" y2="${H - B}"/><text class="tr-tip" y="${T - 4}"></text></g>
      <rect class="tr-hit" x="${L}" y="0" width="${W - L - R}" height="${H}"/>
    </svg>
    <figcaption><span class="k past"></span>past (live) <span class="k fc"></span>forecast${band ? ` <span class="k band"></span>give or take` : ""}${o.blind ? " · no fresh reading: the forecast starts from the last value" : ""}${pastNote ? ` · ${pastNote}` : ""}</figcaption>
  </figure>`;
}

// Hover: a crosshair and the level at that minute (past or forecast).
export function wireTrend(root, o) {
  const fig = root && root.querySelector(".trend");
  if (!fig) return;
  const svg = fig.querySelector("svg"), hit = fig.querySelector(".tr-hit"), g = fig.querySelector(".tr-hover");
  const x0 = Number(fig.dataset.x0), x1 = Number(fig.dataset.x1), shift = Number(fig.dataset.shift), now = Number(fig.dataset.now);
  const line = g.querySelector("line"), tip = g.querySelector("text");
  hit.onpointermove = (e) => {
    const pt = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const p = pt.matrixTransform(svg.getScreenCTM().inverse());
    const x = x0 + ((p.x - L) / (W - L - R)) * (x1 - x0);
    let v = null, kind = "forecast";
    if (x < 0) {
      kind = "reading";
      const list = hist.get(o.id) || [];
      const tNow = liveFeed.live.t;
      let best = null;
      for (const q of list) if (best == null || Math.abs(q.t - tNow - x) < Math.abs(best.t - tNow - x)) best = q;
      v = best && Math.abs(best.t - tNow - x) < 3 ? best.v : null;
      if (best && v == null && Math.abs(best.t - tNow - x) < 3) kind = "no fresh reading";
    } else v = fcAt(o.fc, o.id, x + shift);
    g.removeAttribute("hidden");
    line.setAttribute("x1", p.x); line.setAttribute("x2", p.x);
    tip.setAttribute("x", Math.min(Math.max(p.x, L + 40), W - R - 40));
    tip.setAttribute("text-anchor", "middle");
    tip.textContent = `${clockText(now + x)} · ${v != null ? `${v.toFixed(1)}%` : "no data"} (${kind})`;
    hoverAt = { clientX: e.clientX, clientY: e.clientY };
  };
  hit.onpointerleave = () => { g.setAttribute("hidden", ""); hoverAt = null; };
  // The panel redraws on every live reading; keep the crosshair where the pointer is.
  if (hoverAt) hit.onpointermove(hoverAt);
}
