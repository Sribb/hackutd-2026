// The guided judge tour (#158, part of the judge flow #147). /start ends with "go to the
// console", which opens /?tour=1. Eight small cards walk the judge through one night shift,
// using the console's own features and its own numbers (nothing here makes a number up):
//
//   1 field   you are the night-shift operator: the 3D field, drag to look around
//   2 look    a short camera tour: a tank, a pump, the disposal well (view.focus, then reset)
//   3 gaia    the chat; one-click questions go through the real composer
//   4 alarm   demo backend: the pump trip the demo uses (POST /api/live/faults), or the incident
//             already open. Dataset mode (EOG data, a recording): replay from 15 min before the
//             first pump failure the backend found (eog.js), and the alarm arrives by itself.
//             Then window.gaiaNotify(alert) if #154 is in, else the incident opens in the chat
//   5 clock   the status line countdown and the time bar legend; drag the slider
//   6 fix     the planner's best plan and its dollars. Demo: its first step becomes a PENDING
//             proposal (POST /api/actions) and the judge holds to confirm on the fix card.
//             Dataset mode: the judge holds to approve the plan in the console's plans panel
//   7 phone   window.gaiaPhone (#163): real texts and calls, or the console's pager QR
//   8 done    a recap, and /dataset for any EOG folder, test/ included; pauses the replay clock
//
// Progress is in sessionStorage (gaia.tour), so a reload resumes; ?tour=1 starts over.
// The card never covers the time bar or the composer (place.js). window.__tour reports
// the step for headless checks.

import "./tour.css";
import { BASE } from "../api.js";
import { post } from "../console/auth.js";
import { esc } from "../console/stream.js";
import { usd, clock12, toMin, andList } from "../console/agents.js";
import { highlight } from "../highlight.js";
import { readPhone, prettyUsPhone } from "../start/phone.js";
import { firstPumpFailure, setClock, fixDataset } from "./eog.js";
import { visible, drawRings, placeCard } from "./place.js";

const KEY = "gaia.tour";
const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const $ = (sel) => document.querySelector(sel);
const lc = (s) => String(s ?? "").toLowerCase();
const nice = (id) => lc(String(id || "").replace(/_/g, " "));
const q = (s) => `“${esc(lc(s))}”`;
const C = () => window.__console;
const load = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || "null"); } catch { return null; } };
const save = (v) => { try { sessionStorage.setItem(KEY, JSON.stringify(v)); } catch { /* private mode: no resume */ } };

export function maybeTour() {
  try {
    const u = new URL(location.href);
    if (u.searchParams.get("tour") === "1") {
      save({ step: 0 });
      replay(null);
      u.searchParams.delete("tour"); // a reload resumes instead of starting over
      history.replaceState(history.state, "", u.pathname + (u.searchParams.toString() ? `?${u.searchParams}` : "") + u.hash);
    }
  } catch { /* leave the address as it is */ }
  const s = load();
  if (!s || s.done) return;
  whenReady().then((ok) => { if (ok) start(Math.max(0, Math.min(STEPS.length - 1, Number(s.step) || 0))); });
}

async function whenReady() {
  for (let i = 0; i < 400; i++) {
    const intro = window.__gaiaIntro ? window.__gaiaIntro.state() : "gone";
    if (window.__freeboard && C() && intro === "gone") return true;
    await sleep(150);
  }
  return false;
}

// ------------------------------------------------------------ backend ----
async function getJson(path) {
  const r = await fetch(`${BASE}${path}`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
let fieldP = null;
const field = () => (fieldP = fieldP || getJson("/api/field").catch(() => null));
// The field's own lists first; else the model's IDs (TANK_3, or a sponsor ID like S2_TANK_A in dataset mode).
function idsOf(kind, word, f) {
  const fromField = f && Array.isArray(f[kind]) ? f[kind].map((x) => x && x.id).filter(Boolean) : [];
  const re = new RegExp(`(^|_)${word}(_|$)`);
  const fromModel = window.__freeboard ? [...window.__freeboard.assets.keys()].filter((id) => re.test(id)) : [];
  return fromField.length ? fromField : fromModel;
}
const PRIO = { P1: 0, P2: 1, P3: 2 };
// Dataset mode (#153): /api/field says mode "dataset" and the console shows the judge's EOG data.
const isDataset = async () => { const f = await field(); return !!(f && f.mode === "dataset"); };
// The failure this tab's tour replayed (so back and next never move the clock twice).
const REPLAY = "gaia.tour.replay";
function replay(v) {
  try {
    if (v === undefined) return JSON.parse(sessionStorage.getItem(REPLAY) || "null");
    if (v) sessionStorage.setItem(REPLAY, JSON.stringify(v)); else sessionStorage.removeItem(REPLAY);
  } catch { /* private mode */ }
  return null;
}
async function openIncident(want) {
  const { alerts = [] } = await getJson("/api/alerts");
  const roots = alerts.filter((a) => a.active !== false && (!a.root_id || a.root_id === a.id) && !a.shelved_until && (!want || want(a)));
  roots.sort((x, y) => (PRIO[x.priority] ?? 9) - (PRIO[y.priority] ?? 9) || (x.consequence_t ?? 1e9) - (y.consequence_t ?? 1e9));
  return roots[0] || null;
}

// ----------------------------------------------------------- the card ----
let card = null, rings = [], step = 0, token = 0, timers = [], cleanups = [], targets = [], soft = true;
const alive = (t) => t === token && !!card;
window.__tour = { step: () => (card ? STEPS[step].id : null), index: () => (card ? step : -1), active: () => !!card, notified: [] };

function start(i) {
  if (card) return;
  card = document.createElement("section");
  card.className = "gt-card";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-label", "guided tour");
  card.innerHTML = `<div class="gt-head"><span class="gt-mark" aria-hidden="true">gaia</span><span class="gt-count"></span></div>
    <h2 class="gt-title"></h2><div class="gt-body" aria-live="polite"></div><div class="gt-extra"></div>
    <div class="gt-nav"><button type="button" class="gt-skip" data-skip>skip tour</button><span class="gt-grow"></span>
      <button type="button" class="gt-btn" data-back>back</button><button type="button" class="gt-btn primary" data-next>next</button></div>
    <div class="gt-pillmsg"><span>tour paused while the pager is open</span><button type="button" class="gt-btn" data-unpager>back to the tour</button></div>`;
  document.body.appendChild(card);
  card.querySelector("[data-unpager]").onclick = () => { closePager(); frame(); };
  card.querySelector("[data-skip]").onclick = () => end("skip");
  card.querySelector("[data-back]").onclick = () => go(step - 1);
  card.querySelector("[data-next]").onclick = () => (step === STEPS.length - 1 ? end("end") : go(step + 1));
  addEventListener("resize", place);
  tick = setInterval(frame, 200);
  go(i);
}
let tick = 0;

function end(how) {
  leave();
  save({ done: true });
  // Dataset mode: "end tour" and "skip tour" pause the replay clock.
  if (BASE && (how === "end" || how === "skip")) {
    isDataset().then((on) => { if (on) setClock({ play: false }).catch(() => {}); });
  }
  clearInterval(tick);
  removeEventListener("resize", place);
  for (const r of rings) r.remove();
  rings = [];
  if (card) card.remove();
  card = null;
}

function leave() {
  token += 1;
  for (const t of timers) { clearTimeout(t); clearInterval(t); }
  timers = [];
  for (const fn of cleanups.splice(0)) { try { fn(); } catch (e) { console.warn("tour cleanup", e); } }
  const s = STEPS[step];
  if (card && s && s.leave) { try { s.leave(); } catch (e) { console.warn("tour leave", e); } }
}

function go(i) {
  if (!card || i < 0 || i >= STEPS.length) return;
  leave();
  step = i;
  save({ step: i });
  const s = STEPS[i];
  const t = token;
  card.classList.remove("in");
  card.dataset.step = s.id;
  card.querySelector(".gt-count").textContent = `${i + 1} of ${STEPS.length}`;
  card.querySelector(".gt-title").textContent = s.title;
  card.querySelector("[data-back]").hidden = i === 0;
  card.querySelector("[data-skip]").hidden = i === STEPS.length - 1;
  card.querySelector("[data-next]").textContent = i === STEPS.length - 1 ? "end tour" : "next";
  body("");
  extra("");
  setTargets(s.targets || [], s.soft !== false);
  const ctx = {
    alive: () => alive(t), body, extra, targets: (list, sf = true) => { if (alive(t)) setTargets(list, sf); },
    every: (ms, fn) => { const h = setInterval(() => { if (alive(t)) fn(); }, ms); timers.push(h); fn(); },
    onLeave: (fn) => cleanups.push(fn),
  };
  Promise.resolve().then(() => s.enter(ctx)).catch((e) => { console.warn("tour step", s.id, e); if (alive(t)) body(`<p>this part of the tour could not run here (${esc(lc(e.message || e))}). press next to go on.</p>`); });
  requestAnimationFrame(() => { if (!card) return; place(); card.classList.add("in"); const n = card.querySelector("[data-next]"); if (!document.activeElement || document.activeElement === document.body || card.contains(document.activeElement)) n.focus({ preventScroll: true }); });
}

function body(html) { if (card) { const b = card.querySelector(".gt-body"); if (b._h !== html) { b.innerHTML = html; b._h = html; place(); } } }
function extra(html, bind) {
  if (!card) return;
  const x = card.querySelector(".gt-extra");
  if (x._h !== html) { x.innerHTML = html; x._h = html; if (bind) bind(x); place(); }
}

// ------------------------------------------------------- highlighting ----
function setTargets(list, sf) { targets = list; soft = sf; frame(); }
const els = () => visible(targets);
function frame() {
  if (!card) return;
  drawRings(rings, els());
  pill(!!$(".pager"));
  place();
}
const place = () => { if (card) placeCard(card, soft ? els() : []); };

// The pager overlay covers the screen: the card folds to a small pill in the corner until it closes.
function pill(on) { if (card) card.classList.toggle("pill", on); }
// Through the pager's own close button, so it also drops its Escape listener (main.js).
function closePager() { const p = $(".pager"); if (!p) return; const b = p.querySelector(".pager-card > button"); if (b) b.click(); else p.remove(); }

// --------------------------------------------------------------- steps ----
function sideOf() {
  const st = $("#stage"), chat = $("#csJarvis");
  if (!st || !chat) return { field: "here", chat: "here" };
  const s = st.getBoundingClientRect(), c = chat.getBoundingClientRect();
  return s.left >= c.right - 4 ? { field: "on the right", chat: "on the left" } : { field: "on top", chat: "below the field" };
}
const btns = (list) => list.map(([attr, label, cls = ""]) => `<button type="button" class="gt-btn ${cls}" ${attr}>${esc(label)}</button>`).join("");
const fly = (id) => { const v = C().view(); if (v && v.focus) v.focus(id); highlight.flash("jarvis", [id], 6000); };
const latestWatch = () => [...document.querySelectorAll('#csStream li[data-agent="W"]:not([hidden]):not(.typing)')].pop() || null;
const notified = new Set();

const STEPS = [
  {
    id: "field", title: "you are the night-shift operator", targets: ["#stage"],
    enter(ctx) {
      const name = lc(($("#csFieldName") || {}).textContent || "the lease");
      const now = lc(($("#csClock") || {}).textContent || "");
      body(`<p>it's ${esc(now)} at ${esc(name)}, and you are on shift alone. the field ${sideOf().field} is your field in 3d, just built from your blueprint.</p>
        <p>drag to look around, scroll to zoom, and click anything to see what it is.</p>`);
      void ctx;
    },
  },
  {
    id: "look", title: "a quick look around", targets: ["#stage"],
    async enter(ctx) {
      const f = await field();
      const inc = await openIncident().catch(() => null);
      const tanks = idsOf("tanks", "TANK", f), pumps = idsOf("pumps", "PUMP", f), swds = idsOf("disposal_wells", "SWD", f);
      const pick = (list, want) => (want && list.includes(want) ? want : list[0]);
      const stops = [
        ["tank", pick(tanks, inc && inc.consequence_asset_id), (n) => `${n}: produced water from the wells collects in the tanks. when a tank reaches its shutdown level, the wells feeding it stop.`],
        ["pump", pick(pumps, inc && inc.asset_id), (n) => `${n}: the pumps empty the tanks and send the water on to disposal.`],
        ["disposal well", swds[0], (n) => `${n}: the disposal well, where the water goes back deep underground.`],
      ].filter((s) => s[1]);
      if (!ctx.alive()) return;
      let run = 0; // a click on a name stops the automatic flight
      const live = (r) => ctx.alive() && r === run;
      const show = (k) => {
        const [, id, line] = stops[k];
        fly(id);
        body(`<p>${esc(line(nice(id)))}</p>`);
        chips(k, false);
      };
      const chips = (on, again) => extra(btns(stops.map(([kind], k) => [`data-stop="${k}"`, kind, k === on ? "on" : ""])) + (again ? btns([["data-again", "play it again"]]) : ""), (x) => {
        x.querySelectorAll("[data-stop]").forEach((b) => { b.onclick = () => { run += 1; show(Number(b.dataset.stop)); }; });
        const a = x.querySelector("[data-again]");
        if (a) a.onclick = () => auto();
      });
      const auto = async () => {
        const r = ++run;
        for (let k = 0; k < stops.length; k++) {
          if (!live(r)) return;
          show(k);
          await sleep(3400);
        }
        if (!live(r)) return;
        C().view().reset();
        body("<p>that's the whole field. click a name to fly there again, or press next to meet gaia.</p>");
        chips(-1, true);
      };
      if (!stops.length) { body("<p>this field has no tanks, pumps or disposal wells in the model to fly to.</p>"); return; }
      if (reduced) { body("<p>gaia can fly you to each part of the field. pick one:</p>"); chips(-1, false); return; }
      body("<p>gaia flies you around the field.</p>");
      await sleep(700);
      auto();
    },
    leave() { const v = C() && C().view(); if (v && v.reset) v.reset(); },
  },
  {
    id: "gaia", title: "this is gaia", targets: ["#csJarvis"],
    enter(ctx) {
      const cd = C().countdown();
      const asks = [["what's failing?"]].concat(cd ? [[`how long until ${nice(cd.asset_id)} shuts down?`]] : []);
      body(`<p>the chat ${sideOf().chat} is gaia. its watch, forecast and planner agents post here on their own, from the live data. you can ask anything, by typing or by voice.</p>`);
      const n0 = document.querySelectorAll('#csStream li[data-agent="J"]:not(.typing)').length;
      extra(`<span class="gt-note">try one:</span>${asks.map(([s], k) => `<button type="button" class="gt-btn" data-ask="${k}">${esc(s)}</button>`).join("")}`, (x) => {
        x.querySelectorAll("[data-ask]").forEach((b) => { b.onclick = () => { ask(asks[Number(b.dataset.ask)][0]); b.disabled = true; note(x, "sent through the chat. gaia's answer lands there."); }; });
      });
      ctx.every(500, () => {
        const n = document.querySelectorAll('#csStream li[data-agent="J"]:not(.typing)').length;
        const x = card && card.querySelector(".gt-extra");
        if (n > n0 && x && x.querySelector(".gt-sent")) note(x, "gaia answered in the chat. ask it anything else, or press next.");
      });
    },
  },
  {
    id: "alarm", title: "tonight, something breaks", targets: ["#csWatch", latestWatch],
    async enter(ctx) {
      body("<p>at night a pump can trip with nobody watching. gaia's watch agent is.</p>");
      if (!BASE) { body("<p>this screen has no backend, so there are no live alarms to show. start it with npm run demo.</p>"); return; }
      const dataset = await isDataset();
      let a = dataset ? null : await openIncident();
      if (!ctx.alive()) return;
      if (dataset) {
        // EOG's data is a recording: replay it from just before the first pump failure the backend found.
        const lead = "<p>this is a real week from eog's field data. we're replaying it from just before a pump failed.</p>";
        body(lead);
        const was = replay();
        const ev = was || await firstPumpFailure().catch((e) => { console.warn("tour eog events", e); return null; });
        if (!ctx.alive()) return;
        if (!ev) { body(`${lead}<p>the backend found no pump failure in this data, so there is nothing to replay. press next.</p>`); return; }
        if (!was) {
          const r = await setClock({ at: ev.onset_unix - 900, play: true, speed: 3 }).catch(() => null);
          if (!ctx.alive()) return;
          if (!r || !r.ok) { body(`${lead}<p>the replay clock didn't move (${r ? `http ${r.status}` : "no backend"}${r && r.status === 401 ? ": open the console with its #k= link" : ""}). press next to go on.</p>`); return; }
          replay({ asset_id: ev.asset_id, facility_id: ev.facility_id, onset_unix: ev.onset_unix });
        }
        body(`${lead}<p>${esc(nice(ev.asset_id))}${ev.facility_id ? ` at ${esc(nice(ev.facility_id))}` : ""} is still running. watch the status line: the alarm comes in on its own.</p>`);
        // The failure itself, not an earlier "wearing out" alert on the same pump.
        const mine = (x) => !!x && (x.id === ev.event_id || (x.asset_id === ev.asset_id && /FAIL|TRIP/i.test(x.kind || "")));
        for (let k = 0; k < 90 && !mine(a) && ctx.alive(); k++) { await sleep(500); a = await openIncident(mine).catch(() => null); }
        if (!ctx.alive()) return;
        if (!mine(a)) { body(`${lead}<p>no alarm for ${esc(nice(ev.asset_id))} yet. watch the status line, or press next.</p>`); return; }
      } else if (!a) {
        const f = await field();
        const pumps = idsOf("pumps", "PUMP", f);
        const pump = pumps.includes("PUMP_3") ? "PUMP_3" : pumps[0];
        if (!pump) { body("<p>this field has no pump to trip.</p>"); return; }
        await sleep(reduced ? 0 : 900);
        if (!ctx.alive()) return;
        body(`<p>tripping ${esc(nice(pump))} in the simulator, the way the demo does…</p>`);
        const r = await post(`${BASE}/api/live/faults`, { asset_id: pump, type: "TRIP" });
        if (!r.ok) { if (ctx.alive()) body(`<p>the simulator refused the trip (http ${r.status}). nothing changed. press next to go on.</p>`); return; }
        for (let k = 0; k < 40 && !a && ctx.alive(); k++) { await sleep(500); a = await openIncident().catch(() => null); }
        if (!ctx.alive()) return;
        if (!a) { body("<p>the pump is tripped, but no alarm came in yet. watch the status line, or press next.</p>"); return; }
      }
      const notify = typeof window.gaiaNotify === "function";
      if (!notified.has(a.id)) {
        notified.add(a.id);
        const rec = { id: a.id, via: "chat", at: Date.now() };
        window.__tour.notified.push(rec);
        if (notify) {
          // #154 shows its own notification for a new alert (once per id). Give it ~6 s; call
          // window.gaiaNotify only if none appeared, so the judge never sees the alarm twice.
          const st = () => (typeof C().notify === "function" && C().notify()) || {};
          const seen0 = new Set(st().seen || []);
          let came = false;
          for (let k = 0; k < 24 && ctx.alive(); k++) {
            const n = st();
            if ((n.shown && n.id === a.id) || ((n.seen || []).includes(a.id) && !seen0.has(a.id))) { came = true; break; }
            await sleep(250);
          }
          if (!ctx.alive()) return;
          rec.via = came ? "console" : "gaiaNotify";
          if (!came) { try { window.gaiaNotify(a); } catch (e) { console.warn("gaiaNotify", e); } }
        }
        C().open();
        for (let k = 0; k < 20 && !C().threads().some((t) => t.id === a.id); k++) await sleep(250);
        if (!notify && ctx.alive()) C().thread(a.id);
        if (a.asset_id && ctx.alive()) fly(a.asset_id);
      }
      if (!ctx.alive()) return;
      const when = !a.clock ? "" : dataset ? `${a.clock} utc` : clock12(toMin(a.clock));
      body(`${dataset ? "<p>this is a real week from eog's field data, replayed from just before the pump failed.</p>" : ""}<p>${esc(lc(a.message || "an alarm came in."))}</p>
        <p>watch raised it as ${esc(lc(a.priority || a.severity || "an alarm"))}${when ? ` at ${esc(lc(when))}` : ""}. ${notify
          ? "this is how gaia alerts an operator: one big notification, then the whole incident in the chat."
          : "this is how gaia alerts an operator: the incident opens in the chat, and the phone pager buzzes."}</p>`);
      ctx.targets(["#csWatch", latestWatch, "#gaiaNotify:not([hidden])"]);
      ctx.every(500, () => extra($("#gaiaNotify:not([hidden])") ? `<span class="gt-note">“show me” on the alert flies to it. press next and the tour puts the alert away.</span>` : ""));
    },
    // #154's alert card holds the time bar down while it is up: put it away with its own button.
    leave() { const d = $("#gaiaNotify:not([hidden]) [data-gn=dismiss]"); if (d) d.click(); },
  },
  {
    id: "clock", title: "how long do we have", targets: ["#csForecast", ".timebar"],
    enter(ctx) {
      document.body.classList.add("gt-tb");
      const slider = $("#time");
      const moved = () => { const x = card && card.querySelector(".gt-readout"); if (x) x.textContent = readout(); };
      const readout = () => `the field at ${lc(($("#timeClock") || {}).textContent || "")}, ${lc(($("#timeRel") || {}).textContent || "")}`;
      if (slider) { slider.addEventListener("input", moved); ctx.onLeave(() => slider.removeEventListener("input", moved)); }
      ctx.every(1000, () => {
        const status = lc(($("#csForecast .t") || {}).textContent || "").trim();
        const keys = [...document.querySelectorAll("#timeKey .key")].map((k) => lc(k.textContent).trim()).filter(Boolean);
        body(`<p>the status line counts down: ${status ? q(status) : "no shutdown forecast yet"}.</p>
          ${keys.length ? `<p>the time bar marks ${keys.length === 1 ? "one time" : "two times"}: ${andList(keys.map(q))}.</p>` : ""}
          <p>drag the dot along the time bar to look ahead. the 3d field shows the forecast for that minute.</p>`);
        extra(`<span class="gt-note gt-readout">${esc(readout())}</span>`);
        moved();
      });
    },
    leave() {
      document.body.classList.remove("gt-tb");
      const s = $("#time");
      if (s && s.value !== "0") { s.value = "0"; s.dispatchEvent(new Event("input", { bubbles: true })); }
    },
  },
  {
    id: "fix", title: "the cheapest fix", targets: ["#csPlan", "#csFix"],
    async enter(ctx) {
      const b = C().brief();
      const plan = (b && b.plans && (b.plans.robust || b.plans.cheapest)) || null;
      if (await isDataset()) { if (ctx.alive()) fixDataset(ctx, b, plan, { body, extra, btns }); return; }
      // Late in the night the best plan can be "do nothing" (nothing saves the tank any more):
      // then the planner's cheapest plan that still has a step, from the same /api/plans list.
      let alt = null;
      if (!(plan && plan.actions && plan.actions.length) && BASE) {
        const r = await post(`${BASE}/api/plans`, {}).catch(() => null);
        const list = r && r.ok ? ((await r.json().catch(() => ({}))).plans || []) : [];
        alt = list.find((p) => (p.actions || []).length) || null;
        if (!ctx.alive()) return;
      }
      const action = (alt || plan || {}).actions ? (alt || plan).actions[0] : null;
      let proposing = false, said = "", sawConfirm = false;
      const head = (plan
        ? `<p>the planner's best plan costs ${esc(usd(plan.total_usd))}, against ${esc(usd(b.do_nothing_usd))} if nobody acts: ${esc(lc(plan.summary))}</p>`
        : "<p>the planner has no fix to make right now.</p>")
        + (alt ? `<p>its cheapest plan that still does something costs ${esc(usd(alt.total_usd ?? (alt.cost && alt.cost.total_usd)))}: ${esc(lc(alt.summary))}</p>` : "");
      const rule = "<p>gaia never moves equipment. it proposes a fix, and a person holds to confirm it.</p>";
      const propose = async () => {
        proposing = true;
        said = "proposing…";
        draw();
        const r = await post(`${BASE}/api/actions`, { type: action.type, asset_id: action.asset_id, value: action.value ?? null, target_id: action.target_id ?? null,
          reason: `Step 1 of the planner's ${alt ? "cheapest plan with a step" : "best plan"}, proposed from the guided tour. Nothing moves until a person holds to confirm.` }).catch(() => null);
        proposing = false;
        said = r && r.ok ? "" : `couldn't propose it (${r ? `http ${r.status}` : "no backend"}). nothing was sent.`;
        draw();
      };
      const draw = () => {
        const c = C().card();
        const st = c && c.status;
        if (st === "CONFIRMED") sawConfirm = true;
        let x;
        if (st === "PENDING") x = `<span class="gt-note">press and hold “hold to confirm” on the fix card for a second and a half.</span>`;
        else if (st === "SENDING") x = `<span class="gt-note">sending to the field…</span>`;
        else if (st) x = `<span class="gt-note">${esc(lc(($("#csFix .cs-fx-state") || {}).textContent || st))}</span>`;
        else if (sawConfirm) x = `<span class="gt-note">confirmed. the command went out because a person held the button, not because gaia decided.</span>`;
        else if (action && BASE) x = `${btns([["data-propose", "propose the first step", "primary"]])}<span class="gt-note">${esc(said || "nothing moves until you confirm.")}</span>`;
        else x = `<span class="gt-note">${BASE ? "the planner has no step to propose right now, so there is nothing to confirm. press next." : "without a backend there is nothing to propose."}</span>`;
        extra(x, (el) => { const p = el.querySelector("[data-propose]"); if (p) { p.disabled = proposing; p.onclick = propose; } });
        ctx.targets(st ? ["#csPlan", "#csFix"] : ["#csPlan"]);
        if (st && !draw.scrolled) { draw.scrolled = true; const f = $("#csFix"); if (f) f.scrollIntoView({ block: "nearest" }); }
      };
      body(head + rule);
      ctx.every(300, draw);
    },
  },
  {
    id: "phone", title: "your phone", targets: [".pager-pill"],
    async enter(ctx) {
      const ph = prettyUsPhone(readPhone());
      // #163: window.gaiaPhone says whether this server really texts and calls (provider "twilio").
      const gp = window.gaiaPhone;
      const sub = gp && typeof gp.subscription === "function" ? gp.subscription() : null;
      const provider = (sub && sub.provider) || (ph && gp && typeof gp.status === "function" ? ((await gp.status().catch(() => null)) || {}).provider : null) || "none";
      if (!ctx.alive()) return;
      const where = provider === "twilio" && (sub && sub.masked || ph)
        ? `gaia texts and calls ${esc(sub && sub.masked || ph)} when an alarm comes in. the phone pager works too.`
        : ph ? `you left ${esc(ph)}. texts and calls aren't set up on this server, so tonight the phone pager does it.`
          : "you didn't leave a number, so the phone pager is how alerts reach a phone tonight.";
      // The console's own pager (#161: the "phone pager" pill and window.__pager), else the classic Pager button.
      const btn = [...document.querySelectorAll("#side .brand-row button")].find((b) => /^pager$/i.test(b.textContent.trim()));
      const open = window.__pager && typeof window.__pager.open === "function" ? () => window.__pager.open() : btn ? () => btn.click() : null;
      const pill = !!$(".pager-pill");
      body(`<p>${where}</p>
        <p>open the pager${pill ? " (the “phone pager” button on the field)" : ""} and scan the code with your phone. it buzzes on every alert, and a fix can be confirmed from it with the same press and hold.</p>`);
      extra(open ? btns([["data-pager", "open the pager", "primary"]]) : `<span class="gt-note">the pager is not on this screen.</span>`, (x) => {
        const b = x.querySelector("[data-pager]");
        if (b) b.onclick = () => open();
      });
      void ctx;
    },
    leave() { closePager(); },
  },
  {
    id: "done", title: "you're the operator now", targets: [],
    enter() {
      body(`<p>that's the night shift: the field in 3d, gaia in the chat, an alarm, the countdown, the cheapest fix, and a person confirming it. every number on screen came from the console's own data, not from this tour.</p>
        <p>the same works on any eog folder, test/ included: <a href="/dataset">/dataset</a> shows the alerts, plans and trends for each one.</p>`);
    },
  },
];

function ask(text) {
  const input = $("#csInput"), form = $("#csAsk");
  if (!input || !form) return;
  input.value = text;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  form.requestSubmit();
}
function note(x, text) {
  let n = x.querySelector(".gt-sent");
  if (!n) { n = document.createElement("span"); n.className = "gt-note gt-sent"; x.appendChild(n); }
  n.textContent = text;
  x._h = x.innerHTML;
  place();
}
