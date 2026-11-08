// Dataset console: the sponsor's "Beat the Shutdown" data on one page.
// One source of numbers (#148): detection, countdowns, plans, Last Call, the what-if, the
// handoff and events.json all come from the backend's dataset mode (/api/eog, eog.js).
// The page itself only draws the raw files (network.json, ops.json, telemetry.csv): the
// map, the 3D field and the sensor charts. "Now" comes from the telemetry, never the
// system clock, and any past moment can be replayed as "now".

import "@fontsource/gloock/400.css";
import "@fontsource/great-vibes/400.css"; // the gaia mark, same as the console
import "@fontsource/azeret-mono/400.css";
import "@fontsource/azeret-mono/600.css";
import "./dataset.css";
import { fileSource, backendFileSource, assetIndex, checkDataset, STEP_S } from "./data.js";
import { lineChart, fmtTime } from "./chart.js";
import { normAlerts, sortAlerts, actionLines, durationText, categoryOf, money } from "./alerts.js";
import { eogClient } from "./eog.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const params = new URLSearchParams(location.search);
// npm run demo sets VITE_API=same-origin: the dev server proxies /api to the backend.
const RAW_API = params.get("api") ?? import.meta.env.VITE_API ?? "";
const API = (RAW_API === "same-origin" ? "" : RAW_API).replace(/\/$/, "");

const state = {
  eog: eogClient(API),
  backend: null, // the last GET /api/eog/datasets answer, or null when dataset mode is off
  resolved: [], // resolved events up to the moment on screen (/api/eog/events)
  plansBy: new Map(), // event id -> its /api/eog/plans item (plan, alternatives, wait curve)
  source: null,
  folder: null,
  network: null,
  ops: null,
  store: null,
  assets: null,
  at: null,
  alerts: [],
  detection: "loading", // loading | ok | absent | error
  detectionNote: "",
  selected: null, // { kind: "facility" | "alert" | "system", id }
  win: 24 * 3600,
  view: "map", // map | 3d
  sys3d: null,
  plans: new Map(), // per alert and moment: the what-if plan the operator is editing
  approved: new Map(), // per alert and moment: the approve answer (steps, radio script)
  loadSeq: 0,
  alertSeq: 0,
};

function setStatus(text, tone = "") {
  const el = $("dsStatus");
  el.textContent = text;
  el.dataset.tone = tone;
  el.hidden = !text;
}

// ---------- source and folders ----------

async function pickSource() {
  // The raw files come from the dev server's /eog/ folder (EOG_DIR or FREEBOARD_EOG_DIR,
  // the same folder the backend reads). Detection and plans come from /api/eog.
  try { state.backend = await state.eog.datasets(); } catch { state.backend = null; }
  let files = fileSource("/eog"), info;
  try { info = await files.info(); } catch (e) {
    // A built page (dist/) has no /eog/: read the same files through the backend.
    files = backendFileSource(API);
    info = await files.info().catch(() => { throw e; });
  }
  return { source: files, info };
}

// The folder to open: a folder added since the last visit (the sponsor adds test/ right
// before judging, and it should open first); else the one in the URL; else the last one
// used; else test/ if there is one; else the first.
function folderFromUrl(folders) {
  const want = params.get("folder");
  const testFirst = (list) => list.find((f) => f === "test" || f.endsWith("/test")) || list[0];
  // Known folders per source, so switching between the backend and the files doesn't make
  // every folder look new.
  const kKey = `freeboard.dataset.known:${state.source?.kind || "files"}`;
  let known = null, last = null;
  try { known = JSON.parse(localStorage.getItem(kKey) || "null"); } catch {} // corrupt: start over
  try {
    last = localStorage.getItem("freeboard.dataset.folder");
    localStorage.setItem(kKey, JSON.stringify(folders));
  } catch {}
  // A new folder wins once, even over the URL: a reload at judging must open test/, and the
  // page writes ?folder= on every load.
  const added = Array.isArray(known) ? folders.filter((f) => !known.includes(f)) : [];
  if (added.length) return testFirst(added);
  if (want && folders.includes(want)) return want;
  const active = state.backend?.active;
  if (active && folders.includes(active)) return active; // what the backend (and the console) show now
  if (last && folders.includes(last)) return last;
  return testFirst(folders);
}

async function boot() {
  let picked;
  try {
    picked = await pickSource();
  } catch (e) {
    setStatus("No dataset found. Run npm run demo with FREEBOARD_EOG_DIR pointing at the sponsor data.", "alarm");
    $("dsFoot").textContent = String(e.message || e);
    return;
  }
  state.source = picked.source;
  const folders = await picked.source.folders();
  if (!folders.length) {
    setStatus("The dataset folder has no network.json, ops.json and telemetry.csv sets.", "alarm");
    return;
  }
  const sel = $("dsFolder");
  sel.innerHTML = folders.map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join("");
  sel.value = folderFromUrl(folders);
  sel.addEventListener("change", () => loadFolder(sel.value));
  await loadFolder(sel.value);
}

async function loadFolder(folder) {
  const seq = ++state.loadSeq;
  clearTimeout(slideTimer);
  state.alertSeq++; // an alerts request for the previous folder must not land on this one
  state.folder = folder;
  state.selected = null;
  state.plans.clear();
  state.approved.clear();
  state.picked?.clear(); // picks name this folder's sensors
  state.alerts = [];
  state.resolved = [];
  state.plansBy = new Map();
  state.planSystems = [];
  state.detection = "loading";
  state.storeFolder = null; // the readings on screen belong to no folder until this one loads
  $("dsAt").disabled = true; // no time travel on the old folder's readings under the new name
  try { localStorage.setItem("freeboard.dataset.folder", folder); } catch {}
  const url = new URL(location.href);
  url.searchParams.set("folder", folder);
  url.searchParams.delete("at");
  history.replaceState(null, "", url);
  setStatus(`Loading ${folder}…`);
  let data;
  try {
    data = await state.source.load(folder, (got, total) => {
      if (seq !== state.loadSeq) return;
      const mb = (n) => (n / 1e6).toFixed(1);
      setStatus(`Loading ${folder}: telemetry.csv ${mb(got)}${total ? ` of ${mb(total)}` : ""} MB`);
    });
  } catch (e) {
    if (seq !== state.loadSeq) return;
    setStatus(`Could not load ${folder}: ${e.message}`, "alarm");
    // Never leave the previous folder's map and alerts up under this folder's name.
    state.store = state.network = state.ops = null;
    state.selected = null;
    $("dsMap").innerHTML = "";
    $("dsAlerts").innerHTML = "";
    $("dsAlertCount").textContent = "";
    $("dsResolvedWrap").hidden = true;
    $("dsDetail").innerHTML = "";
    $("dsFoot").textContent = "";
    return;
  }
  if (seq !== state.loadSeq) return;
  Object.assign(state, data);
  state.storeFolder = folder;
  state.assets = assetIndex(data.network);
  const problems = checkDataset(data.network, data.ops);
  const atParam = Number(params.get("at"));
  state.at = Number.isFinite(atParam) && atParam >= data.store.start && atParam <= data.store.now && folder === params.get("folder")
    ? atParam - ((atParam - data.store.start) % STEP_S) : data.store.now;
  setupSlider();
  f3dKey = null; // a new folder can reuse system ids with a different layout
  setStatus(problems.length ? `This dataset has problems the page cannot work around: ${problems.join(" ")}` : "", problems.length ? "alarm" : "");
  renderAll();
  await refreshAlerts();
}

// ---------- time travel ----------

let slideTimer = null; // a pending alerts refresh from the slider; a folder load cancels it

function setupSlider() {
  const s = $("dsAt");
  s.disabled = false;
  const loadSeq = state.loadSeq; // this slider belongs to this load only
  // Start an hour in: a detector needs some history before "now".
  s.min = String(Math.min(state.store.now, state.store.start + 3600));
  s.max = String(state.store.now);
  s.step = String(STEP_S);
  s.value = String(state.at);
  s.oninput = () => {
    if (loadSeq !== state.loadSeq) return;
    state.at = Number(s.value);
    // The alerts on screen belong to the old moment: never show them as current.
    state.alertSeq++;
    state.alerts = [];
    state.detection = "loading";
    renderNow();
    renderMap();
    renderAlerts();
    renderDetail(); // a plan or what-if on screen belongs to the old moment
    clearTimeout(slideTimer);
    slideTimer = setTimeout(() => { if (loadSeq === state.loadSeq) { syncAtToUrl(); refreshAlerts(); } }, 250);
  };
  $("dsLatest").onclick = () => { s.value = String(state.store.now); s.oninput(); };
}

function syncAtToUrl() {
  const url = new URL(location.href);
  if (state.at === state.store.now) url.searchParams.delete("at");
  else url.searchParams.set("at", String(state.at));
  history.replaceState(null, "", url);
}

const travelling = () => state.at !== state.store.now;

function renderNow() {
  $("dsNow").textContent = `${fmtTime(state.at, true)} UTC`;
  $("dsNowKicker").textContent = travelling() ? "Replaying as if now were" : "Now (last reading in the data)";
  $("dsLatest").hidden = !travelling();
  const span = `${fmtTime(state.store.start, true)} to ${fmtTime(state.store.now, true)} UTC`;
  $("dsAtLabel").textContent = travelling()
    ? `Time travel: plans are made from this moment's readings. The data runs ${span}.`
    : `The data runs ${span}. Drag back to replay any moment as now.`;
  document.body.classList.toggle("travelling", travelling());
}

// ---------- alerts ----------

// The backend reads one dataset at a time, for everyone. Opening a folder here makes it
// the active one (POST, so it may ask for the operator PIN).
async function ensureActive(folder) {
  if (state.backend?.active === folder) return;
  state.backend = await state.eog.datasets();
  if (state.backend.active === folder) return;
  if (!state.backend.datasets?.some((d) => d.id === folder))
    throw Object.assign(new Error(`the backend has no dataset named ${folder}`), { status: 404 });
  state.backend = await state.eog.activate(folder);
}

async function refreshAlerts() {
  const seq = ++state.alertSeq;
  const at = state.at, folder = state.folder;
  if (state.storeFolder !== folder) return; // the folder's own readings are not in yet
  try {
    await ensureActive(folder);
    if (seq !== state.alertSeq) return;
    const [al, ev, pl] = await Promise.all([state.eog.alerts(at), state.eog.events(at), state.eog.plans(at)]);
    if (seq !== state.alertSeq || at !== state.at || folder !== state.folder) return;
    // Another tab (the console, a teammate) can switch the backend's dataset.
    const other = [al, ev, pl].find((r) => r.dataset !== folder);
    if (other) { state.backend = null; throw Object.assign(new Error(`the backend has ${other.dataset} active now, not ${folder}`), { status: 409 }); }
    state.plansBy = new Map((pl.plans || []).map((p) => [p.event?.id ?? p.plan?.event_id, p]));
    state.planSystems = pl.systems || [];
    state.alerts = normAlerts(al).map((a) => {
      const none = state.plansBy.get(a.id)?.alternatives?.find((x) => x.actions?.every((y) => y.type === "none"));
      return none ? { ...a, doNothingCost: none.cost } : a;
    });
    state.resolved = normAlerts(ev).filter((a) => a.status === "resolved");
    state.detection = "ok";
    state.detectionNote = "";
  } catch (e) {
    if (seq !== state.alertSeq) return;
    state.alerts = [];
    state.resolved = [];
    state.plansBy = new Map();
    state.planSystems = [];
    state.detection = !e.status || e.status === 503 ? "absent" : "error";
    state.detectionNote = !e.status ? "The backend is not running."
      : e.status === 503 ? `The backend's dataset mode is off (${e.message})`
      : `The backend's alerts did not load: ${e.message}.`;
  }
  renderAlerts();
  renderMap();
  renderDetail();
  renderFoot();
}

// Every list below reads from pool(), so the map, cards and detail agree on one set.
const pool = () => (state.detection === "ok" ? [...state.alerts, ...state.resolved] : []);
const liveAlerts = () => sortAlerts(state.detection === "ok" ? state.alerts : []);
const alertsFor = (facilityId) => liveAlerts().filter((a) => a.facility === facilityId);

function renderAlerts() {
  const box = $("dsAlerts");
  const live = liveAlerts();
  const resolved = pool().filter((a) => a.status === "resolved");
  $("dsAlertCount").textContent = state.detection === "ok" ? `${live.length} in progress` : "";
  if (state.detection === "loading") {
    box.innerHTML = `<p class="ds-hint">Checking for problems…</p>`;
  } else if (state.detection !== "ok") {
    box.innerHTML = `<div class="ds-unknown"><b>Detection is off</b><p>${esc(state.detectionNote)} Alerts, countdowns and plans come only from
      the backend's dataset mode: run <code>npm run demo</code> with <code>FREEBOARD_EOG_DIR</code> set to the sponsor data.</p>
      <p class="ds-hint">The map and charts still show the raw readings. This is not an all-clear.</p></div>`;
  } else if (!live.length) {
    box.innerHTML = `<div class="ds-clear"><b>No problems in progress</b><p>Every pump and pipeline reads normal at ${esc(fmtTime(state.at, true))} UTC.</p></div>`;
  } else {
    box.innerHTML = "";
    live.forEach((a, i) => box.appendChild(alertCard(a, i + 1)));
  }
  const wrap = $("dsResolvedWrap");
  wrap.hidden = !resolved.length;
  if (resolved.length) {
    $("dsResolvedSum").textContent = `Resolved earlier (${resolved.length})`;
    $("dsResolved").innerHTML = resolved.sort((a, b) => (b.onset ?? 0) - (a.onset ?? 0)).map((a) =>
      `<button type="button" class="ds-res" data-id="${esc(a.id)}"><span>${esc(categoryOf(a.category).label)}</span>
       <b>${esc(a.asset || a.facility)}</b><span>${a.onset !== null ? esc(fmtTime(a.onset, true)) : ""}${a.end !== null ? " to " + esc(fmtTime(a.end, true)) : ""}</span></button>`).join("");
    for (const b of $("dsResolved").querySelectorAll(".ds-res")) b.onclick = () => select({ kind: "alert", id: b.dataset.id });
  }
}

function alertCard(a, rank) {
  const c = categoryOf(a.category);
  const card = document.createElement("article");
  card.className = `ds-alert ${c.tone}${state.selected?.kind === "alert" && state.selected.id === a.id ? " on" : ""}`;
  card.tabIndex = 0;
  const since = a.onset !== null ? `since ${fmtTime(a.onset, true)} (${durationText((state.at - a.onset) / 3600)} ago)` : "";
  let body = "";
  if (a.category === "pump_failure") {
    // The backend's trips_at is null when the repair comes first, even with an hours_to_limit.
    const when = a.tripsAt !== null ? fmtTime(a.tripsAt) : null;
    const range = a.hoursP10 !== null && a.hoursP90 !== null ? ` <span class="ds-range">(${durationText(a.hoursP10)} to ${durationText(a.hoursP90)})</span>` : "";
    body += a.tripped
      ? `<div class="ds-count-big"><span class="kicker">Tank at shutdown</span><b>Wells shut in</b><span>${a.tripsAt !== null ? `since ${esc(fmtTime(a.tripsAt))} UTC, ` : ""}until the repair${a.repairEnd ? ` at ${esc(fmtTime(a.repairEnd))} UTC` : ""}</span></div>`
      : a.tripsAt !== null
      ? `<div class="ds-count-big"><span class="kicker">Shuts in at</span><b>${esc(when)} UTC</b><span>in ${esc(durationText(Math.max(0, a.tripsAt - state.at) / 3600))}${range}</span></div>`
      : a.levelPct === null
      ? `<div class="ds-count-big"><span class="kicker">Countdown</span><b>No level readings</b><span>treat the tank as filling: not checked, not safe</span></div>`
      : `<div class="ds-count-big quiet"><span class="kicker">Countdown</span><b>Will not trip</b><span>tank stays under shutdown until the repair</span></div>`;
    if (a.levelPct !== null) body += `<div class="ds-meta">Tank ${Math.round(a.levelPct)}% now</div>`;
  }
  const lines = actionLines(a, { at: state.at, ops: state.ops });
  body += `<ul class="ds-plan">${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`;
  if (a.category === "pump_failure" && a.estCost !== null) {
    const saves = a.doNothingCost !== null && a.doNothingCost > a.estCost ? ` · saves ${money(a.doNothingCost - a.estCost)}` : "";
    body += `<div class="ds-cost"><b>${money(a.estCost)}</b>${a.doNothingCost !== null ? ` vs ${money(a.doNothingCost)} doing nothing` : ""}${saves}</div>`;
  }
  if (a.category === "pump_failure" && a.approval)
    body += `<div class="ds-check ok">Approved by ${esc(a.approval.by || "operator")} at ${esc(fmtTime(a.approval.at))} UTC</div>`;
  else if (a.category === "pump_failure" && a.expiresAt !== null)
    body += `<div class="ds-check ${a.expiresAt - state.at < 1800 ? "alarm" : "warning"}">Last call: start by ${esc(fmtTime(a.expiresAt))} UTC (${esc(durationText(Math.max(0, a.expiresAt - state.at) / 3600))} left)</div>`;
  if (a.confidence !== null) body += `<div class="ds-meta">Confidence ${Math.round(a.confidence * 100)}%</div>`;
  card.innerHTML = `<header><span class="ds-rank">${rank}</span><span class="ds-tag ${c.tone}">${esc(c.short)}</span>
    <b>${esc(a.facility || a.asset)}</b><span class="ds-since">${esc(since)}</span></header>
    <div class="ds-asset">${esc(c.label)} · ${esc(a.asset || "")}</div>${body}`;
  card.onclick = () => select({ kind: "alert", id: a.id });
  card.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select({ kind: "alert", id: a.id }); } };
  return card;
}

// ---------- map ----------

function renderAll() {
  renderNow();
  renderMap();
  renderAlerts();
  renderDetail();
  renderFoot();
}

function renderMap() {
  if (!state.network) return;
  const host = $("dsMap");
  const lim = state.network.defaults.tank_limits_pct;
  const at = state.at;
  const L = (a, m) => state.store.latest(a, m, at);
  const systems = state.network.systems;
  $("dsMapNote").textContent = `${systems.length} systems · ${systems.reduce((n, s) => n + s.facilities.length, 0)} facilities`;
  host.innerHTML = "";
  for (const s of systems) {
    const fleet = state.ops.systems?.[s.id]?.emergency_fleet_size;
    const card = document.createElement("div");
    card.className = "ds-sys";
    const pts = [...s.facilities.map((f) => [f.lat, f.lon]), [s.disposal_site.lat, s.disposal_site.lon]];
    const yard = Array.isArray(s.truck_yard) ? s.truck_yard : null;
    const lat0 = pts.reduce((n, p) => n + p[0], 0) / pts.length;
    const kx = Math.cos((lat0 * Math.PI) / 180);
    const xs = pts.map((p) => p[1] * kx), ys = pts.map((p) => p[0]);
    const W = 320, H = 210, pad = 46;
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const sc = Math.min((W - 2 * pad) / Math.max(1e-6, maxX - minX), (H - 2 * pad) / Math.max(1e-6, maxY - minY));
    const cx = (W - (maxX - minX) * sc) / 2, cy = (H - (maxY - minY) * sc) / 2;
    const P = (lat, lon) => [cx + (lon * kx - minX) * sc, H - (cy + (lat - minY) * sc)];
    const [dx, dy] = P(s.disposal_site.lat, s.disposal_site.lon);
    let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="System ${esc(s.id)} map">`;
    // Truck yard: an arrow at the edge pointing the way it lies, with its fleet.
    if (yard) {
      const [yx, yy] = P(yard[0], yard[1]);
      const ang = Math.atan2(yy - dy, yx - dx);
      const inside = yx > 8 && yx < W - 8 && yy > 8 && yy < H - 8;
      const ex = inside ? yx : dx + Math.cos(ang) * Math.min(Math.abs((W / 2 - 14) / (Math.cos(ang) || 1e-6)), Math.abs((H / 2 - 14) / (Math.sin(ang) || 1e-6)));
      const ey = inside ? yy : dy + Math.sin(ang) * Math.min(Math.abs((W / 2 - 14) / (Math.cos(ang) || 1e-6)), Math.abs((H / 2 - 14) / (Math.sin(ang) || 1e-6)));
      const bx = Math.max(10, Math.min(W - 10, ex)), by = Math.max(10, Math.min(H - 10, ey));
      svg += `<g class="yard"><rect x="${bx - 5}" y="${by - 5}" width="10" height="10"/><text x="${bx + (bx > W / 2 ? -9 : 9)}" y="${by + 3}" text-anchor="${bx > W / 2 ? "end" : "start"}">yard · ${esc(fleet ?? "?")} trucks</text></g>`;
    }
    for (const f of s.facilities) {
      const [fx, fy] = P(f.lat, f.lon);
      const al = alertsFor(f.id);
      const leak = al.some((a) => a.category === "pipeline_leak");
      const flow = L(f.pump.id, "flow_bpd");
      const recv = L(f.pipeline.id, "receive_flow_bpd");
      const cls = leak ? "pipe leak" : (recv ?? 0) > 0 ? "pipe flowing" : "pipe";
      svg += `<line class="${cls}" x1="${fx}" y1="${fy}" x2="${dx}" y2="${dy}"/>`;
      void flow;
    }
    svg += `<g class="disposal"><path d="M${dx} ${dy - 7}L${dx + 7} ${dy}L${dx} ${dy + 7}L${dx - 7} ${dy}Z"/><title>${esc(s.disposal_site.id)} · ${esc(s.disposal_site.pressure_psi)} psi</title></g>`;
    for (const f of s.facilities) {
      const [fx, fy] = P(f.lat, f.lon);
      const al = alertsFor(f.id);
      const level = L(f.tank.id, "level_pct");
      const pump = L(f.pump.id, "status");
      const worst = al.find((a) => a.category === "pump_failure") || al[0];
      const tone = worst ? categoryOf(worst.category).tone : pump === 2 || (level !== null && level >= lim.shutdown) ? "alarm" : level !== null && level >= lim.warning ? "warning" : "";
      const flag = worst ? categoryOf(worst.category).short : pump === 2 ? "FAULT" : "";
      const sel = state.selected && (state.selected.id === f.id || al.some((a) => a.id === state.selected.id)) ? " on" : "";
      const n = f.wells.length;
      let g = `<g class="fac ${tone}${sel}" data-fac="${esc(f.id)}" tabindex="0" role="button" aria-label="${esc(f.id)}">`;
      g += `<circle class="hit" cx="${fx}" cy="${fy}" r="26"/>`;
      f.wells.forEach((w, i) => {
        const ang = Math.PI + (i - (n - 1) / 2) * 0.55 + (fy < H / 2 ? -Math.PI / 2 : Math.PI / 2);
        const wx = fx + Math.cos(ang) * 20, wy = fy + Math.sin(ang) * 20;
        const on = L(w.id, "status");
        g += `<line class="flowline" x1="${fx}" y1="${fy}" x2="${wx}" y2="${wy}"/><circle class="well ${on === 0 ? "off" : on === null ? "unknown" : ""}" cx="${wx}" cy="${wy}" r="3"><title>${esc(w.id)}${on === 0 ? " shut in" : ""}</title></circle>`;
      });
      // Tank: fill to the level, ticks at warning and shutdown.
      const th = 26, tw = 11, tx = fx - tw / 2, ty = fy - th / 2;
      const fill = level === null ? 0 : Math.max(0, Math.min(100, level)) / 100 * th;
      g += `<rect class="tank" x="${tx}" y="${ty}" width="${tw}" height="${th}"/>`;
      if (level !== null) g += `<rect class="tank-fill" x="${tx}" y="${ty + th - fill}" width="${tw}" height="${fill}"/>`;
      for (const v of [lim.warning, lim.shutdown]) g += `<line class="tank-tick" x1="${tx - 2}" x2="${tx + tw + 2}" y1="${ty + th - (v / 100) * th}" y2="${ty + th - (v / 100) * th}"/>`;
      g += `<circle class="pump s${pump ?? "x"}" cx="${fx + 11}" cy="${fy + 9}" r="4"><title>${esc(f.pump.id)} ${pump === 1 ? "running" : pump === 0 ? "stopped" : pump === 2 ? "FAULT" : "no reading"}</title></circle>`;
      const short = f.id.replace(/^.*FACILITY_/, "");
      const lx = fx + (fx > W - 80 ? -16 : 16), anchor = fx > W - 80 ? "end" : "start";
      g += `<text class="fac-name" x="${lx}" y="${fy - 4}" text-anchor="${anchor}">${esc(short)}${flag ? ` · ${esc(flag)}` : ""}</text>`;
      g += `<text class="fac-level" x="${lx}" y="${fy + 9}" text-anchor="${anchor}">${level === null ? "no level" : level.toFixed(0) + "%"}</text>`;
      g += `</g>`;
      svg += g;
    }
    svg += `</svg>`;
    const live = s.facilities.flatMap((f) => alertsFor(f.id));
    card.innerHTML = `<div class="ds-sys-head"><b>${esc(s.id)}</b><span>${s.facilities.length} facilities · ${esc(fleet ?? "?")} trucks · disposal ${esc(s.disposal_site.pressure_psi)} psi</span>${live.length ? `<span class="ds-tag ${live.some((a) => a.category === "pump_failure") ? "alarm" : "warning"}">${live.length} alert${live.length === 1 ? "" : "s"}</span>` : ""}<button type="button" class="ds-compare" data-sys="${esc(s.id)}" title="Compare ${esc(s.id)}'s facilities side by side">Compare</button></div>${svg}`;
    card.querySelector(".ds-compare").onclick = () => select({ kind: "system", id: s.id });
    for (const g of card.querySelectorAll(".fac")) {
      const go = () => select({ kind: "facility", id: g.dataset.fac });
      g.addEventListener("click", go);
      g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    }
    host.appendChild(card);
  }
  render3d();
}

// ---------- 3D ----------

let f3d = null;
let f3dKey = null;
let f3dLoading = null;

function setView(v) {
  state.view = v;
  for (const b of $("dsView").querySelectorAll("button")) b.classList.toggle("on", b.dataset.v === v);
  $("dsMap").hidden = v !== "map";
  $("ds3d").hidden = v !== "3d";
  try { localStorage.setItem("freeboard.dataset.view", v); } catch {}
  if (v === "3d") open3d();
}
for (const b of $("dsView").querySelectorAll("button")) b.onclick = () => setView(b.dataset.v);

async function open3d() {
  if (!f3d && !f3dLoading) {
    f3dLoading = import("./field3d.js").then(({ mountField3d }) => {
      // The generated field (#131) comes from the backend's dataset mode; in file mode
      // the view is built from network.json only.
      // #152's generated field per system (ingest.eog_field). field3d.js keeps the view it
      // builds from network.json when the route is missing or the build fails.
      const glbUrl = (sys) => state.detection === "ok"
        ? { glb: `${API}/api/dataset/field.glb?system=${encodeURIComponent(sys)}`, ids: `${API}/api/dataset/field_ids?system=${encodeURIComponent(sys)}` }
        : null;
      f3d = mountField3d($("ds3dHost"), { onPick: (fac) => fac && select({ kind: "facility", id: fac }), glbUrl });
      f3d.setTip(tipFor);
      window.__dataset.f3d = f3d;
    }).catch((e) => { $("ds3dHost").textContent = `3D did not load: ${e.message}`; });
  }
  await f3dLoading;
  render3d();
}

function tipFor(id) {
  const a = state.assets?.get(id);
  if (!a) return id;
  const L = (m) => state.store.latest(id, m, state.at);
  if (a.kind === "tank") { const v = L("level_pct"); return `${id} · ${v === null ? "no reading" : v.toFixed(1) + "%"}`; }
  if (a.kind === "pump") { const s = L("status"), f = L("flow_bpd"); return `${id} · ${s === 1 ? "running" : s === 2 ? "FAULT" : s === 0 ? "stopped" : "no reading"}${f !== null ? ` · ${Math.round(f)} bbl/d` : ""}`; }
  if (a.kind === "well") { const s = L("status"), w = L("water_bpd"); return `${id} · ${s === 0 ? "shut in" : s === 1 ? "producing" : "no reading"}${w !== null ? ` · ${Math.round(w)} bbl/d water` : ""}`; }
  if (a.kind === "pipeline") { const r = L("receive_flow_bpd"), p = L("pressure_psi"); return `${id} · ${r === null ? "no reading" : Math.round(r) + " bbl/d arriving"}${p !== null ? ` · ${Math.round(p)} psi` : ""}`; }
  return id;
}

function render3d() {
  if (!f3d || state.view !== "3d" || !state.network) return;
  const systems = state.network.systems;
  const selFac = state.selected && (facilityOf(state.selected.id) || facilityOf(pool().find((a) => a.id === state.selected.id)?.facility));
  if (selFac) state.sys3d = selFac.s.id;
  if (!systems.some((s) => s.id === state.sys3d)) state.sys3d = systems[0]?.id;
  const sys = systems.find((s) => s.id === state.sys3d);
  if (!sys) return;
  $("ds3dTabs").innerHTML = systems.map((s) => {
    const n = s.facilities.reduce((k, f) => k + alertsFor(f.id).length, 0);
    return `<button type="button" data-s="${esc(s.id)}" class="${s.id === sys.id ? "on" : ""}">${esc(s.id)}${n ? ` · ${n}` : ""}</button>`;
  }).join("");
  for (const b of $("ds3dTabs").querySelectorAll("button")) b.onclick = () => { state.sys3d = b.dataset.s; render3d(); };
  const lim = state.network.defaults.tank_limits_pct;
  const key = `${state.folder}|${sys.id}`;
  if (f3dKey !== key) { f3d.build(sys, lim); f3dKey = key; }
  const L = (a, m) => state.store.latest(a, m, state.at);
  const view = {};
  for (const f of sys.facilities) {
    const al = alertsFor(f.id);
    const pf = al.some((a) => a.category === "pump_failure");
    const pumpS = L(f.pump.id, "status");
    const level = L(f.tank.id, "level_pct");
    const tankTone = pf || pumpS === 2 || (level !== null && level >= lim.shutdown) ? "alarm" : level !== null && level >= lim.warning ? "warning" : "";
    view[f.tank.id] = { level, tone: tankTone };
    view[f.pump.id] = { pump: pumpS, tone: pf || pumpS === 2 ? "alarm" : al.some((a) => a.category === "pump_degradation") ? "warning" : "" };
    view[f.pipeline.id] = { recv: L(f.pipeline.id, "receive_flow_bpd") ?? 0, tone: al.some((a) => a.category === "pipeline_leak") ? "warning" : "" };
    for (const w of f.wells) view[w.id] = { well: L(w.id, "status") };
  }
  f3d.update(view);
}

// ---------- detail: trends and what-if ----------

function select(sel) {
  state.selected = sel;
  renderAlerts();
  renderMap();
  renderDetail();
  if (matchMedia("(max-width: 900px)").matches) $("dsDetail").scrollIntoView({ behavior: "smooth", block: "start" });
}

function facilityOf(id) {
  for (const s of state.network?.systems || []) for (const f of s.facilities) if (f.id === id) return { s, f };
  return null;
}

function renderDetail() {
  const box = $("dsDetail");
  if (!state.store || !state.selected) {
    box.innerHTML = `<p class="ds-hint">Pick a facility on the map, or an alert, to see its readings and plan.</p>`;
    return;
  }
  const sel = state.selected;
  if (sel.kind === "system") { compare(box, sel.id); return; }
  let alert = null, facId = sel.id;
  if (sel.kind === "alert") {
    alert = pool().find((a) => a.id === sel.id);
    if (!alert) { box.innerHTML = `<p class="ds-hint">That alert is not in progress at this time.</p>`; return; }
    facId = alert.facility;
  } else {
    // A facility with a pump failure in progress opens on its plan.
    alert = alertsFor(sel.id).find((a) => a.category === "pump_failure") || null;
  }
  const hit = facilityOf(facId);
  if (!hit) { box.innerHTML = `<p class="ds-hint">${esc(facId)} is not in this dataset's network.</p>`; return; }
  const { s, f } = hit;
  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "ds-detail-head";
  const c = alert ? categoryOf(alert.category) : null;
  head.innerHTML = `<div><span class="kicker">${esc(s.id)}${c ? ` · ${esc(c.label)}` : ""}</span><h2>${esc(f.id)}</h2></div>
    <div class="ds-win" role="group" aria-label="Trend window">${[[6, "6 h"], [24, "24 h"], [72, "3 d"], [168, "week"]].map(([h, l]) =>
      `<button type="button" data-h="${h}" class="${state.win === h * 3600 ? "on" : ""}">${l}</button>`).join("")}</div>`;
  box.appendChild(head);
  for (const b of head.querySelectorAll("[data-h]")) b.onclick = () => { state.win = Number(b.dataset.h) * 3600; renderDetail(); };

  if (alert && alert.evidence.length) {
    const ev = document.createElement("div");
    ev.className = "ds-evidence";
    ev.innerHTML = `<span class="kicker">Why the backend flagged it</span><ul>${alert.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`;
    box.appendChild(ev);
  }
  if (alert && alert.category === "pump_failure" && alert.status !== "resolved") box.appendChild(planPanel(f, s, alert));
  box.appendChild(traces(f, alert));
}

// Side by side: every facility in one system, one small chart each, on one shared time
// axis and crosshair. One metric at a time so every chart has one unit.
const COMPARE = [
  { key: "level", label: "Tank level", unit: "% full", get: (f) => [[f.tank.id, "level_pct"]], yMin: 0, yMax: 100, limits: true },
  { key: "out", label: "Water out vs arriving", unit: "bbl/day", get: (f) => [[f.pump.id, "flow_bpd", "pump flow"], [f.pipeline.id, "receive_flow_bpd", "arriving at disposal"]], yMin: 0 },
  { key: "lost", label: "Lost in pipeline", unit: "bbl/day, pump running", derived: "lost" },
  { key: "in", label: "Water in", unit: "bbl/day", get: (f) => [[f.id, "inflow_bpd"]], yMin: 0 },
  { key: "psi", label: "Pressure", unit: "psi", get: (f) => [[f.pump.id, "discharge_psi", "pump discharge"], [f.pipeline.id, "pressure_psi", "pipeline at disposal"]] },
];
function lostSeries(f, from, to) {
  const R = (a, k) => state.store.range(a, k, from, to);
  const recv = new Map(R(f.pipeline.id, "receive_flow_bpd"));
  const run = new Map(R(f.pump.id, "status"));
  return R(f.pump.id, "flow_bpd").filter(([t, v]) => run.get(t) === 1 && v > 0 && recv.has(t)).map(([t, v]) => [t, v - recv.get(t)]);
}

function compare(box, systemId) {
  const sys = state.network.systems.find((x) => x.id === systemId);
  if (!sys) { box.innerHTML = `<p class="ds-hint">${esc(systemId)} is not in this dataset.</p>`; return; }
  state.compareMetric ||= "level";
  const m = COMPARE.find((c) => c.key === state.compareMetric) || COMPARE[0];
  box.innerHTML = `<div class="ds-detail-head"><div><span class="kicker">Compare facilities</span><h2>${esc(sys.id)}</h2></div>
    <div class="ds-win" role="group" aria-label="Metric">${COMPARE.map((c) => `<button type="button" data-m="${c.key}" class="${c.key === m.key ? "on" : ""}">${esc(c.label)}</button>`).join("")}</div>
    <div class="ds-win" role="group" aria-label="Trend window">${[[6, "6 h"], [24, "24 h"], [72, "3 d"], [168, "week"]].map(([h, l]) =>
      `<button type="button" data-h="${h}" class="${state.win === h * 3600 ? "on" : ""}">${l}</button>`).join("")}</div></div>`;
  const sp = state.detection === "ok" ? state.planSystems?.find((x) => x.system === sys.id) : null;
  if (sp) box.insertAdjacentHTML("beforeend", `<p class="ds-outage-sum">${esc(sys.id)} at ${esc(fmtTime(state.at))} UTC: ${esc(sp.events.length)} pump failure${sp.events.length === 1 ? "" : "s"} planned together on ${esc(sp.trucks_used)} of ${esc(sp.fleet_size)} trucks, <b>${money(sp.cost)}</b> in all.</p>`);
  for (const b of box.querySelectorAll("[data-m]")) b.onclick = () => { state.compareMetric = b.dataset.m; renderDetail(); };
  for (const b of box.querySelectorAll("[data-h]")) b.onclick = () => { state.win = Number(b.dataset.h) * 3600; renderDetail(); };
  const wrap = document.createElement("div");
  wrap.className = "ds-traces";
  box.appendChild(wrap);
  const to = state.at, from = Math.max(state.store.start, to - state.win);
  const lim = state.network.defaults.tank_limits_pct;
  const charts = [];
  const hover = (t) => charts.forEach((c) => c.show(t));
  requestAnimationFrame(() => {
    for (const f of sys.facilities) {
      const host = document.createElement("div");
      wrap.appendChild(host);
      const al = alertsFor(f.id);
      const marks = al.filter((a) => a.onset).map((a) => ({ t: a.onset, label: categoryOf(a.category).short, tone: categoryOf(a.category).tone }));
      charts.push(lineChart(host, {
        title: f.id, unit: m.unit, from, to, height: 90, yMin: m.yMin, yMax: m.yMax, marks, onHover: hover,
        series: m.derived === "lost" ? [{ name: "pump flow − arriving", points: lostSeries(f, from, to) }]
          : m.get(f).map(([a, k, name]) => ({ name: name || a, points: state.store.range(a, k, from, to) })),
        refs: m.derived === "lost" ? [{ y: 0, label: "none lost" }] : undefined,
        ...(m.limits ? { refs: [{ y: lim.warning, label: `warning ${lim.warning}%`, tone: "warning" }, { y: lim.shutdown, label: `shutdown ${lim.shutdown}%`, tone: "alarm" }] } : {}),
        format: m.limits ? (v) => v.toFixed(1) + "%" : undefined,
      }));
    }
  });
}

function traces(f, alert) {
  const wrap = document.createElement("div");
  wrap.className = "ds-traces";
  const to = state.at;
  let from = Math.max(state.store.start, to - state.win);
  if (alert?.onset && alert.onset - 3600 < from) from = Math.max(state.store.start, alert.onset - 3600);
  const lim = state.network.defaults.tank_limits_pct;
  const R = (a, m) => state.store.range(a, m, from, to);
  const marks = [];
  if (alert?.onset) marks.push({ t: alert.onset, label: "onset", tone: categoryOf(alert.category).tone });
  if (alert?.end && alert.end <= to) marks.push({ t: alert.end, label: "ended" });
  const charts = [];
  const hover = (t) => charts.forEach((c) => c.show(t));
  const add = (opts) => {
    const host = document.createElement("div");
    wrap.appendChild(host);
    charts.push(lineChart(host, { from, to, marks, onHover: hover, ...opts }));
  };
  // Defer until the wrapper is in the page so the charts size to it.
  requestAnimationFrame(() => {
    add({ title: "Tank level", unit: "% full", series: [{ name: f.tank.id, points: R(f.tank.id, "level_pct") }], yMin: 0, yMax: 100,
      refs: [{ y: lim.warning, label: `warning ${lim.warning}%`, tone: "warning" }, { y: lim.shutdown, label: `shutdown ${lim.shutdown}%`, tone: "alarm" }],
      format: (v) => v.toFixed(1) + "%" });
    add({ title: "Water out", unit: "bbl/day", yMin: 0, series: [
      { name: "pump flow", points: R(f.pump.id, "flow_bpd") },
      { name: "arriving at disposal", points: R(f.pipeline.id, "receive_flow_bpd") }] });
    // Pump flow minus what arrives at disposal, while the pump runs. Normal is about 0 with
    // noise; a leak holds it above 0. It reads the same two sensors as the chart above.
    const lost = lostSeries(f, from, to);
    add({ title: "Lost between pump and disposal", unit: "bbl/day, pump running", series: [{ name: "pump flow − arriving", points: lost }],
      refs: [{ y: 0, label: "none lost" }], gapS: 1800, empty: "The pump did not run in this window" });
    add({ title: "Water in", unit: "bbl/day", yMin: 0, series: [{ name: "tank inflow", points: R(f.id, "inflow_bpd") }] });
    add({ title: "Pressure", unit: "psi", series: [
      { name: "pump discharge", points: R(f.pump.id, "discharge_psi") },
      { name: "pipeline at disposal", points: R(f.pipeline.id, "pressure_psi") }] });
    add({ title: "Pump state", unit: "0 stopped · 1 running · 2 fault", height: 70, step: true, yMin: 0, yMax: 2,
      series: [{ name: f.pump.id, points: R(f.pump.id, "status") }],
      format: (v) => (v === 1 ? "running" : v === 2 ? "FAULT" : "stopped") });
    pickedCharts(f, wrap, add, R);
  });
  return wrap;
}

// README: "select anything on the network and plot any of its sensor readings". Any
// sensor of this facility (its wells, tank, pump, pipeline) or its disposal site. Picks of
// the same metric share one chart, up to four, so every chart keeps one unit and one axis.
// "receive_flow_bpd" reads "receive flow": the unit sits beside the title already.
const metricName = (m) => m.replace(/_(bpd|psi|pct|mcfd)$/, "").replace(/_/g, " ");
const unitOf = (m) => (/_pct$/.test(m) ? "% full" : /_bpd$/.test(m) ? "bbl/day" : /_mcfd$/.test(m) ? "mcf/day" : /_psi$/.test(m) ? "psi" : m === "status" ? "0 off · 1 on · 2 fault" : m);
function pickedCharts(f, wrap, add, R) {
  const sys = facilityOf(f.id)?.s;
  const assets = [f.id, f.tank.id, f.pump.id, f.pipeline.id, ...f.wells.map((w) => w.id), sys?.disposal_site?.id].filter(Boolean);
  const picked = (state.picked ||= new Map()).get(f.id) || [];
  const full = (m) => picked.filter((k) => k.split("|")[1] === m).length >= 4;
  const bar = document.createElement("div");
  bar.className = "ds-pick";
  const opts = assets.map((a) => {
    const ms = state.store.metrics(a).filter((m) => !picked.includes(`${a}|${m}`));
    // A metric whose chart already holds four can't take a fifth: say so in the list.
    return ms.length ? `<optgroup label="${esc(a)}">${ms.map((m) => `<option value="${esc(a)}|${esc(m)}" ${full(m) ? "disabled" : ""}>${esc(a)} · ${esc(metricName(m))}${full(m) ? " (chart full)" : ""}</option>`).join("")}</optgroup>` : "";
  }).join("");
  bar.innerHTML = `<span class="kicker">Plot any sensor</span><select aria-label="Add a sensor to plot"><option value="">add a sensor…</option>${opts}</select>
    ${picked.map((k) => `<span class="ds-chip">${esc(k.split("|")[0] + " · " + metricName(k.split("|")[1]))}<button type="button" data-k="${esc(k)}" aria-label="Remove ${esc(k)}">×</button></span>`).join("")}`;
  wrap.appendChild(bar);
  const set = (list) => { state.picked.set(f.id, list); renderDetail(); };
  bar.querySelector("select").onchange = (e) => {
    const k = e.target.value;
    if (!k) return;
    if (full(k.split("|")[1])) { e.target.value = ""; return; }
    set([...picked, k]);
  };
  for (const b of bar.querySelectorAll("[data-k]")) b.onclick = () => set(picked.filter((k) => k !== b.dataset.k));
  const byMetric = new Map();
  for (const k of picked) { const [a, m] = k.split("|"); if (!byMetric.has(m)) byMetric.set(m, []); byMetric.get(m).push(a); }
  for (const [m, list] of byMetric) {
    add({ title: metricName(m), unit: unitOf(m), step: m === "status", yMin: m === "status" || /_bpd$|_pct$|_mcfd$/.test(m) ? 0 : undefined, yMax: m === "status" ? 2 : undefined,
      series: list.map((a) => ({ name: a, points: R(a, m) })) });
  }
}

// The plan for one pump failure in progress, all from the backend: its recommended plan
// (shared trucks), the other options, Last Call (what waiting costs), approval with steps
// and a radio script, and a what-if the backend replays (POST /api/eog/whatif).
const planText = (p) => esc(p.summary || "");
// The approve answer's steps and radio script (kept in state.approved, so a refresh keeps them).
const stepsHtml = (r) => !r ? "" : `<ol class="ds-plan">${(r.steps || []).map((x) => `<li>${esc(x.at ? fmtTime(x.at) + " UTC · " : "")}${esc(x.text)}</li>`).join("")}</ol>
  ${r.radio_script ? `<span class="kicker">Radio script</span><p class="ds-radio">${esc(r.radio_script)}</p>` : ""}`;
function planPanel(f, s, alert) {
  const box = document.createElement("div");
  box.className = "ds-whatif";
  const item = state.plansBy.get(alert.id);
  if (!item) { box.innerHTML = `<p class="ds-hint">The backend has no plan for this failure at this moment.</p>`; return box; }
  const p = item.plan || {};
  const at = state.at;
  const left = alert.repairEnd !== null ? ` · repair due ${esc(fmtTime(alert.repairEnd))} UTC, ${esc(durationText(Math.max(0, alert.repairEnd - at) / 3600))} left` : "";
  const alts = (item.alternatives || []).map((x) => `<tr><th>${planText(x)}</th><td>${money(x.cost)}</td>
    <td class="ds-hint">${x.trips ? `trips ${esc(x.trip_at ? fmtTime(x.trip_at) + " UTC" : "")}` : "stays under shutdown"}</td></tr>`).join("");
  const appr = item.approval || alert.approval;
  box.innerHTML = `<div class="ds-sec-head"><span class="kicker">Plan for this failure</span><span class="ds-count">from ${esc(fmtTime(at))} UTC${left}</span></div>
    <div class="ds-wi-verdict ${p.trips ? "alarm" : "ok"}">${p.trips
      ? `<b>Reaches shutdown at ${esc(fmtTime(p.trip_at))} UTC even so</b><span>${planText(p)}</span>`
      : `<b>${planText(p)}</b><span>peaks at ${Number.isFinite(p.peak_level_pct) ? p.peak_level_pct.toFixed(1) : "?"}%, never reaches shutdown</span>`}</div>
    <ul class="ds-plan">${actionLines(alert, { at, ops: state.ops }).map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
    ${(p.notes || []).length ? `<p class="ds-hint">${esc(p.notes.join(" "))}</p>` : ""}
    <div class="ds-lastcall"></div>
    <table class="ds-costs"><tbody>${alts}</tbody></table>
    <div class="ds-approve">${appr
      ? `<p class="ds-check ok">Approved by ${esc(appr.by || "operator")} at ${esc(fmtTime(appr.at))} UTC.</p>`
      : `<button type="button" data-act="approve">Approve this plan</button>`}<div class="ds-steps">${stepsHtml(state.approved.get(`${alert.id}@${at}`))}</div></div>
    <details class="ds-wi-edit"><summary>Try another plan (the backend replays it)</summary><div class="ds-wi-body"></div></details>`;

  // Last Call: the cost of this failure if nobody acts until each 5-minute start.
  const curve = (item.wait_curve || []).filter((r) => Number.isFinite(r.cost));
  requestAnimationFrame(() => {
    const host = box.querySelector(".ds-lastcall");
    if (!curve.length || !host) return;
    const exp = p.expires_at;
    const head = document.createElement("p");
    head.className = "ds-outage-sum";
    const jump = exp ? curve.find((r) => r.start_ts > exp) : null;
    head.innerHTML = exp
      ? `Last call <b>${esc(fmtTime(exp))} UTC</b> (${esc(durationText(Math.max(0, exp - at) / 3600))} from now). Start later and it costs ${jump ? `<b>${money(jump.cost)}</b>, not ${money(p.cost)}` : "more"}.`
      : `No start time keeps this plan: waiting costs what the chart shows.`;
    host.appendChild(head);
    const chart = document.createElement("div");
    host.appendChild(chart);
    lineChart(chart, {
      title: "Price of waiting", unit: "$, if the plan starts at this time", from: curve[0].start_ts, to: curve[curve.length - 1].start_ts, height: 120, yMin: 0,
      series: [{ name: alert.facility, points: curve.map((r) => [r.start_ts, r.cost]) }],
      marks: exp ? [{ t: exp, label: "last call", tone: "alarm" }] : [], step: true, format: (v) => money(v),
    });
  });

  const ab = box.querySelector("[data-act=approve]");
  if (ab) ab.onclick = async () => {
    ab.disabled = true;
    ab.textContent = "Approving…";
    try {
      const r = await state.eog.approve(alert.id, at);
      state.approved.set(`${alert.id}@${at}`, r); // the steps and script stay on screen through the refresh
      box.querySelector(".ds-steps").innerHTML = stepsHtml(r);
      ab.replaceWith(Object.assign(document.createElement("p"), { className: "ds-check ok", textContent: `Approved by ${r.approval?.by || "operator"}. Nothing moves by itself: call the field with the script below.` }));
      refreshAlerts();
    } catch (e) {
      ab.disabled = false;
      ab.textContent = "Approve this plan";
      box.querySelector(".ds-steps").innerHTML = `<p class="ds-problems">Not approved: ${esc(e.message)}</p>`;
    }
  };

  const det = box.querySelector(".ds-wi-edit");
  det.open = state.plans.has(`${alert.id}@${at}`);
  det.ontoggle = () => { if (det.open) whatIfEditor(det.querySelector(".ds-wi-body"), f, s, alert, item, at); };
  if (det.open) whatIfEditor(det.querySelector(".ds-wi-body"), f, s, alert, item, at);
  return box;
}

const toEdit = (actions) => {
  const t = (actions || []).find((a) => a.type === "dispatch_truck");
  return { trucks: t?.trucks | 0, trips: t?.trips_per_truck | 0,
    shutIns: (actions || []).filter((a) => a.type === "shut_in_wells").flatMap((a) => (a.wells || []).map((w) => ({ well: w, startMin: Number(a.start_after_min) || 0 }))) };
};
const toActions = (e) => {
  const out = [];
  if (e.trucks > 0 && e.trips > 0) out.push({ type: "dispatch_truck", trucks: e.trucks, trips_per_truck: e.trips });
  const byStart = new Map();
  for (const x of e.shutIns) byStart.set(x.startMin, [...(byStart.get(x.startMin) || []), x.well]);
  for (const [m, wells] of byStart) out.push({ type: "shut_in_wells", wells, start_after_min: m });
  return out.length ? out : [{ type: "none" }];
};

let whatIfSeq = 0;
// `at` is the moment the panel was built for: the plan, free trucks and repair time are its.
function whatIfEditor(host, f, s, alert, item, at) {
  const key = `${alert.id}@${at}`;
  if (!state.plans.has(key)) state.plans.set(key, toEdit(item.plan?.actions));
  const e = state.plans.get(key);
  const free = Math.max(0, item.trucks_free ?? 0);
  const fo = state.ops?.systems?.[s.id]?.facilities?.[f.id] || {};
  const horizonMin = alert.repairEnd !== null ? Math.round((alert.repairEnd - at) / 60) : 0;
  const maxTrips = fo.cycle_min ? Math.max(0, Math.floor((horizonMin - fo.response_min) / fo.cycle_min) + 1) : 6;
  e.trucks = Math.min(e.trucks, free);
  host.innerHTML = `<div class="ds-wi-grid"><div class="ds-wi-controls">
      <label>Trucks <select data-k="trucks">${Array.from({ length: free + 1 }, (_, i) => `<option ${i === e.trucks ? "selected" : ""}>${i}</option>`).join("")}</select>
        <span class="ds-hint">${free} free in ${esc(s.id)} after its other failures' plans</span></label>
      <label>Trips each <select data-k="trips">${Array.from({ length: Math.max(maxTrips, e.trips) + 1 }, (_, i) => `<option ${i === e.trips ? "selected" : ""}>${i}</option>`).join("")}</select>
        <span class="ds-hint">${esc(fo.response_min ?? "?")} min out, ${esc(fo.cycle_min ?? "?")} min round trip</span></label>
      <div class="ds-wells"><span class="kicker">Shut in wells early</span>${f.wells.map((w) => {
        const si = e.shutIns.find((x) => x.well === w.id);
        const water = state.store.latest(w.id, "water_bpd", at);
        return `<label class="ds-well"><input type="checkbox" data-well="${esc(w.id)}" ${si ? "checked" : ""}>
          <b>${esc(w.id)}</b><span>${water === null ? "no reading" : Math.round(water) + " bbl/d water"}</span>
          <input type="number" min="0" max="${horizonMin}" step="5" value="${si ? Math.round(si.startMin) : 0}" data-start="${esc(w.id)}" ${si ? "" : "disabled"} aria-label="${esc(w.id)}: minutes from now"><em>min from now</em></label>`;
      }).join("")}</div>
      <div class="ds-wi-buttons"><button type="button" data-act="rec">Recommended plan</button><button type="button" data-act="none">Do nothing</button></div>
    </div><div class="ds-wi-result"><p class="ds-hint">Replaying…</p></div></div>`;
  const redo = () => whatIfEditor(host, f, s, alert, item, at);
  for (const sel of host.querySelectorAll("select[data-k]")) sel.onchange = () => { e[sel.dataset.k] = Number(sel.value); if (e.trucks && !e.trips) e.trips = 1; redo(); };
  for (const cb of host.querySelectorAll("input[data-well]")) cb.onchange = () => {
    e.shutIns = e.shutIns.filter((x) => x.well !== cb.dataset.well);
    if (cb.checked) e.shutIns.push({ well: cb.dataset.well, startMin: 0 });
    redo();
  };
  for (const inp of host.querySelectorAll("input[data-start]")) inp.onchange = () => {
    const si = e.shutIns.find((x) => x.well === inp.dataset.start);
    if (si) si.startMin = Math.max(0, Math.min(horizonMin, Math.round(Number(inp.value) || 0)));
    redo();
  };
  host.querySelector("[data-act=rec]").onclick = () => { state.plans.set(key, toEdit(item.plan?.actions)); redo(); };
  host.querySelector("[data-act=none]").onclick = () => { state.plans.set(key, { trucks: 0, trips: 0, shutIns: [] }); redo(); };

  const seq = ++whatIfSeq;
  const out = host.querySelector(".ds-wi-result");
  state.eog.whatif({ facility_id: f.id, actions: toActions(e), at }).then((w) => {
    if (seq !== whatIfSeq || !out.isConnected || at !== state.at) return;
    const r = w.result, b = w.baseline;
    const lim = state.network.defaults.tank_limits_pct;
    out.innerHTML = `<div class="ds-wi-verdict ${r.trip_at ? "alarm" : "ok"}">${r.trip_at
        ? `<b>Reaches shutdown at ${esc(fmtTime(r.trip_at))} UTC</b><span>wells shut in until the repair</span>`
        : `<b>Stays under shutdown</b><span>peaks at ${r.peak_level_pct.toFixed(1)}%</span>`}</div>
      <table class="ds-costs"><tbody>
        <tr><th>Trucks</th><td>${money(r.truck_cost)}</td><td class="ds-hint">${Math.round(r.removed_bbl)} bbl hauled</td></tr>
        <tr><th>Wells shut in early</th><td>${money(r.shut_in_cost)}</td><td></td></tr>
        <tr><th>Automatic shut-in</th><td>${money(r.auto_shut_in_cost)}</td><td></td></tr>
        <tr class="total"><th>This plan</th><td>${money(r.cost)}</td><td></td></tr>
        <tr><th>Doing nothing</th><td>${money(b.cost)}</td><td class="ds-hint">${b.trip_at ? `trips ${esc(fmtTime(b.trip_at))}` : "never trips"}</td></tr>
        <tr><th>Recommended</th><td>${money(item.plan?.cost)}</td><td></td></tr>
      </tbody></table>
      ${(r.errors || []).length ? `<ul class="ds-problems">${r.errors.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      <div class="ds-wi-chart"></div>`;
    const thin = (pts) => pts.filter((_, i) => i % 3 === 0 || i === pts.length - 1);
    lineChart(out.querySelector(".ds-wi-chart"), {
      title: "Tank level from now to the repair", unit: "% full", from: w.start, to: w.repair_end, height: 150, gapS: 900,
      yMin: Math.max(0, Math.floor((Math.min(w.level_pct, ...r.points.map((x) => x[1])) - 10) / 10) * 10), yMax: 100,
      series: [{ name: "this plan", points: thin(r.points) }, { name: "doing nothing", points: thin(b.points) }],
      refs: [{ y: lim.warning, label: `warning ${lim.warning}%`, tone: "warning" }, { y: lim.shutdown, label: `shutdown ${lim.shutdown}%`, tone: "alarm" }],
      marks: (r.arrivals || []).map((t, i) => ({ t, label: i ? "" : "first load" })), format: (v) => v.toFixed(1) + "%",
    });
  }).catch((err) => {
    if (seq !== whatIfSeq || !out.isConnected) return;
    out.innerHTML = `<p class="ds-problems">The backend did not replay it: ${esc(err.message)}</p>`;
  });
}

// ---------- handoff, export, footer ----------

function openDialog(title, text) {
  $("dsDialogTitle").textContent = title;
  $("dsDialogBody").textContent = text;
  $("dsCopy").onclick = async () => {
    try { await navigator.clipboard.writeText(text); $("dsCopy").textContent = "Copied"; }
    catch { $("dsCopy").textContent = "Select and copy"; }
    setTimeout(() => ($("dsCopy").textContent = "Copy"), 1500);
  };
  $("dsDialog").showModal();
}

$("dsHandoff").onclick = async () => {
  if (!state.store) return;
  if (state.detection !== "ok") { openDialog("Shift handoff", `Detection did not run, so this is not an all-clear. ${state.detectionNote}`); return; }
  try {
    await ensureActive(state.folder);
    const h = await state.eog.handoff(state.at);
    if (h.dataset !== state.folder) throw new Error(`the backend has ${h.dataset} active, not ${state.folder}`);
    openDialog("Shift handoff", h.text);
  } catch (e) {
    openDialog("Shift handoff", `The handoff did not load (${e.message}). This is not an all-clear.`);
  }
};

function download(name, text) {
  const blob = new Blob([text], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

$("dsExport").onclick = async () => {
  if (!state.store) return;
  if (state.detection !== "ok") {
    openDialog("events.json", state.detection === "loading" ? "No events to export yet: detection is still loading." : `No events to export: ${state.detectionNote}`);
    return;
  }
  try {
    // The view (not ?download=1) names its dataset, so the file is never another folder's.
    await ensureActive(state.folder);
    const v = await state.eog.events(state.at);
    if (v.dataset !== state.folder) throw new Error(`the backend has ${v.dataset} active, not ${state.folder}`);
    download("events.json", JSON.stringify(v.events, null, 2));
  } catch (e) {
    openDialog("events.json", `The export did not load (${e.message}).`);
  }
};

function renderFoot() {
  const n = state.store?.times.length ?? 0;
  const src = state.detection === "ok" ? "alerts and plans: the backend's dataset mode (/api/eog)" : "alerts and plans: off";
  $("dsFoot").textContent = state.store
    ? `${state.folder} · network.json, ops.json, telemetry.csv · ${n.toLocaleString("en-US")} readings per sensor, every 5 min · ${state.store.series.size} sensors · ${src}`
    : "";
}

window.addEventListener("resize", (() => { let t; return () => { clearTimeout(t); t = setTimeout(() => { if (state.selected) renderDetail(); }, 200); }; })());
window.__dataset = { state };
boot().then(() => {
  try { if (localStorage.getItem("freeboard.dataset.view") === "3d" || params.get("view") === "3d") setView("3d"); } catch {}
});
