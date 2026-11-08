// Two nights: the same pump trip played twice, side by side.
//
// Left: nobody acts (the baseline forecast). Right: the plan (approved, or the cheapest
// one that keeps every well online). The 3D tanks show the plan's water, with a red
// ghost column at the level the tank would reach if nobody acted. Both ledgers run on
// the backend's own forecasts: every number at the end is the forecast's own total.
//
// All state lives here. main.js passes hooks and adds one button; nothing else changes.

import * as THREE from "three";
import { holdButton } from "./fx.js";

const MIN_PER_SEC = 12;   // 6 hours in 30 seconds
const SLOW_NEAR = 5;      // minutes either side of an event that play slower...
const SLOW_RATE = 3;      // ...at this many minutes per second
const GHOST = new THREE.Color(0xff4b1f);

const firstSentence = (s) => (String(s).match(/^.*?\.(?=\s|$)/) || [String(s)])[0];
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const reduced = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Linear interpolation of a forecast timeline, so levels glide instead of stepping every 5 min.
function levelAt(fc, tankId, t) {
  const rows = fc.timeline;
  let i = 0;
  while (i < rows.length - 1 && rows[i + 1].t_min <= t) i++;
  const a = rows[i], b = rows[Math.min(i + 1, rows.length - 1)];
  const va = a.tanks[tankId], vb = b.tanks[tankId];
  if (va == null) return null;
  if (vb == null || b.t_min === a.t_min) return va;
  const f = Math.min(1, Math.max(0, (t - a.t_min) / (b.t_min - a.t_min)));
  return va + (vb - va) * f;
}

function wellsAt(fc, t) {
  let w = fc.timeline[0].wells_online;
  for (const r of fc.timeline) if (r.t_min <= t && r.wells_online != null) w = r.wells_online;
  return w;
}

// Running cost: lost production accrues per offline or choked well from the minute it
// goes down; trucking and disposal accrue from the first truck. The shape comes from the
// forecast's events and actions; the curve is scaled so it lands exactly on fc.cost.total_usd.
function costCurve(fc, actions, field, horizon) {
  const rate = Object.fromEntries((field.wells || []).map((w) => [w.id, w.lost_usd_hr || 0]));
  const parts = [];
  for (const e of fc.events || []) {
    if (e.type !== "SHUTDOWN_LEVEL") continue;
    for (const w of e.wells_lost || []) parts.push({ from: e.t_min, per: (rate[w] || 0) / 60 });
  }
  for (const a of actions) {
    if (a.type === "CHOKE_WELL") parts.push({ from: a.at_min, per: ((rate[a.asset_id] || 0) * (1 - (Number(a.value) || 0))) / 60 });
  }
  const c = fc.cost || {};
  const trucks = actions.filter((a) => a.type === "SEND_TRUCK").map((a) => a.at_min);
  const t0 = trucks.length ? Math.min(...trucks) : 0;
  const fixed = (c.trucking_usd || 0) + (c.disposal_usd || 0);
  const lost = (t) => parts.reduce((s, p) => s + Math.max(0, t - p.from) * p.per, 0);
  const lostEnd = lost(horizon);
  const kLost = lostEnd > 0 ? (c.lost_production_usd || 0) / lostEnd : 0;
  const raw = (t) => kLost * lost(t) + (t <= t0 ? 0 : fixed * Math.min(1, (t - t0) / Math.max(1, horizon - t0)));
  const total = c.total_usd || 0;
  const end = raw(horizon);
  if (end > 0) return (t) => (raw(t) * total) / end;
  return (t) => total * Math.min(1, t / horizon);
}

export function mountTwoNights(h) {
  const row = document.querySelector(".timebar-row");
  const host = document.querySelector(".timebar") && document.querySelector(".timebar").parentElement;
  if (!row || !host) return;

  const btn = document.createElement("button");
  btn.className = "twobtn";
  btn.type = "button";
  btn.textContent = "Two nights";
  btn.title = "Replay tonight twice: nobody acts, and the plan. Same pump trip, same water.";
  row.appendChild(btn);

  const box = document.createElement("section");
  box.id = "twonights";
  box.hidden = true;
  box.setAttribute("aria-label", "Two nights replay");
  host.appendChild(box);

  let run = null; // { plan, none, with, horizon, cost, events, t, playing, raf, ghosts, saved }

  function pickPlan() {
    const approved = h.approved();
    const list = h.plans();
    const plan = approved || list.find((p) => p.keeps_running !== false) || list[0] || null;
    return plan && withDeadline(plan);
  }

  // Last Call knows how long each set of levers keeps working. Match the plan to the Last Call
  // option that pulls the same levers (same asset IDs), and borrow its "start by" minute.
  function withDeadline(plan) {
    if (plan.expires_min != null) return plan;
    const lc = h.lastCall();
    const key = (ids) => [...new Set(ids)].sort().join(",");
    const want = key(plan.actions.map((a) => a.asset_id));
    const opts = [...((lc && lc.options) || []).map((o) => ({ ...o, ids: (o.steps || []).map((s) => s.asset_id) })),
      ...((lc && lc.ladder) || []).map((o) => ({ ...o, ids: o.asset_ids || [] }))];
    const hit = opts.find((o) => key(o.ids) === want && o.expires_min != null);
    return hit ? { ...plan, expires_min: hit.expires_min, expires_clock: hit.expires_clock } : plan;
  }

  function buildEvents(plan, none, withFc, horizon, field) {
    const nice = h.nice, clock = h.clockAt;
    const inc = h.incident();
    const L = [], R = [];
    const opening = inc ? `${inc.summary}` : "The night starts.";
    L.push({ t: 0, text: `${clock(0)} ${opening} Nobody hears it.` });
    R.push({ t: 0, text: `${clock(0)} ${opening} Gaia flags it and pages the on-call phone.`, good: true });
    for (const e of none.events || []) {
      if (e.type !== "SHUTDOWN_LEVEL") continue;
      const n = (e.wells_lost || []).length;
      L.push({ t: e.t_min, bad: true, text: `${clock(e.t_min)} ${nice(e.asset_id)} reaches shutdown. ${n} well${n === 1 ? "" : "s"} shut in.` });
    }
    const acts = [...plan.actions].sort((a, b) => a.at_min - b.at_min);
    for (const a of acts) R.push({ t: a.at_min, text: `${clock(a.at_min)} ${h.describe(a)}.` });
    for (const e of withFc.events || []) {
      if (e.type !== "SHUTDOWN_LEVEL") continue;
      R.push({ t: e.t_min, bad: true, text: `${clock(e.t_min)} ${nice(e.asset_id)} still reaches shutdown.` });
    }
    // The closest call on the plan side: the tank that comes nearest its shutdown line.
    let close = null;
    for (const tk of field.tanks || []) {
      for (const r of withFc.timeline) {
        const v = r.tanks[tk.id];
        if (v == null) continue;
        const room = tk.shutdown_pct - v;
        if (!close || room < close.room) close = { id: tk.id, room, v, t: r.t_min };
      }
    }
    if (close && close.room > 0) {
      const text = close.t >= horizon
        ? `${clock(close.t)} ${nice(close.id)} is at ${close.v.toFixed(1)}%, still ${close.room.toFixed(1)} points under shutdown.`
        : `${clock(close.t)} Closest call: ${nice(close.id)} peaks at ${close.v.toFixed(1)}%, ${close.room.toFixed(1)} points under shutdown.`;
      R.push({ t: close.t, good: true, text });
    }
    if (plan.expires_min != null && plan.expires_min > 0) {
      R.push({ t: plan.expires_min, good: true, text: `${clock(plan.expires_min)} Last Call: after this minute the plan no longer works. It started in time.` });
    }
    L.sort((a, b) => a.t - b.t);
    R.sort((a, b) => a.t - b.t);
    const stops = [...new Set([...L, ...R].map((e) => e.t).filter((t) => t > 0 && t < horizon))];
    return { L, R, stops };
  }

  function addGhosts() {
    // An x-ray column (drawn through the tank walls) plus a bright waterline on top: the
    // level the tank would reach tonight if nobody acted. The real water mesh keeps the plan's level.
    const ghosts = [];
    for (const tk of h.field().tanks || []) {
      const m = h.levelMesh(tk.id);
      if (!m || !m.parent) continue;
      m.geometry.computeBoundingBox();
      const bb = m.geometry.boundingBox;
      const height = bb.max.y - bb.min.y;
      const xray = { color: GHOST, transparent: true, depthWrite: false, depthTest: false };
      const colMat = new THREE.MeshBasicMaterial({ ...xray, opacity: 0.14 });
      const capMat = new THREE.MeshBasicMaterial({ ...xray, opacity: 0.9 });
      const col = new THREE.Mesh(m.geometry, colMat);
      const cap = new THREE.Mesh(m.geometry, capMat);
      for (const g of [col, cap]) {
        g.position.copy(m.position);
        g.quaternion.copy(m.quaternion);
        g.scale.set(m.scale.x * 1.06, 0.001, m.scale.z * 1.06);
        g.renderOrder = 10;
        m.parent.add(g);
      }
      col.name = `${tk.id}_GHOST`;
      cap.name = `${tk.id}_GHOST_LINE`;
      ghosts.push({ id: tk.id, shutdown: tk.shutdown_pct, col, cap, mats: [colMat, capMat], level: m, y0: m.position.y, height });
    }
    return ghosts;
  }

  function removeGhosts() {
    if (!run) return;
    for (const g of run.ghosts) {
      for (const m of [g.col, g.cap]) m.parent && m.parent.remove(m);
      for (const mat of g.mats) mat.dispose();
    }
    run.ghosts = [];
  }

  function frameAt(t, now) {
    const r = run;
    h.setTime(Math.round(t / 5) * 5);
    const CAP = 0.025;
    for (const g of r.ghosts) {
      const lv = levelAt(r.none, g.id, t);
      const pv = levelAt(r.with, g.id, t);
      if (pv != null) g.level.scale.y = Math.max(0.001, pv / 100);
      const show = lv != null && (pv == null || lv - pv > 0.5); // only where the two nights differ
      g.col.visible = g.cap.visible = show;
      if (!show) continue;
      const f = lv / 100;
      g.col.scale.y = Math.max(0.001, f);
      g.cap.scale.y = CAP;
      g.cap.position.y = g.y0 + g.height * Math.max(0, f - CAP);
      const over = lv >= g.shutdown - 0.05;
      g.mats[1].opacity = over ? 0.55 + 0.45 * (0.5 + 0.5 * Math.sin(now / 140)) : 0.9;
      g.mats[0].opacity = over ? 0.22 : 0.14;
    }
    paint(t);
  }

  function paint(t) {
    const r = run;
    const pct = (100 * t) / r.horizon;
    box.querySelector(".tn-clock").textContent = h.clockAt(t);
    box.querySelector(".tn-bar i").style.width = `${pct}%`;
    for (const side of ["none", "with"]) {
      const fc = r[side];
      const col = box.querySelector(`.tn-col.${side}`);
      col.querySelector(".tn-usd").textContent = h.usd(r.cost[side](t));
      const w = wellsAt(fc, t);
      col.querySelector(".tn-wells").textContent = `${w} of ${r.wellsTotal} wells online`;
      col.querySelector(".tn-wells").classList.toggle("down", w < r.wellsTotal);
      const list = side === "none" ? r.events.L : r.events.R;
      const lis = col.querySelectorAll(".tn-log li");
      list.forEach((e, i) => lis[i] && lis[i].classList.toggle("on", e.t <= t));
    }
    const done = t >= r.horizon;
    box.querySelector(".tn-end").hidden = !done;
    box.querySelector("[data-tn=play]").textContent = done ? "Replay" : r.playing ? "Pause" : "Play";
  }

  function step(now) {
    const r = run;
    if (!r || !r.playing) return;
    const dt = Math.min(0.1, (now - (r.last || now)) / 1000);
    r.last = now;
    const near = r.events.stops.some((s) => Math.abs(s - r.t) < SLOW_NEAR);
    r.t = Math.min(r.horizon, r.t + dt * (near ? SLOW_RATE : MIN_PER_SEC));
    frameAt(r.t, now);
    if (r.t >= r.horizon) { r.playing = false; finish(); return; }
    r.raf = requestAnimationFrame(step);
  }

  function play() {
    if (!run) return;
    if (run.t >= run.horizon) run.t = 0;
    run.playing = true;
    run.last = 0;
    cancelAnimationFrame(run.raf);
    run.raf = requestAnimationFrame(step);
    paint(run.t);
  }

  function pause() {
    if (!run) return;
    run.playing = false;
    cancelAnimationFrame(run.raf);
    paint(run.t);
  }

  function finish() {
    const r = run;
    if (r.logged) return;
    r.logged = true;
    const n = r.wellsTotal - wellsAt(r.none, r.horizon);
    h.log(`Replayed both nights. Nobody acting: ${h.usd(r.none.cost.total_usd)} and ${n} wells down. "${firstSentence(r.plan.summary)}": ${h.usd(r.with.cost.total_usd)}.`);
  }

  function render() {
    const r = run;
    const nTot = r.none.cost.total_usd, wTot = r.with.cost.total_usd;
    const lostWells = r.wellsTotal - wellsAt(r.none, r.horizon);
    const keeps = wellsAt(r.with, r.horizon) === r.wellsTotal;
    const li = (e) => `<li class="${e.bad ? "bad" : e.good ? "good" : ""}">${esc(e.text)}</li>`;
    box.innerHTML = `
      <div class="tn-top">
        <div class="kicker">Two nights · same pump trip, same water</div>
        <div class="tn-head">
          <b class="tn-clock">${h.clockAt(0)}</b>
          <span class="tn-ctl"><button type="button" data-tn="play">Pause</button><button type="button" data-tn="close" aria-label="Close">Close</button></span>
        </div>
        <div class="tn-bar" title="Click to jump"><i></i></div>
        <div class="tn-legend"><span class="sw ghost"></span>Red: water if nobody acts <span class="sw water"></span>Water: with the plan</div>
      </div>
      <section class="tn-col none">
        <div class="kicker">Nobody acts</div>
        <div class="tn-usd">$0</div>
        <div class="tn-wells"></div>
        <ol class="tn-log">${r.events.L.map(li).join("")}</ol>
      </section>
      <section class="tn-col with">
        <div class="kicker">With Gaia</div>
        <div class="tn-usd">$0</div>
        <div class="tn-wells"></div>
        <ol class="tn-log">${r.events.R.map(li).join("")}</ol>
      </section>
      <div class="tn-end" hidden>
        <div class="tn-verdict">Same pump trip, same water. <b>${h.usd(Math.max(0, nTot - wTot))}</b>${lostWells ? ` and ${lostWells} well${lostWells === 1 ? "" : "s"}` : ""} apart.</div>
        <p>Nobody acting costs ${h.usd(nTot)}${lostWells ? ` and shuts in ${lostWells} well${lostWells === 1 ? "" : "s"}` : ""}. "${esc(firstSentence(r.plan.summary))}" costs ${h.usd(wTot)}${keeps ? " and keeps every well online" : ""}.${r.plan.expires_clock ? ` It only works if it starts by ${esc(r.plan.expires_clock)}.` : ""}</p>
        ${h.approved() ? "" : `<button type="button" class="primary hold" data-tn="approve"><i></i><span>Hold to approve this plan</span></button>`}
      </div>`;
    box.querySelector("[data-tn=play]").onclick = () => (run.playing ? pause() : play());
    box.querySelector("[data-tn=close]").onclick = close;
    box.querySelector(".tn-bar").onclick = (e) => {
      const rect = e.currentTarget.getBoundingClientRect();
      const f = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      run.t = (f > 0.98 ? 1 : f) * run.horizon;
      frameAt(run.t, performance.now());
      if (run.t >= run.horizon) finish();
    };
    const ap = box.querySelector("[data-tn=approve]");
    if (ap) holdButton(ap, () => { const plan = run.plan; close({ keepPlan: true }); h.approve(plan); });
  }

  async function open() {
    if (run) return;
    const plan = pickPlan();
    if (!plan) { btn.title = "No plan to replay yet."; return; }
    btn.disabled = true;
    btn.textContent = "Two nights…";
    const saved = { actions: h.getActions(), t: h.getTime() };
    try {
      await h.setActions(plan.actions.map((a) => ({ ...a })), true);
      const withFc = h.fc();
      const none = h.baseline();
      if (withFc === none) throw new Error("the plan forecast did not load");
      const field = h.field();
      const horizon = none.timeline[none.timeline.length - 1].t_min;
      run = {
        plan, none, with: withFc, horizon, saved, t: 0, playing: false,
        wellsTotal: none.timeline[0].wells_online ?? (field.wells || []).length,
        cost: { none: costCurve(none, [], field, horizon), with: costCurve(withFc, plan.actions, field, horizon) },
        events: buildEvents(plan, none, withFc, horizon, field),
      };
      run.ghosts = addGhosts();
      h.focusIncident();
      render();
      box.hidden = false;
      document.body.classList.add("two-nights");
      if (reduced()) { run.t = run.horizon; frameAt(run.t, 0); finish(); } else play();
    } catch (err) {
      console.warn("two nights", err);
      run = null;
    } finally {
      btn.disabled = false;
      btn.textContent = "Two nights";
    }
  }

  function close(opts = {}) {
    if (!run) return;
    pause();
    removeGhosts();
    const saved = run.saved;
    run = null;
    box.hidden = true;
    box.innerHTML = "";
    document.body.classList.remove("two-nights");
    if (!opts.keepPlan) h.setActions(saved.actions, true);
    h.setTime(opts.keepPlan ? 0 : saved.t);
  }

  btn.onclick = open;
  window.addEventListener("keydown", (e) => {
    if (!run || e.target.closest("input, textarea")) return;
    if (e.key === "Escape") close();
    else if (e.key === " ") { e.preventDefault(); run.playing ? pause() : play(); }
  });
  window.addEventListener("freeboard:twonights", open);
  return { open, close };
}
