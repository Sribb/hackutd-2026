// IoT on the 3D model (#80): the live layer (live.js, backend #57) drives the sensor and RTU
// status lights from #46, and highlights what needs a person.
//
//   SENSOR_<asset>_light     that asset's live reading NOW, whatever the time slider shows.
//                            Stale, missing, or no feed is UNKNOWN, never OK. An ALARM
//                            stays ALARM while the feed is up, as the backend sends it.
//   RTU_PAD_<n>_light        the pad's connectors: connected, or unavailable (blinks red).
//   live feed lost           every sensor and RTU light goes UNKNOWN until the feed is back.
//   active alert             its asset pulses through highlight.js (source "alert"; amber if
//                            every active alert is a WARNING).
//   plan run step (#63)      the asset of the step that waits on a person (source "run").
//   quiet hardware           a SENSOR_ node reading UNKNOWN, and an RTU whose link is down
//                            (source "sensor"), while the feed itself is up.
//
// Names and glTF extras follow #46 (blender/iot_parts.py): SENSOR_<asset> carries `for_asset`
// (or `attached_to` / `asset_id`, as parts.js reads them) and `connectors` (comma-separated
// IDs); RTU_PAD_<n> carries `pad` (or `tank_id`) and sits on PAD_<n>, the pad of TANK_<n>.
// Without extras, the node name says the same. A pad's RTU carries the connectors of every
// sensor on that pad's tank, pumps, discharge valves and wells; with none (sim only) it
// follows the simulator. If a node has no `connectors`, an asset whose live reason says
// "Lost the link to CONN_X" counts as down.
//
// Every highlight goes through highlight.js (#86/#101): this module never tints a mesh, it only
// sets the emissive of the status lights, in onBeforeRender, so they win over main.js's
// per-frame paint without main.js changing. highlight.js never lifts or dims status lights.
// Without SENSOR_/RTU_ nodes in the glb it paints nothing.

import * as THREE from "three";
import { highlight as manager } from "./highlight.js";

const COLOR = {
  OK: new THREE.Color(0x8fa36b),
  WARNING: new THREE.Color(0xc9a14a),
  ALARM: new THREE.Color(0xff4b1f),
  UNKNOWN: new THREE.Color(0x9a968e),
};
// Styles for highlight.js (#86, PR #101). Its own `alert`, `run` and `sensor` styles are the
// defaults; a WARNING-only alert set uses the amber color instead of red.
const WARN = { color: 0xc9a14a, note: "New warning." };
const DOWN = /unavail|down|error|fail|lost/i;

// One lamp's emissive for a status at time t (seconds).
function lamp(e, status, t) {
  if (status === "OFFLINE") return e.setScalar(0);
  if (status === "UNAVAILABLE") return e.copy(COLOR.ALARM).multiplyScalar(Math.sin(t * 7) > 0 ? 2.5 : 0.05);
  if (status === "ALARM") return e.copy(COLOR.ALARM).multiplyScalar(3 * (0.55 + 0.45 * Math.sin(t * 4)));
  if (status === "OK" || status === "WARNING") return e.copy(COLOR[status]).multiplyScalar(1.4);
  return e.copy(COLOR.UNKNOWN).multiplyScalar(Math.sin(t * 1.5) > 0 ? 1.2 : 0); // UNKNOWN and anything else
}

export function mountIot3d({ root, field, live, base = "", highlight = manager }) {
  const sensors = new Map(); // asset id -> [light mesh]
  const rtus = new Map();    // tank id -> [light mesh]
  const nodeOf = new Map();  // asset id or tank id -> its SENSOR_/RTU_ node name, for the sensor highlight
  const connOf = new Map();  // asset id -> [connector id], from the SENSOR_ node's extras
  const tankOf = new Map();  // asset id -> its pad's tank id
  const status = { sensors: {}, rtus: {}, alerts: [], run: null };
  const alerts = new Map();  // alert id -> alert, active only
  let run = null;            // the plan run on screen

  for (const t of (field && field.tanks) || []) tankOf.set(t.id, t.id);
  for (const p of (field && field.pumps) || []) {
    tankOf.set(p.id, p.tank_id);
    if (p.valve_id) tankOf.set(p.valve_id, p.tank_id);
  }
  for (const w of (field && field.wells) || []) tankOf.set(w.id, w.tank_id);

  function own(o) {
    if (!o.userData.iot3d) {
      o.material = o.material.clone(); // glTF shares one lamp material; light one lamp, not all
      o.userData.iot3d = { status: "UNKNOWN" };
      o.onBeforeRender = () => { if (o.material.emissive) lamp(o.material.emissive, o.userData.iot3d.status, performance.now() / 1000); };
    }
  }

  function scan() {
    sensors.clear();
    rtus.clear();
    root.traverse((o) => {
      if (!o.isMesh) return;
      const s = o.name.match(/^SENSOR_(.+)_light$/);
      const r = o.name.match(/^RTU_(?:PAD_)?(.+)_light$/);
      if (s) {
        const node = o.parent && o.parent.name === `SENSOR_${s[1]}` ? o.parent : o;
        const x = node.userData;
        const id = x.for_asset || x.attached_to || x.asset_id || s[1];
        const conns = String(node.userData.connectors || "").split(",").map((c) => c.trim()).filter(Boolean);
        if (conns.length) connOf.set(id, conns);
        own(o);
        sensors.set(id, [...(sensors.get(id) || []), o]);
        if (node !== o) nodeOf.set(id, node.name);
      } else if (r) {
        const node = o.parent && /^RTU_/.test(o.parent.name) ? o.parent : o;
        const x = node.userData;
        const n = x.pad ? String(x.pad).replace(/^PAD_/, "") : x.tank_id ? String(x.tank_id).replace(/^TANK_/, "") : r[1];
        own(o);
        rtus.set(`TANK_${n}`, [...(rtus.get(`TANK_${n}`) || []), o]);
        if (node !== o) nodeOf.set(`RTU:TANK_${n}`, node.name);
      }
    });
    update();
  }

  function update() {
    const L = live.live;
    const lost = !!L.lostSince;
    const down = new Set(L.connectors.filter((c) => DOWN.test(String(c.status))).map((c) => c.id));
    for (const [id, meshes] of sensors) {
      const r = live.reading(id);
      const raw = L.assets[id] || {};
      // No reading, a stale one, or no feed: UNKNOWN. Never the OK look without a fresh reading.
      // The backend never hides an ALARM behind a lost link or a quiet sensor (CONTRACT.md), so
      // neither does the light, as long as the feed itself is up.
      const st = lost || !L.available || !r ? "UNKNOWN" : raw.status === "ALARM" ? "ALARM" : r.stale ? "UNKNOWN" : r.status || "UNKNOWN";
      status.sensors[id] = st;
      for (const m of meshes) m.userData.iot3d.status = st;
    }
    for (const [tid, meshes] of rtus) {
      let st = lost || !L.available ? "UNKNOWN" : "OK";
      const conns = new Set();
      for (const [aid, t] of tankOf) if (t === tid) for (const c of connOf.get(aid) || []) conns.add(c);
      if (st === "OK") {
        if ([...conns].some((c) => down.has(c))) st = "UNAVAILABLE";
        // No connector IDs in the glb: fall back to what the backend says about the pad's assets.
        else if (!conns.size && [...tankOf].some(([aid, t]) => t === tid && /Lost the link to CONN_/.test(String((L.assets[aid] || {}).reason || "")))) st = "UNAVAILABLE";
      }
      status.rtus[tid] = st === "OK" ? "connected" : st === "UNAVAILABLE" ? "unavailable" : "UNKNOWN";
      for (const m of meshes) m.userData.iot3d.status = st;
    }
    // Point at the hardware that went quiet: a sensor node whose reading is UNKNOWN, and an RTU
    // whose link is down. Not while the whole feed is lost, when every node would light up.
    const quiet = lost || !L.available ? [] : [
      ...[...sensors.keys()].filter((id) => status.sensors[id] === "UNKNOWN" && nodeOf.has(id)).map((id) => nodeOf.get(id)),
      ...[...rtus.keys()].filter((tid) => status.rtus[tid] === "unavailable" && nodeOf.has(`RTU:${tid}`)).map((tid) => nodeOf.get(`RTU:${tid}`)),
    ];
    status.quiet = quiet;
    hl("sensor", quiet);
  }

  // ------------------------------------------------------------- highlights ----
  const shown = { alert: "", run: "", sensor: "" };
  function hl(source, ids, style) {
    const key = ids.length ? `${ids.join(",")}|${JSON.stringify(style || null)}` : "";
    if (key === shown[source]) return;
    shown[source] = key;
    if (!highlight) return;
    try {
      if (ids.length) highlight.set(source, ids, style);
      else highlight.clear(source);
    } catch (e) {
      console.warn("iot3d: highlight failed", e);
    }
  }
  function renderAlerts() {
    const list = [...alerts.values()].filter((a) => a.asset_id);
    status.alerts = [...new Set(list.map((a) => a.asset_id))];
    hl("alert", status.alerts, list.some((a) => a.severity === "ALARM") ? undefined : WARN);
  }
  // The step a person has to act on, else the one being verified, else the next one waiting.
  function currentStep(r) {
    if (!r || !["RUNNING", "PAUSED"].includes(r.status)) return null;
    const steps = r.steps || [];
    return steps.find((s) => s.status === "PENDING") || steps.find((s) => s.status === "VERIFYING" || s.status === "MISMATCH")
      || steps.find((s) => s.status === "WAITING") || null;
  }
  function renderRun() {
    const s = currentStep(run);
    const ids = s && s.action && s.action.asset_id ? [s.action.asset_id] : [];
    status.run = run ? { id: run.id, status: run.status, step: s ? s.n : null, asset_id: ids[0] || null } : null;
    hl("run", ids);
  }
  function onAlert(a) {
    if (!a || !a.id) return;
    if (a.active === false) alerts.delete(a.id);
    else alerts.set(a.id, a);
    renderAlerts();
  }
  function onRun(r) {
    if (!r || !r.id) return;
    if (["RUNNING", "PAUSED"].includes(r.status)) {
      // A RUNNING run wins. An offered re-plan (PAUSED, not started) doesn't hide a running one.
      if (!run || run.id === r.id || r.status === "RUNNING" || run.status !== "RUNNING") run = r;
    } else if (run && run.id === r.id) run = null;
    renderRun();
  }

  // Catch up once over REST, then follow /ws through live.js.
  async function catchUp() {
    if (!base) return;
    for (const [path, key, fn] of [["/api/alerts", "alerts", onAlert], ["/api/runs", "runs", onRun]]) {
      try {
        const r = await fetch(`${base}${path}`);
        if (r.ok) for (const x of (await r.json())[key] || []) fn(x);
      } catch { /* the feed-lost state covers this, or this backend has no plan runs */ }
    }
  }

  live.on("state", update);
  live.on("feed", update);
  live.on("alert", (m) => onAlert(m.alert));
  live.on("run", (m) => onRun(m.run));
  scan();
  catchUp();

  // For the headless checks and the console's dev tools.
  const api = {
    get status() { return status; },
    lights: () => ({ sensors: [...sensors.keys()], rtus: [...rtus.keys()] }),
    rescan: scan,
    highlight: !!highlight,
    // What highlight.js holds for this module's sources right now.
    shown: () => (highlight ? Object.fromEntries(["alert", "run", "sensor"].map((s) => [s, highlight.get(s)])) : {}),
  };
  window.__iot3d = api;
  return api;
}
