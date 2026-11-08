// gaia working on the screen (#171). A reply with several UI directives plays them one by one,
// 600 to 900 ms apart, under a small live step list in the stream that ticks as each runs:
//   flying to pump 3 · tracing pipes to tank 3 · jumping to 2:47 am · opening the plans
// One directive (or none) applies at once, as before. Every directive still goes through
// directives.apply (the whitelist), so nothing here can move equipment.

import { esc } from "../console/stream.js";

const reduced = () => typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
const PANEL_WORDS = { plans: "the plans", walk: "the plan walk", whatif: "the what-if", asset: "the selection", log: "the shift log", tanks: "the tank levels", alerts: "the alerts" };

// The words for a step: the directive's own `step` when the backend gave one, else ours.
export function stepLabel(d, nice = (x) => x) {
  if (d && typeof d.step === "string" && d.step.trim()) return d.step.trim().toLowerCase().slice(0, 60);
  const a = (d && d.args) || {};
  const who = d && d.asset_id && d.asset_id !== "ALL" ? String(nice(d.asset_id)).toLowerCase() : "";
  switch (d && d.action) {
    case "focus": return who ? `flying to ${who}` : "moving the camera";
    case "trace_pipes": return who ? `tracing pipes from ${who}` : "tracing pipes";
    case "set_time": return a.clock ? `jumping to ${String(a.clock).toLowerCase()}` : Number(a.t_min) ? `jumping ${Math.round(Number(a.t_min))} min ahead` : "back to now";
    case "open_panel": return `opening ${PANEL_WORDS[a.panel] || "a panel"}`;
    case "highlight": return "marking what matters";
    case "show_sensors": return who ? `showing ${who}'s sensors` : "showing every sensor";
    case "explode": return who ? `opening up ${who}` : "opening it up";
    case "isolate": return who ? `isolating ${who}` : "isolating it";
    case "collapse": return "putting it back";
    case "reset_view": return "resetting the view";
    case "try_whatif": return "drawing the what-if";
    case "chart": return "drawing the chart";
    default: return "working";
  }
}

// What the view says when a step did nothing: the label then says that, never the claim.
const NOPE = /^(no pipes|that panel isn't|i don't know|i couldn't|nothing is open)/i;

// makeSteps({ stream, apply(d) -> said, nice, hasId, pause }) -> { play(directives) -> Promise<said>, busy() }
export function makeSteps({ stream, apply, nice, hasId = null, pause = 750 }) {
  let busy = false;
  let run = 0;

  async function play(list) {
    const ds = (list || []).filter((d) => d && d.action);
    if (ds.length <= 1) {
      const said = ds.length ? apply(ds[0]) || "" : "";
      return said;
    }
    const me = ++run;
    busy = true;
    const items = ds.map((d) => `<li data-state="todo"><i aria-hidden="true"></i><span>${esc(stepLabel(d, nice))}</span></li>`).join("");
    const li = stream.line("steps", "", { extra: `<ol class="ag-steps" aria-label="gaia's steps">${items}</ol>` });
    li.classList.add("ag-steps-b");
    const rows = [...li.querySelectorAll(".ag-steps li")];
    const wait = reduced() ? 250 : pause;
    let first = "";
    for (let i = 0; i < ds.length; i++) {
      if (me !== run) break; // a newer answer took over the screen
      rows[i].dataset.state = "now";
      const d = ds[i];
      const span = rows[i].querySelector("span");
      if (hasId && d.asset_id && d.asset_id !== "ALL" && !hasId(d.asset_id)) {
        rows[i].dataset.state = "skip";
        span.textContent = `${stepLabel(d, nice)}: not on this screen`;
        await new Promise((res) => setTimeout(res, wait / 2));
        continue;
      }
      let said = "";
      try {
        const r = apply(d);
        said = (r && typeof r.then === "function" ? await Promise.race([r.catch(() => ""), new Promise((res) => setTimeout(() => res(""), 2500))]) : r) || "";
      } catch (e) { console.warn("gaia step", e); rows[i].dataset.state = "skip"; }
      if (typeof said !== "string") said = "";
      let m;
      if (NOPE.test(said)) { rows[i].dataset.state = "skip"; span.textContent = said.split(/\.\s/)[0].replace(/\.\s*$/, "").toLowerCase().slice(0, 80); }
      else if ((m = /^(\d+) pipes? touch/i.exec(said))) span.textContent += ` · ${m[1]} pipe${m[1] === "1" ? "" : "s"}`;
      if (said && !first && !NOPE.test(said)) first = said;
      await new Promise((res) => setTimeout(res, wait));
      if (rows[i].dataset.state === "now") rows[i].dataset.state = "done";
    }
    li.classList.add("ag-done");
    if (me === run) busy = false;
    return first;
  }

  // A newer play() or a stop (open palm, reset) leaves the remaining steps unplayed.
  function cancel() { run++; busy = false; }
  return { play, cancel, busy: () => busy };
}
