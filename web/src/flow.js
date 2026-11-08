// Flowing pipes (docs/3D_QUALITY.md, method 8). A faint stripe scrolls along a pipe in
// the direction the water moves, only while something is pushing water through it:
//   PIPE_TANK_n_PUMP_n, PIPE_PUMP_n_VALVE_D_n, PIPE_VALVE_D_n_TRUNK   pump n running and
//                                                                      its discharge valve open
//   PIPE_WELL_k_TANK_n                                                 well k producing
//   PIPE_INLET_n, PIPE_INLET_n_RISER                                   any well on tank n producing
//   PIPE_TRUNK_k                                                       a running pump upstream of it
// A stopped or failed pump freezes its stripes and fades them out, so flow is information,
// not decoration. Load-out and crossover lines stay still: nothing in the feed says when
// they carry water.
//
// The stripe is a world-space patch on the pipe's own material (onBeforeCompile), so it
// needs no UVs and no extra draw calls.

import * as THREE from "three";

const COLOR = new THREE.Color(0.1, 0.55, 0.75);
const SPEED = 2.4; // metres per second along the pipe
const SPACING = 4; // metres between stripes

function patch(mat, u) {
  mat.onBeforeCompile = (sh) => {
    sh.uniforms.uFlowDir = u.dir;
    sh.uniforms.uFlowPhase = u.phase;
    sh.uniforms.uFlowOn = u.on;
    sh.vertexShader = sh.vertexShader
      .replace("#include <common>", "#include <common>\nvarying vec3 vFlowW;")
      .replace("#include <worldpos_vertex>", "#include <worldpos_vertex>\nvFlowW = (modelMatrix * vec4(transformed, 1.0)).xyz;");
    sh.fragmentShader = sh.fragmentShader
      .replace("#include <common>", "#include <common>\nuniform vec3 uFlowDir; uniform float uFlowPhase; uniform float uFlowOn; varying vec3 vFlowW;")
      .replace("#include <emissivemap_fragment>", `#include <emissivemap_fragment>
float flowS = smoothstep(0.78, 1.0, fract((dot(vFlowW, uFlowDir) - uFlowPhase) / ${SPACING.toFixed(1)}));
totalEmissiveRadiance += vec3(${COLOR.r}, ${COLOR.g}, ${COLOR.b}) * flowS * 0.7 * uFlowOn;`);
  };
  mat.customProgramCacheKey = () => "flow";
  mat.needsUpdate = true;
}

const center = (o) => new THREE.Box3().setFromObject(o).getCenter(new THREE.Vector3());

// root: the loaded field. Returns { update(dt, view), pipes } or null if the field has no
// pipes this module knows.
export function mountFlow(root) {
  root.updateMatrixWorld(true);
  const node = (name) => root.getObjectByName(name);
  const pos = (name) => { const o = node(name); return o ? center(o) : null; };
  const swds = ["SWD_1", "SWD_2"].map((id) => ({ id, p: pos(id) })).filter((s) => s.p);
  const pumpTie = new Map(); // pump n -> where its discharge meets the trunk
  const pipes = [];

  const add = (o, from, to, feeds) => {
    if (!from || !to) return;
    const dir = to.clone().sub(from);
    if (dir.lengthSq() < 1e-6) return;
    const u = { dir: { value: dir.normalize() }, phase: { value: 0 }, on: { value: 0 } };
    o.traverse((m) => {
      if (!m.isMesh) return;
      m.material = m.material.clone();
      patch(m.material, u);
    });
    pipes.push({ name: o.name, u, feeds });
  };

  root.traverse((o) => {
    if (!/^PIPE_/.test(o.name) || /^PIPE_/.test(o.parent?.name || "")) return;
    const n = o.name;
    let m;
    if ((m = n.match(/^PIPE_TANK_(\d+)_PUMP_\1$/))) add(o, pos(`TANK_${m[1]}`), pos(`PUMP_${m[1]}`), { pump: m[1] });
    else if ((m = n.match(/^PIPE_PUMP_(\d+)_VALVE_D_\1$/))) add(o, pos(`PUMP_${m[1]}`), pos(`VALVE_D_${m[1]}`), { pump: m[1] });
    else if ((m = n.match(/^PIPE_VALVE_D_(\d+)_TRUNK$/))) {
      const c = center(o);
      pumpTie.set(m[1], c);
      add(o, pos(`VALVE_D_${m[1]}`), c, { pump: m[1] });
    } else if ((m = n.match(/^PIPE_WELL_(\d+)_TANK_(\d+)$/))) add(o, pos(`WELL_${m[1]}`), pos(`TANK_${m[2]}`), { wells: [`WELL_${m[1]}`] });
    else if ((m = n.match(/^PIPE_INLET_(\d+)_RISER$/))) { const c = center(o); add(o, c, c.clone().add(new THREE.Vector3(0, 1, 0)), { tank: `TANK_${m[1]}` }); }
    else if ((m = n.match(/^PIPE_INLET_(\d+)$/))) add(o, center(o), pos(`TANK_${m[1]}`), { tank: `TANK_${m[1]}` });
    else if (/^PIPE_TRUNK_\d+$/.test(n) && swds.length) {
      const c = center(o);
      const s = swds.reduce((a, b) => (b.p.distanceTo(c) < a.p.distanceTo(c) ? b : a));
      add(o, c, s.p, { trunk: { c, swd: s.p } });
    }
  });
  if (!pipes.length) return null;

  // Trunk: a segment carries water when a running pump ties in on the far side of it from
  // the disposal it drains to.
  for (const p of pipes) {
    const t = p.feeds.trunk;
    if (!t) continue;
    const d = t.c.distanceTo(t.swd);
    p.feeds.pumps = [...pumpTie].filter(([, tie]) => {
      const near = swds.reduce((a, b) => (b.p.distanceTo(tie) < a.p.distanceTo(tie) ? b : a));
      return near.p.equals(t.swd) && tie.distanceTo(t.swd) > d;
    }).map(([n]) => n);
  }

  const wellsOf = new Map(); // TANK_n -> its wells, from the flowline names
  for (const p of pipes) {
    const m = p.name.match(/^PIPE_WELL_\d+_TANK_(\d+)$/);
    if (m) (wellsOf.get(`TANK_${m[1]}`) || wellsOf.set(`TANK_${m[1]}`, []).get(`TANK_${m[1]}`)).push(...p.feeds.wells);
  }

  const DOWN = new Set(["ALARM", "OFFLINE", "NO_DATA", "UNKNOWN"]);
  const runs = (v) => !!v && !DOWN.has(v.status) && v.running !== false && v.stale !== true;
  // CONTRACT.md: valve position is "OPEN" or "CLOSED". A valve with no known position
  // shows no flow: the scene never draws water it can't vouch for.
  const pumpFlows = (view, n) => runs(view[`PUMP_${n}`]) && (!node(`VALVE_D_${n}`) || (view[`VALVE_D_${n}`] || {}).position === "OPEN");

  const flowing = (p, view) => {
    const f = p.feeds;
    if (f.pump) return pumpFlows(view, f.pump);
    if (f.wells) return f.wells.some((w) => runs(view[w]));
    if (f.tank) return (wellsOf.get(f.tank) || []).some((w) => runs(view[w]));
    if (f.pumps) return f.pumps.some((n) => pumpFlows(view, n));
    return false;
  };

  return {
    pipes,
    // view: the console's per-asset view (status, running, position). Eases on and off over
    // about half a second; the phase only advances while water moves.
    update(dt, view) {
      const k = Math.min(1, dt * 3);
      for (const p of pipes) {
        const on = flowing(p, view) ? 1 : 0;
        p.u.on.value += (on - p.u.on.value) * k;
        if (on) p.u.phase.value = (p.u.phase.value + dt * SPEED) % (SPACING * 1000);
      }
    },
    debug: (view) => pipes.map((p) => ({ name: p.name, flowing: flowing(p, view), on: +p.u.on.value.toFixed(2), pumps: p.feeds.pumps })),
  };
}
