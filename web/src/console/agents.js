// Watch, Forecast and Planner (spec §2): plain templates over deterministic data. No LLM.
//
// The brief (spec §5 `ws brief`, also GET /api/jarvis/brief) is the ONE source for the shutdown
// clock and the plan dollars. The status line, the Forecast bubble, the Planner bubble and the
// timebar all read the same normalized brief, so they can never disagree (the #118 screenshot
// had the countdown say "about 2:50 AM" from tank math while Jarvis said 2:47 AM).
//
// Until the backend sends `brief`, fallbackBrief() builds the same shape, clearly marked
// source: "fallback":
//   countdown  with the live layer up: the earliest CF-7 alert consequence (the backend's live
//              forecast, the same forecast Jarvis's "time left" reads). Without a live layer:
//              the do-nothing /api/forecast, polled every 30 s.
//   plans      /api/plans, polled every 30 s (the shift plan, not re-planned live).
//   do nothing /api/forecast baseline cost.total_usd.

import { esc } from "./stream.js";

export const usd = (n) => (n == null || !isFinite(n) ? "—" : "$" + Math.round(n).toLocaleString("en-US"));

// "2:47 AM", "02:47", "2:47" -> minutes of the day.
export function toMin(clock) {
  const m = /^\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM)?/i.exec(String(clock || ""));
  if (!m) return null;
  let h = Number(m[1]);
  if (m[3]) { h %= 12; if (/pm/i.test(m[3])) h += 12; }
  return h * 60 + Number(m[2]);
}
export function clock12(min) {
  if (min == null || !isFinite(min)) return "--:--";
  const x = ((Math.round(min) % 1440) + 1440) % 1440, hh = Math.floor(x / 60), mm = x % 60;
  return `${hh % 12 || 12}:${String(mm).padStart(2, "0")} ${hh < 12 ? "AM" : "PM"}`;
}
// Minutes from `now` to `at` on a 24 h clock, in -720..720.
export function minsUntil(at, now) {
  if (at == null || now == null) return null;
  let d = at - now;
  if (d > 720) d -= 1440;
  if (d < -720) d += 1440;
  return d;
}
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
export const andList = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

// ------------------------------------------------------------- brief ----
function planOf(p) {
  if (!p) return null;
  const total = p.total_usd ?? (p.cost && p.cost.total_usd);
  return { id: p.id || null, summary: p.summary || p.text || "", total_usd: total == null ? null : Number(total),
    robust: p.robust !== false, keeps_running: p.keeps_running !== false, actions: p.actions || [],
    expires_clock: p.expires_clock || null, cannot_save: p.cannot_save || [] };
}

// Track A's brief, read loosely so small naming differences don't blank the console.
export function normalizeBrief(raw) {
  if (!raw || typeof raw !== "object") return null;
  const b = raw.brief || raw;
  const c = b.countdown || null;
  let countdown = null;
  if (c && (c.asset_id || c.asset)) {
    const at = toMin(c.clock || c.at_clock || c.shutdown_clock);
    countdown = {
      asset_id: String(c.asset_id || c.asset).toUpperCase(),
      at,
      low: toMin(c.low_clock || c.clock_low || c.low) ?? null,
      high: toMin(c.high_clock || c.clock_high || c.high) ?? null,
      wells: Array.isArray(c.wells_lost) ? c.wells_lost.length : Number(c.wells_lost) || 0,
      text: c.text || "",
    };
    if (countdown.at == null && c.minutes != null) countdown.minutesFromBrief = Number(c.minutes);
  }
  const plans = b.plans || {};
  return {
    source: "brief",
    countdown,
    unknown_tanks: (b.unknown_tanks || (c && c.unknown_tanks) || []).map((x) => String(x).toUpperCase()),
    plans: { cheapest: planOf(plans.cheapest), robust: planOf(plans.robust) },
    do_nothing_usd: b.do_nothing_usd ?? null,
    last_call: b.last_call || null,
    now_clock: b.clock || b.now || null,
  };
}

export function fallbackBrief({ alerts, forecast, plans, startClock, liveUp, unknown }) {
  let countdown = null, from = "forecast";
  if (liveUp) {
    from = "alerts";
    const withC = alerts.filter((a) => a.active !== false && a.consequence_clock && a.consequence_asset_id);
    withC.sort((a, b) => (a.consequence_t ?? 1e9) - (b.consequence_t ?? 1e9));
    const a = withC[0];
    if (a) {
      const at = toMin(a.consequence_clock);
      const spread = a.minutes_to_consequence != null && a.minutes_to_consequence_low != null ? a.minutes_to_consequence - a.minutes_to_consequence_low : null;
      const w = /(\d+) wells?/.exec(a.consequence || "");
      countdown = { asset_id: a.consequence_asset_id, at, low: spread != null && at != null ? at - spread : null,
        high: spread != null && at != null ? at + spread : null, wells: w ? Number(w[1]) : 0, text: a.consequence || "" };
    }
  } else if (forecast) {
    const ev = (forecast.events || []).find((e) => e.type === "SHUTDOWN_LEVEL");
    const start = toMin(startClock);
    if (ev && start != null) {
      countdown = { asset_id: ev.asset_id, at: start + ev.t_min,
        low: ev.t_min_low != null ? start + ev.t_min_low : null, high: ev.t_min_high != null ? start + ev.t_min_high : null,
        wells: (ev.wells_lost || []).length, text: ev.text || "" };
    }
  }
  const list = ((plans && plans.plans) || []).map(planOf).filter(Boolean);
  const robust = list.find((p) => p.robust && p.keeps_running) || null;
  return {
    source: "fallback", from,
    countdown,
    unknown_tanks: unknown,
    plans: { cheapest: list[0] || null, robust },
    do_nothing_usd: forecast && forecast.cost ? forecast.cost.total_usd : null,
    last_call: null,
  };
}

// --------------------------------------------------------- visuals ----
const SCALE = 360; // the countdown bar is now -> 6 h
export function countdownBar(cd, nowMin, unknown, { mini = false } = {}) {
  const cls = `cs-cd${mini ? " mini" : ""}`;
  if (unknown.length && !cd) return `<span class="${cls} hatched unknown" aria-hidden="true"><i class="fill" style="width:100%"></i></span>`;
  if (!cd) return `<span class="${cls} none" aria-hidden="true"></span>`;
  const left = minsUntil(cd.at, nowMin);
  const pct = (m) => Math.max(0, Math.min(100, (m / SCALE) * 100));
  const fill = left == null ? 0 : pct(left);
  const lo = cd.low != null ? pct(minsUntil(cd.low, nowMin)) : null;
  const hi = cd.high != null ? pct(minsUntil(cd.high, nowMin)) : null;
  return `<span class="${cls}${unknown.length ? " hatched" : ""}${left != null && left <= 60 ? " soon" : ""}" aria-hidden="true">
    <i class="fill" style="width:${fill.toFixed(1)}%"></i>${lo != null && hi != null ? `<i class="band" style="left:${lo.toFixed(1)}%;width:${Math.max(1, hi - lo).toFixed(1)}%"></i>` : ""}</span>`;
}
export function planBars(fix, wait) {
  const max = Math.max(fix || 0, wait || 0, 1);
  const w = (x) => (x == null ? 0 : Math.max(2, (x / max) * 100));
  return `<div class="cs-pb"><span>fix</span><span class="bar"><i class="fix" style="width:${w(fix).toFixed(1)}%"></i></span><b data-v="fix">${usd(fix)}</b>
    <span>wait</span><span class="bar"><i class="wait" style="width:${w(wait).toFixed(1)}%"></i></span><b data-v="wait">${usd(wait)}</b></div>`;
}
export function dot(level, label) {
  return `<span class="cs-dot ${level}"></span>${label ? ` <span class="cs-vlabel">${esc(label)}</span>` : ""}`;
}

// -------------------------------------------------------- templates ----
export const prioOf = (a) => a.priority || (a.severity === "ALARM" ? "P1" : a.severity === "WARNING" ? "P2" : "P3");
export const levelOf = (a) => (prioOf(a) === "P1" || a.severity === "ALARM" ? "red" : prioOf(a) === "P2" || a.severity === "WARNING" ? "amber" : "gray");
const short = (s, n = 140) => { s = String(s || "").trim(); return s.length <= n ? s : s.slice(0, n - 1).replace(/\s+\S*$/, "") + "…"; };

export function watchText(a, event, nice) {
  const msg = a.message || a.kind || "";
  if (event === "resolved") return short(`${nice(a.asset_id)} cleared: ${msg.replace(/\.$/, "")}.`);
  if (event === "acked") return short(`Acknowledged: ${msg}`);
  if (event === "shelved") return short(`Shelved ${nice(a.asset_id)}${a.shelved_until ? ` until ${a.shelved_until}` : ""}${a.shelve_reason ? `: ${a.shelve_reason}` : ""}.`);
  if (event === "unshelved") return short(`Back from the shelf: ${msg}`);
  return short(msg.replace(/: it reports stopped\./, ".").replace(/ is no longer being emptied\./, " is filling."));
}

// The Forecast agent's words. Unknown tanks always come first; never an all-clear.
export function forecastText(cd, nowMin, unknown, feedLost, nice, fromAlerts) {
  const blind = unknown.length ? `Can't vouch for ${andList(unknown.map(nice))}: no fresh reading. ` : "";
  if (feedLost) return short(`Can't vouch for any tank: the live feed is lost. ${cd ? `Last forecast: ${nice(cd.asset_id)} shuts in at ${clock12(cd.at)}.` : ""}`);
  if (!cd) {
    return short(`${blind}${fromAlerts ? "No live alert leads to a shutdown in the next 6 hours." : `No tank${unknown.length ? " with a reading" : ""} reaches shutdown in the next 6 hours if nothing changes.`}`);
  }
  const band = cd.low != null && cd.high != null ? ` (${clock12(cd.low).replace(/ [AP]M$/, "")}–${clock12(cd.high)})` : "";
  const left = minsUntil(cd.at, nowMin);
  const when = left != null && left <= 0 ? `is at shutdown now (forecast ${clock12(cd.at)})` : `shuts in at ${clock12(cd.at)}${band} if nobody acts`;
  return short(`${blind}${nice(cd.asset_id)} ${when}.${cd.wells ? ` ${plural(cd.wells, "well")} go${cd.wells === 1 ? "es" : ""} down.` : ""}`, 180);
}

export function plannerText(plan, wait, cd, unknown) {
  if (!plan) {
    if (!cd && !unknown.length) return "No fix needed: nothing shuts down in the next 6 hours.";
    return unknown.length && !cd ? "No plan can vouch for a tank with no reading. Check it in person." : "No plan keeps every well running. Ask Jarvis for options.";
  }
  const start = plan.expires_clock ? ` Start by ${plan.expires_clock}.` : "";
  const first = (plan.summary.split(/(?<=\.)\s/)[0] || plan.summary).replace(/\.$/, "");
  return short(`${first}: ${usd(plan.total_usd)} vs ${usd(wait)} doing nothing.${start}`, 180);
}
