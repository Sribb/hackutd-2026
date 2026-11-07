import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { loadAll, assetIds } from "./api.js";
import { buildPlaceholder } from "./placeholder.js";

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- scene ----
const canvas = $("scene");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0c0f);
scene.fog = new THREE.Fog(0x0a0c0f, 90, 180);

const camera = new THREE.PerspectiveCamera(40, 1, 0.5, 500);
camera.position.set(-6, 46, 62);
const controls = new OrbitControls(camera, canvas);
controls.target.set(4, 0, 2);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.47;
controls.minDistance = 12;
controls.maxDistance = 140;

scene.add(new THREE.HemisphereLight(0xbfc8d6, 0x1a1d22, 0.9));
const moon = new THREE.DirectionalLight(0xdfe6f0, 1.4);
moon.position.set(-30, 60, 25);
moon.castShadow = true;
moon.shadow.mapSize.set(2048, 2048);
Object.assign(moon.shadow.camera, { left: -60, right: 60, top: 60, bottom: -60, far: 200 });
scene.add(moon);

const ground = new THREE.Mesh(new THREE.PlaneGeometry(400, 400), new THREE.MeshStandardMaterial({ color: 0x15181c, roughness: 1 }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
ground.name = "ground";
scene.add(ground);

function resize() {
  const { clientWidth: w, clientHeight: h } = canvas.parentElement;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
addEventListener("resize", resize);
resize();

// ---------------------------------------------------------------- model ----
async function loadModel() {
  try {
    const head = await fetch("/field.glb", { method: "HEAD" });
    if (!head.ok) throw new Error("no field.glb");
    const gltf = await new GLTFLoader().loadAsync("/field.glb");
    return { root: gltf.scene, from: "blender/field.glb" };
  } catch {
    return { root: buildPlaceholder(), from: "placeholder" };
  }
}

// ---------------------------------------------------------- asset index ----
// Maps asset ID -> { root, meshes, levelMesh }. Clicking any child resolves to its asset.
const assets = new Map();
let ids = new Set();

function idFor(obj) {
  // Walk up from the clicked mesh. A node named exactly with an ID wins. Children like
  // PUMP_3_motor, and TANK_3_LEVEL kept outside the tank group, resolve to their asset.
  for (let o = obj; o; o = o.parent) {
    if (ids.has(o.name)) return o.name;
    const m = o.name.match(/^([A-Z]+(?:_[A-Z]+)*_\d+)(?:_.*)?$/);
    if (m && ids.has(m[1])) return m[1];
  }
  return null;
}

function indexModel(root) {
  root.traverse((o) => {
    if (!o.isMesh) return;
    o.castShadow = o.receiveShadow = true;
    const id = idFor(o);
    if (!id) return;
    const a = assets.get(id) || { id, meshes: [], levelMesh: null, root: null };
    if (/_LEVEL$/.test(o.name)) a.levelMesh = o;
    else {
      // Own material per mesh so status color never bleeds into other assets.
      o.material = o.material.clone();
      o.userData.base = { color: o.material.color.clone(), emissive: (o.material.emissive || new THREE.Color()).clone() };
      a.meshes.push(o);
    }
    assets.set(id, a);
  });
  root.traverse((o) => { if (assets.has(o.name)) assets.get(o.name).root = o; });
  for (const a of assets.values()) if (!a.root) a.root = a.meshes[0] || a.levelMesh;
}

// --------------------------------------------------------------- status ----
const COLORS = {
  WARNING: new THREE.Color(0xf0a63a),
  ALARM: new THREE.Color(0xef5a50),
  NO_DATA: new THREE.Color(0xf0a63a),
};

function paint(id, status, t) {
  const a = assets.get(id);
  if (!a) return;
  const pulse = 0.55 + 0.45 * Math.sin(t * 4);
  for (const m of a.meshes) {
    const base = m.userData.base;
    m.material.color.copy(base.color);
    if (m.material.emissive) m.material.emissive.copy(base.emissive);
    if (status === "OFFLINE") m.material.color.multiplyScalar(0.3);
    else if (COLORS[status]) {
      m.material.color.lerp(COLORS[status], status === "ALARM" ? 0.85 : 0.6);
      if (m.material.emissive) m.material.emissive.copy(COLORS[status]).multiplyScalar(status === "ALARM" ? 0.5 * pulse : 0.25);
    }
    if (id === selectedId && m.material.emissive) m.material.emissive.add(new THREE.Color(0x2a2f36));
  }
}

// ------------------------------------------------------------ app state ----
let data = null;
let selectedId = null;
let tMin = 0;
let field = null;

const tankInfo = (id) => (field.tanks || []).find((t) => t.id === id);
const clockAt = (min) => {
  const [h, m] = (data.state.clock || "00:00").split(":").map(Number);
  const x = Math.round(h * 60 + m + min) % 1440, hh = Math.floor(x / 60), mm = x % 60;
  return `${hh % 12 || 12}:${String(mm).padStart(2, "0")} ${hh < 12 ? "AM" : "PM"}`;
};
const nice = (id) => id.replace(/_/g, " ").replace(/\b([A-Z])([A-Z]*)\b/g, (_, a, b) => a + b.toLowerCase()).replace(/\bXo\b/, "XO").replace(/\bSwd\b/, "SWD");

// The view of the field at forecast minute t. At t = 0 it's /api/state as sent.
function viewAt(t) {
  const fc = data.forecast;
  const row = fc.timeline.reduce((best, r) => (r.t_min <= t ? r : best), fc.timeline[0]);
  const shut = new Map(fc.events.filter((e) => e.type === "SHUTDOWN_LEVEL" && e.t_min <= t).map((e) => [e.asset_id, e]));
  const view = {};
  for (const id of ids) {
    const s = data.state.assets[id];
    view[id] = s ? { ...s } : { status: "NO_DATA" };
  }
  if (t > 0) {
    for (const [tid, pct] of Object.entries(row.tanks || {})) {
      if (!view[tid]) continue;
      view[tid].level_pct = pct;
      const info = tankInfo(tid);
      if (info && view[tid].status === "OK" && pct >= info.shutdown_pct - 5) view[tid].status = "WARNING";
    }
    for (const [tid, e] of shut) {
      view[tid].status = "ALARM";
      view[tid].reason = `${e.text}. Its wells are shut in.`;
      for (const w of field.wells || []) {
        if (w.tank_id === tid && view[w.id]) {
          view[w.id] = { ...view[w.id], status: "OFFLINE", reason: `Shut in when ${nice(tid)} reached shutdown at ${clockAt(e.t_min)}.` };
        }
      }
    }
  }
  return view;
}

// ------------------------------------------------------------------- UI ----
function statusTag(s) {
  const label = s === "NO_DATA" ? "NO DATA" : s;
  return `<span class="status ${s}"><i></i>${label}</span>`;
}

function renderIncident() {
  const inc = data.state.incident;
  const box = $("incident");
  if (!inc) { box.hidden = true; return; }
  const ev = data.forecast.events.find((e) => e.type === "SHUTDOWN_LEVEL");
  const wellsAtRisk = ev ? (field.wells || []).filter((w) => w.tank_id === ev.asset_id).length : 0;
  let countdown = "";
  if (ev) {
    const range = ev.t_min_low != null
      ? ` <span class="muted">(${clockAt(ev.t_min_low)} to ${ev.t_min_high == null ? "later" : clockAt(ev.t_min_high)})</span>` : "";
    countdown = `<div class="countdown">${nice(ev.asset_id)} reaches shutdown at ${clockAt(ev.t_min)}${range}</div>
      <div class="sub">${Math.round(ev.t_min)} min from now. ${wellsAtRisk} wells go down with it.</div>`;
  }
  box.hidden = false;
  box.innerHTML = `<div class="kicker">Incident</div>
    <div class="head">${statusTag("ALARM")} <span>${inc.summary}</span></div>${countdown}`;
  box.querySelector(".head").style.cursor = "pointer";
  box.querySelector(".head").onclick = () => select(inc.root_asset_id);
}

function renderSelected(view) {
  const box = $("selected");
  if (!selectedId) { box.innerHTML = `<div class="kicker">Selected</div><div class="empty">Click anything in the field.</div>`; return; }
  const s = view[selectedId] || { status: "NO_DATA" };
  const rows = [];
  const add = (k, v) => v != null && v !== "" && rows.push(`<dt>${k}</dt><dd>${v}</dd>`);
  const tank = tankInfo(selectedId);
  if (tank) {
    add("Level", s.level_pct != null ? `${s.level_pct.toFixed(1)}%` : null);
    add("Shutdown at", `${tank.shutdown_pct}%`);
    add("Capacity", `${tank.capacity_bbl.toLocaleString()} bbl`);
    if (s.level_pct != null) {
      const room = Math.round((tank.capacity_bbl * (tank.shutdown_pct - s.level_pct)) / 100);
      add(room >= 0 ? "Headroom" : "Over shutdown", `${Math.abs(room).toLocaleString()} bbl`);
    }
  }
  const pump = (field.pumps || []).find((p) => p.id === selectedId);
  if (pump) { add("Tank", nice(pump.tank_id)); add("Rated", `${pump.rate_bbl_hr} bbl/hr`); }
  const valve = (field.valves || []).find((v) => v.id === selectedId);
  if (valve) { add("Position", s.position); add("Connects", (valve.connects || []).map(nice).join(" to ")); }
  const well = (field.wells || []).find((w) => w.id === selectedId);
  if (well) { add("Feeds", nice(well.tank_id)); add("Water", `${well.water_bbl_hr} bbl/hr`); add("Lost if down", `$${well.lost_usd_hr}/hr`); }
  const swd = (field.disposal_wells || []).find((d) => d.id === selectedId);
  if (swd) add("Max rate", `${swd.max_bbl_hr} bbl/hr`);
  const truck = (field.trucks || []).find((t) => t.id === selectedId);
  if (truck) {
    add("Capacity", `${truck.capacity_bbl} bbl`);
    add("Location", s.location ? nice(s.location) : null);
    for (const [tid, m] of Object.entries(truck.minutes_to || {})) add(`To ${nice(tid)}`, `${m} min`);
  }
  const reason = s.status === "NO_DATA"
    ? "No reading for this asset. It is not treated as safe."
    : s.reason || "";
  box.innerHTML = `<div class="kicker">Selected</div>
    <div class="asset-id">${selectedId}</div>${statusTag(s.status)}
    ${reason ? `<div class="reason">${reason}</div>` : ""}
    ${rows.length ? `<dl class="kv">${rows.join("")}</dl>` : ""}`;
}

function renderTanks(view) {
  $("tanks").innerHTML = (field.tanks || []).map((t) => {
    const s = view[t.id] || { status: "NO_DATA" };
    const pct = s.level_pct;
    return `<div class="tank-row" data-id="${t.id}">
      <span class="name">${nice(t.id)}</span>
      <div class="bar"><div class="fill ${s.status}" style="width:${pct == null ? 0 : Math.min(100, pct)}%"></div>
        <div class="stop" style="left:${t.shutdown_pct}%"></div></div>
      <span class="pct">${pct == null ? "no data" : pct.toFixed(1) + "%"}</span></div>`;
  }).join("");
  for (const row of $("tanks").querySelectorAll(".tank-row")) row.onclick = () => select(row.dataset.id);
}

function renderMarks() {
  const max = Number($("time").max);
  const marks = [0, 60, 120, 180, 240, 300, 360].map((m) => `<span style="left:${(100 * m) / max}%">${clockAt(m)}</span>`);
  for (const e of data.forecast.events) {
    if (e.t_min_low != null) {
      const hi = e.t_min_high == null ? max : e.t_min_high;
      marks.push(`<div class="range" style="left:${(100 * e.t_min_low) / max}%;width:${(100 * (hi - e.t_min_low)) / max}%"></div>`);
    }
  }
  $("marks").innerHTML = marks.join("");
}

function render() {
  const view = viewAt(tMin);
  $("clock").textContent = clockAt(0); // the real "now" never moves with the slider
  $("tankKicker").textContent = tMin === 0 ? "Tank levels" : `Tank levels at ${clockAt(tMin)} (forecast)`;
  $("timeClock").textContent = clockAt(tMin);
  $("timeRel").textContent = tMin === 0 ? "now" : `+${tMin} min`;
  renderSelected(view);
  renderTanks(view);
  // Tank fill meshes: scale Y from 0 (empty) to 1 (full).
  for (const t of field.tanks || []) {
    const a = assets.get(t.id);
    const pct = view[t.id] && view[t.id].level_pct;
    if (a && a.levelMesh && pct != null) a.levelMesh.scale.y = Math.max(0.001, pct / 100);
  }
  lastView = view;
}
let lastView = {};

function select(id) {
  selectedId = id;
  if (selBox) { scene.remove(selBox); selBox = null; }
  const a = id && assets.get(id);
  if (a && a.root) {
    selBox = new THREE.BoxHelper(a.root, 0xe7e9ec);
    selBox.material.transparent = true;
    selBox.material.opacity = 0.6;
    scene.add(selBox);
  }
  render();
}
let selBox = null;

// --------------------------------------------------------------- picking ----
const ray = new THREE.Raycaster();
const ptr = new THREE.Vector2();
function pick(ev) {
  const r = canvas.getBoundingClientRect();
  ptr.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ptr, camera);
  for (const hit of ray.intersectObjects(scene.children, true)) {
    const id = idFor(hit.object);
    if (id) return id;
  }
  return null;
}
let downAt = null;
canvas.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; });
canvas.addEventListener("pointerup", (e) => {
  // A drag is camera movement, not a click.
  if (downAt && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) < 5) select(pick(e));
  downAt = null;
});
canvas.addEventListener("pointermove", (e) => {
  const id = pick(e);
  const tag = $("hoverTag");
  canvas.classList.toggle("pointing", !!id);
  if (!id) { tag.hidden = true; return; }
  const r = canvas.getBoundingClientRect();
  tag.hidden = false;
  tag.style.left = `${e.clientX - r.left}px`;
  tag.style.top = `${e.clientY - r.top}px`;
  const s = lastView[id] || {};
  tag.textContent = s.status && s.status !== "OK" ? `${id} · ${s.status === "NO_DATA" ? "NO DATA" : s.status}` : id;
});
canvas.addEventListener("pointerleave", () => { $("hoverTag").hidden = true; });

$("time").addEventListener("input", (e) => { tMin = Number(e.target.value); render(); });
addEventListener("keydown", (e) => { if (e.key === "Escape") select(null); });

// Fit the whole field in view, whatever size the Blender model turns out to be.
// The operator should never need to touch the camera (README usability test).
function frame(root) {
  const box = new THREE.Box3().setFromObject(root);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.x, size.z) / 2;
  const fov = THREE.MathUtils.degToRad(camera.fov);
  const fitH = radius / Math.tan(fov / 2);
  const fitW = fitH / Math.min(1, camera.aspect);
  const dist = Math.max(fitH, fitW) * 1.12;
  // Aim a little toward the camera so the field clears the time bar at the bottom.
  controls.target.set(center.x, 0, center.z + size.z * 0.12);
  camera.position.set(center.x, dist * 0.68, center.z + dist * 0.74);
  controls.maxDistance = dist * 2.5;
  scene.fog.near = dist * 1.4;
  scene.fog.far = dist * 3;
  controls.update();
}

// ----------------------------------------------------------------- boot ----
async function boot() {
  const [d, model] = await Promise.all([loadAll(), loadModel()]);
  data = d;
  field = d.field;
  ids = new Set(assetIds(field));
  scene.add(model.root);
  indexModel(model.root);

  const missing = [...ids].filter((id) => !assets.has(id));
  const src = $("source");
  src.textContent = d.source === "live" ? "LIVE API" : "FAKE DATA";
  src.className = "badge" + (d.source === "live" ? "" : " fake");
  const notes = [...(d.forecast.warnings || [])];
  if (d.warning) notes.unshift(d.warning);
  if (missing.length) notes.push(`Not in the model: ${missing.join(", ")}`);
  $("warnings").innerHTML = notes.map((n) => `<div>${n}</div>`).join("");
  const note = $("modelNote");
  note.hidden = false;
  note.textContent = model.from === "placeholder" ? "Placeholder geometry until blender/field.glb lands" : "Model: blender/field.glb";
  $("loading").remove();
  frame(model.root);

  renderIncident();
  renderMarks();
  render();
  window.__freeboard = { assets, select, viewAt: (t) => viewAt(t), missing };
}

const clock = new THREE.Clock();
renderer.setAnimationLoop(() => {
  const t = clock.getElapsedTime();
  controls.update();
  if (data) for (const id of ids) paint(id, (lastView[id] || {}).status || "NO_DATA", t);
  if (selBox) selBox.update();
  renderer.render(scene, camera);
});

boot().catch((e) => {
  $("loading").textContent = `Failed to start: ${e.message}`;
  console.error(e);
});
