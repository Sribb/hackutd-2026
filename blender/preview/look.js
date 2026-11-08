// Look-dev shared by the preview pages (index.html and generate.js): image-based light from CC0
// HDRIs (PMREM), Khronos PBR Neutral tone mapping, soft sun shadows, a sky dome and distance fog
// that meet at the same horizon color, a desert floor out to that horizon, a day / dusk / night
// toggle, and the moving parts animated the way docs/CONTRACT.md says. Nothing here edits the
// field's own nodes except lamp materials, which are cloned per mesh first so one lamp never
// lights another. Night follows docs/3D_QUALITY.md (#111): the moonlit HDRI at environment
// intensity 0.3, exposure 0.9, Neutral tone mapping so status colors stay true (AgX turns ALARM
// red pinkish), and shadow bias -0.0005 / normalBias 0.04 against acne on pads and tanks.
import * as THREE from "three";
import { RGBELoader } from "three/addons/loaders/RGBELoader.js";

const HDRI = { day: "../assets/hdri/goegap_1k.hdr", night: "../assets/hdri/rogland_moonlit_night_2k.hdr" };
// sun: compass bearing and elevation in degrees (0 = north = -Z in three.js, 90 = east = +X).
export const MODES = {
  day: { hdri: "day", env: 0.9, sky: 1.0, horizon: 0xcfc6b8, sun: [215, 52], sunColor: 0xfff0dc, sunI: 2.6,
         exposure: 1.0, fog: [1.2, 9], lamps: 0, ground: 1.0 },
  dusk: { hdri: "day", env: 0.32, sky: 0.42, skyTint: 0xffb68a, horizon: 0xd7967a, sun: [252, 7], sunColor: 0xffa060,
          sunI: 2.2, exposure: 1.3, fog: [0.9, 7], lamps: 0.45, ground: 0.95 },
  night: { hdri: "night", env: 0.3, sky: 0.35, horizon: 0x1a2232, sun: [150, 38], sunColor: 0x9db4ff, sunI: 0.45,
           exposure: 0.9, fog: [0.6, 5], lamps: 1, ground: 0.9 },
};
const LAMP_OK = new THREE.Color(0x8fa36b);  // the console's OK lamp color (web/src/main.js)
const WARM = new THREE.Color(0xffdcae);

const domeVert = `varying vec3 vDir;
void main() { vDir = normalize((modelMatrix * vec4(position, 0.0)).xyz);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
// The HDRI above, the fog color at and below the horizon. Tone mapped like every other material,
// then mixed with the fog color in output space, which is how three.js applies fog too.
const domeFrag = `uniform sampler2D map; uniform float hasMap, intensity, yaw; uniform vec3 tint, horizon, zenith;
varying vec3 vDir;
#include <common>
void main() {
  vec3 d = normalize(vDir);
  float c = cos(yaw), s = sin(yaw);
  vec3 r = vec3(c * d.x - s * d.z, d.y, s * d.x + c * d.z);
  vec2 uv = vec2(atan(r.z, r.x) * RECIPROCAL_PI2 + 0.5, asin(clamp(r.y, -1.0, 1.0)) * RECIPROCAL_PI + 0.5);
  vec3 sky = hasMap > 0.5 ? texture2D(map, uv).rgb * intensity * tint : mix(horizon, zenith, clamp(d.y * 2.5, 0.0, 1.0));
  gl_FragColor = vec4(sky, 1.0);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
  gl_FragColor.rgb = mix(gl_FragColor.rgb, horizon, 1.0 - smoothstep(0.0, 0.28, d.y));
}`;

function dirFrom(az, el) {  // compass bearing and elevation to a unit vector in three.js axes
  const a = THREE.MathUtils.degToRad(az), e = THREE.MathUtils.degToRad(el);
  return new THREE.Vector3(Math.sin(a) * Math.cos(e), Math.sin(e), -Math.cos(a) * Math.cos(e));
}

function brightestYaw(tex) {  // yaw of the HDRI's sun or moon, so the light can match it
  const { data, width, height } = tex.image;
  let best = -1, bi = 0;
  const half = data instanceof Uint16Array;
  for (let i = 0; i < width * height; i += 3) {
    const v = half ? THREE.DataUtils.fromHalfFloat(data[i * 4]) + THREE.DataUtils.fromHalfFloat(data[i * 4 + 1])
      : data[i * 4] + data[i * 4 + 1];
    if (v > best) { best = v; bi = i; }
  }
  const u = (bi % width) / width, v = Math.floor(bi / width) / height;
  return { phi: (u - 0.5) * 2 * Math.PI, el: (v - 0.5) * Math.PI };
}

export function createLook({ renderer, scene, camera, container, initial }) {
  renderer.toneMapping = THREE.NeutralToneMapping ?? THREE.ACESFilmicToneMapping;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  const pmrem = new THREE.PMREMGenerator(renderer);
  const loader = new RGBELoader();
  const cache = {};
  const loadHdri = (key) => (cache[key] ||= new Promise((ok, bad) => loader.load(HDRI[key], (tex) => {
    tex.mapping = THREE.EquirectangularReflectionMapping;
    ok({ tex, env: pmrem.fromEquirectangular(tex).texture, peak: brightestYaw(tex) });
  }, undefined, bad)));

  const domeMat = new THREE.ShaderMaterial({
    uniforms: { map: { value: null }, hasMap: { value: 0 }, intensity: { value: 1 }, yaw: { value: 0 },
                tint: { value: new THREE.Color(1, 1, 1) }, horizon: { value: new THREE.Color() }, zenith: { value: new THREE.Color(0x4a6a96) } },
    vertexShader: domeVert, fragmentShader: domeFrag, side: THREE.BackSide, depthWrite: false, depthTest: false, fog: false,
  });
  const dome = new THREE.Mesh(new THREE.SphereGeometry(1, 48, 24), domeMat);
  dome.name = "LOOK_sky"; dome.renderOrder = -1; dome.frustumCulled = false; dome.raycast = () => {};
  const floorMat = new THREE.MeshStandardMaterial({ color: 0x7a6248, roughness: 1, metalness: 0 });
  const floor = new THREE.Mesh(new THREE.CircleGeometry(1, 96), floorMat);
  floor.name = "LOOK_floor"; floor.rotation.x = -Math.PI / 2; floor.position.y = -0.45; floor.receiveShadow = true; floor.raycast = () => {};
  const sun = new THREE.DirectionalLight(0xffffff, 2);
  sun.name = "LOOK_sun"; sun.castShadow = true;
  Object.assign(sun.shadow, { bias: -0.0005, normalBias: 0.04, radius: 3 });  // no acne (3D_QUALITY.md)
  sun.shadow.mapSize.set(4096, 4096);
  const fill = new THREE.HemisphereLight(0xdfe8f0, 0x5a5048, 0);  // only used if an HDRI fails to load
  scene.add(dome, floor, sun, sun.target, fill);
  scene.fog = new THREE.Fog(0xffffff, 100, 1000);

  const state = { mode: null, field: null, span: 200, center: new THREE.Vector3(), lamps: [], spots: [], moving: null, ready: null };

  function fitToField() {
    const { span, center } = state;
    dome.scale.setScalar(Math.max(camera.far * 0.9, 10));
    floor.scale.setScalar(span * 30);
    floor.position.set(center.x, -0.45, center.z);
    const c = sun.shadow.camera, h = span * 0.62;
    Object.assign(c, { left: -h, right: h, top: h, bottom: -h, near: 1, far: span * 4 });
    c.updateProjectionMatrix();
    sun.shadow.normalBias = 0.04 * Math.max(1, span / 470);  // bigger shadow box, bigger texels (Avon is 470 m)
    sun.target.position.copy(center);
  }

  function attach(root) {
    state.field = root;
    root.updateMatrixWorld(true);
    const box = new THREE.Box3(), tanks = [], owners = {};
    root.traverse((o) => {
      if (o.userData?.asset_id) owners[o.userData.asset_id] = o;
      if (o.userData?.kind === "tank") tanks.push(o.getWorldPosition(new THREE.Vector3()));
      if (!o.isMesh) return;
      o.castShadow = !/^SITE_(ground|road)/.test(o.name);
      o.receiveShadow = true;
      let scenery = false;
      for (let p = o; p; p = p.parent) if (/^SCENERY/.test(p.name)) scenery = true;
      if (!scenery) box.expandByObject(o);
    });
    if (box.isEmpty()) box.setFromObject(root);
    const size = box.getSize(new THREE.Vector3());
    state.span = Math.max(size.x, size.z, 60);
    box.getCenter(state.center);
    sampleGround(root);
    for (const s of state.spots) { s.target.removeFromParent(); s.removeFromParent(); }
    state.lamps = []; state.spots = [];
    root.traverse((o) => {
      if (!o.isMesh || !/(_lamps|_light|_headlights|_beacon)$/.test(o.name) || !o.material?.emissive) return;
      o.material = o.material.clone();
      state.lamps.push(o);
      if (!/_lamps$/.test(o.name)) return;
      // A light tower lights its own pad: aim at the nearest tank, or at the disposal well it serves.
      const head = o.localToWorld(boxCenter(o));
      const owner = owners[o.name.replace(/_lamps$/, "")];
      const aim = owner ? owner.getWorldPosition(new THREE.Vector3())
        : tanks.reduce((a, b) => (a && a.distanceTo(head) < b.distanceTo(head) ? a : b), null) || head.clone().setY(0);
      addSpot(o, head, aim.setY(0));
    });
    state.moving = findMoving(root);
    fitToField();
    if (state.mode) apply(state.mode);
  }

  function boxCenter(mesh) {
    mesh.geometry.computeBoundingBox();
    return mesh.geometry.boundingBox.getCenter(new THREE.Vector3());
  }

  function addSpot(mesh, headWorld, aimWorld) {
    const spot = new THREE.SpotLight(WARM, 0, 80, 1.05, 0.7, 2);
    spot.position.copy(mesh.worldToLocal(headWorld.clone()));
    spot.target.position.copy(mesh.worldToLocal(aimWorld.clone()));
    spot.userData.full = 160;
    mesh.add(spot, spot.target);  // children of the tower, so they hide and move with it
    state.spots.push(spot);
  }

  function sampleGround(root) {  // the floor takes the lease ground's average color (texture times its color factor)
    let map = null, factor = null;
    root.traverse((o) => {
      if (!map && /^SITE_ground/.test(o.name) && o.material?.map?.image) { map = o.material.map.image; factor = o.material.color; }
    });
    if (!map) return;
    try {
      const cv = document.createElement("canvas"); cv.width = cv.height = 16;
      const g = cv.getContext("2d"); g.drawImage(map, 0, 0, 16, 16);
      const px = g.getImageData(0, 0, 16, 16).data;
      let r = 0, gg = 0, b = 0;
      for (let i = 0; i < px.length; i += 4) { r += px[i]; gg += px[i + 1]; b += px[i + 2]; }
      const n = px.length / 4;
      floorMat.color.setRGB(r / n / 255, gg / n / 255, b / n / 255, THREE.SRGBColorSpace).multiply(factor);
      floorMat.userData.base = floorMat.color.clone();
    } catch { /* keep the default tan */ }
  }

  async function apply(name) {
    const m = MODES[name] || MODES.day;
    state.mode = name in MODES ? name : "day";
    renderer.toneMappingExposure = m.exposure;
    const horizon = new THREE.Color(m.horizon);
    scene.fog.color.copy(horizon);
    scene.fog.near = state.span * m.fog[0];
    scene.fog.far = state.span * m.fog[1];
    domeMat.uniforms.horizon.value.copy(horizon).convertLinearToSRGB();
    domeMat.uniforms.zenith.value.set(name === "night" ? 0x0b1220 : 0x5d7fae);
    domeMat.uniforms.tint.value.set(m.skyTint ?? 0xffffff);
    domeMat.uniforms.intensity.value = m.sky;
    floorMat.color.copy(floorMat.userData.base || floorMat.color).multiplyScalar(m.ground);
    sun.color.set(m.sunColor);
    sun.intensity = m.sunI;
    sun.position.copy(state.center).add(dirFrom(...m.sun).multiplyScalar(state.span * 1.5));
    for (const l of state.lamps) {
      const tower = /_lamps$/.test(l.name), status = /_light$/.test(l.name);
      if (/_beacon$/.test(l.name)) l.material.emissive.setScalar(0);
      else l.material.emissive.copy(status ? LAMP_OK : WARM).multiplyScalar(m.lamps * (tower ? 3 : status ? 1.4 : 1.2));
    }
    for (const s of state.spots) s.intensity = s.userData.full * m.lamps;
    document.documentElement.dataset.look = state.mode;
    try { localStorage.setItem("preview-look", state.mode); } catch { /* private mode */ }
    for (const b of container.querySelectorAll(".look-toggle button")) b.setAttribute("aria-pressed", String(b.dataset.look === state.mode));
    try {
      const h = await loadHdri(m.hdri);
      if (state.mode !== name) return;
      scene.environment = h.env;
      scene.environmentIntensity = m.env;
      fill.intensity = 0;
      domeMat.uniforms.map.value = h.tex;
      domeMat.uniforms.hasMap.value = 1;
      // Turn the HDRI so its own sun or moon sits where our shadow-casting light is.
      const want = Math.atan2(sun.position.z - state.center.z, sun.position.x - state.center.x);
      const yaw = want - h.peak.phi;
      domeMat.uniforms.yaw.value = -yaw;
      scene.environmentRotation.set(0, -yaw, 0);
      state.ready = state.mode;
    } catch (e) {
      console.warn("HDRI did not load, using a plain sky", e);
      scene.environment = null;
      fill.intensity = name === "night" ? 0.25 : 1.4;
      domeMat.uniforms.hasMap.value = 0;
      state.ready = state.mode;
    }
  }

  function mountToggle() {
    if (!document.getElementById("look-style")) {
      const st = document.createElement("style");
      st.id = "look-style";
      st.textContent = `.look-toggle{position:absolute;top:12px;right:12px;z-index:3;display:flex;border:1px solid rgba(255,255,255,.18);border-radius:6px;overflow:hidden;background:rgba(21,25,30,.72);backdrop-filter:blur(4px)}
.look-toggle button{background:none;border:0;color:#dde3ea;font:12px/1 system-ui,sans-serif;padding:7px 11px;cursor:pointer}
.look-toggle button+button{border-left:1px solid rgba(255,255,255,.12)}
.look-toggle button[aria-pressed="true"]{background:#f2b33d;color:#1a1a1a;font-weight:600}
.look-toggle button:focus-visible{outline:2px solid #f2b33d;outline-offset:-2px}`;
      document.head.appendChild(st);
    }
    const bar = document.createElement("div");
    bar.className = "look-toggle"; bar.setAttribute("role", "group"); bar.setAttribute("aria-label", "Lighting");
    bar.innerHTML = Object.keys(MODES).map((k) => `<button type="button" data-look="${k}" aria-pressed="false">${k[0].toUpperCase()}${k.slice(1)}</button>`).join("");
    bar.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) apply(b.dataset.look); });
    container.appendChild(bar);
  }

  function update() {
    dome.position.copy(camera.position);
    dome.scale.setScalar(Math.max(camera.far * 0.9, 10));
  }

  mountToggle();
  let saved = null;
  try { saved = localStorage.getItem("preview-look"); } catch { /* private mode */ }
  apply(initial in MODES ? initial : saved in MODES ? saved : "day");
  // For headless checks: the live objects, and the mean color of the next rendered frame.
  Object.assign(state, { scene, renderer, camera, sun, dome, floor, modes: MODES, apply });
  state.sample = () => {
    renderer.render(scene, camera);
    const gl = renderer.getContext(), w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;
    const px = new Uint8Array(w * h * 4);
    gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
    const rows = (y0, y1) => { let s = 0, n = 0; for (let y = y0; y < y1; y++) for (let x = 0; x < w; x += 7) { const i = (y * w + x) * 4; s += px[i] + px[i + 1] + px[i + 2]; n += 3; } return s / n; };
    return { mean: rows(0, h), top: rows(Math.floor(h * 0.9), h), bottom: rows(0, Math.floor(h * 0.1)) };
  };
  window.__look = state;
  return { attach, apply, update, animate: (t, dt) => animateMoving(state.moving, t, dt), state };
}

// ---------- moving parts (docs/CONTRACT.md, "Moving parts") ----------
export function findMoving(root) {
  const parts = { rotors: [], beams: [], cranks: [] };
  let i = 0;
  root.traverse((o) => {
    const phase = (i++ * 2.399) % (2 * Math.PI);
    if (/_rotor$/.test(o.name)) parts.rotors.push(o);
    else if (/^WELL_\w+_beam$/.test(o.name) || o.parent?.userData?.kind === "well" && /_beam$/.test(o.name)) parts.beams.push({ o, phase });
    else if (/^WELL_\w+_crank$/.test(o.name) || o.parent?.userData?.kind === "well" && /_crank$/.test(o.name)) parts.cranks.push({ o, phase });
  });
  return parts;
}

export function animateMoving(parts, t, dt) {
  if (!parts) return;
  for (const o of parts.rotors) o.rotation.x += dt * 9;                                    // pump running
  for (const { o, phase } of parts.beams) o.rotation.x = 0.25 * Math.sin(t * 1.7 + phase);  // walking beam, about +-0.25 rad
  for (const { o, phase } of parts.cranks) o.rotation.x = t * 1.7 + phase;                 // crank turns with it
}
