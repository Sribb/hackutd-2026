// The monitoring strip (#154): a slim glanceable row along the top of the 3D field.
//   tank 1 ▃▃▃▏ 52% · 4 s ago  ...  pumps 5 of 6 up  ·  wells 24 of 24  ·  1 alarm  ·  shutdown tank 3 in 58 min
// Calm (white on near-black) until something is wrong: amber for a warning, red for an alarm.
// It reads what the console already has, nothing new:
//   levels, pumps, wells   main.js viewAt(0): the live reading, else the /api/state snapshot
//   reading age            live.js ageText / staleMinutes
//   alerts                 the console's alert map (index.js), the same count the status line shows
//   shutdown               the brief's countdown, the one clock the status line and gaia read
// A missing or stale reading says "no reading", never a level, and never counts as up or online.
//
//   const m = mountMonitor(stage, { focus, openAlerts })
//   m.render(s)   s = { field, view, unknown, alerts, down, countdown, left, src, vouch, asOf, inSystem }

import * as liveFeed from "../live.js";
import { frozen } from "../trend.js";
import { esc } from "./stream.js";
import { clock12 } from "./agents.js";

const BLIND = new Set(["NO_DATA", "UNKNOWN"]);
const blind = (v) => !v || BLIND.has(v.status) || v.stale === true;
const lc = (s) => String(s).toLowerCase();
// A sponsor id's system prefix ("s4 tank c" from S4_TANK_C): the strip shows one system's assets.
const bare = (name) => String(name).replace(/^s\d+\s+/, "");
// "tank 3" or "tank c" in full, "t3" or "tc" where the canvas is narrow (CSS picks one).
const label = (name) => { const m = /^(\D+?)\s+(\w{1,3})$/.exec(bare(name)); return m ? `<i class="lg">${esc(m[1])} </i><i class="sm">${esc(m[1][0])}</i>${esc(m[2])}` : esc(bare(name)); };
// The console's clock: "2:48 AM" on the demo field, "14:05 UTC" on an EOG dataset.
const clockOf = (min) => (liveFeed.live.dataset && min != null ? `${String(Math.floor((((min % 1440) + 1440) % 1440) / 60)).padStart(2, "0")}:${String((((Math.round(min) % 60) + 60) % 60)).padStart(2, "0")} UTC` : clock12(min));

export function mountMonitor(host, H) {
  const el = document.createElement("div");
  el.className = "gm-strip";
  el.id = "gaiaMonitor";
  el.setAttribute("role", "status");
  el.setAttribute("aria-label", "Field monitor");
  el.innerHTML = `<div class="gm-in"><div class="gm-tanks"></div><div class="gm-stats"></div></div>`;
  host.appendChild(el);
  const tanksEl = el.querySelector(".gm-tanks"), statsEl = el.querySelector(".gm-stats");
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-focus],[data-alerts]");
    if (!b) return;
    if (b.dataset.focus) H.focus(b.dataset.focus);
    else H.openAlerts();
  });
  const set = (node, html) => { if (node._h !== html) { node._h = html; node.innerHTML = html; } };

  function tankHtml(t, s) {
    const v = s.view[t.id];
    const name = label(lc(s.nice(t.id)));
    const stuck = frozen(t.id);
    const noRead = blind(v) || v.level_pct == null || s.unknown.includes(t.id) && !stuck;
    const shut = `left:${Math.min(100, t.shutdown_pct)}%`;
    if (noRead) {
      const m = liveFeed.staleMinutes(t.id);
      const age = m != null && m >= 1 ? `for ${m} min` : liveFeed.live.lostSince ? "feed lost" : "unknown";
      const title = `${s.nice(t.id)}: no fresh level reading${m != null && m >= 1 ? ` for ${m} min` : ""}. Not treated as safe. Shutdown line ${t.shutdown_pct}%.`;
      return `<button type="button" class="gm-tank unknown" data-focus="${esc(t.id)}" title="${esc(title)}" aria-label="${esc(title)}">
        <span class="gm-l"><span>${name}</span><b><i class="lg">no reading</i><i class="sm">n/a</i></b></span><span class="gm-bar hatched"><s style="${shut}"></s></span><span class="gm-age">${esc(age)}</span></button>`;
    }
    const pct = v.level_pct;
    const warnAt = t.warning_pct != null ? t.warning_pct : t.shutdown_pct - 10;
    const lvl = v.status === "ALARM" || pct >= t.shutdown_pct ? "red" : v.status === "WARNING" || pct >= warnAt || stuck ? "amber" : "";
    const ageT = liveFeed.ageText(t.id);
    const age = stuck ? "stuck?" : ageT || (s.asOf ? `as of ${lc(s.asOf)}` : "");
    const src = stuck ? `the gauge has read exactly ${stuck.v.toFixed(2)}% for ${stuck.minutes} min and may be stuck`
      : ageT ? `live reading, ${ageT}` : `from the ${s.asOf || "boot"} snapshot (/api/state)`;
    const title = `${s.nice(t.id)}: ${pct.toFixed(1)}%, shutdown at ${t.shutdown_pct}%. ${src[0].toUpperCase()}${src.slice(1)}.`;
    return `<button type="button" class="gm-tank ${lvl}" data-focus="${esc(t.id)}" title="${esc(title)}" aria-label="${esc(title)}">
      <span class="gm-l"><span>${name}</span><b>${pct.toFixed(0)}%</b></span>
      <span class="gm-bar"><i style="width:${Math.max(0, Math.min(100, pct)).toFixed(1)}%"></i><s style="${shut}"></s></span><span class="gm-age">${esc(age)}</span></button>`;
  }

  // Up, down, no reading: a pump that trips reads ALARM or running false; a well that is shut in reads OFFLINE.
  function count(list, s, upFn) {
    const out = { up: 0, down: [], unknown: [] };
    for (const x of list) {
      const v = s.view[x.id];
      if (blind(v) && !(v && v.status === "ALARM")) out.unknown.push(x.id);
      else if (upFn(v)) out.up++;
      else out.down.push(x.id);
    }
    return out;
  }
  // value: [long, short] html; the short one stands alone (no label) on a narrow canvas.
  function stat(cls, key, [long, short], title, attrs = "") {
    return `<button type="button" class="gm-stat ${cls}" title="${esc(title)}" aria-label="${esc(title)}" ${attrs}><span class="gm-k">${esc(key)}</span><b><span class="lg">${long}</span><span class="sm">${short}</span></b></button>`;
  }

  return {
    el,
    render(s) {
      if (!s || !s.field) return;
      // Dataset mode: the 3D shows one system, so the strip does too (alerts and the countdown stay field-wide).
      const here = (x) => !s.inSystem || s.inSystem(x.id);
      const tanks = (s.field.tanks || []).filter(here);
      set(tanksEl, tanks.map((t) => tankHtml(t, s)).join(""));
      const live = liveFeed.live.available ? "the live feed" : `the ${s.asOf || "boot"} snapshot`;
      const pumpList = (s.field.pumps || []).filter(here), wellList = (s.field.wells || []).filter(here);
      const pumps = count(pumpList, s, (v) => v.status !== "ALARM" && v.running !== false && v.status !== "OFFLINE");
      const wells = count(wellList, s, (v) => v.status !== "OFFLINE" && v.status !== "ALARM");
      const np = pumpList.length, nw = wellList.length;
      const unk = (o) => (o.unknown.length ? ` <small>· ${o.unknown.length} no reading</small>` : "");
      const pumpsTxt = pumps.down.length === 1 ? `${esc(bare(lc(s.nice(pumps.down[0]))))} down` : `${pumps.up} of ${np} up`;
      const pumpsSm = pumps.down.length === 1 ? pumpsTxt : `pumps ${pumps.up}/${np}`;
      const cdName = s.countdown ? bare(lc(s.nice(s.countdown.asset_id))) : "";
      const cdLeft = s.countdown ? (s.left != null && s.left <= 0 ? "now" : `in ${Math.max(0, Math.round(s.left))} min`) : "";
      const html = [
        stat(pumps.down.length ? "red" : pumps.unknown.length ? "unknown" : "", "pumps", [`${pumpsTxt}${unk(pumps)}`, `${pumpsSm}${pumps.unknown.length ? "?" : ""}`],
          `${pumps.up} of ${np} pumps running${pumps.down.length ? `; down: ${pumps.down.map(s.nice).join(", ")}` : ""}${pumps.unknown.length ? `; no reading: ${pumps.unknown.map(s.nice).join(", ")}` : ""}. From ${live}.`,
          pumps.down.length ? `data-focus="${esc(pumps.down[0])}"` : ""),
        stat(wells.down.length ? "amber" : wells.unknown.length ? "unknown" : "", "wells", [`${wells.up} of ${nw} online${unk(wells)}`, `wells ${wells.up}/${nw}${wells.unknown.length ? "?" : ""}`],
          `${wells.up} of ${nw} wells flowing${wells.down.length ? `; shut in: ${wells.down.map(s.nice).join(", ")}` : ""}${wells.unknown.length ? `; no reading: ${wells.unknown.map(s.nice).join(", ")}` : ""}. From ${live}.`),
        s.down
          ? stat("red", "alerts", ["not received", "alerts down"], "Can't reach the backend, so alerts are not being received. Not the same as no alerts.", "data-alerts")
          : stat(s.alerts.red ? "red" : s.alerts.amber ? "amber" : "", "alerts", s.alerts.n ? [`${s.alerts.n} ${s.alerts.red ? "alarm" : "alert"}${s.alerts.n === 1 ? "" : "s"}`, `${s.alerts.n} ${s.alerts.red ? "alarm" : "alert"}${s.alerts.n === 1 ? "" : "s"}`] : ["none", "no alerts"],
            `${s.alerts.n} active root alert${s.alerts.n === 1 ? "" : "s"} from /api/alerts and the live socket${s.alerts.rel ? `, ${s.alerts.rel} related` : ""}.`, "data-alerts"),
        s.countdown
          ? stat(s.left != null && s.left <= 60 ? "red" : "amber", "shutdown",
            [`${esc(cdName)} ${cdLeft}`, `${esc(cdName.replace(/^(\D)\D*?\s+(\w{1,3})$/, "$1$2"))} ${cdLeft === "now" ? "at shutdown" : cdLeft}`],
            `${s.nice(s.countdown.asset_id)} reaches shutdown at ${clockOf(s.countdown.at)} if nobody acts, from ${s.src}.`, `data-focus="${esc(s.countdown.asset_id)}"`)
          : stat(s.vouch ? "" : "unknown", "shutdown", s.vouch ? ["none in 6 h", "no shutdown"] : ["can't vouch", "can't vouch"], s.vouch ? `No tank reaches shutdown in the next 6 hours, from ${s.src}.` : `No shutdown forecast, but the console can't vouch for the field (no live feed or a tank without a reading). From ${s.src}.`),
      ].join("");
      set(statsEl, html);
      const red = !!el.querySelector(".red"), amber = !!el.querySelector(".amber");
      el.classList.toggle("is-red", red);
      el.classList.toggle("is-amber", !red && amber);
    },
  };
}
