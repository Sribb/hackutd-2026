// "your 3D model is being generated": a slow, full-screen replay of the field being built,
// ported from blender/preview/generate.js (the generation replay) for the start page. Same
// steps and the same model (/field.glb, the demo field the console opens on): the ground,
// road and pads fade in, the equipment rises type by type, then the pipe runs sweep across.
// Every count shown comes from the nodes in field.glb. Only the timing is slowed down.
//
//   const film = await loadFilm(hostEl)   loads three.js and the model, starts rendering
//   await film.play(onCaption)            runs the replay; onCaption(text) for each step
//   film.finish()                         jump to the built field (reduced motion, skip)
//   film.dispose()
//
// Lighting: a room environment for image-based light, a low warm key and a cool sky fill,
// fog the color of the page, so it reads as dusk without the preview's HDRI files.

import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { DRACOLoader } from "three/addons/loaders/DRACOLoader.js";
import { MeshoptDecoder } from "three/addons/libs/meshopt_decoder.module.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

const TYPES = [  // build order, the words used in captions, and the marker ring color and size
  ["tank", "tanks", 0xf2b33d, 4.5], ["pump", "pumps", 0x7fb2e5, 1.6], ["valve", "valves", 0xb9a3e3, 1.2],
  ["well", "wells", 0x9fd17a, 2.2], ["disposal_well", "disposal wells", 0xe5896f, 3.5], ["loadout", "load-outs", 0x8b96a3, 1.4],
  ["yard", "the yard", 0x8b96a3, 6], ["truck", "trucks", 0xdde3ea, 2.4], ["other", "the rest", 0x8b96a3, 1.5],
];
const KIND_BY_PREFIX = { TANK: "tank", PUMP: "pump", VALVE: "valve", WELL: "well", SWD: "disposal_well", LOADOUT: "loadout", YARD: "yard", TRUCK: "truck" };
const FOG = 0x0b0f16;

function classify(root) {  // same rules as generate.js
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

export async function loadFilm(host) {
  const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.localClippingEnabled = true;
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.domElement.setAttribute("aria-hidden", "true");
  host.prepend(renderer.domElement);

  const scene = new THREE.Scene();
  scene.fog = new THREE.Fog(FOG, 200, 1200);
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  scene.environmentIntensity = 0.45;
  scene.add(new THREE.HemisphereLight(0x9fb4e0, 0x2a2420, 1.1));
  const sun = new THREE.DirectionalLight(0xffd2a8, 2.1);
  sun.position.set(-300, 160, 120);
  scene.add(sun);
  const camera = new THREE.PerspectiveCamera(38, 1, 0.5, 6000);
  const markers = new THREE.Group();
  scene.add(markers);
  const pipePlane = new THREE.Plane(new THREE.Vector3(-1, 0, 0), 0);

  const resize = () => {
    const w = host.clientWidth || innerWidth, h = host.clientHeight || innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
  };
  addEventListener("resize", resize);
  resize();

  // ---- the model ----
  const draco = new DRACOLoader().setDecoderPath("/draco/");
  const gltf = await new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).setDRACOLoader(draco).loadAsync("/field.glb");
  const groups = {};
  for (const root of [...gltf.scene.children]) {
    const g = classify(root);
    (groups[g] = groups[g] || []).push(root);
    root.userData.p0 = root.position.clone();
    root.userData.s0 = root.scale.clone();
    root.traverse((o) => {
      if (!o.isMesh) return;
      o.material = Array.isArray(o.material) ? o.material.map((m) => m.clone()) : o.material.clone();
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      o.userData.m0 = mats.map((m) => ({ transparent: m.transparent, opacity: m.opacity }));
      if (g === "pipe") for (const m of mats) m.clippingPlanes = [pipePlane];
    });
  }
  for (const r of groups.hidden || []) r.visible = false;
  scene.add(gltf.scene);
  const box = new THREE.Box3();
  for (const [g, roots] of Object.entries(groups)) if (g !== "scenery" && g !== "hidden") for (const r of roots) box.expandByObject(r);
  if (box.isEmpty()) box.setFromObject(gltf.scene);
  const center = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3());
  const d = Math.max(size.x, size.z, 40) * 0.9;
  scene.fog.near = d * 1.1; scene.fog.far = d * 5;
  camera.far = d * 12; camera.updateProjectionMatrix();

  // ---- a clock that tweens and waits ----
  let simT = 0, tweens = [], waiters = [], runId = 0, built = false;
  const easeOut = (k) => 1 - (1 - k) ** 3;
  const easeBack = (k) => 1 + 1.6 * (k - 1) ** 3 + 0.6 * (k - 1) ** 2;  // a softer overshoot than the preview's
  const tween = (ms, fn, ease = easeOut) => tweens.push({ t0: simT, ms, fn, ease });
  const wait = (ms) => new Promise((r) => waiters.push({ at: simT + ms, r }));

  const meshesOf = (root, fn) => root.traverse((o) => {
    if (!o.isMesh) return;
    const mats = Array.isArray(o.material) ? o.material : [o.material];
    mats.forEach((m, i) => fn(m, o.userData.m0[i]));
  });
  const setOpacity = (root, k) => meshesOf(root, (m, m0) => { m.transparent = k < 1 || m0.transparent; m.opacity = m0.opacity * k; });
  const fadeIn = (root, ms) => { root.visible = true; setOpacity(root, 0); tween(ms, (k) => setOpacity(root, k)); };
  const rise = (root, ms) => {
    const { p0, s0 } = root.userData;
    root.visible = true;
    tween(ms, (k) => {
      root.scale.copy(s0).multiplyScalar(Math.max(0.001, k));
      root.position.set(p0.x, p0.y - 4 * (1 - Math.min(k, 1)), p0.z);
    }, easeBack);
  };

  function reset() {
    for (const [g, roots] of Object.entries(groups)) for (const r of roots) {
      if (g === "hidden") continue;
      r.visible = false;
      r.position.copy(r.userData.p0); r.scale.copy(r.userData.s0); setOpacity(r, 1);
    }
    markers.clear();
    pipePlane.constant = box.min.x - 10;
    built = false;
  }

  function finish() {
    runId++;
    tweens = []; waiters = [];
    for (const [g, roots] of Object.entries(groups)) for (const r of roots) {
      if (g === "hidden") continue;
      r.visible = true; r.position.copy(r.userData.p0); r.scale.copy(r.userData.s0); setOpacity(r, 1);
      meshesOf(r, (m) => { m.clippingPlanes = []; });
    }
    markers.clear();
    built = true;
  }

  const count = (kind) => (groups[kind] || []).length;
  const assets = TYPES.reduce((n, [k]) => n + count(k), 0);

  async function play(say = () => {}) {
    const id = ++runId;
    const alive = () => id === runId;
    tweens = []; waiters = [];
    reset();

    say(`reading the blueprint: ${assets} pieces of equipment found`);
    await wait(3200); if (!alive()) return;

    say("laying out the ground, the lease road and the pads");
    for (const g of ["site", "road", "scenery"]) for (const r of groups[g] || []) fadeIn(r, 1600);
    await wait(900); if (!alive()) return;
    for (const r of groups.pad || []) fadeIn(r, 1200);
    const marks = new Map();
    for (const [kind, , color, radius] of TYPES) for (const r of groups[kind] || []) {
      const ring = new THREE.Mesh(new THREE.RingGeometry(radius * 0.8, radius, 40),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false, fog: false }));
      ring.rotation.x = -Math.PI / 2;
      ring.position.set(r.userData.p0.x, 0.15, r.userData.p0.z);
      markers.add(ring); marks.set(r, ring);
      tween(900, (k) => { ring.material.opacity = 0.8 * k; });
    }
    await wait(2600); if (!alive()) return;

    for (const [kind, words] of TYPES) {
      const roots = groups[kind] || [];
      if (!roots.length) continue;
      say(`raising ${words}: ${roots.length}`);
      const gap = Math.min(180, 1500 / roots.length);
      for (const r of roots) {
        rise(r, 1000);
        const ring = marks.get(r);
        if (ring) tween(800, (k) => { ring.material.opacity = 0.8 * (1 - k); if (k >= 1) markers.remove(ring); });
        await wait(gap); if (!alive()) return;
      }
      await wait(500); if (!alive()) return;
    }

    const pipes = groups.pipe || [];
    if (pipes.length) {
      say(`piping it together: ${pipes.length} runs tie wells, tanks, pumps and disposal`);
      for (const r of pipes) r.visible = true;
      tween(3600, (k) => { pipePlane.constant = box.min.x - 10 + (box.max.x - box.min.x + 20) * k; });
      await wait(3800); if (!alive()) return;
    }
    finish();
  }

  reset();  // nothing shows until the replay lays it out

  // ---- render: a slow orbit around the lease ----
  const clock = new THREE.Clock();
  let ang = -0.9;
  renderer.setAnimationLoop(() => {
    // The replay runs on wall time, so a slow GPU drops frames instead of slowing the film.
    // Capped at 1 s so a hidden tab does not jump to the end when it comes back.
    const dt = Math.min(clock.getDelta(), 1);
    simT += dt * 1000;
    tweens = tweens.filter((t) => { const p = Math.min((simT - t.t0) / t.ms, 1); t.fn(t.ease(Math.max(p, 0))); return p < 1; });
    waiters = waiters.filter((w) => (simT >= w.at ? (w.r(), false) : true));
    if (!reduced) ang += Math.min(dt, 0.1) * (built ? 0.035 : 0.05);
    const narrow = camera.aspect < 0.8 ? 1.5 : 1;  // a phone in portrait needs the camera further out
    camera.position.set(center.x + Math.cos(ang) * d * 0.95 * narrow, d * 0.5 * narrow, center.z + Math.sin(ang) * d * 0.95 * narrow);
    camera.lookAt(center);
    renderer.render(scene, camera);
  });

  return {
    play, finish, assets,
    built: () => built,
    dispose() {
      runId++;
      renderer.setAnimationLoop(null);
      removeEventListener("resize", resize);
      draco.dispose(); pmrem.dispose(); renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
