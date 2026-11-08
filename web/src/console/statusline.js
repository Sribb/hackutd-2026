// The 32 px status line (spec §1). One row over the full-bleed 3D field:
//   FREEBOARD 1:52 AM │ Feed │ WATCH ● 1 alarm +3 │ FORECAST ▓▓▓░ Tank 3 55 min │ PLAN $fix vs $wait │ ⚑ 1 fix waiting │ Jarvis ●
// Click any segment to open Jarvis at that agent's latest message. FORECAST also shows the timebar.

import { esc } from "./stream.js";

export function mountStatusLine(host, on) {
  const bar = document.createElement("header");
  bar.id = "csStatus";
  bar.className = "cs-status";
  bar.setAttribute("role", "status");
  bar.innerHTML = `
    <span class="cs-brand">FREEBOARD</span><time id="csClock">--:--</time>
    <button type="button" class="cs-seg" id="csFeed" data-seg="W"></button>
    <button type="button" class="cs-seg" id="csWatch" data-seg="W"><span class="k">WATCH</span><span class="v"></span></button>
    <button type="button" class="cs-seg" id="csForecast" data-seg="F"><span class="k">FORECAST</span><span class="v"></span></button>
    <button type="button" class="cs-seg" id="csPlan" data-seg="P"><span class="k">PLAN</span><span class="v"></span></button>
    <button type="button" class="cs-seg cs-pill" id="csPending" data-seg="FIX" hidden></button>
    <span class="cs-iso" id="csIso" hidden></span>
    <span class="cs-grow"></span>
    <button type="button" class="cs-seg cs-jtab" id="csJarvisSeg" data-seg="J" aria-label="Jarvis (J)">Jarvis <i class="cs-unread" hidden></i></button>`;
  host.prepend(bar);
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
      set($("csFeed"), `<span class="cs-dot ${s.feed.level}"></span> ${esc(s.feed.text)}`, `cs-seg cs-feed ${s.feed.level}`);
      $("csFeed").title = s.feed.title || "";
      // Jarvis open narrows the line: each segment has a full and a short form (CSS picks one).
      set($("csWatch").querySelector(".v"), `<span class="cs-dot ${s.watch.level}"></span> <span class="full">${esc(s.watch.text)}</span><span class="short">${esc(s.watch.short || s.watch.text)}</span>`);
      set($("csForecast").querySelector(".v"), `${s.forecast.bar}<span class="t full">${esc(s.forecast.text)}</span><span class="t short">${esc(s.forecast.short || s.forecast.text)}</span>`);
      $("csForecast").title = s.forecast.title || "";
      $("csForecast").classList.toggle("unknown", !!s.forecast.unknown);
      set($("csPlan").querySelector(".v"), esc(s.plan.text));
      $("csPlan").title = s.plan.title || "";
      const pend = $("csPending");
      pend.hidden = !s.pending;
      if (s.pending) set(pend, `⚑ ${s.pending} fix${s.pending === 1 ? "" : "es"} waiting`);
      const iso = $("csIso");
      iso.hidden = !s.isolated;
      if (s.isolated) set(iso, `Isolated: ${esc(s.isolated)} · <button type="button" class="link" data-showall>Show all</button>`);
      const all = iso.querySelector("[data-showall]");
      if (all) all.onclick = () => on("RESET");
      bar.querySelector(".cs-unread").hidden = !s.unread;
      $("csJarvisSeg").classList.toggle("open", !!s.open);
    },
  };
}
