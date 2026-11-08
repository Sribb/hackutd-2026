// The gaia conversation (the center column, ChatGPT style): a quiet header with the thread
// title, the thread itself (agent stream, then the one fix card), and the composer at the
// bottom with mic, send and a one-line disclaimer. With nothing in the thread, a greeting and
// suggestion pills sit around the composer in the middle of the column.

import { esc } from "./stream.js";
import { ICON } from "../gaia/icons.js";

const SUGGEST = [["what's failing", "What's failing?"], ["how long do we have", "Time left"], ["cheapest fix", "What's the cheapest fix?"], ["show tank 3", "show tank 3"]];
const CHIPS_SEL = [["open", "open it"], ["sensors", "show its sensors"], ["pipes", "trace its pipes"], ["isolate", "isolate it"], ["reset", "reset"]];

// What to say, under the big mic: the examples follow the field (dataset mode names its own tank).
const talkEg = (asset) => ["walk me through it", asset, "what's the cheapest fix?"].map((t) => `<span>"${esc(t)}"</span>`).join(" ");

export function mountSidebar(app, on) {
  const side = document.createElement("section");
  side.id = "csJarvis";
  side.className = "cs-side cs-empty";
  side.setAttribute("aria-label", "gaia");
  side.innerHTML = `
    <header class="cs-sh">
      <button type="button" class="cs-icon" id="csNavOpen" title="Show threads" aria-label="Show threads">${ICON.sidebar}</button>
      <button type="button" class="cs-icon" id="csNewTop" title="New chat" aria-label="New chat">${ICON.compose}</button>
      <b id="csTitle">new chat</b>
      <span class="cs-live" id="csLive"><span class="cs-dot gray"></span> <span>connecting</span></span>
      <span class="cs-grow"></span>
      <button type="button" class="cs-icon" id="csHelp" title="Gestures and keys" aria-label="Help">?</button>
      <button type="button" class="cs-icon cs-fieldbtn" id="csFieldOpen" title="Show the field" aria-label="Show the field">${ICON.panel}</button>
      <button type="button" class="cs-icon" id="csClose" title="Hide the chat (J)" aria-label="Hide the chat">${ICON.expand}</button>
    </header>
    <div class="cs-help" id="csHelpCard" hidden>
      voice: hold space (or hold a mic) and talk; release to send. try "walk me through it", "show its sensors", "trace its pipes", "open the plans", "go to 3 am", "reset".<br>
      keys: j chat · / type · esc reset view · t time<br>
      hands: point to look · pinch to select · pinch and drag to turn · open palm to stop · two hands to zoom. hands never confirm a fix.
    </div>
    <div class="cs-scroll" id="csScroll">
      <div class="cs-hello" id="csHello"><h1 id="csHelloText">what's happening on the field?</h1>
        <div class="cs-talk-hero">
          <button type="button" class="cs-talk" id="csTalk" aria-label="Talk to gaia">${ICON.mic}</button>
          <p class="cs-talk-hint"><kbd>hold space</kbd> and talk, or hold the mic</p>
          <p class="cs-talk-eg" id="csTalkEg">${talkEg("show tank 3")}</p>
          <button type="button" class="cs-talk-hands" id="csHandsHero" aria-pressed="false">or use your hands: point, pinch, open palm</button>
        </div>
      </div>
      <ol class="cs-stream" id="csStream" aria-live="polite"></ol>
      <section class="cs-fix" id="csFix" hidden aria-live="assertive"></section>
    </div>
    <div class="cs-dock">
      <div class="cs-quick" id="csQuick"></div>
      <form class="cs-ask" id="csAsk" autocomplete="off">
        <input id="csInput" placeholder="or type to gaia" aria-label="Ask gaia" enterkeyhint="send">
        <div class="cs-ask-row">
          <span class="cs-selchip" id="csSel" hidden></span>
          <label class="cs-hands" title="Hand control: the camera stays on this laptop. Hands never confirm."><input type="checkbox" id="csHands" role="switch" aria-label="Hand control"><i></i><span>hands</span></label>
          <span class="cs-grow"></span>
          <button type="button" class="cs-mic cs-mic-main" id="csMic" aria-label="Talk to gaia">${ICON.mic}</button>
          <button type="submit" class="cs-send" id="csSend" aria-label="Send">${ICON.up}</button>
        </div>
      </form>
      <div class="cs-sugg" id="csSugg">${SUGGEST.map(([l, s]) => `<button type="button" class="cs-pill-btn" data-say="${esc(s)}"${s === "show tank 3" ? ` data-k="asset"` : ""}>${esc(l)}</button>`).join("")}</div>
      <p class="cs-disclaimer">gaia proposes; a person confirms every action</p>
    </div>`;
  app.appendChild(side);
  const $ = (id) => side.querySelector(`#${id}`);
  $("csClose").onclick = () => on("close");
  $("csFieldOpen").onclick = () => on("field");
  $("csNavOpen").onclick = () => on("nav");
  $("csNewTop").onclick = () => on("new");
  $("csHelp").onclick = () => { $("csHelpCard").hidden = !$("csHelpCard").hidden; };
  $("csHands").onchange = (e) => on("hands", e.target.checked);
  $("csHandsHero").onclick = () => { const box = $("csHands"); box.checked = !box.checked; on("hands", box.checked); };
  $("csAsk").onsubmit = (e) => {
    e.preventDefault();
    const v = $("csInput").value;
    $("csInput").value = "";
    $("csSend").classList.remove("on");
    on("ask", v);
  };
  $("csInput").addEventListener("input", () => $("csSend").classList.toggle("on", !!$("csInput").value.trim()));
  $("csSugg").querySelectorAll("[data-say]").forEach((b) => { b.onclick = () => on("ask", b.dataset.say); });
  let chipsKey = "";
  return {
    el: side,
    stream: $("csStream"),
    scroll: $("csScroll"),
    fix: $("csFix"),
    input: $("csInput"),
    mic: $("csMic"),
    talk: $("csTalk"),
    send: $("csSend"),
    setTitle(t) { if ($("csTitle").textContent !== t) $("csTitle").textContent = t; },
    setHello(t) { if ($("csHelloText").textContent !== t) $("csHelloText").textContent = t; },
    // The "show tank 3" pill names an asset this field has (dataset mode: its first tank).
    suggestAsset(say) {
      const b = $("csSugg").querySelector("[data-k=asset]");
      if (b) { b.textContent = say; b.dataset.say = say; }
      const eg = $("csTalkEg"), html = talkEg(say);
      if (eg._h !== html) { eg.innerHTML = html; eg._h = html; }
    },
    setLive(level, text) {
      const html = `<span class="cs-dot ${level}"></span> <span>${esc(text)}</span>`;
      if ($("csLive")._h !== html) { $("csLive").innerHTML = html; $("csLive")._h = html; }
    },
    setHands(state) {
      const box = $("csHands");
      box.checked = state === "live" || state === "starting";
      box.closest(".cs-hands").dataset.state = state;
      box.closest(".cs-hands").querySelector("span").textContent = state === "starting" ? "starting…" : "hands";
      const hero = $("csHandsHero");
      hero.setAttribute("aria-pressed", String(box.checked));
      hero.textContent = state === "starting" ? "starting the camera…" : box.checked ? "hands on: point to look, pinch to select, open palm to stop" : state === "unavailable" ? "hand control needs a camera" : "or use your hands: point, pinch, open palm";
    },
    setSelection(id, nice) {
      const key = id ? `sel:${id}` : "none";
      const sel = $("csSel");
      sel.hidden = !id;
      if (id) {
        // "about tank 3", "about S4 tank c": what "this" and "it" mean to gaia (IDs keep their case).
        const words = String(nice(id)).split(" ").map((w) => (/\d/.test(w) ? w : w.toLowerCase())).join(" ");
        sel.innerHTML = `about ${esc(words)} <button type="button" aria-label="Clear selection">×</button>`;
        sel.title = `gaia reads "this", "it" and "that" as ${nice(id)}`;
        sel.querySelector("button").onclick = () => on("deselect");
      }
      if (key === chipsKey) return;
      chipsKey = key;
      const chips = id ? CHIPS_SEL : [];
      $("csQuick").innerHTML = chips.map(([label, say]) => `<button type="button" class="cs-q" data-say="${esc(say)}">${esc(label)}</button>`).join("");
      $("csQuick").querySelectorAll("[data-say]").forEach((b) => { b.onclick = () => on("ask", b.dataset.say); });
    },
  };
}
