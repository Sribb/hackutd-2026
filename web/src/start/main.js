// The start page (/start, #147): the opening of the judge flow.
//
//   intro     the word "gaia" writes itself, slower than the console's intro, then "continue"
//   landing   one paragraph on what gaia is, over our own Blender render, gaia's tree growing
//             below; "track my oil field" opens three choices
//   phone     hackathon judges leave a number for alerts (sessionStorage only, sent nowhere)
//   brief     you are the night-shift operator, one line at a time
//   build     the field being built in 3D (build.js), then "go to the console" -> /?tour=1
//
// The blueprint and photo choices go to the console's own upload screen (/#upload, upload.js).
// ?intro=0 skips the intro. window.__start reports the step for headless checks.

import "@fontsource/great-vibes/400.css";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "../auth.js"; // a #k= console token opened here is kept for this tab, so the console still has it
import "./start.css";
import { mountTree } from "./tree.js";
import { parseUsPhone, prettyUsPhone, readPhone, savePhone } from "./phone.js";

const $ = (id) => document.getElementById(id);
const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
const T = (ms) => (reduced ? Math.min(ms, 200) : ms);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const frame = () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
const params = new URLSearchParams(location.search);

let step = "intro";
let film = null, filmP = null, filmErr = null;
const setStep = (s) => { step = s; document.body.dataset.step = s; };
window.__start = { step: () => step, phone: () => readPhone(), built: () => !!(film && film.built()), filmError: () => filmErr && String(filmErr.message || filmErr),
  film: () => (film ? { ...film.source, assets: film.assets } : null) };

const tree = mountTree($("sTree"));

// ---------------------------------------------------------------- 1. intro ----
const intro = $("sIntro");
async function runIntro() {
  // Write the strokes in Great Vibes, not a fallback: wait for the local font, at most 400 ms.
  if (document.fonts && document.fonts.load) {
    await Promise.race([document.fonts.load('400 160px "Great Vibes"', "gaia").catch(() => {}), sleep(400)]);
  }
  intro.classList.add("go");
  await sleep(T(5600)); // the last letter fills at about 4.9 s
  if (step !== "intro") return;
  $("sContinue").hidden = false;
  await frame();
  intro.classList.add("settled");
}
$("sContinue").addEventListener("click", () => toLanding());

// ---------------------------------------------------------------- steps ----
async function swap(next) {
  const cur = document.querySelector(".s-step.in");
  if (cur && cur !== next) {
    cur.classList.remove("in");
    await sleep(T(800));
    cur.hidden = true;
  }
  next.hidden = false;
  await frame();
  next.classList.add("in");
}

// ---------------------------------------------------------------- 2. landing ----
async function toLanding() {
  if (step !== "intro") return;
  setStep("landing");
  intro.classList.add("out");
  const stage = $("sStage");
  stage.hidden = false;
  await frame();
  stage.classList.add("on");
  tree.grow();
  setTimeout(() => { intro.hidden = true; }, T(1500));
  await sleep(T(500));
  await swap($("sLanding"));
  $("sTrack").focus({ preventScroll: true });
}

$("sTrack").addEventListener("click", async () => {
  const choices = $("sChoices");
  if (!choices.hidden) return;
  $("sTrack").closest(".s-actions").hidden = true;
  choices.hidden = false;
  await frame();
  choices.classList.add("in");
  choices.querySelector(".s-choice").focus({ preventScroll: true });
});

// ---------------------------------------------------------------- 3. phone ----
const input = $("sPhoneIn"), err = $("sPhoneErr");
$("sJudges").addEventListener("click", async () => {
  if (step !== "landing") return;
  setStep("phone");
  prefetchFilm(); // three.js and the model load while the judge types and reads
  const saved = readPhone();
  if (saved) input.value = prettyUsPhone(saved);
  await swap($("sPhone"));
  input.focus({ preventScroll: true });
});
input.addEventListener("input", () => { err.textContent = ""; input.removeAttribute("aria-invalid"); });
$("sPhoneForm").addEventListener("submit", (e) => {
  e.preventDefault();
  if (step !== "phone") return;
  const e164 = parseUsPhone(input.value);
  if (!e164) {
    err.textContent = "that doesn't look like a phone number in the united states. try 10 digits, like (214) 555-0134.";
    input.setAttribute("aria-invalid", "true");
    input.focus();
    return;
  }
  savePhone(e164);
  input.value = prettyUsPhone(e164);
  toBrief();
});
$("sPhoneSkip").addEventListener("click", () => {
  if (step !== "phone") return;
  savePhone(""); // skipping clears a number left from earlier in this tab
  toBrief();
});

// ---------------------------------------------------------------- 4. briefing ----
async function toBrief() {
  setStep("brief");
  prefetchFilm();
  await swap($("sBrief"));
  const lines = [...$("sBriefLines").children];
  for (const [i, li] of lines.entries()) {
    if (step !== "brief") return;
    li.classList.add("in");
    await sleep(T(i === 0 ? 2800 : 3400));
  }
  const go = $("sBriefGo");
  go.hidden = false;
  await frame();
  go.classList.add("in");
  go.focus({ preventScroll: true });
}
$("sBriefGo").addEventListener("click", () => toBuild());

// ---------------------------------------------------------------- 5. build ----
function prefetchFilm() {
  if (!filmP) {
    filmP = import("./build.js").then((m) => m.loadFilm($("sFilm"))).then((f) => (film = f));
    filmP.catch((e) => { filmErr = e; });
  }
  return filmP;
}

let capTok = 0;
function caption(text) {
  const el = $("sCaption"), tok = ++capTok;
  el.classList.add("fade");
  setTimeout(() => { if (tok !== capTok) return; el.textContent = text; el.classList.remove("fade"); }, reduced ? 0 : 450);
}

async function toBuild() {
  if (step !== "brief") return;
  setStep("build");
  const b = $("sBuild"), stage = $("sStage");
  b.hidden = false;
  await frame();
  b.classList.add("on");
  setTimeout(() => { stage.classList.remove("on"); setTimeout(() => { stage.hidden = true; }, T(1600)); }, T(1200));
  await sleep(T(1400));
  b.classList.add("t1");
  await sleep(T(2000));
  b.classList.add("t2");
  caption("opening the field");
  await sleep(T(1400));
  try {
    await prefetchFilm();
    if (reduced) film.finish();
    else await film.play(caption);
    $("sBuildH").textContent = "your 3d model is ready";
    caption("tonight's shift starts in the console.");
  } catch (e) {
    filmErr = e;
    caption("the 3d preview could not load on this screen. the console will open your field.");
  }
  const go = $("sConsole");
  go.hidden = false;
  await frame();
  go.classList.add("in");
  go.focus({ preventScroll: true });
}

// ---------------------------------------------------------------- go ----
if (params.get("intro") === "0") {
  intro.classList.add("go", "settled");
  toLanding();
} else {
  runIntro();
}
