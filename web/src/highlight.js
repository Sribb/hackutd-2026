// One highlight manager for the 3D field (#86). Every source of emphasis goes through here,
// so two sources never fight over the same mesh.
//
//   import { highlight } from "./highlight.js";
//
//   highlight.set(source, ids, style?)    Show `ids` for `source`, replacing what that source showed.
//       ids    asset IDs ("TANK_3"), part or node names ("TANK_3_lt", "SENSOR_TANK_3_LT",
//              "PIPE_TANK_3_PUMP_3"), or Object3D nodes. Unknown names are ignored.
//       style  a name from STYLES ("alarm", "run", ...) or an object
//              { color: 0xRRGGBB, priority, level, emissive, pulse, dimOthers, note }.
//              Default: the style with the same name as the source, else "focus".
//   highlight.clear(source)               Stop showing that source.
//   highlight.flash(source, ids, ms, style?)  set(), then clear() after ms.
//   highlight.get(source)  highlight.sources()  highlight.notes(assetId)   read back.
//
// Priorities. On each mesh the highest priority wins the outline color and fill. Emissive
// lifts add up (so trying + selected look exactly as they did in main.js before).
//   part 100 > hover 90 > jarvis 80 = phone 80 > selection 70 > run 60 > trying 50
//   > alarm 40 = alert 40 > sensor 35 > related 20
// inspect.js drives hover, part, selection, related, trying, alarm, jarvis and phone.
// Other modules own their sources: iot3d.js uses run (plan-run steps from /ws `run`,
// #75), alert (new alerts) and sensor (SENSOR_/RTU_ lights). Any new source name works.
//
// Status lights (*_light, *_beacon, *_headlights, *_lamps) never get a lift or a dim: they
// always show status. Scenery, the site and roads are never highlighted.
//
// Drawing: proxies of the highlighted meshes (same geometry and world matrix, flat color) are
// drawn from their own small scene into a mask target, and one screen pass draws a 2 px
// outline wherever a mesh's level rises above its neighbor's. The main scene, its lights and
// its materials are never touched, so three.js never re-checks the ~600 lit materials.
// Outlines show through anything in front, so a valve behind a tank is still easy to find.

import * as THREE from "three";

const C = (hex) => new THREE.Color(hex);
export const STYLES = {
  part:      { priority: 100, color: 0xffffff, level: 1.0, emissive: C(0x34383e) },
  hover:     { priority: 90, color: 0xe8e2d6, level: 0.5 },
  jarvis:    { priority: 80, color: 0xb9a7f0, level: 0.5, emissive: C(0x1c1830), pulse: 1.2, note: "gaia pointed here." },
  phone:     { priority: 80, color: 0xb9a7f0, level: 0.5, emissive: C(0x1c1830), pulse: 1.2, note: "Shown from a phone." },
  selection: { priority: 70, color: 0xe8e2d6, level: 0.5, emissive: C(0x2a2f36), dimOthers: true },
  run:       { priority: 60, color: 0x8fa36b, level: 0.5, pulse: 1.6, note: "Plan step." },
  trying:    { priority: 50, color: 0x8fb8c8, level: 0.5, emissive: C(0x8fb8c8).multiplyScalar(0.35), note: "Part of the what-if." },
  alarm:     { priority: 40, color: 0xff4b1f, level: 0.5, pulse: 4 },
  alert:     { priority: 40, color: 0xff4b1f, level: 0.5, pulse: 4, note: "New alert." },
  sensor:    { priority: 35, color: 0xc9a14a, level: 0.5, pulse: 2 },
  related:   { priority: 20, color: 0x8d877d, level: 0.25, note: "Connected to the selection." },
  focus:     { priority: 75, color: 0xb9a7f0, level: 0.5 },
};
const LAMP = /_(light|beacon|headlights|lamps)$/;
const DIM = 0.4;

const sources = new Map(); // name -> { ids, style, meshes, timer }
let ctx = null;            // { renderer, scene, camera, resolve(id) -> meshes[], dimmable() -> meshes[] }
let version = 0, built = -1;
let winners = new Map();   // mesh -> { style, emissive: Color|null }
const pulses = [];

function styleOf(source, style) {
  const base = typeof style === "string" ? STYLES[style] : style ? { ...(STYLES[source] || STYLES.focus), ...style } : STYLES[source] || STYLES.focus;
  return { ...(base || STYLES.focus), emissive: base && base.emissive ? new THREE.Color(base.emissive) : null };
}

export const highlight = {
  set(source, ids, style) {
    const list = (Array.isArray(ids) ? ids : ids == null ? [] : [ids]).filter((x) => x != null);
    const prev = sources.get(source);
    if (prev && prev.timer) clearTimeout(prev.timer);
    if (!list.length) { this.clear(source); return; }
    sources.set(source, { ids: list, style: styleOf(source, style), meshes: null });
    version++;
  },
  clear(source) {
    const prev = sources.get(source);
    if (!prev) return;
    if (prev.timer) clearTimeout(prev.timer);
    sources.delete(source);
    version++;
  },
  flash(source, ids, ms = 6000, style) {
    this.set(source, ids, style);
    const s = sources.get(source);
    if (s) s.timer = setTimeout(() => { if (sources.get(source) === s) this.clear(source); }, ms);
  },
  get(source) { const s = sources.get(source); return s ? s.ids.map((x) => (typeof x === "string" ? x : x.name)) : []; },
  sources() { return [...sources.keys()]; },
  // Notes from every source that covers this asset, for the tooltip ("gaia pointed here.").
  notes(assetId) {
    const out = [];
    for (const [, s] of sources) if (s.style.note && s.ids.includes(assetId) && !out.includes(s.style.note)) out.push(s.style.note);
    return out;
  },
  // The style that won on a mesh this frame, for tests: { source, priority } or null.
  winner(mesh) { const w = winners.get(mesh); return w ? { source: w.source, priority: w.style.priority } : null; },
  refresh() { version++; },
};

// main.js (through inspect.js) binds the renderer once the model is in.
export function bindHighlight(c) { ctx = c; version++; }

function rebuild() {
  built = version;
  winners = new Map();
  pulses.length = 0;
  for (const [name, s] of sources) {
    const meshes = new Set();
    for (const id of s.ids) for (const m of ctx.resolve(id)) meshes.add(m);
    s.meshes = meshes;
    if (s.style.pulse) pulses.push(s.style);
    for (const m of meshes) {
      const w = winners.get(m);
      const lift = s.style.emissive && !LAMP.test(m.name) ? s.style.emissive : null;
      if (!w) winners.set(m, { style: s.style, source: name, emissive: lift ? lift.clone() : null });
      else {
        if (lift) w.emissive = w.emissive ? w.emissive.add(lift) : lift.clone();
        if (s.style.priority > w.style.priority) Object.assign(w, { style: s.style, source: name });
      }
    }
  }
  // One flat proxy per highlighted mesh, in the mask scene.
  for (const [mesh, p] of proxies) if (!winners.has(mesh)) { maskScene.remove(p); proxies.delete(mesh); }
  for (const [mesh, w] of winners) {
    let p = proxies.get(mesh);
    if (!p) {
      p = new THREE.Mesh(mesh.geometry, maskMat(w.style));
      p.matrixAutoUpdate = false;
      p.matrixWorldAutoUpdate = false;
      proxies.set(mesh, p);
      maskScene.add(p);
    }
    p.material = maskMat(w.style);
  }
}

// After main.js paint() and before the render: lifts and dimming. Painted meshes (the ones
// main.js resets from userData.base every frame) are the only ones touched, so nothing drifts.
export function applyHighlight() {
  if (!ctx) return;
  if (built !== version) rebuild();
  for (const [m, w] of winners) if (w.emissive && m.userData.base && m.material.emissive) m.material.emissive.add(w.emissive);
  let dim = false;
  for (const [, s] of sources) if (s.style.dimOthers) dim = true;
  if (dim) for (const m of ctx.dimmable()) if (!winners.has(m) && !LAMP.test(m.name) && m.userData.base) m.material.color.multiplyScalar(DIM);
}

// ------------------------------------------------------------- outline ----
let rt = null, quad = null;
const maskScene = new THREE.Scene();
maskScene.matrixWorldAutoUpdate = false; // proxies copy their mesh's world matrix each frame
const proxies = new Map();                // highlighted mesh -> flat proxy in maskScene
const quadScene = new THREE.Scene(), quadCam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
const maskMats = new Map();
const size = new THREE.Vector2(), clearColor = new THREE.Color();
const sph = new THREE.Sphere(), cam = new THREE.Vector3(), ndc = new THREE.Vector3();
const OFFS = Array.from({ length: 16 }, (_, i) => { const a = (i * Math.PI) / 8, r = i % 2 ? 0.55 : 1; return new THREE.Vector2(Math.cos(a) * r, Math.sin(a) * r); });

// One flat material per look (color, level, pulse), shared by every source with that look.
function maskMat(style) {
  const key = `${style.color}|${style.level}|${style.pulse || 0}`;
  if (maskMats.has(key)) return maskMats.get(key);
  const m = new THREE.ShaderMaterial({
    uniforms: { tint: { value: new THREE.Vector4(((style.color >> 16) & 255) / 255, ((style.color >> 8) & 255) / 255, (style.color & 255) / 255, style.level) } },
    vertexShader: "void main() { gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }",
    fragmentShader: "uniform vec4 tint; void main() { gl_FragColor = tint; }",
    fog: false, toneMapped: false, side: THREE.DoubleSide,
  });
  m.userData.rgb = m.uniforms.tint.value.clone();
  maskMats.set(key, m);
  return m;
}
const shown = (o) => { for (let n = o; n; n = n.parent) if (!n.visible) return false; return true; };

function ensureTargets(w, h) {
  if (!rt) {
    rt = new THREE.WebGLRenderTarget(w, h, { minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter, depthBuffer: true });
    quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), new THREE.ShaderMaterial({
      uniforms: { mask: { value: rt.texture }, texel: { value: new THREE.Vector2() }, width: { value: 2 }, offs: { value: OFFS } },
      vertexShader: "varying vec2 vUv; void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }",
      fragmentShader: `uniform sampler2D mask; uniform vec2 texel; uniform float width; uniform vec2 offs[16]; varying vec2 vUv;
        void main() {
          vec4 c = texture2D(mask, vUv);
          vec4 best = vec4(0.0); float hits = 0.0;
          for (int i = 0; i < 16; i++) {
            vec4 n = texture2D(mask, vUv + offs[i] * texel * width);
            if (n.a > c.a + 0.05) { hits += 1.0; if (n.a > best.a) best = n; }
          }
          float edge = clamp(hits / 3.0, 0.0, 1.0);
          float fill = c.a > 0.95 ? 0.2 : c.a > 0.45 ? 0.05 : 0.0;
          gl_FragColor = edge > 0.0 ? vec4(best.rgb, 0.95 * edge) : vec4(c.rgb, fill);
        }`,
      transparent: true, depthTest: false, depthWrite: false, toneMapped: false,
    }));
    quad.frustumCulled = false;
    quadScene.add(quad);
  }
  if (rt.width !== w || rt.height !== h) rt.setSize(w, h);
}

// After main.js renders the frame: draw the outlines on top. One mask render of only the
// highlighted meshes plus one screen pass, and nothing at all when no source is active.
export function drawOutlines(time) {
  if (!ctx || !winners.size) return;
  const { renderer, camera } = ctx;
  renderer.getDrawingBufferSize(size);
  const k = renderer.getPixelRatio() >= 1.5 ? 0.5 : 1;
  const w = Math.max(1, Math.round(size.x * k)), h = Math.max(1, Math.round(size.y * k));
  ensureTargets(w, h);
  for (const s of pulses) {
    const m = maskMat(s);
    const p = 0.55 + 0.45 * Math.sin(time * s.pulse);
    m.uniforms.tint.value.set(m.userData.rgb.x * p, m.userData.rgb.y * p, m.userData.rgb.z * p, s.level);
  }
  // The screen pass only runs inside the box that holds every outline (CSS px, from the
  // proxies' bounding spheres), so a few small highlights cost a few small rectangles of work.
  const cw = size.x / renderer.getPixelRatio(), chh = size.y / renderer.getPixelRatio();
  const focal = chh / 2 / Math.tan((camera.fov * Math.PI) / 360);
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, all = false;
  camera.updateMatrixWorld();
  for (const [mesh, p] of proxies) {
    p.visible = shown(mesh);
    if (!p.visible) continue;
    p.geometry = mesh.geometry;
    p.matrixWorld.copy(mesh.matrixWorld);
    if (all) continue;
    if (!p.geometry.boundingSphere) p.geometry.computeBoundingSphere();
    sph.copy(p.geometry.boundingSphere).applyMatrix4(p.matrixWorld);
    cam.copy(sph.center).applyMatrix4(camera.matrixWorldInverse);
    if (-cam.z <= sph.radius + camera.near) { all = true; continue; }
    ndc.copy(sph.center).project(camera);
    const rr = (sph.radius * focal) / -cam.z, sx = ((ndc.x + 1) / 2) * cw, sy = ((1 - ndc.y) / 2) * chh;
    x0 = Math.min(x0, sx - rr); x1 = Math.max(x1, sx + rr); y0 = Math.min(y0, sy - rr); y1 = Math.max(y1, sy + rr);
  }
  const target = renderer.getRenderTarget(), auto = renderer.autoClear, ca = renderer.getClearAlpha();
  renderer.getClearColor(clearColor);
  renderer.setRenderTarget(rt);
  renderer.setClearColor(0x000000, 0);
  renderer.clear(true, true, false);
  try {
    renderer.render(maskScene, camera);
  } finally {
    renderer.setRenderTarget(target);
    renderer.setClearColor(clearColor, ca);
  }
  quad.material.uniforms.texel.value.set(1 / w, 1 / h);
  quad.material.uniforms.width.value = 2;
  const pad = 8;
  x0 = Math.max(0, Math.floor(x0 - pad)); y0 = Math.max(0, Math.floor(y0 - pad));
  x1 = Math.min(cw, Math.ceil(x1 + pad)); y1 = Math.min(chh, Math.ceil(y1 + pad));
  if (!all && (x1 <= x0 || y1 <= y0)) return; // every outline is off screen
  renderer.autoClear = false;
  if (!all) { renderer.setScissor(x0, chh - y1, x1 - x0, y1 - y0); renderer.setScissorTest(true); }
  renderer.render(quadScene, quadCam);
  renderer.setScissorTest(false);
  renderer.autoClear = auto;
  lastScissor = all ? 1 : ((x1 - x0) * (y1 - y0)) / (cw * chh);
}
let lastScissor = 0;
// Share of the screen the last outline pass covered (0 to 1), for the frame-time checks.
export const outlineCoverage = () => lastScissor;
