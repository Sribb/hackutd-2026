// The gaia intro: a near-black screen with the word "gaia" written on in white, lowercase
// cursive (Great Vibes, self-hosted), then a fade into the console.
//
//   const intro = mountIntro();   at page start, before the field loads
//   intro.ready()                 the model and the first data are in
//   intro.fail()                  boot failed: get out of the way so the error shows
//
// How it is drawn: one SVG <text>, one <tspan> per letter. Each letter's outline is drawn with
// stroke-dasharray / stroke-dashoffset (staggered g, a, i, a over about 1.9 s), then its fill
// fades in. Under prefers-reduced-motion the word is shown filled and still.
//
// Timing: shown at least MIN_MS so the word finishes writing, and never more than PAST_READY_MS
// past ready. ?intro=0 skips it (tests). window.__gaiaIntro.state() says where it is.

import "@fontsource/great-vibes/400.css";
import "./intro.css";

const MIN_MS = 2200;
const PAST_READY_MS = 1000;
const FADE_MS = 700;
const SLOW_MS = 3200;

export function mountIntro() {
  const skip = new URLSearchParams(location.search).get("intro") === "0";
  const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  let state = skip ? "gone" : "writing";
  const t0 = performance.now();
  const api = { ready() {}, fail() {}, state: () => state };
  window.__gaiaIntro = { state: () => state, reduced };
  if (skip) return api;

  const el = document.createElement("div");
  el.className = `gaia-intro${reduced ? " still" : ""}`;
  el.setAttribute("role", "img");
  el.setAttribute("aria-label", "gaia");
  el.innerHTML = `
    <svg class="gaia-word" viewBox="0 0 640 280" aria-hidden="true">
      <text x="320" y="196" text-anchor="middle">${[..."gaia"].map((c, i) => `<tspan class="l l${i}">${c}</tspan>`).join("")}</text>
    </svg>
    <p class="gaia-sub" hidden>reading the field</p>`;
  document.body.appendChild(el);
  document.body.classList.add("gaia-intro-on");

  // Start writing once the font is in, so the strokes trace Great Vibes and not a fallback.
  // The file is local, so this is a few ms; never wait more than 400 ms for it.
  const go = () => el.classList.add("go");
  if (document.fonts && document.fonts.load) {
    Promise.race([document.fonts.load('400 160px "Great Vibes"', "gaia"), new Promise((r) => setTimeout(r, 400))]).then(go, go);
  } else go();

  const slow = setTimeout(() => { if (state !== "gone") el.querySelector(".gaia-sub").hidden = false; }, SLOW_MS);

  function leave() {
    if (state === "leaving" || state === "gone") return;
    state = "leaving";
    clearTimeout(slow);
    el.classList.add("out");
    document.body.classList.remove("gaia-intro-on");
    setTimeout(() => { el.remove(); state = "gone"; }, reduced ? 200 : FADE_MS);
  }

  api.ready = () => {
    if (state !== "writing") return;
    state = "ready";
    // The field is drawing now: let it show through, darkened and blurred, for the last moment.
    el.classList.add("tint");
    const now = performance.now();
    const wait = Math.max(0, Math.min(t0 + MIN_MS - now, PAST_READY_MS));
    setTimeout(leave, wait);
  };
  api.fail = () => leave();

  // Boot failed in main.js (backend down, a crash): the #loading card carries the words.
  const loading = document.getElementById("loading");
  if (loading) {
    new MutationObserver(() => {
      if (loading.classList.contains("down") || /Failed to start/.test(loading.textContent || "")) leave();
    }).observe(loading, { attributes: true, childList: true, characterData: true, subtree: true });
  }
  return api;
}
