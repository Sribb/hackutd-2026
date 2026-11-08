// Frame-rate guard for the field, and the toggle for the desert layer (#88).
//
// The desert layer is one root named SCENERY (about 115k extra triangles, soil and road
// textures). Nothing under it is clickable.
//
// After the field loads, the console measures its own frame rate. Under MIN_FPS it steps
// quality down, cheapest visual loss first, and stops as soon as the field is smooth:
//   1. pixel density 2x -> 1.5x   2. -> 1x   3. scenery off
// A step that doesn't raise the frame rate by 10% is undone, and the guard stops there.
// The model note says what it changed. A click on the Scenery toggle is remembered on this
// browser and turns the automatic scenery step off. ?scenery=on|off in the URL wins for this
// visit, and ?quality=full skips the guard entirely (for recording video on a fast machine).

const KEY = "freeboard.scenery";
const MIN_FPS = 45;
const WARMUP_MS = 1500;  // shaders compile and textures upload on the first frames
const SAMPLE_MS = 2000;

const param = (k) => new URLSearchParams(location.search).get(k);

function saved() {
  const q = param("scenery");
  if (q === "on" || q === "off") return q;
  try { return localStorage.getItem(KEY); } catch { return null; }
}

// Frames per second over busy time only: gaps over 250 ms (a hidden tab) don't count.
function measure(ms) {
  return new Promise((done) => {
    let frames = 0, busy = 0, last = performance.now();
    const tick = (now) => {
      const d = now - last;
      last = now;
      if (d < 250) { frames++; busy += d; }
      if (busy < ms) requestAnimationFrame(tick);
      else done((frames * 1000) / busy);
    };
    requestAnimationFrame(tick);
  });
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

export function mountScenery(root, noteEl, renderer) {
  if (!noteEl) return null;
  const layer = root && root.getObjectByName("SCENERY");
  const why = document.createElement("span");
  why.className = "scenery-why";
  let btn = null;

  const set = (on) => {
    if (!layer) return;
    layer.visible = on;
    btn.textContent = on ? "Hide scenery" : "Show scenery"; // says what a click does
    btn.setAttribute("aria-pressed", String(on));
  };

  if (layer) {
    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "scenery-btn";
    noteEl.append(" · ", btn);
    noteEl.classList.add("has-toggle");
    btn.onclick = () => {
      set(!layer.visible);
      try { localStorage.setItem(KEY, layer.visible ? "on" : "off"); } catch { /* private window */ }
    };
  }
  noteEl.append(why);

  const choice = saved();
  set(choice !== "off");
  if (param("quality") === "full") return { set };

  const dpr = renderer ? renderer.getPixelRatio() : 1;
  const steps = [];
  if (renderer && dpr > 1.5) steps.push({ label: "1.5x pixels", run: () => renderer.setPixelRatio(1.5), undo: () => renderer.setPixelRatio(dpr) });
  if (renderer && dpr > 1) steps.push({ label: "1x pixels", run: () => renderer.setPixelRatio(1), undo: () => renderer.setPixelRatio(Math.min(dpr, 1.5)) });
  if (layer && !choice) steps.push({ label: "scenery off", run: () => set(false), undo: () => set(true) });

  (async () => {
    await wait(WARMUP_MS);
    let fps = await measure(SAMPLE_MS);
    const first = fps;
    const done = [];
    for (const s of steps) {
      if (fps >= MIN_FPS) break;
      s.run();
      await wait(300);
      const next = await measure(SAMPLE_MS);
      // A step that doesn't buy at least 10% means the bottleneck is elsewhere (a busy CPU,
      // not pixels or triangles). Put it back: losing quality for nothing helps nobody.
      if (next < fps * 1.1) { s.undo(); break; }
      done.push(s.label);
      fps = next;
    }
    if (done.length) {
      why.textContent = `Auto: ${done.join(", ")} (${Math.round(first)} to ${Math.round(fps)} fps)`;
      why.title = `The field ran at ${Math.round(first)} fps, so quality stepped down to keep it smooth. Add ?quality=full to the URL to skip this.`;
    }
  })();
  return { set };
}
