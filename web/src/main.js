import "@fontsource/gloock/400.css";
import "@fontsource/azeret-mono/400.css";
import "@fontsource/azeret-mono/600.css";
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import * as api from "./api.js";
import { buildPlaceholder } from "./placeholder.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const usd = (n) => "$" + Math.round(n).toLocaleString("en-US");

// ---------------------------------------------------------------- scene ----
const canvas = $("scene");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.outputColorSpace = THREE.SRGBColorSpace;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x050505);
scene.fog = new THREE.Fog(0x050505, 90, 180);

const camera = new THREE.PerspectiveCamera(40, 1, 0.5, 500);
camera.position.set(-6, 46, 62);
const controls = new OrbitControls(camera, canvas);
controls.target.set(4, 0, 2);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.47;
controls.minDistance = 8;
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
    // field.glb puts the ID in glTF extras, which three.js loads into userData.
    if (o.userData && ids.has(o.userData.asset_id)) return o.userData.asset_id;
    if (ids.has(o.name)) return o.name;
    if (/^(PIPE|STOP|ROAD|PAD|SITE|CAM)_/.test(o.name)) return null; // scenery, never an asset
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
  root.updateMatrixWorld(true);
  root.traverse((o) => {
    if (/^(STOP|ROAD)_/.test(o.name)) waypoints.set(o.name, o.getWorldPosition(new THREE.Vector3()));
    if (assets.has(o.name)) assets.get(o.name).root = o;
    // Moving parts from CONTRACT.md "What's in field.glb".
    const m = o.name.match(/^(PUMP_\d+)_rotor$|^(VALVE_[A-Z0-9_]+?)_wheel$|^(WELL_\d+)_(beam|crank)$/);
    if (m) movers.push({ node: o, id: m[1] || m[2] || m[3], kind: m[1] ? "rotor" : m[2] ? "wheel" : m[4], rest: o.rotation.clone() });
  });
  for (const a of assets.values()) if (!a.root) a.root = a.meshes[0] || a.levelMesh;
  for (const a of assets.values()) {
    if (!/^TRUCK_/.test(a.id) || !a.root) continue;
    a.home = { pos: a.root.position.clone(), quat: a.root.quaternion.clone() };
    // The truck's long axis is its forward axis. Used to point it along the road.
    const size = new THREE.Box3().setFromObject(a.root).getSize(new THREE.Vector3());
    a.forward = size.x >= size.z ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 0, 1);
  }
}

// ---------------------------------------------------------------- trucks ----
// When a what-if sends a truck, the slider drives it on the same timing the forecast
// uses: drive to the tank, load, haul to the nearest disposal well, drive back, repeat.
// Routes follow the STOP_* and ROAD_JCT_* points in field.glb (CONTRACT.md). The
// placeholder model has none, so the truck drives straight there.
const waypoints = new Map();
const LOAD_MIN = 20, HAUL_MIN = 30; // same as the reference forecast until field.json says otherwise

function stopPos(id) {
  if (waypoints.has(`STOP_${id}`)) return waypoints.get(`STOP_${id}`).clone();
  const b = boxOf(id);
  return b ? b.getCenter(new THREE.Vector3()).setY(b.min.y) : null;
}
function route(fromId, toId, fromPos) {
  const a = fromPos || stopPos(fromId), b = stopPos(toId);
  if (!a || !b) return null;
  const ja = waypoints.get(`ROAD_JCT_${fromId}`), jb = waypoints.get(`ROAD_JCT_${toId}`);
  return ja && jb ? [a, ja.clone(), jb.clone(), b] : [a, b];
}
function along(points, frac) {
  const lens = points.slice(1).map((p, i) => p.distanceTo(points[i]));
  let d = Math.max(0, Math.min(1, frac)) * lens.reduce((x, y) => x + y, 0);
  for (let i = 0; i < lens.length; i++) {
    if (d <= lens[i] || i === lens.length - 1) {
      const k = lens[i] ? Math.min(1, d / lens[i]) : 1;
      return { pos: points[i].clone().lerp(points[i + 1], k), dir: points[i + 1].clone().sub(points[i]) };
    }
    d -= lens[i];
  }
  return { pos: points[0].clone(), dir: new THREE.Vector3(1, 0, 0) };
}
function nearestSwd(tankId) {
  const t = stopPos(tankId);
  let best = null, bestD = Infinity;
  for (const d of field.disposal_wells || []) {
    const p = stopPos(d.id);
    if (p && t && p.distanceTo(t) < bestD) { best = d.id; bestD = p.distanceTo(t); }
  }
  return best;
}
// Where a sent truck is at minute t, and what it's doing.
function truckAt(truck, action, t) {
  const a = assets.get(truck.id);
  const drive = (truck.minutes_to || {})[action.target_id];
  if (!a || !a.home || drive == null) return null;
  const home = a.root.parent.localToWorld(a.home.pos.clone());
  const tank = action.target_id, swd = nearestSwd(tank);
  if (t < action.at_min) return { pos: null, text: `Dispatch at ${clockAt(action.at_min)}` };
  let x = t - action.at_min;
  if (x < drive) return { ...along(route("YARD_1", tank, home), x / drive), text: `Driving to ${nice(tank)}, arrives ${clockAt(action.at_min + drive)}` };
  x -= drive;
  for (;;) {
    if (x < LOAD_MIN) return { ...along([stopPos(tank), stopPos(tank)], 1), dir: null, text: `Loading at ${nice(tank)}` };
    x -= LOAD_MIN;
    if (!swd) return { pos: stopPos(tank), text: `At ${nice(tank)}` };
    if (x < HAUL_MIN) return { ...along(route(tank, swd), x / HAUL_MIN), text: `Hauling to ${nice(swd)}` };
    x -= HAUL_MIN;
    if (x < drive) return { ...along(route(swd, tank), x / drive), text: `Back to ${nice(tank)}` };
    x -= drive;
  }
}
const _q = new THREE.Quaternion();
function placeTrucks(view) {
  for (const truck of field.trucks || []) {
    const a = assets.get(truck.id);
    if (!a || !a.home) continue;
    const act = actions.find((x) => x.type === "SEND_TRUCK" && x.asset_id === truck.id);
    const at = act && truckAt(truck, act, tMin);
    if (view[truck.id] && at) view[truck.id] = { ...view[truck.id], location_text: at.text };
    if (!at || !at.pos) { a.root.position.copy(a.home.pos); a.root.quaternion.copy(a.home.quat); continue; }
    a.root.position.copy(a.root.parent.worldToLocal(at.pos.clone()));
    if (at.dir && at.dir.lengthSq() > 1e-6) {
      _q.setFromUnitVectors(a.forward, at.dir.clone().setY(0).normalize());
      a.root.quaternion.copy(a.home.quat).premultiply(_q);
    }
  }
}

// Animated parts: pumps spin while running, pumpjacks rock while producing,
// a valve's handwheel turns while a what-if changes it.
const movers = [];
function animateMovers(dt, t) {
  for (const mv of movers) {
    const s = (lastView[mv.id] || {}).status;
    const running = s !== "ALARM" && s !== "OFFLINE" && s !== "NO_DATA";
    if (mv.kind === "rotor" && running) mv.node.rotation.x += dt * 12;
    if (mv.kind === "crank" && running) mv.node.rotation.x += dt * 1.6;
    if (mv.kind === "beam") mv.node.rotation.x = mv.rest.x + (running ? 0.25 * Math.sin(t * 1.6) : 0);
    if (mv.kind === "wheel" && actions.some((a) => a.asset_id === mv.id)) mv.node.rotation.y += dt * 2;
  }
}

function boxOf(id) {
  const a = assets.get(id);
  return a && a.root ? new THREE.Box3().setFromObject(a.root) : null;
}

// --------------------------------------------------------------- status ----
const COLORS = {
  WARNING: new THREE.Color(0xc9a14a),
  ALARM: new THREE.Color(0xff4b1f),
  NO_DATA: new THREE.Color(0xc9a14a),
};
const TRYING = new THREE.Color(0x58c4e8);
const SELECTED = new THREE.Color(0x2a2f36);

function paint(id, status, t) {
  const a = assets.get(id);
  if (!a) return;
  const pulse = 0.55 + 0.45 * Math.sin(t * 4);
  const trying = actions.some((x) => x.asset_id === id);
  for (const m of a.meshes) {
    const base = m.userData.base;
    m.material.color.copy(base.color);
    if (m.material.emissive) m.material.emissive.copy(base.emissive);
    if (status === "OFFLINE") m.material.color.multiplyScalar(0.3);
    else if (COLORS[status]) {
      m.material.color.lerp(COLORS[status], status === "ALARM" ? 0.85 : 0.6);
      if (m.material.emissive) m.material.emissive.copy(COLORS[status]).multiplyScalar(status === "ALARM" ? 0.5 * pulse : 0.25);
    }
    if (trying && m.material.emissive) m.material.emissive.add(TRYING.clone().multiplyScalar(0.35));
    if (id === selectedId && m.material.emissive) m.material.emissive.add(SELECTED);
  }
}

// ------------------------------------------------------------ app state ----
let data = null;       // { field, state, baseline, lc, plans }
let field = null;
let fc = null;         // the forecast on screen: baseline, or the what-if result
let actions = [];      // the what-if action list, contract shape
let selectedId = null;
let tMin = 0;
let walk = null;       // { plan, steps, radio, i } while walking an approved plan
let whatifNote = "";
let lastView = {};

const LEAD_DEFAULTS = { SET_VALVE_manual: 20, SET_VALVE_remote: 1, CHOKE_WELL: 2, SEND_TRUCK: 5, SET_PUMP: 1 };
const lead = (k) => (field.lead_min && field.lead_min[k] != null ? field.lead_min[k] : LEAD_DEFAULTS[k]);

const tankInfo = (id) => (field.tanks || []).find((t) => t.id === id);
const clockAt = (min) => {
  const [h, m] = (data.state.clock || "00:00").split(":").map(Number);
  const x = Math.round(h * 60 + m + min) % 1440, hh = Math.floor(x / 60), mm = x % 60;
  return `${hh % 12 || 12}:${String(mm).padStart(2, "0")} ${hh < 12 ? "AM" : "PM"}`;
};
const nice = (id) => id.replace(/_/g, " ").replace(/\b([A-Z])([A-Z]*)\b/g, (_, a, b) => a + b.toLowerCase()).replace(/\bXo\b/, "XO").replace(/\bSwd\b/, "SWD");
const shutdownOf = (f) => (f && f.events ? f.events.find((e) => e.type === "SHUTDOWN_LEVEL") : null);

// The tank tonight's incident threatens: the first shutdown in the baseline forecast.
function incidentTank() {
  const ev = shutdownOf(data.baseline);
  if (ev) return ev.asset_id;
  const root = data.state.incident && data.state.incident.root_asset_id;
  const pump = (field.pumps || []).find((p) => p.id === root);
  return pump ? pump.tank_id : null;
}

// What the operator can try on an asset. Each option is one contract action.
function options(id) {
  const tank = incidentTank();
  if (!tank) return [];
  const valve = (field.valves || []).find((v) => v.id === id);
  if (valve && (valve.connects || []).includes(tank) && (data.state.assets[id] || {}).position !== "OPEN") {
    const l = lead(valve.manual === false ? "SET_VALVE_remote" : "SET_VALVE_manual");
    return [{ label: `Open it`, hint: valve.manual === false ? `remote, ${l} min` : `rover drives out, ${l} min`,
              action: { type: "SET_VALVE", asset_id: id, value: "OPEN", at_min: l } }];
  }
  const truck = (field.trucks || []).find((t) => t.id === id);
  if (truck && truck.minutes_to && truck.minutes_to[tank] != null) {
    const l = lead("SEND_TRUCK");
    return [{ label: `Send to ${nice(tank)}`, hint: `arrives in ${l + truck.minutes_to[tank]} min`,
              action: { type: "SEND_TRUCK", asset_id: id, target_id: tank, at_min: l } }];
  }
  const well = (field.wells || []).find((w) => w.id === id);
  if (well && well.tank_id === tank) {
    const l = lead("CHOKE_WELL");
    return [
      { label: "Choke to 50%", hint: `-${well.water_bbl_hr / 2} bbl/hr, $${well.lost_usd_hr / 2}/hr`, action: { type: "CHOKE_WELL", asset_id: id, value: 0.5, at_min: l } },
      { label: "Shut in", hint: `-${well.water_bbl_hr} bbl/hr, $${well.lost_usd_hr}/hr`, action: { type: "CHOKE_WELL", asset_id: id, value: 0, at_min: l } },
    ];
  }
  return [];
}

function describe(a) {
  if (a.type === "SET_VALVE") return `Open ${nice(a.asset_id)}`;
  if (a.type === "SEND_TRUCK") return `Send ${nice(a.asset_id)} to ${nice(a.target_id)}`;
  if (a.type === "CHOKE_WELL") return a.value === 0 ? `Shut in ${nice(a.asset_id)}` : `Choke ${nice(a.asset_id)} to ${Math.round(a.value * 100)}%`;
  if (a.type === "SET_PUMP") return `Turn ${nice(a.asset_id)} ${a.value}`;
  return `${a.type} ${a.asset_id}`;
}

// The view of the field at forecast minute t. At t = 0 it's /api/state as sent.
function viewAt(t) {
  const row = fc.timeline.reduce((best, r) => (r.t_min <= t ? r : best), fc.timeline[0]);
  const shut = new Map(fc.events.filter((e) => e.type === "SHUTDOWN_LEVEL" && e.t_min <= t).map((e) => [e.asset_id, e]));
  const view = {};
  for (const id of ids) {
    const s = data.state.assets[id];
    // A yard has no sensor, so no reading is normal there. Anything else without one is NO DATA.
    view[id] = s ? { ...s } : /^YARD_/.test(id) ? { status: "OK" } : { status: "NO_DATA" };
  }
  for (const a of actions) {
    const v = view[a.asset_id];
    if (!v) continue;
    const live = t >= a.at_min;
    v.trying = live ? `${describe(a)} (what-if, in effect)` : `${describe(a)} (what-if, takes effect at ${clockAt(a.at_min)})`;
    if (a.type === "SET_VALVE" && live) v.position = a.value;
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

// ------------------------------------------------------------- what-if ----
let pending = 0;
async function setActions(next, note = "") {
  const ticket = ++pending;
  const prev = actions;
  actions = next;
  whatifNote = note;
  const out = next.length ? await api.forecast(next) : data.baseline;
  if (ticket !== pending) return; // a newer click won
  if (out.unknown) {
    actions = prev;
    whatifNote = out.warnings[0];
  } else {
    fc = out;
  }
  renderAll();
}

function toggle(opt) {
  const same = actions.find((a) => a.asset_id === opt.action.asset_id);
  const rest = actions.filter((a) => a.asset_id !== opt.action.asset_id);
  const isSame = same && api.actionKey(same) === api.actionKey(opt.action);
  setActions(isSame ? rest : [...rest, opt.action]);
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
  const ev = shutdownOf(data.baseline);
  const wellsAtRisk = ev ? (field.wells || []).filter((w) => w.tank_id === ev.asset_id).length : 0;
  let countdown = "";
  if (ev) {
    const range = ev.t_min_low != null
      ? ` <span class="muted">(${clockAt(ev.t_min_low)} to ${ev.t_min_high == null ? "later" : clockAt(ev.t_min_high)})</span>` : "";
    countdown = `<div class="countdown">${nice(ev.asset_id)} reaches shutdown at ${clockAt(ev.t_min)}${range}</div>
      <div class="sub">${Math.round(ev.t_min)} min from now if nobody acts. ${wellsAtRisk} wells go down with it.</div>`;
  }
  const lc = data.lc;
  let last = "";
  if (lc && lc.status === "INCIDENT" && lc.options && lc.options.length) {
    const best = lc.options[0];
    last = `<div class="lastcall"><b>Last Call:</b> the cheapest plan (${usd(best.cost.total_usd)}) only works if you start by
      <b>${best.expires_clock}</b>.${lc.point_of_no_return ? ` After ${lc.point_of_no_return.clock}, nothing keeps every well online.` : ""}</div>`;
  }
  box.hidden = false;
  box.innerHTML = `<div class="kicker">Incident</div>
    <div class="head">${statusTag("ALARM")} <span>${esc(inc.summary)}</span></div>${countdown}${last}`;
  box.querySelector(".head").style.cursor = "pointer";
  box.querySelector(".head").onclick = () => select(inc.root_asset_id, true);
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
    add("Location", s.location_text || (s.location ? nice(s.location) : null));
    for (const [tid, m] of Object.entries(truck.minutes_to || {})) add(`To ${nice(tid)}`, `${m} min`);
  }
  const fuse = data.lc && data.lc.assets && data.lc.assets[selectedId];
  if (fuse && fuse.useful_until_min != null) add("Can help until", fuse.useful_until_clock);

  const reason = s.status === "NO_DATA" ? "No reading for this asset. It is not treated as safe." : s.reason || "";
  const opts = options(selectedId);
  const current = actions.find((a) => a.asset_id === selectedId);
  const buttons = opts.map((o, i) => {
    const on = current && api.actionKey(current) === api.actionKey(o.action);
    return `<button class="try ${on ? "on" : ""}" data-i="${i}">${on ? "✓ " : ""}${o.label}<span>${o.hint}</span></button>`;
  }).join("");
  box.innerHTML = `<div class="kicker">Selected</div>
    <div class="asset-id">${selectedId}</div>${statusTag(s.status)}
    ${reason ? `<div class="reason">${esc(reason)}</div>` : ""}
    ${s.trying ? `<div class="reason trying">${esc(s.trying)}</div>` : ""}
    ${rows.length ? `<dl class="kv">${rows.join("")}</dl>` : ""}
    ${buttons ? `<div class="tries"><div class="kicker">Try a change</div>${buttons}</div>` : ""}`;
  box.querySelectorAll("button.try").forEach((b) => { b.onclick = () => toggle(opts[Number(b.dataset.i)]); });
}

function renderWhatif() {
  const box = $("whatif");
  if (!actions.length && !whatifNote) { box.hidden = true; return; }
  box.hidden = false;
  const base = shutdownOf(data.baseline);
  const now = shutdownOf(fc);
  let verdict;
  if (!actions.length) verdict = "";
  else if (!now) verdict = `<div class="verdict good">Every tank stays below shutdown through ${clockAt(360)}.</div>`;
  else {
    const gained = base ? Math.round(now.t_min - base.t_min) : 0;
    verdict = `<div class="verdict bad">${nice(now.asset_id)} still reaches shutdown at ${clockAt(now.t_min)}${gained > 0 ? `, ${gained} min later than doing nothing` : ""}.</div>`;
  }
  const c = fc.cost;
  const cost = actions.length && c ? `<dl class="kv">
      <dt>Total</dt><dd><b>${usd(c.total_usd)}</b> <span class="muted">vs ${usd(data.baseline.cost.total_usd)} doing nothing</span></dd>
      <dt>Trucking</dt><dd>${usd(c.trucking_usd || 0)}</dd><dt>Disposal</dt><dd>${usd(c.disposal_usd || 0)}</dd>
      <dt>Lost production</dt><dd>${usd(c.lost_production_usd || 0)}</dd></dl>` : "";
  box.innerHTML = `<div class="kicker">Trying</div>
    ${actions.map((a, i) => `<div class="act"><span>${describe(a)} <span class="muted">at ${clockAt(a.at_min)}</span></span><button data-i="${i}" aria-label="Remove">×</button></div>`).join("")}
    ${verdict}${cost}
    ${whatifNote ? `<div class="note">${esc(whatifNote)}</div>` : ""}
    ${actions.length ? `<button class="link" id="clearAll">Clear all</button>` : ""}`;
  box.querySelectorAll(".act button").forEach((b) => { b.onclick = () => setActions(actions.filter((_, i) => i !== Number(b.dataset.i))); });
  const clear = $("clearAll");
  if (clear) clear.onclick = () => setActions([]);
}

function renderPlans() {
  const box = $("plans");
  const list = (data.plans && data.plans.plans) || [];
  if (!list.length) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<div class="kicker">Cheapest plans</div>` + list.map((p, i) => {
    const stamp = p.expires_clock ? `Start by ${p.expires_clock}${p.robust === false ? " · fails if inflow runs 15% hot" : ""}` : "";
    return `<div class="plan">
      <div class="plan-top"><span class="rank">${i + 1}</span><span class="sum">${esc(p.summary)}</span><b>${usd(p.cost.total_usd)}</b></div>
      ${stamp ? `<div class="stamp ${p.robust === false ? "fragile" : ""}">${stamp}</div>` : ""}
      ${p.keeps_running === false ? `<div class="stamp fragile">Can't save: ${(p.cannot_save || []).map(nice).join(", ")}</div>` : ""}
      <div class="plan-btns"><button data-try="${i}">Try it</button><button class="primary" data-approve="${i}">Approve</button></div>
    </div>`;
  }).join("");
  box.querySelectorAll("[data-try]").forEach((b) => { b.onclick = () => setActions(list[Number(b.dataset.try)].actions.map((a) => ({ ...a }))); });
  box.querySelectorAll("[data-approve]").forEach((b) => { b.onclick = () => startWalk(list[Number(b.dataset.approve)]); });
}

function renderWalk() {
  const box = $("walk");
  if (!walk) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = `<div class="kicker">Approved plan · step ${walk.i + 1} of ${walk.steps.length}</div>
    <ol class="steps">${walk.steps.map((s, i) => `<li class="${i === walk.i ? "on" : i < walk.i ? "done" : ""}" data-i="${i}">${esc(s.text)}</li>`).join("")}</ol>
    <div class="plan-btns">
      <button id="walkBack" ${walk.i === 0 ? "disabled" : ""}>Back</button>
      <button class="primary" id="walkNext">${walk.i === walk.steps.length - 1 ? "Done" : "Next step"}</button>
    </div>
    ${walk.radio ? `<details><summary>Radio script</summary><p>${esc(walk.radio)}</p></details>` : ""}`;
  box.querySelectorAll(".steps li").forEach((li) => { li.onclick = () => goStep(Number(li.dataset.i)); });
  $("walkBack").onclick = () => goStep(walk.i - 1);
  $("walkNext").onclick = () => (walk.i === walk.steps.length - 1 ? endWalk() : goStep(walk.i + 1));
}

async function startWalk(plan) {
  await setActions(plan.actions.map((a) => ({ ...a })));
  const res = await api.approve(plan.id);
  walk = { plan, steps: res.steps || [], radio: res.radio_script || "", i: 0 };
  if (!walk.steps.length) walk = null;
  else goStep(0);
  renderAll();
}
function goStep(i) {
  if (!walk) return;
  walk.i = Math.max(0, Math.min(walk.steps.length - 1, i));
  select(walk.steps[walk.i].asset_id, true);
  renderWalk();
}
function endWalk() { walk = null; renderAll(); }

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
  for (const row of $("tanks").querySelectorAll(".tank-row")) row.onclick = () => select(row.dataset.id, true);
}

function renderMarks() {
  const max = Number($("time").max);
  const marks = [0, 60, 120, 180, 240, 300, 360].map((m) => `<span style="left:${(100 * m) / max}%">${clockAt(m)}</span>`);
  for (const e of fc.events) {
    if (e.t_min_low != null) {
      const hi = e.t_min_high == null ? max : e.t_min_high;
      marks.push(`<div class="range" style="left:${(100 * e.t_min_low) / max}%;width:${(100 * (hi - e.t_min_low)) / max}%"></div>`);
    }
  }
  $("marks").innerHTML = marks.join("");
}

function renderSources() {
  const fakes = Object.entries(api.sources).filter(([, v]) => v === "fake").map(([k]) => k);
  const src = $("source");
  if (!fakes.length) { src.textContent = "LIVE API"; src.className = "badge"; src.title = ""; return; }
  const all = fakes.length === Object.keys(api.sources).length;
  src.textContent = all ? "FAKE DATA" : `LIVE · ${fakes.length} FAKE`;
  src.className = "badge fake";
  src.title = `Fake data for: ${fakes.join(", ")}`;
}

function render() {
  const view = viewAt(tMin);
  placeTrucks(view);
  $("clock").textContent = clockAt(0); // the real "now" never moves with the slider
  $("tankKicker").textContent = tMin === 0 ? "Tank levels" : `Tank levels at ${clockAt(tMin)} (forecast)`;
  $("timeClock").textContent = clockAt(tMin);
  $("timeRel").textContent = tMin === 0 ? "now" : `+${tMin} min`;
  $("timeLabel").textContent = actions.length ? `Forecast with ${actions.length} change${actions.length > 1 ? "s" : ""}` : "Forecast, no action";
  renderSelected(view);
  renderTanks(view);
  // Tank fill meshes: scale Y from 0 (empty) to 1 (full).
  for (const t of field.tanks || []) {
    const a = assets.get(t.id);
    const pct = view[t.id] && view[t.id].level_pct;
    if (a && a.levelMesh && pct != null) a.levelMesh.scale.y = Math.max(0.001, pct / 100);
  }
  updateFuses();
  lastView = view;
}

function renderAll() {
  renderSources();
  renderIncident();
  renderWhatif();
  renderPlans();
  renderWalk();
  renderMarks();
  render();
}

// --------------------------------------------------- Last Call fuse rings ----
// A ring around each asset that can still help. It burns down as the slider moves
// toward the last minute the asset is part of any working plan, then disappears.
const fuses = new Map();
const fuseMat = new THREE.MeshBasicMaterial({ color: 0xf0a63a, transparent: true, opacity: 0.9, side: THREE.DoubleSide, depthWrite: false });
const fuseTrack = new THREE.MeshBasicMaterial({ color: 0xf0a63a, transparent: true, opacity: 0.14, side: THREE.DoubleSide, depthWrite: false });

function buildFuses() {
  const lc = data.lc;
  if (!lc || !lc.assets) return;
  for (const [id, info] of Object.entries(lc.assets)) {
    if (info.useful_until_min == null) continue;
    const box = boxOf(id);
    if (!box) continue;
    const size = box.getSize(new THREE.Vector3());
    const c = box.getCenter(new THREE.Vector3());
    const r = Math.max(size.x, size.z) * 0.55 + 0.9;
    const w = Math.max(0.32, r * 0.09); // stays visible at any model scale
    const track = new THREE.Mesh(new THREE.RingGeometry(r, r + w, 64), fuseTrack);
    const burn = new THREE.Mesh(new THREE.RingGeometry(r, r + w, 64), fuseMat);
    for (const m of [track, burn]) {
      m.rotation.x = -Math.PI / 2;
      m.position.set(c.x, box.min.y + 0.06, c.z);
      m.renderOrder = 2;
      m.name = "fuse";
      scene.add(m);
    }
    fuses.set(id, { track, burn, r, w, until: info.useful_until_min, frac: -1 });
  }
}

function updateFuses() {
  for (const f of fuses.values()) {
    const frac = Math.max(0, Math.min(1, (f.until - tMin) / f.until));
    const visible = frac > 0 && !actions.length;
    f.track.visible = f.burn.visible = visible;
    if (!visible || Math.abs(frac - f.frac) < 1e-3) continue;
    f.frac = frac;
    f.burn.geometry.dispose();
    f.burn.geometry = new THREE.RingGeometry(f.r, f.r + f.w, 64, 1, Math.PI / 2, Math.PI * 2 * frac);
  }
}

// --------------------------------------------------------------- camera ----
let fly = null;
function flyTo(id) {
  const box = boxOf(id);
  if (!box) return;
  const c = box.getCenter(new THREE.Vector3());
  const span = Math.max(8, box.getSize(new THREE.Vector3()).length());
  const dir = camera.position.clone().sub(controls.target).normalize();
  fly = {
    t0: performance.now(), dur: 900,
    fromT: controls.target.clone(), toT: new THREE.Vector3(c.x, 0, c.z),
    fromP: camera.position.clone(), toP: new THREE.Vector3(c.x, 0, c.z).add(dir.multiplyScalar(span * 2.6 + 26)),
  };
}

let home = null;
function frame(root) {
  // Fit the whole field in view, whatever size the Blender model turns out to be.
  // The operator should never need to touch the camera (README usability test).
  // Frame the assets, not the whole site: the model's ground and roads run far past them.
  const box = new THREE.Box3();
  for (const a of assets.values()) if (a.root) box.expandByObject(a.root);
  if (box.isEmpty()) box.setFromObject(root);
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
  controls.minDistance = Math.max(4, radius * 0.05);
  camera.near = Math.max(0.1, dist / 2000);
  camera.far = dist * 6;
  camera.updateProjectionMatrix();
  scene.fog.near = dist * 1.4;
  scene.fog.far = dist * 3;
  // Shadows cover the whole field, whatever its size.
  const half = Math.max(size.x, size.z) * 0.6;
  Object.assign(moon.shadow.camera, { left: -half, right: half, top: half, bottom: -half, near: 1, far: dist * 4 });
  moon.position.set(center.x - half * 0.5, half, center.z + half * 0.4);
  moon.target.position.copy(center);
  scene.add(moon.target);
  moon.shadow.camera.updateProjectionMatrix();
  // Sit under the model's own ground (if it has one) so the two never z-fight.
  const whole = new THREE.Box3().setFromObject(root);
  ground.position.set(center.x, Math.min(box.min.y, whole.min.y) - Math.max(0.05, half * 0.002), center.z);
  ground.scale.setScalar(Math.max(1, (half * 6) / 400));
  controls.update();
  home = { t: controls.target.clone(), p: camera.position.clone() };
}
function flyHome() {
  if (!home) return;
  fly = { t0: performance.now(), dur: 900, fromT: controls.target.clone(), toT: home.t.clone(), fromP: camera.position.clone(), toP: home.p.clone() };
}

// ------------------------------------------------------------ selection ----
let selBox = null;
function select(id, focus = false) {
  selectedId = id;
  if (selBox) { scene.remove(selBox); selBox = null; }
  const a = id && assets.get(id);
  if (a && a.root) {
    selBox = new THREE.BoxHelper(a.root, 0xe7e9ec);
    selBox.material.transparent = true;
    selBox.material.opacity = 0.6;
    scene.add(selBox);
    if (focus) flyTo(id);
  }
  render();
}

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
canvas.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; fly = null; });
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
  const parts = [id];
  if (s.status && s.status !== "OK") parts.push(s.status === "NO_DATA" ? "NO DATA" : s.status);
  const f = fuses.get(id);
  if (f && f.burn.visible) parts.push(`can help until ${data.lc.assets[id].useful_until_clock}`);
  tag.textContent = parts.join(" · ");
});
canvas.addEventListener("pointerleave", () => { $("hoverTag").hidden = true; });

$("time").addEventListener("input", (e) => { tMin = Number(e.target.value); render(); });
addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" && e.target.type !== "range") return;
  if (e.key === "Escape") { if (walk) endWalk(); select(null); flyHome(); }
});

// ----------------------------------------------------------------- boot ----
async function boot() {
  const [f, state, model] = await Promise.all([api.getField(), api.getState(), loadModel()]);
  field = f;
  ids = new Set(api.assetIds(field));
  const [baseline, lc, plans] = await Promise.all([api.forecast([]), api.lastCall(), api.plans()]);
  data = { field, state, baseline, lc, plans };
  fc = baseline;
  scene.add(model.root);
  indexModel(model.root);

  const missing = [...ids].filter((id) => !assets.has(id));
  const notes = [...(baseline.warnings || [])];
  if (api.usingBackend && Object.values(api.sources).includes("fake")) notes.unshift("Some endpoints are not on the backend yet. The badge says which.");
  if (missing.length) notes.push(`Not in the model: ${missing.join(", ")}`);
  $("warnings").innerHTML = notes.map((n) => `<div>${esc(n)}</div>`).join("");
  const note = $("modelNote");
  note.hidden = false;
  note.textContent = model.from === "placeholder" ? "Placeholder geometry until blender/field.glb lands" : "Model: blender/field.glb";
  $("loading").remove();
  frame(model.root);
  buildFuses();
  renderAll();
  window.__freeboard = { assets, select, viewAt: (t) => viewAt(t), missing, setActions, get actions() { return actions; } };
}

const clock = new THREE.Clock();
renderer.setAnimationLoop(() => {
  const dt = clock.getDelta();
  const t = clock.elapsedTime;
  if (fly) {
    const k = Math.min(1, (performance.now() - fly.t0) / fly.dur);
    const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
    controls.target.lerpVectors(fly.fromT, fly.toT, e);
    camera.position.lerpVectors(fly.fromP, fly.toP, e);
    if (k === 1) fly = null;
  }
  controls.update();
  if (data) {
    for (const id of ids) paint(id, (lastView[id] || {}).status || "NO_DATA", t);
    animateMovers(dt, t);
  }
  if (selBox) selBox.update();
  renderer.render(scene, camera);
});

boot().catch((e) => {
  const l = $("loading");
  if (l) l.textContent = `Failed to start: ${e.message}`;
  console.error(e);
});
