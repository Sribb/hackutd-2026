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
export function holdButton(btn, onCommit, ms = 1500) {
  let t0 = 0, raf = 0, done = false;
  const set = (p) => btn.style.setProperty("--p", p.toFixed(3));
  const tick = (now) => {
    const p = Math.min(1, (now - t0) / ms);
    set(p);
    if (p >= 1) {
      done = true;
      btn.classList.remove("holding");
      btn.classList.add("committed");
      if (navigator.vibrate) navigator.vibrate(30);
      onCommit();
      return;
    }
    raf = requestAnimationFrame(tick);
  };
  const start = (e) => {
    if (done || btn.disabled) return;
    if (e) e.preventDefault();
    cancelAnimationFrame(raf);
    t0 = performance.now();
    btn.classList.add("holding");
    raf = requestAnimationFrame(tick);
  };
  const stop = () => {
    if (done) return;
    cancelAnimationFrame(raf);
    btn.classList.remove("holding");
    set(0);
  };
  btn.addEventListener("pointerdown", start);
  btn.addEventListener("pointerup", stop);
  btn.addEventListener("pointerleave", stop);
  btn.addEventListener("pointercancel", stop);
  btn.addEventListener("keydown", (e) => {
    if ((e.key === " " || e.key === "Enter") && !e.repeat) start(e);
  });
  btn.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") stop(); });
  btn.addEventListener("click", (e) => e.preventDefault()); // a plain click never commits
  set(0);
}
