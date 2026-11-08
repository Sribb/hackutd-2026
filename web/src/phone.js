// Phone view (#55): what a night operator keeps in their pocket.
// Live alerts, newest first, and pending fixes with hold-to-confirm or reject. Same rule as
// the console: nothing moves without a person confirming. No login (demo only).

import "@fontsource/gloock/400.css";
import "@fontsource/azeret-mono/400.css";
import "@fontsource/azeret-mono/600.css";
import "./phone.css";
import * as liveFeed from "./live.js";
import { authFetch } from "./auth.js";
import { holdButton, afterHold, isHolding } from "./fx.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const nice = (id) => String(id || "").replace(/_/g, " ").replace(/\b([A-Z])([A-Z]*)\b/g, (_, a, b) => a + b.toLowerCase()).replace(/\bXo\b/, "XO").replace(/\bSwd\b/, "SWD");

// Which backend to talk to. A phone can't reach the laptop's 127.0.0.1, so:
//   ?api=http://192.168.1.20:8000   wins, for this page load only (never remembered: one
//                                   crafted link must not point this phone elsewhere for good);
//   else VITE_API, with a loopback host swapped for the host this page came from;
//   else this page's host on port 8000.
function apiBase() {
  const q = new URLSearchParams(location.search).get("api");
  // Older builds saved ?api= here. Drop it so a link opened once can't stick.
  try { localStorage.removeItem("freeboard.phone.api"); } catch { /* private mode */ }
  const env = import.meta.env.VITE_API === "same-origin" ? location.origin : import.meta.env.VITE_API;
  let u;
  try { u = new URL(q || env || `${location.protocol}//${location.hostname}:8000`); } catch { u = new URL(location.origin); }
  if (/^(127\.0\.0\.1|localhost|\[::1\])$/.test(u.hostname) && !/^(127\.0\.0\.1|localhost)$/.test(location.hostname)) u.hostname = location.hostname;
  return u.origin;
}
const BASE = apiBase();

const alerts = new Map();
const proposals = new Map();
const fresh = new Set(); // alert ids that arrived while the page was open
let loadError = "";      // why the last GET failed, or "" once one worked

// True when this page can't be sure it is hearing alerts: the last fetch failed and the
// socket isn't open, or the live feed was lost. Then the page must never look calm.
function deaf() {
  const L = liveFeed.live;
  return !!L.lostSince || (!!loadError && !L.connected);
}

async function call(path, body) {
  const r = await authFetch(`${BASE}${path}`, body === undefined ? undefined
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(typeof out.detail === "string" ? out.detail : `HTTP ${r.status}`), { status: r.status });
  return out;
}

async function load() {
  try {
    const [a, p] = await Promise.all([call("/api/alerts"), call("/api/actions")]);
    for (const x of a.alerts || []) alerts.set(x.id, x);
    for (const x of p.proposals || []) proposals.set(x.id, x);
    loadError = "";
  } catch (e) {
    loadError = e.message || "no answer";
  }
  draw();
}

function buzz() {
  if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
  document.body.classList.remove("flash");
  void document.body.offsetWidth; // restart the animation
  document.body.classList.add("flash");
}

liveFeed.on("alert", (msg) => {
  if (!msg.alert) return;
  if (!alerts.has(msg.alert.id) && msg.alert.active !== false) { fresh.add(msg.alert.id); buzz(); }
  alerts.set(msg.alert.id, msg.alert);
  draw();
});
liveFeed.on("proposal", (msg) => {
  if (!msg.proposal) return;
  const isNew = !proposals.has(msg.proposal.id);
  proposals.set(msg.proposal.id, msg.proposal);
  if (isNew && msg.proposal.status === "PENDING") buzz();
  draw();
});
// The feed going down or coming back changes whether the alert list may look calm.
// Coming back, re-read the lists: alerts raised while we were deaf never reached the socket.
let wasDeaf = false;
liveFeed.on("feed", () => {
  drawFeed();
  const d = deaf();
  if (wasDeaf && !d) load(); else draw();
  wasDeaf = d;
});
liveFeed.on("state", drawFeed);
setInterval(() => { if (loadError) load(); }, 15000);

function drawFeed() {
  const L = liveFeed.live, el = $("phFeed");
  if (L.lostSince) {
    const s = Math.floor((Date.now() - L.lostSince) / 1000);
    el.className = "ph-feed lost";
    el.textContent = `feed lost ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  } else {
    el.className = `ph-feed ${L.connected ? "ok" : ""}`;
    el.textContent = L.connected ? "live" : "connecting";
  }
}
setInterval(() => { if (liveFeed.live.lostSince) drawFeed(); }, 1000);

function describe(a) {
  if (!a) return "";
  if (a.type === "SET_VALVE") return `${String(a.value).toLowerCase() === "open" ? "Open" : "Close"} ${nice(a.asset_id)}`;
  if (a.type === "SET_PUMP") return `Turn ${nice(a.asset_id)} ${String(a.value).toLowerCase()}`;
  if (a.type === "CHOKE_WELL") return a.value === 0 ? `Shut in ${nice(a.asset_id)}` : `Choke ${nice(a.asset_id)} to ${Math.round(a.value * 100)}%`;
  if (a.type === "SEND_TRUCK") return `Send ${nice(a.asset_id)}${a.target_id ? ` to ${nice(a.target_id)}` : ""}`;
  return `${a.type} ${a.asset_id}`;
}

function draw() {
  // Never swap a hold-to-confirm button out from under a finger: redraw when the hold ends.
  if (isHolding()) { afterHold(draw); return; }
  // Active alerts first, newest first; resolved ones (active: false) sink and dim.
  const list = [...alerts.values()].sort((a, b) => (a.active === false) - (b.active === false)
    || (String(b.t) > String(a.t) ? 1 : String(b.t) < String(a.t) ? -1 : 0)).slice(0, 12);
  const down = deaf()
    ? `<p class="ph-down" role="alert">Can't reach the backend. Alerts are NOT being received.${loadError ? ` <span>(${esc(loadError)})</span>` : ""}</p>`
    : "";
  $("phAlerts").innerHTML = down + (list.length
    ? list.map((a) => `<article class="ph-alert ${esc(a.severity)} ${fresh.has(a.id) ? "new" : ""} ${a.active === false ? "resolved" : ""}">
        <div class="ph-meta"><span class="sev">${a.active === false ? "RESOLVED" : esc(a.severity)}</span><span>${esc(String(a.kind || "").replace(/_/g, " "))}</span><time>${esc(clockOf(a))}</time></div>
        <h2>${esc(a.message)}</h2>
        <div class="ph-row">${a.asset_id ? `<span class="ph-asset">${esc(nice(a.asset_id))}</span>` : ""}
          ${a.asset_id ? `<button class="ph-show" data-show="${esc(a.asset_id)}">Show</button>` : ""}
          ${a.active !== false && !a.acked ? `<button class="ph-ack" data-ack="${esc(a.id)}">Ack</button>` : a.acked ? `<span class="ph-asset">acked</span>` : ""}</div>
      </article>`).join("")
    : down ? "" : `<p class="ph-empty">No alerts. This page buzzes when one comes in.</p>`);

  const pend = [...proposals.values()].filter((p) => p.status === "PENDING");
  const done = [...proposals.values()].filter((p) => p.status !== "PENDING").slice(-3).reverse();
  $("phProps").innerHTML = (pend.length ? `<h3>Waiting for you</h3>` : "") + pend.map((p) => `
      <article class="ph-prop">
        ${p.run_id ? `<div class="ph-asset">Plan run · step ${esc(p.step)}</div>` : ""}
        <h2>${esc(p.text || describe(p.action))}</h2>
        ${p.reason ? `<p>${esc(p.reason)}</p>` : ""}
        <div class="ph-btns">
          <button class="ph-reject" data-reject="${esc(p.id)}">Reject</button>
          <button class="ph-confirm hold" data-confirm="${esc(p.id)}"><i></i><span>Hold to confirm</span></button>
        </div>
      </article>`).join("")
    + (done.length ? `<h3>Decided</h3>` + done.map((p) => `<div class="ph-done ${esc(p.status)}"><b>${esc(DONE_WORD[p.status] || p.status)}</b> ${esc(p.text || describe(p.action))}${p.decided_by ? ` <span>(${p.status === "REJECTED" ? "rejected" : "confirmed"} ${esc(WHERE[p.decided_by] || `by ${p.decided_by}`)})</span>` : ""}${p.status === "UNCERTAIN" ? `<p>No answer from the device: it may have moved. Check it in person. Not retried.</p>` : p.status === "FAILED" && p.result ? `<p>${esc(p.result)}</p>` : ""}</div>`).join("") : "");
  $("phAlerts").querySelectorAll("[data-ack]").forEach((b) => { b.onclick = () => ack(b.dataset.ack); });
  // "Show" points every open console at the asset (ws ui focus, #38).
  $("phAlerts").querySelectorAll("[data-show]").forEach((b) => {
    b.onclick = () => { if (!liveFeed.send({ type: "ui", action: "focus", asset_id: b.dataset.show })) $("phFoot").textContent = "Not connected, so the consoles can't be pointed."; };
  });
  $("phProps").querySelectorAll("[data-reject]").forEach((b) => { b.onclick = () => decide(b.dataset.reject, "reject"); });
  $("phProps").querySelectorAll("[data-confirm]").forEach((b) => holdButton(b, () => decide(b.dataset.confirm, "confirm"), 1500));
  $("phFoot").innerHTML = `Backend ${esc(BASE)} · no login (demo only)`;
}

// A sent command is never shown as done until the connector says so (SENDING), and one with no
// answer (UNCERTAIN) may have moved the device.
const WHERE = { console: "on the console", phone: "on a phone" };
const DONE_WORD = { SENDING: "Sending…", CONFIRMED: "Sent", REJECTED: "Rejected", FAILED: "Failed", UNCERTAIN: "Uncertain" };

// The live clock ("01:48") if the backend sent one, else the time part of the ISO stamp.
function clockOf(a) {
  if (a.clock) return a.clock;
  const m = String(a.t || "").match(/T(\d\d:\d\d)/);
  return m ? m[1] : String(a.t || "");
}

// Acknowledge = "I've seen it". It doesn't resolve the alert or change equipment.
async function ack(id) {
  try {
    const out = await call(`/api/alerts/${encodeURIComponent(id)}/ack`, {});
    alerts.set(id, { ...alerts.get(id), acked: true, ...(out && out.id ? out : {}) });
  } catch (e) {
    $("phFoot").textContent = e.message;
  }
  draw();
}

async function decide(id, verb) {
  try {
    const out = await call(`/api/actions/${encodeURIComponent(id)}/${verb}`, { by: "phone" });
    proposals.set(out.id, out);
  } catch (e) {
    $("phFoot").textContent = e.status === 409 ? "Already decided on another screen." : e.message;
    await load();
    return;
  }
  draw();
}

// Keep the screen awake once the operator touches it (a pocket page that sleeps can't buzz).
addEventListener("pointerdown", async () => {
  try { if (navigator.wakeLock) await navigator.wakeLock.request("screen"); } catch { /* not allowed here */ }
}, { once: true });

liveFeed.start(BASE, "phone");
load();
draw();
