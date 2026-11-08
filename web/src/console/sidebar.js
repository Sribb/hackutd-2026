// The gaia conversation (the center column, ChatGPT style): a quiet header with the thread
// title, the thread itself (agent stream, then the one fix card), and the composer at the
// bottom with mic, send and a one-line disclaimer. With nothing in the thread, a greeting and
// suggestion pills sit around the composer in the middle of the column.

import { esc } from "./stream.js";
import { ICON } from "../gaia/icons.js";

const SUGGEST = [["what's failing", "What's failing?"], ["how long do we have", "Time left"], ["cheapest fix", "What's the cheapest fix?"], ["show tank 3", "show tank 3"]];
const CHIPS_SEL = [["open", "open it"], ["sensors", "show its sensors"], ["pipes", "trace its pipes"], ["isolate", "isolate it"], ["reset", "reset"]];

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
      keys: j chat · / ask · esc reset view · t time<br>
      hands: point to look · pinch to open · open palm to turn · two hands to zoom. hands never confirm a fix.
    </div>
    <div class="cs-scroll" id="csScroll">
      <div class="cs-hello" id="csHello"><h1 id="csHelloText">what's happening on the field?</h1></div>
      <ol class="cs-stream" id="csStream" aria-live="polite"></ol>
      <section class="cs-fix" id="csFix" hidden aria-live="assertive"></section>
    </div>
    <div class="cs-dock">
      <div class="cs-quick" id="csQuick"></div>
      <form class="cs-ask" id="csAsk" autocomplete="off">
        <input id="csInput" placeholder="ask gaia anything" aria-label="Ask gaia" enterkeyhint="send">
        <div class="cs-ask-row">
          <span class="cs-selchip" id="csSel" hidden></span>
          <label class="cs-hands" title="Hand control: the camera stays on this laptop. Hands never confirm."><input type="checkbox" id="csHands" role="switch" aria-label="Hand control"><i></i><span>hands</span></label>
          <span class="cs-grow"></span>
          <button type="button" class="cs-mic" id="csMic" title="Speak to gaia" aria-label="Speak to gaia">${ICON.mic}</button>
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
    send: $("csSend"),
    setTitle(t) { if ($("csTitle").textContent !== t) $("csTitle").textContent = t; },
    setHello(t) { if ($("csHelloText").textContent !== t) $("csHelloText").textContent = t; },
    // The "show tank 3" pill names an asset this field has (dataset mode: its first tank).
    suggestAsset(say) { const b = $("csSugg").querySelector("[data-k=asset]"); if (b) { b.textContent = say; b.dataset.say = say; } },
    setLive(level, text) {
      const html = `<span class="cs-dot ${level}"></span> <span>${esc(text)}</span>`;
      if ($("csLive")._h !== html) { $("csLive").innerHTML = html; $("csLive")._h = html; }
    },
    setHands(state) {
      const box = $("csHands");
      box.checked = state === "live" || state === "starting";
      box.closest(".cs-hands").dataset.state = state;
      box.closest(".cs-hands").querySelector("span").textContent = state === "starting" ? "starting…" : "hands";
    },
    setSelection(id, nice) {
      const key = id ? `sel:${id}` : "none";
      const sel = $("csSel");
      sel.hidden = !id;
      if (id) {
        sel.innerHTML = `${esc(nice(id))} <button type="button" aria-label="Clear selection">×</button>`;
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
