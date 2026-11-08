// gaia's chart answers (#159): reads the data a chart intent asked for and posts one card,
// with no LLM. The words and the lines come from the same numbers (chartcard.js).
//
//   live field    past:     GET /api/history (CF-14), the readings the console showed
//                 forecast: the do-nothing forecast the console already holds (POST /api/forecast)
//   sponsor data  GET /api/eog/clock, then GET /api/eog/series (dataset mode)
//
//   makeCharts(D) -> { answer(spec) }
//   D: { BASE, data, hasId, nice, say(text, extra), stream, focus(id) }

import * as liveFeed from "../live.js";
import { toMin } from "./agents.js";
import { chartSvg, explain, clockText, fmt, metricWord, MAX_SERIES } from "./chartcard.js";
import "./chartcard.css";

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const dur = (m) => (m === 60 ? "hour" : m % 60 === 0 ? `${m / 60} hours` : m > 90 ? `${(m / 60).toFixed(1).replace(/\.0$/, "")} hours` : `${Math.round(m)} min`);
const SPONSOR_ID = /\bS\d+_[A-Z]+_[A-Z0-9]+\b/g;
const HAS_SPONSOR_ID = /\bS\d+_[A-Z]+_[A-Z0-9]+\b/;

// The forecast level for a tank at minute t, linear between timeline rows (as trend.js reads it).
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
  return null; // past the forecast's horizon: no line, not the last value
}

export function makeCharts(D) {
  let network = null;

  async function getJson(path) {
    let r;
    try { r = await fetch(`${D.BASE}${path}`); } catch { throw new Error("Can't reach the backend, so I can't read that data. Nothing was drawn."); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(typeof j.detail === "string" ? j.detail : `The backend answered ${r.status}.`), { status: r.status });
    return j;
  }

  // ------------------------------------------------------------ live field ----
  async function liveModel(spec) {
    const d = (D.data && D.data()) || {};
    const fc = d.baseline || null;
    const L0 = liveFeed.live;
    const frac = (t) => (typeof t === "number" ? t - Math.floor(t) : 0);
    let nowMin = toMin(L0.clock) ?? toMin(d.state && d.state.clock) ?? toMin(fc && fc.clock) ?? 0;
    if (L0.clock) nowMin += frac(L0.t);
    const items = spec.items;
    const ids = items.map((i) => i.id);
    let past = spec.past;
    if (spec.since != null) past = Math.min(720, (((nowMin - spec.since) % 1440) + 1440) % 1440 || 720);
    if (past == null) past = spec.ahead != null ? 0 : 60;
    past = Math.round(past);
    const notes = [];

    let hist = null;
    if (past > 0) {
      if (!D.BASE) notes.push("no backend, so no past readings");
      else {
        const step = past <= 120 ? 1 : past <= 360 ? 2 : 5;
        try {
          hist = await getJson(`/api/history?minutes=${past}&step_min=${step}&asset_id=${encodeURIComponent(ids.join(","))}&signal=${encodeURIComponent(items[0].metric)}`);
          if (hist.note || typeof hist.t !== "number") { notes.push("no live layer, so no past readings"); hist = null; }
          else if (hist.clock) nowMin = (toMin(hist.clock) ?? nowMin) + frac(hist.t);
        } catch (e) { notes.push(e.status === 404 ? "this backend keeps no reading history" : "couldn't read past readings"); }
      }
    }

    const showFc = !!(spec.forecast && fc && fc.timeline && fc.timeline.length);
    const fcMin = fc ? (toMin(fc.clock) ?? toMin(d.state && d.state.clock)) : null;
    let shift = showFc && fcMin != null ? nowMin - fcMin : 0;
    if (shift < -720) shift += 1440;
    if (shift > 720) shift -= 1440;
    shift = Math.max(0, shift);
    const horizon = showFc ? fc.timeline[fc.timeline.length - 1].t_min - shift : 0;
    const evOf = (id) => showFc && (fc.events || []).find((e) => e.type === "SHUTDOWN_LEVEL" && e.asset_id === id);
    let ahead = 0;
    if (showFc) {
      ahead = spec.ahead ?? Math.min(360, Math.max(120, ...ids.map((id) => { const e = evOf(id); return e ? Math.ceil((e.t_min_high ?? e.t_min) - shift + 15) : 120; })));
      ahead = Math.max(10, Math.min(ahead, horizon));
    }
    if (spec.forecast && !showFc) notes.push("no forecast on screen yet");

    const series = items.map((it) => {
      const h = hist && (hist.series || []).find((s) => s.asset_id === it.id && s.signal === it.metric);
      const since = hist && typeof hist.since_t_min === "number" ? hist.since_t_min : -Infinity;
      const pastPts = h ? h.points.filter((p) => p[0] >= -past).map(([x, v]) => ({ x, v: x < since ? null : typeof v === "number" ? v : null })) : [];
      const r = L0.available ? liveFeed.reading(it.id) : null;
      const staleMin = r && r.stale ? liveFeed.staleMinutes(it.id) : null;
      const s = { id: it.id, label: D.nice ? D.nice(it.id).toLowerCase() : it.label, metric: it.metric, past: pastPts, fc: null, band: null, cross: null, staleMin };
      if (!showFc) return s;
      s.fc = [];
      for (let x = 0; x <= ahead + 1e-6; x += 2) s.fc.push({ x, v: fcAt(fc, it.id, x + shift) });
      const ev = evOf(it.id);
      if (ev && ev.t_min - shift >= 0 && ev.t_min - shift <= ahead) {
        s.cross = { x: ev.t_min - shift, low: ev.t_min_low != null ? Math.max(0, ev.t_min_low - shift) : null, high: ev.t_min_high != null ? ev.t_min_high - shift : null };
        // Give or take: the forecast stretched in time so it crosses at t_min_low and t_min_high.
        if (ev.t_min_low != null && ev.t_min_high != null && ev.t_min > 0) {
          const lo = [], hi = [], end = fc.timeline[fc.timeline.length - 1].t_min;
          for (let x = 0; x <= ahead + 1e-6; x += 2) {
            const t = x + shift;
            lo.push({ x, v: fcAt(fc, it.id, Math.min(t * (ev.t_min / ev.t_min_low), end)) });
            hi.push({ x, v: fcAt(fc, it.id, t * (ev.t_min / ev.t_min_high)) });
          }
          s.band = { lo, hi };
        }
      }
      return s;
    });

    const tanks = ((d.field && d.field.tanks) || []);
    const shut = [...new Set(items.map((it) => (tanks.find((t) => t.id === it.id) || {}).shutdown_pct).filter((v) => typeof v === "number"))];
    const refs = items[0].kind === "TANK" ? shut.map((y) => ({ y, label: `shutdown ${fmt(y)}%`, tone: "alarm", name: "shutdown" })) : [];

    // Where the numbers came from, and how old they are.
    const cap = [];
    if (hist) {
      const ages = ids.map((id) => ({ id, a: liveFeed.ageText(id), m: liveFeed.staleMinutes(id) })).sort((a, b) => (b.m ?? 0) - (a.m ?? 0));
      const worst = ages[0];
      cap.push(`live readings, newest ${worst && worst.a ? worst.a : "of unknown age"}${ids.length > 1 && worst && worst.a ? ` (oldest of the ${ids.length})` : ""}`);
      if (typeof hist.since_t_min === "number" && hist.since_t_min > -past + 1) cap.push(`recorded since ${clockText(nowMin + hist.since_t_min)}`);
    }
    if (showFc) cap.push(`forecast made ${clockText(fcMin)}${shift >= 1 ? `, ${Math.round(shift)} min ago` : ""}, if nobody acts`);
    cap.push(...notes);
    if (spec.cut) cap.push(`showing the first ${MAX_SERIES} of ${MAX_SERIES + spec.cut}`);
    const src = [hist && "GET /api/history", showFc && "POST /api/forecast"].filter(Boolean).join(" and ");
    return {
      unit: items[0].unit, x0: -past, x1: showFc ? ahead : 0, nowMin, utc: false, step: hist ? hist.step_min || 1 : 1,
      windowText: spec.since != null ? `since ${spec.sinceText}` : `over the last ${dur(past)}`,
      refs, series, fcShown: showFc, caption: cap.join(" · "), title: `${items.length > 1 ? series.map((s) => s.label).join(", ") : series[0].label} ${metricWord(items[0].metric)}`,
      src: src || "nothing", ids: ids.filter((id) => D.hasId(id)), focusId: ids.find((id) => D.hasId(id)) || null,
    };
  }

  // ---------------------------------------------------------- sponsor data ----
  async function datasetModel(spec) {
    if (!D.BASE) throw new Error("Sponsor data needs the backend in dataset mode. Nothing was drawn.");
    const clock = await getJson("/api/eog/clock");
    const now = clock.now, step = clock.step_s || 300;
    const nowMin = (((now % 86400) + 86400) % 86400) / 60;
    let from;
    if (spec.since != null) { from = now - (now % 86400) + spec.since * 60; if (from > now) from -= 86400; }
    else from = now - (spec.past ?? 360) * 60;
    const items = spec.items;
    const got = await Promise.all(items.map((it) => getJson(`/api/eog/series?asset_id=${encodeURIComponent(it.id)}&metric=${encodeURIComponent(it.metric)}&from=${from}&to=${now}&max_points=2000`)));
    const series = items.map((it, i) => {
      const pts = [];
      let prev = null;
      for (const [t, v] of got[i].points || []) {
        if (prev != null && t - prev > 1.5 * step) pts.push({ x: (prev + step - now) / 60, v: null }); // missing rows: a gap
        pts.push({ x: (t - now) / 60, v: typeof v === "number" ? v : null });
        prev = t;
      }
      if (prev != null && now - prev > 1.5 * step) pts.push({ x: 0, v: null });
      return { id: it.id, label: it.id, metric: it.metric, past: pts, fc: null, band: null, cross: null, staleMin: prev != null && now - prev > 1.5 * step ? (now - prev) / 60 : null };
    });
    const refs = [];
    const it0 = items[0];
    if (it0.metric === "level_pct" || it0.metric === "pressure_psi") {
      try {
        network = network || await getJson("/api/eog/network");
        const fac = (network.facilities || []).find((f) => (f.tank && f.tank.id === it0.id) || (f.pipeline && f.pipeline.id === it0.id));
        if (it0.metric === "level_pct") {
          const w = (fac && fac.tank && fac.tank.warning_pct) ?? (network.limits || {}).warning_pct;
          const s = (fac && fac.tank && fac.tank.shutdown_pct) ?? (network.limits || {}).shutdown_pct;
          if (typeof w === "number") refs.push({ y: w, label: `warning ${fmt(w)}%`, tone: "warn", name: "warning" });
          if (typeof s === "number") refs.push({ y: s, label: `shutdown ${fmt(s)}%`, tone: "alarm", name: "shutdown" });
        } else if (fac && fac.pipeline && typeof fac.pipeline.max_pressure_psi === "number" && items.length === 1) {
          refs.push({ y: fac.pipeline.max_pressure_psi, label: `pipeline max ${fmt(fac.pipeline.max_pressure_psi)} psi`, tone: "warn", name: "pipeline max" });
        }
      } catch { /* limits are context; the readings still stand */ }
    }
    const past = Math.max(5, Math.round((now - from) / 60));
    return {
      unit: it0.unit, x0: -past, x1: 0, nowMin, utc: true, step: step / 60,
      windowText: spec.since != null ? `since ${spec.sinceText} utc` : `over the last ${dur(past)}`,
      refs, series, fcShown: false,
      caption: [`sponsor data, ${clock.dataset}`, `newest reading ${clockText(nowMin)} utc (data time)`, clock.live === false ? "clock is before the end of the data" : "", spec.cut ? `showing the first ${MAX_SERIES} of ${MAX_SERIES + spec.cut}` : ""].filter(Boolean).join(" · "),
      title: `${items.map((i) => i.id).join(", ")} ${metricWord(it0.metric)}`,
      src: "GET /api/eog/series", ids: items.map((i) => i.id).filter((id) => D.hasId(id)), focusId: items.map((i) => i.id).find((id) => D.hasId(id)) || null,
    };
  }

  // ------------------------------------------------------------- the card ----
  function cardHtml(m) {
    const multi = m.series.length > 1;
    const keys = multi
      ? m.series.map((s, i) => (D.hasId(s.id)
        ? `<button type="button" class="cc-key" data-asset="${esc(s.id)}"><i class="sw s${i + 1}"></i>${esc(s.label)}</button>`
        : `<span class="cc-key"><i class="sw s${i + 1}"></i><span class="cc-id">${esc(s.label)}</span></span>`))
      : [];
    if (m.series.some((s) => s.past && s.past.length) && m.fcShown) keys.push(`<span class="cc-key"><i class="sw past"></i>past</span>`);
    if (m.fcShown) keys.push(`<span class="cc-key"><i class="sw fc"></i>forecast</span>`);
    if (m.series.some((s) => s.band)) keys.push(`<span class="cc-key"><i class="sw band"></i>give or take</span>`);
    const title = esc(m.title).replace(SPONSOR_ID, (id) => `<span class="cc-id">${id}</span>`);
    return `<figure class="cc-card${m.focusId ? " go" : ""}" data-chart="${esc(m.series.map((s) => s.id).join(","))}"${m.focusId ? ` tabindex="0" title="show ${esc(m.series[0].label)} in 3d"` : ""}>
      <div class="cc-head"><b>${title}</b><span class="cc-unit">${esc(m.unit)}${m.utc ? " · utc" : ""}</span></div>
      ${chartSvg(m)}
      ${keys.length ? `<div class="cc-legend">${keys.join("")}</div>` : ""}
      <figcaption class="cc-src" title="${esc(m.src)}">${esc(m.caption)}</figcaption>
    </figure>`;
  }

  function bind(li, m) {
    const card = li.querySelector(".cc-card");
    if (card && m.focusId) {
      const go = (e) => { if (e.target.closest("button")) return; D.focus(m.focusId); };
      card.addEventListener("click", go);
      card.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(e); } });
    }
    // Sponsor ids stay as written in the sentence (everything else on screen is lowercase).
    const p = li.querySelector(".cs-text");
    if (!p) return;
    const walk = document.createTreeWalker(p, NodeFilter.SHOW_TEXT);
    const hits = [];
    for (let n = walk.nextNode(); n; n = walk.nextNode()) if (HAS_SPONSOR_ID.test(n.nodeValue)) hits.push(n);
    for (const n of hits) {
      const span = document.createElement("span");
      span.innerHTML = esc(n.nodeValue).replace(SPONSOR_ID, (id) => `<span class="cc-id">${id}</span>`);
      n.replaceWith(...span.childNodes);
    }
  }

  async function answer(spec) {
    if (!spec || spec.error) { D.say((spec && spec.error) || "I couldn't read what to plot.", { tag: "chart" }); return; }
    D.stream.showTyping("J");
    let m;
    try {
      m = spec.source === "dataset" ? await datasetModel(spec) : await liveModel(spec);
    } catch (e) {
      D.stream.hideTyping("J");
      D.say(e.status === 503 ? `I can't draw that here: ${e.message}` : e.message, { tag: "error" });
      return;
    }
    D.stream.hideTyping("J");
    D.say(explain(m), { tag: "chart", ids: m.ids, visual: cardHtml(m), cls: "cc", bind: (li) => bind(li, m) });
  }

  return { answer };
}
