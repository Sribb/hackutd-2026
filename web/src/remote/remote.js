// Freeboard Remote (#114): the default screen. One column beside the full-screen 3D field,
// ported from Vinny's phone remote (backend-handoff-vinny:backend/app/phone.html):
//
//   countdown   time to shutdown, or an all-clear only when every tank has a fresh reading
//   fixes       proposals waiting for a person: hold to confirm, one card per proposal id
//   quick asks  What's failing, Cheapest fix, Time left, Show Tank 3, +1 hour, Live
//   Jarvis      chat; asset chips fly the camera and light the field through highlight.js
//   alerts      priority P1/P2/P3, folded under their root cause, ack
//   bar         talk (Web Speech API, when the browser has it) or type
//
// The old panels stay one click away behind "Classic" (remembered per browser).
//
// Data: the one /ws socket in live.js, and the docs/CONTRACT.md "Live layer (#38)" endpoints.
// Every POST carries X-Freeboard-Token and /ws carries ?token= (token.js).
//
// Safety rules this file keeps:
//   1. Never an all-clear while any tank is UNKNOWN or stale: "Can't vouch for N tanks".
//   2. Confirm is a full 1.2 s hold. Letting go, leaving the button, or any redraw of the
//      fixes list cancels it. A card is one proposal id, sent at most once from here.
//      While the request is out the card says SENDING. If we can't tell whether it arrived,
//      it says UNCERTAIN ("sent, delivery not confirmed"), never "failed", with no retry.

import { installTransport, authHeaders } from "./token.js";
import { BASE } from "../api.js";
import * as liveFeed from "../live.js";
import * as shiftlog from "../shiftlog.js";
import { highlight } from "../highlight.js";
import "./remote.css";

installTransport(); // before main.js starts live.js, so the one socket gets ?token=

const HOLD_MS = 1200;
const MODE_KEY = "freeboard.layout";
const PRIO = { P1: 0, P2: 1, P3: 2 };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const niceId = (id) => String(id || "").replace(/_/g, " ").replace(/\b([A-Z])([A-Z]*)\b/g, (_, a, b) => a + b.toLowerCase()).replace(/\bXo\b/, "XO").replace(/\bSwd\b/, "SWD");
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const andList = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

let H = null;            // hooks from main.js
let root = null;
const el = {};
const chat = [];         // { who: "you"|"jarvis"|"note", text, chips?: [id], source? }
const proposals = new Map();
const local = new Map(); // proposal id -> { state: "SENDING"|"UNCERTAIN"|"NOTE", text? } (this screen only)
const sentOnce = new Set(); // proposal ids this screen has sent a confirm for; never sent twice
const alerts = new Map();
let busy = false;
let reach = "unknown";   // backend reachable for alerts/actions: "unknown" | "ok" | "down" | "none"
let hold = null;         // the one hold in progress: { btn, id, t0, raf, timer }
let rec = null, listening = false;

// --------------------------------------------------------------- mount ----
export function mountRemote(hooks) {
  H = hooks;
  const nice = (id) => (H.nice ? H.nice(id) : niceId(id));
  H.niceName = nice;
  root = document.createElement("aside");
  root.id = "remote";
  root.className = "remote";
  root.setAttribute("aria-label", "Freeboard Remote");
  root.innerHTML = `
    <header class="rm-head">
      <div class="brand">FREEBOARD</div>
      <span class="rm-conn" id="rmConn">connecting</span>
      <button id="rmClassic" title="Show the classic panels">Classic</button>
    </header>
    <section class="rm-count calm" id="rmCount" aria-live="polite">
      <div class="rm-clock" id="rmClock">--:--</div>
      <div class="rm-big" id="rmBig">Reading the field</div>
      <div class="rm-sub" id="rmSub"></div>
      <div class="rm-vouch" id="rmVouch" hidden></div>
      <div class="rm-inc" id="rmInc"></div>
    </section>
    <section class="rm-fixes" id="rmFixes" hidden></section>
    <section class="rm-quick" aria-label="Quick asks">
      <div class="kicker">Quick</div>
      <div class="rm-quick-row">
        <button data-say="What's failing?">What's failing</button>
        <button data-say="What's the cheapest fix?">Cheapest fix</button>
        <button data-say="How long until shutdown?">Time left</button>
        <button data-say="Show me Tank 3" data-show="TANK_3">Show Tank 3</button>
        <button data-time="+60">+1 hour</button>
        <button data-time="0">Live</button>
      </div>
    </section>
    <section class="rm-jarvis">
      <div class="kicker">Jarvis</div>
      <ol class="rm-chat" id="rmChat" aria-live="polite"></ol>
    </section>
    <section class="rm-alerts">
      <div class="kicker">Alerts</div>
      <div id="rmAlerts"></div>
    </section>
    <form class="rm-bar" id="rmBar" autocomplete="off">
      <button type="button" id="rmMic" class="rm-mic" title="Talk to Jarvis" hidden>Talk</button>
      <input id="rmSay" placeholder="Ask Jarvis" enterkeyhint="send" aria-label="Ask Jarvis">
      <button type="submit" class="primary" id="rmSend">Send</button>
    </form>`;
  (H.app || document.getElementById("app")).appendChild(root);
  for (const id of ["rmConn", "rmCount", "rmClock", "rmBig", "rmSub", "rmVouch", "rmInc", "rmFixes", "rmChat", "rmAlerts", "rmSay", "rmMic", "rmBar"]) el[id] = root.querySelector(`#${id}`);

  // Classic, and the way back, from the classic side panel.
  root.querySelector("#rmClassic").onclick = () => setMode("classic");
  const back = document.createElement("button");
  back.id = "rmBack";
  back.className = "upload-open";
  back.textContent = "Remote";
  back.title = "Back to the Remote layout";
  back.onclick = () => setMode("remote");
  const row = document.querySelector("#side .brand-row");
  if (row) row.appendChild(back);
  setMode(getMode(), true);

  root.querySelectorAll("[data-say]").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.show) showAsset(b.dataset.show);
      ask(b.dataset.say);
    };
  });
  root.querySelectorAll("[data-time]").forEach((b) => { b.onclick = () => shiftTime(b.dataset.time); });
  el.rmBar.onsubmit = (e) => {
    e.preventDefault();
    const v = el.rmSay.value;
    el.rmSay.value = "";
    ask(v);
  };
  mountMic();

  liveFeed.on("state", () => { drawCount(); drawConn(); });
  liveFeed.on("feed", (L) => {
    drawConn();
    drawCount();
    if (L && L.connected) refresh(); // catch up on anything sent while the socket was down
  });
  liveFeed.on("alert", (m) => {
    if (!m.alert) return;
    alerts.set(m.alert.id, { ...(alerts.get(m.alert.id) || {}), ...m.alert });
    drawAlerts();
  });
  liveFeed.on("proposal", (m) => { if (m.proposal) takeProposal(m.proposal); });
  liveFeed.on("ui", (m) => {
    if (m.action === "focus" && m.asset_id && m.source === "phone") note(`A phone pointed every screen at ${nice(m.asset_id)}.`);
  });
  // A Jarvis reply another screen asked for, once the backend broadcasts them.
  liveFeed.on("jarvis", (m) => {
    const text = m.reply || (m.data && m.data.reply);
    // (When the backend starts broadcasting replies, it should mark the asker's own copy so it isn't shown twice.)
    if (text && !m.mine) chat.push({ who: "jarvis", text, source: m.by ? `asked by ${m.by}` : m.source });
    drawChat();
  });

  if (!BASE) reach = "none";
  else refresh();
  drawConn();
  drawCount();
  drawFixes();
  drawChat();
  drawAlerts();
  let ticks = 0;
  setInterval(() => {
    drawCount();
    drawConn();
    if (reach === "down" && ++ticks % 5 === 0) refresh(); // keep trying; "can't reach" until it answers
  }, 1000);
  return { setMode, ask };
}

// ---------------------------------------------------------------- mode ----
function getMode() {
  try { return localStorage.getItem(MODE_KEY) === "classic" ? "classic" : "remote"; } catch { return "remote"; }
}
function setMode(mode, quiet = false) {
  document.body.classList.toggle("fb-remote", mode === "remote");
  document.body.classList.toggle("fb-classic", mode !== "remote");
  if (!quiet) { try { localStorage.setItem(MODE_KEY, mode); } catch { /* not remembered, still switches */ } }
  // The stage keeps its width, but tell three.js anyway in case the column changed.
  requestAnimationFrame(() => dispatchEvent(new Event("resize")));
}

// --------------------------------------------------------------- server ----
async function call(path, body, ms = 15000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), ms);
  try {
    const post = body !== undefined;
    const r = await fetch(`${BASE}${path}`, {
      method: post ? "POST" : "GET",
      headers: authHeaders(post ? { "Content-Type": "application/json" } : {}),
      body: post ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    });
    const out = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(typeof out.detail === "string" ? out.detail : `HTTP ${r.status} from ${path}`), { status: r.status });
    return out;
  } finally {
    clearTimeout(timer);
  }
}

async function refresh() {
  if (!BASE) return;
  try {
    const [a, p] = await Promise.all([call("/api/alerts"), call("/api/actions")]);
    reach = "ok";
    for (const x of a.alerts || []) alerts.set(x.id, x);
    for (const x of p.proposals || []) takeProposal(x, true);
    drawFixes();
  } catch {
    reach = "down";
  }
  drawAlerts();
}

// ----------------------------------------------------------- countdown ----
const toMin = (hhmm) => {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(hhmm || ""));
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
};
const clock12 = (min) => {
  const x = ((Math.round(min) % 1440) + 1440) % 1440, hh = Math.floor(x / 60), mm = x % 60;
  return `${hh % 12 || 12}:${String(mm).padStart(2, "0")} ${hh < 12 ? "AM" : "PM"}`;
};

// Tanks we can't vouch for: main.js's own list (snapshot and view) plus any tank the live
// layer reports UNKNOWN or stale, or every live tank while the feed is lost.
function blindTanks() {
  const out = new Set((H.blind && H.blind()) || []);
  const L = liveFeed.live;
  for (const id of (H.tanks && H.tanks()) || []) {
    const r = liveFeed.reading(id);
    if (r && (r.stale || r.status === "UNKNOWN" || r.level_pct == null)) out.add(id);
    else if (!r && L.available) out.add(id); // the live layer answers but says nothing about it
  }
  return [...out];
}

// The next tank to reach shutdown, or null.
// With the live layer: from each tank's fresh level and fill rate (observed, else expected), so
// it follows the field as fixes land. Tanks whose rate isn't known yet come back in `unsure`.
// Without the live layer: the do-nothing forecast main.js shows (/api/forecast).
function nextShutdown(nowMin) {
  const L = liveFeed.live;
  if (L.available && H.tank) {
    let best = null, full = null;
    const unsure = [];
    for (const id of (H.tanks && H.tanks()) || []) {
      const r = liveFeed.reading(id), info = H.tank(id);
      if (!r || r.stale || r.status === "UNKNOWN" || r.level_pct == null || !info || info.shutdown_pct == null) continue; // blind: blindTanks()
      if (r.level_pct >= info.shutdown_pct) { if (!full) full = { asset_id: id, level: r.level_pct, shut: info.shutdown_pct, min: 0 }; continue; }
      const rate = r.observed_bbl_hr ?? r.expected_bbl_hr;
      if (rate == null || !info.capacity_bbl) { unsure.push(id); continue; }
      if (rate <= 0) continue;
      const min = (((info.shutdown_pct - r.level_pct) / 100) * info.capacity_bbl / rate) * 60;
      if (min <= 360 && (!best || min < best.min)) best = { asset_id: id, min, rate, level: r.level_pct, shut: info.shutdown_pct };
    }
    const hit = full || best;
    return { live: true, hit: hit ? { ...hit, at: nowMin == null ? null : nowMin + hit.min } : null, unsure };
  }
  const ev = H.shutdown && H.shutdown();
  if (!ev) return { live: false, hit: null, unsure: [] };
  const start = toMin(H.stateClock && H.stateClock());
  const at = start == null ? null : start + ev.t_min;
  let left = at == null || nowMin == null ? ev.t_min : at - nowMin;
  if (left > 720) left -= 1440;
  if (left < -720) left += 1440;
  const range = ev.t_min_low != null && start != null
    ? ` (${clock12(start + ev.t_min_low)} to ${ev.t_min_high == null ? "later" : clock12(start + ev.t_min_high)})` : "";
  return { live: false, hit: { asset_id: ev.asset_id, min: Math.max(0, left), at, range, past: left <= 0 }, unsure: [] };
}

function drawCount() {
  if (!root) return;
  const nice = H.niceName;
  const L = liveFeed.live;
  // The live layer's clock when it runs, else the shift clock (real time from /api/state's clock).
  const shown = L.clock ? clock12(toMin(L.clock)) : H.ready && H.ready() ? shiftlog.clockNow() : "--:--";
  if (el.rmClock.textContent !== shown) el.rmClock.textContent = shown;
  const box = el.rmCount;
  const set = (cls, big, sub) => {
    box.className = `rm-count ${cls}`;
    if (el.rmBig.textContent !== big) el.rmBig.textContent = big;
    if (el.rmSub.textContent !== sub) el.rmSub.textContent = sub;
  };
  if (!H.ready || !H.ready()) {
    set("calm", "Reading the field", "Nothing is called safe until every tank has a reading.");
    el.rmVouch.hidden = true;
    el.rmInc.textContent = "";
    return;
  }
  const nowMin = toMin(L.clock) ?? toMin(H.stateClock && H.stateClock());
  const blind = blindTanks();
  const { live, hit, unsure } = nextShutdown(nowMin);
  // Tanks with a level but no fill rate yet can't be vouched for either.
  const cant = [...new Set([...blind, ...unsure])];
  const noRate = unsure.filter((id) => !blind.includes(id));
  const why = [blind.length ? `no fresh reading for ${andList(blind.map(nice))}` : "",
    noRate.length ? `no fill rate yet for ${andList(noRate.map(nice))} (it just changed)` : ""].filter(Boolean).join("; ");
  const Why = why.charAt(0).toUpperCase() + why.slice(1);
  if (hit && hit.min <= 0 && live) {
    set("alarm", "At shutdown", `${nice(hit.asset_id)} reads ${hit.level.toFixed(0)}%, at its ${hit.shut}% shutdown level. Its wells shut in.`);
  } else if (hit && hit.past) {
    set("alarm", "Shutdown time", `${nice(hit.asset_id)} was forecast to shut in${hit.at == null ? "" : ` at ${clock12(hit.at)}`}. Check it now.`);
  } else if (hit && live) {
    set("alarm", `${Math.round(hit.min)} min`, `to shutdown. ${nice(hit.asset_id)} is at ${hit.level.toFixed(0)}% (shuts in at ${hit.shut}%), filling ${Math.round(hit.rate)} bbl/hr${hit.at == null ? "" : `: about ${clock12(hit.at)}`} if nothing changes.`);
  } else if (hit) {
    set("alarm", `${Math.round(hit.min)} min`, `to shutdown. ${nice(hit.asset_id)} shuts in${hit.at == null ? "" : ` at ${clock12(hit.at)}`}${hit.range || ""} if nobody acts.`);
  } else if (cant.length) {
    set("warn", `Can't vouch for ${plural(cant.length, "tank")}`, `${Why}. Not treated as safe. Check in person.`);
  } else {
    set("clear", "All clear", "Every tank has a fresh reading and stays below shutdown for the next 6 hours.");
  }
  // With a countdown on screen, tanks we can't vouch for still get their own warning line.
  el.rmVouch.hidden = !(hit && cant.length);
  if (hit && cant.length) el.rmVouch.textContent = `Can't vouch for ${plural(cant.length, "tank")}: ${why}.`;
  const inc = H.incident && H.incident();
  const incText = inc && !inc.resolved_clock ? inc.summary || "" : "";
  if (el.rmInc.textContent !== incText) el.rmInc.textContent = incText;
}

function drawConn() {
  if (!root) return;
  const L = liveFeed.live;
  let cls = "off", text = "connecting";
  if (!BASE) text = "no backend";
  else if (L.lostSince) { cls = "lost"; text = `feed lost ${Math.round((Date.now() - L.lostSince) / 1000)} s`; }
  else if (L.connected) { cls = "on"; text = "live"; }
  else if (L.available) { cls = "on"; text = "live (polling)"; }
  el.rmConn.className = `rm-conn ${cls}`;
  if (el.rmConn.textContent !== text) el.rmConn.textContent = text;
}

// ---------------------------------------------------------------- fixes ----
function takeProposal(p, quiet = false) {
  proposals.set(p.id, p);
  const l = local.get(p.id);
  // The server's word on a final state replaces what this screen guessed.
  if (l && (p.status === "CONFIRMED" || p.status === "REJECTED")) local.delete(p.id);
  if (l && l.state === "SENDING" && p.status !== "PENDING") local.delete(p.id);
  if (!quiet) drawFixes();
}

function describe(a) {
  const nice = H.niceName;
  if (!a) return "";
  if (a.type === "SET_VALVE") return `Set ${nice(a.asset_id)} ${String(a.value).toLowerCase()}.`;
  if (a.type === "SET_PUMP") return `Turn ${nice(a.asset_id)} ${String(a.value).toLowerCase()}.`;
  if (a.type === "CHOKE_WELL") return a.value === 0 ? `Shut in ${nice(a.asset_id)}.` : `Choke ${nice(a.asset_id)} to ${Math.round(a.value * 100)}%.`;
  if (a.type === "SEND_TRUCK") return `Send ${nice(a.asset_id)}${a.target_id ? ` to ${nice(a.target_id)}` : ""}.`;
  return `${a.type} ${a.asset_id}`;
}

// What a card shows: the server's status, unless this screen knows more right now.
function stateOf(p) {
  const l = local.get(p.id);
  if (l && (l.state === "SENDING" || l.state === "UNCERTAIN")) return l.state;
  if (p.status === "FAILED") return "UNCERTAIN"; // the connector didn't confirm delivery
  return p.status;
}

const ORDER = { PENDING: 0, SENDING: 1, UNCERTAIN: 2, CONFIRMED: 3, REJECTED: 4 };
function drawFixes() {
  if (!root) return;
  cancelHold(); // any redraw cancels a hold in progress
  const list = [...proposals.values()];
  const open = list.filter((p) => ["PENDING", "SENDING", "UNCERTAIN"].includes(stateOf(p)))
    .sort((a, b) => ORDER[stateOf(a)] - ORDER[stateOf(b)] || (a.created_t > b.created_t ? 1 : -1));
  const done = list.filter((p) => !open.includes(p)).sort((a, b) => ((b.decided_t || "") > (a.decided_t || "") ? 1 : -1)).slice(0, 2);
  el.rmFixes.hidden = !open.length && !done.length;
  if (el.rmFixes.hidden) { el.rmFixes.innerHTML = ""; return; }
  el.rmFixes.innerHTML = `<div class="kicker">${open.length ? `Waiting for you · ${open.length}` : "Fixes"}</div>` +
    [...open, ...done].map(card).join("");
  el.rmFixes.querySelectorAll("[data-hold]").forEach((b) => bindHold(b, b.dataset.hold));
  el.rmFixes.querySelectorAll("[data-reject]").forEach((b) => { b.onclick = () => reject(b.dataset.reject); });
  el.rmFixes.querySelectorAll("[data-asset]").forEach((b) => { b.onclick = () => showAsset(b.dataset.asset); });
}

function card(p) {
  const st = stateOf(p);
  const l = local.get(p.id);
  const text = p.text || describe(p.action);
  const who = p.decided_by ? ` by ${esc(p.decided_by)}` : "";
  const src = p.source ? `from ${esc(p.source)}` : "";
  const asset = p.action && p.action.asset_id;
  let foot = "";
  if (st === "PENDING") {
    foot = `<div class="rm-btns">
        <button type="button" class="rm-reject" data-reject="${esc(p.id)}">Reject</button>
        <button type="button" class="primary hold rm-hold" data-hold="${esc(p.id)}" aria-label="Press and hold for 1.2 seconds to confirm: ${esc(text)}"><i></i><span>Hold to confirm</span></button>
      </div>${l && l.state === "NOTE" ? `<div class="rm-note">${esc(l.text)}</div>` : ""}`;
  } else if (st === "SENDING") {
    foot = `<div class="rm-btns"><button type="button" class="primary rm-sending" disabled>Sending</button></div>
      <div class="rm-note">Confirmed${who}. On its way to the equipment.</div>`;
  } else if (st === "UNCERTAIN") {
    const said = p.status === "FAILED" && p.result ? ` The connector said: ${esc(typeof p.result === "string" ? p.result : JSON.stringify(p.result))}` : "";
    foot = `<div class="rm-uncertain"><b>UNCERTAIN</b> Sent, delivery not confirmed. Check the field before retrying.${said}</div>`;
  } else if (st === "CONFIRMED") {
    foot = `<div class="rm-done">Done${who}.${p.result ? ` ${esc(typeof p.result === "string" ? p.result : "")}` : ""}</div>`;
  } else if (st === "REJECTED") {
    foot = `<div class="rm-note">Rejected${who}. Nothing was sent.</div>`;
  }
  return `<article class="rm-fix ${st}" data-id="${esc(p.id)}">
      <div class="rm-fix-top"><span class="rm-state">${st === "PENDING" ? "Confirm?" : esc(st)}</span><span class="muted">${src}</span></div>
      <div class="rm-fix-text">${esc(text)}</div>
      ${p.reason ? `<div class="rm-fix-why">${esc(p.reason)}</div>` : ""}
      ${asset ? `<button type="button" class="rm-chip" data-asset="${esc(asset)}">${esc(H.niceName(asset))}</button>` : ""}
      ${foot}
    </article>`;
}

function cancelHold() {
  if (!hold) return;
  cancelAnimationFrame(hold.raf);
  clearTimeout(hold.timer);
  hold.btn.classList.remove("holding");
  hold.btn.style.setProperty("--p", "0");
  hold = null;
}

// The commit runs on a timer, checked against the clock, so a slow frame never shortens the
// hold and a busy 3D view never stretches it. The fill is drawn on animation frames.
function bindHold(btn, id) {
  btn.style.setProperty("--p", "0");
  const start = (e) => {
    if (btn.disabled) return;
    if (e) e.preventDefault();
    cancelHold();
    const h = { btn, id, t0: performance.now(), raf: 0, timer: 0 };
    hold = h;
    btn.classList.add("holding");
    const draw = () => {
      if (hold !== h) return;
      btn.style.setProperty("--p", Math.min(1, (performance.now() - h.t0) / HOLD_MS).toFixed(3));
      h.raf = requestAnimationFrame(draw);
    };
    const done = () => {
      if (hold !== h || !btn.isConnected) return;
      const left = HOLD_MS - (performance.now() - h.t0);
      if (left > 0) { h.timer = setTimeout(done, left); return; }
      cancelAnimationFrame(h.raf);
      hold = null;
      btn.classList.remove("holding");
      btn.style.setProperty("--p", "1");
      if (navigator.vibrate) navigator.vibrate(30);
      confirmFix(id);
    };
    h.raf = requestAnimationFrame(draw);
    h.timer = setTimeout(done, HOLD_MS);
  };
  const stop = () => { if (hold && hold.btn === btn) cancelHold(); };
  btn.addEventListener("pointerdown", start);
  for (const ev of ["pointerup", "pointerleave", "pointercancel", "lostpointercapture", "blur", "contextmenu"]) btn.addEventListener(ev, stop);
  btn.addEventListener("keydown", (e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) start(e); });
  btn.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") stop(); });
  btn.addEventListener("click", (e) => e.preventDefault()); // a click is never a confirm
}

async function confirmFix(id) {
  const p = proposals.get(id);
  // One card is one proposal id, and this screen sends it at most once.
  if (!p || p.status !== "PENDING" || sentOnce.has(id) || (local.has(id) && local.get(id).state !== "NOTE")) return;
  sentOnce.add(id);
  local.set(id, { state: "SENDING" });
  drawFixes();
  const text = p.text || describe(p.action);
  try {
    const out = await call(`/api/actions/${encodeURIComponent(id)}/confirm`, { by: "console" }, 12000);
    local.delete(id);
    takeProposal(out, true);
    if (out.status === "CONFIRMED" || out.status === "SENDING") log("approve", `CONFIRMED fix: ${text}`);
    else if (out.status === "FAILED") { log("alert", `UNCERTAIN fix (sent, delivery not confirmed): ${text}`); }
  } catch (e) {
    if (e.status === 409) {
      local.delete(id);
      note("That fix was already decided on another screen.");
      refresh();
    } else if (e.status >= 400 && e.status < 500) {
      // Refused before anything was sent (no token, wrong PIN, unknown id). A new hold may try again.
      sentOnce.delete(id);
      local.set(id, { state: "NOTE", text: `${e.message} Nothing was sent.` });
    } else {
      // Timeout, dropped connection, or a server error after the request left: we can't tell.
      local.set(id, { state: "UNCERTAIN" });
      log("alert", `UNCERTAIN fix (sent, delivery not confirmed): ${text}`);
    }
  }
  drawFixes();
}

async function reject(id) {
  const p = proposals.get(id);
  if (!p || p.status !== "PENDING" || sentOnce.has(id)) return;
  try {
    takeProposal(await call(`/api/actions/${encodeURIComponent(id)}/reject`, { by: "console" }), true);
    log("whatif", `Rejected fix: ${p.text || describe(p.action)}`);
  } catch (e) {
    if (e.status === 409) { note("That fix was already decided on another screen."); refresh(); }
    else local.set(id, { state: "NOTE", text: e.message });
  }
  drawFixes();
}

function log(kind, text) {
  try { shiftlog.add(kind, text); if (H.logged) H.logged(); } catch { /* the log is a convenience here */ }
}

// --------------------------------------------------------------- Jarvis ----
async function ask(message, voice = false) {
  message = String(message || "").trim();
  if (!message || busy) return;
  if (!BASE) { note("Jarvis needs the backend. Start it with npm run demo."); return; }
  const sel = H.selected && H.selected();
  chat.push({ who: "you", text: message });
  busy = true;
  drawChat();
  try {
    const r = await call("/api/jarvis", { message, selected_asset_ids: sel ? [sel] : [] }, 30000);
    const chips = [...new Set((r.ui || []).filter((u) => u.action === "focus" && u.asset_id).map((u) => u.asset_id))];
    chat.push({ who: "jarvis", text: r.reply || "", chips, source: r.source });
    if (chips.length) {
      H.focus(chips[chips.length - 1]);
      highlight.flash("jarvis", chips, 8000);
    }
    for (const p of r.proposals || []) takeProposal(p, true);
    if ((r.proposals || []).length) drawFixes();
    if (voice) speak(r.reply);
  } catch (e) {
    chat.push({ who: "note", text: e.name === "AbortError" ? "Jarvis took too long to answer." : e.status === 503 ? `Jarvis can't answer that here: ${e.message}` : e.message });
  } finally {
    busy = false;
    drawChat();
  }
}

function note(text) {
  chat.push({ who: "note", text });
  drawChat();
}

function drawChat() {
  if (!root) return;
  const nice = H.niceName;
  const items = chat.slice(-40).map((m) => `<li class="m-${m.who}">
      ${m.who === "you" ? "<b>You</b>" : m.who === "jarvis" ? `<b>Jarvis</b>${m.source ? ` <span class="muted">${esc(m.source)}</span>` : ""}` : ""}
      <p>${esc(m.text)}</p>
      ${m.chips && m.chips.length ? `<div class="rm-chips">${m.chips.map((id) => `<button type="button" class="rm-chip" data-asset="${esc(id)}">${esc(nice(id))}</button>`).join("")}</div>` : ""}
    </li>`);
  if (busy) items.push(`<li class="m-jarvis"><b>Jarvis</b><p class="jv-wait">reading the field</p></li>`);
  if (!chat.length && !busy) items.push(`<li class="m-note"><p>Ask anything, or press a quick ask. "Why is Tank 3 rising?"</p></li>`);
  el.rmChat.innerHTML = items.join("");
  el.rmChat.querySelectorAll("[data-asset]").forEach((b) => { b.onclick = () => showAsset(b.dataset.asset); });
  el.rmChat.scrollTop = el.rmChat.scrollHeight;
  el.rmSay.disabled = busy;
}

function showAsset(id) {
  H.focus(id);
  highlight.flash("jarvis", [id], 8000);
}

// "+1 hour" and "Live" move the console's own forecast slider. Nothing is sent anywhere.
function shiftTime(spec) {
  const s = document.getElementById("time");
  if (!s) return;
  const max = Number(s.max) || 360;
  const next = spec === "0" ? 0 : Math.max(0, Math.min(max, Number(s.value) + Number(spec)));
  s.value = String(next);
  s.dispatchEvent(new Event("input", { bubbles: true }));
  const start = toMin(H.stateClock && H.stateClock());
  note(next === 0 ? "Back to now." : `Showing the do-nothing forecast at ${start == null ? `+${next} min` : clock12(start + next)}. Nothing was sent.`);
}

function speak(text) {
  try {
    if (!text || !window.speechSynthesis) return;
    speechSynthesis.cancel();
    speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  } catch { /* no voice here */ }
}

function mountMic() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return; // typing always works
  try {
    rec = new SR();
    rec.lang = "en-US";
    rec.interimResults = false;
    rec.maxAlternatives = 1;
  } catch { rec = null; return; }
  const mic = el.rmMic;
  mic.hidden = false;
  rec.onresult = (e) => {
    const said = e.results && e.results[0] && e.results[0][0] ? e.results[0][0].transcript : "";
    if (said) ask(said, true);
  };
  rec.onerror = (e) => { if (e.error && e.error !== "no-speech" && e.error !== "aborted") note(`Mic: ${e.error}. Type instead.`); };
  rec.onend = () => { listening = false; mic.classList.remove("listening"); mic.textContent = "Talk"; };
  mic.onclick = () => {
    if (listening) { try { rec.stop(); } catch { /* already stopped */ } return; }
    try {
      rec.start();
      listening = true;
      mic.classList.add("listening");
      mic.textContent = "Listening";
    } catch { note("The mic isn't available. Type instead."); }
  };
}

// --------------------------------------------------------------- alerts ----
function prioOf(a) {
  if (a.priority && PRIO[a.priority] != null) return a.priority;
  // Until the alerts API sends priority (CF-7), rank by severity. Marked as such on screen.
  return a.severity === "ALARM" || a.severity === "CRITICAL" ? "P1" : a.severity === "WARNING" ? "P2" : "P3";
}

function drawAlerts() {
  if (!root) return;
  const nice = H.niceName;
  const box = el.rmAlerts;
  if (reach === "none") { box.innerHTML = `<div class="empty">No backend, so no live alerts. Start it with npm run demo.</div>`; return; }
  if (reach === "down") { box.innerHTML = `<div class="rm-warn">Can't reach the backend, so alerts may be missing. Not the same as no alerts.</div>`; return; }
  const active = [...alerts.values()].filter((a) => a.active !== false);
  if (!active.length) { box.innerHTML = `<div class="empty">${reach === "ok" ? "No active alerts." : "Checking alerts."}</div>`; return; }
  const byId = new Map(active.map((a) => [a.id, a]));
  const kids = new Map();
  const roots = [];
  for (const a of active) {
    if (a.root_id && a.root_id !== a.id && byId.has(a.root_id)) {
      if (!kids.has(a.root_id)) kids.set(a.root_id, []);
      kids.get(a.root_id).push(a);
    } else roots.push(a);
  }
  const rank = (a) => [a.shelved_until ? 1 : 0, PRIO[prioOf(a)], a.minutes_to_consequence ?? 1e9];
  roots.sort((a, b) => {
    const ra = rank(a), rb = rank(b);
    for (let i = 0; i < ra.length; i++) if (ra[i] !== rb[i]) return ra[i] - rb[i];
    return (b.t || "") > (a.t || "") ? 1 : -1;
  });
  box.innerHTML = roots.map((a) => {
    const pr = prioOf(a);
    const rel = kids.get(a.id) || [];
    const mins = a.minutes_to_consequence != null ? ` <span class="rm-mins">${Math.round(a.minutes_to_consequence)} min</span>` : "";
    return `<article class="rm-alert ${pr}${a.acked ? " acked" : ""}${a.shelved_until ? " shelved" : ""}" data-id="${esc(a.id)}">
      <div class="rm-alert-top">
        <span class="rm-prio ${pr}" title="${a.priority ? "Priority from the forecast" : "Priority from severity until the alerts API sends one"}">${pr}</span>
        <b>${esc(nice(a.asset_id))}</b>${mins}
        <span class="muted">${esc(a.clock || "")}</span>
      </div>
      <div class="rm-alert-msg">${esc(a.message || a.kind || "")}</div>
      ${a.shelved_until ? `<div class="rm-note">Shelved until ${esc(a.shelved_until)}${a.shelve_reason ? `: ${esc(a.shelve_reason)}` : ""}</div>` : ""}
      ${rel.length ? `<details class="rm-rel"><summary>+${rel.length} related</summary>${rel.map((k) => `<div><span class="rm-prio ${prioOf(k)}">${prioOf(k)}</span> ${esc(nice(k.asset_id))}: ${esc(k.message || k.kind)}</div>`).join("")}</details>` : ""}
      <div class="rm-btns">
        ${a.asset_id ? `<button type="button" data-asset="${esc(a.asset_id)}">Show</button>` : ""}
        ${a.acked ? `<span class="muted">Acknowledged</span>` : `<button type="button" data-ack="${esc(a.id)}">Ack</button>`}
      </div>
    </article>`;
  }).join("");
  box.querySelectorAll("[data-asset]").forEach((b) => { b.onclick = () => showAsset(b.dataset.asset); });
  box.querySelectorAll("[data-ack]").forEach((b) => { b.onclick = () => ack(b.dataset.ack, b); });
}

async function ack(id, btn) {
  btn.disabled = true;
  try {
    const out = await call(`/api/alerts/${encodeURIComponent(id)}/ack`, {});
    alerts.set(id, { ...(alerts.get(id) || {}), ...out, acked: true });
    const a = alerts.get(id);
    log("alert", `Acknowledged: ${a.message || a.kind || id}`);
  } catch (e) {
    note(`Couldn't acknowledge that alert: ${e.message}`);
  }
  drawAlerts();
}
