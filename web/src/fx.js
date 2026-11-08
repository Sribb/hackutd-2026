// Small effects for the Night Survey look: decode-on-change text and hold-to-commit.
// Nothing here owns data. main.js calls these after it renders.

const GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789/\\<>[]#%+=-";
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const seen = new Map(); // decode key -> last text shown

// Any element with data-decode="key" scrambles in when its text differs from the last
// time that key was rendered. Re-rendering the same text does nothing, so panels that
// redraw on every slider tick stay still.
export function decode(root) {
  for (const el of root.querySelectorAll("[data-decode]")) {
    const key = el.dataset.decode;
    const text = el.textContent;
    if (seen.get(key) === text) continue;
    seen.set(key, text);
    if (reduced || !text.trim()) continue;
    scramble(el, text);
  }
}

function scramble(el, text) {
  const t0 = performance.now();
  const dur = Math.min(700, 180 + text.length * 14);
  const step = (now) => {
    if (!el.isConnected) return;
    const k = Math.min(1, (now - t0) / dur);
    const fixed = Math.floor(k * text.length);
    let out = text.slice(0, fixed);
    for (let i = fixed; i < text.length; i++) {
      out += text[i] === " " ? " " : GLYPHS[(Math.random() * GLYPHS.length) | 0];
    }
    el.textContent = out;
    if (k < 1) requestAnimationFrame(step);
    else el.textContent = text;
  };
  requestAnimationFrame(step);
}

// Hold-to-commit. The button fills over `ms`; letting go early cancels. Works with a
// mouse, a finger, or by holding Space or Enter while the button has focus.
// Holds in progress anywhere on the page. While one runs, lists that hold-to-confirm
// buttons live in must not redraw (that would swap the button under the finger), so they
// queue their redraw with afterHold() and it runs when the hold ends.
let activeHolds = 0;
const queued = new Set();
export const isHolding = () => activeHolds > 0;
export function afterHold(fn) {
  if (activeHolds > 0) queued.add(fn);
  else fn();
}
function holdEnded() {
  activeHolds = Math.max(0, activeHolds - 1);
  if (activeHolds) return;
  const run = [...queued];
  queued.clear();
  for (const fn of run) fn();
}

// Press and hold to commit. Only a full, unbroken hold on this exact button commits:
// letting go anywhere, losing focus, hiding the page, or the button leaving the page
// (a redraw) cancels it. A plain click never commits.
export function holdButton(btn, onCommit, ms = 1500) {
  let t0 = 0, raf = 0, done = false, holding = false;
  const set = (p) => btn.style.setProperty("--p", p.toFixed(3));
  const finish = () => {
    if (!holding) return;
    holding = false;
    cancelAnimationFrame(raf);
    btn.classList.remove("holding");
    window.removeEventListener("pointerup", stop, true);
    window.removeEventListener("pointercancel", stop, true);
    window.removeEventListener("keyup", keyStop, true);
    window.removeEventListener("blur", stop);
    document.removeEventListener("visibilitychange", stop);
  };
  const tick = (now) => {
    if (!holding) return;
    if (!btn.isConnected || btn.disabled) { stop(); return; } // redrawn or disabled mid-hold: never commit
    const p = Math.min(1, (now - t0) / ms);
    set(p);
    if (p >= 1) {
      done = true;
      finish();
      btn.classList.add("committed");
      if (navigator.vibrate) navigator.vibrate(30);
      try { onCommit(); } finally { holdEnded(); }
      return;
    }
    raf = requestAnimationFrame(tick);
  };
  const start = (e) => {
    if (done || holding || btn.disabled) return;
    if (e) e.preventDefault();
    holding = true;
    activeHolds++;
    t0 = performance.now();
    btn.classList.add("holding");
    // Listen on the window too: a release that lands anywhere else still ends the hold.
    window.addEventListener("pointerup", stop, true);
    window.addEventListener("pointercancel", stop, true);
    window.addEventListener("keyup", keyStop, true);
    window.addEventListener("blur", stop);
    document.addEventListener("visibilitychange", stop);
    raf = requestAnimationFrame(tick);
  };
  function stop() {
    if (done || !holding) return;
    finish();
    set(0);
    holdEnded();
  }
  function keyStop(e) { if (e.key === " " || e.key === "Enter") stop(); }
  btn.addEventListener("pointerdown", start);
  btn.addEventListener("pointerleave", stop);
  btn.addEventListener("keydown", (e) => {
    if ((e.key === " " || e.key === "Enter") && !e.repeat) start(e);
  });
  btn.addEventListener("click", (e) => e.preventDefault()); // a plain click never commits
  set(0);
}
