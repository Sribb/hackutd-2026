// The guided judge tour (#158, part of the judge flow #147). /start ends with "go to the
// console", which opens /?tour=1. Eight small cards walk the judge through one night shift,
// using the console's own features and its own numbers (nothing here makes a number up):
//
//   1 field   you are the night-shift operator: the 3D field, drag to look around
//   2 look    a short camera tour: a tank, a pump, the disposal well (view.focus, then reset)
//   3 gaia    the chat; one-click questions go through the real composer
//   4 alarm   the pump trip the demo uses (POST /api/live/faults), or the incident already open;
//             then window.gaiaNotify(alert) if #154 is in, else the incident opens in the chat
//   5 clock   the status line countdown and the time bar legend; drag the slider
//   6 fix     the planner's best plan and its dollars; its first step becomes a PENDING
//             proposal (POST /api/actions) and the judge holds to confirm on the fix card
//   7 phone   the number from /start (sessionStorage gaia.judge.phone) and the console's pager QR
//   8 done    a recap, and /dataset for any EOG folder, test/ included
//
// Progress is in sessionStorage (gaia.tour), so a reload resumes; ?tour=1 starts over.
// The card never covers the time bar or the composer (place() below). window.__tour reports
// the step for headless checks.

import "./tour.css";
import { BASE } from "../api.js";
import { post } from "../console/auth.js";
import { esc } from "../console/stream.js";
import { usd, clock12, toMin, andList } from "../console/agents.js";
import { highlight } from "../highlight.js";
import { readPhone, prettyUsPhone } from "../start/phone.js";

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
function idsOf(kind, prefix, f) {
  const fromField = f && Array.isArray(f[kind]) ? f[kind].map((x) => x.id) : [];
  const fromModel = window.__freeboard ? [...window.__freeboard.assets.keys()].filter((id) => id.startsWith(prefix)) : [];
  return fromField.length ? fromField : fromModel;
}
const PRIO = { P1: 0, P2: 1, P3: 2 };
async function openIncident() {
  const { alerts = [] } = await getJson("/api/alerts");
  const roots = alerts.filter((a) => a.active !== false && (!a.root_id || a.root_id === a.id) && !a.shelved_until);
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
  card.querySelector("[data-unpager]").onclick = () => { const p = $(".pager"); if (p) p.remove(); frame(); };
  card.querySelector("[data-skip]").onclick = () => end();
  card.querySelector("[data-back]").onclick = () => go(step - 1);
  card.querySelector("[data-next]").onclick = () => (step === STEPS.length - 1 ? end() : go(step + 1));
  addEventListener("resize", place);
  tick = setInterval(frame, 200);
  go(i);
}
let tick = 0;

function end() {
  leave();
  save({ done: true });
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
function els() {
  return targets.map((t) => (typeof t === "function" ? t() : $(t))).filter((el) => {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== "hidden";
  });
}
function frame() {
  if (!card) return;
  const list = els();
  while (rings.length < list.length) { const r = document.createElement("div"); r.className = "gt-ring"; r.setAttribute("aria-hidden", "true"); document.body.appendChild(r); rings.push(r); }
  rings.forEach((ring, k) => {
    const el = list[k];
    ring.hidden = !el;
    if (!el) return;
    const r = clipToView(el.getBoundingClientRect());
    Object.assign(ring.style, { left: `${r.left - 4}px`, top: `${r.top - 4}px`, width: `${r.width + 8}px`, height: `${r.height + 8}px` });
  });
  pill(!!$(".pager"));
  place();
}
const clipToView = (r) => { const left = Math.max(2, r.left), top = Math.max(2, r.top); return { left, top, width: Math.min(innerWidth - 2, r.right) - left, height: Math.min(innerHeight - 2, r.bottom) - top }; };

// The pager overlay covers the screen: the card folds to a small pill in the corner until it closes.
function pill(on) { if (card) card.classList.toggle("pill", on); }

// ------------------------------------------------------------ placing ----
// Never on the time bar (where it sits when it is up, even while it is tucked away) or the
// composer; preferably not on what this step points at.
function hardRects() {
  const out = [];
  const tb = $(".timebar"), st = $("#stage");
  if (tb && st) {
    const s = st.getBoundingClientRect();
    if (s.width > 0) {
      out.push({ left: s.left + tb.offsetLeft, top: s.top + tb.offsetTop, right: s.left + tb.offsetLeft + tb.offsetWidth, bottom: s.top + tb.offsetTop + tb.offsetHeight });
      const b = tb.getBoundingClientRect(); // its on-screen box, clipped to the stage it lives in
      const c = { left: Math.max(b.left, s.left), top: Math.max(b.top, s.top), right: Math.min(b.right, s.right), bottom: Math.min(b.bottom, s.bottom) };
      if (c.right > c.left && c.bottom > c.top) out.push(c);
    }
  }
  for (const sel of [".cs-dock", "#csAsk"]) { const el = $(sel); if (el) { const r = el.getBoundingClientRect(); if (r.width > 0 && r.height > 0) out.push(r); } }
  return out;
}
const hits = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

function place() {
  if (!card) return;
  if (card.classList.contains("pill")) { Object.assign(card.style, { left: "8px", top: "8px", maxHeight: "" }); return; }
  card.style.maxHeight = "none";
  const w = card.offsetWidth, h = card.offsetHeight, vw = innerWidth, vh = innerHeight;
  const hard = hardRects();
  const softR = soft ? els().map((el) => el.getBoundingClientRect()).filter((r) => r.width * r.height < vw * vh * 0.3) : [];
  const st = $("#stage") && $("#stage").getBoundingClientRect();
  const chat = $("#csJarvis") && $("#csJarvis").getBoundingClientRect();
  const spots = [];
  if (st && st.width > w * 0.8) spots.push([st.right - w - 14, st.top + 58], [st.left + 14, st.top + 58]);
  if (chat && chat.width > 0) spots.push([chat.left + (chat.width - w) / 2, chat.top + 56]);
  spots.push([(vw - w) / 2, 10], [vw - w - 10, 10], [10, 10]);
  let best = null;
  for (const [x0, y0] of spots) {
    const x = Math.max(8, Math.min(vw - w - 8, x0)), y = Math.max(8, Math.min(vh - 120, y0));
    // Room down to the first hard box under this spot (or the bottom of the window).
    let room = vh - 8 - y;
    let blocked = false;
    for (const r of hard) {
      if (!(x < r.right && r.left < x + w)) continue;
      if (r.top <= y && r.bottom > y) blocked = true;
      else if (r.top > y) room = Math.min(room, r.top - 8 - y);
    }
    if (blocked || room < 110) continue;
    const fits = room >= h;
    const box = { left: x, top: y, right: x + w, bottom: y + Math.min(h, room) };
    const clean = !softR.some((r) => hits(box, r));
    const score = (fits ? 0 : 2) + (clean ? 0 : 1);
    if (!best || score < best.score || (score === best.score && room > best.room && !fits)) best = { x, y, room, score };
    if (score === 0) break;
  }
  if (!best) best = { x: Math.max(8, (vw - w) / 2), y: 8, room: Math.max(110, vh - 16) };
  Object.assign(card.style, { left: `${Math.round(best.x)}px`, top: `${Math.round(best.y)}px`, maxHeight: `${Math.floor(Math.min(best.room, vh - 16))}px` });
}

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
      const tanks = idsOf("tanks", "TANK_", f), pumps = idsOf("pumps", "PUMP_", f), swds = idsOf("disposal_wells", "SWD_", f);
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
      let a = await openIncident();
      if (!ctx.alive()) return;
      if (!a) {
        const f = await field();
        const pumps = idsOf("pumps", "PUMP_", f);
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
        window.__tour.notified.push({ id: a.id, via: notify ? "gaiaNotify" : "chat" });
        if (notify) { try { window.gaiaNotify(a); } catch (e) { console.warn("gaiaNotify", e); } }
        C().open();
        for (let k = 0; k < 20 && !C().threads().some((t) => t.id === a.id); k++) await sleep(250);
        if (!notify && ctx.alive()) C().thread(a.id);
        if (a.asset_id && ctx.alive()) fly(a.asset_id);
      }
      if (!ctx.alive()) return;
      const when = a.clock ? clock12(toMin(a.clock)) : "";
      body(`<p>${esc(lc(a.message || "an alarm came in."))}</p>
        <p>watch raised it as ${esc(lc(a.priority || a.severity || "an alarm"))}${when ? ` at ${esc(lc(when))}` : ""}. ${notify
          ? "this is how gaia alerts an operator: one big notification, then the whole incident in the chat."
          : "this is how gaia alerts an operator: the incident opens in the chat, and the phone pager buzzes."}</p>`);
      ctx.targets(["#csWatch", latestWatch]);
    },
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
        else x = `<span class="gt-note">${plan ? "this plan has no step to propose." : ""}</span>`;
        extra(x, (el) => { const p = el.querySelector("[data-propose]"); if (p) { p.disabled = proposing; p.onclick = propose; } });
        ctx.targets(st ? ["#csPlan", "#csFix"] : ["#csPlan"]);
        if (st && !draw.scrolled) { draw.scrolled = true; const f = $("#csFix"); if (f) f.scrollIntoView({ block: "nearest" }); }
      };
      body(head + rule);
      ctx.every(300, draw);
    },
  },
  {
    id: "phone", title: "your phone", targets: [],
    enter(ctx) {
      const ph = prettyUsPhone(readPhone());
      const pager = [...document.querySelectorAll("#side .brand-row button")].find((b) => /^pager$/i.test(b.textContent.trim()));
      body(`<p>${ph ? `tonight's alerts go to ${esc(ph)}. texts and calls are coming; for now the phone pager does it.` : "you didn't leave a number, so the phone pager is how alerts reach a phone tonight."}</p>
        <p>open the pager and scan the code with your phone. it buzzes on every alert, and a fix can be confirmed from it with the same press and hold.</p>`);
      extra(pager ? btns([["data-pager", "open the pager", "primary"]]) : `<span class="gt-note">the pager is not on this screen.</span>`, (x) => {
        const b = x.querySelector("[data-pager]");
        if (b) b.onclick = () => pager.click();
      });
      void ctx;
    },
    leave() { const p = $(".pager"); if (p) p.remove(); },
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
