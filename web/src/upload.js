// Upload screen (#43): drop a site plan, check what was read, build the field.
//
//   POST /api/ingest      multipart `file`  -> { ok, errors, review, counts, source, blueprint }
//   POST /api/blueprints  { blueprint }     -> { id, status, errors, warnings, counts, glb_url, note }
//   GET  /api/blueprints/{id}               -> same, polled while status is "building"
//
// Nothing is built while `errors` is non-empty, and every `review` item needs a human to
// fill it in or tick it. When the build is ready the console reopens on /api/field and the
// new field.glb. Asset IDs only ever come from /api/field.

import { BASE } from "./api.js";
import { authFetch } from "./auth.js";
import { holdButton } from "./fx.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const KINDS = [
  ["tanks", "Tank"], ["pumps", "Pump"], ["valves", "Valve"], ["wells", "Well"],
  ["disposal_wells", "Disposal well"], ["trucks", "Truck"], ["yards", "Yard"],
];
const NUMERIC = new Set(["capacity_bbl", "shutdown_pct", "warning_pct", "pump_low_pct", "rate_bbl_hr", "water_bbl_hr",
  "lost_usd_hr", "max_bbl_hr", "capacity_bbl", "load_minutes", "minutes_to_disposal"]);
const ACCEPT = ".png,.jpg,.jpeg,.webp,.gif,.pdf,.csv,.json";

let st = null; // { file, t0, res, fixes: Map<reviewIndex, {checked, values}> }
let timer = 0;

export function mountUpload() {
  const btn = document.createElement("button");
  btn.id = "openUpload";
  btn.className = "upload-open";
  btn.textContent = "Load field";
  document.querySelector(".brand-row").appendChild(btn);
  btn.onclick = open;
  if (location.hash === "#upload") open();
  addEventListener("hashchange", () => { if (location.hash === "#upload" && (!$("upload") || $("upload").hidden)) open(); });
}

function open() {
  let box = $("upload");
  if (!box) {
    box = document.createElement("div");
    box.id = "upload";
    box.className = "upload";
    document.body.appendChild(box);
  }
  st = null;
  box.hidden = false;
  history.replaceState(null, "", "#upload");
  drawDrop(box);
}

function close() {
  clearInterval(timer);
  $("upload").hidden = true;
  history.replaceState(null, "", location.pathname + location.search);
}

function shell(step, body) {
  const steps = ["Drop", "Check", "Build"].map((s, i) => `<span class="${i === step ? "on" : i < step ? "done" : ""}">0${i + 1} ${s}</span>`).join("");
  return `<header class="up-head">
      <div class="brand">GAIA</div>
      <nav class="up-steps">${steps}</nav>
      <span class="up-clock" id="upClock">${st && st.t0 ? "" : "--.-"}</span>
      <button id="upClose">Close</button>
    </header>${body}`;
}

function tickClock() {
  clearInterval(timer);
  timer = setInterval(() => {
    const el = $("upClock");
    if (el && st && st.t0) el.textContent = `${((performance.now() - st.t0) / 1000).toFixed(1)} s`;
  }, 100);
}

// ---------------------------------------------------------------- step 1 ----
function drawDrop(box, error = "") {
  box.innerHTML = shell(0, `<section class="up-drop" id="upDrop" tabindex="0">
      <h1>Bring your own field.</h1>
      <p>Drop a site plan here. A photo or PDF of the plan, a CSV equipment list, or a blueprint JSON.</p>
      <p class="muted">Claude reads drawings on the server. It never invents equipment or numbers. Anything it isn't sure of comes back for you to check.</p>
      <label class="up-pick"><input type="file" id="upFile" accept="${ACCEPT}" hidden><span>Choose a file</span></label>
      ${BASE ? "" : `<p class="note">No backend set (VITE_API). Only blueprint JSON can be checked here, and nothing can be built.</p>`}
      ${error ? `<p class="up-err">${esc(error)}</p>` : ""}
    </section>`);
  $("upClose").onclick = close;
  const drop = $("upDrop");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) ingest(e.dataTransfer.files[0]); };
  $("upFile").onchange = (e) => e.target.files[0] && ingest(e.target.files[0]);
}

async function ingest(file) {
  st = { file, t0: performance.now(), res: null, fixes: new Map() };
  const box = $("upload");
  box.innerHTML = shell(0, `<section class="up-drop reading"><h1>Reading ${esc(file.name)}</h1>
    <p class="muted">${/\.(png|jpe?g|webp|gif|pdf)$/i.test(file.name) ? "Claude is reading the drawing. Usually 10 to 30 seconds." : "Parsing the file."}</p>
    <div class="up-scan"></div></section>`);
  $("upClose").onclick = close;
  tickClock();
  try {
    st.res = BASE ? await postIngest(file) : await localIngest(file);
  } catch (e) {
    clearInterval(timer);
    drawDrop(box, e.message);
    return;
  }
  drawCheck();
}

async function postIngest(file) {
  const fd = new FormData();
  fd.append("file", file);
  const r = await authFetch(`${BASE}/api/ingest`, { method: "POST", body: fd });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `Upload failed: HTTP ${r.status}`);
  return body;
}

// Without a backend only a blueprint JSON can be shown. It is not checked for gaps.
async function localIngest(file) {
  if (!/\.json$/i.test(file.name)) throw new Error("Reading drawings and CSV files needs the backend. Start it and set VITE_API.");
  const bp = JSON.parse(await file.text());
  const counts = Object.fromEntries(KINDS.map(([k]) => [k, (bp[k] || []).length]));
  return { ok: true, errors: [], review: [{ id: "", label: "Offline", issue: "No backend, so nothing checked this file for missing numbers." }],
           counts, source: { file: file.name, read_by: "browser" }, blueprint: bp };
}

// ---------------------------------------------------------------- step 2 ----
// Which fields a review item asks a human for, read from the issue text.
function askedFields(item) {
  const m = item.issue.match(/Missing ([a-z_]+(?:(?:, | and |, and )[a-z_]+)*)/);
  if (m) return m[1].split(/, and |, | and /);
  if (/manual or remote/i.test(item.issue)) return ["manual", "max_bbl_hr"];
  return [];
}

function findAsset(id) {
  for (const [k] of KINDS) for (const a of st.res.blueprint[k] || []) if (a.id === id) return a;
  return null;
}

function reviewDone() {
  return st.res.review.every((item, i) => {
    const f = st.fixes.get(i) || { checked: false, values: {} };
    const fields = askedFields(item);
    if (fields.length) return fields.every((k) => f.values[k] !== undefined && f.values[k] !== "");
    return f.checked;
  });
}

function drawCheck() {
  const { res } = st;
  const flagged = new Set(res.review.map((r) => r.id));
  const rows = [];
  for (const [k, label] of KINDS) {
    for (const a of res.blueprint[k] || []) {
      const ing = a._ingest || {};
      const conf = ing.confidence == null ? null : Math.round(ing.confidence * 100);
      const nums = Object.entries(a).filter(([key, v]) => NUMERIC.has(key) && typeof v === "number").map(([key, v]) => `${key} ${v}`).join(" · ");
      const link = a.tank_id ? `→ ${a.tank_id}` : a.connects ? a.connects.join(" ↔ ") : "";
      rows.push(`<tr class="${flagged.has(a.id) ? "flag" : ""} ${conf != null && conf < 80 ? "low" : ""}">
        <td>${label}</td><td class="id">${esc(a.id)}</td><td>${esc(ing.label && ing.label !== a.id ? ing.label : "")}</td>
        <td class="num">${conf == null ? "" : `${conf}%`}</td><td class="muted">${esc(link)}</td><td class="muted">${esc(nums)}</td></tr>`);
    }
  }
  const review = res.review.map((item, i) => {
    const fields = askedFields(item);
    const f = st.fixes.get(i) || { checked: false, values: {} };
    const inputs = fields.map((k) => k === "manual"
      ? `<label>${k}<select data-i="${i}" data-k="${k}"><option value="">?</option><option value="true" ${f.values[k] === true ? "selected" : ""}>manual (rover)</option><option value="false" ${f.values[k] === false ? "selected" : ""}>remote</option></select></label>`
      : `<label>${k === "minutes_to" ? "minutes to each tank" : k}<input data-i="${i}" data-k="${k}" inputmode="decimal" value="${f.values[k] ?? ""}" placeholder="?"></label>`).join("");
    return `<li class="${fields.length ? "" : "tick"}">
      <div><b>${esc(item.id || item.label)}</b> <span class="muted">${esc(item.label && item.label !== item.id ? item.label : "")}</span></div>
      <div class="issue">${esc(item.issue)}</div>
      ${inputs ? `<div class="fills">${inputs}</div>` : `<label class="ok"><input type="checkbox" data-i="${i}" ${f.checked ? "checked" : ""}> I checked this</label>`}
    </li>`;
  }).join("");
  // "Fill all": a CSV leaves the same number missing on every tank or well. One typed
  // value fills every empty box asking for that field. A person types it; nothing is guessed.
  const asking = new Map();
  res.review.forEach((item, i) => {
    for (const k of askedFields(item)) {
      if (k === "manual") continue;
      if (!asking.has(k)) asking.set(k, []);
      asking.get(k).push(i);
    }
  });
  const fillAll = [...asking].filter(([, list]) => list.length > 1).map(([k, list]) => {
    const empty = list.filter((i) => { const f = st.fixes.get(i); return !f || f.values[k] === undefined || f.values[k] === ""; }).length;
    return `<form class="fill-all" data-k="${k}"><label>${k} <span class="muted">${empty} of ${list.length} empty</span>
      <input inputmode="decimal" placeholder="value" ${empty ? "" : "disabled"}></label><button ${empty ? "" : "disabled"}>Fill all</button></form>`;
  }).join("");
  const counts = KINDS.filter(([k]) => res.counts[k]).map(([k, l]) => `${res.counts[k]} ${l.toLowerCase()}${res.counts[k] > 1 ? "s" : ""}`).join(" · ");
  const blocked = res.errors.length > 0;
  $("upload").innerHTML = shell(1, `<section class="up-check">
      <div class="up-left">
        <div class="kicker">Read from ${esc(res.source.file)} by ${esc(res.source.read_by)}${res.source.model ? ` (${esc(res.source.model)})` : ""}</div>
        <h2>${esc(res.blueprint.name || "Uploaded field")}</h2>
        <p class="muted">${counts}</p>
        <div class="up-table"><table><thead><tr><th>Type</th><th>ID</th><th>On the plan</th><th>Sure</th><th>Link</th><th>Numbers</th></tr></thead>
          <tbody>${rows.join("")}</tbody></table></div>
      </div>
      <aside class="up-right">
        ${blocked ? `<div class="kicker up-err">Can't build: ${res.errors.length} error${res.errors.length > 1 ? "s" : ""}</div>
          <ul class="up-errors">${res.errors.map((e) => `<li>${esc(typeof e === "string" ? e : `${e.id || ""} ${e.issue || e.message || JSON.stringify(e)}`)}</li>`).join("")}</ul>
          <p class="muted">Fix the file and drop it again.</p>` : ""}
        <div class="kicker">For you to check · ${res.review.length}</div>
        ${fillAll ? `<div class="fill-alls">${fillAll}</div>` : ""}
        ${res.review.length ? `<ol class="up-review">${review}</ol>` : `<p class="muted">Nothing. Every asset has the numbers the forecast needs.</p>`}
        <div class="up-actions">
          <button id="upAgain">Different file</button>
          <button class="primary hold" id="upBuild" ${blocked || !reviewDone() || !BASE ? "disabled" : ""}><i></i><span>Hold to build</span></button>
        </div>
        ${!BASE ? `<p class="note">Building needs the backend.</p>` : ""}
      </aside>
    </section>`);
  $("upClose").onclick = close;
  $("upAgain").onclick = () => { clearInterval(timer); drawDrop($("upload")); };
  $("upload").querySelectorAll(".fills input, .fills select").forEach((el) => {
    el.onchange = () => {
      const i = Number(el.dataset.i), k = el.dataset.k;
      const f = st.fixes.get(i) || { checked: false, values: {} };
      if (el.tagName === "SELECT") f.values[k] = el.value === "" ? "" : el.value === "true";
      else f.values[k] = el.value.trim() === "" || isNaN(Number(el.value)) ? "" : Number(el.value);
      st.fixes.set(i, f);
      drawCheck();
    };
  });
  $("upload").querySelectorAll("form.fill-all").forEach((form) => {
    form.onsubmit = (e) => {
      e.preventDefault();
      const k = form.dataset.k, v = Number(form.querySelector("input").value);
      if (form.querySelector("input").value.trim() === "" || isNaN(v)) return;
      for (const i of asking.get(k)) {
        const f = st.fixes.get(i) || { checked: false, values: {} };
        if (f.values[k] === undefined || f.values[k] === "") f.values[k] = v;
        st.fixes.set(i, f);
      }
      drawCheck();
    };
  });
  $("upload").querySelectorAll(".ok input").forEach((el) => {
    el.onchange = () => {
      const i = Number(el.dataset.i);
      const f = st.fixes.get(i) || { checked: false, values: {} };
      f.checked = el.checked;
      st.fixes.set(i, f);
      drawCheck();
    };
  });
  const b = $("upBuild");
  if (!b.disabled) holdButton(b, build, 1200);
}

// ---------------------------------------------------------------- step 3 ----
function fixedBlueprint() {
  const bp = structuredClone(st.res.blueprint);
  st.res.review.forEach((item, i) => {
    const f = st.fixes.get(i);
    if (!f) return;
    const a = (() => { for (const [k] of KINDS) for (const x of bp[k] || []) if (x.id === item.id) return x; return null; })();
    if (!a) return;
    for (const [k, v] of Object.entries(f.values)) {
      if (v === "") continue;
      // A truck's minutes_to is a map {TANK_n: minutes}. One typed number means "that many
      // minutes to every tank"; a value the file already had for a tank is kept.
      if (k === "minutes_to" && typeof v === "number") {
        a.minutes_to = Object.fromEntries((bp.tanks || []).map((tk) => [tk.id, (a.minutes_to || {})[tk.id] ?? v]));
      } else a[k] = v;
    }
  });
  return bp;
}

async function build() {
  const box = $("upload");
  const say = (title, sub, cls = "") => {
    box.innerHTML = shell(2, `<section class="up-drop building ${cls}"><h1>${esc(title)}</h1><p class="muted">${esc(sub)}</p><div class="up-scan"></div></section>`);
    $("upClose").onclick = close;
  };
  say("Building the field", "The generator lays out every asset from the library.");
  try {
    let r = await call("/api/blueprints", { blueprint: fixedBlueprint() });
    for (let n = 0; r.status === "building" && n < 120; n++) {
      say("Building the field", r.note || `${Object.values(r.counts || {}).reduce((x, y) => x + y, 0) || ""} assets placed so far.`);
      await new Promise((ok) => setTimeout(ok, 1000));
      r = await call(`/api/blueprints/${encodeURIComponent(r.id)}`);
    }
    if (r.status !== "ready" && r.status !== "fallback") {
      throw new Error((r.errors || []).map((e) => (typeof e === "string" ? e : e.message || JSON.stringify(e))).join(" ") || `Build ${r.status}.`);
    }
    const secs = ((performance.now() - st.t0) / 1000).toFixed(1);
    try {
      if (r.glb_url) sessionStorage.setItem("freeboard.glb", /^https?:/.test(r.glb_url) ? r.glb_url : `${BASE}${r.glb_url}`);
      sessionStorage.setItem("freeboard.built", JSON.stringify({ name: st.res.blueprint.name || st.file.name, secs, status: r.status, note: r.note || "" }));
    } catch { /* storage blocked: the console still opens on /api/field */ }
    say(r.status === "fallback" ? "Built, on the standard model" : "Field ready", r.note || `${secs} s from drop to field.`, "done");
    clearInterval(timer);
    setTimeout(() => { location.hash = ""; location.reload(); }, 900);
  } catch (e) {
    clearInterval(timer);
    say("Build failed", e.message, "failed");
    const again = document.createElement("button");
    again.textContent = "Back to the check";
    again.onclick = () => { tickClock(); drawCheck(); };
    box.querySelector(".up-drop").appendChild(again);
  }
}

async function call(path, body) {
  const r = await authFetch(`${BASE}${path}`, body === undefined ? undefined
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(out.detail ? (typeof out.detail === "string" ? out.detail : JSON.stringify(out.detail)) : `HTTP ${r.status} from ${path}`);
  return out;
}
