// Phone view (#55): what a night operator keeps in their pocket.
// Live alerts, newest first, and pending fixes with hold-to-confirm or reject. Same rule as
// the console: nothing moves without a person confirming. No login (demo only).

import "@fontsource/gloock/400.css";
import "@fontsource/azeret-mono/400.css";
import "@fontsource/azeret-mono/600.css";
import "./phone.css";
import * as liveFeed from "./live.js";
import { holdButton } from "./fx.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const nice = (id) => String(id || "").replace(/_/g, " ").replace(/\b([A-Z])([A-Z]*)\b/g, (_, a, b) => a + b.toLowerCase()).replace(/\bXo\b/, "XO").replace(/\bSwd\b/, "SWD");

// Which backend to talk to. A phone can't reach the laptop's 127.0.0.1, so:
//   ?api=http://192.168.1.20:8000   wins and is remembered on this phone;
//   else VITE_API, with a loopback host swapped for the host this page came from;
//   else this page's host on port 8000.
function apiBase() {
  const q = new URLSearchParams(location.search).get("api");
  try { if (q) localStorage.setItem("freeboard.phone.api", q); } catch { /* private mode */ }
  let saved = null;
  try { saved = localStorage.getItem("freeboard.phone.api"); } catch { /* ignore */ }
  const pick = q || saved || import.meta.env.VITE_API || `${location.protocol}//${location.hostname}:8000`;
  const u = new URL(pick);
  if (/^(127\.0\.0\.1|localhost|\[::1\])$/.test(u.hostname) && !/^(127\.0\.0\.1|localhost)$/.test(location.hostname)) u.hostname = location.hostname;
  return u.origin;
}
const BASE = apiBase();

const alerts = new Map();
const proposals = new Map();
const fresh = new Set(); // alert ids that arrived while the page was open

async function call(path, body) {
  const r = await fetch(`${BASE}${path}`, body === undefined ? undefined
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
  } catch (e) {
    $("phFoot").textContent = `Can't reach ${BASE}: ${e.message}`;
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
  if (!alerts.has(msg.alert.id)) { fresh.add(msg.alert.id); buzz(); }
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
liveFeed.on("feed", drawFeed);
liveFeed.on("state", drawFeed);

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
  const list = [...alerts.values()].sort((a, b) => (String(b.t) > String(a.t) ? 1 : String(b.t) < String(a.t) ? -1 : 0));
  $("phAlerts").innerHTML = list.length
    ? list.map((a) => `<article class="ph-alert ${esc(a.severity)} ${fresh.has(a.id) ? "new" : ""}">
        <div class="ph-meta"><span class="sev">${esc(a.severity)}</span><span>${esc(String(a.kind || "").replace(/_/g, " "))}</span><time>${esc(a.t || "")}</time></div>
        <h2>${esc(a.message)}</h2>
        ${a.asset_id ? `<div class="ph-asset">${esc(nice(a.asset_id))}</div>` : ""}
      </article>`).join("")
    : `<p class="ph-empty">No alerts. This page buzzes when one comes in.</p>`;

  const pend = [...proposals.values()].filter((p) => p.status === "PENDING");
  const done = [...proposals.values()].filter((p) => p.status !== "PENDING").slice(-3).reverse();
  $("phProps").innerHTML = (pend.length ? `<h3>Waiting for you</h3>` : "") + pend.map((p) => `
      <article class="ph-prop">
        <h2>${esc(describe(p.action))}</h2>
        ${p.reason ? `<p>${esc(p.reason)}</p>` : ""}
        <div class="ph-btns">
          <button class="ph-reject" data-reject="${esc(p.id)}">Reject</button>
          <button class="ph-confirm hold" data-confirm="${esc(p.id)}"><i></i><span>Hold to confirm</span></button>
        </div>
      </article>`).join("")
    + (done.length ? `<h3>Decided</h3>` + done.map((p) => `<div class="ph-done ${esc(p.status)}"><b>${esc(p.status)}</b> ${esc(describe(p.action))}</div>`).join("") : "");
  $("phProps").querySelectorAll("[data-reject]").forEach((b) => { b.onclick = () => decide(b.dataset.reject, "reject"); });
  $("phProps").querySelectorAll("[data-confirm]").forEach((b) => holdButton(b, () => decide(b.dataset.confirm, "confirm"), 1500));
  $("phFoot").innerHTML = `Backend ${esc(BASE)} · no login (demo only)`;
}

async function decide(id, verb) {
  try {
    const out = await call(`/api/actions/${encodeURIComponent(id)}/${verb}`, {});
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

liveFeed.start(BASE);
load();
draw();
