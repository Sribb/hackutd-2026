// The field's one quiet status line, over the top of the 3D canvas pane (spec §1, gaia look):
//   live · 1 alarm · tank 3 shuts in 55 min · best plan $1,798 vs $7,320 if nothing changes
// Plain lowercase text, no boxes, borders or hatching. Each part is a button: a click opens the
// conversation at that agent's latest message. On the right: the clock and the canvas controls
// (expand to full width, close the field pane).

import { esc } from "./stream.js";
import { ICON } from "../gaia/icons.js";

export function mountStatusLine(host, on) {
  const bar = document.createElement("header");
  bar.id = "csStatus";
  bar.className = "cs-status";
  bar.setAttribute("role", "status");
  bar.innerHTML = `
    <span class="cs-line">
      <button type="button" class="cs-seg cs-feed" id="csFeed" data-seg="W"></button>
      <button type="button" class="cs-seg" id="csWatch" data-seg="W"><span class="v"></span></button>
      <button type="button" class="cs-seg" id="csForecast" data-seg="F"><span class="v"></span></button>
      <button type="button" class="cs-seg" id="csPlan" data-seg="P"><span class="v"></span></button>
      <button type="button" class="cs-seg cs-pill" id="csPending" data-seg="FIX" hidden></button>
      <span class="cs-seg cs-iso" id="csIso" hidden></span>
    </span>
    <span class="cs-grow"></span>
    <time id="csClock">--:--</time>
    <button type="button" class="cs-icon" id="csCanvasFull" data-seg="FULL" title="Field full width" aria-label="Field full width">${ICON.expand}</button>
    <button type="button" class="cs-icon" id="csCanvasClose" data-seg="NOFIELD" title="Close the field pane" aria-label="Close the field pane">${ICON.close}</button>
    <button type="button" class="cs-seg cs-jtab" id="csJarvisSeg" data-seg="J" aria-label="Chat (J)" hidden>chat <i class="cs-unread" hidden></i></button>`;
  host.appendChild(bar);
  bar.querySelectorAll("[data-seg]").forEach((b) => { b.onclick = () => on(b.dataset.seg, b); });
  const $ = (id) => bar.querySelector(`#${id}`);
  const set = (el, html, cls) => {
    if (el._html !== html) { el.innerHTML = html; el._html = html; }
    if (cls != null && el.className !== cls) el.className = cls;
  };
  return {
    el: bar,
    render(s) {
      const c = $("csClock");
      if (c.textContent !== s.clock) c.textContent = s.clock;
      set($("csFeed"), `<span class="cs-dot ${s.feed.level}"></span>${esc(s.feed.text)}`, `cs-seg cs-feed ${s.feed.level}`);
      $("csFeed").title = s.feed.title || "";
      set($("csWatch").querySelector(".v"), `${s.watch.level !== "gray" ? `<span class="cs-dot ${s.watch.level}"></span>` : ""}<span class="full">${esc(s.watch.text)}</span>`);
      set($("csForecast").querySelector(".v"), `<span class="t full">${esc(s.forecast.text)}</span>`);
      $("csForecast").title = s.forecast.title || "";
      $("csForecast").classList.toggle("unknown", !!s.forecast.unknown);
      $("csPlan").hidden = !s.plan.text;
      set($("csPlan").querySelector(".v"), esc(s.plan.text));
      $("csPlan").title = s.plan.title || "";
      const pend = $("csPending");
      pend.hidden = !s.pending;
      if (s.pending) set(pend, `${s.pending} fix${s.pending === 1 ? "" : "es"} waiting`);
      const iso = $("csIso");
      iso.hidden = !s.isolated;
      if (s.isolated) set(iso, `isolated: ${esc(s.isolated)} · <button type="button" class="link" data-showall>show all</button>`);
      const all = iso.querySelector("[data-showall]");
      if (all) all.onclick = () => on("RESET");
      bar.querySelector(".cs-unread").hidden = !s.unread;
      $("csJarvisSeg").hidden = !!s.open;
      set($("csCanvasFull"), s.full ? ICON.shrink : ICON.expand);
      $("csCanvasFull").title = s.full ? "Back to the chat" : "Field full width";
    },
  };
}
