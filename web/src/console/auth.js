// Control token for the console (spec §2 "Tokens"), on top of web/src/auth.js (#104), which
// already keeps the token in sessionStorage, takes it from #k=, sends X-Freeboard-Token on
// every write, asks for the operator PIN once on a 401 and retries once ("Wrong PIN. Nothing
// was sent."), says "This device can't do that." on a 403, and gives live.js the token for
// the first /ws message (falling back to ?token= on older backends).
//
// This module adds the spec's other way in, ?token=<t> in the query: taken once at import,
// stripped from the address bar, and kept in the same place as #k=.

import { token, setToken, authFetch, askPin, notice } from "../auth.js";

function grabQuery() {
  try {
    const u = new URL(location.href);
    const t = u.searchParams.get("token");
    if (!t) return;
    setToken(t);
    u.searchParams.delete("token");
    history.replaceState(history.state, "", u.pathname + (u.searchParams.toString() ? `?${u.searchParams}` : "") + u.hash);
  } catch { /* leave the URL as it is */ }
}
grabQuery();

export { token, authFetch };
export function authHeaders(headers = {}) {
  const t = token();
  return t ? { ...headers, "X-Freeboard-Token": t } : { ...headers };
}

// A console POST with the token, the same 401 -> PIN -> retry once as auth.js's authFetch, but
// each request gets its own timeout that starts when it is sent. The PIN prompt can take as
// long as a person needs: a timer that ran through it would abort the retry, and the fix card
// would call a command that was never sent UNCERTAIN.
export async function post(url, body, ms = 15000) {
  const send = async () => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), ms);
    try {
      return await fetch(url, { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify(body ?? {}), signal: ctl.signal });
    } finally { clearTimeout(timer); }
  };
  let r = await send();
  if (r.status === 401) {
    let msg = "Enter the operator PIN to send commands.";
    try { const j = await r.clone().json(); if (j && typeof j.detail === "string") msg = j.detail; } catch { /* default */ }
    const pin = await askPin(msg);
    if (!pin) return r; // dismissed: nothing was sent
    setToken(pin);
    r = await send();
    if (r.status === 401) { setToken(""); notice("Wrong PIN. Nothing was sent."); }
  }
  if (r.status === 403) notice("This device can't do that. Nothing was sent.");
  if (r.status === 429) { // #57: a burst of wrong PINs locks out for Retry-After seconds
    const s = Number(r.headers.get("Retry-After"));
    notice(`Too many wrong PINs. Nothing was sent. Try again in ${s > 0 ? (s >= 90 ? `${Math.ceil(s / 60)} min` : `${s} s`) : "a minute"}.`);
  }
  return r;
}
