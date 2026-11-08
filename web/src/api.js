// Talks to the backend in the docs/CONTRACT.md shape.
// Set VITE_API (e.g. VITE_API=http://127.0.0.1:8000 npm run dev) to use the real server.
// Each endpoint falls back to web/public/fake/*.json on its own, so a backend that only
// serves /api/field and /api/state still works. The console shows which data is fake.
// It never treats missing data as safe.

// VITE_API=same-origin (npm run demo): the Vite server proxies /api and /ws to the backend,
// so the laptop and phones on the Wi-Fi use whatever host served the page.
import { authFetch } from "./auth.js";
const RAW_API = import.meta.env.VITE_API || "";
export const BASE = (RAW_API === "same-origin" ? location.origin : RAW_API).replace(/\/$/, "");
const fakeCache = new Map();

async function getJson(url, init) {
  const r = await authFetch(url, init);
  if (!r.ok) {
    let detail = "";
    try { const j = await r.json(); detail = typeof j.detail === "string" ? j.detail : ""; } catch { /* no body */ }
    throw Object.assign(new Error(detail || `${url}: HTTP ${r.status}`), { status: r.status });
  }
  return r.json();
}

// Recorded data only when there is no backend, when ?offline is in the URL, or when the
// backend doesn't have that endpoint at all (404). A backend that is down, erroring, or
// refusing (401/403/5xx, no answer) never gets silently replaced by recorded numbers (#104):
// the call fails, and the screen says so.
const OFFLINE = new URLSearchParams(location.search).has("offline");
export class BackendDown extends Error {}

function fake(name) {
  if (!fakeCache.has(name)) fakeCache.set(name, getJson(`${import.meta.env.BASE_URL}fake/${name}.json`));
  return fakeCache.get(name);
}

// Which endpoints are answered by fake data right now. Shown in the UI.
export const sources = {};

async function live(name, path, body) {
  if (BASE && !OFFLINE) {
    const init = body === undefined ? undefined
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    try {
      const out = await getJson(`${BASE}${path}`, init);
      sources[name] = "live";
      return out;
    } catch (e) {
      if (e.status !== 404) {
        sources[name] = "down";
        throw new BackendDown(`${path}: ${e.status ? `HTTP ${e.status}` : "no answer"}${e.status && e.message && !/HTTP/.test(e.message) ? ` (${e.message})` : ""}`);
      }
      /* 404: this backend doesn't have the endpoint yet, so recorded data, badged */
    }
  }
  sources[name] = "fake";
  return null;
}

export const usingBackend = !!BASE && !OFFLINE;

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
  let out;
  try {
    out = await live("forecast", "/api/forecast", body);
  } catch (e) {
    if (!(e instanceof BackendDown) || !actions.length) throw e;
    // A what-if the backend can't answer has no forecast. Never a recorded one.
    return { timeline: [], events: [], cost: null, unknown: true, warnings: [`The backend didn't answer (${e.message}), so this change has no forecast.`] };
  }
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
