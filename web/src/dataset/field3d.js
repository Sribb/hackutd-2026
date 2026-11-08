// 3D view of one system on the dataset page, built straight from network.json so it works
// for any folder (test/ included) with no model file. Facilities sit at their real
// lat/lon; each tank fills to its reading at the moment on screen, against rings at the
// warning and shutdown limits; pumps, wells and pipelines show their state.
// Color carries state only (ISA-101): gray is normal, amber is warning, red is alarm.
//
// Slot for the generated field: mount() takes an optional `glbUrl(systemId)`. When it
// answers with a URL that loads, that model replaces the built one, and assets are
// matched by node name (S1_TANK_A and so on).
//
// Loaded on demand (dynamic import) so the dataset page stays light until 3D is opened.

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { MeshoptDecoder } from "three/addons/libs/meshopt_decoder.module.js";

const C = {
  ground: 0x0b0b0a, pad: 0x1a1916, steel: 0x6d6a64, shell: 0x8a867d, ink: 0xe8e2d6,
  water: 0x3a5a78, warning: 0xc9a14a, alarm: 0xff4b1f, off: 0x2a2825, pipe: 0x4a4740, flow: 0x9fb9cf,
};
const KM = 10; // scene units per km

// glbUrl(systemId) -> { glb, ids } URLs, or null. ids is Sriyan's ids.json (#131).
export function mountField3d(host, { onPick, glbUrl } = {}) {
  host.innerHTML = "";
  const canvas = document.createElement("canvas");
  canvas.className = "ds-3d-canvas";
  host.appendChild(canvas);
  const tip = document.createElement("div");
  tip.className = "ds-3d-tip";
  tip.hidden = true;
  host.appendChild(tip);

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x050505);
  scene.fog = new THREE.Fog(0x050505, 120, 320);
  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 1000);
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.maxPolarAngle = Math.PI * 0.46;
  scene.add(new THREE.HemisphereLight(0x8a96a8, 0x0b0b0a, 0.9));
  const moon = new THREE.DirectionalLight(0xc8d4e6, 1.3);
  moon.position.set(-40, 80, 30);
  moon.castShadow = true;
  moon.shadow.mapSize.set(2048, 2048);
  moon.shadow.bias = -0.0005;
  moon.shadow.normalBias = 0.04;
  Object.assign(moon.shadow.camera, { left: -80, right: 80, top: 80, bottom: -80, far: 300 });
  scene.add(moon);
  const ground = new THREE.Mesh(new THREE.CircleGeometry(400, 64), new THREE.MeshStandardMaterial({ color: C.ground, roughness: 1 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  let root = null;
  let items = new Map(); // asset id -> { kind, mesh(es), facility }
  let pickables = [];
  let flows = [];
  let systemId = null;
  let mode = "built"; // built | glb
  let lastView = {};
  let buildSeq = 0;
  let raf = 0;
  let disposed = false;

  const mat = (color, extra = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.7, metalness: 0.2, ...extra });

  function clear() {
    if (!root) return;
    scene.remove(root);
    root.traverse((o) => { o.geometry?.dispose?.(); if (o.material) [].concat(o.material).forEach((m) => m.dispose()); });
    root = null;
    items = new Map();
    pickables = [];
    flows = [];
  }

  // Builds one system. `limits` = { warning, shutdown } in percent.
  // Shows the built view at once, then swaps in the generated field if one loads.
  function build(system, limits) {
    const seq = ++buildSeq;
    buildFromJson(system, limits);
    const urls = glbUrl ? glbUrl(system.id) : null;
    if (!urls) return;
    loadGlb(urls).then((m) => {
      if (!m || seq !== buildSeq) return;
      useGlb(m, system);
      update(lastView);
    }).catch((e) => console.warn("field.glb for", system.id, "not used:", e.message));
  }

  async function loadGlb({ glb, ids }) {
    const r = await fetch(ids, { cache: "no-store" });
    if (!r.ok) throw new Error(`ids ${r.status}`);
    const idMap = await r.json();
    const gltf = await new GLTFLoader().setMeshoptDecoder(MeshoptDecoder).loadAsync(glb);
    return { scene: gltf.scene, ids: idMap };
  }

  // The generated field: assets found by ids.json, each mesh gets its own material so a
  // state tint never spreads to a shared one.
  function useGlb({ scene: model, ids }, system) {
    clear();
    mode = "glb";
    systemId = system.id;
    root = model;
    root.traverse((o) => {
      if (o.isMesh) {
        o.castShadow = o.receiveShadow = true;
        o.material = [].concat(o.material).map((m) => m.clone());
        if (o.material.length === 1) o.material = o.material[0];
        for (const m of [].concat(o.material)) m.userData.base = { color: m.color?.clone(), emissive: m.emissive?.clone() };
      }
    });
    const node = (name) => (name ? root.getObjectByName(name) : null);
    const meshes = (o) => { const out = []; o?.traverse((x) => x.isMesh && out.push(x)); return out; };
    for (const [assetId, name] of Object.entries(ids.assets || {})) {
      const n = node(name);
      if (!n) continue;
      const kind = /_TANK_/.test(assetId) ? "tank" : /_PUMP_/.test(assetId) ? "pump" : /_WELL_/.test(assetId) ? "well"
        : /_PIPELINE_/.test(assetId) ? "pipeline" : /_FACILITY_/.test(assetId) ? "facility" : "other";
      const facility = (system.facilities.find((f) => [f.id, f.tank.id, f.pump.id, f.pipeline.id, ...f.wells.map((w) => w.id)].includes(assetId)) || {}).id || null;
      const it = { kind, facility, glb: true, meshes: meshes(n) };
      if (kind === "tank") {
        it.level = node(`${name}_LEVEL`);
        // A name and level tag over the tank, as in the built view.
        const tb = new THREE.Box3().setFromObject(n);
        it.label = makeLabel();
        it.label.scale.set(16, 4, 1);
        it.label.position.set((tb.min.x + tb.max.x) / 2, tb.max.y + 6, (tb.min.z + tb.max.z) / 2);
        it.name = assetId.replace(/^.*TANK_/, "");
        root.add(it.label);
      }
      if (kind === "pump") it.light = node(`${name}_light`);
      if (kind === "pipeline") it.meshes = (ids.pipes?.[assetId] || []).flatMap((p) => meshes(node(p))).concat(it.meshes);
      if (kind === "facility") continue;
      items.set(assetId, it);
      for (const m of it.meshes) { m.userData.id = assetId; m.userData.kind = kind; m.userData.facility = facility; pickables.push(m); }
    }
    scene.add(root);
    // Frame the equipment (pads and the disposal site), not the whole ground slab.
    const box = new THREE.Box3();
    for (const name of new Set([...Object.values(ids.assets || {}), ...Object.values(ids.pads || {})])) {
      const n = node(name);
      if (n) box.expandByObject(n);
    }
    if (box.isEmpty()) box.setFromObject(root);
    const c = box.getCenter(new THREE.Vector3()), size = box.getSize(new THREE.Vector3());
    const d = Math.max(size.x, size.z) * 1.05 + 12;
    for (const it of items.values()) if (it.label) { it.label.scale.set(d * 0.11, d * 0.0275, 1); it.label.position.y += d * 0.02; }
    camera.position.set(c.x - d * 0.3, d * 0.75, c.z + d * 0.85);
    controls.target.set(c.x, 0, c.z);
    controls.maxDistance = d * 4;
    camera.far = d * 10;
    camera.updateProjectionMatrix();
    scene.fog.near = d * 1.5;
    scene.fog.far = d * 4;
    // The generated field brings its own ground: hide ours so the two never z-fight, and
    // fit the moon's shadow box to the model (acne comes from a box that does not fit).
    ground.visible = false;
    const half = Math.max(size.x, size.z) * 0.6 + 5;
    Object.assign(moon.shadow.camera, { left: -half, right: half, top: half, bottom: -half, near: 1, far: half * 6 });
    moon.position.set(c.x - half * 0.5, half * 1.5, c.z + half * 0.4);
    moon.target.position.copy(c);
    scene.add(moon.target);
    moon.shadow.camera.updateProjectionMatrix();
    moon.shadow.normalBias = 0.04 * Math.max(1, half / 60);
  }

  function tint(meshes, tone) {
    for (const o of meshes) for (const m of [].concat(o.material)) {
      const b = m.userData.base || {};
      if (b.color) m.color.copy(b.color);
      if (b.emissive) m.emissive.copy(b.emissive);
      if (tone && m.emissive) { m.emissive.setHex(tone); m.emissiveIntensity = 0.45; }
      else if (m.emissive) m.emissiveIntensity = 1;
    }
  }

  function buildFromJson(system, limits) {
    clear();
    mode = "built";
    ground.visible = true;
    Object.assign(moon.shadow.camera, { left: -80, right: 80, top: 80, bottom: -80, near: 0.5, far: 300 });
    moon.position.set(-40, 80, 30);
    moon.target.position.set(0, 0, 0);
    moon.shadow.camera.updateProjectionMatrix();
    moon.shadow.normalBias = 0.04;
    systemId = system.id;
    root = new THREE.Group();
    root.name = system.id;
    const pts = [...system.facilities.map((f) => [f.lat, f.lon]), [system.disposal_site.lat, system.disposal_site.lon]];
    const lat0 = pts.reduce((n, p) => n + p[0], 0) / pts.length;
    const lon0 = pts.reduce((n, p) => n + p[1], 0) / pts.length;
    // True directions, but the spread is normalized so the farthest facility sits about
    // 42 units from the disposal site: equipment stays readable whatever the real distances.
    const kx0 = Math.cos((lat0 * Math.PI) / 180) * 111.32 * KM, kz0 = 110.57 * KM;
    const raw = (lat, lon) => new THREE.Vector3((lon - lon0) * kx0, 0, -(lat - lat0) * kz0);
    const d0 = raw(system.disposal_site.lat, system.disposal_site.lon);
    const far = Math.max(1e-6, ...system.facilities.map((f) => raw(f.lat, f.lon).distanceTo(d0)));
    const k = 42 / far;
    const P = (lat, lon) => raw(lat, lon).multiplyScalar(k);

    const disposal = P(system.disposal_site.lat, system.disposal_site.lon);
    const dsp = new THREE.Mesh(new THREE.CylinderGeometry(3.2, 3.6, 1.2, 24), mat(C.steel));
    dsp.position.copy(disposal).setY(0.6);
    dsp.castShadow = dsp.receiveShadow = true;
    dsp.userData = { id: system.disposal_site.id, kind: "disposal" };
    root.add(dsp);
    pickables.push(dsp);

    let span = 10;
    for (const f of system.facilities) {
      const at = P(f.lat, f.lon);
      span = Math.max(span, at.distanceTo(disposal));
      const g = new THREE.Group();
      g.position.copy(at);
      root.add(g);
      const pad = new THREE.Mesh(new THREE.BoxGeometry(14, 0.3, 11), mat(C.pad, { roughness: 1, metalness: 0 }));
      pad.position.y = 0.15;
      pad.receiveShadow = true;
      g.add(pad);

      // Tank: a translucent shell, the water inside at its level, rings at the limits.
      const r = 1.6 + Math.sqrt(f.tank.capacity_bbl / 1000) * 1.3;
      const h = 7;
      const shell = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, 32, 1, true),
        mat(C.shell, { transparent: true, opacity: 0.28, side: THREE.DoubleSide, depthWrite: false }));
      shell.position.y = h / 2 + 0.3;
      g.add(shell);
      const water = new THREE.Mesh(new THREE.CylinderGeometry(r * 0.97, r * 0.97, 1, 32), mat(C.water, { roughness: 0.3 }));
      water.castShadow = true;
      g.add(water);
      for (const [v, color] of [[limits.warning, C.warning], [limits.shutdown, C.alarm]]) {
        const ring = new THREE.Mesh(new THREE.TorusGeometry(r * 1.01, 0.06, 6, 48), new THREE.MeshBasicMaterial({ color }));
        ring.rotation.x = Math.PI / 2;
        ring.position.y = 0.3 + (v / 100) * h;
        g.add(ring);
      }
      const tankHit = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, 12), new THREE.MeshBasicMaterial({ visible: false }));
      tankHit.position.y = h / 2 + 0.3;
      tankHit.userData = { id: f.tank.id, kind: "tank", facility: f.id };
      g.add(tankHit);
      pickables.push(tankHit);
      const label = makeLabel();
      label.position.set(0, h + 3.2, 0);
      g.add(label);
      items.set(f.tank.id, { kind: "tank", water, h, facility: f.id, label, name: f.id.replace(/^.*FACILITY_/, "") });

      // Pump skid with a status lamp.
      const pump = new THREE.Mesh(new THREE.BoxGeometry(2.4, 1.4, 1.6), mat(C.steel));
      pump.position.set(r + 2.6, 1, 0);
      pump.castShadow = true;
      pump.userData = { id: f.pump.id, kind: "pump", facility: f.id };
      g.add(pump);
      pickables.push(pump);
      const lamp = new THREE.Mesh(new THREE.SphereGeometry(0.35, 12, 8), new THREE.MeshStandardMaterial({ color: 0x111111, emissive: C.ink, emissiveIntensity: 0 }));
      lamp.position.set(r + 2.6, 2.05, 0);
      g.add(lamp);
      items.set(f.pump.id, { kind: "pump", mesh: pump, lamp, facility: f.id });

      // Wells in an arc on the far side of the pad.
      const n = f.wells.length;
      f.wells.forEach((w, i) => {
        const a = Math.PI + (i - (n - 1) / 2) * 0.55;
        const head = new THREE.Mesh(new THREE.CylinderGeometry(0.35, 0.5, 2.2, 10), mat(C.steel));
        head.position.set(Math.cos(a) * (r + 4), 1.4, Math.sin(a) * (r + 4));
        head.castShadow = true;
        head.userData = { id: w.id, kind: "well", facility: f.id };
        g.add(head);
        pickables.push(head);
        items.set(w.id, { kind: "well", mesh: head, facility: f.id });
      });

      // Pipeline to the disposal site: a tube, and a flow strip that scrolls while water arrives.
      const from = at.clone().add(new THREE.Vector3(r + 3.8, 0.5, 0));
      const to = disposal.clone().setY(0.5);
      const curve = new THREE.LineCurve3(from, to);
      const tube = new THREE.Mesh(new THREE.TubeGeometry(curve, 1, 0.35, 8), mat(C.pipe));
      tube.castShadow = true;
      tube.userData = { id: f.pipeline.id, kind: "pipeline", facility: f.id };
      root.add(tube);
      pickables.push(tube);
      const len = from.distanceTo(to);
      const tex = flowTexture();
      tex.repeat.set(Math.max(1, len / 3), 1);
      const strip = new THREE.Mesh(new THREE.TubeGeometry(curve, 1, 0.37, 8),
        new THREE.MeshBasicMaterial({ map: tex, transparent: true, opacity: 0, depthWrite: false, color: C.flow }));
      root.add(strip);
      flows.push({ strip, tex });
      items.set(f.pipeline.id, { kind: "pipeline", mesh: tube, strip, tex, facility: f.id });
    }
    scene.add(root);
    const d = span * 1.25 + 22;
    camera.position.set(-d * 0.35, d * 0.75, d * 0.95);
    controls.target.set(0, 0, 0);
    controls.maxDistance = d * 3;
    controls.minDistance = 8;
    camera.far = d * 8;
    camera.updateProjectionMatrix();
    scene.fog.near = d * 1.4;
    scene.fog.far = d * 3.5;
  }

  // Text sprites for facility names and levels, redrawn only when the text changes.
  function makeLabel() {
    const c = document.createElement("canvas");
    c.width = 256; c.height = 64;
    const tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace;
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: false, transparent: true }));
    sp.scale.set(10, 2.5, 1);
    sp.renderOrder = 10;
    sp.userData = { canvas: c, tex, text: null };
    return sp;
  }
  function setLabel(sp, text, tone) {
    const key = text + "|" + (tone || "");
    if (sp.userData.text === key) return;
    sp.userData.text = key;
    const c = sp.userData.canvas, x = c.getContext("2d");
    x.clearRect(0, 0, c.width, c.height);
    x.fillStyle = "rgba(5,5,5,0.72)";
    x.fillRect(0, 8, c.width, 48);
    x.font = "600 28px 'Azeret Mono', ui-monospace, monospace";
    x.textAlign = "center";
    x.textBaseline = "middle";
    x.fillStyle = tone === "alarm" ? "#ff4b1f" : tone === "warning" ? "#c9a14a" : "#e8e2d6";
    x.fillText(text, c.width / 2, 33);
    sp.userData.tex.needsUpdate = true;
  }

  let flowTex = null;
  function flowTexture() {
    if (!flowTex) {
      const c = document.createElement("canvas");
      c.width = 64; c.height = 4;
      const x = c.getContext("2d");
      x.fillStyle = "rgba(0,0,0,0)"; x.fillRect(0, 0, 64, 4);
      x.fillStyle = "#fff"; x.fillRect(0, 0, 18, 4);
      flowTex = new THREE.CanvasTexture(c);
      flowTex.wrapS = flowTex.wrapT = THREE.RepeatWrapping;
    }
    const t = flowTex.clone();
    t.needsUpdate = true;
    return t;
  }

  // view: { [assetId]: { level, pump, flow, recv, well, tone } } at the moment on screen.
  // tone per asset: "" | "warning" | "alarm". null readings show as unknown (dim, no fill).
  function update(view) {
    lastView = view;
    for (const [id, it] of items) {
      const v = view[id] || {};
      const tone = v.tone ? (v.tone === "alarm" ? C.alarm : C.warning) : null;
      if (it.glb) {
        if (it.kind === "tank") {
          if (it.level) { const lvl = v.level; it.level.visible = lvl !== null && lvl !== undefined; if (it.level.visible) it.level.scale.y = Math.max(0.001, Math.min(1, lvl / 100)); }
          setLabel(it.label, `${it.name}  ${v.level === null || v.level === undefined ? "no level" : Math.round(v.level) + "%"}`, v.tone);
          tint(it.meshes, tone);
        } else if (it.kind === "pump") {
          tint(it.meshes, tone ?? (v.pump === 2 ? C.alarm : null));
          for (const m of it.light ? [].concat(it.light.material || []) : []) if (m.emissive) {
            m.emissive.setHex(v.pump === 2 ? C.alarm : v.pump === 1 ? C.ink : C.off);
            m.emissiveIntensity = v.pump === null || v.pump === undefined ? 0 : v.pump === 0 ? 0.3 : 1.6;
          }
        } else if (it.kind === "well") {
          tint(it.meshes, null);
          if (v.well === 0) for (const o of it.meshes) for (const m of [].concat(o.material)) m.color?.multiplyScalar(0.35);
        } else if (it.kind === "pipeline") {
          tint(it.meshes, tone);
        }
        continue;
      }
      if (it.kind === "tank") {
        const lvl = v.level;
        it.water.visible = lvl !== null && lvl !== undefined;
        if (it.water.visible) {
          const hh = Math.max(0.02, (Math.min(100, lvl) / 100) * it.h);
          it.water.scale.y = hh;
          it.water.position.y = 0.3 + hh / 2;
          it.water.material.color.setHex(tone ?? C.water);
          it.water.material.emissive?.setHex(tone ? tone : 0x000000);
          it.water.material.emissiveIntensity = tone ? 0.35 : 0;
        }
        setLabel(it.label, `${it.name}  ${lvl === null || lvl === undefined ? "no level" : Math.round(lvl) + "%"}`, v.tone);
      } else if (it.kind === "pump") {
        const s = v.pump;
        it.mesh.material.color.setHex(tone ?? (s === 2 ? C.alarm : C.steel));
        it.lamp.material.emissive.setHex(s === 2 ? C.alarm : s === 1 ? C.ink : C.off);
        it.lamp.material.emissiveIntensity = s === null || s === undefined ? 0 : s === 0 ? 0.3 : 1.6;
      } else if (it.kind === "well") {
        it.mesh.material.color.setHex(v.well === 0 ? C.off : C.steel);
      } else if (it.kind === "pipeline") {
        it.mesh.material.color.setHex(tone ?? C.pipe);
        it.strip.material.opacity = v.recv > 0 ? 0.55 : 0;
        it.strip.userData.speed = v.recv > 0 ? Math.min(2, v.recv / 2000) : 0;
      }
    }
  }

  // Tooltip and click on assets.
  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  const hit = (ev) => {
    const r = canvas.getBoundingClientRect();
    ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    return ray.intersectObjects(pickables, false)[0]?.object || null;
  };
  let tipText = null;
  canvas.addEventListener("pointermove", (ev) => {
    const o = hit(ev);
    canvas.style.cursor = o ? "pointer" : "default";
    if (!o) { tip.hidden = true; return; }
    const r = host.getBoundingClientRect();
    tip.textContent = tipText ? tipText(o.userData.id) : o.userData.id;
    tip.style.left = `${ev.clientX - r.left + 12}px`;
    tip.style.top = `${ev.clientY - r.top + 12}px`;
    tip.hidden = false;
  });
  canvas.addEventListener("pointerleave", () => (tip.hidden = true));
  let down = null;
  canvas.addEventListener("pointerdown", (ev) => (down = [ev.clientX, ev.clientY]));
  canvas.addEventListener("pointerup", (ev) => {
    if (!down || Math.hypot(ev.clientX - down[0], ev.clientY - down[1]) > 5) return;
    const o = hit(ev);
    if (o && onPick) onPick(o.userData.facility || null, o.userData.id);
  });

  function resize() {
    const w = host.clientWidth || 600, h = host.clientHeight || 420;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(host);
  resize();

  const clock = new THREE.Clock();
  const loop = () => {
    if (disposed) return;
    raf = requestAnimationFrame(loop);
    if (!host.isConnected || host.offsetParent === null) return; // hidden: no work
    const dt = clock.getDelta();
    for (const f of flows) f.tex.offset.x -= dt * (f.strip.userData.speed || 0);
    controls.update();
    renderer.render(scene, camera);
  };
  loop();

  return {
    build,
    get mode() { return mode; },
    debug: () => ({ scene, camera, controls, root }),
    update,
    setTip(fn) { tipText = fn; },
    get system() { return systemId; },
    glbUrl,
    dispose() {
      disposed = true;
      cancelAnimationFrame(raf);
      ro.disconnect();
      clear();
      renderer.dispose();
    },
  };
}
