// Control token and PIN for the console (#103/#104, spec §2 "Tokens").
//
//   token()          the token this tab holds ("" when none)
//   authHeaders(h)   h plus X-Freeboard-Token when a token is set
//   wsToken()        for live.start(base, role, wsToken): /ws?token=
//   onNotice(fn)     fn(text) for "Wrong PIN. Nothing was sent." and "This device can't do that."
//
// The token comes from ?token= or #k= once, is stripped from the address bar, and kept in
// sessionStorage (this tab only). installAuth() wraps window.fetch so every non-GET request to
// the backend carries X-Freeboard-Token, even from modules that don't know about tokens (main.js,
// jarvis.js). On a 401 it asks for the operator PIN once, keeps it for the session, and retries
// that one request once. A 401 means the server refused before doing anything, so the retry can
// never send a command twice. A second 401 says "Wrong PIN. Nothing was sent." and forgets it.

import { BASE } from "../api.js";

const KEY = "freeboard.token";
let memo = "";
const notices = new Set();

function store(value) {
  memo = value || "";
  try {
    if (value) sessionStorage.setItem(KEY, value);
    else sessionStorage.removeItem(KEY);
  } catch { /* private mode: memory only */ }
}

// Take ?token= or #k= from the URL once, then strip it so it never sits in history or a screenshot.
function grab() {
  let value = "";
  try {
    const u = new URL(location.href);
    const q = u.searchParams.get("token");
    const h = /(?:^#|&)k=([^&]*)/.exec(u.hash || "");
    if (q) value = q;
    else if (h) { try { value = decodeURIComponent(h[1]); } catch { value = h[1]; } }
    if (!q && !h) return;
    u.searchParams.delete("token");
    const rest = u.hash.slice(1).split("&").filter((p) => p && !p.startsWith("k=")).join("&");
    history.replaceState(history.state, "", u.pathname + (u.searchParams.toString() ? `?${u.searchParams}` : "") + (rest ? `#${rest}` : ""));
  } catch { return; }
  if (value) store(value);
}
grab();

export function token() {
  try { return sessionStorage.getItem(KEY) || memo || ""; } catch { return memo || ""; }
}
export const wsToken = () => token();
export function authHeaders(headers = {}) {
  const t = token();
  return t ? { ...headers, "X-Freeboard-Token": t } : { ...headers };
}
export function onNotice(fn) { notices.add(fn); return () => notices.delete(fn); }
function notice(text) { for (const fn of notices) { try { fn(text); } catch (e) { console.error(e); } } }

const ours = (url) => {
  const origins = new Set([location.origin]);
  try { if (BASE) origins.add(new URL(BASE).origin); } catch { /* ignore */ }
  return origins.has(url.origin);
};

// ------------------------------------------------------------------ PIN ----
// One prompt at a time; concurrent 401s share it. Resolves to the PIN, or "" if dismissed.
let asking = null;
export function askPin() {
  if (asking) return asking;
  asking = new Promise((resolve) => {
    const box = document.createElement("div");
    box.className = "cs-pin";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.innerHTML = `<form class="cs-pin-card" autocomplete="off">
        <div class="cs-kicker">Operator PIN</div>
        <p>This screen needs the operator PIN to send commands. Nothing has been sent.</p>
        <input id="csPin" type="password" inputmode="numeric" aria-label="Operator PIN" autocomplete="one-time-code">
        <div class="cs-row"><button type="button" data-x>Cancel</button><button type="submit" class="primary">Send</button></div>
      </form>`;
    document.body.appendChild(box);
    const input = box.querySelector("input");
    const done = (v) => { box.remove(); asking = null; resolve(v); };
    box.querySelector("form").onsubmit = (e) => { e.preventDefault(); done(input.value.trim()); };
    box.querySelector("[data-x]").onclick = () => done("");
    box.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.stopPropagation(); done(""); } });
    setTimeout(() => input.focus(), 0);
  });
  return asking;
}

// ---------------------------------------------------------------- fetch ----
let installed = false;
export function installAuth() {
  if (installed) return;
  installed = true;
  const realFetch = window.fetch.bind(window);
  const withToken = (init, t) => {
    const h = new Headers(init.headers || undefined);
    if (t) h.set("X-Freeboard-Token", t);
    return { ...init, headers: h };
  };
  window.fetch = async (input, init = {}) => {
    let url, method;
    try {
      url = new URL(typeof input === "string" ? input : input.url, location.href);
      method = String(init.method || (typeof input !== "string" && input.method) || "GET").toUpperCase();
    } catch { return realFetch(input, init); }
    if (method === "GET" || method === "HEAD" || !ours(url) || typeof input !== "string") return realFetch(input, init);
    const sent = token();
    const first = await realFetch(input, withToken(init, sent));
    if (first.status === 403) { notice("This device can't do that."); return first; }
    if (first.status !== 401) return first;
    // Someone else may have entered the PIN while this request was out.
    let pin = token() !== sent ? token() : await askPin();
    if (!pin) { notice("No PIN. Nothing was sent."); return first; }
    store(pin);
    const again = await realFetch(input, withToken(init, pin));
    if (again.status === 401) { store(""); notice("Wrong PIN. Nothing was sent."); }
    else if (again.status === 403) notice("This device can't do that.");
    return again;
  };
}
