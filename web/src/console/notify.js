// The alert notification (#154): a big card that slides in over the 3D field when a new
// alert arrives (a pump trips, in the demo or in an EOG dataset), with a short tone if the
// browser allows it, the tab title and favicon flashing until someone acts on it, and one
// plain sentence to the judge's phone (window.gaiaPhone, #160; a no-op without a provider).
//
//   const n = makeNotifier(H)
//   n.consider(alert, event, becameP1)   the console's alert source (index.js onAlert: /ws in the
//                                        demo, the /api/alerts poll in dataset mode) calls this
//   n.incident(inc)                      /api/state's incident, every second: a new one notifies
//                                        too, through its alert when the console has it
//   n.show(alert)                        show one now (window.gaiaNotify uses it)
//   n.top()                              the most urgent active root alert (for the judge tour)
//
// Every number comes from what the console already has: the shutdown clock from the same brief
// the status line reads (H.brief), else the alert's own consequence; the fix and its cost from
// the brief's plans, once they have been re-read after the alert. Nothing is invented: no plan
// says so in words. The same alert notifies once (by id), whichever path asked first.

import * as liveFeed from "../live.js";
import { esc } from "./stream.js";
import { toMin, clock12, minsUntil, usd, prioOf, levelOf, watchText } from "./agents.js";

const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const PLAN_WAIT_MS = 20000;  // a new alert: say "checking the plans" until they are re-read, at most this long
const PHONE_WAIT_MS = 12000; // the phone sentence waits this long at most for the re-read plans
const rank = (a) => (levelOf(a) === "red" ? 0 : 10) + Number(String(prioOf(a)).slice(1) || 3);
// The console's clock: "2:48 AM" on the demo field, "14:05 UTC" on an EOG dataset (its own time).
const clockOf = (min) => {
  if (min == null || !isFinite(min)) return "--:--";
  if (!liveFeed.live.dataset) return clock12(min);
  const x = ((Math.round(min) % 1440) + 1440) % 1440;
  return `${String(Math.floor(x / 60)).padStart(2, "0")}:${String(x % 60).padStart(2, "0")} UTC`;
};

export function makeNotifier(H) {
  const seen = new Set();
  let cur = null;        // { a, at, fresh } on screen
  let lastInc = undefined;
  let flashT = null, flashOn = false, title0 = document.title, icon0 = null;
  let audio = null;
  const phoned = new Map(); // alert id -> { at, sent }

  const card = document.createElement("section");
  card.id = "gaiaNotify";
  card.className = "gn-card";
  card.hidden = true;
  card.setAttribute("role", "alertdialog");
  card.setAttribute("aria-live", "assertive");
  card.setAttribute("aria-labelledby", "gnHead");
  document.body.appendChild(card);

  // Browsers only let a page make sound after a person has touched it. Unlock on the first touch.
  const unlock = () => {
    try {
      if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state === "suspended") audio.resume().catch(() => {});
    } catch { audio = null; }
  };
  addEventListener("pointerdown", unlock, { once: true, capture: true });
  addEventListener("keydown", unlock, { once: true, capture: true });

  function tone(red) {
    try {
      if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
      if (audio.state !== "running") return false;
      const t = audio.currentTime;
      for (const [i, f] of (red ? [880, 660] : [660]).entries()) {
        const o = audio.createOscillator(), g = audio.createGain();
        o.type = "sine";
        o.frequency.value = f;
        g.gain.setValueAtTime(0, t + i * 0.22);
        g.gain.linearRampToValueAtTime(0.16, t + i * 0.22 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.22 + 0.2);
        o.connect(g).connect(audio.destination);
        o.start(t + i * 0.22);
        o.stop(t + i * 0.22 + 0.22);
      }
      return true;
    } catch { return false; }
  }

  // ------------------------------------------------------------- title ----
  const DOT = (c) => `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="13" fill="${c}"/></svg>`)}`;
  function icons() { return [...document.querySelectorAll('link[rel~="icon"]')]; }
  function setIcon(href) {
    const list = icons();
    if (!icon0) icon0 = list.map((l) => [l, l.getAttribute("href"), l.getAttribute("type")]);
    for (const l of list) { l.setAttribute("href", href); l.setAttribute("type", "image/svg+xml"); }
  }
  function startFlash(text, red) {
    stopFlash();
    title0 = document.title;
    const alertTitle = `(!) ${text}`;
    const color = red ? "#ff5a3c" : "#e0b252";
    const frame = () => {
      flashOn = !flashOn;
      document.title = flashOn ? alertTitle : title0;
      setIcon(flashOn ? DOT(color) : (icon0 && icon0[0] && icon0[0][1]) || DOT("#777"));
    };
    if (reduced()) { document.title = alertTitle; setIcon(DOT(color)); flashT = -1; return; }
    frame();
    flashT = setInterval(frame, 900);
  }
  function stopFlash() {
    if (flashT == null) return;
    if (flashT !== -1) clearInterval(flashT);
    flashT = null;
    flashOn = false;
    document.title = title0;
    if (icon0) for (const [l, href, type] of icon0) { l.setAttribute("href", href); if (type) l.setAttribute("type", type); else l.removeAttribute("type"); }
    icon0 = null;
  }

  // ----------------------------------------------------------- content ----
  // IDs inside the backend's sentences read as names: "S4_PUMP_C failed" -> "S4 Pump C failed".
  const pretty = (t) => String(t || "").replace(/\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b/g, (id) => H.nice(id));
  const headOf = (a) => pretty(String(a.message || a.kind || "new alert").split(/[:.]/)[0].trim());
  // The rest of the alert's sentence, unless it only says the shutdown again (the next line does).
  function whatFailed(a, sd) {
    const w = pretty(watchText(a, "raised", H.nice));
    const head = headOf(a);
    const rest = (w.startsWith(head) ? w.slice(head.length).replace(/^[.:]\s*/, "") : w).trim();
    return sd && /shutdown/i.test(rest) ? "" : rest;
  }
  const inMin = (m) => { m = Math.round(m); return m < 90 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ` ${m % 60} min` : ""}`; };

  // The shutdown this alert leads to: the brief's countdown when it is about the same tank
  // (one clock everywhere), else the alert's own consequence.
  function shutdownOf(a) {
    const b = H.brief();
    const cd = b && b.countdown;
    const tank = a.consequence_asset_id || null;
    if (cd && cd.at != null && (!tank || cd.asset_id === tank)) {
      return { tank: cd.asset_id, at: cd.at, low: cd.low, high: cd.high, wells: cd.wells,
        src: b.source === "brief" ? "the live brief" : b.from === "alerts" ? "the live alert forecast" : "the shift forecast" };
    }
    if (tank && a.consequence_clock) {
      const at = toMin(a.consequence_clock);
      const spread = a.minutes_to_consequence != null && a.minutes_to_consequence_low != null ? a.minutes_to_consequence - a.minutes_to_consequence_low : null;
      const w = Array.isArray(a.wells_lost) ? a.wells_lost.length : Number((/(\d+) wells?/.exec(a.consequence || "") || [])[1] || 0);
      return { tank, at, low: spread != null ? at - spread : null, high: spread != null ? at + spread : null, wells: w, src: "the alert's own forecast" };
    }
    return null;
  }

  // "between 2:40 am and 2:56 am, ", or one time when both ends print the same (EOG data often
  // has p10 = p90), and nothing when that one time is the shutdown time itself.
  function band(sd) {
    if (sd.low == null || sd.high == null) return "";
    const lo = clockOf(sd.low), hi = clockOf(sd.high);
    if (lo !== hi) return `between ${esc(lo)} and ${esc(hi)}, `;
    return lo === clockOf(sd.at) ? "" : `at ${esc(lo)}, `;
  }

  // The plan to show: the brief's cheapest robust plan (the Planner's, the status line's),
  // only once the plans have been re-read since a new alert (else it could be the last incident's).
  function planFor() {
    if (cur && !cur.fresh && H.plansAt() > cur.at) cur.fresh = true;
    const waiting = cur && !cur.fresh && Date.now() - cur.at < PLAN_WAIT_MS;
    const b = H.brief() || { plans: {} };
    const plan = waiting ? null : (b.plans && (b.plans.robust || b.plans.cheapest)) || null;
    return { plan, waiting, wait: b.do_nothing_usd, src: b.source === "brief" ? "the live brief" : "the shift plans" };
  }
  const firstOf = (s) => (String(s || "").split(/(?<=\.)\s/)[0] || "").replace(/\.$/, "");

  function render() {
    if (!cur) return;
    const a = cur.a;
    const red = levelOf(a) === "red";
    const sd = shutdownOf(a);
    const left = sd ? minsUntil(sd.at, H.nowMin()) : null;
    const { plan, waiting, wait, src } = planFor();
    const unknown = (H.unknown && H.unknown()) || [];
    const sdHtml = sd
      ? `<p class="gn-sd"><b>${esc(H.nice(sd.tank))} reaches shutdown at ${esc(clockOf(sd.at))}</b>${left == null ? "" : left <= 0 ? ", now" : `, in <b>${esc(inMin(left))}</b>`}.${sd.wells ? ` ${sd.wells} well${sd.wells === 1 ? "" : "s"} go${sd.wells === 1 ? "es" : ""} down with it.` : ""}</p>
        <p class="gn-src">${band(sd)}from ${esc(sd.src)}${a.clock ? `, alert raised ${esc(clockOf(toMin(a.clock)))}` : ""}</p>`
      : `<p class="gn-sd">${esc(pretty(a.consequence) || (unknown.length ? `no shutdown time: no fresh reading from ${unknown.map(H.nice).join(", ")}. treat it as unknown, not safe.` : "no shutdown forecast for this alert."))}</p>`;
    let fixHtml;
    if (plan && plan.total_usd != null) {
      fixHtml = `<p class="gn-fix"><span class="gn-k">cheapest fix</span>${esc(pretty(firstOf(plan.summary)) || "the plan")}: <b class="gn-usd">${esc(usd(plan.total_usd))}</b>${wait != null ? ` vs ${esc(usd(wait))} if nobody acts` : ""}.${plan.expires_clock ? ` start by ${esc(plan.expires_clock)}.` : ""}</p>
        <p class="gn-src">from ${esc(src)}. nothing is sent until a person holds to confirm.</p>`;
    } else if (waiting) {
      fixHtml = `<p class="gn-fix muted"><span class="gn-k">cheapest fix</span>checking the plans for this incident…</p>`;
    } else {
      fixHtml = `<p class="gn-fix muted"><span class="gn-k">cheapest fix</span>no plan keeps every well running yet. ask gaia for options.</p>`;
    }
    const html = `
      <div class="gn-top"><span class="cs-dot ${red ? "red" : "amber"}"></span><span>${red ? "alarm" : "warning"} · ${esc(prioOf(a))}${a.clock ? ` · ${esc(clockOf(toMin(a.clock)))}` : ""}</span>
        <span class="gn-grow"></span><span class="gn-id">${esc(a.asset_id || "")}</span></div>
      <h2 id="gnHead" class="gn-head">${esc(headOf(a))}</h2>
      ${whatFailed(a, sd) ? `<p class="gn-what">${esc(whatFailed(a, sd))}</p>` : ""}
      ${sdHtml}
      ${fixHtml}
      <div class="gn-row"><button type="button" class="gn-show" data-gn="show">show me</button><button type="button" class="gn-dismiss" data-gn="dismiss">dismiss</button></div>`;
    if (card._html !== html) {
      card._html = html;
      card.innerHTML = html;
      card.querySelector("[data-gn=show]").onclick = showMe;
      card.querySelector("[data-gn=dismiss]").onclick = () => dismiss("dismissed");
    }
    card.classList.toggle("red", red);
    card.classList.toggle("amber", !red);
    place();
  }

  // The phone gets one plain sentence: what failed, when the tank shuts down, the cheapest fix
  // and its cost. Same numbers as the card. window.gaiaPhone (#160) is a no-op without a provider.
  function phoneText(a) {
    const sd = shutdownOf(a);
    const left = sd ? minsUntil(sd.at, H.nowMin()) : null;
    const { plan, wait } = planFor();
    const head = headOf(a);
    const what = sd
      ? `${H.nice(sd.tank)} reaches shutdown at ${clockOf(sd.at)}${left != null && left > 0 ? `, in ${inMin(left)}` : left != null ? " (now)" : ""}${sd.wells ? `, taking ${sd.wells} well${sd.wells === 1 ? "" : "s"} down` : ""}`
      : pretty(a.consequence || "no shutdown is forecast").replace(/\.$/, "");
    const how = pretty(firstOf(plan && plan.summary)).replace(/^./, (c) => c.toLowerCase());
    const fix = plan && plan.total_usd != null
      ? `the cheapest fix costs ${usd(plan.total_usd)}${wait != null ? ` vs ${usd(wait)} if nobody acts` : ""}${how ? `: ${how}` : ""}`
      : "no plan keeps every well running yet, so ask gaia for options";
    return `gaia: ${head}${a.clock ? ` at ${clockOf(toMin(a.clock))}` : ""}; ${what}; ${fix}.`;
  }
  // Only when a judge left a number on /start and is signed up (no POST otherwise). One send
  // per alert id here; the backend also keeps track.
  const phoneOn = () => !!(window.gaiaPhone && window.gaiaPhone.send && (!window.gaiaPhone.subscription || window.gaiaPhone.subscription()));
  function phoneTick() {
    for (const [id, p] of phoned) {
      if (p.sent) continue;
      if (!phoneOn()) { if (Date.now() - p.at > PHONE_WAIT_MS) p.sent = "off"; continue; }
      const ready = (cur && cur.a.id === id ? cur.fresh : H.plansAt() > p.at) || Date.now() - p.at > PHONE_WAIT_MS;
      if (!ready) continue;
      p.sent = true;
      p.text = phoneText(cur && cur.a.id === id ? cur.a : p.a);
      try {
        const r = window.gaiaPhone && window.gaiaPhone.send ? window.gaiaPhone.send(id, p.text) : null;
        if (r && r.catch) r.catch(() => {});
      } catch { /* the phone is a convenience; the card is the alert */ }
    }
  }

  // Over the 3D field, under the status line and the monitoring strip, never down on the time bar
  // or over the chat. With the field pane closed, over the middle of the window.
  function place() {
    if (card.hidden) return;
    const stage = H.stage.getBoundingClientRect();
    const onScreen = stage.width > 200 && stage.left > -1000 && stage.left < innerWidth;
    let left, top, width, maxH;
    if (onScreen) {
      // A short canvas (a phone): the card takes the top of the field, over the status line and strip.
      const short = stage.height < 560;
      const tops = short ? [] : [...H.stage.querySelectorAll("#csStatus, .gm-strip, #twonights")]
        .filter((e) => !e.hidden && e.offsetParent !== null).map((e) => e.getBoundingClientRect().bottom);
      top = short ? stage.top + 8 : Math.max(stage.top + 12, ...tops) + 12;
      width = Math.min(760, stage.width - (short ? 16 : 24));
      left = stage.left + (stage.width - width) / 2;
      const tb = H.stage.querySelector(".timebar");
      const need = Math.min(card.scrollHeight || 420, 560);
      const above = tb ? stage.top + tb.offsetTop - 10 - top : stage.bottom - 12 - top;
      // No room above the time bar for the card: the time bar steps down while the alert is up,
      // so the two never overlap.
      const hideTb = !!tb && above < need;
      document.body.classList.toggle("gn-tb-hide", hideTb);
      maxH = hideTb ? stage.bottom - 12 - top : above;
    } else {
      width = Math.min(760, innerWidth - 24);
      left = (innerWidth - width) / 2;
      top = 64;
      maxH = innerHeight - 96;
    }
    Object.assign(card.style, { left: `${Math.round(left)}px`, top: `${Math.round(top)}px`, width: `${Math.round(width)}px`, maxHeight: `${Math.round(maxH)}px` });
  }
  addEventListener("resize", () => requestAnimationFrame(place));

  // ------------------------------------------------------------ actions ----
  function show(a, opts = {}) {
    if (!a || !a.id) return { shown: false, reason: "no alert" };
    if (cur && cur.a.id === a.id && !card.hidden) { cur.a = { ...cur.a, ...a }; render(); return { shown: true, id: a.id, again: true }; }
    const isNew = !seen.has(a.id);
    seen.add(a.id);
    // A new alert re-reads the plans (index.js poll), and the phone hears about it once.
    if (isNew && H.onNew) H.onNew(a);
    if (isNew) phoned.set(a.id, { at: Date.now(), a, sent: false });
    // The plans on hand belong to this alert only if they were read after the console first had it.
    const fresh = opts.fresh != null ? !!opts.fresh : H.plansAt() > (H.seenAt(a.id) || Infinity);
    cur = { a, at: Date.now(), fresh, via: opts.via || "alert" };
    card.hidden = false;
    card.classList.remove("in");
    card._html = "";
    render();
    void card.offsetWidth; // restart the slide-in
    card.classList.add("in");
    // Big and unmissable (Sriyan): a red alarm also lights the whole screen edge.
    document.body.classList.toggle("gn-alarm", card.classList.contains("red"));
    const sound = opts.sound === false ? false : tone(levelOf(a) === "red");
    startFlash(headOf(a).toLowerCase(), levelOf(a) === "red");
    if (H.log) H.log("alert", `Notified: ${a.message || a.id}`);
    phoneTick();
    return { shown: true, id: a.id, sound, phone: isNew };
  }
  function dismiss(why = "dismissed") {
    if (!cur) return;
    stopFlash();
    card.classList.remove("in");
    card.hidden = true;
    document.body.classList.remove("gn-tb-hide", "gn-alarm");
    card._html = "";
    const a = cur.a;
    cur = null;
    if (H.onDismiss) H.onDismiss(a, why);
  }
  function showMe() {
    if (!cur) return;
    const a = cur.a;
    dismiss("show");
    H.showMe(a);
  }

  const isRoot = (a) => !a.root_id || a.root_id === a.id;
  // The console's alert source: a new root alert (an alarm or a warning), active and not shelved.
  // Once per id. While a more urgent alert's card is up, a less urgent one stays in the chat.
  function consider(a, event, becameP1) {
    if (!a || !a.id || seen.has(a.id)) return false;
    if (!isRoot(a) || a.active === false || a.shelved_until || levelOf(a) === "gray") return false;
    if (!(event === "raised" || becameP1)) return false;
    if (cur && !card.hidden && rank(a) > rank(cur.a)) { seen.add(a.id); return false; }
    return show(a).shown;
  }

  // /api/state's incident (the demo's, or the dataset's top alert). The first one seen is
  // what was there at load; a new one after that notifies, through its alert when the console
  // has one (same id, same dedupe), else from the incident's own words.
  function incident(inc) {
    const key = inc ? String(inc.event_id || `${inc.root_asset_id || ""}|${inc.summary || ""}`) : null;
    if (lastInc === undefined) { lastInc = key; return; }
    if (key === lastInc) return;
    lastInc = key;
    if (!inc || !key) return;
    const list = (H.alerts() || []).filter((x) => x.active !== false && isRoot(x));
    const a = list.find((x) => x.id === inc.event_id) || list.find((x) => x.asset_id === inc.root_asset_id);
    if (a) { consider(a, "raised"); return; }
    if (seen.has(key)) return;
    const pump = /pump/i.test(String(inc.category || inc.root_asset_id || ""));
    consider({ id: key, asset_id: inc.root_asset_id || "", message: inc.summary || "New incident.", severity: inc.severity || (pump || !inc.category ? "ALARM" : "WARNING"), active: true }, "raised");
  }

  // The alert a tour would show: the most urgent active root (P1 first, then the soonest shutdown).
  function top() {
    const list = (H.alerts() || []).filter((a) => a.active !== false && !a.shelved_until && isRoot(a));
    list.sort((x, y) => (rank(x) === rank(y) ? (x.consequence_t ?? 1e9) - (y.consequence_t ?? 1e9) : rank(x) - rank(y)));
    return list[0] || null;
  }

  // Once a second: keep the minutes and the plan current, drop it if the alert cleared, phone.
  function tick() {
    phoneTick();
    if (!cur) return;
    const live = (H.alerts() || []).find((x) => x.id === cur.a.id);
    if (live) cur.a = { ...cur.a, ...live };
    if (cur.a.active === false) { dismiss("resolved"); return; }
    render();
  }
  addEventListener("keydown", (e) => { if (e.key === "Escape" && cur) dismiss("dismissed"); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && cur) place(); });

  return {
    consider, incident, show, dismiss, top, tick, place,
    state: () => ({ shown: !!cur && !card.hidden, id: cur ? cur.a.id : null, seen: [...seen], flashing: flashT != null,
      phone: [...phoned].map(([id, p]) => ({ id, sent: p.sent, text: p.text || null })) }),
  };
}
