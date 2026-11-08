// Generation replay (generate.html). Plays a template's real ingest, review, and build outputs
// as seven steps. Nothing here is invented: lists come from ingest.json, numbers from
// build_report.json, and every 3D node from the generator's field.glb.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { createLook } from "./look.js";

const $ = (s) => document.querySelector(s);
const params = new URLSearchParams(location.search);
const STEPS = [
  ["upload", "Upload", "The site plan arrives"],
  ["read", "Read", "Each line becomes an asset with an ID"],
  ["review", "Review", "A person fills in what the plan didn't say"],
  ["layout", "Lay out", "Ground, lease road, and pads"],
  ["build", "Build", "Equipment goes up, type by type"],
  ["pipe", "Pipe", "Lines tie wells, tanks, pumps, and disposal together"],
  ["done", "Ready", "The console opens on this field"],
];
const TYPES = [  // build order, with the label shown in the counters and the marker color
  ["tank", "Tanks", 0xf2b33d, 4.5], ["pump", "Pumps", 0x7fb2e5, 1.6], ["valve", "Valves", 0xb9a3e3, 1.2],
  ["well", "Wells", 0x9fd17a, 2.2], ["disposal_well", "Disposal wells", 0xe5896f, 3.5], ["loadout", "Load-outs", 0x8b96a3, 1.4],
  ["yard", "Yard", 0x8b96a3, 6], ["truck", "Trucks", 0xdde3ea, 2.4], ["other", "Other", 0x8b96a3, 1.5],
];
const KIND_BY_PREFIX = { TANK: "tank", PUMP: "pump", VALVE: "valve", WELL: "well", SWD: "disposal_well", LOADOUT: "loadout", YARD: "yard", TRUCK: "truck" };

// ---------- scene ----------
const view = $("#view");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.localClippingEnabled = true;
view.prepend(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(42, 1, 0.5, 5000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.47;
// Sky, IBL, sun shadows, fog, and the Day / Dusk / Night toggle (?look=day|dusk|night), shared with index.html.
const look = createLook({ renderer, scene, camera, container: view, initial: params.get("look") });
const markers = new THREE.Group();
scene.add(markers);
const pipePlane = new THREE.Plane(new THREE.Vector3(-1, 0, 0), 0);

function resize() {
  const w = view.clientWidth, h = view.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / Math.max(h, 1);
  camera.updateProjectionMatrix();
}
addEventListener("resize", resize);
resize();

// ---------- a timeline that runs on a speed-scaled clock ----------
let simT = 0, speed = Number(params.get("speed")) || 1, runId = 0, tweens = [], waiters = [];
const easeOut = (k) => 1 - (1 - k) ** 3;
const easeBack = (k) => 1 + 2.4 * (k - 1) ** 3 + 1.4 * (k - 1) ** 2;
function tween(ms, fn, ease = easeOut) { tweens.push({ t0: simT, ms, fn, ease }); }
function wait(ms) { return new Promise((r) => waiters.push({ at: simT + ms, r })); }
function setSpeed(s) {
  speed = s;
  document.querySelectorAll("[data-speed]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.speed) === s)));
}

// ---------- state ----------
const state = { tpl: null, ingest: null, report: null, blueprint: null, gltf: null, groups: null, step: null, done: false, parts: null };
window.__replay = state;  // for headless checks

function classify(root) {
  const name = root.name || "", kind = root.userData?.kind;
  if (kind === "waypoint" || /^(STOP_|CAM_|ROAD_JCT_)/.test(name)) return "hidden";
  if (/^SCENERY/.test(name)) return "scenery";
  if (/^PIPE_/.test(name)) return "pipe";
  if (/^PAD_/.test(name)) return "pad";
  if (/^ROAD/.test(name)) return "road";
  if (/^(SITE|GROUND)/.test(name)) return "site";
  if (kind) return TYPES.some(([k]) => k === kind) ? kind : "other";
  return KIND_BY_PREFIX[name.split("_")[0]] || "other";
}

function prepare(gltf) {
  const groups = {};
  for (const root of [...gltf.scene.children]) {
    const g = classify(root);
    (groups[g] = groups[g] || []).push(root);
    root.userData.p0 = root.position.clone();
    root.userData.s0 = root.scale.clone();
    root.traverse((o) => {
      if (!o.isMesh) return;
      o.material = o.material.clone();
      o.userData.m0 = { transparent: o.material.transparent, opacity: o.material.opacity };
      if (g === "pipe") o.material.clippingPlanes = [pipePlane];
    });
  }
  const box = new THREE.Box3().setFromObject(gltf.scene);
  const parts = { levels: [] };  // rotors, beams, and cranks are animated by look.js
  gltf.scene.traverse((o) => {
    if (/^(TANK_\w+)_LEVEL$/.test(o.name)) parts.levels.push({ o, tank: o.name.replace(/_LEVEL$/, ""), y0: o.scale.y });
  });
  return { groups, box, parts };
}

function setOpacity(root, k) {
  root.traverse((o) => {
    if (!o.isMesh) return;
    o.material.transparent = k < 1 || o.userData.m0.transparent;
    o.material.opacity = o.userData.m0.opacity * k;
  });
}
function fadeIn(root, ms) { root.visible = true; setOpacity(root, 0); tween(ms, (k) => setOpacity(root, k)); }
function rise(root, ms) {
  const { p0, s0 } = root.userData;
  root.visible = true;
  tween(ms, (k) => {
    root.scale.copy(s0).multiplyScalar(Math.max(0.001, k));
    root.position.set(p0.x, p0.y - 4 * (1 - Math.min(k, 1)), p0.z);
  }, easeBack);
}
function resetScene() {
  const { groups } = state;
  for (const [g, roots] of Object.entries(groups)) for (const r of roots) {
    r.visible = false;
    r.position.copy(r.userData.p0);
    r.scale.copy(r.userData.s0);
    setOpacity(r, 1);
    if (g === "pipe") r.traverse((o) => o.isMesh && (o.material.clippingPlanes = [pipePlane]));
  }
  markers.clear();
  pipePlane.constant = state.box.min.x - 10;
  for (const l of state.parts.levels) l.o.scale.y = l.y0;
  state.done = false;
}

// ---------- side panel ----------
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const SECTION_LABEL = { tanks: "Tanks", pumps: "Pumps", valves: "Valves", wells: "Wells", disposal_wells: "Disposal wells", trucks: "Trucks", yards: "Yards" };

function drawSteps(active) {
  const idx = STEPS.findIndex(([k]) => k === active);
  $("#steps").innerHTML = STEPS.map(([k, title, text], i) =>
    `<li class="${i < idx || active === "done" && i === idx ? "done" : i === idx ? "on" : ""}" id="st-${k}"><b>${title}</b><span class="info">${text}</span></li>`).join("");
}
function caption(step, sub, frac) {
  const [, title] = STEPS.find(([k]) => k === step);
  $("#capStep").textContent = `${STEPS.findIndex(([k]) => k === step) + 1} of ${STEPS.length}  ${title}`;
  $("#capSub").textContent = sub;
  $("#bar i").style.width = `${Math.round(frac * 100)}%`;
}
function enter(step, sub) {
  state.step = step;
  drawSteps(step);
  caption(step, sub, STEPS.findIndex(([k]) => k === step) / (STEPS.length - 1));
}

// ---------- the seven steps ----------
async function play() {
  const id = ++runId;
  tweens = []; waiters = [];
  const alive = () => id === runId;
  const { ingest, report, groups } = state;
  resetScene();
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.5;
  const detail = $("#detail");

  enter("upload", `${ingest.file}, ${(ingest.bytes / 1024).toFixed(1)} KB`);
  detail.innerHTML = `<h2>${esc(ingest.file)}</h2><pre class="src" id="src">${esc(ingest.preview)}</pre>`;
  await wait(1600); if (!alive()) return;

  const how = ingest.read_by === "parser" ? "Parsed directly, no Claude call needed" : "Read by Claude with structured outputs";
  enter("read", `${how}. ${ingest.items.length} assets.`);
  detail.innerHTML = `<h2>${how}</h2><div id="items"></div>`;
  let section = null;
  const per = Math.min(90, 2600 / Math.max(ingest.items.length, 1));
  for (const it of ingest.items) {
    if (it.section !== section) { section = it.section; $("#items").insertAdjacentHTML("beforeend", `<h2>${SECTION_LABEL[section] || section}</h2>`); }
    const conf = it.confidence == null ? "" : `<span class="pill ${it.confidence >= 0.8 ? "ok" : "warn"}">${Math.round(it.confidence * 100)}%</span>`;
    $("#items").insertAdjacentHTML("beforeend", `<div class="item"><div><span class="id">${esc(it.id)}</span> <span class="from">from "${esc(it.label)}"${it.evidence ? `, ${esc(it.evidence)}` : ""}</span></div>${conf}</div>`);
    detail.scrollTop = detail.scrollHeight;
    await wait(per); if (!alive()) return;
  }
  await wait(500); if (!alive()) return;

  const nReview = ingest.review.length;
  enter("review", nReview ? `${nReview} numbers weren't in the file, so a person fills them in` : "Every number was in the file. Nothing to review.");
  detail.innerHTML = nReview ? `<h2>${nReview} items need a person</h2><div id="rev"></div>` :
    `<h2>Review</h2><div class="callout">Nothing to review. Every number the forecast needs was in the file, and nothing was guessed.</div>`;
  for (const r of ingest.review) $("#rev").insertAdjacentHTML("beforeend",
    `<div class="item" data-id="${esc(r.id)}"><div><span class="id">${esc(r.id)}</span> <span class="from">${esc(r.label || "")}</span></div><span class="pill warn">needs input</span><div class="issue">${esc(r.issue)}</div></div>`);
  await wait(nReview ? 1200 : 1000); if (!alive()) return;
  for (const f of ingest.filled) {
    const row = $(`#rev [data-id="${CSS.escape(f.id)}"]`);
    if (row) {
      row.querySelector(".pill").className = "pill ok"; row.querySelector(".pill").textContent = "filled";
      const iss = row.querySelector(".issue"); iss.classList.add("fixed");
      iss.textContent = `${f.field} = ${typeof f.value === "object" ? JSON.stringify(f.value).replace(/"/g, "") : f.value}`;
      row.scrollIntoView({ block: "nearest" });
    }
    await wait(Math.min(160, 2000 / Math.max(ingest.filled.length, 1))); if (!alive()) return;
  }
  if (nReview) await wait(600); if (!alive()) return;

  enter("layout", "Positions come from the plan, or from auto-layout where the plan has none");
  detail.innerHTML = `<h2>Lay out</h2><div class="counters" id="lay"></div>`;
  for (const g of ["site", "road", "scenery"]) for (const r of groups[g] || []) fadeIn(r, 900);
  await wait(500); if (!alive()) return;
  for (const r of groups.pad || []) fadeIn(r, 600);
  const marks = new Map();
  for (const [kind, , color, radius] of TYPES) for (const r of groups[kind] || []) {
    const ring = new THREE.Mesh(new THREE.RingGeometry(radius * 0.8, radius, 40), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9, side: THREE.DoubleSide, depthWrite: false }));
    ring.rotation.x = -Math.PI / 2;
    ring.position.set(r.userData.p0.x, 0.15, r.userData.p0.z);
    markers.add(ring); marks.set(r, ring);
  }
  $("#lay").innerHTML = `<span>Pads</span><span class="n">${(groups.pad || []).filter((p) => /^PAD_\d+$/.test(p.name)).length}</span>` +
    `<span>Asset positions marked</span><span class="n">${marks.size}</span>` + ((groups.scenery || []).length ? `<span>Scenery layer</span><span class="n">on</span>` : "");
  await wait(1600); if (!alive()) return;

  enter("build", `${marks.size} assets from the template library, each named by its blueprint ID`);
  detail.innerHTML = `<h2>Build</h2><div class="counters" id="cnt"></div>`;
  const shown = Object.fromEntries(TYPES.map(([k]) => [k, 0]));
  const drawCounts = () => { $("#cnt").innerHTML = TYPES.filter(([k]) => (groups[k] || []).length).map(([k, label]) => `<span>${label}</span><span class="n">${shown[k]} / ${groups[k].length}</span>`).join(""); };
  drawCounts();
  for (const [kind] of TYPES) {
    const roots = groups[kind] || [];
    const gap = Math.min(110, 900 / Math.max(roots.length, 1));
    for (const r of roots) {
      rise(r, 650);
      const ring = marks.get(r);
      if (ring) tween(500, (k) => { ring.material.opacity = 0.9 * (1 - k); if (k >= 1) markers.remove(ring); });
      shown[kind]++; drawCounts();
      await wait(gap); if (!alive()) return;
    }
    if (roots.length) await wait(250); if (!alive()) return;
  }
  for (const r of groups.other || []) if (!r.visible) rise(r, 500);
  await wait(500); if (!alive()) return;

  const pipes = groups.pipe || [];
  enter("pipe", `${pipes.length} pipe runs follow tank_id, pumps, and valves`);
  detail.innerHTML = `<h2>Pipe</h2><div class="counters"><span>Pipe runs</span><span class="n">${pipes.length}</span></div>`;
  for (const r of pipes) r.visible = true;
  const { min, max } = state.box;
  tween(2400, (k) => { pipePlane.constant = min.x - 10 + (max.x - min.x + 20) * k; });
  await wait(2500); if (!alive()) return;

  finish();
}

function finish() {
  const { groups, report, ingest, blueprint, tpl } = state;
  tweens = []; waiters = [];
  for (const [g, roots] of Object.entries(groups)) for (const r of roots) {
    if (g === "hidden") continue;
    r.visible = true; r.position.copy(r.userData.p0); r.scale.copy(r.userData.s0); setOpacity(r, 1);
    r.traverse((o) => o.isMesh && (o.material.clippingPlanes = []));
  }
  markers.clear();
  const pct = blueprint?.initial?.tank_pct || {};
  for (const l of state.parts.levels) {
    const k = pct[l.tank] == null ? l.y0 : pct[l.tank] / 100;
    const from = l.o.scale.y;
    tween(1200, (e) => { l.o.scale.y = from + (k - from) * e; });
    const gauge = l.o.parent?.getObjectByName(`${l.tank}_gauge`) || scene.getObjectByName(`${l.tank}_gauge`);
    if (gauge) gauge.position.y = 0.3 + 4.3 * k;
  }
  state.done = true;
  controls.autoRotateSpeed = 0.35;
  enter("done", `Built for real in ${report.seconds} s. This replay was slowed down so you can follow it.`);
  $("#bar i").style.width = "100%";
  const tri = (report.triangles ?? 0).toLocaleString();
  $("#detail").innerHTML = `<h2>${esc(tpl.name)}</h2><div class="stats">
    <span>Assets</span><span class="v">${ingest.items.length}</span>
    <span>Asked on review</span><span class="v">${ingest.review.length}</span>
    <span>Triangles</span><span class="v">${tri}</span>
    <span>File size</span><span class="v">${report.glb_mb} MB</span>
    <span>Real build time</span><span class="v">${report.seconds} s</span>
    <span>Detail level</span><span class="v">${esc(report.detail?.level || "full")}</span></div>
    <div class="callout">Every node is named by its blueprint ID, so the forecast, plans, Last Call, and Jarvis plug in with no extra mapping. Pumps spin, pumpjacks rock, and each tank shows its starting level${Object.keys(pct).length ? "" : " (this file has none, so tanks show the preview level)"}.</div>`;
}

// ---------- the render loop ----------
const clock = new THREE.Clock();
renderer.setAnimationLoop(() => {
  const dt = Math.min(clock.getDelta(), 0.1);
  simT += dt * 1000 * speed;
  tweens = tweens.filter((t) => { const p = Math.min((simT - t.t0) / t.ms, 1); t.fn(t.ease(Math.max(p, 0))); return p < 1; });
  waiters = waiters.filter((w) => (simT >= w.at ? (w.r(), false) : true));
  if (state.done) look.animate(simT / 1000, dt);  // pumps spin and pumpjacks rock once the field is built
  controls.update();
  look.update();
  renderer.render(scene, camera);
});

// ---------- loading ----------
const loader = new GLTFLoader();
const getJSON = (u) => fetch(u).then((r) => { if (!r.ok) throw new Error(`${u}: HTTP ${r.status}`); return r.json(); });

async function load(tpl) {
  runId++;
  $("#capStep").textContent = "Loading"; $("#capSub").textContent = tpl.name;
  const [ingest, report, blueprint, gltf] = await Promise.all([getJSON(tpl.ingest), getJSON(tpl.report), getJSON(tpl.blueprint),
    new Promise((ok, bad) => loader.load(tpl.glb, ok, undefined, () => bad(new Error(`Couldn't load ${tpl.glb}. Run blender/build_templates.py.`))))]);
  if (state.gltf) scene.remove(state.gltf.scene);
  Object.assign(state, { tpl, ingest, report, blueprint, gltf }, prepare(gltf));
  scene.add(gltf.scene);
  look.attach(gltf.scene);
  const size = state.box.getSize(new THREE.Vector3()), center = state.box.getCenter(new THREE.Vector3());
  const d = Math.max(size.x, size.z) * 0.95;
  camera.position.set(center.x - d * 0.55, d * 0.55, center.z + d * 0.75);
  controls.target.copy(center);
  camera.far = d * 10; camera.updateProjectionMatrix();
  $("#note").textContent = tpl.note;
  $("#open").href = `index.html?glb=${encodeURIComponent(tpl.glb)}`;
  history.replaceState(null, "", `?t=${tpl.id}${speed !== 1 ? `&speed=${speed}` : ""}`);
  if (params.get("autoplay") === "0") { resetScene(); finish(); } else play();
}

async function boot() {
  const { templates } = await getJSON("../templates/index.json");
  $("#tpl").innerHTML = templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  const pick = () => templates.find((t) => t.id === $("#tpl").value);
  $("#tpl").value = templates.some((t) => t.id === params.get("t")) ? params.get("t") : templates[0].id;
  $("#tpl").addEventListener("change", () => load(pick()).catch(fail));
  $("#play").addEventListener("click", () => state.gltf && play());
  $("#skip").addEventListener("click", () => { if (state.gltf) { runId++; finish(); } });
  document.querySelectorAll("[data-speed]").forEach((b) => b.addEventListener("click", () => setSpeed(Number(b.dataset.speed))));
  addEventListener("keydown", (e) => { if (e.key === "r" && e.target === document.body) play(); });
  setSpeed(speed);
  drawSteps(null);
  await load(pick());
}
function fail(e) { replayError(String(e.message || e)); }
boot().catch(fail);
