// Hand control (#45). Behind the Hands button; the mouse still does everything.
//
//   point (index finger out)   moves the cursor and highlights what's under it   = hover
//   pinch (thumb to index)     selects it and opens Jarvis on it                 = click
//   open palm, moving          orbits the camera                                 = drag
//   two hands apart/together   zooms                                             = wheel
//
// Agentic mode (the gaia console, #171: hooks.agentic): pinch selects what's under the cursor
// when it lets go (the selection chip follows), pinch and drag orbits, and an open palm is
// "stop": hooks.palm() hushes gaia and closes panels. Point still hovers.
//
// MediaPipe Hand Landmarker (@mediapipe/tasks-vision, Apache-2.0) runs in the browser. The
// camera feed never leaves the laptop. The gesture rules follow AirBench
// (github.com/VARSHITH707/AirBench, MIT): a gesture counts only after it holds for a few
// frames, and the cursor is smoothed so a shaky hand doesn't jitter the selection.

const MODEL_URL = "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task";
const LOCAL_MODEL = "/mediapipe/hand_landmarker.task"; // drop the file here to run with no network
const HOLD_FRAMES = 4;      // a gesture must hold this many frames before it counts
const SMOOTH = 0.35;        // cursor smoothing, 0..1 (higher follows faster)
const PINCH_ON = 0.33, PINCH_OFF = 0.45; // thumb-index gap / hand size, with hysteresis
const DRAG_START = 0.03;    // agentic pinch: the hand moves this far (of the frame) before it orbits

const $ = (id) => document.getElementById(id);

// Loads the WASM runtime (served from /mediapipe/wasm) and the hand model. GPU first, CPU if not.
export async function loadLandmarker() {
  const { FilesetResolver, HandLandmarker } = await import("@mediapipe/tasks-vision");
  const files = await FilesetResolver.forVisionTasks("/mediapipe/wasm");
  const local = await fetch(LOCAL_MODEL, { method: "HEAD" }).then((r) => r.ok && !/text\/html/.test(r.headers.get("content-type") || "")).catch(() => false);
  const opts = (delegate) => ({
    baseOptions: { modelAssetPath: local ? LOCAL_MODEL : MODEL_URL, delegate },
    runningMode: "VIDEO", numHands: 2,
    minHandDetectionConfidence: 0.6, minHandPresenceConfidence: 0.6, minTrackingConfidence: 0.5,
  });
  return HandLandmarker.createFromOptions(files, opts("GPU")).catch(() => HandLandmarker.createFromOptions(files, opts("CPU")));
}

export function mountHands(hooks) {
  // hooks: { canvas, stage, hoverAt(x, y) -> id|null, clickAt(x, y) -> id|null, orbit(dx, dy), zoom(factor),
  //          agentic?: bool, palm?() }
  const agentic = !!hooks.agentic;
  const btn = document.createElement("button");
  btn.id = "handsBtn";
  btn.className = "hands-btn";
  btn.textContent = "Hands";
  btn.title = "Control the field with your hands (camera stays on this laptop)";
  hooks.stage.appendChild(btn);

  const hud = document.createElement("div");
  hud.className = "hands-hud";
  hud.hidden = true;
  hud.innerHTML = `<video id="handsVideo" playsinline muted></video>
    <div class="hands-read"><b id="handsGesture">starting</b><span id="handsFps"></span></div>
    <div class="hands-legend">${agentic ? "point = hover · pinch = select · pinch and drag = turn · open palm = stop · two hands = zoom" : "point = hover · pinch = select · open palm = orbit · two hands = zoom"}</div>`;
  hooks.stage.appendChild(hud);

  const cursor = document.createElement("div");
  cursor.className = "hands-cursor";
  cursor.hidden = true;
  hooks.stage.appendChild(cursor);

  let running = false, landmarker = null, stream = null, raf = 0;
  btn.onclick = () => (running ? stop() : start());

  async function start() {
    btn.disabled = true;
    btn.textContent = "Starting";
    try {
      landmarker = await loadLandmarker();
      stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480, facingMode: "user" }, audio: false });
      const video = $("handsVideo");
      video.srcObject = stream;
      await video.play();
      running = true;
      hud.hidden = false;
      btn.textContent = "Hands off";
      loop();
    } catch (e) {
      btn.textContent = "Hands";
      hud.hidden = false;
      $("handsGesture").textContent = e && e.name === "NotAllowedError" ? "camera blocked" : "hands unavailable";
      $("handsFps").textContent = e && e.message ? e.message.slice(0, 80) : "";
      console.warn("hand control:", e);
      stop(true);
    } finally {
      btn.disabled = false;
    }
  }

  function stop(keepHud = false) {
    running = false;
    cancelAnimationFrame(raf);
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    if (!keepHud) hud.hidden = true;
    cursor.hidden = true;
    btn.textContent = "Hands";
  }

  // ---------------------------------------------------------- per frame ----
  let last = -1, frames = 0, fpsT = performance.now();
  let cand = "none", candN = 0, gesture = "none";
  let cur = null;              // smoothed cursor in stage pixels
  let palmPrev = null, spanPrev = null, pinched = false;
  let pinchFrom = null, dragged = false, lastAt = null; // agentic pinch: where it began, did it move

  function loop() {
    if (!running) return;
    raf = requestAnimationFrame(loop);
    const video = $("handsVideo");
    if (video.readyState < 2 || video.currentTime === last) return;
    last = video.currentTime;
    const res = landmarker.detectForVideo(video, performance.now());
    frames++;
    const now = performance.now();
    if (now - fpsT > 1000) { $("handsFps").textContent = `${Math.round((frames * 1000) / (now - fpsT))} fps`; frames = 0; fpsT = now; }
    step(res.landmarks || []);
  }

  function step(hands) {
    const seen = classify(hands);
    if (seen === cand) candN++;
    else { cand = seen; candN = 1; }
    if (candN >= HOLD_FRAMES && gesture !== cand) {
      const was = gesture;
      gesture = cand;
      palmPrev = spanPrev = null;
      if (agentic && was === "pinch") endPinch();
      if (agentic && gesture === "palm" && hooks.palm) hooks.palm();
    }
    $("handsGesture").textContent = gesture === "none" ? "no hand" : gesture;
    cursor.className = `hands-cursor ${gesture}`;

    const rect = hooks.canvas.getBoundingClientRect();
    // The camera is a mirror: flip x so moving your hand right moves the cursor right.
    const toStage = (p) => ({ x: (1 - p.x) * rect.width, y: p.y * rect.height });

    if (gesture === "zoom" && hands.length === 2) {
      const span = dist(palm(hands[0]), palm(hands[1]));
      if (spanPrev) hooks.zoom(spanPrev / span);
      spanPrev = span;
      cursor.hidden = true;
      return;
    }
    if (!hands.length) { cursor.hidden = true; hooks.hoverAt(null); pinched = false; pinchFrom = null; return; }

    const h = hands[0];
    const target = gesture === "orbit" ? toStage(palm(h)) : toStage(h[8]);
    cur = cur ? { x: cur.x + (target.x - cur.x) * SMOOTH, y: cur.y + (target.y - cur.y) * SMOOTH } : target;
    cursor.hidden = false;
    cursor.style.transform = `translate(${cur.x}px, ${cur.y}px)`;
    const cx = rect.left + cur.x, cy = rect.top + cur.y;

    if (gesture === "orbit") {
      const p = palm(h);
      if (palmPrev) hooks.orbit(-(p.x - palmPrev.x), p.y - palmPrev.y); // mirrored x
      palmPrev = p;
      return;
    }
    if (agentic && gesture === "pinch") {
      // Pinch and drag orbits; a pinch that lets go without moving selects (endPinch).
      const p = palm(h);
      if (!pinchFrom) { pinchFrom = p; dragged = false; palmPrev = p; lastAt = { x: cx, y: cy }; }
      if (!dragged && dist(p, pinchFrom) > DRAG_START) dragged = true;
      if (dragged && palmPrev) hooks.orbit(-(p.x - palmPrev.x), p.y - palmPrev.y);
      else { lastAt = { x: cx, y: cy }; hooks.hoverAt(cx, cy); }
      palmPrev = p;
      return;
    }
    if (gesture === "point" || gesture === "pinch") hooks.hoverAt(cx, cy);
    if (!agentic && gesture === "pinch" && !pinched) { pinched = true; hooks.clickAt(cx, cy); }
    if (gesture !== "pinch") pinched = false;
  }

  function endPinch() {
    if (pinchFrom && !dragged && lastAt) hooks.clickAt(lastAt.x, lastAt.y);
    pinchFrom = null;
    dragged = false;
  }

  // ---------------------------------------------------------- gestures ----
  function classify(hands) {
    if (hands.length === 2 && hands.every((h) => openPalm(h) || pinchRatio(h) < PINCH_OFF)) return "zoom";
    if (!hands.length) return "none";
    const h = hands[0];
    const r = pinchRatio(h);
    if (r < PINCH_ON || (gesture === "pinch" && r < PINCH_OFF)) return "pinch";
    if (openPalm(h)) return agentic ? "palm" : "orbit";
    if (extended(h, 8, 6) && !extended(h, 12, 10) && !extended(h, 16, 14)) return "point";
    return gesture === "pinch" || gesture === "point" ? gesture : "none";
  }
  return { stop };
}

export const shape = {};
const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
const size = (h) => dist(h[0], h[9]) || 1e-3;                     // wrist to middle knuckle
const pinchRatio = (h) => dist(h[4], h[8]) / size(h);              // thumb tip to index tip
const extended = (h, tip, pip) => dist(h[tip], h[0]) > dist(h[pip], h[0]) * 1.15;
const openPalm = (h) => [8, 12, 16, 20].every((t) => extended(h, t, t - 2)) && pinchRatio(h) > 0.6;
const palm = (h) => ({ x: (h[0].x + h[5].x + h[17].x) / 3, y: (h[0].y + h[5].y + h[17].y) / 3 });
Object.assign(shape, { pinchRatio, openPalm, extended, palm });
