import "@fontsource/gloock/400.css";
import "@fontsource/azeret-mono/400.css";
import "@fontsource/azeret-mono/600.css";
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { mountFlow } from "./flow.js";
import { MeshoptDecoder } from "three/addons/libs/meshopt_decoder.module.js";
import { DRACOLoader } from "three/addons/loaders/DRACOLoader.js";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { mountSky, DEFAULT_SITE } from "./sky.js";
import { startSeismic, onQuakes, seismicSummary } from "./seismic.js";
import { startWeather, onWeather, hazards, exposure, remoteOnly, weather, summary as wxSummary } from "./weather.js";
import * as api from "./api.js";
import { phoneToken } from "./auth.js";
import { buildPlaceholder } from "./placeholder.js";
import { decode, holdButton, isHolding, afterHold } from "./fx.js";
import { mountIot3d } from "./iot3d.js";
import * as shiftlog from "./shiftlog.js";
import { mountUpload } from "./upload.js";
import { mountHands } from "./hands.js";
import { mountJarvis, jarvisSelectionChanged } from "./jarvis.js";
import * as liveFeed from "./live.js";
import { mountTwoNights } from "./twonights.js";
import { mountScenery } from "./scenery.js";
import { mountInspect } from "./inspect.js";
import { mountAlarms, alarms } from "./alarms.js";
import { startTrend, trendHtml, wireTrend, frozen } from "./trend.js";
import { mountRuns, runsSupported, runsOn, execute as executeRun, peopleOut } from "./runs.js";
import { mountSecurity, securityLines } from "./security.js";
import "./console/auth.js"; // takes ?token= into auth.js (#104) before anything talks to the backend
import { mountConsole } from "./console/index.js";

// Jarvis console (docs/JARVIS_CONSOLE.md) is the default screen; the old panels stay at ?layout=classic.
const layout = new URLSearchParams(location.search).get("layout") === "classic" ? "classic" : "console";

const $ = (id) => document.getElementById(id);
// Meshopt-compressed glb (#112/#117) loads with no extra files; a plain glb is unaffected.
// Draco (#97, build_field.py --compress): one decoder for every load, fetched from /draco/ only
// when a model uses it. An uncompressed model never touches it.
let draco = null;
const gltfLoader = () => {
  if (!draco) draco = new DRACOLoader().setDecoderPath("/draco/");
  return new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).setDRACOLoader(draco);
};
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

scene.add(new THREE.HemisphereLight(0x8a96a8, 0x0b0b0a, 0.7));
const moon = new THREE.DirectionalLight(0xc8d4e6, 1.25);
moon.position.set(-30, 60, 25);
moon.castShadow = true;
moon.shadow.mapSize.set(2048, 2048);
Object.assign(moon.shadow.camera, { left: -60, right: 60, top: 60, bottom: -60, far: 200 });
// Shadow acne fix (docs/3D_QUALITY.md, method 1): no cost, removes the rings on pads and
// the stripes on tank shells. normalBias scales with the shadow box in frame().
moon.shadow.bias = -0.0005;
moon.shadow.normalBias = 0.04;
scene.add(moon);

const ground = new THREE.Mesh(new THREE.PlaneGeometry(400, 400), new THREE.MeshStandardMaterial({ color: 0x15181c, roughness: 1 }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
ground.name = "ground";
scene.add(ground);
// Day and night (#85): sky, lights, lamps and site brightness follow the field clock plus the slider.
const sky = mountSky({ scene, renderer, camera, key: moon, stage: $("stage"), site: () => field && field.site, offsetMin: () => tMin });

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
  // A field built from an uploaded blueprint (upload.js) wins over the repo's model.
  let built = null;
  try { built = sessionStorage.getItem("freeboard.glb"); } catch { /* storage blocked */ }
  if (built) {
    try {
      const gltf = await gltfLoader().loadAsync(built);
      return { root: gltf.scene, from: "your uploaded blueprint" };
    } catch {
      try { sessionStorage.removeItem("freeboard.glb"); } catch { /* ignore */ }
    }
  }
  try {
    const head = await fetch("/field.glb", { method: "HEAD" });
    if (!head.ok) throw new Error("no field.glb");
    const gltf = await gltfLoader().loadAsync("/field.glb");
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

// Blueprint equipment (#47): separators, flares, compressors... built into the glb but not
// in /api/field. Clickable for its ID and type, never forecast and never shown as OK.
const equipment = new Map();
const EQUIPMENT_NAMES = { SEPARATOR: "Separator", HEATER_TREATER: "Heater treater", FLARE: "Flare", COMPRESSOR: "Compressor",
  LACT: "LACT unit", METER_RUN: "Meter run", OTHER: "Other equipment" };

function indexModel(root) {
  root.traverse((o) => {
    const u = o.userData || {};
    if (u.kind === "equipment" && o.name && !ids.has(o.name)) {
      equipment.set(o.name, { type: u.equipment_type || "OTHER", label: u.label || "", model: u.model || "generic" });
      ids.add(o.name);
    }
  });
  root.traverse((o) => {
    if (!o.isMesh) return;
    o.castShadow = o.receiveShadow = true;
    // Small scenery (grass, rocks, fence, terrain) is marked cast_shadow: false (#97): cheaper shadows.
    for (let n = o; n; n = n.parent) if (n.userData && n.userData.cast_shadow !== undefined) { o.castShadow = n.userData.cast_shadow !== false; break; }
    // Night look: the site itself (ground, roads, pads, scenery) sinks into the dark so
    // the equipment, lamps and status colors carry the scene.
    for (let n = o; n; n = n.parent) {
      if (/^(SITE|PAD|SCENERY)_/.test(n.name)) {
        o.material = o.material.clone();
        o.material.color.multiplyScalar(0.28);
        break;
      }
    }
    const id = idFor(o);
    if (!id) return;
    const a = assets.get(id) || { id, meshes: [], levelMesh: null, root: null };
    if (/_LEVEL$/.test(o.name)) a.levelMesh = o;
    else if (/_(light|beacon|headlights)$/.test(o.name)) {
      // Status lamps from field.glb (CONTRACT.md "Lights"): lit by status, never tinted like a body.
      o.material = o.material.clone();
      (a.lights = a.lights || []).push(o);
    } else {
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
    const m = o.name.match(/^(PUMP_\d+)_rotor$|^(VALVE_[A-Z0-9_]+?)_wheel$|^(WELL_\d+)_(beam|crank|pitman|bridle)$/);
    if (m) movers.push({ node: o, id: m[1] || m[2] || m[3], kind: m[1] ? "rotor" : m[2] ? "wheel" : m[4], rest: o.rotation.clone() });
    const g = o.name.match(/^(TANK_\d+)_gauge$/);
    if (g) movers.push({ node: o, id: g[1], kind: "gauge", rest: o.rotation.clone() });
    const ax = o.name.match(/^(TRUCK_\d+)_axle_\d$/);
    if (ax) movers.push({ node: o, id: ax[1], kind: "axle", rest: o.rotation.clone() });
  });
  for (const a of assets.values()) if (!a.root) a.root = a.meshes[0] || a.levelMesh;
  buildJacks();
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
    if (view[truck.id] && at) view[truck.id] = { ...view[truck.id], location_text: at.text, on_job: !!at.pos, driving: !!(at.pos && at.dir) };
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

// Pumpjack linkage (CONTRACT.md, #121): with <well>_pitman in the model, the crank drives the
// beam through the pitman, a four-bar linkage, so the arms stay on the crank pins and the
// horse head moves the way a real one does (fast down, slow up). Well frame, Blender axes
// (y along the beam toward the motor, z up), in which three.js rotation.x turns the same way:
//   beam pivot O (0, 4.1); equalizer E = O + R(beam) (3.55, -0.24);
//   crank pin P = (2.7 + 0.85 cos c, 1.6 + 0.85 sin c), c = crank rotation.x.
// The pitman's length is |E - P| at the model's rest pose. Without a pitman node (older
// models) the beam keeps its plain rocking.
const JACK = { oy: 0, oz: 4.1, ey: 3.55, ez: -0.24, py: 2.7, pz: 1.6, r: 0.85 };
const jacks = new Map(); // well id -> { beam, crank, pitman, bridle, L }
const rot = (y, z, a) => [y * Math.cos(a) - z * Math.sin(a), y * Math.sin(a) + z * Math.cos(a)];
const equalizer = (a) => { const [y, z] = rot(JACK.ey, JACK.ez, a); return [JACK.oy + y, JACK.oz + z]; };
const crankPin = (c) => [JACK.py + JACK.r * Math.cos(c), JACK.pz + JACK.r * Math.sin(c)];
function buildJacks() {
  jacks.clear();
  const by = new Map();
  for (const mv of movers) if (/^(beam|crank|pitman|bridle)$/.test(mv.kind)) by.set(mv.id, { ...(by.get(mv.id) || {}), [mv.kind]: mv.node });
  for (const [id, j] of by) {
    if (!j.beam || !j.crank || !j.pitman) continue;
    const e = equalizer(j.beam.rotation.x), p = crankPin(j.crank.rotation.x);
    jacks.set(id, { ...j, L: Math.hypot(e[0] - p[0], e[1] - p[1]) });
    // Real pumpjacks on a lease don't move in step: start each crank at its own angle.
    const n = Number((id.match(/\d+/) || [0])[0]);
    j.crank.rotation.x += (n * 2.399) % (2 * Math.PI); // golden angle: spread evenly
  }
}
// The beam angle that puts the equalizer at the pitman's length from the crank pin: the
// intersection of two circles, the one above the pin (the pitman hangs from the beam).
function solveBeam(j) {
  const P = crankPin(j.crank.rotation.x), rE = Math.hypot(JACK.ey, JACK.ez);
  const dy = P[0] - JACK.oy, dz = P[1] - JACK.oz, d = Math.hypot(dy, dz);
  const a = (rE * rE - j.L * j.L + d * d) / (2 * d), h2 = rE * rE - a * a;
  if (!(d > 0) || h2 < 0) return null;
  const h = Math.sqrt(h2), by = JACK.oy + (a * dy) / d, bz = JACK.oz + (a * dz) / d;
  const c1 = [by - (h * dz) / d, bz + (h * dy) / d], c2 = [by + (h * dz) / d, bz - (h * dy) / d];
  const pick = c1[1] >= c2[1] ? c1 : c2;
  return Math.atan2(pick[1] - JACK.oz, pick[0] - JACK.oy) - Math.atan2(JACK.ez, JACK.ey);
}
function animateJack(j) {
  const b = solveBeam(j);
  if (b == null) return;
  j.beam.rotation.x = b;
  const E = equalizer(b), P = crankPin(j.crank.rotation.x);
  j.pitman.rotation.x = Math.atan2(P[0] - E[0], -(P[1] - E[1])) - b; // CONTRACT.md: atan2(dy, -dz) - beam
  if (j.bridle) j.bridle.rotation.x = -b; // hangs plumb over the wellhead
}

function animateMovers(dt, t) {
  for (const mv of movers) {
    if (mv.kind === "pitman" || mv.kind === "bridle") continue;
    if (mv.kind === "beam" && jacks.has(mv.id)) continue; // driven by its crank below
    const s = (lastView[mv.id] || {}).status;
    const running = s !== "ALARM" && s !== "OFFLINE" && s !== "NO_DATA" && s !== "UNKNOWN";
    if (mv.kind === "rotor" && running) mv.node.rotation.x += dt * 12;
    if (mv.kind === "crank" && running) mv.node.rotation.x += dt * 1.6;
    if (mv.kind === "beam") mv.node.rotation.x = mv.rest.x + (running ? 0.25 * Math.sin(t * 1.6) : 0);
    if (mv.kind === "wheel" && actions.some((a) => a.asset_id === mv.id)) mv.node.rotation.y += dt * 2;
    // Float marker on the tank's level board: position.y = 0.3 + 4.3 x level (CONTRACT.md).
    if (mv.kind === "gauge") { const p = (lastView[mv.id] || {}).level_pct; if (p != null) mv.node.position.y = 0.3 + 4.3 * Math.min(1, p / 100); }
    if (mv.kind === "axle" && (lastView[mv.id] || {}).driving) mv.node.rotation.z += dt * 8;
  }
  for (const j of jacks.values()) animateJack(j);
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
  UNKNOWN: new THREE.Color(0x3a3936), // stale sensor: dimmed gray, never the OK look
};
const TRYING = new THREE.Color(0x8fb8c8);
const LAMP = { OK: new THREE.Color(0x8fa36b), WARNING: new THREE.Color(0xc9a14a), ALARM: new THREE.Color(0xff4b1f), NO_DATA: new THREE.Color(0xc9a14a), UNKNOWN: new THREE.Color(0x9a968e) };
const BEACON = new THREE.Color(0xff9a1f);
const SELECTED = new THREE.Color(0x2a2f36);

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
      // A textured part (#127's look pass) multiplies the diffuse color by the texture and the
      // weathering, about 25% darker. State must not get quieter with a nicer model, so the
      // emissive part carries more of it there.
      const gain = m.material.map ? 1.8 : 1;
      if (m.material.emissive) m.material.emissive.copy(COLORS[status]).multiplyScalar((status === "ALARM" ? 0.5 * pulse : 0.25) * gain);
    }
    // The trying (TRYING x 0.35) and selected (SELECTED) lifts now come from highlight.js (#86), after paint().
  }
  for (const l of a.lights || []) {
    const e = l.material.emissive;
    if (!e) continue;
    const v = lastView[id] || {};
    if (/_beacon$/.test(l.name)) e.copy(BEACON).multiplyScalar(v.on_job ? (Math.sin(t * 9) > 0 ? 2.5 : 0.1) : 0);
    else if (/_headlights$/.test(l.name)) e.setScalar(v.on_job ? 1.2 : 0);
    else if (status === "OFFLINE" || !LAMP[status]) e.setScalar(0);
    else e.copy(LAMP[status]).multiplyScalar(status === "ALARM" ? 3 * pulse : status === "NO_DATA" || status === "UNKNOWN" ? (Math.sin(t * (status === "UNKNOWN" ? 1.5 : 3)) > 0 ? 1.2 : 0) : 1.4);
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
let approved = null;   // { plan, steps, done, at } once a plan is held to approve

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
    if (equipment.has(id)) { view[id] = { status: "DISPLAY" }; continue; }
    view[id] = s ? { ...s } : /^YARD_/.test(id) ? { status: "OK" } : { status: "NO_DATA" };
    // Live IoT reading (#54) beats the snapshot. Stale is UNKNOWN, never OK.
    const r = liveFeed.reading(id);
    if (r) {
      view[id] = { ...view[id], status: r.status, live: true, stale: r.stale };
      for (const k of ["position", "running"]) if (r[k] !== undefined) view[id][k] = r[k];
      if (r.level_pct != null) view[id].level_pct = r.level_pct;
      if (r.reason && r.status !== "UNKNOWN") view[id].reason = r.reason;
      if (r.status === "UNKNOWN") {
        const last = r.last_status && r.last_status !== "OK" ? ` Last known: ${r.last_status}${r.level_pct != null ? ` at ${r.level_pct.toFixed(1)}%` : ""}.` : "";
        // The backend's own reason (#38) says which signal went quiet; keep it unless the feed itself is lost.
        view[id].reason = r.reason && !liveFeed.live.lostSince ? `${r.reason}${last}`
          : `No reading for ${ago(liveFeed.staleMinutes(id))}${liveFeed.live.lostSince ? " (live feed lost)" : ""}. Not treated as safe.${last}`;
      }
    }
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
      view[tid].reason = `${String(e.text).replace(/\.$/, "")}. Its wells are shut in.`;
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
async function setActions(next, note = "", quiet = false) {
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
    if (!quiet) logWhatif(next);
  }
  renderAll();
}

// One line in the shift log per what-if result, so the next crew sees what was weighed.
function logWhatif(list) {
  if (!list.length) { shiftlog.add("whatif", "Cleared every what-if. Back to the no-action forecast."); return; }
  const ev = shutdownOf(fc);
  const outcome = ev ? `${nice(ev.asset_id)} still reaches shutdown at ${clockAt(ev.t_min)}` : `every tank holds through ${clockAt(360)}`;
  shiftlog.add("whatif", `Tried ${list.map(describe).join(" + ")}: ${outcome}${fc.cost ? `, ${usd(fc.cost.total_usd)}` : ""}.`);
}

function toggle(opt) {
  const same = actions.find((a) => a.asset_id === opt.action.asset_id);
  const rest = actions.filter((a) => a.asset_id !== opt.action.asset_id);
  const isSame = same && api.actionKey(same) === api.actionKey(opt.action);
  setActions(isSame ? rest : [...rest, opt.action]);
}

// ------------------------------------------------------------------- UI ----
function statusTag(s) {
  const label = s === "NO_DATA" ? "NO DATA" : s === "DISPLAY" ? "NOT MONITORED" : s;
  // (UNKNOWN = a stale live sensor. It renders hatched, never like OK.)
  return `<span class="status ${s}"><i></i>${label}</span>`;
}

// Minutes the shift clock has moved past the clock the forecast was computed at
// (data.state.clock). The forecast is re-read every minute with a backend; offline it isn't,
// so countdowns subtract this to stay in step with the header clock.
// Unwrapped across midnight and past 12 h: once the clock has run ahead, a later reading
// picks the whole-day turn closest to the last answer, so a deadline that passed never reads
// as ahead again. A new forecast clock starts the count over.
let since = { key: "", d: 0 };
function minutesSinceData() {
  const m = /^(\d{1,2}):(\d{2})(?::\d{2})? (AM|PM)$/.exec(shiftlog.clockNow());
  const key = String(data.state.clock || "00:00");
  const [h0, m0] = key.split(":").map(Number);
  if (!m) return 0;
  const now = ((+m[1] % 12) + (m[3] === "PM" ? 12 : 0)) * 60 + +m[2];
  const raw = (((now - (h0 * 60 + m0)) % 1440) + 1440) % 1440;
  let d;
  if (since.key !== key) d = raw > 720 ? 0 : raw; // a clock behind the data (just re-read): nothing to subtract
  else d = Math.max(0, raw + 1440 * Math.round((since.d - raw) / 1440));
  since = { key, d };
  return d;
}

// A forecast minute the shift clock has already passed. Deadlines from an old forecast must
// say they passed, not still read as ahead of the crew.
const passed = (min) => Number.isFinite(min) && minutesSinceData() > min;
const sinceRead = () => `forecast made from the ${clockAt(0)} readings`;
// Where a fresh plan comes from: approving re-scores on the live state when the backend runs plans.
const rescore = () => { const r = runsOn(); return r === true ? "Holding to approve re-scores a plan on the live state first." : r === false ? "Ask Jarvis for a plan from the live state." : ""; };

function renderIncident() {
  const inc = data.state.incident;
  const box = $("incident");
  const ev = shutdownOf(data.baseline);
  // No incident reported but the forecast has a shutdown: never a silent screen.
  if (!inc && !ev) { box.hidden = true; return; }
  const wellsAtRisk = ev ? (field.wells || []).filter((w) => w.tank_id === ev.asset_id).length : 0;
  let countdown = "";
  if (ev) {
    const range = ev.t_min_low != null
      ? ` <span class="muted">(${clockAt(ev.t_min_low)} to ${ev.t_min_high == null ? "later" : clockAt(ev.t_min_high)})</span>` : "";
    const left = Math.max(0, Math.round(ev.t_min - minutesSinceData()));
    const late = Math.round(minutesSinceData() - ev.t_min);
    // data-decode on the headline only: decode() sets textContent, which would flatten the range's styling.
    countdown = `<div class="countdown"><span data-decode="countdown">${nice(ev.asset_id)} reaches shutdown at ${clockAt(ev.t_min)}</span>${range}</div>
      <div class="sub">${left > 0 ? `${left} min from now if nobody acts.` : late >= 2 ? `That time passed ${late} min ago (${sinceRead()}). Check the tank in person.` : "Due now if nobody has acted."} ${wellsAtRisk} wells ${late >= 2 ? "depend on it" : "go down with it"}.</div>`;
  }
  const blind = blindTanks();
  if (blind.length) {
    const names = andList(blind.map(nice));
    countdown += `<div class="verdict warn" data-decode="blind">${ev ? `No reading for ${names}, so the forecast can't vouch for ${blind.length === 1 ? "it" : "them"}. Check in person.` : noShutdownText(blind)}</div>`;
  }
  const lc = data.lc;
  let last = "";
  if (lc && lc.status === "INCIDENT" && lc.options && lc.options.length) {
    const best = lc.options[0];
    const hot = best.robust === false && !best.expires_clock_hot ? " It already falls short if inflow runs 15% high." : "";
    const pnr = lc.point_of_no_return;
    const pnrText = !pnr ? "" : passed(pnr.t_min)
      ? ` The forecast said nothing keeps every well online after ${pnr.clock}, and that time has passed.`
      : ` At the forecast inflow, nothing keeps every well online after ${pnr.clock}.`;
    last = passed(best.expires_min)
      ? `<div class="lastcall"><b>Last Call passed at ${best.expires_clock}:</b> the cheapest plan (${usd(best.cost.total_usd)}) no longer works as planned (${sinceRead()}).${rescore() ? ` ${rescore()}` : ""}${pnrText}</div>`
      : `<div class="lastcall"><b>Last Call:</b> the cheapest plan (${usd(best.cost.total_usd)}) only works if you start by
      <b>${best.expires_clock}</b>.${hot}${pnrText}</div>`;
  }
  box.hidden = false;
  box.innerHTML = `<div class="kicker">Incident</div>
    <div class="head">${statusTag(inc ? "ALARM" : "WARNING")} <span>${inc ? esc(inc.summary) : `No incident reported, but the forecast has ${esc(nice(ev.asset_id))} reaching shutdown.`}</span></div>${countdown}${last}`;
  box.querySelector(".head").style.cursor = "pointer";
  box.querySelector(".head").onclick = () => select(inc ? inc.root_asset_id : ev.asset_id, true);
  decode(box);
}

function renderSelected(view) {
  const box = $("selected");
  if (!selectedId) { box.innerHTML = `<div class="kicker">Selected</div><div class="empty">Click anything in the field.</div>`; return; }
  const s = view[selectedId] || { status: "NO_DATA" };
  const rows = [];
  const add = (k, v) => v != null && v !== "" && rows.push(`<dt>${k}</dt><dd>${v}</dd>`);
  const eq = equipment.get(selectedId);
  if (eq) {
    add("Type", esc(EQUIPMENT_NAMES[eq.type] || eq.type.replace(/_/g, " ").toLowerCase()));
    add("Label", eq.label ? esc(eq.label) : null);
    if (eq.model === "placeholder") add("Model", "Placeholder box. No 3D model for this type yet.");
  }
  const tank = tankInfo(selectedId);
  if (tank) {
    const age = tMin === 0 ? liveFeed.ageText(selectedId) : null;
    add("Level", s.level_pct != null ? `${s.level_pct.toFixed(1)}%${tMin === 0 ? ` <span class="age">${age || `snapshot${dataAt && dataAt !== "boot" ? ` ${esc(dataAt)}` : ""}`}</span>` : ` <span class="age">forecast</span>`}` : null);
    add("Shutdown at", `${tank.shutdown_pct}%`);
    add("Capacity", `${tank.capacity_bbl.toLocaleString()} bbl`);
    if (s.level_pct != null) {
      const room = Math.round((tank.capacity_bbl * (tank.shutdown_pct - s.level_pct)) / 100);
      add(room >= 0 ? "Headroom" : "Over shutdown", `${Math.abs(room).toLocaleString()} bbl`);
    }
  }
  // CF-3: past and forecast on one line. A what-if moves only the dashed part.
  const trendOpts = tank && { id: selectedId, fc, shutdownPct: tank.shutdown_pct, fcClock: (fc && fc.clock) || data.state.clock, sliderMin: tMin, blind: blindTanks().includes(selectedId) };
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

  const reason = eq ? "Display only. No sensor or forecast covers this equipment, so the console makes no claim about it."
    : s.status === "NO_DATA" ? "No reading for this asset. It is not treated as safe." : s.reason || "";
  const opts = options(selectedId);
  const current = actions.find((a) => a.asset_id === selectedId);
  const buttons = opts.map((o, i) => {
    const on = current && api.actionKey(current) === api.actionKey(o.action);
    return `<button class="try ${on ? "on" : ""}" data-i="${i}">${on ? "✓ " : ""}${o.label}<span>${o.hint}</span></button>`;
  }).join("");
  box.innerHTML = `<div class="kicker">Selected</div>
    <div class="asset-id" data-decode="asset">${selectedId}</div>${statusTag(s.status)}
    ${reason ? `<div class="reason">${esc(reason)}</div>` : ""}
    ${s.trying ? `<div class="reason trying">${esc(s.trying)}</div>` : ""}
    ${rows.length ? `<dl class="kv">${rows.join("")}</dl>` : ""}
    ${tank ? trendHtml(trendOpts) : ""}
    ${buttons ? `<div class="tries"><div class="kicker">Try a change</div>${buttons}</div>` : ""}`;
  box.querySelectorAll("button.try").forEach((b) => { b.onclick = () => toggle(opts[Number(b.dataset.i)]); });
  if (tank) wireTrend(box, trendOpts);
  decode(box);
}

// No fresh reading: NO_DATA, UNKNOWN, or a stale reading kept in ALARM (an alarm is never
// hidden by a lost signal, but the number under it is still old).
const isBlind = (v) => !!v && (v.status === "NO_DATA" || v.status === "UNKNOWN" || v.stale === true);

// Tanks whose gauge reads exactly the same value for 10+ live minutes (trend.js frozen()).
const frozenLogged = new Set();
function frozenTanks() {
  return (field.tanks || []).map((t) => t.id).filter((id) => {
    const f = frozen(id);
    if (f && !frozenLogged.has(id)) {
      frozenLogged.add(id);
      shiftlog.add("alarm", `${liveFeed.gaugeDrill() === id ? "DRILL · " : ""}${nice(id)} gauge has read exactly ${f.v.toFixed(2)}% for ${f.minutes} min. A working gauge jitters; this one may be stuck. Check the level in person.`);
      setTimeout(renderLog, 0);
    }
    if (!f) frozenLogged.delete(id);
    return !!f;
  });
}

// Tanks with no level reading right now, or a gauge that may be stuck. The console never vouches for them.
function blindTanks() {
  const frozenNow = frozenTanks();
  return (field.tanks || []).map((t) => t.id).filter((id) => {
    const s = (data.state.assets || {})[id], v = lastView[id];
    return !s || s.level_pct == null || isBlind(v) || frozenNow.includes(id);
  });
}

const andList = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

// What the forecast can honestly say when no tank reaches shutdown.
function noShutdownText(blind) {
  const until = clockAt(360);
  if (!blind.length) return `Every tank stays below shutdown through ${until}.`;
  const names = andList(blind.map(nice));
  if (blind.length === (field.tanks || []).length) return `No tank has a level reading, so the console can't vouch for any of them. Check ${names} in person.`;
  return `Every tank with a reading stays below shutdown through ${until}. No reading for ${names}, so the console can't vouch for ${blind.length === 1 ? "that tank" : "those tanks"}. Check in person.`;
}

// The plan on the list with exactly these commands (timing aside), if any.
function planFor(acts) {
  const k = (xs) => xs.map(api.actionKey).sort().join("|");
  const want = k(acts || []);
  return want ? ((data.plans && data.plans.plans) || []).find((p) => k(p.actions || []) === want) || null : null;
}

function renderWhatif() {
  const box = $("whatif");
  if (!actions.length && !whatifNote) { box.hidden = true; return; }
  box.hidden = false;
  const base = shutdownOf(data.baseline);
  const now = shutdownOf(fc);
  let verdict;
  if (!actions.length) verdict = "";
  else if (!now) {
    const blind = blindTanks();
    // A plan the planner calls fragile holds only at the forecast inflow: never plain green.
    // Only a plan the planner checked at +15% inflow gets plain green. Fragile: amber, and says so.
    // Not checked (a hand-made what-if): says it holds at the forecast inflow.
    const robust = (planFor(actions) || {}).robust;
    // Every tank blind: the console vouches for nothing, so no inflow clause either.
    const allBlind = blind.length && blind.length === (field.tanks || []).length;
    const [first, ...rest] = noShutdownText(blind).split(/(?<=\.) /);
    const clause = allBlind || robust === true ? "" : robust === false ? " Not if inflow runs 15% high." : " Not checked at +15% inflow.";
    const text = allBlind ? noShutdownText(blind) : `${robust === true ? first : first.replace(/\.$/, " at the forecast inflow.")}${clause}${rest.length ? ` ${rest.join(" ")}` : ""}`;
    verdict = `<div class="verdict ${blind.length || robust === false ? "warn" : "good"}" data-decode="verdict">${text}</div>`;
  }
  else {
    const gained = base ? Math.round(now.t_min - base.t_min) : 0;
    verdict = `<div class="verdict bad" data-decode="verdict">${nice(now.asset_id)} still reaches shutdown at ${clockAt(now.t_min)}${gained > 0 ? `, ${gained} min later than doing nothing` : ""}.</div>`;
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
  decode(box);
  const clear = $("clearAll");
  if (clear) clear.onclick = () => setActions([]);
}

// Last Call's deadline for a plan: the option with the same commands (type and asset) and
// the same cost. Plans from /api/plans don't carry expires_clock themselves.
function deadlineFor(p) {
  if (p.expires_clock) return { clock: p.expires_clock, hot: p.expires_clock_hot, min: p.expires_min };
  const k = (xs) => xs.map((a) => `${a.type}:${a.asset_id}`).sort().join("|");
  const want = k(p.actions || []);
  const o = ((data.lc && data.lc.options) || []).find((x) => x.expires_clock && x.cost && p.cost
    && Math.round(x.cost.total_usd) === Math.round(p.cost.total_usd) && k(x.steps || []) === want);
  return o ? { clock: o.expires_clock, hot: o.expires_clock_hot, min: o.expires_min } : null;
}
const firstSentence = (t) => { const m = /^(.+?\.)(\s|$)/.exec(String(t || "")); return m ? m[1] : String(t || ""); };
let allPlans = false; // the plan list shows the cheapest and the cheapest robust until asked

function renderPlans() {
  // Never swap a Hold to approve button out from under a finger (storm search, weather).
  if (isHolding()) { afterHold(renderPlans); return; }
  const box = $("plans");
  const list = (data.plans && data.plans.plans) || [];
  if (!list.length) { box.hidden = true; return; }
  box.hidden = false;
  // Storm check (weather.js): who each plan sends outside, weighed against the real weather.
  const hz = hazards();
  const safe = hz.length ? remoteOnly(list, field) : null;
  // Nothing flagged: one line, details on hover. A hazard (or weather not checked) gets the full block.
  const quiet = !hz.length && weather().status === "ok" && !/not checked|last checked/.test(wxSummary());
  const wx = quiet ? `<div class="wx quiet" title="${esc(wxSummary())}"><span class="wx-k">Storm check</span> ${weather().drill ? "DRILL · " : ""}no NWS flags, 4 h</div>`
    : `<div class="wx ${hz.length ? "hot" : ""}" title="${esc(hz.map((h) => h.why).join(" "))}"><span class="wx-k">Storm check</span> ${esc(wxSummary())}
    ${hz.length ? `<div class="wx-pick">${safe ? `Keeps everyone inside: plan ${safe.i + 1} (${usd(safe.p.cost.total_usd)}), remote only.`
      : storm && storm.plan ? `Keeps everyone inside: ${esc(storm.plan.summary)} ${usd(storm.plan.cost.total_usd)}, checked with the forecast${data.baseline && data.baseline.cost ? ` (doing nothing: ${usd(data.baseline.cost.total_usd)})` : ""}. <button class="link" data-storm>Try it</button>`
      : storm && storm.busy ? "Looking for a plan that keeps everyone inside…"
      : storm && storm.none ? `No remote-only fix keeps every tank below shutdown (${esc(storm.none)}). Weigh the shutdown against sending people out.`
      : "No plan on the list keeps everyone inside. Weigh the shutdown against sending people out."}</div>` : ""}</div>`;
  // Short list: the cheapest, the cheapest that holds at +15% inflow, and the remote-only
  // plan when the weather calls for it. The rest behind one link.
  const robustI = list.findIndex((p) => p.robust === true && p.keeps_running !== false);
  const show = new Set([0, robustI, safe ? safe.i : -1, approved ? list.findIndex((p) => p.id === approved.plan.id) : -1].filter((i) => i >= 0));
  const hidden = allPlans ? 0 : list.length - show.size;
  // Seismic check (seismic.js): quakes near the lease, for disposal capacity. Context, not an alarm.
  const sq = seismicSummary(undefined, (field.disposal_wells || []).map((d) => nice(d.id)));
  const quake = `<div class="wx ${sq.flag ? "hot" : "quiet"}" title="${esc(sq.detail)}"><span class="wx-k">Seismic check</span> ${esc(sq.line)}${sq.flag ? `<div class="wx-pick">${esc(sq.detail)}</div>` : ""}</div>`;
  box.innerHTML = `<div class="kicker">Cheapest plans</div>${wx}${quake}` + list.map((p, i) => {
    if (!allPlans && !show.has(i)) return "";
    const dl = deadlineFor(p);
    const hold = p.robust === true ? "Holds even if inflow runs 15% high" : p.robust === false ? "Holds at the forecast inflow, not at +15%" : "";
    const stamp = [dl ? (passed(dl.min) ? `Start-by time ${dl.clock} has passed${runsOn() === true ? ": approving re-scores it first" : ""}` : `Start by ${dl.clock}`) : "", hold].filter(Boolean).join(" · ");
    // An approved plan whose run was cancelled or failed can be approved again.
    const mine = approved && approved.plan.id === p.id && !approved.ended;
    const endedHere = approved && approved.plan.id === p.id && approved.ended;
    const out = exposure(p, field);
    const roles = {};
    out.forEach((o) => { roles[o.who] = (roles[o.who] || 0) + 1; });
    const who = out.length ? `${out.length} go${out.length === 1 ? "es" : ""} out: ${Object.entries(roles).map(([w, n]) => (n > 1 ? `${n} ${w.replace("truck driver", "truck drivers").replace("rover at the valve", "rovers")}` : w.replace(" at the valve", ""))).join(", ")}` : "Remote only: nobody goes out";
    const risky = hz.length && out.length;
    return `<div class="plan">
      <div class="plan-top"><span class="rank">${i + 1}</span><span class="sum" title="${esc(p.summary)}">${esc(firstSentence(p.summary))}</span><b>${usd(p.cost.total_usd)}</b></div>
      ${stamp ? `<div class="stamp ${p.robust === false ? "fragile" : ""}">${stamp}</div>` : ""}
      ${p.keeps_running === false ? `<div class="stamp fragile">Can't save: ${(p.cannot_save || []).map(nice).join(", ")}</div>` : ""}
      <div class="stamp who ${risky ? "fragile" : hz.length ? "safe" : ""}">${esc(who)}${risky ? ` · in ${esc(hz.map((h) => h.key).join(", "))}` : ""}</div>
      ${endedHere ? `<div class="stamp fragile">Run ${esc(approved.ended.toLowerCase())} at ${approved.done} of ${approved.steps.length} done</div>` : ""}
      ${mine ? `<div class="approved-stamp">Approved ${esc(approved.at)} · ${approved.done} of ${approved.steps.length} done</div>`
        : `<div class="plan-btns"><button data-try="${i}">Try it</button><button class="primary hold" data-approve="${i}" title="Press and hold to approve"><i></i><span>${approved && !approved.ended ? "Hold to approve instead" : risky ? `Hold to approve · ${out.length} out` : "Hold to approve"}</span></button></div>`}
    </div>`;
  }).join("") + (list.length > show.size ? `<button class="link more-plans" data-more>${allPlans ? "Fewer plans ▴" : `${hidden} more plan${hidden === 1 ? "" : "s"} ▸`}</button>` : "");
  const more = box.querySelector("[data-more]");
  if (more) more.onclick = () => { allPlans = !allPlans; renderPlans(); };
  box.querySelectorAll("[data-try]").forEach((b) => { b.onclick = () => setActions(list[Number(b.dataset.try)].actions.map((a) => ({ ...a }))); });
  const sb = box.querySelector("[data-storm]");
  if (sb) sb.onclick = () => setActions(storm.plan.actions.map((a) => ({ ...a })));
  if (hz.length && !safe && !storm) findStormPlan();
  box.querySelectorAll("[data-approve]").forEach((b) => holdButton(b, () => startWalk(list[Number(b.dataset.approve)])));
}

// When the weather makes on-site work risky and no listed plan is remote only, look for one
// with the real forecast: shut in the threatened tank's wells from the console, the most water
// per dollar of lost oil first, until no tank reaches shutdown. Only against a live forecast:
// fake data would make up the answer. A what-if, not an approval: the planner owns approvals.
let storm = null; // { busy } | { plan } | { none: why }
async function findStormPlan() {
  if (storm) return;
  storm = { busy: true };
  const done = (x) => { storm = x; renderPlans(); };
  if (api.sources.forecast !== "live") return done({ none: "needs the live forecast" });
  const tank = incidentTank();
  const wells = (field.wells || []).filter((w) => w.tank_id === tank)
    .sort((a, b) => b.water_bbl_hr / (b.lost_usd_hr || 1) - a.water_bbl_hr / (a.lost_usd_hr || 1));
  const at = lead("CHOKE_WELL");
  for (let k = 1; k <= wells.length; k++) {
    for (const value of [0.5, 0]) {
      const acts = wells.slice(0, k).map((w) => ({ type: "CHOKE_WELL", asset_id: w.id, value, at_min: at }));
      let f;
      try { f = await api.forecast(acts); } catch { return done({ none: "the forecast didn't answer" }); }
      if (f.unknown) return done({ none: "the forecast didn't answer" });
      if (!shutdownOf(f)) {
        const names = andList(wells.slice(0, k).map((w) => nice(w.id)));
        return done({ plan: { summary: `${value === 0 ? "Shut in" : "Choke to 50%"} ${names} from the console. ${noShutdownText(blindTanks())}`, actions: acts, cost: f.cost || { total_usd: 0 } } });
      }
    }
  }
  done({ none: `even shutting in every well on ${nice(tank || "the tank")} isn't enough` });
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
  $("walkNext").onclick = () => (walk.i === walk.steps.length - 1 ? endWalk(true) : goStep(walk.i + 1));
}

async function startWalk(plan) {
  await setActions(plan.actions.map((a) => ({ ...a })));
  // A backend with plan runs (#75) carries the plan out itself: each step becomes a proposal
  // that waits for a hold-to-confirm in the run panel. The static walk stays for older backends.
  if (await runsSupported()) {
    walk = null;
    const r = await executeRun(plan);
    // runs.js logged the approval; keep the full plan (actions, cost) for the handoff.
    if (r && approved && approved.run === r.id && r.plan_id === plan.id) approved.plan = plan;
    renderAll();
    $("run").scrollIntoView({ block: "nearest", behavior: "smooth" });
    return;
  }
  let res;
  try {
    res = await api.approve(plan.id);
  } catch (e) {
    // Locked (401/403) or backend down: say so where the operator is looking. Nothing was approved.
    whatifNote = `Not approved: ${e.status === 401 || /401|PIN/.test(e.message) ? "control is locked. Enter the operator PIN, or open the console with its #k= link." : e.message}`;
    renderAll();
    return;
  }
  walk = { plan, steps: res.steps || [], radio: res.radio_script || "", i: 0 };
  approved = { plan, steps: walk.steps, done: 0, at: shiftlog.clockNow() };
  shiftlog.add("approve", `APPROVED ${plan.summary} (${usd(plan.cost.total_usd)})${plan.expires_clock ? `, start by ${plan.expires_clock}` : ""}.`);
  if (!walk.steps.length) walk = null;
  else goStep(0);
  renderAll();
  if (walk) $("walk").scrollIntoView({ block: "nearest", behavior: "smooth" });
}
function goStep(i) {
  if (!walk) return;
  const next = Math.max(0, Math.min(walk.steps.length - 1, i));
  if (next > walk.i && approved) {
    for (let k = walk.i; k < next; k++) if (k >= approved.done) shiftlog.add("step", `Done: ${walk.steps[k].text}`);
    approved.done = Math.max(approved.done, next);
  }
  walk.i = next;
  select(walk.steps[walk.i].asset_id, true);
  renderWalk();
  renderLog();
}
function endWalk(finished = false) {
  if (finished && walk && approved) {
    shiftlog.add("step", `Done: ${walk.steps[walk.i].text}`);
    approved.done = walk.steps.length;
    shiftlog.add("approve", "Every step of the approved plan is done.");
  }
  walk = null;
  renderAll();
}

function renderTanks(view) {
  $("tanks").innerHTML = (field.tanks || []).map((t) => {
    const s = view[t.id] || { status: "NO_DATA" };
    const pct = s.level_pct;
    const fz = tMin === 0 && !!frozen(t.id); // gauge may be stuck: never shown as a live number
    return `<div class="tank-row" data-id="${t.id}">
      <span class="name">${nice(t.id)}</span>
      <div class="bar"><div class="fill ${s.status}" style="width:${pct == null ? 0 : Math.min(100, pct)}%"></div>
        <div class="stop" style="left:${t.shutdown_pct}%"></div></div>
      <span class="pct ${isBlind(s) || fz ? "stale" : ""}">${pct == null ? "no data" : `${isBlind(s) ? "last " : ""}${pct.toFixed(1)}%`}${fz ? `<small class="age">stuck?</small>` : tMin === 0 && liveFeed.ageText(t.id) ? `<small class="age">${liveFeed.ageText(t.id)}</small>` : ""}</span></div>`;
  }).join("");
  const blind = (field.tanks || []).filter((t) => isBlind(view[t.id]) && (view[t.id] || {}).status !== "NO_DATA");
  const stuck = tMin === 0 ? frozenTanks().filter((id) => !blind.some((t) => t.id === id)) : [];
  if (blind.length || stuck.length) {
    $("tanks").insertAdjacentHTML("beforeend", `<div class="vouch">${[
      ...blind.map((t) => `Can't vouch for ${nice(t.id)}: no reading for ${ago(liveFeed.staleMinutes(t.id))}. Its forecast starts from the last value we had.`),
      ...stuck.map((id) => { const f = frozen(id); return `${liveFeed.gaugeDrill() === id ? "DRILL · " : ""}Can't vouch for ${nice(id)}: its gauge has read exactly ${f.v.toFixed(2)}% for ${f.minutes} min. A working gauge jitters; this one may be stuck. Check the level in person.`; }),
    ].join("<br>")}</div>`);
  }
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
  // The two times the operator plans around, as ticks on the slider: Last Call (start the
  // cheapest plan by then) and the shutdown if nobody acts.
  const tick = (m, cls, label) => (m != null && m >= 0 && m <= max ? `<i class="tick ${cls}" style="left:${(100 * m) / max}%" title="${esc(label)}"></i>` : "");
  const lcBest = data.lc && data.lc.status === "INCIDENT" && data.lc.options && data.lc.options[0];
  if (lcBest && typeof lcBest.expires_min === "number") marks.push(tick(lcBest.expires_min, "lc", `Last Call ${lcBest.expires_clock}: start the cheapest plan by then`));
  const sd = shutdownOf(data.baseline);
  if (sd) marks.push(tick(sd.t_min, "sd", `${nice(sd.asset_id)} reaches shutdown at ${clockAt(sd.t_min)} if nobody acts`));
  $("marks").innerHTML = marks.join("");
}

function renderSources() {
  const fakes = Object.entries(api.sources).filter(([, v]) => v === "fake").map(([k]) => k);
  const down = Object.entries(api.sources).filter(([, v]) => v === "down").map(([k]) => k);
  const src = $("source");
  if (down.length) { src.textContent = `BACKEND DOWN · ${down.join(", ")}`; src.className = "badge down"; src.title = "These calls failed. Their last values are not refreshed."; return; }
  if (!fakes.length) { src.textContent = "LIVE API"; src.className = "badge"; src.title = ""; return; }
  const all = fakes.length === Object.keys(api.sources).length;
  src.textContent = all ? "FAKE DATA" : `LIVE · ${fakes.length} FAKE`;
  src.className = "badge fake";
  src.title = `Fake data for: ${fakes.join(", ")}`;
}

function render() {
  const view = viewAt(tMin);
  placeTrucks(view);
  $("clock").textContent = shiftlog.clockNow(true); // the real "now" never moves with the slider
  // Slider minute 0 is when the forecast was computed. Once the shift clock has moved on,
  // calling it "now" would put two different times on screen under the same word.
  const behind = minutesSinceData();
  const liveLevels = liveFeed.live.available && !liveFeed.live.lostSince;
  $("tankKicker").textContent = tMin === 0 ? (liveLevels ? "Tank levels" : `Tank levels · as of ${clockAt(0)}`) : `Tank levels at ${clockAt(tMin)} (forecast)`;
  $("timeClock").textContent = clockAt(tMin);
  $("timeRel").textContent = tMin === 0 ? (behind >= 1 ? "forecast start" : "now") : `+${tMin} min`;
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

const ago = (m) => (m == null ? "an unknown time" : m < 1 ? "under a minute" : `${m} min`);

// Live feed badge: connectors, and a loud "live feed lost" with how long ago.
function renderFeed() {
  const el = $("feed");
  if (!el) return;
  const L = liveFeed.live;
  if (!api.usingBackend || !L.available) { el.hidden = true; return; }
  el.hidden = false;
  if (L.lostSince) {
    const s = Math.floor((Date.now() - L.lostSince) / 1000);
    el.className = "feed lost";
    el.innerHTML = `<b>Live feed lost</b> ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} ago. Every value is stale.`;
    return;
  }
  el.className = "feed";
  el.innerHTML = `<span class="muted">Live</span> ${liveFeed.connectorBadge().map((c) => `<span class="conn ${c.ok ? "ok" : "down"}">${c.kind}${c.ok ? "" : " unavailable"}</span>`).join(" ")}${L.connected ? "" : ` <span class="muted">(polling)</span>`}`;
}

function renderAll() {
  renderFeed();
  renderLog();
  renderSources();
  renderIncident();
  renderWhatif();
  renderPlans();
  renderWalk();
  renderMarks();
  render();
}

// ------------------------------------------------------- shift handoff ----
// Every what-if, approval and finished step is stamped with the shift clock. The
// handoff note is what the 6 AM crew reads: incident, plan, steps, forecast, log.
function renderLog() {
  const box = $("log");
  const items = shiftlog.entries();
  // Keep a half-typed note (and its focus) across redraws: alarms and runs log while you type.
  const oldIn = $("noteIn");
  const draft = oldIn ? oldIn.value : "";
  const typing = oldIn && document.activeElement === oldIn;
  box.innerHTML = `<div class="kicker">Shift log</div>
    <ol class="log">${items.slice().reverse().map((e) => `<li class="${e.kind}"><time>${e.at}</time><span>${esc(e.text)}</span></li>`).join("") || `<li class="empty">Nothing yet. What you try and approve lands here.</li>`}</ol>
    <form id="noteForm" class="note-form"><input id="noteIn" placeholder="Add a note for the next crew" maxlength="240" autocomplete="off"></form>
    <div class="plan-btns"><button id="copyHandoff">Copy handoff</button><button id="saveHandoff">Download .txt</button></div>`;
  if (draft) $("noteIn").value = draft;
  if (typing) $("noteIn").focus();
  $("noteForm").onsubmit = (e) => {
    e.preventDefault();
    const v = $("noteIn").value.trim();
    if (v) { $("noteIn").value = ""; shiftlog.add("note", v); renderLog(); }
  };
  $("copyHandoff").onclick = async () => {
    try { await navigator.clipboard.writeText(handoffText()); $("copyHandoff").textContent = "Copied"; }
    catch { saveHandoff(); }
  };
  $("saveHandoff").onclick = saveHandoff;
}
function saveHandoff() {
  const url = URL.createObjectURL(new Blob([handoffText()], { type: "text/plain" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: `freeboard-handoff-${shiftlog.clockNow().replace(/[: ]/g, "")}.txt` });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
// CF-4: fixed sections in a fixed order, so the next crew always finds the same thing in
// the same place, and nothing in it is typed by hand:
//   1 open alarms by priority   2 shelved alarms   3 blind sensors   4 the approved plan
//   5 the next deadline         6 the what-ifs tried
// then the tank levels and the full shift log.
function handoffText() {
  const L = [];
  const head = (n, title) => L.push("", `${n}. ${title}`);
  const inc = data.state.incident;
  const base = shutdownOf(data.baseline);
  const nowView = viewAt(0);
  L.push("GAIA SHIFT HANDOFF", `Written ${shiftlog.clockNow()} for the next crew.`);
  if (inc) L.push(`Incident: ${inc.summary}`);
  if (base) {
    const wells = (field.wells || []).filter((w) => w.tank_id === base.asset_id).length;
    L.push(passed(base.t_min)
      ? `If nothing changed: ${nice(base.asset_id)} was forecast to reach shutdown at ${clockAt(base.t_min)}, taking ${wells} wells with it. That time has passed (${sinceRead()}): check the tank in person.`
      : `If nothing changes: ${nice(base.asset_id)} reaches shutdown at ${clockAt(base.t_min)}, and ${wells} wells go down with it.`);
  }

  const roots = alarms.roots();
  const open = roots.filter((a) => !a.shelved), shelf = roots.filter((a) => a.shelved);
  const src = alarms.derived() ? " (derived from the forecast: no alerts feed)" : "";
  const cons = (a) => {
    const m = alarms.minutesLeft(a);
    return a.consequence_clock && a.consequence_asset_id
      ? ` ${nice(a.consequence_asset_id)} reaches shutdown at ${a.consequence_clock}${m != null ? (m > 0 ? ` (${m} min)` : " (now)") : ""} if nothing changes.`
      : a.consequence ? ` ${a.consequence}` : "";
  };

  head(1, `OPEN ALARMS${src}`);
  const st = alarms.loadState();
  if (st.error && !st.loadedOnce && !alarms.derived()) L.push(`  Not checked: the alarm list did not load (${st.error}). Not all clear.`);
  else {
    if (st.error && !alarms.derived()) L.push(`  As of the last good read (the list is not refreshing: ${st.error}).`);
    if (!open.length) L.push(blindTanks().length ? "  None, but some tanks have no reading (see 3). Not all clear." : "  None.");
  }
  for (const a of open) {
    L.push(`  ${a.priority || a.severity}  ${nice(a.asset_id || "")}: ${a.message}${cons(a)}`);
    const tags = [a.related_count ? `+${a.related_count} related` : "", a.acked ? `acked${a.acked_by ? ` by ${a.acked_by}` : ""}` : "NOT ACKED", a.usd_at_risk ? `${usd(a.usd_at_risk)} at risk` : ""].filter(Boolean);
    L.push(`      ${tags.join(" · ")}`);
  }

  head(2, "SHELVED ALARMS");
  if (!shelf.length) L.push("  None.");
  for (const a of shelf) {
    const left = alarms.shelfLeft(a);
    L.push(`  ${a.priority || a.severity}  ${nice(a.asset_id || "")}: ${a.message}${cons(a)}`);
    L.push(`      Shelved${a.shelved_by ? ` by ${a.shelved_by}` : ""}: "${a.shelve_reason || "no reason recorded"}". Back${a.shelved_until ? ` at ${a.shelved_until}` : ""}${left != null ? ` (${left} min left)` : ""}.`);
    if (typeof a.consequence_t === "number" && typeof a.shelved_until_t === "number" && a.consequence_t < a.shelved_until_t) L.push(`      WARNING: the shutdown at ${a.consequence_clock} comes before this shelf ends.`);
  }

  head(3, "BLIND SENSORS");
  const blindIds = Object.keys(nowView).filter((id) => isBlind(nowView[id]));
  for (const id of frozenTanks()) {
    if (blindIds.includes(id)) continue;
    const f = frozen(id);
    L.push(`  ${nice(id)}: gauge reads exactly ${f.v.toFixed(2)}% for ${f.minutes} min, may be stuck. Check the level in person.`);
  }
  const stuckIds = frozenTanks();
  for (const id of blindTanks()) if (!blindIds.includes(id) && !stuckIds.includes(id)) blindIds.push(id);
  if (!blindIds.length && !stuckIds.length) L.push("  None. Every monitored asset has a fresh reading.");
  for (const id of blindIds) {
    const age = liveFeed.staleMinutes(id);
    L.push(`  ${nice(id)}: no fresh reading${age != null ? ` for ${age} min` : ""}. Check it in person; Gaia can't vouch for it.`);
  }

  head(4, "APPROVED PLAN");
  if (!approved) L.push("  No plan approved yet.");
  else {
    L.push(`  Approved ${approved.at}: ${approved.plan.summary} (${usd(approved.plan.cost.total_usd)}).${approved.ended ? ` The run ended ${approved.ended}${approved.reason ? `: ${approved.reason}` : ""}.` : ""}`);
    approved.steps.forEach((s, i) => {
      // Run steps carry their own status (#75); a static walk counts steps walked.
      const word = s.status ? (s.status === "DONE" ? "done" : s.status.toLowerCase()) : i < approved.done ? "done" : "pending";
      L.push(`  [${word === "done" ? "x" : " "}] ${word.padEnd(9)}${s.text}${s.status === "MISMATCH" && s.verify ? ` (${s.verify})` : ""}`);
    });
  }

  head(5, "NEXT DEADLINE");
  const lc = data.lc;
  if (approved && approved.ended) L.push(`  The approved plan's run ended ${approved.ended} with ${approved.done} of ${approved.steps.length} steps done. Nothing more from it reaches equipment; choose a plan again.`);
  else if (approved) L.push(approved.done >= approved.steps.length ? "  Every step of the approved plan is done." : `  Finish the approved plan: ${approved.steps.length - approved.done} step${approved.steps.length - approved.done === 1 ? "" : "s"} still pending.`);
  if (lc && lc.options && lc.options.length) {
    const o = lc.options[0], pnr = lc.point_of_no_return;
    L.push(passed(o.expires_min)
      ? `  Last Call passed at ${o.expires_clock}: the cheapest plan (${usd(o.cost.total_usd)}) no longer works as planned (${sinceRead()}).${rescore() ? ` ${rescore()}` : ""}`
      : `  Last Call: the cheapest plan (${usd(o.cost.total_usd)}) only works if started by ${o.expires_clock}.`);
    if (pnr) L.push(passed(pnr.t_min) ? `  The point of no return (${pnr.clock}) has passed: the forecast had no plan keeping every well online after it.` : `  After ${pnr.clock}, nothing keeps every well online.`);
  } else if (lc && lc.status) L.push(`  Last Call: ${lc.headline || lc.status}.`);
  else L.push("  No Last Call deadline on hand.");
  const now = shutdownOf(fc);
  L.push(`  On screen${actions.length ? ` (with ${actions.map(describe).join(" + ")})` : ""}: ${now ? `${nice(now.asset_id)} reaches shutdown at ${clockAt(now.t_min)}.` : noShutdownText(blindTanks())}`);

  for (const p of peopleOut()) L.push(`  PEOPLE OUT: ${p.who} at ${nice(p.asset)}${p.lastAgo != null ? `, last check-in ${p.lastAgo} min ago` : ""}${p.missed ? " (MISSED CHECK-IN)" : ""}.`);
  L.push(`  Weather: ${wxSummary()}`);
  const sq = seismicSummary(undefined, (field.disposal_wells || []).map((d) => nice(d.id)));
  L.push(`  Seismic: ${sq.line}${sq.flag ? ` ${sq.detail}` : ""}`);
  if (approved && hazards().length) {
    const out = exposure(approved.plan, field);
    if (out.length) L.push(`  The approved plan sends ${out.length} out (${out.map((o) => `${o.who}, ${nice(o.asset_id)}`).join("; ")}) in ${hazards().map((h) => h.text).join(", ")}.`);
  }

  head(6, "WHAT-IFS TRIED");
  const tried = shiftlog.entries().filter((e) => e.kind === "whatif");
  if (!tried.length) L.push("  None.");
  for (const e of tried) L.push(`  ${e.at.padStart(8)}  ${e.text}`);

  head(7, "REFUSED REQUESTS (security log)");
  L.push(...securityLines());

  // Levels as they read right now (live readings win over the boot snapshot), stamped,
  // so the next crew never acts on a number that was stale when it was written (#104).
  L.push("", `TANK LEVELS as of ${shiftlog.clockNow()}${liveFeed.live.available ? (liveFeed.live.lostSince ? " (live feed LOST: last known values)" : " (live)") : (dataAt && dataAt !== "boot" ? ` (snapshot from ${dataAt})` : " (snapshot from when the console opened)")}`);
  for (const t of field.tanks || []) {
    const s = nowView[t.id] || {};
    if (isBlind(s) && s.status !== "NO_DATA") L.push(`  ${nice(t.id)}: NO FRESH READING${s.level_pct != null ? ` (last ${s.level_pct.toFixed(1)}%)` : ""}${s.status === "ALARM" ? ", still in ALARM" : ""}. Not safe until checked.`);
    else L.push(s.level_pct != null ? `  ${nice(t.id)}: ${s.level_pct.toFixed(1)}% (shutdown ${t.shutdown_pct}%)` : `  ${nice(t.id)}: NO READING. Not safe until checked.`);
  }
  L.push("", "FULL SHIFT LOG");
  for (const e of shiftlog.entries()) L.push(`${e.at.padStart(8)}  ${e.text}`);
  const fakes = Object.entries(api.sources).filter(([, v]) => v === "fake").map(([k]) => k);
  L.push("", fakes.length ? `Data: fake for ${fakes.join(", ")}.` : "Data: live API.");
  return L.join("\n") + "\n";
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
  moon.shadow.normalBias = 0.04 * Math.max(1, half / 60); // one shadow texel grows with the box
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
  const changed = id !== selectedId;
  selectedId = id;
  if (changed) jarvisSelectionChanged();
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
// inspect.js (#86) does the picking: part-level hits, small parts by their asset's bounds,
// and scenery, ground and roads never pickable. It still resolves IDs with idFor() above.
function pick(ev) {
  return inspect.pick(ev.clientX, ev.clientY);
}
let downAt = null;
canvas.addEventListener("pointerdown", (e) => { downAt = [e.clientX, e.clientY]; fly = null; });
canvas.addEventListener("pointerup", (e) => {
  // A drag is camera movement, not a click.
  if (downAt && Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) < 5) select(pick(e));
  downAt = null;
});
canvas.addEventListener("pointermove", (e) => hoverAt(e.clientX, e.clientY));
// Hover at a screen point. The mouse and hand control (hands.js) both come through here.
// The tooltip (asset, part, status, live number, Last Call fuse) and hover highlight are in inspect.js.
function hoverAt(clientX, clientY) {
  return inspect.hover(clientX, clientY);
}
canvas.addEventListener("pointerleave", () => { $("hoverTag").hidden = true; });

$("time").addEventListener("input", (e) => { tMin = Number(e.target.value); render(); });
addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" && e.target.type !== "range") return;
  if (e.target.tagName === "INPUT") return;
  // Console: Esc resets the view only; an approved plan walk keeps going (spec §3 Reset).
  if (e.key === "Escape") { if (ui) ui.reset(); else { if (walk) endWalk(); select(null); flyHome(); } }
});

// ---------------------------------------------------------------- pager ----
// A QR code for /phone. A judge scans it and their own phone becomes the night pager.
function mountPager() {
  const btn = document.createElement("button");
  btn.className = "upload-open";
  btn.textContent = "Pager";
  document.querySelector(".brand-row").appendChild(btn);
  btn.onclick = async () => {
    // The phone token rides in the fragment, only if this console was opened with one (#104).
    const pt = phoneToken();
    const url = (import.meta.env.VITE_PHONE_URL || `${location.origin}/phone`) + (pt ? `#k=${encodeURIComponent(pt)}` : "");
    const QR = (await import("qrcode")).default;
    const svg = await QR.toString(url, { type: "svg", margin: 1, color: { dark: "#e8e2d6", light: "#050505" } });
    const local = /^(127\.0\.0\.1|localhost)$/.test(new URL(url).hostname);
    const box = document.createElement("div");
    box.className = "pager";
    box.innerHTML = `<div class="pager-card"><div class="kicker">The night pager</div>
      <div class="pager-qr">${svg}</div>
      <p>Scan with a phone on this Wi-Fi. It buzzes on every alert, and a fix can be confirmed from it with a press and hold.</p>
      <code>${esc(url.split("#")[0])}</code>
      ${local ? `<p class="note">This address only works on this laptop. Start with <b>npm run demo:lan</b> to get a Wi-Fi address.</p>` : ""}
      ${pt ? `<p class="muted">This QR carries a phone token (never shown as text): that phone can confirm, reject and acknowledge, nothing else.</p>`
        : `<p class="muted">No control token: anyone on this network can confirm. Start with npm run demo to get one.</p>`}
      <button>Close</button></div>`;
    box.onclick = (e) => { if (e.target === box || e.target.tagName === "BUTTON") box.remove(); };
    document.body.appendChild(box);
  };
}

// ----------------------------------------------------------------- boot ----
if (api.usingBackend) liveFeed.start(api.BASE); // live.js sends the auth.js token in the first /ws message
startTrend(api.usingBackend ? api.BASE : "");
// The backend sends state every second, and render() rebuilds the Selected card and the
// tank list. A rebuild between mousedown and mouseup swallows the click, so while a press is
// held inside the side panel the redraw waits for the release.
let pressInSide = false, renderQueued = false;
addEventListener("pointerdown", (e) => { pressInSide = !!(e.target && e.target.closest && e.target.closest("#side")); }, true);
const released = () => { pressInSide = false; if (renderQueued) { renderQueued = false; setTimeout(() => { if (data) { renderFeed(); render(); } }, 0); } };
addEventListener("pointerup", released, true);
addEventListener("pointercancel", released, true);
let queuedAt = 0;
liveFeed.on("state", () => {
  if (!data) return;
  // At most 1.5 s: a press with no release (a scrollbar drag, a release outside the window)
  // must not freeze the levels and the clock.
  if (pressInSide && (!renderQueued || Date.now() - queuedAt < 1500)) { if (!renderQueued) queuedAt = Date.now(); renderQueued = true; return; }
  pressInSide = false;
  renderQueued = false;
  renderFeed();
  render();
});

// #104: the countdown, plans and Last Call were read once at boot and went stale as the
// night moved. Re-read them every minute, and soon after any alert. A failed read leaves
// the old values on screen under a BACKEND DOWN badge, never recorded ones.
let dataAt = "boot";
let refreshing = null;
async function refreshData() {
  if (!data || !api.usingBackend || refreshing) return;
  refreshing = (async () => {
    try {
      const [state, baseline, lc, plans] = await Promise.all([api.getState(), api.forecast([]), api.lastCall(), api.plans()]);
      Object.assign(data, { state, baseline, lc, plans });
      if (storm && !storm.busy) storm = null; // the field moved: look again with the new forecast
      if (!actions.length && !document.body.classList.contains("two-nights")) fc = baseline;
      dataAt = shiftlog.clockNow();
    } catch (e) {
      console.warn("refresh", e.message);
    } finally {
      refreshing = null;
      if (isHolding()) afterHold(renderAll); // never swap a hold button out from under a finger
      else renderAll();
    }
  })();
}
setInterval(refreshData, 60000);
// The countdown counts down between forecast reads (and offline, where there are none).
setInterval(() => { if (data) renderIncident(); }, 30000);
let alertKick = 0;
liveFeed.on("alert", () => { clearTimeout(alertKick); alertKick = setTimeout(refreshData, 2000); });
liveFeed.on("feed", () => { if (data) { renderFeed(); render(); } });
mountUpload();
mountPager();
// Two nights (counterfactual replay): all of it lives in twonights.js. These hooks are its only door in.
mountTwoNights({
  field: () => field,
  baseline: () => data.baseline,
  fc: () => fc,
  plans: () => (data && data.plans && data.plans.plans) || [],
  lastCall: () => (data && data.lc) || null,
  approved: () => (approved && !approved.ended ? approved.plan : null),
  incident: () => data.state.incident,
  getActions: () => actions.map((a) => ({ ...a })),
  setActions: (list, quiet) => setActions(list, "", quiet),
  getTime: () => tMin,
  setTime: (t) => { tMin = t; $("time").value = String(t); render(); },
  levelMesh: (id) => (assets.get(id) || {}).levelMesh || null,
  approve: (plan) => startWalk(plan),
  focusIncident: () => { const id = incidentTank(); if (id) select(id, true); },
  log: (text) => { shiftlog.add("whatif", text); renderLog(); },
  describe, nice, clockAt, usd,
});
if (layout === "classic") mountJarvis({
  stage: $("stage"),
  selected: () => selectedId,
  focus: (id) => { if (ids.has(id)) { select(id, true); inspect.pointed(id); } },
  logged: () => renderLog(),
  // A picture of the 3D view for "what am I looking at", at most 640 px wide (about 0.5 MB).
  snapshot() {
    renderer.render(scene, camera);
    const k = Math.min(1, 640 / canvas.width);
    const c = Object.assign(document.createElement("canvas"), { width: Math.round(canvas.width * k), height: Math.round(canvas.height * k) });
    c.getContext("2d").drawImage(canvas, 0, 0, c.width, c.height);
    return c.toDataURL("image/png").split(",")[1];
  },
});
// Jarvis console (Track B): status line, agent stream, sidebar, fix card. Everything lives in
// console/; these hooks are its only door in.
const ui = layout === "console" ? mountConsole({
  app: $("app"), stage: $("stage"), canvas, scene, camera, controls, renderer, BASE: api.BASE,
  ready: () => !!data,
  data: () => data,
  stateClock: () => (data ? data.state.clock : null),
  clockNow: () => shiftlog.clockNow(),
  ids: () => [...ids],
  tanks: () => ((field && field.tanks) || []).map((t) => t.id),
  tankOf: (id) => { const p = ((field && field.pumps) || []).find((x) => x.id === id) || ((field && field.wells) || []).find((x) => x.id === id); return p ? p.tank_id : null; },
  blind: () => (data ? blindTanks() : []),
  selected: () => selectedId,
  select: (id) => select(id),
  focus: (id) => { if (ids.has(id)) { select(id, true); inspect.pointed(id, "jarvis"); } },
  flyHome: () => flyHome(),
  getTime: () => tMin,
  setTime: (t) => { tMin = t; $("time").value = String(t); render(); },
  getActions: () => actions.map((a) => ({ ...a })),
  setActions: (list) => setActions(list),
  walking: () => !!walk,
  describe, nice, clockAt, usd,
  logged: () => renderLog(),
}) : null;
if (layout === "classic") {
  const back = document.createElement("button");
  back.className = "upload-open";
  back.textContent = "Console";
  back.title = "Back to the Jarvis console";
  back.onclick = () => { const u = new URL(location.href); u.searchParams.delete("layout"); location.href = u.toString(); };
  document.querySelector("#side .brand-row").appendChild(back);
}
// Hand control drives the same hover, select, orbit and zoom the mouse does.
mountHands({
  canvas, stage: $("stage"),
  hoverAt,
  clickAt(x, y) {
    const id = pick({ clientX: x, clientY: y });
    if (!id) return null;
    select(id, true);
    // Jarvis (#44) listens for this and opens on the asset.
    dispatchEvent(new CustomEvent("freeboard:jarvis", { detail: { asset_id: id, from: "pinch" } }));
    return id;
  },
  orbit(dx, dy) {
    fly = null;
    const off = camera.position.clone().sub(controls.target);
    const s = new THREE.Spherical().setFromVector3(off);
    s.theta -= dx * Math.PI * 1.6;
    s.phi = Math.max(0.15, Math.min(controls.maxPolarAngle, s.phi - dy * Math.PI));
    camera.position.copy(controls.target).add(new THREE.Vector3().setFromSpherical(s));
  },
  zoom(f) {
    fly = null;
    const off = camera.position.clone().sub(controls.target);
    const len = Math.max(controls.minDistance, Math.min(controls.maxDistance, off.length() * Math.pow(f, 1.5)));
    camera.position.copy(controls.target).add(off.setLength(len));
  },
});
// Highlight and part-level picking (#86). Every highlight source goes through highlight.js.
const inspect = mountInspect({
  scene, camera, renderer, canvas, controls, tag: $("hoverTag"), assets, idFor, select,
  ids: () => ids, view: () => lastView, field: () => field, selected: () => selectedId, actions: () => actions,
  equipment: (id) => { const e = equipment.get(id); return e ? { name: EQUIPMENT_NAMES[e.type] || "Equipment", label: e.label } : null; },
  fuse: (id) => { const f = fuses.get(id); return f && f.burn.visible && data.lc.assets[id] ? data.lc.assets[id].useful_until_clock : null; },
  staleMin: (id) => liveFeed.staleMinutes(id),
});
async function boot() {
  const [f, state, model] = await Promise.all([api.getField(), api.getState(), loadModel()]);
  field = f;
  ids = new Set(api.assetIds(field));
  const [baseline, lc, plans] = await Promise.all([api.forecast([]), api.lastCall(), api.plans()]);
  data = { field, state, baseline, lc, plans };
  shiftlog.start(state.clock || "00:00", (state.incident && state.incident.summary) || "");
  // The live clock, carried on at the last known speed while the feed is lost, so the header
  // and log stamps never jump back to the page-load clock.
  shiftlog.followClock(() => liveFeed.clockEstimate());
  let built = null;
  try { built = JSON.parse(sessionStorage.getItem("freeboard.built") || "null"); sessionStorage.removeItem("freeboard.built"); } catch { /* ignore */ }
  if (built) shiftlog.add("open", `Field "${built.name}" built from an uploaded blueprint in ${built.secs} s${built.status === "fallback" ? `, on the standard model (${built.note})` : ""}.`);
  if (!shiftlog.entries().some((e) => e.kind === "open")) {
    const ev = shutdownOf(baseline);
    shiftlog.add("open", `Console opened. ${(state.incident && state.incident.summary) || "No incident."}${ev ? (passed(ev.t_min) ? ` ${nice(ev.asset_id)} was forecast to reach shutdown at ${clockAt(ev.t_min)} (${sinceRead()}); that time has passed.` : ` ${nice(ev.asset_id)} reaches shutdown at ${clockAt(ev.t_min)} if nobody acts.`) : ""}`);
  }
  shiftlog.flushEarly(); // live alerts that came in while the model loaded
  fc = baseline;
  scene.add(model.root);
  sky.attach(model.root); // before indexModel darkens the site, so the sky knows its daytime colors
  indexModel(model.root);
  flow = mountFlow(model.root); // stripes along pipes that carry water right now
  mountIot3d({ root: model.root, field, live: liveFeed, base: api.BASE }); // #80: sensor and RTU lights, alert and run highlights

  const missing = [...ids].filter((id) => !assets.has(id));
  const notes = [...(baseline.warnings || [])];
  if (api.usingBackend && Object.values(api.sources).includes("fake")) notes.unshift("Some endpoints are not on the backend yet. The badge says which.");
  if (missing.length) notes.push(`Not in the model: ${missing.join(", ")}`);
  $("warnings").innerHTML = notes.map((n) => `<div>${esc(n)}</div>`).join("");
  const note = $("modelNote");
  note.hidden = false;
  note.textContent = model.from === "placeholder" ? "Placeholder geometry until blender/field.glb lands" : `Model: ${model.from}`;
  mountScenery(model.root, note, renderer); // #88 scenery toggle, plus a 45 fps guard (pixels first, then scenery)
  // Compile every shader behind the loading screen, so the first frames do not stall
  // (0.4 to 1.6 s measured). Never hold the console longer than 4 s for it.
  try {
    await Promise.race([renderer.compileAsync(scene, camera), new Promise((r) => setTimeout(r, 4000))]);
  } catch (e) { console.warn("compileAsync:", e); }
  $("loading").remove();
  frame(model.root);
  buildFuses();
  inspect.ready(model.root);
  if (ui) ui.ready(model.root);
  renderAll();
  // Storm check: the real NWS weather at the site, read in the browser (no key, no backend).
  let hadHazard = false;
  onWeather(() => {
    const hz = hazards();
    if (hz.length && !hadHazard) { shiftlog.add("alarm", `Storm check: ${hz.map((h) => h.text).join(", ")}. Plans that send people out are flagged.`); renderLog(); }
    hadHazard = hz.length > 0;
    if (data) renderPlans();
  });
  startWeather(field.site || DEFAULT_SITE, { offline: api.OFFLINE });
  // Seismic check: USGS quakes near the lease, read in the browser. Logged once if flagged.
  let quakeLogged = false;
  onQuakes(() => {
    const sq = seismicSummary(undefined, (field.disposal_wells || []).map((d) => nice(d.id)));
    if (sq.flag && !quakeLogged) { quakeLogged = true; shiftlog.add("note", `Seismic check: ${sq.line}`); renderLog(); }
    if (data) renderPlans();
  });
  startSeismic(field.site || DEFAULT_SITE, { offline: api.OFFLINE });
  // CF-1 alarm list: all of it lives in alarms.js. These hooks are its only door in.
  mountAlarms($("alarms"), {
    base: api.usingBackend ? api.BASE : "",
    incident: () => data.state.incident,
    firstShutdown: () => shutdownOf(data.baseline),
    blindTanks,
    nice,
    usd,
    clockAt,
    startClock: () => clockAt(0),
    clockIn: (m) => shiftlog.clockNow(false, m),
    select: (id) => select(id, true),
    log: (text) => { shiftlog.add("alarm", text); renderLog(); },
  });
  // #126 security log: refused requests (wrong tokens, writes from another site). Its own card.
  mountSecurity($("security"), api.usingBackend ? api.BASE : "", (text) => { shiftlog.add("alarm", text); renderLog(); }, () => shiftlog.clockNow());
  // #75 plan runs: the run panel under the plans. It feeds the handoff's approved plan.
  mountRuns($("run"), {
    base: api.usingBackend ? api.BASE : "",
    usd,
    nice,
    clockIn: (m) => shiftlog.clockNow(false, m),
    // Who a step sends out alone: the rover to a manual valve (a remote valve is worked from here).
    outside: (a) => (a && a.type === "SET_VALVE" && exposure({ actions: [a] }, field).length ? "Rover" : null),
    select: (id) => select(id, true),
    log: (text) => { shiftlog.add("step", text); renderLog(); },
    progress: (r) => {
      if (!approved || approved.run !== r.id) {
        // "01:48" from the backend is when the run started; a reload must not restamp it.
        const st = /^(\d{1,2}):(\d{2})$/.exec(r.start_clock || "");
        const at = st ? `${(+st[1] % 12) || 12}:${st[2]} ${+st[1] < 12 ? "AM" : "PM"}` : shiftlog.clockNow();
        if (!approved || approved.run) approved = { plan: { id: r.plan_id, summary: r.summary, cost: { total_usd: r.cost_usd }, actions: r.steps.map((s) => s.action).filter(Boolean) }, steps: r.steps, done: 0, at, run: r.id };
        else return;
      }
      approved.steps = r.steps;
      approved.done = r.steps.filter((s) => s.status === "DONE").length;
      approved.ended = r.status === "FAILED" || r.status === "CANCELLED" ? r.status : null;
      approved.reason = approved.ended ? String(r.reason || "").replace(/\.+$/, "") : "";
      approved.logged = approved.logged || new Set();
      let fresh = false;
      for (const s of r.steps) {
        if (approved.logged.has(s.n) || !["DONE", "MISMATCH", "FAILED", "REJECTED"].includes(s.status)) continue;
        approved.logged.add(s.n);
        shiftlog.add("step", s.status === "DONE" ? `Done: ${s.text}${s.verify ? ` ${s.verify}` : ""}` : `Step ${s.status.toLowerCase()}: ${s.text}${s.verify || s.result ? ` ${s.verify || s.result}` : ""}`);
        fresh = true;
      }
      if (fresh) renderLog();
      renderPlans(); // the approved plan's card shows how far the run got
    },
  });
  window.__freeboard = { assets, select, viewAt: (t) => viewAt(t), missing, setActions, flow: () => flow && flow.debug(lastView), get actions() { return actions; }, handoff: () => handoffText(),
    // Pumpjack linkage, for checks: per well, beam angle and how far the pitman's foot is from its crank pin (m).
    jacks: () => [...jacks].map(([id, j]) => { const E = equalizer(j.beam.rotation.x), P = crankPin(j.crank.rotation.x); return { id, beam: +j.beam.rotation.x.toFixed(3), gap: +Math.abs(Math.hypot(E[0] - P[0], E[1] - P[1]) - j.L).toFixed(4) }; }) };
}

const clock = new THREE.Clock();
let lastSec = -1;
let flow = null;
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
    if (flow) flow.update(dt, lastView);
    sky.update(dt); // after paint(), so lamps and headlights layer on top of the status colors
    if ((t | 0) !== lastSec) { lastSec = t | 0; $("clock").textContent = shiftlog.clockNow(true); if (liveFeed.live.lostSince) renderFeed(); }
  }
  if (selBox) selBox.update();
  inspect.update(); // highlight lifts and dimming, after paint()
  if (ui) ui.update(dt, t);
  renderer.render(scene, camera);
  inspect.overlay(t); // outlines on top of the frame
});

// ?fresh starts a new shift log, crew list and burst line once. Every module has read it by
// now: drop it from the address bar so a reload mid-shift keeps the log.
boot().then(() => {
  try {
    const u = new URL(location.href);
    if (u.searchParams.has("fresh")) { u.searchParams.delete("fresh"); history.replaceState(null, "", u.pathname + u.search + u.hash); }
  } catch { /* leave the address as it is */ }
}).catch((e) => {
  const l = $("loading");
  console.error(e);
  if (!l) return;
  if (e instanceof api.BackendDown) {
    // #104: never fall back to recorded numbers when a backend was asked for and failed.
    l.classList.add("down");
    $("clock").textContent = "--:--"; // no field clock without a backend
    l.innerHTML = `<b>GAIA</b><span class="down-head">The backend isn't answering</span>
      <span>${esc(e.message)} at ${esc(api.BASE)}</span>
      <span>Nothing on this screen would be live, so the console shows nothing rather than guess.</span>
      <span class="down-btns"><button type="button" data-retry>Retry</button><a href="?offline">Open the recorded demo instead (clearly marked)</a></span>`;
    l.querySelector("[data-retry]").onclick = () => location.reload();
    return;
  }
  l.textContent = `Failed to start: ${e.message}`;
});
