// The slim left icon rail (about 56 px): the tiny cursive gaia mark, then new chat, threads
// (opens the thread popover from threads.js), layers (time, sky and scenery for the field) and
// settings (light or dark shell, classic view, datasets). Light is the default shell; the 3D
// field keeps its own night look either way. The choice is remembered in this browser only.

import { ICON } from "./icons.js";

const KEY = "gaia.theme";
const GEAR = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></svg>';
const LAYERS = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l9 5-9 5-9-5z"/><path d="M3 13l9 5 9-5"/></svg>';

function readTheme() { try { return localStorage.getItem(KEY) === "dark" ? "dark" : "light"; } catch { return "light"; } }
export function applyTheme(t) {
  document.body.classList.toggle("cs-dark", t === "dark");
  document.body.classList.toggle("cs-light", t !== "dark");
  try { localStorage.setItem(KEY, t); } catch { /* private window: this visit only */ }
}

export function mountRail(app, H) {
  applyTheme(readTheme());
  const rail = document.createElement("nav");
  rail.className = "cs-rail";
  rail.setAttribute("aria-label", "gaia");
  rail.innerHTML = `
    <span class="cs-rail-mark" aria-label="gaia" title="gaia">gaia</span>
    <button type="button" class="cs-icon" data-r="new" title="New chat" aria-label="New chat">${ICON.compose}</button>
    <button type="button" class="cs-icon" data-r="threads" title="Threads" aria-label="Threads">${ICON.chat}<i class="cs-unread" hidden></i></button>
    <button type="button" class="cs-icon" data-r="layers" title="Layers: time, sky, scenery" aria-label="Layers">${LAYERS}</button>
    <span class="cs-grow"></span>
    <button type="button" class="cs-icon" data-r="settings" title="Settings" aria-label="Settings">${GEAR}</button>`;
  app.prepend(rail);
  const menu = document.createElement("div");
  menu.className = "cs-pop cs-settings";
  menu.hidden = true;
  menu.innerHTML = `
    <div class="cs-pop-label">theme</div>
    <div class="cs-seg2" role="radiogroup" aria-label="Theme">
      <button type="button" data-theme="light" role="radio">light</button><button type="button" data-theme="dark" role="radio">dark</button>
    </div>
    <a class="cs-navrow quiet" href="/dataset">datasets</a>
    <a class="cs-navrow quiet" data-classic href="?layout=classic">classic view</a>`;
  app.appendChild(menu);
  const mark = () => menu.querySelectorAll("[data-theme]").forEach((b) => b.setAttribute("aria-checked", String(document.body.classList.contains(`cs-${b.dataset.theme}`))));
  mark();
  menu.querySelectorAll("[data-theme]").forEach((b) => { b.onclick = () => { applyTheme(b.dataset.theme); mark(); }; });
  menu.querySelector("[data-classic]").href = (() => { const u = new URL(location.href); u.searchParams.set("layout", "classic"); u.searchParams.delete("intro"); return u.pathname + u.search; })();

  rail.querySelectorAll("[data-r]").forEach((b) => {
    b.onclick = (e) => {
      e.stopPropagation();
      const r = b.dataset.r;
      if (r === "settings") { menu.hidden = !menu.hidden; H.nav(false); return; }
      menu.hidden = true;
      if (r === "new") H.newChat();
      else if (r === "threads") H.nav(!H.navOpen());
      else if (r === "layers") H.layers();
    };
  });
  // A click anywhere else closes the popovers.
  addEventListener("pointerdown", (e) => {
    if (!e.target.closest) return;
    if (!e.target.closest(".cs-settings") && !e.target.closest('[data-r="settings"]')) menu.hidden = true;
    if (H.navOpen() && !e.target.closest(".cs-nav") && !e.target.closest('[data-r="threads"]') && !e.target.closest("#csNavOpen")) H.nav(false);
  }, true);
  return {
    el: rail,
    unread(v) { rail.querySelector('[data-r="threads"] .cs-unread').hidden = !v; },
  };
}
