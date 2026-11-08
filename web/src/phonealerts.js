// Text and voice alerts to the judge's phone (#160). The number comes from /start
// (sessionStorage "gaia.judge.phone", E.164) and goes to the backend only from a tab that
// holds the operator token. The backend sends through Twilio when it is configured; when it
// isn't, these calls say so and the phone pager is the fallback.
//
//   await phoneAlerts.subscribe()        -> {id, masked, provider} or null (no number / no backend)
//   await phoneAlerts.send(id, text)     -> {provider, queued} or null
//   phoneAlerts.status()                 -> {provider, subscribers} or null

import { BASE } from "./api.js";
import { withToken } from "./auth.js";

const KEY = "gaia.judge.phone";
const SUB = "gaia.judge.phone.sub";
const ss = {
  get: (k) => { try { return sessionStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { sessionStorage.setItem(k, v); } catch { /* private window */ } },
};

async function call(path, init) {
  if (!BASE) return null;
  try {
    const r = await fetch(`${BASE}${path}`, init);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

const post = (path, body) => call(path, withToken({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }));

export const phoneAlerts = {
  number: () => ss.get(KEY),
  status: () => call("/api/phone/status"),
  async subscribe() {
    const phone = ss.get(KEY);
    if (!phone) return null;
    const r = await post("/api/phone/subscribe", { phone });
    if (r?.id) ss.set(SUB, JSON.stringify({ id: r.id, masked: r.masked, provider: r.provider }));
    return r;
  },
  subscription() {
    try { return JSON.parse(ss.get(SUB) || "null"); } catch { return null; }
  },
  // One alert id reaches each phone once (the backend keeps track).
  send(alertId, text, { call: voice = true } = {}) {
    if (!alertId || !text) return Promise.resolve(null);
    return post("/api/phone/alert", { alert_id: String(alertId), text, call: voice });
  },
};

if (typeof window !== "undefined") {
  window.gaiaPhone = phoneAlerts;
  // A judge who left a number on /start is signed up as the console opens (needs the
  // operator token in this tab; without it the backend refuses and nothing is stored).
  if (phoneAlerts.number() && !phoneAlerts.subscription()) setTimeout(() => { phoneAlerts.subscribe(); }, 1500);
}
