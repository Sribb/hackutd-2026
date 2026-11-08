// Freeboard Jarvis console (docs/JARVIS_CONSOLE.md, Track B). The operator's default screen:
// the 3D field full bleed under a 32 px status line, and Jarvis as a right sidebar (J key).
// Classic stays at ?layout=classic.
//
//   mountConsole(hooks) -> { ready(root), update(dt, t), reset(), open(), close() }
//   window.__console     test and debug hooks (see the bottom of this file)
//
// Agents: Watch, Forecast and Planner are templates over data (agents.js). Jarvis answers:
// route L in the browser (intents.js), then the backend's rules, then Claude.
// Safety: nothing moves equipment without a full hold on exactly one proposal (fixcard.js);
// never an all-clear while a tank is UNKNOWN or the feed is lost; SENDING and UNCERTAIN are
// shown as they are; every POST carries the control token (auth.js).

import * as liveFeed from "../live.js";
import * as shiftlog from "../shiftlog.js";
import { highlight } from "../highlight.js";
import { holdButton } from "../fx.js";
import { post as authPost } from "./auth.js";
import { makeStream, esc } from "./stream.js";
import { makeFixCard } from "./fixcard.js";
import { mountStatusLine } from "./statusline.js";
import { mountSidebar } from "./sidebar.js";
import { makeDirectives } from "./directives.js";
import { makeViewStub } from "./viewstub.js";
import { mountMonitor } from "./monitor.js";
import { makeNotifier } from "./notify.js";
import * as intents from "./intents.js";
import { makeCharts } from "./chartask.js";
import {
  normalizeBrief, fallbackBrief, toMin, clock12, minsUntil, usd, andList,
  countdownBar, planBars, dot, prioOf, levelOf, watchText, forecastText, plannerText,
} from "./agents.js";
import "./console.css";
import { mountIntro } from "../gaia/intro.js";
import { mountThreads } from "../gaia/threads.js";
import { mountAgent } from "../agent/index.js";
import { makeLayout } from "../gaia/layout.js";
import { mountRail } from "../gaia/rail.js";

const POLL_MS = 30000;
const BRIEF_RETRY_MS = 5 * 60000;
const DOWN_TEXT = "Can't reach the backend, so alerts are not being received";
const HEALTH_MS = 10000;
const FALSE_CLEAR = /\b(all clear|all quiet|every tank is safe|everything is fine)\b/i;
const DRAWER_PANELS = { plans: "plans", walk: "walk", whatif: "whatif", asset: "selected", log: "log", tanks: "tanks" };

export function mountConsole(H) {
  document.body.classList.add("fb-console");
  const intro = mountIntro(); // the handwritten gaia, until the field and first data are in
  const layout = makeLayout();
  const app = H.app;
  const BASE = H.BASE;
  const nice = (id) => H.nice(id);
  H.hasId = (id) => !!id && H.ids().includes(id);

  // ------------------------------------------------------------ state ----
  const alerts = new Map();
  let reach = BASE ? "unknown" : "none";     // backend reachable for alerts/actions
  let brief = null;                          // normalized; source "brief" | "fallback"
  let briefFromServer = false, briefNext = 0;
  let fbForecast = null, fbPlans = null;     // fallback inputs, refreshed every 30 s
  let open = true, unread = false;   // open: the chat is on screen (false: the field is full width)
  let lastSel = undefined;
  const autoOpened = new Set();
  const seenPrio = new Map();                // alert id -> priority last seen
  let lastF = null, lastP = null, lastUnknown = "", lastLost = null, lastCallSaid = new Set();
  let started = false;
  let tbUntil = 0, tbHover = false;
  let askChain = Promise.resolve();
  let lastChip = { id: null, t: 0 };
  let drawerFrom = null;                     // { el, parent, next } while a panel is borrowed

  // --------------------------------------------------------------- DOM ----
  const status = mountStatusLine(H.stage, onSegment);
  // #154: the monitoring strip under the status line, and the big alert notification.
  const monitor = mountMonitor(H.stage, { focus: (id) => { if (H.hasId(id)) { view().focus(id); highlight.flash("jarvis", [id], 8000); } }, openAlerts: () => onSegment("W") });
  const side = mountSidebar(app, onSide);
  const tab = document.createElement("button");
  tab.type = "button";
  tab.id = "csTab";
  tab.className = "cs-tab";
  tab.setAttribute("aria-label", "Show the chat (J)");
  tab.innerHTML = `<span>chat</span><i class="cs-unread" hidden></i>`;
  tab.hidden = true;
  tab.onclick = () => setOpen(true);
  app.appendChild(tab);
  const drawer = document.createElement("section");
  drawer.className = "cs-drawer";
  drawer.hidden = true;
  drawer.innerHTML = `<header><span class="cs-kicker" id="csDrawerName"></span><button type="button" class="cs-icon" aria-label="Close panel">×</button></header><div class="cs-drawer-body"></div>`;
  side.fix.parentElement.insertBefore(drawer, side.fix);
  drawer.querySelector("button").onclick = () => closeDrawer(true);
  const hot = document.createElement("div");
  hot.className = "cs-tb-hot";
  H.stage.appendChild(hot);
  const timebar = document.querySelector(".timebar");
  hot.addEventListener("pointerenter", () => { tbHover = true; });
  if (timebar) {
    timebar.addEventListener("pointerenter", () => { tbHover = true; });
    timebar.addEventListener("pointerleave", () => { tbHover = false; tbUntil = Date.now() + 10000; });
    const skyBtn = document.createElement("button");
    skyBtn.type = "button";
    skyBtn.className = "cs-skybtn";
    skyBtn.textContent = "time of day";
    skyBtn.title = "Lighting: follow the field clock, pick a time, or play a whole day";
    skyBtn.onclick = () => document.body.classList.toggle("cs-sky-open");
    const row = timebar.querySelector(".timebar-row");
    if (row) row.appendChild(skyBtn);
  }
  hot.addEventListener("pointerleave", () => { tbHover = false; tbUntil = Date.now() + 10000; });

  const stream = makeStream(side.stream, {
    nice, ids: H.ids, hasId: H.hasId, clockNow: () => nowClock(),
    chip: (id) => chip(id),
  });
  stream.onPost(() => { if (!open) unread = true; });

  const railRef = { r: null };
  // Threads (left sidebar): incidents, the field overview, the person's chats (gaia/threads.js).
  const threads = mountThreads(app, side, {
    onNew: () => side.input.focus(),
    onLog: () => openPanel("log"),
    onOverview: () => { resetView(); },
    onNav: (v) => layout.nav(v),
    onUnread: (v) => { if (railRef.r) railRef.r.unread(v); },
    onView: () => { if (!open) setOpen(true); },
    clock: (c) => clock12(toMin(c)).toLowerCase(),
    greeting: () => (activeAlerts().length || alertsDown() || unknownTanks().length || !vouchable() ? "what's happening on the field?" : "all quiet on the lease"),
  });
  const rail = mountRail(app, { newChat: () => threads.newChat(), nav: (v) => layout.nav(v), navOpen: () => layout.navOpen(), layers: () => onSegment("LAYERS") });
  railRef.r = rail;
  let agent = null; // gaia as an agent (agent/index.js): steps, voice, the offline walk-through

  const fix = makeFixCard(side.fix, {
    post, clockNow: () => nowClock(), liveClock: () => liveFeed.live.clock,
    note: (text, ids) => stream.line("note", text, { chips: ids || [] }),
    log, refresh, effectFromForecast,
  });

  const directives = makeDirectives({
    hasId: H.hasId, setActions: (list) => H.setActions(list.map((a) => ({ at_min: 0, ...a }))),
    setTimeFrom, resetView: () => ({ ok: true, said: resetView() }), openPanel,
  }, view);

  const notifier = makeNotifier({
    stage: H.stage, nice, brief: () => computeBrief(), nowMin: () => nowMin(), unknown: () => unknownTanks(),
    alerts: () => [...alerts.values()], log,
    // "show me": the field pane open, the 3D on the failed asset, and the incident open in the chat.
    showMe(a) {
      if (!layout.fieldOpen()) layout.field(true);
      setOpen(true);
      threads.view(a.root_id && a.root_id !== a.id ? a.root_id : a.id);
      const ids = [a.asset_id, a.consequence_asset_id].filter((x) => H.hasId(x));
      if (ids.length) { view().focus(ids[0]); highlight.flash("jarvis", ids, 8000); }
      requestAnimationFrame(() => scrollTo("W"));
    },
  });
  const tourAt = new URLSearchParams(location.search).get("tour") === "1" ? Date.now() : 0;
  let tourDone = !tourAt;

  const stub = makeViewStub({
    nice, scene: H.scene, hasId: H.hasId, tanks: H.tanks, focus: H.focus, select: H.select, flyHome: H.flyHome,
    setTime: H.setTime, tankOf: H.tankOf,
  });
  function view() {
    return H.view || (window.__console3d && window.__console3d.view) || stub;
  }
  agent = mountAgent({
    side, stream, apply: (d) => directives.apply(d), nice, hasId: H.hasId, selected: H.selected, tankOf: H.tankOf,
    brief: () => computeBrief(), alerts: () => activeAlerts(), unknown: () => unknownTanks(),
    say: (t, x) => say(t, x), ask: (t) => { setOpen(true); ask(t); }, closePanels: () => closeDrawer(true), BASE, getJson,
    chart: (t) => { const m = intents.match(t, { hasId: H.hasId, ids: H.ids, selected: H.selected, opened: () => false }); return m && m.chart && !m.chart.error ? charts.answer(m.chart) : "I couldn't draw that chart here."; },
    // The console's own clock words: "2:47 am" on the live field, "11:57 utc" on a dataset (#153).
    now: () => nowMin(), dataset: () => onDataset(),
    clock: (min) => (onDataset() ? `${String(Math.floor((((min % 1440) + 1440) % 1440) / 60)).padStart(2, "0")}:${String((((min % 1440) + 1440) % 1440) % 60).padStart(2, "0")} utc` : clock12(min).toLowerCase()),
  });
  function onDataset() { const d = H.data(); return !!(liveFeed.live.dataset || (d && d.field && d.field.mode === "dataset")); }
  // Chart cards (chartask.js): drawn from the data in the browser, no AI. A click flies to the asset.
  const charts = makeCharts({
    BASE, data: H.data, hasId: H.hasId, nice, stream, say: (text, extra) => say(text, extra),
    focus: (id) => { view().focus(id); highlight.flash("jarvis", [id], 8000); },
  });

  // --------------------------------------------------------- helpers ----
  function nowMin() { return toMin(liveFeed.live.clock) ?? toMin(H.clockNow()); }
  function nowClock() { return liveFeed.live.clock ? clock12(toMin(liveFeed.live.clock)) : H.clockNow(); }
  function post(path, body, ms = 15000) { return authPost(`${BASE}${path}`, body, ms); }
  async function getJson(path) {
    const r = await fetch(`${BASE}${path}`);
    if (!r.ok) throw Object.assign(new Error(`HTTP ${r.status}`), { status: r.status });
    return r.json();
  }
  function log(kind, text) {
    try { shiftlog.add(kind, text); if (H.logged) H.logged(); } catch { /* the log is a convenience */ }
  }

  // Tanks the console can't vouch for: no fresh level, the live layer says nothing, or the feed is lost.
  function unknownTanks() {
    const L = liveFeed.live;
    const out = new Set((H.blind && H.blind()) || []);
    for (const id of H.tanks()) {
      if (!L.available) continue;
      const r = liveFeed.reading(id);
      if (!r || r.stale || r.status === "UNKNOWN" || r.level_pct == null) out.add(id);
    }
    if (brief && brief.source === "brief") for (const id of brief.unknown_tanks) out.add(id);
    return [...out].filter((id) => H.hasId(id)).sort();
  }
  const feedLost = () => !!liveFeed.live.lostSince;
  // The backend is gone for alerts: a failed fetch or health ping, or the live socket lost.
  const alertsDown = () => reach === "down" || feedLost();
  // Only a live feed that is up, with the backend answering, can ever back a "nothing to do".
  // With no live layer (a static /api/state), the console never says the field is fine.
  const vouchable = () => liveFeed.live.available && liveFeed.live.connected && !feedLost() && reach === "ok";
  function setDown() {
    if (reach === "down") return;
    reach = "down";
    if (started) stream.say("W", { text: `${DOWN_TEXT}. Not the same as no alerts.`, visual: dot("red", "backend down"), ids: [] }, { urgent: true });
  }
  const activeAlerts = () => [...alerts.values()].filter((a) => a.active !== false);

  function computeBrief() {
    if (briefFromServer && brief && brief.source === "brief") return brief;
    const d = H.data();
    brief = fallbackBrief({
      alerts: activeAlerts(),
      forecast: fbForecast || (d && d.baseline) || null,
      plans: fbPlans || (d && d.plans) || null,
      startClock: H.stateClock(),
      liveUp: liveFeed.live.available,
      unknown: unknownTanks(),
    });
    return brief;
  }

  // ----------------------------------------------------------- polling ----
  async function poll() {
    if (!BASE) return;
    if (Date.now() >= briefNext) {
      try {
        const b = normalizeBrief(await getJson("/api/jarvis/brief"));
        if (b) { brief = b; briefFromServer = true; }
      } catch (e) {
        briefFromServer = false;
        if (e.status === 404 || e.status === 405) briefNext = Date.now() + BRIEF_RETRY_MS;
      }
    }
    if (!briefFromServer) {
      try {
        const [f, p] = await Promise.all([
          post("/api/forecast", { horizon_minutes: 360, step_minutes: 5, actions: [] }).then((r) => (r.ok ? r.json() : null)),
          post("/api/plans", {}).then((r) => (r.ok ? r.json() : null)),
        ]);
        if (f && f.events) fbForecast = f;
        if (p && p.plans) fbPlans = p;
      } catch { /* keep the last good values; the feed segment says if the backend is down */ }
    }
    agentsTick();
  }

  async function refresh() {
    if (!BASE) return;
    try {
      const [a, p] = await Promise.all([getJson("/api/alerts"), getJson("/api/actions")]);
      reach = "ok";
      for (const x of a.alerts || []) { alerts.set(x.id, x); threads.alert(x); if (!seenPrio.has(x.id)) seenPrio.set(x.id, prioOf(x)); }
      for (const x of p.proposals || []) fix.take(x, true);
      fix.draw();
    } catch {
      setDown();
    }
    agentsTick();
  }
  async function ping() {
    if (!BASE) return;
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 4000);
      const r = await fetch(`${BASE}/api/health`, { signal: ctl.signal }).finally(() => clearTimeout(t));
      if (!r.ok) throw new Error(String(r.status));
      if (reach !== "ok") refresh();
    } catch { setDown(); }
  }

  // What a confirmed-in-the-future action would do, from the do-nothing forecast (until the
  // backend has /api/jarvis/preview).
  async function effectFromForecast(action) {
    if (!action) return "";
    try {
      const r = await post("/api/forecast", { horizon_minutes: 360, step_minutes: 5, actions: [{ ...action, at_min: 0 }] });
      if (!r.ok) return "";
      const f = await r.json();
      const ev = (f.events || []).find((e) => e.type === "SHUTDOWN_LEVEL");
      const start = toMin(H.stateClock());
      const wait = (fbForecast || (H.data() && H.data().baseline) || {}).cost;
      const head = ev ? `With this, ${nice(ev.asset_id)} still shuts in at ${clock12(start + ev.t_min)}.` : `With this, no tank reaches shutdown by ${clock12(start + 360)}.`;
      return `${head}${f.cost ? ` ${usd(f.cost.total_usd)} vs ${usd(wait && wait.total_usd)} doing nothing` : ""} (shift forecast).`;
    } catch { return ""; }
  }

  // ------------------------------------------------------------ agents ----
  function watchVisual(a) {
    const rel = activeAlerts().filter((x) => x.root_id === a.id).length;
    return dot(levelOf(a), `${prioOf(a)}${rel ? ` · +${rel} related` : ""}${a.priority ? "" : " (from severity)"}`);
  }
  function watchExtra(a) {
    if (a.active === false || !BASE) return {};
    const acked = a.acked;
    return {
      extra: `<div class="cs-actions">${acked ? `<span class="muted">acknowledged</span>` : `<button type="button" data-ack>ack</button>`}
        <button type="button" data-shelve-open>shelve…</button></div>
        <form class="cs-shelve" hidden>
          <label>For <select name="m"><option>15</option><option selected>30</option><option>60</option><option>120</option><option>240</option></select> min</label>
          <input name="reason" placeholder="Why? A shelve needs a reason." aria-label="Shelve reason">
          <button type="button" class="primary hold" data-shelve-hold><i></i><span>hold to shelve</span></button>
          <div class="cs-fx-note" data-shelve-note hidden></div>
        </form>`,
      bind(li) {
        const ack = li.querySelector("[data-ack]");
        if (ack) ack.onclick = () => ackAlert(a.id, ack);
        const form = li.querySelector(".cs-shelve");
        const openBtn = li.querySelector("[data-shelve-open]");
        if (openBtn) openBtn.onclick = () => { form.hidden = !form.hidden; };
        const hold = li.querySelector("[data-shelve-hold]");
        if (hold) holdButton(hold, () => shelveAlert(a.id, Number(form.m.value), form.reason.value, li), 1500);
      },
    };
  }
  async function ackAlert(id, btn) {
    btn.disabled = true;
    try {
      const r = await post(`/api/alerts/${encodeURIComponent(id)}/ack`, { by: "console" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const out = await r.json().catch(() => ({}));
      alerts.set(id, { ...(alerts.get(id) || {}), ...out, acked: true });
      const a = alerts.get(id);
      log("alert", `Acknowledged: ${a.message || id}`);
      stream.say("W", { text: watchText(a, "acked", nice), visual: watchVisual(a), ids: [a.asset_id], key: a.id });
    } catch (e) {
      btn.disabled = false;
      stream.line("note", `Couldn't acknowledge that alert (${e.message}). Nothing changed.`);
    }
  }
  async function shelveAlert(id, minutes, reason, li) {
    const note = li.querySelector("[data-shelve-note]");
    if (!String(reason || "").trim()) { note.hidden = false; note.textContent = "Why? A shelve needs a reason. Nothing was sent."; return; }
    try {
      const r = await post(`/api/alerts/${encodeURIComponent(id)}/shelve`, { minutes, reason, by: "console" });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) {
        const d = Array.isArray(out.detail) ? out.detail.map((x) => x.msg).join(" ") : out.detail;
        note.hidden = false;
        note.textContent = `${d || `Refused (${r.status}).`} Nothing was sent.`;
        return;
      }
      alerts.set(id, { ...(alerts.get(id) || {}), ...(out.alert || out) });
      log("alert", `Shelved ${minutes} min: ${reason}`);
    } catch {
      note.hidden = false;
      note.textContent = "Couldn't reach the backend. Not shelved.";
    }
  }

  function onAlert(m) {
    const a0 = m.alert;
    if (!a0) return;
    const prev = alerts.get(a0.id);
    const a = { ...(prev || {}), ...a0 };
    alerts.set(a.id, a);
    threads.alert(a);
    const ev = m.event || (prev ? "updated" : "raised");
    const p = prioOf(a), was = seenPrio.get(a.id);
    seenPrio.set(a.id, p);
    const root = !a.root_id || a.root_id === a.id;
    const becameP1 = p === "P1" && root && a.active !== false && (ev === "raised" || (was && was !== "P1"));
    const urgent = a.severity === "ALARM" && ev === "raised";
    if (ev === "raised") log("alert", `${a.severity}: ${a.message}`);
    const news = ev !== "updated" || becameP1;
    stream.say("W", { text: watchText(a, ev, nice), visual: watchVisual(a), ids: [a.asset_id, a.consequence_asset_id].filter(Boolean), key: a.id, ...(ev === "resolved" ? {} : watchExtra(a)) }, { news, urgent: urgent || becameP1 });
    if (becameP1 && !autoOpened.has(a.id)) { autoOpened.add(a.id); setOpen(true); threads.view(a.id); }
    // A new incident: re-read the forecast and plans soon, so the card's fix is this incident's.
    if (notifier.consider(a, ev, becameP1)) setTimeout(poll, 2500);
    agentsTick();
  }

  function postForecast(news, urgent = false) {
    const b = computeBrief();
    const unknown = unknownTanks();
    const cd = b.countdown;
    const fromAlerts = b.source === "fallback" && b.from === "alerts";
    const label = b.source === "brief" ? "live brief" : fromAlerts ? "live alerts' forecast" : "shift forecast, every 30 s";
    stream.say("F", {
      text: forecastText(cd, nowMin(), unknown, feedLost(), nice, fromAlerts) + (vouchable() || cd ? "" : " No live feed, so I can't vouch for the field."),
      visual: `${countdownBar(cd, nowMin(), unknown)}<span class="cs-vlabel">${cd ? `now → ${clock12(cd.at)}` : "now → +6 h"}</span>`,
      ids: [...unknown, cd && cd.asset_id].filter(Boolean),
      tag: label, cls: unknown.length ? "unknown" : "",
    }, { news, urgent });
  }
  function postPlanner(news) {
    const b = computeBrief();
    const plan = b.plans.robust || null;
    const wait = b.do_nothing_usd;
    const unknown = unknownTanks();
    const nothing = !b.countdown && !unknown.length && vouchable();
    stream.say("P", {
      text: plannerText(nothing ? null : plan, wait, b.countdown, unknown),
      visual: !nothing && plan ? planBars(plan.total_usd, wait) : "",
      ids: [],
      tag: b.source === "brief" ? "live brief" : "shift plan",
      extra: !nothing && plan && plan.actions.length ? `<div class="cs-actions"><button type="button" data-try>try it</button><button type="button" data-plans>all plans</button></div>` : "",
      bind(li) {
        const t = li.querySelector("[data-try]");
        if (t) t.onclick = () => { H.setActions(plan.actions.map((x) => ({ ...x }))); stream.line("note", "Showing that plan on the forecast. Nothing was sent."); };
        const all = li.querySelector("[data-plans]");
        if (all) all.onclick = () => openPanel("plans");
      },
    }, { news });
  }

  // Called on every data change and once a second: posts news, refreshes live visuals in place.
  function agentsTick() {
    if (!H.ready()) return;
    const b = computeBrief();
    const unknown = unknownTanks();
    const uKey = unknown.join(",");
    const lost = feedLost();
    if (!started) {
      started = true;
      const act = activeAlerts().filter((a) => !a.root_id && !a.shelved_until);
      act.sort((x, y) => (prioOf(x) > prioOf(y) ? 1 : -1));
      if (act.length) stream.say("W", { text: watchText(act[0], "raised", nice), visual: watchVisual(act[0]), ids: [act[0].asset_id, act[0].consequence_asset_id].filter(Boolean), key: act[0].id, ...watchExtra(act[0]) });
      else stream.say("W", { text: alertsDown() ? `${DOWN_TEXT}. Not the same as no alerts.` : !liveFeed.live.available ? "No live feed, so no live alerts. That is not the same as no alerts." : unknown.length ? `No alarms, but no fresh reading from ${andList(unknown.map(nice))}. Treat ${unknown.length === 1 ? "it" : "them"} as unknown, not safe.` : "No alarms. Watching the field.", visual: dot(unknown.length ? "gray hatched" : "gray", unknown.length ? "UNKNOWN" : "none"), ids: unknown });
      postForecast(true);
      postPlanner(true);
      lastUnknown = uKey;
      lastLost = lost;
      lastF = b.countdown ? { asset: b.countdown.asset_id, at: b.countdown.at } : null;
      lastP = planKey(b);
      return;
    }
    if (lost !== lastLost) {
      lastLost = lost;
      stream.say("W", lost
        ? { text: `Live feed lost. ${DOWN_TEXT}. Every reading is stale; nothing is called safe until it's back.`, visual: dot("red", "feed lost"), ids: [] }
        : { text: "Live feed is back. Readings are fresh again.", visual: dot("gray", "feed back"), ids: [] }, { urgent: true });
    }
    if (uKey !== lastUnknown) {
      const added = unknown.filter((id) => !lastUnknown.split(",").includes(id));
      lastUnknown = uKey;
      for (const id of added) {
        const m = liveFeed.staleMinutes(id);
        stream.say("W", { text: `No reading from ${nice(id)}${m != null && m >= 1 ? ` for ${m} min` : ""}. Treat it as unknown, not safe.`, visual: dot("gray hatched", "UNKNOWN"), ids: [id] });
      }
      postForecast(true);
    }
    const cd = b.countdown;
    const moved = (!!cd) !== (!!lastF) || (cd && lastF && (cd.asset_id !== lastF.asset || Math.abs((cd.at ?? 0) - (lastF.at ?? 0)) >= 5));
    if (moved) { lastF = cd ? { asset: cd.asset_id, at: cd.at } : null; postForecast(true); }
    else postForecast(false);
    const pk = planKey(b);
    let pNews = pk !== lastP;
    lastP = pk;
    const lc = b.plans.robust && b.plans.robust.expires_clock ? minsUntil(toMin(b.plans.robust.expires_clock), nowMin()) : null;
    for (const mark of [15, 5]) {
      const k = `${b.plans.robust && b.plans.robust.id}:${mark}`;
      if (lc != null && lc <= mark && lc > 0 && !lastCallSaid.has(k)) { lastCallSaid.add(k); pNews = true; }
    }
    postPlanner(pNews);
  }
  const planKey = (b) => `${b.plans.robust ? `${b.plans.robust.id}:${b.plans.robust.total_usd}` : "-"}:${b.do_nothing_usd}:${!!b.countdown}`;

  // ------------------------------------------------------------ Jarvis ----
  function say(text, extra = {}) {
    const safe = guard(text);
    stream.post("J", { text: safe, ids: extra.ids || [], tag: extra.tag || "local", ...extra, text: safe });
    if (agent) agent.spoke(safe);
  }
  // Never an all-clear while a tank is unknown or the feed is lost, whoever wrote the words.
  function guard(text) {
    const unknown = unknownTanks();
    if (!FALSE_CLEAR.test(String(text || "")) || (!unknown.length && vouchable())) return text;
    if (!vouchable() && !unknown.length) return "I can't call it quiet: there's no live feed right now, so I can't vouch for the field.";
    return feedLost() ? "I can't call it quiet: the live feed is lost, so every reading is stale."
      : `I can't call it quiet: no fresh reading from ${andList(unknown.map(nice))}. Treat ${unknown.length === 1 ? "it" : "them"} as unknown.`;
  }

  function ask(message, opts = {}) {
    const text = String(message || "").trim();
    if (!text) return;
    if (!opts.quiet) stream.line("you", text);
    if (agent && agent.before(text)) return;
    const ctx ={ hasId: H.hasId, ids: H.ids, selected: H.selected, opened: () => (view().state() || {}).open };
    const m = intents.match(text, ctx);
    if (m) {
      if (m.chart) { askChain = askChain.then(() => charts.answer(m.chart)).catch(() => {}); return; }
      if (m.hands) { setHands(m.hands === "on"); say(m.hands === "on" ? "Hands on. Point to look, pinch to select, pinch and drag to turn, open palm to stop." : "Hands off. The mouse does everything."); return; }
      if (m.clearWhatif) { H.setActions([]); say("Cleared every what-if. Back to the do-nothing forecast."); return; }
      const said = directives.apply({ action: m.action, asset_id: m.asset_id, args: m.args });
      if (m.action === "focus") {
        // Fly there now; the words come from the backend (rules).
      } else {
        const def = { reset_view: "View reset.", collapse: "Closed.", set_time: "" }[m.action];
        say(said || def || "Done.", { ids: m.asset_id && m.asset_id !== "ALL" ? [m.asset_id] : [] });
      }
      if (!m.ask) return;
    }
    askChain = askChain.then(() => askBackend(text)).catch(() => {});
  }

  async function askBackend(text) {
    if (!BASE) { say("gaia needs the backend for that. The 3D view still works: try “open pump 3” or “reset”."); return; }
    const t0 = performance.now();
    stream.showTyping("J");
    const slow = setTimeout(() => stream.showTyping("J", "asking claude…"), 2000);
    let r, out;
    try {
      const sel = H.selected();
      r = await post("/api/jarvis", { message: text, selected: sel || null, selected_asset_ids: sel ? [sel] : [] }, 30000);
      out = await r.json().catch(() => ({}));
    } catch (e) {
      clearTimeout(slow);
      stream.hideTyping("J");
      say(e.name === "AbortError" ? "gaia took too long to answer. Nothing was sent." : "Can't reach gaia. The 3D view and the fix card still work.", { tag: "error" });
      return;
    }
    const wait = 250 - (performance.now() - t0);
    if (wait > 0) await new Promise((res) => setTimeout(res, wait));
    clearTimeout(slow);
    stream.hideTyping("J");
    if (!r.ok) {
      const d = typeof out.detail === "string" ? out.detail : `HTTP ${r.status}`;
      say(r.status === 503 ? `I can't answer that here: ${d}` : r.status === 401 ? "Wrong PIN. Nothing was sent." : r.status === 403 ? "This device can't do that." : d, { tag: "error" });
      return;
    }
    const ids = [];
    for (const d of out.ui || []) if (d.asset_id && d.asset_id !== "ALL" && !ids.includes(d.asset_id)) ids.push(d.asset_id);
    const said = await agent.play(out.ui || []); // several steps play one by one under a step list
    const reply = out.reply || said || "";
    say(reply, { ids: [...ids, ...stream.chipsFor(reply)].filter((x, i, a) => a.indexOf(x) === i), tag: out.source || "rules", ...offerCards(out.offers) });
    for (const p of out.proposals || []) fix.take(p, true);
    if ((out.proposals || []).length) { fix.draw(); setOpen(true); }
  }

  // offer_ack / offer_shelve from Track A: cards, never automatic.
  function offerCards(offers) {
    const list = (offers || []).filter((o) => o && o.alert_id && alerts.has(o.alert_id));
    if (!list.length) return {};
    const a = alerts.get(list[0].alert_id);
    return watchExtra(a);
  }

  // ----------------------------------------------------------- chips ----
  function chip(id) {
    const again = lastChip.id === id && performance.now() - lastChip.t < 8000;
    lastChip = { id, t: performance.now() };
    if (again) {
      const s = view().explode(id);
      if (s && s.said) say(s.said, { ids: [id] });
      return;
    }
    view().focus(id);
    highlight.flash("jarvis", [id], 8000);
  }

  // ------------------------------------------------------------- view ----
  function setTimeFrom(args) {
    let t = null;
    if (args.t_min != null) t = Number(args.t_min);
    else if (args.clock != null) {
      const at = typeof args.clock === "number" ? args.clock : toMin(args.clock);
      const start = toMin(H.stateClock());
      if (at != null && start != null) t = ((at - start) % 1440 + 1440) % 1440;
    }
    if (t == null || !isFinite(t)) return { ok: false, said: "I couldn't read that time." };
    t = Math.max(0, Math.min(360, Math.round(t / 5) * 5));
    H.setTime(t);
    if (t > 0) tbUntil = Date.now() + 10000;
    const start = toMin(H.stateClock());
    return { ok: true, said: t === 0 ? "Back to now." : `Showing the forecast at ${start == null ? `+${t} min` : clock12(start + t)}. Nothing was sent.` };
  }

  function resetView() {
    view().reset();
    closeDrawer(false);
    const acts = H.getActions();
    return acts.length ? `View reset. What-if still on: ${acts.map(H.describe).join(", ")}. Say “clear what-if” to remove it.` : "";
  }

  function openPanel(panel) {
    if (panel === "alerts") { setOpen(true); scrollTo("W"); return { ok: true, said: "" }; }
    const id = DRAWER_PANELS[panel];
    const el = id && document.getElementById(id);
    if (!el) return { ok: false, said: "That panel isn't on this screen." };
    const card = id === "tanks" ? el.closest(".card") || el : el;
    closeDrawer(false);
    drawerFrom = { el: card, parent: card.parentElement, next: card.nextSibling, id };
    drawer.querySelector(".cs-drawer-body").appendChild(card);
    drawer.querySelector("#csDrawerName").textContent = { log: "shift log", whatif: "what-if", walk: "plan walk", asset: "selected", tanks: "tank levels" }[panel] || panel;
    drawer.hidden = false;
    setOpen(true);
    threads.refresh();
    requestAnimationFrame(() => drawer.scrollIntoView({ block: "nearest" }));
    return { ok: true, said: "" };
  }
  function closeDrawer(user) {
    if (!drawerFrom) { drawer.hidden = true; return; }
    const { el, parent, next } = drawerFrom;
    parent.insertBefore(el, next);
    drawerFrom = null;
    drawer.hidden = true;
    threads.refresh();
    if (user) drawerDismissed = Date.now();
  }
  let drawerDismissed = 0;
  // An approved plan walk or a what-if must stay visible with the old aside hidden.
  function autoDrawer() {
    if (Date.now() - drawerDismissed < 15000) return;
    const want = H.walking() ? "walk" : H.getActions().length ? "whatif" : null;
    if (want && (!drawerFrom || drawerFrom.id !== want) && !(drawerFrom && drawerFrom.id === "plans")) openPanel(want);
    if (!want && drawerFrom && (drawerFrom.id === "walk" || drawerFrom.id === "whatif")) closeDrawer(false);
  }

  // ------------------------------------------------------------ hands ----
  function handsState() {
    const b = document.getElementById("handsBtn");
    if (!b) return "unavailable";
    return b.disabled || /starting/i.test(b.textContent) ? "starting" : /off/i.test(b.textContent) ? "live" : "off";
  }
  function setHands(on) {
    const b = document.getElementById("handsBtn");
    if (!b) { say("Hand control isn't available on this screen. The mouse does everything."); return; }
    const s = handsState();
    if ((on && s === "off") || (!on && s === "live")) b.click();
    if (on && !open) setOpen(true);
  }
  // A pinch on an asset: select it (hands.js did), then ask the rules about THAT asset.
  addEventListener("freeboard:jarvis", (e) => {
    const id = e.detail && e.detail.asset_id;
    if (!id || !H.hasId(id)) return;
    setOpen(true);
    ask(`status of ${nice(id)}`);
  });

  // ------------------------------------------------------------ open ----
  function setOpen(v) {
    if (open === v) return;
    open = v;
    layout.chat(v);
    document.body.classList.toggle("cs-open", v);
    if (v) unread = false;
    if (!v && handsState() !== "off" && handsState() !== "unavailable") setHands(false); // closing Jarvis turns hands off
    renderStatus();
  }
  function scrollTo(agent) {
    const li = agent === "FIX" ? side.fix : stream.latest(agent);
    if (!li) return;
    if (li.hidden && li.dataset.thread) threads.view(li.dataset.thread);
    if (li.hidden) return;
    li.scrollIntoView({ block: "nearest" });
    li.classList.remove("flash");
    void li.offsetWidth;
    li.classList.add("flash");
  }
  function onSegment(seg) {
    if (seg === "RESET") { const s = resetView(); if (s) say(s); return; }
    if (seg === "F") tbUntil = Date.now() + 10000;
    if (seg === "J") { setOpen(!open); return; }
    if (seg === "FULL") { setOpen(!open); return; }
    if (seg === "LAYERS") { tbUntil = Date.now() + 15000; document.body.classList.toggle("cs-sky-open"); return; }
    if (seg === "XRAY") { const b = document.querySelector(".scenery-btn"); if (b) b.click(); document.body.classList.toggle("cs-xray"); return; }
    if (seg === "FOLLOW") { const id = followId(); if (id) { view().focus(id); highlight.flash("jarvis", [id], 8000); } return; }
    if (seg === "NOFIELD") { layout.field(false); if (!open) setOpen(true); return; }
    setOpen(true);
    scrollTo(seg);
  }
  // The asset the active incident is about: its consequence (the tank) first, else the asset.
  function followId() {
    const a = alerts.get(threads.active());
    return a ? (H.hasId(a.consequence_asset_id) ? a.consequence_asset_id : H.hasId(a.asset_id) ? a.asset_id : null) : null;
  }
  function onSide(kind, v) {
    if (kind === "close") setOpen(false);
    else if (kind === "field") layout.field(true);
    else if (kind === "nav") layout.nav(!layout.navOpen());
    else if (kind === "new") threads.newChat();
    else if (kind === "ask") { setOpen(true); ask(v); }
    else if (kind === "hands") setHands(v);
    else if (kind === "deselect") H.select(null);
  }

  addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = e.target && e.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target && e.target.isContentEditable)) return;
    if (e.key === "j" || e.key === "J") { e.preventDefault(); setOpen(!open); }
    else if (e.key === "/") { e.preventDefault(); setOpen(true); side.input.focus(); }
    else if (e.key === "t" || e.key === "T") { tbUntil = tbUntil > Date.now() ? 0 : Date.now() + 10000; }
  });

  // ------------------------------------------------------- status line ----
  // One quiet lowercase line over the field: "live · 1 alarm · tank 3 shuts in 55 min · best plan ...".
  function renderStatus() {
    const L = liveFeed.live;
    const lc = (x) => String(x).toLowerCase();
    let feed;
    if (!BASE) feed = { level: "amber", text: "no backend", title: "Start the backend with npm run demo." };
    else if (L.lostSince) {
      const s = Math.floor((Date.now() - L.lostSince) / 1000);
      feed = { level: "red", text: `live feed lost ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` };
    } else if (L.connected) feed = { level: "green", text: "live", title: `Live from ${liveFeed.connectorBadge().map((c) => c.kind).join(", ")}.` };
    else if (L.available) feed = { level: "gray", text: "polling" };
    else if (reach === "down") feed = { level: "amber", text: "no backend" };
    else feed = { level: "gray", text: "connecting" };
    side.setLive(feed.level, feed.text);

    const unknown = unknownTanks();
    const act = activeAlerts().filter((a) => !a.shelved_until);
    const roots = act.filter((a) => !a.root_id || !alerts.has(a.root_id) || a.root_id === a.id);
    let watch;
    if (alertsDown()) watch = { level: "red", text: lc(DOWN_TEXT) };
    else if (roots.length) {
      const red = act.some((a) => levelOf(a) === "red");
      const amber = act.some((a) => levelOf(a) === "amber");
      const rel = act.length - roots.length;
      watch = { level: red ? "red" : amber ? "amber" : "gray", text: `${roots.length} ${red ? "alarm" : "alert"}${roots.length === 1 ? "" : "s"}${rel ? `, ${rel} related` : ""}${unknown.length ? ` · ${unknown.length} unknown` : ""}` };
    } else if (unknown.length) watch = { level: "gray", text: `no alarms · ${unknown.length} tank${unknown.length === 1 ? "" : "s"} unknown` };
    else if (!vouchable()) watch = { level: "gray", text: L.available ? "no alarms · feed not live" : "no live alerts · no live feed" };
    else watch = { level: "gray", text: "no alarms" };

    const b = computeBrief();
    const cd = b.countdown;
    const n = nowMin();
    const left = cd ? minsUntil(cd.at, n) : null;
    const cdText = cd ? lc(`${nice(cd.asset_id)} ${left != null && left <= 0 ? "is at shutdown" : `shuts in ${Math.max(0, Math.round(left))} min`}`) : "";
    let ftext;
    if (L.lostSince) ftext = `can't vouch: feed lost${cd ? ` · ${cdText}` : ""}`;
    else if (unknown.length) ftext = `can't vouch for ${lc(andList(unknown.map(nice)))}${cd ? ` · ${cdText}` : ""}`;
    else ftext = cd ? cdText : vouchable() ? "no shutdown in 6 h" : "no shutdown forecast · not live";
    const src = b.source === "brief" ? "the live brief" : b.from === "alerts" ? "the backend's live forecast (alerts)" : "the shift forecast (/api/forecast, every 30 s)";
    const forecast = { text: ftext, unknown: unknown.length > 0,
      title: cd ? `${nice(cd.asset_id)} shuts in at ${clock12(cd.at)}${cd.low != null && cd.high != null ? ` (${clock12(cd.low)} to ${clock12(cd.high)})` : ""}, from ${src}.` : `From ${src}.` };

    const plan = b.plans.robust;
    const nothing = !cd && !unknown.length && vouchable();
    const planSeg = nothing ? { text: "no fix needed" } : plan ? { text: `best plan ${usd(plan.total_usd)} vs ${usd(b.do_nothing_usd)} if nothing changes`, title: `Cheapest robust fix vs doing nothing, from ${b.source === "brief" ? "the live brief" : "the shift plans"}.` } : { text: "" };

    status.render({
      title: lc((H.data() && H.data().field && (H.data().field.name || H.data().field.site_name)) || "the lease"), follow: !!followId(),
      clock: lc(nowClock()), feed, watch, forecast, plan: planSeg,
      pending: fix.pending().length, unread, open, full: !open,
      isolated: (view().state() || {}).isolated ? lc(nice(view().state().isolated)) : "",
    });
    tab.hidden = true;
    if (H.ready() && H.liveView) {
      const red = act.some((a) => levelOf(a) === "red"), amber = act.some((a) => levelOf(a) === "amber");
      monitor.render({
        field: H.data().field, view: H.liveView(), unknown, nice: H.nice, down: alertsDown(),
        alerts: { n: roots.length, rel: act.length - roots.length, red: roots.length > 0 && red, amber: roots.length > 0 && amber },
        countdown: cd, left, src, vouch: vouchable() && !unknown.length,
        asOf: liveFeed.live.available ? null : H.clockAt(0),
      });
    }
  }

  // ------------------------------------------------------------ socket ----
  liveFeed.on("alert", onAlert);
  liveFeed.on("proposal", (m) => { if (m.proposal) { const isNew = !fix.has(m.proposal.id); fix.take(m.proposal); if (isNew && m.proposal.status === "PENDING" && !open) unread = true; } });
  liveFeed.on("ui", (m) => { if (H.ready()) directives.apply({ ...m, type: "ui" }, { echo: true }); }); // not before the field is in
  liveFeed.on("brief", (m) => { const b = normalizeBrief(m); if (b) { brief = b; briefFromServer = true; agentsTick(); } });
  liveFeed.on("feed", (L) => { if (L && L.connected) refresh(); agentsTick(); });
  liveFeed.on("state", () => agentsTick());

  if (BASE) { refresh(); poll(); }
  setInterval(poll, POLL_MS);
  setInterval(() => { if (reach === "down") refresh(); }, 5000);
  setInterval(ping, HEALTH_MS);
  let lastTick = 0, lastStatus = 0;

  // ------------------------------------------------------------- hooks ----
  const api = {
    ready(root) {
      // No debug "Model:" label; the scenery toggle moves into the timebar.
      const note = document.getElementById("modelNote");
      const sc = note && note.querySelector(".scenery-btn");
      const row = document.querySelector(".timebar-row");
      if (sc && row) row.insertBefore(sc, row.querySelector(".cs-skybtn"));
      const f = H.data() && H.data().field;
      // The "show tank 3" pill names this field's first tank in dataset mode, or wherever tank 3 isn't.
      const tank0 = f && (f.mode === "dataset" || !H.hasId("TANK_3")) && (f.tanks || []).map((t) => t.id).find((id) => H.hasId(id));
      if (tank0) side.suggestAsset(`show ${tank0.replace(/_/g, " ").toLowerCase()}`);
      agentsTick();
      renderStatus();
      intro.ready();
      void root;
    },
    update() {
      const now = performance.now();
      const sel = H.selected();
      if (sel !== lastSel) { lastSel = sel; side.setSelection(sel, nice); }
      const up = H.getTime() !== 0 || tbHover || Date.now() < tbUntil;
      if (H.getTime() !== 0) tbUntil = Date.now() + 10000;
      document.body.classList.toggle("cs-tb-up", up);
      if (now - lastStatus > 250) { lastStatus = now; renderStatus(); side.setHands(handsState()); autoDrawer(); }
      if (now - lastTick > 1000) {
        lastTick = now;
        agentsTick();
        notifier.tick();
        // ?tour=1 (the judge tour): show the current top alert once it is in, after a beat.
        if (!tourDone && started && Date.now() - tourAt > 1500) {
          const a = notifier.top();
          if (a) { tourDone = true; notifier.show(a, { via: "tour" }); } else if (Date.now() - tourAt > 20000) tourDone = true;
        }
      }
    },
    reset() { const s = resetView(); if (s) say(s); },
    open: () => setOpen(true),
    close: () => setOpen(false),
  };

  window.__console = {
    open: () => setOpen(true), close: () => setOpen(false), isOpen: () => open,
    brief: () => computeBrief(), countdown: () => { const b = computeBrief(); return b.countdown ? { ...b.countdown, clock: clock12(b.countdown.at) } : null; },
    unknown: () => unknownTanks(), now: () => nowMin(), reach: () => reach, vouchable: () => vouchable(), card: () => fix.shown(), cardLog: () => fix.log(),
    ask: (t) => ask(t), view: () => view(), reset: () => api.reset(), directives: () => directives.seen(),
    threads: () => threads.list(), thread: (id) => (id ? threads.view(id) : threads.viewing()), intro: () => intro.state(),
    agent: () => (agent ? agent.state() : null),
    camera: () => (H.controls && H.controls.target ? H.controls.target.toArray().map((x) => +x.toFixed(2)) : null),
    notify: () => notifier.state(),
  };
  // #154, for the judge tour: gaiaNotify() shows the current top alert; gaiaNotify(alert or id) a given one.
  window.gaiaNotify = (x) => {
    const a = x == null ? notifier.top() : typeof x === "string" ? alerts.get(x) : x && x.id && alerts.has(x.id) ? { ...alerts.get(x.id), ...x } : x;
    return a ? notifier.show(a, { via: "api" }) : { shown: false, reason: "no active alert" };
  };
  return api;
}

export { esc };
