// Talks to the backend in the docs/CONTRACT.md shape.
// Set VITE_API (e.g. VITE_API=http://127.0.0.1:8000 npm run dev) to use the real server.
// Without it, or if the server is unreachable, the console uses web/public/fake/*.json
// and says so on screen. It never treats missing data as safe.

const BASE = (import.meta.env.VITE_API || "").replace(/\/$/, "");

async function getJson(url, init) {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

async function fake(name) {
  return getJson(`${import.meta.env.BASE_URL}fake/${name}.json`);
}

export async function loadAll() {
  if (BASE) {
    try {
      const [field, state, forecast] = await Promise.all([
        getJson(`${BASE}/api/field`),
        getJson(`${BASE}/api/state`),
        getJson(`${BASE}/api/forecast`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ horizon_minutes: 360, step_minutes: 5, actions: [] }),
        }),
      ]);
      return { field, state, forecast, source: "live", warning: null };
    } catch (e) {
      const [field, state, forecast] = await Promise.all([fake("field"), fake("state"), fake("forecast")]);
      return { field, state, forecast, source: "fake", warning: `Backend unreachable at ${BASE}. Showing fake data.` };
    }
  }
  const [field, state, forecast] = await Promise.all([fake("field"), fake("state"), fake("forecast")]);
  return { field, state, forecast, source: "fake", warning: null };
}

// Every asset ID the field defines, in contract order.
export function assetIds(field) {
  const ids = [];
  for (const key of ["tanks", "pumps", "valves", "wells", "disposal_wells", "trucks", "yards"]) {
    for (const a of field[key] || []) ids.push(a.id);
  }
  return ids;
}
