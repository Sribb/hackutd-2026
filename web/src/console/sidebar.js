// The Jarvis sidebar (spec §1): header (live dot, Hands toggle, ?, ×), the agent stream, the one
// fix card, quick chips, and the input with the selection chip. ~380 px on the right.

import { esc } from "./stream.js";

const CHIPS_NONE = [["What's failing", "What's failing?"], ["Time left", "Time left"], ["Cheapest fix", "What's the cheapest fix?"], ["Reset", "reset"]];
const CHIPS_SEL = [["Open", "open it"], ["Sensors", "show its sensors"], ["Pipes", "trace its pipes"], ["Isolate", "isolate it"], ["Reset", "reset"]];

export function mountSidebar(app, on) {
  const side = document.createElement("aside");
  side.id = "csJarvis";
  side.className = "cs-side";
  side.hidden = true;
  side.setAttribute("aria-label", "Jarvis");
  side.innerHTML = `
    <header class="cs-sh">
      <b>Jarvis</b><span class="cs-live" id="csLive"><span class="cs-dot gray"></span> <span>connecting</span></span>
      <span class="cs-grow"></span>
      <label class="cs-hands" title="Hand control: the camera stays on this laptop. Hands never confirm."><span>Hands</span>
        <input type="checkbox" id="csHands" role="switch" aria-label="Hand control"><i></i></label>
      <button type="button" class="cs-icon" id="csHelp" title="Gestures and keys" aria-label="Help">?</button>
      <button type="button" class="cs-icon" id="csClose" title="Close (J)" aria-label="Close Jarvis">×</button>
    </header>
    <div class="cs-help" id="csHelpCard" hidden>
      <b>Keys</b> J Jarvis · / ask · Esc reset view · T time<br>
      <b>Hands</b> Point to look · Pinch to open · Open palm to turn · Two hands to zoom. Hands never confirm a fix.
    </div>
    <ol class="cs-stream" id="csStream" aria-live="polite"></ol>
    <section class="cs-fix" id="csFix" hidden aria-live="assertive"></section>
    <div class="cs-quick" id="csQuick"></div>
    <form class="cs-ask" id="csAsk" autocomplete="off">
      <span class="cs-selchip" id="csSel" hidden></span>
      <input id="csInput" placeholder="Ask Jarvis…" aria-label="Ask Jarvis" enterkeyhint="send">
      <button type="submit" class="cs-send" aria-label="Send">⏎</button>
    </form>`;
  app.appendChild(side);
  const $ = (id) => side.querySelector(`#${id}`);
  $("csClose").onclick = () => on("close");
  $("csHelp").onclick = () => { $("csHelpCard").hidden = !$("csHelpCard").hidden; };
  $("csHands").onchange = (e) => on("hands", e.target.checked);
  $("csAsk").onsubmit = (e) => {
    e.preventDefault();
    const v = $("csInput").value;
    $("csInput").value = "";
    on("ask", v);
  };
  let chipsKey = "";
  return {
    el: side,
    stream: $("csStream"),
    fix: $("csFix"),
    input: $("csInput"),
    setLive(level, text) {
      const html = `<span class="cs-dot ${level}"></span> <span>${esc(text)}</span>`;
      if ($("csLive")._h !== html) { $("csLive").innerHTML = html; $("csLive")._h = html; }
    },
    setHands(state) {
      const box = $("csHands");
      box.checked = state === "live" || state === "starting";
      box.closest(".cs-hands").dataset.state = state;
      box.closest(".cs-hands").querySelector("span").textContent = state === "starting" ? "Starting…" : "Hands";
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
      const chips = id ? CHIPS_SEL : CHIPS_NONE;
      $("csQuick").innerHTML = chips.map(([label, say]) => `<button type="button" class="cs-q" data-say="${esc(say)}">${esc(label)}</button>`).join("");
      $("csQuick").querySelectorAll("[data-say]").forEach((b) => { b.onclick = () => on("ask", b.dataset.say); });
    },
  };
}
