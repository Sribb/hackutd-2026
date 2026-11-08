// Jarvis side panel (#44). A side panel, never the main interface.
//
//   POST /api/jarvis            { message, selected_asset_ids?, image_png_base64? }
//                               -> { reply, source: "rules"|"claude", ui: [{type:"ui", action:"focus", asset_id}], proposals: [...] }
//   GET  /api/actions           -> { proposals }
//   POST /api/actions/{id}/confirm | /reject   -> the updated proposal
//   GET  /api/alerts            -> { alerts: [{ id, asset_id, kind, severity, message, t }] }
//   /ws                         { type: "state"|"alert"|"proposal"|"ui", ... }
//
// Nothing reaches equipment without Confirm, and Confirm is a press and hold.

import { BASE } from "./api.js";
import { holdButton, decode } from "./fx.js";
import * as shiftlog from "./shiftlog.js";
import * as liveFeed from "./live.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const nice = (id) => String(id || "").replace(/_/g, " ").replace(/\b([A-Z])([A-Z]*)\b/g, (_, a, b) => a + b.toLowerCase()).replace(/\bXo\b/, "XO").replace(/\bSwd\b/, "SWD");

let hooks = null;   // { stage, selected() -> id|null, focus(id), logged(), snapshot() -> base64 png }
let open = false;
let busy = false;
const chat = [];    // { who: "you"|"jarvis"|"note", text, source? }
const proposals = new Map();
const alerts = new Map();

export function mountJarvis(h) {
  hooks = h;
  const btn = document.createElement("button");
  btn.id = "jarvisBtn";
  btn.className = "jarvis-btn";
  btn.textContent = "Jarvis";
  h.stage.appendChild(btn);
  const panel = document.createElement("section");
  panel.id = "jarvis";
  panel.className = "jarvis";
  panel.hidden = true;
  h.stage.appendChild(panel);
  btn.onclick = () => (open ? hide() : show());
  addEventListener("freeboard:jarvis", (e) => {
    const id = e.detail && e.detail.asset_id;
    show();
    if (id && e.detail.from === "pinch") ask(`What's going on with ${nice(id)}?`, [id]);
  });
  // The socket is shared (live.js), so alerts, proposals and focus come from there.
  liveFeed.on("alert", (msg) => onSocket(msg));
  liveFeed.on("proposal", (msg) => onSocket(msg));
  liveFeed.on("ui", (msg) => onSocket(msg));
  liveFeed.on("feed", () => { if (open) draw(); });
}

function show() {
  open = true;
  $("jarvis").hidden = false;
  $("jarvisBtn").classList.add("on");
  if (BASE) refresh();
  draw();
  setTimeout(() => $("jvIn") && $("jvIn").focus(), 0);
}

function hide() {
  open = false;
  $("jarvis").hidden = true;
  $("jarvisBtn").classList.remove("on");
}

async function call(path, body) {
  const r = await fetch(`${BASE}${path}`, body === undefined ? undefined
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(typeof out.detail === "string" ? out.detail : `HTTP ${r.status} from ${path}`), { status: r.status });
  return out;
}

async function refresh() {
  try {
    const [a, p] = await Promise.all([call("/api/alerts"), call("/api/actions")]);
    for (const x of a.alerts || []) alerts.set(x.id, x);
    for (const x of p.proposals || []) proposals.set(x.id, x);
    draw();
  } catch { /* the panel still works for chat; the badge shows the backend is missing */ }
}

function onSocket(msg) {
  if (msg.type === "alert" && msg.alert) {
    const isNew = !alerts.has(msg.alert.id);
    alerts.set(msg.alert.id, msg.alert);
    if (isNew) {
      shiftlog.add("alert", `${msg.alert.severity}: ${msg.alert.message}`);
      hooks.logged();
      $("jarvisBtn").classList.add("ping");
    }
  } else if (msg.type === "proposal" && msg.proposal) {
    proposals.set(msg.proposal.id, msg.proposal);
  } else if (msg.type === "ui" && msg.action === "focus" && msg.asset_id) {
    hooks.focus(msg.asset_id);
  }
  if (open) draw();
}

async function ask(message, ids, withView = false) {
  if (!message.trim() || busy) return;
  if (!BASE) { chat.push({ who: "note", text: "Jarvis needs the backend. Start it and set VITE_API." }); draw(); return; }
  const selected = ids || (hooks.selected() ? [hooks.selected()] : []);
  chat.push({ who: "you", text: message, about: selected });
  busy = true;
  draw();
  try {
    const body = { message, selected_asset_ids: selected };
    if (withView) body.image_png_base64 = hooks.snapshot();
    const r = await call("/api/jarvis", body);
    chat.push({ who: "jarvis", text: r.reply || "", source: r.source });
    for (const u of r.ui || []) if (u.action === "focus" && u.asset_id) hooks.focus(u.asset_id);
    for (const p of r.proposals || []) proposals.set(p.id, p);
  } catch (e) {
    chat.push({ who: "note", text: e.status === 503 ? `Jarvis can't answer that here: ${e.message}` : e.message });
  } finally {
    busy = false;
    draw();
  }
}

async function decide(p, verb) {
  try {
    const out = await call(`/api/actions/${encodeURIComponent(p.id)}/${verb}`, { by: "console" });
    proposals.set(out.id, out);
    shiftlog.add(verb === "confirm" ? "approve" : "whatif", `${verb === "confirm" ? "CONFIRMED" : "Rejected"} fix: ${p.text || describe(p.action)} ${p.reason || ""}`.trim());
    hooks.logged();
  } catch (e) {
    chat.push({ who: "note", text: e.status === 409 ? "That fix was already decided elsewhere." : e.message });
    refresh();
  }
  draw();
}

function describe(a) {
  if (!a) return "";
  if (a.type === "SET_VALVE") return `set ${nice(a.asset_id)} ${String(a.value).toLowerCase()}`;
  if (a.type === "SET_PUMP") return `turn ${nice(a.asset_id)} ${String(a.value).toLowerCase()}`;
  if (a.type === "CHOKE_WELL") return a.value === 0 ? `shut in ${nice(a.asset_id)}` : `choke ${nice(a.asset_id)} to ${Math.round(a.value * 100)}%`;
  if (a.type === "SEND_TRUCK") return `send ${nice(a.asset_id)}${a.target_id ? ` to ${nice(a.target_id)}` : ""}`;
  return `${a.type} ${a.asset_id}`;
}

function draw() {
  if (!open) return;
  $("jarvisBtn").classList.remove("ping");
  const sel = hooks.selected();
  // Resolved alerts (active: false, #38) drop off the panel; the shift log keeps them.
  const live = [...alerts.values()].filter((a) => a.active !== false).sort((a, b) => (b.t > a.t ? 1 : -1)).slice(0, 4);
  const pend = [...proposals.values()].sort((a, b) => (b.created_t > a.created_t ? 1 : -1));
  // Redraws come from the socket too, so keep whatever the operator is typing.
  const typed = $("jvIn") ? $("jvIn").value : "";
  const typing = document.activeElement === $("jvIn");
  $("jarvis").innerHTML = `
    <header class="jv-head"><b>Jarvis</b><span class="muted">${!BASE ? "no backend" : liveFeed.live.connected ? "live" : liveFeed.live.lostSince ? "feed lost" : "connecting"}</span><button id="jvClose">Close</button></header>
    ${live.length ? `<div class="jv-alerts">${live.map((a) => `<div class="jv-alert ${a.severity}" data-id="${esc(a.asset_id)}"><span class="status ${a.severity}"><i></i>${esc(a.kind.replace(/_/g, " "))}</span> ${esc(a.message)}</div>`).join("")}</div>` : ""}
    <ol class="jv-chat" id="jvChat">${chat.map((m, i) => `<li class="m-${m.who}">${m.who === "you" ? "<b>You</b>" : m.who === "jarvis" ? `<b>Jarvis</b>${m.source ? ` <span class="muted">${esc(m.source)}</span>` : ""}` : ""}
        <p ${m.who === "jarvis" && i === chat.length - 1 ? `data-decode="jv${i}"` : ""}>${esc(m.text)}</p></li>`).join("")}
      ${busy ? `<li class="m-jarvis"><b>Jarvis</b><p class="jv-wait">reading the field</p></li>` : ""}
      ${!chat.length && !busy ? `<li class="m-note"><p>Ask about the field. "Why is Tank 3 rising?" Select or pinch an asset to ask about it.</p></li>` : ""}</ol>
    ${pend.length ? `<div class="jv-props"><div class="kicker">Proposed fixes</div>${pend.map((p) => `
      <div class="jv-prop ${p.status}">
        <div><b>${esc(p.text || describe(p.action))}</b> <span class="muted">${esc(p.status)}${p.decided_by ? ` by ${esc(p.decided_by)}` : ""}</span></div>
        ${p.reason ? `<div class="muted">${esc(p.reason)}</div>` : ""}
        ${p.result ? `<div class="muted">${esc(typeof p.result === "string" ? p.result : JSON.stringify(p.result))}</div>` : ""}
        ${p.status === "PENDING" ? `<div class="plan-btns"><button data-reject="${esc(p.id)}">Reject</button><button class="primary hold" data-confirm="${esc(p.id)}"><i></i><span>Hold to confirm</span></button></div>` : ""}
      </div>`).join("")}</div>` : ""}
    <form id="jvForm" class="jv-form">
      ${sel ? `<span class="jv-chip">${esc(sel)}</span>` : ""}
      <input id="jvIn" placeholder="${sel ? `Ask about ${esc(nice(sel))}` : "Ask Jarvis"}" autocomplete="off" ${busy ? "disabled" : ""}>
      <button type="button" id="jvView" title="Send a picture of the 3D view with the question">What am I looking at</button>
    </form>`;
  $("jvIn").value = typed;
  if (typing) $("jvIn").focus();
  $("jvClose").onclick = hide;
  $("jvForm").onsubmit = (e) => { e.preventDefault(); const v = $("jvIn").value; $("jvIn").value = ""; ask(v); };
  $("jvView").onclick = () => ask($("jvIn").value.trim() || "What am I looking at?", undefined, true);
  $("jarvis").querySelectorAll(".jv-alert").forEach((el) => { el.onclick = () => el.dataset.id && hooks.focus(el.dataset.id); });
  $("jarvis").querySelectorAll("[data-reject]").forEach((b) => { b.onclick = () => decide(proposals.get(b.dataset.reject), "reject"); });
  $("jarvis").querySelectorAll("[data-confirm]").forEach((b) => holdButton(b, () => decide(proposals.get(b.dataset.confirm), "confirm")));
  const list = $("jvChat");
  list.scrollTop = list.scrollHeight;
  decode($("jarvis"));
}

// main.js calls this when the selection changes, so the chip and placeholder follow it.
export function jarvisSelectionChanged() {
  if (open) draw();
}
