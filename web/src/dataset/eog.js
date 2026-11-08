// The backend's dataset mode (/api/eog, docs/CONTRACT.md "Dataset mode"): the one source
// of detection, countdowns, plans, Last Call, the handoff and events.json for this page.
// The page draws the raw files itself (map, 3D, sensor charts); every number about a
// problem or a plan comes from here.

import { authFetch } from "../auth.js";

export function eogClient(base = "") {
  const root = `${base}/api/eog`;
  const q = (at, extra = {}) => {
    const p = new URLSearchParams();
    if (at !== null && at !== undefined) p.set("at", String(at));
    for (const [k, v] of Object.entries(extra)) if (v !== null && v !== undefined) p.set(k, String(v));
    const s = p.toString();
    return s ? `?${s}` : "";
  };
  async function json(r) {
    let body = null;
    try { body = await r.json(); } catch { /* not JSON */ }
    if (!r.ok) throw Object.assign(new Error(body?.detail || `HTTP ${r.status}`), { status: r.status });
    return body;
  }
  const get = (path, at, extra) => fetch(`${root}${path}${q(at, extra)}`, { cache: "no-store" }).then(json);
  const post = (path, body) => authFetch(`${root}${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}),
  }).then(json);
  return {
    datasets: () => get("/datasets"),
    activate: (id) => post("/datasets/activate", { id }),
    alerts: (at) => get("/alerts", at),
    events: (at) => get("/events", at),
    plans: (at) => get("/plans", at),
    handoff: (at) => get("/handoff", at),
    whatif: (body) => post("/whatif", body),
    approve: (eventId, at) => post(`/plans/${encodeURIComponent(eventId)}/approve`, { at }),
  };
}
