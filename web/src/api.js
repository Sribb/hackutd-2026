// Talks to the backend in the docs/CONTRACT.md shape.
// Set VITE_API (e.g. VITE_API=http://127.0.0.1:8000 npm run dev) to use the real server.
// Each endpoint falls back to web/public/fake/*.json on its own, so a backend that only
// serves /api/field and /api/state still works. The console shows which data is fake.
// It never treats missing data as safe.

export const BASE = (import.meta.env.VITE_API || "").replace(/\/$/, "");
const fakeCache = new Map();

async function getJson(url, init) {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

function fake(name) {
  if (!fakeCache.has(name)) fakeCache.set(name, getJson(`${import.meta.env.BASE_URL}fake/${name}.json`));
  return fakeCache.get(name);
}

// Which endpoints are answered by fake data right now. Shown in the UI.
export const sources = {};

async function live(name, path, body) {
  if (BASE) {
    try {
      const init = body === undefined ? undefined
        : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
      const out = await getJson(`${BASE}${path}`, init);
      sources[name] = "live";
      return out;
    } catch {
      /* fall through to fake */
    }
  }
  sources[name] = "fake";
  return null;
}

export const usingBackend = !!BASE;

export async function getField() {
  return (await live("field", "/api/field")) || fake("field");
}

export async function getState() {
  return (await live("state", "/api/state")) || fake("state");
}

// Same key format as web/scripts/make_fakes.py.
export function actionKey(a) {
  const v = a.value === undefined ? "" : typeof a.value === "number" ? a.value.toFixed(1) : String(a.value);
  return `${a.type}:${a.asset_id}:${v}:${a.target_id || ""}`;
}

export async function forecast(actions) {
  const body = { horizon_minutes: 360, step_minutes: 5, actions };
  const out = await live("forecast", "/api/forecast", body);
  if (out) return out;
  const base = await fake("forecast");
  if (!actions.length) return base;
  const w = await fake("whatif");
  const hit = w.results[actions.map(actionKey).sort().join("|")];
  if (!hit) {
    // Never invent a future: say we don't know.
    return { ...base, events: [], cost: null, unknown: true,
             warnings: ["No fake forecast for this combination. Start the backend for any action list."] };
  }
  const timeline = base.timeline.map((row, i) => ({
    t_min: row.t_min,
    tanks: { ...row.tanks, ...Object.fromEntries(w.changing.map((t) => [t, hit.tanks[t][i]])) },
    wells_online: hit.wells_online[i],
  }));
  return { timeline, events: hit.events, cost: hit.cost, warnings: base.warnings };
}

export async function plans() {
  return (await live("plans", "/api/plans", {})) || fake("plans");
}

export async function approve(planId) {
  const out = await live("approve", `/api/plans/${encodeURIComponent(planId)}/approve`, {});
  if (out) return out;
  return (await fake("approve"))[planId] || { steps: [], radio_script: "" };
}

export async function lastCall() {
  return (await live("lastcall", "/api/lastcall", {})) || fake("lastcall");
}

// Every asset ID the field defines, in contract order. YARD_1 is in the contract
// but has no key in /api/field, so it is always added.
export function assetIds(field) {
  const ids = [];
  for (const key of ["tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards"]) {
    for (const a of field[key] || []) ids.push(a.id);
  }
  if (!ids.includes("YARD_1")) ids.push("YARD_1");
  return ids;
}
