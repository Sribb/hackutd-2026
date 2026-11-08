// Control token (#103/#104). The demo URL carries it in the fragment, console#k=<token>, so it
// never reaches a server log or a Referer. On load we keep it in sessionStorage
// ('freeboard.token', this tab only) and strip it from the address bar.
//
//   authHeaders(headers?)  adds X-Freeboard-Token when a token is set
//   wsUrl(url)             adds ?token= to a /ws URL when a token is set
//
// Until #104 adds the header in api.js, jarvis.js and live.js, installTransport() (called once
// by remote.js at import time, before live.js opens the socket) adds them on the way out:
// only for non-GET requests and /ws to this page's origin or the API base, only when a token is
// set, and never over a header or ?token= the caller already sent. #104 can drop this by calling
// authHeaders() and wsUrl() directly.

import { BASE } from "../api.js";

const KEY = "freeboard.token";
let memo = "";

function grab() {
  let m = null;
  try { m = /(?:^#|&)k=([^&]*)/.exec(location.hash || ""); } catch { return; }
  if (!m) return;
  let value = m[1];
  try { value = decodeURIComponent(value); } catch { /* keep it as typed */ }
  try { if (value) sessionStorage.setItem(KEY, value); } catch { /* private mode: token lives only in memory */ memo = value; }
  // Strip k= from the fragment, keep anything else that was there.
  const rest = location.hash.slice(1).split("&").filter((p) => p && !p.startsWith("k=")).join("&");
  try { history.replaceState(history.state, "", location.pathname + location.search + (rest ? `#${rest}` : "")); } catch { /* ignore */ }
}
grab();

export function token() {
  try { return sessionStorage.getItem(KEY) || memo || ""; } catch { return memo || ""; }
}

export function authHeaders(headers = {}) {
  const t = token();
  return t ? { ...headers, "X-Freeboard-Token": t } : { ...headers };
}

export function wsUrl(url) {
  const t = token();
  if (!t) return url;
  try {
    const u = new URL(url, location.href);
    if (!u.searchParams.has("token")) u.searchParams.set("token", t);
    return u.toString();
  } catch { return url; }
}

const origins = () => {
  const out = new Set([location.origin]);
  try { if (BASE) out.add(new URL(BASE).origin); } catch { /* ignore */ }
  return out;
};
const ours = (u) => origins().has(u.origin.replace(/^ws/, "http"));

let installed = false;
export function installTransport() {
  if (installed) return;
  installed = true;
  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    try {
      const url = new URL(typeof input === "string" ? input : input.url, location.href);
      const method = String(init.method || (typeof input !== "string" && input.method) || "GET").toUpperCase();
      const t = token();
      if (t && method !== "GET" && method !== "HEAD" && ours(url)) {
        const h = new Headers(init.headers || (typeof input !== "string" ? input.headers : undefined));
        if (!h.has("X-Freeboard-Token")) h.set("X-Freeboard-Token", t);
        init = { ...init, headers: h };
      }
    } catch { /* leave the request as it was */ }
    return realFetch(input, init);
  };
  const RealWS = window.WebSocket;
  class TokenWebSocket extends RealWS {
    constructor(url, protocols) {
      let out = url;
      try {
        const u = new URL(String(url), location.href);
        if (/\/ws$/.test(u.pathname) && ours(u)) out = wsUrl(u.toString());
      } catch { /* leave it */ }
      super(out, protocols);
    }
  }
  window.WebSocket = TokenWebSocket;
}
