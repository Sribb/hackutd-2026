// Control token for writes (TASK #104, CONTRACT.md "Control token").
//
// - The token arrives in the URL fragment: `#k=<token>` (the console gets the operator token,
//   the phone QR the phone token). A fragment is never sent to a server or in a Referer.
//   It is kept in sessionStorage for this tab and stripped from the address bar at once.
// - The console URL may also carry `p=<phone token>` for its Pager QR. Only someone who
//   opened the console with that fragment ever has it.
// - Every non-GET request sends `X-Freeboard-Token`. /ws gets `?token=`.
// - A 401 on a write asks for the operator PIN (CF-5) and retries once with it. Unset on the
//   backend = no token needed, so nothing changes until the backend turns it on.

const KEY = "freeboard.token";
const PHONE_KEY = "freeboard.phoneToken";
const store = {
  get: (k) => { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
  set: (k, v) => { try { v ? sessionStorage.setItem(k, v) : sessionStorage.removeItem(k); } catch { /* private mode */ } },
};

function captureFragment() {
  const h = location.hash.replace(/^#/, "");
  if (!/(^|&)(k|p)=/.test(h)) return;
  const keep = [];
  for (const part of h.split("&")) {
    const [k, v = ""] = part.split("=");
    // A mangled link (bad %-escape) must not stop the console from loading: drop that part.
    let val = null;
    try { val = decodeURIComponent(v); } catch { val = null; }
    if (k === "k") { if (val) store.set(KEY, val); }
    else if (k === "p") { if (val) store.set(PHONE_KEY, val); }
    else if (part) keep.push(part);
  }
  history.replaceState(null, "", location.pathname + location.search + (keep.length ? `#${keep.join("&")}` : ""));
}
captureFragment();
// Opening a #k= link in a tab that already has the console only changes the hash.
addEventListener("hashchange", captureFragment);

export const token = () => store.get(KEY);
export const phoneToken = () => store.get(PHONE_KEY);
export const setToken = (t) => store.set(KEY, t);

const isWrite = (init) => !!init && !!init.method && init.method.toUpperCase() !== "GET" && init.method.toUpperCase() !== "HEAD";

export function withToken(init) {
  const t = token();
  if (!t || !isWrite(init)) return init;
  const headers = new Headers((init && init.headers) || {});
  headers.set("X-Freeboard-Token", t);
  return { ...init, headers };
}

export function wsUrl(url) {
  const t = token();
  return t ? `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(t)}` : url;
}

// fetch, plus the token on writes, plus one PIN prompt and retry on a 401.
export async function authFetch(url, init) {
  let r = await fetch(url, withToken(init));
  if (r.status === 401 && isWrite(init)) {
    let msg = "Enter the operator PIN to send commands.";
    try { const j = await r.clone().json(); if (j && j.detail) msg = String(j.detail); } catch { /* keep default */ }
    const pin = await askPin(msg);
    if (pin) {
      setToken(pin);
      r = await fetch(url, withToken(init));
      // CF-5: a wrong PIN is forgotten at once, and the operator is told nothing moved.
      if (r.status === 401) { setToken(""); notice("Wrong PIN. Nothing was sent."); }
    }
  }
  // A phone token (or any limited token) asking for an operator action.
  if (r.status === 403 && isWrite(init)) notice("This device can't do that. Nothing was sent.");
  // #57 locks out a burst of wrong PINs (429 + Retry-After).
  if (r.status === 429 && isWrite(init)) {
    const s = Number(r.headers.get("Retry-After"));
    notice(`Too many wrong PINs. Nothing was sent. Try again in ${s > 0 ? (s >= 90 ? `${Math.ceil(s / 60)} min` : `${s} s`) : "a minute"}.`);
  }
  return r;
}

// One short line at the bottom of the screen, for outcomes the caller may not show.
let noticeTimer = 0;
export function notice(text) {
  let el = document.querySelector(".auth-notice");
  if (!el) {
    el = document.createElement("div");
    el.className = "auth-notice";
    el.setAttribute("role", "alert");
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { el.hidden = true; }, 6000);
}

let asking = null;
export function askPin(message) {
  if (asking) return asking;
  asking = new Promise((done) => {
    const box = document.createElement("form");
    box.className = "pin-ask";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Operator PIN");
    box.innerHTML = `<div class="kicker">Control locked</div>
      <p></p>
      <input type="password" autocomplete="off" inputmode="numeric" aria-label="Operator PIN" required>
      <div class="pin-btns"><button type="button" data-x>Cancel</button><button type="submit" class="primary">Unlock</button></div>`;
    box.querySelector("p").textContent = message;
    const end = (v) => { box.remove(); asking = null; done(v); };
    box.onsubmit = (e) => { e.preventDefault(); end(box.querySelector("input").value.trim()); };
    box.querySelector("[data-x]").onclick = () => end("");
    box.addEventListener("keydown", (e) => { if (e.key === "Escape") end(""); });
    document.body.appendChild(box);
    box.querySelector("input").focus();
  });
  return asking;
}
