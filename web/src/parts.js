// Every mesh in field.glb resolved to its asset and its part, plus what that part is in
// plain words and the live number that matters for it (#86).
//
// Names follow docs/CONTRACT.md "What's in field.glb": an asset is the node named with its ID
// (glTF extras asset_id), and its parts are children named <ID>_<part>. A few parts live
// outside the asset's node and are mapped back by name: LOADOUT_n is Tank n's truck hookup,
// SWD_n_tower holds SWD n's lamps, and SENSOR_* / RTU_* attach to the asset named in their
// extras (asset_id, attached_to, tank_id) or inside their own name.
// Scenery, the site, pads, roads and waypoints are never assets and never parts.

export const NEVER = /^(SITE|PAD|ROAD|STOP|CAM|SKY)_|^SCENERY(_|$)|^ground$/;

export const nice = (id) => String(id || "").replace(/_/g, " ").replace(/\b([A-Z])([A-Z]*)\b/g, (_, a, b) => a + b.toLowerCase())
  .replace(/\bXo\b/, "XO").replace(/\bSwd\b/, "SWD").replace(/\bRtu\b/, "RTU").replace(/\bLt\b/, "LT").replace(/\bLshh\b/, "LSHH");

export function kindOf(id, equipment) {
  if (equipment && equipment(id)) return "equipment";
  const m = /^(TANK|PUMP|VALVE_D|VALVE_XO|VALVE|WELL|SWD|TRUCK|YARD|SENSOR|RTU)_/.exec(id || "");
  return m ? m[1].toLowerCase() : "asset";
}

// The node that carries an asset ID: its own extras, or a SENSOR_/RTU_/LOADOUT_ name that
// points at one. Returns null for anything that isn't part of an asset.
function ownerOf(node, ids) {
  for (let o = node; o; o = o.parent) {
    if (NEVER.test(o.name) || (o.userData && o.userData.scenery)) return null;
    const u = o.userData || {};
    const lo = /^LOADOUT_(\d+)$/.exec(o.name);
    if (lo && ids.has(`TANK_${lo[1]}`)) return { id: `TANK_${lo[1]}`, node: o };
    if (/^(SENSOR|RTU)_/.test(o.name)) {
      if (ids.has(o.name)) return { id: o.name, node: null };
      const to = [u.asset_id, u.attached_to, u.tank_id].find((x) => ids.has(x));
      if (to) return { id: to, node: o };
      const inner = [...ids].filter((x) => new RegExp(`(^|_)${x}(_|$)`).test(o.name.replace(/^(SENSOR|RTU)_/, ""))).sort((a, b) => b.length - a.length)[0];
      if (inner) return { id: inner, node: o };
    }
  }
  return undefined;
}

// Builds the index for one loaded model.
//   index.info(mesh)   -> { id, part (Object3D), key ("wheel", "LEVEL", "loadout", ...) } or null
//   index.meshes(id)   -> every mesh of the asset, parts outside its node included
//   index.parts(id)    -> [{ node, key, meshes }]
//   index.pipes        -> [{ node, ids: Set }] pipes and the assets they touch
export function buildIndex(root, { ids, idFor, assetRoot }) {
  const byMesh = new Map(), byAsset = new Map(), pipes = [];
  const sorted = [...ids].sort((a, b) => b.length - a.length);
  root.updateMatrixWorld(true);
  root.traverse((o) => {
    if (/^PIPE_/.test(o.name) && (o.userData.pipe_id || o.isMesh || o.children.length) && !/^PIPE_/.test(o.parent && o.parent.name)) {
      const rest = o.name.slice(5).replace(/(^|_)INLET_(\d+)/g, "$1TANK_$2").replace(/(^|_)LOADOUT_(\d+)/g, "$1TANK_$2").replace(/(^|_)XO_(\d+)/g, "$1VALVE_XO_$2");
      const touch = new Set(sorted.filter((x) => new RegExp(`(^|_)${x}(_|$)`).test(rest)));
      pipes.push({ node: o, ids: touch });
    }
    if (!o.isMesh) return;
    const own = ownerOf(o, ids);
    if (own === null) return;
    const id = own ? own.id : idFor(o);
    if (!id) return;
    const top = assetRoot(id);
    let part = null, lowest = null;
    for (let n = o, below = null; n && n !== root.parent; below = n, n = n.parent) {
      if (n === top) { part = below || n; break; }
      if (!lowest && (n.name.startsWith(`${id}_`) || n === (own && own.node))) lowest = n;
      if (own && n === own.node) { part = n; break; }
    }
    part = part || lowest || o;
    const key = /^LOADOUT_/.test(part.name) ? "loadout" : /^SENSOR_/.test(part.name) ? "sensor" : /^RTU_/.test(part.name) ? "rtu"
      : part.name.startsWith(`${id}_`) ? part.name.slice(id.length + 1) : part === top ? "body" : part.name;
    const info = { id, part, key };
    byMesh.set(o, info);
    if (!byAsset.has(id)) byAsset.set(id, { meshes: [], parts: new Map() });
    const a = byAsset.get(id);
    a.meshes.push(o);
    if (!a.parts.has(part)) a.parts.set(part, { node: part, key, meshes: [] });
    a.parts.get(part).meshes.push(o);
  });
  return {
    info: (mesh) => byMesh.get(mesh) || null,
    meshes: (id) => (byAsset.get(id) ? byAsset.get(id).meshes : []),
    parts: (id) => (byAsset.get(id) ? [...byAsset.get(id).parts.values()] : []),
    pickable: () => [...byMesh.keys()],
    assetIds: () => [...byAsset.keys()],
    pipes,
  };
}

// --------------------------------------------------------- plain words ----
const GENERIC = [
  [/^LEVEL$/, "brine level", "The salt water inside the tank, drawn at the current level."],
  [/^gauge$/, "level gauge", "The float marker on the outside level board. It rides up and down with the brine."],
  [/^lt$/, "radar level sensor", "Reads the brine level from the top of the tank. The console's level comes from it."],
  [/^lshh$/, "high-level switch", "Trips the shutdown before the tank can overflow."],
  [/^loadout$/, "load-out stand", "Where a truck hooks up to drain the tank."],
  [/^rotor$/, "motor fan", "Spins while the pump runs. A still fan means no flow."],
  [/^light$/, "status light", "Shows the status color. The time of day never changes it."],
  [/^wheel$/, "handwheel", "Turned by hand to open or close the valve. It spins while a what-if changes it."],
  [/^beam$/, "walking beam", "Rocks up and down while the well produces."],
  [/^crank$/, "crank and counterweights", "Turn while the well produces. Still means shut in."],
  [/^axle_(\d+)$/, "wheels", "Spin while the truck drives."],
  [/^beacon$/, "job beacon", "Flashes amber while the truck is on a job."],
  [/^headlights$/, "head and tail lights", "On while the truck drives at night."],
  [/^gate$/, "fence gate", "The gate in the disposal well's fence."],
  [/^lamps$/, "light tower lamps", "Site lighting. On from dusk to dawn."],
  [/^tower/, "light tower", "Mast and footing for the site lights."],
  [/^sensor$/, "sensor", "Sends this asset's reading to the console."],
  [/^rtu$/, "remote terminal unit", "The field box that sends this asset's readings and takes confirmed commands."],
  [/^(label|label_cap)$/, "painted number", "The asset number painted where a driver can read it."],
  [/^tag$/, "ID tag", "The tag with the asset ID."],
  [/^sign$/, "sign", "The site sign."],
  [/^glass$/, "cab windows", "The truck's cab glass."],
  [/^detail_tank$/, "settling tank", "Lets solids settle out before the water is injected."],
  [/concrete/, "concrete footing", "The concrete the equipment stands on."],
  [/gravel/, "gravel pad", "The gravel pad around the equipment."],
  [/pipe/, "piping", "Pipes and fittings on this asset."],
  [/rubber/, "hoses and seals", "Rubber hoses, gaskets and seals."],
  [/steel/, "steelwork", "Frames, rails and fittings."],
];
const BY_KIND = {
  tank: { tank: ["tank shell", "The steel tank. The cutaway shows the brine inside."], steel: ["stairs and railings", "The stairs and the walkway on top."] },
  pump: { steel: ["pump and motor", "The transfer pump and the electric motor that drives it."], body: ["transfer pump", "Moves the tank's water to the disposal line."] },
  valve_d: { steel: ["valve body", "The gate valve on the pump's discharge line."] },
  valve_xo: { steel: ["valve body", "The gate valve on the crossover line between the two tanks."] },
  well: { steel: ["pumpjack frame", "The pumpjack that lifts oil and water from the well."], pipe: ["flowline", "Carries the well's water to its tank."] },
  swd: { steel: ["injection equipment", "Pumps and piping that inject the water underground."] },
  truck: { tank: ["cargo tank", "Holds the water the truck hauls."], steel: ["cab and chassis", "The truck itself."] },
};

export function partWords(kind, key) {
  const k = BY_KIND[kind] && Object.entries(BY_KIND[kind]).find(([p]) => p === key || (p === "steel" && /steel/.test(key)));
  if (k) return { label: k[1][0], what: k[1][1] };
  for (const [re, label, what] of GENERIC) {
    const m = re.exec(key);
    if (m) return { label: re.source.startsWith("^axle") ? `${label} (axle ${m[1]})` : label, what };
  }
  return { label: key.replace(/_/g, " ").toLowerCase(), what: "" };
}

const pct = (x) => `${Math.round(x)}%`;
const STATUS_WORD = { NO_DATA: "NO DATA", DISPLAY: "not monitored" };

// The headline for a hover: "Tank 3 level gauge: 71%, ALARM", plus what the asset or part is.
//   ctx: { view (lastView[id]), field, staleMin, equipment, trying }
export function describe(id, key, ctx) {
  const kind = kindOf(id, ctx.equipment);
  const v = ctx.view || {};
  const f = ctx.field || {};
  const find = (list) => (f[list] || []).find((x) => x.id === id);
  const status = v.status || "NO_DATA";
  const words = key && key !== "body" ? partWords(kind, key) : null;
  let live = "", what = "";

  if (kind === "tank") {
    const t = find("tanks") || {};
    const wells = (f.wells || []).filter((w) => w.tank_id === id).length;
    what = `Holds produced water from ${wells} well${wells === 1 ? "" : "s"}${t.shutdown_pct != null ? ` and shuts down at ${t.shutdown_pct}%` : ""}.`;
    live = v.level_pct != null ? pct(v.level_pct) : "no level reading";
    if (key === "lshh" && t.shutdown_pct != null) live = `trips at ${t.shutdown_pct}%, now ${v.level_pct != null ? pct(v.level_pct) : "unknown"}`;
    if (key === "loadout" && words) words.what = `Where a truck hooks up to drain ${nice(id)}.`;
  } else if (kind === "pump") {
    const p = find("pumps") || {};
    what = `Moves ${nice(p.tank_id || "its tank")}'s water to the disposal line${p.rate_bbl_hr ? ` at up to ${p.rate_bbl_hr} bbl/hr` : ""}.`;
    live = v.running === false || status === "ALARM" ? "stopped" : status === "OFFLINE" ? "off" : status === "OK" || status === "WARNING" ? "running" : "";
  } else if (kind === "valve_d" || kind === "valve_xo" || kind === "valve") {
    const val = find("valves") || {};
    const ends = (val.connects || []).map(nice);
    what = kind === "valve_xo" ? `Crossover between ${ends.join(" and ") || "two tanks"}. Opening it lets one tank help the other.`
      : `Discharge valve from ${ends[0] || "its pump"} toward ${ends[1] || "the disposal line"}.`;
    live = v.position ? String(v.position).toLowerCase() : "";
  } else if (kind === "well") {
    const w = find("wells") || {};
    what = `Pumpjack well feeding ${nice(w.tank_id || "its tank")}${w.water_bbl_hr ? `, ${w.water_bbl_hr} bbl/hr of water` : ""}.`;
    live = status === "OFFLINE" ? "shut in" : status === "ALARM" ? "down" : status === "OK" || status === "WARNING" ? "producing" : "";
  } else if (kind === "swd") {
    const d = find("disposal_wells") || {};
    what = `Disposal well. Injects salt water underground${d.max_bbl_hr ? `, up to ${d.max_bbl_hr} bbl/hr` : ""}.`;
    live = d.max_bbl_hr ? `takes up to ${d.max_bbl_hr} bbl/hr` : "";
  } else if (kind === "truck") {
    const tr = find("trucks") || {};
    what = `Vacuum truck${tr.capacity_bbl ? `, ${tr.capacity_bbl} bbl` : ""}.`;
    live = v.location_text || (v.position ? `at ${nice(v.position)}` : v.location ? `at ${nice(v.location)}` : "");
    if (key === "beacon") live = v.on_job ? "on a job" : "off";
    if (/^axle_/.test(key || "")) live = v.driving ? "turning" : "still";
  } else if (kind === "yard") {
    what = "Truck yard. Trucks start and end their runs here.";
  } else if (kind === "equipment") {
    const eq = ctx.equipment(id) || {};
    what = `${eq.name || "Equipment"}${eq.label ? ` (${eq.label})` : ""}. Display only: no sensor or forecast covers it.`;
    live = "not monitored";
  }
  if (key === "light" || key === "beacon" && kind !== "truck") live = STATUS_WORD[status] || status;
  if (ctx.trying) live = live ? `${live}, ${ctx.trying}` : ctx.trying;
  if (status === "UNKNOWN") live = `${live ? `${live}, ` : ""}no reading for ${ctx.staleMin == null ? "an unknown time" : `${ctx.staleMin} min`}`;
  const showStatus = status !== "OK" && status !== "DISPLAY" && !(key === "light" && live === status);
  const tail = [live, showStatus ? STATUS_WORD[status] || status : ""].filter(Boolean).join(", ");
  const title = `${nice(id)}${words ? ` ${words.label}` : ""}`;
  return { title, live: tail, text: tail ? `${title}: ${tail}` : title, what: (words && words.what) || what, kind };
}

// Links from /api/field: wells and pumps to their tank, valves to what they connect. A valve
// passes flow, so a tank's crossover also brings in the tank on the other side.
export function linkGraph(field) {
  const g = new Map();
  const add = (a, b) => {
    if (!a || !b || a === b) return;
    for (const [x, y] of [[a, b], [b, a]]) { if (!g.has(x)) g.set(x, new Set()); g.get(x).add(y); }
  };
  for (const w of field.wells || []) add(w.id, w.tank_id);
  for (const p of field.pumps || []) add(p.id, p.tank_id);
  for (const v of field.valves || []) { for (const c of v.connects || []) add(v.id, c); add(v.id, v.pump_id); }
  for (const e of field.equipment || []) { add(e.id, e.tank_id); for (const c of e.connects || []) add(e.id, c); }
  for (const c of field.connections || []) { const [a, b] = Array.isArray(c) ? c : [c && c.from, c && c.to]; add(a, b); }
  return g;
}
export function relatedTo(g, id) {
  const out = new Set(g.get(id) || []);
  for (const n of [...out]) if (/^VALVE_/.test(n)) for (const m of g.get(n) || []) out.add(m);
  out.delete(id);
  return out;
}
