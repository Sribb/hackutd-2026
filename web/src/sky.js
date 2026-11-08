// Day and night for the 3D field (#85).
//
// The time of day is the console's field clock (the big clock) plus the forecast slider
// offset. The demo opens at night (1:47 AM), and dragging the slider toward 7:47 AM walks
// through astronomical, nautical and civil twilight into sunrise. The Sky control can pin a
// time of day or play a whole day instead, and it never touches the scenario clock.
//
// Sun: NOAA's solar position equations. Moon: the low-precision lunar series (Meeus,
// Astronomical Algorithms, ch. 47, truncated). Both run in the browser from the site's
// lat/lon (blueprint `site`, else Mentone, TX) and the date. No network calls.
//
// Changes with the sun: the sky dome, fog, hemisphere light, the key light (the moon light
// at night, the sun by day) and its shadows, exposure, how bright the site and scenery read,
// field lamps (*_lamps) and truck headlights (on below -2 degrees).
// Never changes: status lights (*_light, *_beacon) and the status colors on equipment.
//
// Test hooks: window.__freeboardSky = { state(), set(mode, minutes), events(), attach(root) }.

import * as THREE from "three";
import * as shiftlog from "./shiftlog.js";

export const DEFAULT_SITE = { name: "Mentone, TX", lat: 31.7, lon: -103.6, timezone: "America/Chicago" };
const LAMPS_ON_BELOW = -2;    // sun altitude in degrees
const PLAY_MIN_PER_SEC = 36;  // "Play a day" runs 24 hours in 40 seconds
const R = Math.PI / 180;
const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };

// ------------------------------------------------------------ astronomy ----
// Sun altitude and azimuth in degrees (azimuth clockwise from north), NOAA's equations.
export function sunPosition(ms, lat, lon) {
  const jc = (ms / 86400000 + 2440587.5 - 2451545) / 36525;
  const L0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360;
  const M = 357.52911 + jc * (35999.05029 - 0.0001537 * jc);
  const e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc);
  const C = Math.sin(M * R) * (1.914602 - jc * (0.004817 + 0.000014 * jc)) + Math.sin(2 * M * R) * (0.019993 - 0.000101 * jc) + Math.sin(3 * M * R) * 0.000289;
  const om = 125.04 - 1934.136 * jc;
  const lambda = L0 + C - 0.00569 - 0.00478 * Math.sin(om * R);
  const eps = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60 + 0.00256 * Math.cos(om * R);
  const decl = Math.asin(Math.sin(eps * R) * Math.sin(lambda * R));
  const y = Math.tan((eps * R) / 2) ** 2;
  const eqTime = (4 / R) * (y * Math.sin(2 * L0 * R) - 2 * e * Math.sin(M * R) + 4 * e * y * Math.sin(M * R) * Math.cos(2 * L0 * R)
    - 0.5 * y * y * Math.sin(4 * L0 * R) - 1.25 * e * e * Math.sin(2 * M * R));
  const tst = ((((ms / 60000) % 1440) + eqTime + 4 * lon) % 1440 + 1440) % 1440;
  const ha = (tst / 4 - 180) * R;
  const phi = lat * R;
  const zen = Math.acos(clamp(Math.sin(phi) * Math.sin(decl) + Math.cos(phi) * Math.cos(decl) * Math.cos(ha), -1, 1));
  let alt = 90 - zen / R;
  const te = Math.tan(alt * R);
  const refr = alt > 85 ? 0 : alt > 5 ? 58.1 / te - 0.07 / te ** 3 + 0.000086 / te ** 5
    : alt > -0.575 ? 1735 + alt * (-518.2 + alt * (103.4 + alt * (-12.79 + alt * 0.711))) : -20.772 / te;
  alt += refr / 3600;
  const den = Math.cos(phi) * Math.sin(zen);
  const a = den === 0 ? 0 : Math.acos(clamp((Math.sin(phi) * Math.cos(zen) - Math.sin(decl)) / den, -1, 1)) / R;
  return { alt, az: ha > 0 ? (a + 180) % 360 : (540 - a) % 360, lambda };
}

// Moon altitude, azimuth (degrees, clockwise from north) and lit fraction. Good to about a degree.
export function moonPosition(ms, lat, lon, sunLambda) {
  const d = ms / 86400000 + 2440587.5 - 2451545;
  const L = R * (218.316 + 13.176396 * d), M = R * (134.963 + 13.064993 * d), F = R * (93.272 + 13.22935 * d);
  const l = L + R * 6.289 * Math.sin(M), b = R * 5.128 * Math.sin(F), e = R * 23.4397;
  const ra = Math.atan2(Math.sin(l) * Math.cos(e) - Math.tan(b) * Math.sin(e), Math.cos(l));
  const dec = Math.asin(Math.sin(b) * Math.cos(e) + Math.cos(b) * Math.sin(e) * Math.sin(l));
  const H = R * (280.16 + 360.9856235 * d) + R * lon - ra, phi = R * lat;
  const alt = Math.asin(Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H)) / R;
  const az = (Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi)) / R + 540) % 360;
  const elong = Math.acos(clamp(Math.cos(b) * Math.cos(l - (sunLambda == null ? 0 : sunLambda * R)), -1, 1));
  return { alt, az, illum: (1 - Math.cos(elong)) / 2 };
}

// A direction in the scene for an altitude and azimuth. +X is east, -Z is north, Y is up.
export function skyDir(alt, az, out = new THREE.Vector3()) {
  const c = Math.cos(alt * R);
  return out.set(Math.sin(az * R) * c, Math.sin(alt * R), -Math.cos(az * R) * c);
}

export function phaseOf(alt, rising) {
  if (alt > 6) return "Day";
  if (alt > -0.833) return rising ? "Sunrise" : "Sunset";
  if (alt > -6) return "Civil twilight";
  if (alt > -12) return "Nautical twilight";
  if (alt > -18) return "Astronomical twilight";
  return "Night";
}

// ----------------------------------------------------------- local time ----
const fmts = new Map();
function wallClock(ms, tz) {
  if (!fmts.has(tz)) fmts.set(tz, new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric" }));
  const p = {};
  for (const x of fmts.get(tz).formatToParts(new Date(ms))) p[x.type] = Number(x.value);
  return p;
}
function offsetMin(ms, site) {
  try {
    const p = wallClock(ms, site.timezone);
    return Math.round((Date.UTC(p.year, p.month - 1, p.day, p.hour % 24, p.minute, p.second) - Math.floor(ms / 1000) * 1000) / 60000);
  } catch {
    return Math.round(site.lon / 15) * 60; // unknown timezone: solar time zone from the longitude
  }
}
// The UTC instant for a wall-clock time at the site. Minutes past 1440 roll into the next day.
export function localToUtc(day, minutes, site) {
  const guess = Date.UTC(day.y, day.mo - 1, day.d) + minutes * 60000;
  const first = guess - offsetMin(guess, site) * 60000;
  return guess - offsetMin(first, site) * 60000;
}
function todayAt(site) {
  const q = /[?&]skydate=(\d{4})-(\d{2})-(\d{2})/.exec(location.search);
  if (q) return { y: +q[1], mo: +q[2], d: +q[3] };
  try { const p = wallClock(Date.now(), site.timezone); return { y: p.year, mo: p.month, d: p.day }; }
  catch { const n = new Date(); return { y: n.getFullYear(), mo: n.getMonth() + 1, d: n.getDate() }; }
}
const clockText = (min) => {
  const x = ((Math.round(min) % 1440) + 1440) % 1440, h = Math.floor(x / 60);
  return `${h % 12 || 12}:${String(x % 60).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
};
function fieldMinutes() {
  const m = /(\d+):(\d+)(?::(\d+))?\s*(AM|PM)/.exec(shiftlog.clockNow(true));
  if (!m) return 0;
  return ((+m[1] % 12) + (m[4] === "PM" ? 12 : 0)) * 60 + +m[2] + (+m[3] || 0) / 60;
}

// ------------------------------------------------------------- palette ----
// Keyed by sun altitude. Everything at or below -18 degrees is Ronak's Night Survey look,
// value for value (background and fog 0x050505, hemisphere 0.7, moon light 1.25).
const KEY = (alt, zen, hor, hs, hg, hi, kc, ki, site, exp, stars, glow) => ({ alt, zen, hor, hs, hg, hi, kc, ki, site, exp, stars, glow });
const PALETTE = [
  KEY(-90, 0x050505, 0x050505, 0x8a96a8, 0x0b0b0a, 0.70, 0xc8d4e6, 1.25, 0.00, 1.00, 1.00, 0.0),
  KEY(-18, 0x050505, 0x050505, 0x8a96a8, 0x0b0b0a, 0.70, 0xc8d4e6, 1.25, 0.00, 1.00, 1.00, 0.0),
  KEY(-12, 0x05070d, 0x0b1019, 0x8a96a8, 0x0c0c0c, 0.74, 0xc4cfe4, 1.22, 0.04, 1.00, 0.70, 0.05),
  KEY(-6, 0x0b1326, 0x2a2a44, 0x8e98b6, 0x15130f, 0.86, 0xb4bedc, 1.10, 0.20, 1.03, 0.20, 0.25),
  KEY(-2, 0x1d3060, 0x92605a, 0x9ca6c6, 0x2a2218, 1.00, 0xffa272, 0.95, 0.42, 1.04, 0.04, 0.60),
  KEY(2, 0x385d9a, 0xe49a6c, 0xb2c0da, 0x4e3e2c, 1.20, 0xffb27a, 1.60, 0.70, 1.02, 0.00, 0.80),
  KEY(8, 0x4d7ec0, 0xe4c6a4, 0xc0d2ea, 0x6e5a40, 1.45, 0xffd8ae, 2.20, 0.90, 1.00, 0.00, 0.50),
  KEY(20, 0x4880cc, 0xc6d6e4, 0xc4d8f0, 0x86704f, 1.65, 0xfff1de, 2.60, 1.00, 1.00, 0.00, 0.25),
  KEY(90, 0x3e78c8, 0xbcd0e2, 0xc4d8f0, 0x8a7356, 1.70, 0xfff6ea, 2.75, 1.00, 1.00, 0.00, 0.20),
].map((k) => ({ ...k, zen: new THREE.Color(k.zen), hor: new THREE.Color(k.hor), hs: new THREE.Color(k.hs), hg: new THREE.Color(k.hg), kc: new THREE.Color(k.kc) }));
const COLOR_KEYS = ["zen", "hor", "hs", "hg", "kc"], NUM_KEYS = ["hi", "ki", "site", "exp", "stars", "glow"];
const look = { zen: new THREE.Color(), hor: new THREE.Color(), hs: new THREE.Color(), hg: new THREE.Color(), kc: new THREE.Color() };
function lookAt(alt) {
  let i = 0;
  while (i < PALETTE.length - 2 && alt > PALETTE[i + 1].alt) i++;
  const a = PALETTE[i], b = PALETTE[i + 1], k = clamp((alt - a.alt) / (b.alt - a.alt), 0, 1);
  for (const c of COLOR_KEYS) look[c].copy(a[c]).lerp(b[c], k);
  for (const n of NUM_KEYS) look[n] = a[n] + (b[n] - a[n]) * k;
  return look;
}

// ------------------------------------------------------------- sky dome ----
const DOME_VS = `varying vec3 vDir;
void main() { vDir = position; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const DOME_FS = `uniform vec3 zen; uniform vec3 hor; uniform vec3 gnd; uniform vec3 sunDir; uniform vec3 sunCol; uniform vec3 moonDir;
uniform float glow; uniform float stars; uniform float moonVis; uniform float time;
varying vec3 vDir;
float hash(vec3 p) { p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
void main() {
  vec3 d = normalize(vDir);
  float h = d.y;
  vec3 col = h > 0.0 ? mix(hor, zen, pow(h, 0.5)) : mix(hor, gnd, smoothstep(0.0, 0.06, -h));
  float cs = max(dot(d, sunDir), 0.0);
  col += sunCol * glow * (0.28 * pow(cs, 6.0) + 0.5 * pow(cs, 48.0)) * smoothstep(-0.3, 0.02, h);
  col += sunCol * 3.0 * smoothstep(0.99982, 0.99993, cs) * step(-0.02, h);
  if (stars > 0.0 && h > 0.0) {
    vec3 q = d * 420.0, c = floor(q);
    float s = hash(c);
    vec3 at = c + 0.5 + 0.3 * (vec3(hash(c + 1.7), hash(c + 3.1), hash(c + 5.3)) - 0.5);
    float tw = 0.65 + 0.35 * sin(time * (1.0 + 3.0 * s) + s * 40.0);
    col += vec3(0.55) * stars * tw * step(0.9965, s) * smoothstep(0.42, 0.0, length(q - at)) * smoothstep(0.0, 0.12, h);
  }
  float cm = dot(d, moonDir);
  if (moonVis > 0.0 && cm > 0.99968) {
    vec3 o = (d - moonDir * cm) / 0.0253;
    float r2 = dot(o, o);
    vec3 n = o - moonDir * sqrt(max(0.0, 1.0 - r2));
    float lit = smoothstep(-0.06, 0.12, dot(n, sunDir));
    col = mix(col, vec3(0.62, 0.64, 0.66) * (0.06 + 0.94 * lit), moonVis * smoothstep(1.0, 0.82, r2));
  }
  gl_FragColor = vec4(col, 1.0);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
}`;

// A soft warm pool of light on the ground under a light tower.
function poolTexture() {
  const c = Object.assign(document.createElement("canvas"), { width: 128, height: 128 });
  const g = c.getContext("2d"), r = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  r.addColorStop(0, "rgba(255,255,255,1)"); r.addColorStop(0.45, "rgba(255,255,255,.45)"); r.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = r; g.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(c);
}

// --------------------------------------------------------------- mount ----
// hooks: { scene, renderer, camera, key (the moon DirectionalLight), stage, site() -> field.site, offsetMin() -> forecast minutes }
export function mountSky(hooks) {
  const { scene, renderer, camera, key } = hooks;
  renderer.toneMapping = THREE.LinearToneMapping; // at exposure 1 this is pixel for pixel the same as no tone mapping
  const hemi = scene.children.find((o) => o.isHemisphereLight);
  const uni = {
    zen: { value: new THREE.Color() }, hor: { value: new THREE.Color() }, gnd: { value: new THREE.Color() },
    sunDir: { value: new THREE.Vector3(0, -1, 0) }, sunCol: { value: new THREE.Color() }, moonDir: { value: new THREE.Vector3(0, 1, 0) },
    glow: { value: 0 }, stars: { value: 1 }, moonVis: { value: 0 }, time: { value: 0 },
  };
  const dome = new THREE.Mesh(new THREE.SphereGeometry(1, 48, 24), new THREE.ShaderMaterial({
    uniforms: uni, vertexShader: DOME_VS, fragmentShader: DOME_FS, side: THREE.BackSide, depthWrite: false, depthTest: false, fog: false,
  }));
  Object.assign(dome, { name: "SKY_dome", renderOrder: -10, frustumCulled: false });
  dome.raycast = () => {};
  scene.add(dome);

  const site = { ...DEFAULT_SITE };
  let day = todayAt(site), mode = "field", pick = 13 * 60, play = 0, lampLevel = -1, lastSite = -1, built = false;
  const tracked = [];   // { mesh, day, night, own } site and scenery meshes whose brightness follows the sun
  const lamps = [];     // *_lamps meshes { mesh, base }
  const headlights = []; // TRUCK_n_headlights: main.js lights them on a job, the sky gates them to the dark
  const pools = [];
  const poolTex = poolTexture();
  let ground = null, groundNight = null;
  const groundDay = new THREE.Color(0.6, 0.53, 0.42).multiplyScalar(0.85); // M_GROUND, slightly darker so the pads read
  const now = { minutes: 0, utc: 0, sun: null, moon: null, phase: "Night", rising: false };
  const sunV = new THREE.Vector3(), moonV = new THREE.Vector3(), keyV = new THREE.Vector3(), nightV = new THREE.Vector3(-0.5, 1, 0.4).normalize();
  const dir = new THREE.Vector3(), q0 = new THREE.Quaternion(), q1 = new THREE.Quaternion(), ember = new THREE.Color(1, 0.55, 0.3);

  function readSite() {
    const s = (hooks.site && hooks.site()) || null;
    const lat = Number(s && s.lat), lon = Number(s && s.lon);
    const ok = s && Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180;
    let tz = (s && s.timezone) || (ok ? null : DEFAULT_SITE.timezone);
    if (tz) { try { new Intl.DateTimeFormat("en-US", { timeZone: tz }); } catch { tz = null; } }
    const next = ok ? { name: s.name || "the site", lat, lon, timezone: tz || "" } : { ...DEFAULT_SITE };
    if (next.lat !== site.lat || next.lon !== site.lon || next.timezone !== site.timezone) { Object.assign(site, next); day = todayAt(site); events.cache = null; }
  }

  // Before main.js indexes the model: remember each site and scenery mesh's daytime color.
  function attach(root) {
    root.updateMatrixWorld(true);
    root.traverse((o) => {
      if (!o.isMesh || !o.material || Array.isArray(o.material)) return;
      if (/_headlights$/.test(o.name)) headlights.push(o);
      if (/_lamps$/.test(o.name)) {
        lamps.push({ mesh: o, base: (o.material.emissive || new THREE.Color()).clone() });
        addPool(o);
      }
      for (let n = o; n && n !== root.parent; n = n.parent) {
        if (/^(SITE|PAD|SCENERY)_|^SCENERY$/.test(n.name) || (n.userData && n.userData.scenery)) {
          tracked.push({ mesh: o, mat: o.material, day: o.material.color.clone(), night: null });
          break;
        }
      }
    });
    lastSite = -1;
  }
  function addPool(lampMesh) {
    const box = new THREE.Box3().setFromObject(lampMesh);
    const c = box.getCenter(new THREE.Vector3());
    const tower = lampMesh.parent;
    const aim = tower ? new THREE.Vector3().setFromMatrixColumn(tower.matrixWorld, 0).setY(0) : new THREE.Vector3();
    if (aim.lengthSq() > 1e-6) c.add(aim.normalize().multiplyScalar(9));
    const baseY = tower ? tower.getWorldPosition(new THREE.Vector3()).y : box.min.y - 8;
    const m = new THREE.Mesh(new THREE.CircleGeometry(13, 40), new THREE.MeshBasicMaterial({
      map: poolTex, color: 0xffcf8a, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2, toneMapped: false, fog: false,
    }));
    m.rotation.x = -Math.PI / 2;
    m.position.set(c.x, baseY + 0.08, c.z);
    Object.assign(m, { name: "SKY_lamp_pool", renderOrder: 1 });
    m.raycast = () => {};
    m.visible = false;
    scene.add(m);
    pools.push(m);
  }

  // Sunrise, sunset and the three twilights for one local day, scanned at 2-minute steps.
  function events(dayIndex = 0) {
    const ck = `${site.lat},${site.lon},${site.timezone},${day.y}-${day.mo}-${day.d},${dayIndex}`;
    if (events.cache && events.cache.key === ck) return events.cache.out;
    const out = {};
    let prev = null, best = -99;
    for (let m = 0; m <= 1440; m += 2) {
      const a = sunPosition(localToUtc(day, dayIndex * 1440 + m, site), site.lat, site.lon).alt;
      if (a > best) { best = a; out.noon = m; }
      if (prev != null) {
        for (const [lim, up, dn] of [[-18, "astroDawn", "astroDusk"], [-12, "nauticalDawn", "nauticalDusk"], [-6, "civilDawn", "civilDusk"], [-0.833, "sunrise", "sunset"]]) {
          if (prev < lim && a >= lim) out[up] = m - 2 + (2 * (lim - prev)) / (a - prev);
          if (prev >= lim && a < lim) out[dn] = m - 2 + (2 * (prev - lim)) / (prev - a);
        }
      }
      prev = a;
    }
    events.cache = { key: ck, out };
    return out;
  }

  function minutesNow() {
    if (mode === "pick") return pick;
    if (mode === "play") return play;
    return fieldMinutes() + Number((hooks.offsetMin && hooks.offsetMin()) || 0);
  }

  // ------------------------------------------------------------ control ----
  const ctl = document.createElement("div");
  ctl.className = "sky-ctl";
  ctl.innerHTML = `<div class="sky-head"><span class="kicker">Sky</span><b id="skyTime">-</b><span id="skyPhase"></span></div>
    <div class="sky-sub" id="skySub"></div>
    <div class="sky-modes"><button data-mode="field" class="on" title="Follows the field clock and the forecast slider">Field clock</button><button data-mode="pick" title="Pick any time of day. The scenario clock does not change.">Pick a time</button><button data-mode="play" title="Plays 24 hours in 40 seconds">Play a day</button></div>
    <div class="sky-pick" id="skyPick" hidden><input id="skyRange" type="range" min="0" max="1435" step="5" aria-label="Time of day for the sky">
      <div class="sky-presets"><button data-at="sunrise">Sunrise</button><button data-at="noon">Noon</button><button data-at="sunset">Sunset</button><button data-at="civilDusk">Dusk</button><button data-at="midnight">Midnight</button></div></div>
    <div class="sky-sub muted" id="skyEvents"></div>`;
  (hooks.stage || document.body).appendChild(ctl);
  const $ = (id) => ctl.querySelector(`#${id}`);
  function setMode(m, minutes) {
    const was = minutesNow();
    mode = m;
    if (m === "pick") pick = minutes != null ? ((minutes % 1440) + 1440) % 1440 : ((Math.round(was / 5) * 5) % 1440 + 1440) % 1440;
    if (m === "play") play = minutes != null ? minutes : was;
    ctl.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
    $("skyPick").hidden = m === "field";
    $("skyRange").value = String(Math.round(pick));
  }
  // Every control applies at once, so the sky and its readout never lag a frame behind a click.
  const apply = (m, minutes) => { setMode(m, minutes); if (built) update(0); };
  ctl.querySelectorAll("[data-mode]").forEach((b) => { b.onclick = () => apply(b.dataset.mode); });
  $("skyRange").addEventListener("input", (e) => apply("pick", Number(e.target.value)));
  ctl.querySelectorAll("[data-at]").forEach((b) => {
    b.onclick = () => { const ev = events(Math.floor(now.minutes / 1440)); apply("pick", b.dataset.at === "midnight" ? 0 : Math.round(ev[b.dataset.at] ?? 720)); };
  });
  function place() {
    const bar = hooks.stage && hooks.stage.querySelector(".timebar");
    ctl.style.bottom = `${(bar ? bar.offsetHeight : 70) + 24}px`;
  }
  addEventListener("resize", place);
  place();

  // -------------------------------------------------------------- frame ----
  function update(dt = 0) {
    if (!built) { readSite(); built = true; }
    if (mode === "play") play += dt * PLAY_MIN_PER_SEC;
    const min = minutesNow();
    const utc = localToUtc(day, min, site);
    const sun = sunPosition(utc, site.lat, site.lon);
    const moon = moonPosition(utc, site.lat, site.lon, sun.lambda);
    const rising = sunPosition(utc + 600000, site.lat, site.lon).alt > sun.alt;
    Object.assign(now, { minutes: min, utc, sun, moon, rising, phase: phaseOf(sun.alt, rising) });
    const L = lookAt(sun.alt);
    skyDir(sun.alt, sun.az, sunV);
    skyDir(moon.alt, moon.az, moonV);

    // Sky, fog and background share the horizon color so the ground fades into the sky.
    uni.zen.value.copy(L.zen); uni.hor.value.copy(L.hor); uni.gnd.value.copy(L.hor);
    uni.sunDir.value.copy(sunV); uni.moonDir.value.copy(moonV);
    uni.sunCol.value.copy(L.kc).lerp(ember, 1 - smooth(-2, 12, sun.alt));
    uni.glow.value = L.glow; uni.stars.value = L.stars;
    uni.moonVis.value = smooth(-1, 3, moon.alt) * (1 - smooth(4, 20, sun.alt) * 0.7) * (0.25 + 0.75 * moon.illum);
    uni.time.value += dt;
    dome.position.copy(camera.position);
    dome.scale.setScalar(camera.far * 0.8);
    if (scene.background && scene.background.isColor) scene.background.copy(L.zen);
    if (scene.fog) scene.fog.color.copy(L.hor);
    if (hemi) { hemi.color.copy(L.hs); hemi.groundColor.copy(L.hg); hemi.intensity = L.hi; }
    renderer.toneMappingExposure = L.exp;

    // Key light: the moon light at night (moon direction when it's well up, else Ronak's
    // direction), swinging to the sun through dawn. Its elevation is kept off the horizon so
    // shadows stay on the field.
    if (key) {
      const wMoon = smooth(5, 25, moon.alt);
      keyV.copy(nightV).lerp(moonV.y > 0 ? moonV : nightV, wMoon).normalize();
      const sunLight = skyDir(Math.max(sun.alt, 7), sun.az, dir);
      const wSun = smooth(-4, 4, sun.alt);
      q0.setFromUnitVectors(keyV, sunLight);
      keyV.applyQuaternion(q1.identity().slerp(q0, wSun));
      key.color.copy(L.kc);
      key.intensity = L.ki;
      const half = key.shadow.camera.right || 60;
      key.position.copy(key.target.position).addScaledVector(keyV, half * 1.25);
    }

    // Site, pads and scenery: Ronak's darkened night colors at night, their own colors by day.
    if (Math.abs(L.site - lastSite) > 1e-3) {
      lastSite = L.site;
      for (const t of tracked) {
        if (!t.night) {
          if (t.mesh.material !== t.mat) t.night = t.mesh.material.color.clone();   // main.js already made it dark
          else { t.mesh.material = t.mesh.material.clone(); t.night = t.day.clone().multiplyScalar(0.28); }
        }
        t.mesh.material.color.copy(t.night).lerp(t.day, L.site);
      }
      ground = ground || scene.getObjectByName("ground");
      if (ground && ground.material) {
        groundNight = groundNight || ground.material.color.clone();
        ground.material.color.copy(groundNight).lerp(groundDay, L.site);
      }
    }

    // Field lamps and truck headlights: on below -2 degrees, with a short warm-up.
    const target = sun.alt < LAMPS_ON_BELOW ? 1 : 0;
    lampLevel = lampLevel < 0 ? target : lampLevel + (target - lampLevel) * Math.min(1, dt * 8);
    if (Math.abs(lampLevel - target) < 0.004) lampLevel = target;
    for (const l of lamps) {
      const e = l.mesh.material.emissive;
      if (!e) continue;
      // Lamps on an equipment asset are repainted by main.js every frame: add on top. Pad lamps aren't: set them.
      if (!l.mesh.userData.base) e.copy(l.base);
      e.r += 2.2 * lampLevel; e.g += 1.8 * lampLevel; e.b += 1.25 * lampLevel;
    }
    for (const p of pools) { p.visible = lampLevel > 0.01; p.material.opacity = 0.2 * lampLevel; }
    for (const h of headlights) if (h.material && h.material.emissive) h.material.emissive.multiplyScalar(lampLevel);

    if (hooks.stage) hooks.stage.dataset.sky = sun.alt > 2 ? "day" : sun.alt > -12 ? "twilight" : "night";
    draw();
  }

  let lastDraw = "";
  function draw() {
    const ev = events(Math.floor(now.minutes / 1440));
    const txt = `${clockText(now.minutes)}|${now.phase}|${Math.round(now.sun.alt)}|${Math.round(now.moon.alt)}|${lampLevel > 0.5}|${mode}`;
    if (txt === lastDraw) return;
    lastDraw = txt;
    $("skyTime").textContent = clockText(now.minutes);
    $("skyPhase").textContent = now.phase;
    $("skySub").textContent = `Sun ${Math.round(now.sun.alt)}°, moon ${Math.round(now.moon.alt)}° (${Math.round(now.moon.illum * 100)}% lit). Field lamps ${lampLevel > 0.5 ? "on" : "off"}.`;
    $("skyEvents").textContent = ev.sunrise != null && ev.sunset != null
      ? `${site.name}: sunrise ${clockText(ev.sunrise)}, sunset ${clockText(ev.sunset)}.` : `${site.name}: no sunrise or sunset today.`;
    if (mode !== "field") $("skyRange").value = String(Math.round(((now.minutes % 1440) + 1440) % 1440));
  }

  const api = {
    attach, update, events: () => events(Math.floor(now.minutes / 1440)),
    set: (m, minutes) => { setMode(m, minutes); update(0); },
    state: () => ({
      mode, minutes: now.minutes, clock: clockText(now.minutes), utc: new Date(now.utc).toISOString(), phase: now.phase,
      sunAlt: now.sun && now.sun.alt, sunAz: now.sun && now.sun.az, moonAlt: now.moon && now.moon.alt, moonIllum: now.moon && now.moon.illum,
      lampsOn: now.sun ? now.sun.alt < LAMPS_ON_BELOW : null, lampLevel,
      key: key ? { intensity: key.intensity, color: key.color.getHexString(), dir: keyV.toArray() } : null,
      hemi: hemi ? { intensity: hemi.intensity, sky: hemi.color.getHexString(), ground: hemi.groundColor.getHexString() } : null,
      fog: scene.fog ? scene.fog.color.getHexString() : null, exposure: renderer.toneMappingExposure, siteLevel: lastSite,
      site: { ...site }, tracked: tracked.length, lamps: lamps.length,
    }),
  };
  window.__freeboardSky = api;
  return api;
}
