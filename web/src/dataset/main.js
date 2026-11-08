// Dataset console: the sponsor's "Beat the Shutdown" data on one page.
// Loads any dataset folder (network.json, ops.json, telemetry.csv), shows every system,
// lists the backend's alerts in SPEC order, replays a pump-failure plan minute by
// minute, trends any sensor, and can replay any past moment as "now".
// "Now" always comes from the telemetry, never from the system clock.

import "@fontsource/gloock/400.css";
import "@fontsource/great-vibes/400.css"; // the gaia mark, same as the console
import "@fontsource/azeret-mono/400.css";
import "@fontsource/azeret-mono/600.css";
import "./dataset.css";
import { apiSource, fileSource, assetIndex, checkDataset, STEP_S } from "./data.js";
import { lineChart, fmtTime } from "./chart.js";
import { replay, doNothing, inputFor, planFromActions, planToActions, cheapestJoint, walkCurve, money, revenuePerHour } from "./plan.js";
import { normAlerts, sortAlerts, actionLines, handoffText, durationText, categoryOf, trucksInUse } from "./alerts.js";
import { screenDataset, screenAt, screenSeries, recentMean, faultRuns, noiseSd, confidenceAt, WEAR } from "./screen.js";

const $ = (id) => document.getElementById(id);
// A peak just under the limit must not print as the limit: 91.96% is "91.9%", not "92.0%".
const peak1 = (v) => (Math.floor(v * 10 + 1e-9) / 10).toFixed(1);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const params = new URLSearchParams(location.search);
const API = params.get("api") ?? import.meta.env.VITE_API ?? "";

const state = {
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
  selected: null, // { kind: "facility" | "alert" | "whatif", id }
  win: 24 * 3600,
  view: "map", // map | 3d
  sys3d: null,
  plans: new Map(), // per alert/what-if: the plan the operator is looking at
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
  // The backend's dataset mode first; the dev server's /eog/ folder when it has none.
  const api = apiSource(API);
  try {
    const info = await api.info();
    if (Array.isArray(info?.folders)) return { source: api, info };
  } catch (e) {
    if (e.status && e.status !== 404) console.warn("dataset API:", e.message);
  }
  const files = fileSource("/eog");
  const info = await files.info();
  return { source: files, info };
}

function folderFromUrl(folders) {
  const want = params.get("folder");
  if (want && folders.includes(want)) return want;
  try {
    const last = localStorage.getItem("freeboard.dataset.folder");
    if (last && folders.includes(last)) return last;
  } catch {}
  return folders[0];
}

async function boot() {
  let picked;
  try {
    picked = await pickSource();
  } catch (e) {
    setStatus("No dataset found. Start the backend with dataset mode, or run the dev server with EOG_DIR pointing at the sponsor data.", "alarm");
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
  state.alerts = [];
  state.detection = "loading";
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
    return;
  }
  if (seq !== state.loadSeq) return;
  Object.assign(state, data);
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
  // Start an hour in: a detector needs some history before "now".
  s.min = String(Math.min(state.store.now, state.store.start + 3600));
  s.max = String(state.store.now);
  s.step = String(STEP_S);
  s.value = String(state.at);
  s.oninput = () => {
    state.at = Number(s.value);
    // The alerts on screen belong to the old moment: never show them as current.
    state.alertSeq++;
    state.alerts = [];
    state.detection = "loading";
    renderNow();
    renderMap();
    renderAlerts();
    clearTimeout(slideTimer);
    const seq = state.loadSeq;
    slideTimer = setTimeout(() => { if (seq === state.loadSeq) { syncAtToUrl(); refreshAlerts(); } }, 250);
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

async function refreshAlerts() {
  const seq = ++state.alertSeq;
  const at = state.at;
  if (state.source.kind !== "api") {
    state.alerts = [];
    state.detection = "absent";
    state.detectionNote = "Detection runs in the backend's dataset mode, and this page is reading the files directly.";
    renderAlerts();
    renderDetail();
    return;
  }
  try {
    const folder = state.folder;
    const resp = await state.source.alerts(travelling() ? at : null, folder);
    if (seq !== state.alertSeq || at !== state.at || folder !== state.folder) return;
    // The backend answers for the folder it has loaded, which another tab can change.
    if (resp && !Array.isArray(resp) && resp.folder && resp.folder !== folder)
      throw Object.assign(new Error(`the backend has ${resp.folder} loaded, not ${folder}`), { status: 409 });
    state.alerts = normAlerts(resp);
    state.detection = "ok";
    state.detectionNote = "";
  } catch (e) {
    if (seq !== state.alertSeq) return;
    state.alerts = [];
    state.detection = e.status === 404 ? "absent" : "error";
    state.detectionNote = e.status === 404 ? "The backend has no alerts for datasets yet." : `The alerts did not load (${e.message}).`;
  }
  renderAlerts();
  renderMap();
  renderDetail();
}

// When the backend's detection is not there (file mode, or its alerts failed), the page
// screens the readings itself: pump fault signals, leaks and worn pumps (screen.js). Every
// list below reads from pool(), so the map, cards and detail agree on one set.
const usesScreen = () => state.detection === "absent" || state.detection === "error";
const pool = () => (state.detection === "ok" ? state.alerts : usesScreen() ? screenPool() : []);
const liveAlerts = () => sortAlerts(pool().filter((a) => a.status !== "resolved"));
const alertsFor = (facilityId) => liveAlerts().filter((a) => a.facility === facilityId);
// Readings at the moment on screen. Well rates are the last hour's mean of producing
// readings, so one noisy sample does not move a countdown by minutes.
const readers = () => ({
  latest: (a, m) => state.store.latest(a, m, state.at),
  rate: (w) => state.store.mean(w, "water_bpd", state.at, 3600, true),
  // While the pump is down and the tank fills in a straight line, fit the last 30 min of
  // level readings instead of trusting one noisy sample. Otherwise the latest reading.
  level: (tank, pump) => {
    const st = state.store;
    for (let k = 0; k < 6; k++) if (st.latest(pump, "status", state.at - k * STEP_S) === 1) return null;
    const lim = state.network.defaults.tank_limits_pct.shutdown;
    const raw = st.latest(tank, "level_pct", state.at);
    if (raw === null || raw >= lim - 0.5) return null; // at the limit the level holds: no line to fit
    const fit = st.fitAt(tank, "level_pct", state.at, 6);
    return fit === null || Math.abs(fit - raw) > 2 ? null : fit;
  },
});
const repairH = () => state.ops?.expected_repair_hours?.pump_failure ?? null;

// Pumps whose own status signal reads 2 (fault) at this moment, with when that run of
// fault readings began. This is one raw sensor, not detection: a leak or a worn pump
// never shows here. Used only when the backend's detection is not available.
function pumpFaults(at = state.at) {
  const out = [];
  if (!state.network || !state.store) return out;
  const { times, series } = state.store;
  for (const s of state.network.systems) for (const f of s.facilities) {
    if (state.store.latest(f.pump.id, "status", at) !== 2) continue;
    const col = series.get(f.pump.id + "|status");
    let i = Math.min(times.length - 1, Math.floor((at - times[0]) / STEP_S));
    while (i > 0 && col[i - 1] === 2) i--; // a gap ends the run: never stretch the onset across missing data
    out.push({ system: s.id, facility: f.id, pump: f.pump.id, onset: times[i] });
  }
  return out;
}

// The page's own cheapest-plan search for every pump failure in progress, by system, so
// tanks in one system share its trucks. In file mode it is the plan; in backend mode it
// checks the backend's. Keyed by facility. Memoized per folder and moment.
let jointMemo = { key: null, map: new Map() };
function ownPlans() {
  const key = `${state.folder}@${state.at}@${state.detection}@${state.alerts.length}`;
  if (jointMemo.key === key) return jointMemo.map;
  const map = new Map();
  const h = repairH();
  const failures = state.detection === "ok"
    ? state.alerts.filter((a) => a.status !== "resolved" && a.category === "pump_failure" && a.facility && a.onset !== null)
        .map((a) => ({ system: a.system, facility: a.facility, onset: a.onset, repairEnd: a.repairEnd }))
    : pumpFaults();
  const bySystem = new Map();
  for (const x of failures) {
    if (h === null) break;
    const horizonMin = Math.round(((x.repairEnd ?? x.onset + h * 3600) - state.at) / 60);
    if (horizonMin <= 0) continue;
    const built = inputFor({ network: state.network, ops: state.ops, facilityId: x.facility, ...readers(), horizonMin });
    if (!built.input || !built.opsFacility) continue;
    const sys = built.system.id;
    if (!bySystem.has(sys)) bySystem.set(sys, []);
    bySystem.get(sys).push({ id: x.facility, input: built.input, truckSpec: {
      responseMin: built.opsFacility.response_min, cycleMin: built.opsFacility.cycle_min,
      capacityBbl: state.ops.emergency_trucking.truck_capacity_bbl, costPerTrip: state.ops.emergency_trucking.cost_per_trip } });
  }
  for (const [sys, tanks] of bySystem) {
    const fleet = state.ops.systems?.[sys]?.emergency_fleet_size ?? 0;
    const best = cheapestJoint(tanks, fleet);
    if (!best) continue;
    for (const p of best.picks) {
      const tk = tanks.find((t) => t.id === p.id);
      map.set(p.id, { ...p, doNothing: doNothing(tk.input), shared: tanks.length > 1 ? tanks.map((t) => t.id) : null });
    }
  }
  jointMemo = { key, map, bySystem };
  return map;
}

// README "How big should the trucking fleet be?": the cheapest total for this system's
// failures in progress with 0 to fleet + 2 trucks. Same search, only the fleet changes.
function fleetCurve(systemId) {
  ownPlans();
  const tanks = jointMemo.bySystem?.get(systemId);
  if (!tanks?.length) return null;
  const fleet = state.ops.systems?.[systemId]?.emergency_fleet_size ?? 0;
  const out = [];
  for (let n = 0; n <= fleet + 2; n++) out.push({ n, total: cheapestJoint(tanks, n)?.total ?? null, current: n === fleet });
  return { tanks: tanks.map((t) => t.id), fleet, rows: out };
}

function fleetHtml(systemId) {
  const c = fleetCurve(systemId);
  if (!c || c.rows.some((r) => r.total === null)) return "";
  const max = Math.max(...c.rows.map((r) => r.total), 1);
  const cur = c.rows.find((r) => r.current)?.total ?? null;
  const rows = c.rows.map((r) => {
    const save = cur !== null && r.n > c.fleet && cur - r.total > 0.5 ? ` · saves ${money(cur - r.total)}` : "";
    return `<div class="ds-fleet-row${r.current ? " on" : ""}"><span>${r.n} truck${r.n === 1 ? "" : "s"}${r.current ? " (today)" : ""}</span>
      <i style="width:${Math.max(1, (r.total / max) * 100).toFixed(1)}%"></i><b>${money(r.total)}</b><em>${save}</em></div>`;
  }).join("");
  return `<div class="ds-fleet"><span class="kicker">Fleet size: cheapest total for ${esc(systemId)}'s ${c.tanks.length} failure${c.tanks.length === 1 ? "" : "s"} in progress</span>${rows}</div>`;
}

// ---------- the page's own screen ----------

// Pumps the screen could not check at this moment: stopped ones (no flow to compare), and
// ones missing a fresh reading of any signal the screen reads. Faulted pumps are listed
// as failures, so they are not repeated here.
function unscreened(at = state.at) {
  const off = [], blind = [];
  for (const sys of state.network.systems) {
    const disp = Number(sys.disposal_site?.pressure_psi);
    for (const f of sys.facilities) {
      const st = state.store.latest(f.pump.id, "status", at);
      if (st === 2) continue;
      if (st === 0) { off.push(f.pump.id); continue; }
      const fresh = st === 1 && Number.isFinite(disp) && ["flow_bpd", "discharge_psi"].every((m) => state.store.latest(f.pump.id, m, at) !== null)
        && state.store.latest(f.pipeline.id, "receive_flow_bpd", at) !== null;
      if (!fresh) blind.push(f.pump.id);
    }
  }
  return { off, blind };
}

// Memos are keyed on the store object itself, so a folder reload (even one with the same
// name or the same "now") can never serve another load's screen.
let screenMemo = { store: null, events: [] };
function screenEvents() {
  if (screenMemo.store !== state.store) screenMemo = { store: state.store, events: state.store && state.network ? screenDataset(state.network, state.store) : [] };
  return screenMemo.events;
}

const pct1 = (v) => `${v.toFixed(1)}%`;
const bpd = (v) => `${Math.round(v).toLocaleString("en-US")} bbl/day`;
const blank = { confidence: null, estCost: null, doNothingCost: null, hours: null, hoursP10: null, hoursP90: null, levelPct: null,
  lostRevPerHour: null, tripsAt: null, repairEnd: null, tripped: false, planSummary: null };

// Leaks and worn pumps the screen had seen by `at`, plus pump failures that had already
// ended, as alert-shaped objects with plain-English evidence. Memoized per moment.
let poolMemo = { store: null, at: null, list: [] };
function screenPool() {
  if (poolMemo.store === state.store && poolMemo.at === state.at) return poolMemo.list;
  const at = state.at;
  const list = [];
  for (const e of screenAt(screenEvents(), at)) {
    const hit = facilityOf(e.facility);
    if (!hit) continue;
    const disp = Number(hit.s.disposal_site?.pressure_psi);
    const ser = screenSeries(state.store, hit.f, disp);
    const ongoing = e.status === "ongoing";
    const ev = [];
    const pumpSt = ongoing ? state.store.latest(hit.f.pump.id, "status", at) : 1;
    if (e.category === "pipeline_leak") {
      const now = ongoing ? recentMean(ser.lost, at) : e.atSeen;
      const flow = state.store.mean(hit.f.pump.id, "flow_bpd", ongoing ? at : e.seen, 3600, true);
      if (now !== null) ev.push(`${ongoing ? "Over the last hour of pumping" : "When it was seen"}, ${pct1(now)} of the pump's flow${flow ? ` (about ${bpd((now / 100) * flow)})` : ""} did not arrive at disposal. Healthy is 0, within about 1%.`);
    } else {
      const now = ongoing ? recentMean(ser.excess, at) : e.atSeen;
      if (now !== null) ev.push(`${ongoing ? "Over the last hour of pumping" : "When it was seen"}, discharge pressure ran ${Math.round(-now)} psi under the healthy ${Math.round(disp + WEAR.healthy)} psi (disposal + 150).`);
      const before = state.store.mean(hit.f.pump.id, "flow_bpd", e.onset, 6 * 3600, true);
      const after = state.store.mean(hit.f.pump.id, "flow_bpd", ongoing ? at : e.end ?? e.seen, 6 * 3600, true);
      if (before && after && after < before) ev.push(`It moves ${pct1(((before - after) / before) * 100)} less water than before it started (${bpd(after)} vs ${bpd(before)}).`);
    }
    ev.push(e.afterGap && e.from
      ? `The pump was off from ${fmtTime(e.from, true)} to ${fmtTime(e.onset, true)} UTC; it started in that window and showed at the first reading after.`
      : `Started about ${fmtTime(e.onset, true)} UTC, fitted from the readings. First seen ${fmtTime(e.seen, true)} UTC.`);
    const sig = e.category === "pipeline_leak" ? ser.lost : ser.excess;
    const conf = confidenceAt(sig, ongoing ? at : e.seen, noiseSd(sig, e.onset), e.category === "pipeline_leak" ? 1 : -1, 24, e.onset);
    if (conf) ev.push(`Confidence ${Math.round(conf.share * 100)}%: ${conf.k} of the ${conf.n} latest running readings since it started sit outside this ${e.category === "pipeline_leak" ? "line" : "pump"}'s normal noise (±${e.category === "pipeline_leak" ? pct1(conf.band) : `${Math.round(conf.band)} psi`}).`);
    if (pumpSt === null) ev.push("No fresh reading from the pump now, so these are its last running readings. Not checked since.");
    else if (pumpSt !== 1) ev.push("The pump is not running now, so these are its last running readings.");
    if (!ongoing) ev.push(e.endedByFault ? `Ended when the pump failed at ${fmtTime(e.end, true)} UTC (a separate pump failure).` : `Back to normal by ${fmtTime(e.end, true)} UTC.`);
    list.push({ ...blank, id: `screen:${e.category}:${e.asset}:${e.onset}`, screened: true, category: e.category, status: e.status,
      system: e.system, facility: e.facility, asset: e.asset, onset: e.onset, end: e.end, evidence: ev, confidence: conf ? conf.share : null,
      actions: [{ type: "field_inspection", asset_id: e.asset }] });
  }
  // Pump failures that ended by `at`, from the status signal (ongoing ones are fault rows).
  for (const sys of state.network.systems) for (const f of sys.facilities) {
    for (const r of faultRuns(state.store, f.pump.id)) {
      if (r.onset > at || r.end === null || r.end > at) continue;
      list.push({ ...blank, id: `fault:${f.id}:${r.onset}`, screened: true, category: "pump_failure", status: "resolved",
        system: sys.id, facility: f.id, asset: f.pump.id, onset: r.onset, end: r.end, actions: [],
        evidence: [`${f.pump.id} read status 2 (fault) from ${fmtTime(r.onset, true)} to ${fmtTime(r.last, true)} UTC.`] });
    }
  }
  poolMemo = { store: state.store, at: state.at, list };
  return list;
}

// A pump fault as an alert-shaped object, so the what-if can plan it. No backend plan.
const faultAlert = (x) => ({ id: `fault:${x.facility}`, synthetic: true, category: "pump_failure", status: "ongoing",
  system: x.system, facility: x.facility, asset: x.pump, onset: x.onset, end: null, actions: [], evidence: [],
  estCost: null, doNothingCost: null, hours: null, levelPct: null, confidence: null });

// Pumps in fault now, each with the do-nothing replay and the page's own plan, in the
// SPEC alerts order: soonest shutdown first, ties to the bigger loss of revenue.
function faultRows() {
  const h = repairH();
  return pumpFaults().map((x) => {
    const horizonMin = h === null ? 0 : Math.round((x.onset + h * 3600 - state.at) / 60);
    const built = horizonMin > 0 ? inputFor({ network: state.network, ops: state.ops, facilityId: x.facility, ...readers(), horizonMin }) : null;
    const r = built?.input ? doNothing(built.input) : null;
    const loss = built?.input ? built.input.wells.reduce((n, w) => n + (w.producing ? w.revPerHour : 0), 0) : 0;
    const own = ownPlans().get(x.facility);
    const level = state.store.latest(x.facility && facilityOf(x.facility)?.f.tank.id, "level_pct", state.at);
    // The same fault as a filled-in alert, for the handoff and the export.
    const a = { ...faultAlert(x), lostRevPerHour: loss, levelPct: level, tripped: !!r?.tripped,
      hours: r ? (r.tripped ? 0 : r.safe ? null : r.tripsAtMin / 60) : null,
      repairEnd: h === null ? null : x.onset + h * 3600,
      actions: own ? planToActions(own.plan, x.facility) : [], estCost: own ? own.result.cost.total : null,
      doNothingCost: own ? own.doNothing.cost.total : r ? r.cost.total : null,
      evidence: [`${x.pump} reads status 2 (fault) since ${fmtTime(x.onset, true)} UTC.`] };
    return { x, r, loss, own, a };
  }).sort((p, q) => (p.r?.tripsAtMin ?? Infinity) - (q.r?.tripsAtMin ?? Infinity) || q.loss - p.loss);
}

function renderAlerts() {
  const box = $("dsAlerts");
  const live = liveAlerts();
  const resolved = pool().filter((a) => a.status === "resolved");
  $("dsAlertCount").textContent = state.detection === "ok" ? `${live.length} in progress` : "";
  if (state.detection === "loading") {
    box.innerHTML = `<p class="ds-hint">Checking for problems…</p>`;
  } else if (state.detection !== "ok") {
    const rows = faultRows();
    $("dsAlertCount").textContent = `${rows.length + live.length} in progress`;
    box.innerHTML = `<div class="ds-unknown"><b>Screened by this page</b><p>${esc(state.detectionNote)} gaia screened the readings itself:
      pump fault signals, leaks (pump flow that never reaches disposal) and worn pumps (discharge pressure sagging).</p>
      <p class="ds-hint">It is a screen, not the backend's detection. Anything it can't read, it doesn't call safe.</p></div>`;
    if (rows.length) {
      const wrap = document.createElement("div");
      wrap.className = "ds-faults";
      wrap.innerHTML = `<span class="kicker">Pump failures: pumps reporting a fault on their own status signal</span>`;
      for (const { x, r, own, a: full } of rows) {
        const a = faultAlert(x);
        const when = !r ? "" : r.tripped ? "tank at shutdown: its wells are shut in until the repair" : r.safe ? "will not reach shutdown before the repair" : `shuts in at ${fmtTime(state.at + r.tripsAtMin * 60)} UTC (in ${durationText(r.tripsAtMin / 60)}) if nobody acts`;
        const b = document.createElement("button");
        b.type = "button";
        b.className = `ds-fault${state.selected?.id === a.id ? " on" : ""}`;
        let plan = "";
        if (own) {
          const lines = actionLines(full, { at: state.at, ops: state.ops });
          plan = `<ul class="ds-plan">${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
            <span class="ds-cost"><b>${money(own.result.cost.total)}</b> vs ${money(own.doNothing.cost.total)} doing nothing${own.shared ? ` · shares ${esc(x.system)}'s trucks` : ""}</span>`;
        }
        b.innerHTML = `<span class="ds-tag alarm">FAULT</span><b>${esc(x.pump)}</b><span>since ${esc(fmtTime(x.onset, true))}${when ? " · " + esc(when) : ""}</span><em>Plan it</em>${plan}`;
        b.onclick = () => select({ kind: "fault", id: a.id });
        wrap.appendChild(b);
      }
      box.appendChild(wrap);
    }
    const wrap = document.createElement("div");
    wrap.className = "ds-faults";
    wrap.innerHTML = `<span class="kicker">Leaks and worn pumps, from the readings</span>`;
    live.forEach((a, i) => wrap.appendChild(alertCard(a, rows.length + i + 1)));
    const gaps = unscreened();
    const note = [
      !live.length ? `None in progress at ${esc(fmtTime(state.at, true))} UTC among the pumps it could check.` : "",
      gaps.off.length ? `${gaps.off.length} pump${gaps.off.length === 1 ? " is" : "s are"} stopped, so ${gaps.off.length === 1 ? "its line" : "their lines"} can't be checked until ${gaps.off.length === 1 ? "it runs" : "they run"}.` : "",
      gaps.blind.length ? `Not checked, no fresh reading: ${esc(gaps.blind.join(", "))}.` : "",
    ].filter(Boolean).join(" ");
    if (note) wrap.insertAdjacentHTML("beforeend", `<p class="ds-hint">${note}</p>`);
    box.appendChild(wrap);
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
    const when = a.hours !== null ? fmtTime(state.at + a.hours * 3600) : null;
    const range = a.hoursP10 !== null && a.hoursP90 !== null ? ` <span class="ds-range">(${durationText(a.hoursP10)} to ${durationText(a.hoursP90)})</span>` : "";
    body += a.tripped
      ? `<div class="ds-count-big"><span class="kicker">Tank at shutdown</span><b>Wells shut in</b><span>${a.tripsAt !== null ? `since ${esc(fmtTime(a.tripsAt))} UTC, ` : ""}until the repair${a.repairEnd ? ` at ${esc(fmtTime(a.repairEnd))} UTC` : ""}</span></div>`
      : a.hours !== null
      ? `<div class="ds-count-big"><span class="kicker">Shuts in at</span><b>${esc(when)} UTC</b><span>in ${esc(durationText(a.hours))}${range}</span></div>`
      : `<div class="ds-count-big quiet"><span class="kicker">Countdown</span><b>Will not trip</b><span>tank stays under shutdown until the repair</span></div>`;
    if (a.levelPct !== null) body += `<div class="ds-meta">Tank ${Math.round(a.levelPct)}% now</div>`;
  }
  const lines = actionLines(a, { at: state.at, ops: state.ops });
  body += `<ul class="ds-plan">${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`;
  if (a.category === "pump_failure" && a.estCost !== null) {
    const saves = a.doNothingCost !== null && a.doNothingCost > a.estCost ? ` · saves ${money(a.doNothingCost - a.estCost)}` : "";
    body += `<div class="ds-cost"><b>${money(a.estCost)}</b>${a.doNothingCost !== null ? ` vs ${money(a.doNothingCost)} doing nothing` : ""}${saves}</div>`;
  }
  const check = a.category === "pump_failure" ? replayCheck(a) : null;
  if (check) body += `<div class="ds-check ${check.tone}">${esc(check.text)}</div>`;
  if (a.confidence !== null) body += `<div class="ds-meta">Confidence ${Math.round(a.confidence * 100)}%</div>`;
  card.innerHTML = `<header><span class="ds-rank">${rank}</span><span class="ds-tag ${c.tone}">${esc(c.short)}</span>
    <b>${esc(a.facility || a.asset)}</b><span class="ds-since">${esc(since)}</span></header>
    <div class="ds-asset">${esc(c.label)} · ${esc(a.asset || "")}</div>${body}`;
  card.onclick = () => select({ kind: "alert", id: a.id });
  card.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select({ kind: "alert", id: a.id }); } };
  return card;
}

// Replays the backend's plan from this moment's readings. If the replay says the tank
// trips but the backend's plan says it won't (or the costs differ), the card says so.
function replayCheck(a) {
  const h = repairH();
  if (!a.facility || a.onset === null || (h === null && !a.repairEnd)) return null;
  const horizonMin = Math.round(((a.repairEnd ?? a.onset + h * 3600) - state.at) / 60);
  if (horizonMin <= 0) return { tone: "ok", text: "Repair time is up: the pump should be back." };
  const plan = { ...planFromActions(a.actions, a.facility), fleet: undefined };
  const built = inputFor({ network: state.network, ops: state.ops, facilityId: a.facility,
    ...readers(), horizonMin, plan });
  if (built.error) return { tone: "warning", text: `Replay not possible: ${built.error}` };
  const r = replay(built.input);
  if (r.tripped) return { tone: "alarm", text: "Replayed: the tank is already at shutdown, so its wells stay shut in until the repair." };
  if (!r.safe && a.actions.some((x) => x.type === "dispatch_truck" || x.type === "shut_in_wells"))
    return { tone: "alarm", text: `Replay disagrees: with this plan the tank still reaches shutdown at ${fmtTime(state.at + r.tripsAtMin * 60)} UTC.` };
  if (a.estCost !== null && Math.abs(r.cost.total - a.estCost) > Math.max(10, a.estCost * 0.03))
    return { tone: "warning", text: `Replay prices this plan at ${money(r.cost.total)}, not ${money(a.estCost)}.` };
  const own = ownPlans().get(a.facility);
  if (own && own.result.cost.total < r.cost.total - Math.max(10, r.cost.total * 0.03))
    return { tone: "warning", text: `Gaia's own planner finds ${money(own.result.cost.total)} here (the backend's plan replays at ${money(r.cost.total)}). Open the plan to compare.` };
  return { tone: r.safe ? "ok" : "warning", text: r.safe ? `Replayed: peaks at ${peak1(r.peakPct)}%, never reaches shutdown.` : `Replayed: the tank reaches shutdown at ${fmtTime(state.at + r.tripsAtMin * 60)} UTC, as the backend says.` };
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
      const sel = state.selected && (state.selected.id === f.id || state.selected.id === `fault:${f.id}` || al.some((a) => a.id === state.selected.id)) ? " on" : "";
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
      const glbUrl = (sys) => state.source?.kind === "api"
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
  const selFac = state.selected && (facilityOf(state.selected.id) || facilityOf(state.selected.id.replace(/^fault:/, ""))
    || facilityOf(pool().find((a) => a.id === state.selected.id)?.facility));
  if (selFac) state.sys3d = selFac.s.id;
  if (!systems.some((s) => s.id === state.sys3d)) state.sys3d = systems[0]?.id;
  const sys = systems.find((s) => s.id === state.sys3d);
  if (!sys) return;
  $("ds3dTabs").innerHTML = systems.map((s) => {
    const n = s.facilities.reduce((k, f) => k + alertsFor(f.id).length + (state.store.latest(f.pump.id, "status", state.at) === 2 && state.detection !== "ok" ? 1 : 0), 0);
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
  if (sel.kind === "fault") {
    const x = pumpFaults().find((f) => `fault:${f.facility}` === sel.id);
    if (!x) { box.innerHTML = `<p class="ds-hint">That pump is not reporting a fault at this time.</p>`; return; }
    alert = faultAlert(x);
    facId = x.facility;
  } else if (sel.kind === "alert") {
    alert = pool().find((a) => a.id === sel.id);
    if (!alert) { box.innerHTML = `<p class="ds-hint">That alert is not in progress at this time.</p>`; return; }
    facId = alert.facility;
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

  if (alert && alert.category === "pump_failure" && alert.status !== "resolved") box.appendChild(whatIf(f, s, alert));
  else if (sel.kind === "whatif") box.appendChild(whatIf(f, s, null));
  else if (alert && alert.evidence.length) {
    const ev = document.createElement("div");
    ev.className = "ds-evidence";
    ev.innerHTML = `<span class="kicker">${alert.screened ? "Why gaia's screen flagged it" : "Why the backend flagged it"}</span><ul>${alert.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`;
    box.appendChild(ev);
  }
  if (!alert || alert.category !== "pump_failure") {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ds-whatif-btn";
    btn.textContent = sel.kind === "whatif" ? "Back to readings" : "What if this pump failed now?";
    btn.onclick = () => select(sel.kind === "whatif" ? { kind: "facility", id: f.id } : { kind: "whatif", id: f.id });
    box.appendChild(btn);
  }
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

// README "Go further": what if the whole disposal site shut down? Every pump in the system
// stops at once, every tank fills, and they all share the system's trucks. Plans every tank
// together for the outage, and prices the same outage with 0 to fleet + 3 trucks.
// Assumes the trucks haul to another site on the same cycle times as ops.json.
const outageMemo = new Map(); // `${at}@${system}@${hours}` -> Promise of the result, per loaded store
let outageStore = null, outageSeq = 0;
const outageWorkers = [], outagePending = new Map();
// One tank per worker, a small pool sized to the machine.
function outageTank(tank, maxK) {
  const size = Math.max(1, Math.min(4, (navigator.hardwareConcurrency || 2) - 1));
  if (!outageWorkers.length) for (let i = 0; i < size; i++) {
    const w = new Worker(new URL("./outage.worker.js", import.meta.url), { type: "module" });
    w.onmessage = (e) => { const p = outagePending.get(e.data.id); outagePending.delete(e.data.id); p?.(e.data); };
    outageWorkers.push(w);
  }
  const id = ++outageSeq;
  return new Promise((resolve) => { outagePending.set(id, resolve); outageWorkers[id % outageWorkers.length].postMessage({ id, tank, maxK }); });
}
function outageRun(tanks, maxFleet) {
  return Promise.all(tanks.map((t) => outageTank(t, maxFleet))).then((rows) => {
    const bad = rows.find((r) => r.error);
    if (bad) return { error: bad.error };
    return { curve: walkCurve(tanks, rows.map((r) => r.row), maxFleet), nothing: rows.map((r, i) => ({ id: tanks[i].id, r: r.nothing })) };
  });
}
function outagePlan(sys, hours) {
  if (outageStore !== state.store) { outageMemo.clear(); outageStore = state.store; }
  const key = `${state.at}@${sys.id}@${hours}`;
  if (outageMemo.has(key)) return outageMemo.get(key);
  const horizonMin = Math.round(hours * 60);
  const tanks = [], skipped = [];
  for (const f of sys.facilities) {
    const built = inputFor({ network: state.network, ops: state.ops, facilityId: f.id, ...readers(), horizonMin });
    if (!built.input || !built.opsFacility) { skipped.push(`${f.id}: ${built.error || "no truck times in ops.json"}`); continue; }
    tanks.push({ id: f.id, input: built.input, truckSpec: {
      responseMin: built.opsFacility.response_min, cycleMin: built.opsFacility.cycle_min,
      capacityBbl: state.ops.emergency_trucking.truck_capacity_bbl, costPerTrip: state.ops.emergency_trucking.cost_per_trip } });
  }
  const fleet = state.ops.systems?.[sys.id]?.emergency_fleet_size ?? 0;
  const p = (tanks.length ? outageRun(tanks, fleet + 3) : Promise.resolve({ curve: [], nothing: [] })).then((r) => ({
    hours, fleet, tanks, skipped, error: r.error || null, curve: r.curve || [], today: r.curve?.[fleet]?.best ?? null, nothing: r.nothing || [],
  }));
  outageMemo.set(key, p);
  return p;
}

function outagePanel(sys) {
  const el = document.createElement("div");
  el.className = "ds-outage";
  const repair = repairH();
  const choices = [...new Set([repair, 12, 24].filter((h) => Number.isFinite(h) && h > 0))];
  state.outageH ||= null;
  const head = `<span class="kicker">What if ${esc(sys.disposal_site?.id || "the disposal site")} shut down now?</span>
    <div class="ds-win" role="group" aria-label="Outage length">${choices.map((h) => `<button type="button" data-o="${h}" class="${state.outageH === h ? "on" : ""}">${h} h${h === repair ? " (a repair)" : ""}</button>`).join("")}</div>`;
  if (!state.outageH) {
    el.innerHTML = `${head}<p class="ds-hint">Every pump in ${esc(sys.id)} stops, every tank fills, and they share ${esc(sys.id)}'s trucks. Pick how long it lasts.</p>`;
  } else {
    el.innerHTML = `${head}<p class="ds-hint">Planning every tank together, with 0 to ${(state.ops.systems?.[sys.id]?.emergency_fleet_size ?? 0) + 3} trucks. A long outage takes a few seconds.</p>`;
  }
  for (const b of el.querySelectorAll("[data-o]")) b.onclick = () => { state.outageH = state.outageH === Number(b.dataset.o) ? null : Number(b.dataset.o); renderDetail(); };
  if (!state.outageH) return el;
  const want = { at: state.at, h: state.outageH, sys: sys.id };
  outagePlan(sys, state.outageH).then((o) => {
    // The operator may have moved on while it planned: draw only for what is still asked.
    if (!el.isConnected || state.at !== want.at || state.outageH !== want.h) return;
    const end = state.at + o.hours * 3600;
    if (!o.today) { el.innerHTML = `${head}<p class="ds-hint">No plan possible: ${esc(o.error || o.skipped.join("; ") || "no tanks with readings")}.</p>`; wire(); return; }
    const nothingTotal = o.nothing.reduce((n, x) => n + x.r.cost.total, 0);
    const rows = o.today.picks.map((p) => {
      const dn = o.nothing.find((x) => x.id === p.id).r;
      const trips = dn.tripped ? "already at shutdown" : dn.safe ? "never reaches shutdown" : `shuts in ${fmtTime(state.at + dn.tripsAtMin * 60)} UTC`;
      const fake = { category: "pump_failure", facility: p.id, system: sys.id, hours: dn.safe ? null : dn.tripsAtMin / 60, tripped: dn.tripped, actions: planToActions(p.plan, p.id) };
      const lines = actionLines(fake, { at: state.at, ops: state.ops });
      return `<tr><th>${esc(p.id)}<span class="ds-hint">${esc(trips)} if nobody acts</span></th>
        <td><ul class="ds-plan">${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></td>
        <td>${money(p.result.cost.total)}${p.result.safe ? "" : `<span class="ds-hint">shuts in ${esc(fmtTime(state.at + p.result.tripsAtMin * 60))}</span>`}</td></tr>`;
    }).join("");
    const max = Math.max(...o.curve.map((c) => c.best?.total ?? 0), 1);
    const bars = o.curve.map((c) => {
      const save = c.n > o.fleet && o.today.total - c.best.total > 0.5 ? ` · saves ${money(o.today.total - c.best.total)}` : "";
      return `<div class="ds-fleet-row${c.n === o.fleet ? " on" : ""}"><span>${c.n} truck${c.n === 1 ? "" : "s"}${c.n === o.fleet ? " (today)" : ""}</span>
        <i style="width:${Math.max(1, (c.best.total / max) * 100).toFixed(1)}%"></i><b>${money(c.best.total)}</b><em>${save}</em></div>`;
    }).join("");
    const extra = o.curve.find((c) => c.n > o.fleet && o.today.total - c.best.total > 0.5);
    el.innerHTML = `${head}
      <p class="ds-outage-sum">Until ${esc(fmtTime(end, true))} UTC: <b>${money(o.today.total)}</b> with ${esc(sys.id)}'s ${o.fleet} truck${o.fleet === 1 ? "" : "s"}, vs <b>${money(nothingTotal)}</b> doing nothing.${
        extra ? ` With ${extra.n} trucks it would cost ${money(o.today.total - (o.today.total - extra.best.total))}, ${money(o.today.total - extra.best.total)} less.` : ` More trucks would not save anything.`}</p>
      <table class="ds-costs ds-outage-t"><tbody>${rows}</tbody></table>
      <div class="ds-fleet"><span class="kicker">Fleet size for this outage</span>${bars}</div>
      ${o.skipped.length ? `<p class="ds-problems">${esc(o.skipped.join(" "))}</p>` : ""}
      <p class="ds-hint">Assumes the trucks haul to another site on the cycle times in ops.json, and the wells keep their last hour's rates.</p>`;
    wire();
    function wire() { for (const b of el.querySelectorAll("[data-o]")) b.onclick = () => { state.outageH = state.outageH === Number(b.dataset.o) ? null : Number(b.dataset.o); renderDetail(); }; }
  });
  return el;
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
  box.appendChild(outagePanel(sys));
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
  });
  return wrap;
}

// What-if: replays a plan for one tank from this moment and prices it against doing
// nothing. Starts from the backend's plan for an alert; the operator can change it.
function whatIf(f, s, alert) {
  const box = document.createElement("div");
  box.className = "ds-whatif";
  const h = repairH();
  const key = alert ? `alert:${alert.id}@${state.at}` : `fac:${f.id}@${state.at}`;
  let horizonMin;
  if (alert) horizonMin = alert.repairEnd ? Math.round((alert.repairEnd - state.at) / 60)
    : alert.onset !== null && h !== null ? Math.round((alert.onset + h * 3600 - state.at) / 60) : null;
  else horizonMin = h !== null ? Math.round(h * 60) : null;
  if (horizonMin === null) { box.innerHTML = `<p class="ds-hint">ops.json has no pump repair time, so no plan can be checked.</p>`; return box; }
  if (horizonMin <= 0) { box.innerHTML = `<p class="ds-hint">The repair time is up at this moment: the pump should be running again.</p>`; return box; }

  const fleetTotal = state.ops.systems?.[s.id]?.emergency_fleet_size ?? 0;
  let used = trucksInUse(state.alerts, s.id, alert?.id);
  if (alert?.synthetic) {
    // Pump faults planned by hand share the system's fleet the same way.
    for (const x of pumpFaults()) {
      if (x.system !== s.id || x.facility === f.id) continue;
      used += (state.plans.get(`alert:fault:${x.facility}@${state.at}`) ?? ownPlans().get(x.facility)?.plan)?.trucks || 0;
    }
  }
  const fleet = Math.max(0, fleetTotal - used);
  if (!state.plans.has(key)) {
    const own = alert?.synthetic ? ownPlans().get(f.id)?.plan : null;
    state.plans.set(key, own ? structuredClone(own) : alert ? planFromActions(alert.actions, f.id) : { trucks: 0, trips: 0, shutIns: [] });
  }
  const plan = state.plans.get(key);
  if (plan.trucks > fleet) { plan.trucks = fleet; delete plan.groups; } // the select can only show what is free
  const built = inputFor({ network: state.network, ops: state.ops, facilityId: f.id, ...readers(), horizonMin, plan: { ...plan, fleet } });
  if (built.error) { box.innerHTML = `<p class="ds-hint">${esc(built.error)}</p>`; return box; }
  // A hypothetical failure: the pump stops now, so the tank fills from its current level.
  const r = replay(built.input);
  const base = doNothing(built.input);
  const fo = built.opsFacility || {};
  const lim = state.network.defaults.tank_limits_pct;
  const maxTrips = fo.cycle_min ? Math.max(0, Math.floor((horizonMin - fo.response_min) / fo.cycle_min) + 1) : 0;

  const title = !alert ? "Practice: if this pump failed now" : alert.synthetic ? `Plan by hand: ${alert.asset} failed ${fmtTime(alert.onset, true)}` : "Plan for this failure";
  const backendCost = alert?.estCost;
  box.innerHTML = `<div class="ds-sec-head"><span class="kicker">${esc(title)}</span>
      <span class="ds-count">repair done ${esc(fmtTime(state.at + horizonMin * 60))} UTC · ${esc(durationText(horizonMin / 60))} left</span></div>
    <div class="ds-wi-grid">
      <div class="ds-wi-controls">
        <label>Trucks <select data-k="trucks">${Array.from({ length: fleet + 1 }, (_, i) => `<option ${i === plan.trucks ? "selected" : ""}>${i}</option>`).join("")}</select>
          <span class="ds-hint">${esc(fleet)} of ${esc(fleetTotal)} free in ${esc(s.id)}${used ? ` (${used} on other alerts)` : ""}</span></label>
        <label>Trips each <select data-k="trips">${Array.from({ length: Math.max(maxTrips, plan.trips) + 1 }, (_, i) => `<option ${i === plan.trips ? "selected" : ""}>${i}</option>`).join("")}</select>
          <span class="ds-hint">${esc(fo.response_min ?? "?")} min out, ${esc(fo.cycle_min ?? "?")} min round trip, ${esc(state.ops.emergency_trucking.truck_capacity_bbl)} bbl and ${money(state.ops.emergency_trucking.cost_per_trip)} a trip</span></label>
        <div class="ds-wells"><span class="kicker">Shut in wells early</span>
          ${f.wells.map((w) => {
            const si = plan.shutIns.find((x) => x.well === w.id);
            const rph = revenuePerHour(w, state.ops.prices);
            const wi = built.input.wells.find((x) => x.id === w.id);
            return `<label class="ds-well"><input type="checkbox" data-well="${esc(w.id)}" ${si ? "checked" : ""} ${wi && !wi.producing ? "disabled" : ""}>
              <b>${esc(w.id)}</b><span>${Math.round(wi?.waterBpd ?? 0)} bbl/d water · ${money(rph)}/h</span>
              <input type="number" min="0" max="${horizonMin}" step="5" value="${si ? Math.round(si.startMin) : 0}" data-start="${esc(w.id)}" ${si ? "" : "disabled"} aria-label="${esc(w.id)}: minutes from now"><em>min from now</em></label>`;
          }).join("")}
        </div>
        <div class="ds-wi-buttons">
          <button type="button" data-act="cheapest">Cheapest plan</button>
          ${alert && !alert.synthetic ? `<button type="button" data-act="backend">Backend plan</button>` : ""}
          <button type="button" data-act="clear">Do nothing</button>
        </div>
      </div>
      <div class="ds-wi-result">
        <div class="ds-wi-verdict ${r.safe ? "ok" : "alarm"}">${r.safe
          ? `<b>Stays under shutdown</b><span>peaks at ${peak1(r.peakPct)}%</span>`
          : r.tripped ? `<b>Tank already at shutdown</b><span>its wells stay shut in until the repair; no plan changes that now</span>`
          : `<b>Reaches shutdown at ${esc(fmtTime(state.at + r.tripsAtMin * 60))} UTC</b><span>wells shut in until the repair</span>`}</div>
        <table class="ds-costs"><tbody>
          <tr><th>Trucks</th><td>${money(r.cost.trucks)}</td><td class="ds-hint">${r.tripsMade} trip${r.tripsMade === 1 ? "" : "s"}, ${Math.round(r.removedBbl)} bbl</td></tr>
          <tr><th>Wells shut in early</th><td>${money(r.cost.shutIn)}</td><td></td></tr>
          <tr><th>Automatic shut-in</th><td>${money(r.cost.auto)}</td><td></td></tr>
          <tr class="total"><th>This plan</th><td>${money(r.cost.total)}</td><td></td></tr>
          <tr><th>Doing nothing</th><td>${money(base.cost.total)}</td><td class="ds-hint">${base.safe ? "never trips" : `trips ${esc(fmtTime(state.at + base.tripsAtMin * 60))}`}</td></tr>
          ${backendCost !== null && backendCost !== undefined ? `<tr><th>Backend's plan</th><td>${money(backendCost)}</td><td></td></tr>` : ""}
        </tbody></table>
        ${r.problems.length ? `<ul class="ds-problems">${r.problems.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
        <div class="ds-wi-chart"></div>
        ${alert ? fleetHtml(s.id) : ""}
      </div>
    </div>`;

  const rerender = () => renderDetail();
  for (const sel of box.querySelectorAll("select[data-k]")) sel.onchange = () => { plan[sel.dataset.k] = Number(sel.value); if (plan.trucks && !plan.trips) plan.trips = 1; rerender(); };
  for (const cb of box.querySelectorAll("input[data-well]")) cb.onchange = () => {
    const id = cb.dataset.well;
    plan.shutIns = plan.shutIns.filter((x) => x.well !== id);
    if (cb.checked) plan.shutIns.push({ well: id, startMin: 0 });
    rerender();
  };
  for (const inp of box.querySelectorAll("input[data-start]")) inp.onchange = () => {
    const si = plan.shutIns.find((x) => x.well === inp.dataset.start);
    if (si) si.startMin = Math.max(0, Math.min(horizonMin, Number(inp.value) || 0));
    rerender();
  };
  const b = box.querySelector("[data-act=backend]");
  if (b) b.onclick = () => { state.plans.set(key, planFromActions(alert.actions, f.id)); rerender(); };
  box.querySelector("[data-act=cheapest]").onclick = () => {
    // For a failure in progress, the joint search (shared trucks); for practice, this tank
    // alone with the trucks that are free.
    let p = alert ? ownPlans().get(f.id)?.plan : null;
    if (!p && fo.response_min !== undefined) {
      const best = cheapestJoint([{ id: f.id, input: { ...built.input, trucks: null, shutIns: [] }, truckSpec: {
        responseMin: fo.response_min, cycleMin: fo.cycle_min,
        capacityBbl: state.ops.emergency_trucking.truck_capacity_bbl, costPerTrip: state.ops.emergency_trucking.cost_per_trip } }], fleet);
      p = best?.picks[0]?.plan;
    }
    if (p) { state.plans.set(key, structuredClone(p)); rerender(); }
  };
  box.querySelector("[data-act=clear]").onclick = () => { state.plans.set(key, { trucks: 0, trips: 0, shutIns: [] }); rerender(); };

  requestAnimationFrame(() => {
    const t = (m) => state.at + m * 60;
    const marks = [];
    if (plan.trucks && plan.trips && fo.response_min !== undefined)
      for (let k = 0; k < plan.trips; k++) marks.push({ t: t(fo.response_min + k * fo.cycle_min), label: k ? "" : `${plan.trucks} truck${plan.trucks > 1 ? "s" : ""}` });
    for (const si of plan.shutIns) marks.push({ t: t(si.startMin), label: "shut-in" });
    lineChart(box.querySelector(".ds-wi-chart"), {
      title: "Tank level from now to the repair", unit: "% full", from: state.at, to: t(horizonMin), height: 150, gapS: 600,
      // Zoom to the band that matters: from a little under the lowest level to 100%.
      yMin: Math.max(0, Math.floor((Math.min(built.input.levelPct, ...r.points.map((p) => p[1])) - 10) / 10) * 10), yMax: 100,
      series: [
        { name: "this plan", points: r.points.filter((_, i) => i % 5 === 0 || i === r.points.length - 1).map(([m, v]) => [t(m), v]) },
        { name: "doing nothing", points: base.points.filter((_, i) => i % 5 === 0 || i === base.points.length - 1).map(([m, v]) => [t(m), v]) },
      ],
      refs: [{ y: lim.warning, label: `warning ${lim.warning}%`, tone: "warning" }, { y: lim.shutdown, label: `shutdown ${lim.shutdown}%`, tone: "alarm" }],
      marks, format: (v) => v.toFixed(1) + "%",
    });
  });
  return box;
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

$("dsHandoff").onclick = () => {
  if (!state.store) return;
  const screened = usesScreen();
  const alerts = screened ? [...faultRows().map((r) => r.a), ...pool()] : state.alerts;
  let text = handoffText({
    folder: state.folder, at: state.at, now: state.store.now, alerts,
    resolved: alerts.filter((a) => a.status === "resolved"), detection: screened ? "screen" : state.detection, ops: state.ops,
  });
  if (screened) {
    const gaps = unscreened();
    if (gaps.off.length) text += `\nStopped, so their lines could not be checked: ${gaps.off.join(", ")}.`;
    if (gaps.blind.length) text += `\nNot checked, no fresh reading: ${gaps.blind.join(", ")}.`;
  }
  openDialog("Shift handoff", text);
};

// events.json in the SPEC shape, from the page's own screen (file mode). Every event says
// in its evidence that it came from this page, not the backend.
function screenEventsJson() {
  const iso = (t) => (t === null ? null : new Date(t * 1000).toISOString().replace(".000Z", "Z"));
  const list = [...faultRows().map((r) => r.a), ...pool()].sort((a, b) => a.onset - b.onset);
  return list.map((a, i) => ({
    event_id: `GAIA-${String(i + 1).padStart(3, "0")}`,
    system_id: a.system, facility_id: a.facility, asset_id: a.asset, category: a.category, status: a.status,
    onset_ts: iso(a.onset), onset_unix: a.onset, end_ts: a.status === "resolved" ? iso(a.end) : null, end_unix: a.status === "resolved" ? a.end : null,
    confidence: a.confidence === null ? null : Math.round(a.confidence * 100) / 100,
    evidence: [...a.evidence, "Screened from the readings by gaia's dataset page; the backend's detection did not run."],
    ...(a.category === "pump_failure" && a.status === "ongoing" ? {
      level_pct: a.levelPct === null ? null : Math.round(a.levelPct * 10) / 10,
      hours_to_limit: a.hours === null ? null : { p50: Math.round(a.hours * 100) / 100 },
      recommended_actions: a.actions.length ? a.actions : [{ type: "none" }],
      est_cost: a.estCost === null ? null : Math.round(a.estCost),
    } : { hours_to_limit: null, recommended_actions: a.status === "ongoing" ? a.actions : [] }),
  }));
}

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
  if (usesScreen()) { download("events.json", JSON.stringify(screenEventsJson(), null, 2)); return; }
  const url = state.source?.eventsUrl(travelling() ? state.at : null);
  if (!url || state.detection !== "ok") {
    openDialog("events.json", "No events to export yet: detection is still loading.");
    return;
  }
  try {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${r.status}`);
    download("events.json", await r.text());
  } catch (e) {
    openDialog("events.json", `The export did not load (${e.message}).`);
  }
};

function renderFoot() {
  const n = state.store?.times.length ?? 0;
  const src = state.source?.kind === "api" ? "backend dataset mode" : "files from the dev server";
  $("dsFoot").textContent = state.store
    ? `${state.folder} · network.json, ops.json, telemetry.csv · ${n.toLocaleString("en-US")} readings per sensor, every 5 min · ${state.store.series.size} sensors · source: ${src}`
    : "";
}

window.addEventListener("resize", (() => { let t; return () => { clearTimeout(t); t = setTimeout(() => { if (state.selected) renderDetail(); }, 200); }; })());
window.__dataset = { state };
boot().then(() => {
  try { if (localStorage.getItem("freeboard.dataset.view") === "3d" || params.get("view") === "3d") setView("3d"); } catch {}
});
