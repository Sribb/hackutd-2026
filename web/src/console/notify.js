// The alert notification (#154): a big card that slides in over the 3D field when a new
// root alert arrives (a pump trips), with a short tone if the browser allows it and the tab
// title and favicon flashing until someone acts on it.
//
//   const n = makeNotifier(H)
//   n.consider(alert, event, becameP1)   the console's alert source (index.js onAlert) calls this
//   n.show(alert)                        show one now (window.gaiaNotify uses it)
//   n.top()                              the alert a tour would show: the most urgent active root
//
// Every number on the card comes from what the console already has: the shutdown clock from
// the same brief the status line reads (H.brief), else the alert's own consequence; the fix
// and its cost from the brief's plans. Nothing is invented: no plan says so in words.
// The same alert notifies once (by id), whichever path asked first.

import { esc } from "./stream.js";
import { toMin, clock12, minsUntil, usd, prioOf, levelOf, watchText } from "./agents.js";

const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
const PLAN_WAIT_MS = 20000; // plans re-read after an alert; say "checking" for this long first

export function makeNotifier(H) {
  const seen = new Set();
  let cur = null;        // { a, at } on screen
  let flashT = null, flashOn = false, title0 = document.title, icon0 = null;
  let audio = null;

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
  const headOf = (a) => String(a.message || a.kind || "new alert").split(/[:.]/)[0].trim();
  function whatFailed(a) {
    const w = watchText(a, "raised", H.nice);
    const head = headOf(a);
    const rest = w.startsWith(head) ? w.slice(head.length).replace(/^[.:]\s*/, "") : w;
    return rest.trim();
  }

  // The shutdown this alert leads to: the brief's countdown when it is about the same tank
  // (one clock everywhere), else the alert's own consequence.
  function shutdownOf(a) {
    const b = H.brief();
    const cd = b && b.countdown;
    const tank = a.consequence_asset_id || null;
    if (cd && (!tank || cd.asset_id === tank)) {
      return { tank: cd.asset_id, at: cd.at, low: cd.low, high: cd.high, wells: cd.wells,
        src: b.source === "brief" ? "the live brief" : b.from === "alerts" ? "the live alert forecast" : "the shift forecast" };
    }
    if (tank && a.consequence_clock) {
      const at = toMin(a.consequence_clock);
      const spread = a.minutes_to_consequence != null && a.minutes_to_consequence_low != null ? a.minutes_to_consequence - a.minutes_to_consequence_low : null;
      const w = /(\d+) wells?/.exec(a.consequence || "");
      return { tank, at, low: spread != null ? at - spread : null, high: spread != null ? at + spread : null, wells: w ? Number(w[1]) : 0, src: "the alert's own forecast" };
    }
    return null;
  }

  function render() {
    if (!cur) return;
    const a = cur.a;
    const red = levelOf(a) === "red";
    const b = H.brief() || { plans: {} };
    const sd = shutdownOf(a);
    const now = H.nowMin();
    const left = sd ? minsUntil(sd.at, now) : null;
    const plan = (b.plans && (b.plans.robust || b.plans.cheapest)) || null;
    const wait = b.do_nothing_usd;
    const unknown = (H.unknown && H.unknown()) || [];
    const sdHtml = sd
      ? `<p class="gn-sd"><b>${esc(H.nice(sd.tank))} reaches shutdown at ${esc(clock12(sd.at))}</b>${left == null ? "" : left <= 0 ? ", now" : `, in <b>${Math.round(left)} min</b>`}.${sd.wells ? ` ${sd.wells} well${sd.wells === 1 ? "" : "s"} go${sd.wells === 1 ? "es" : ""} down with it.` : ""}</p>
        <p class="gn-src">${sd.low != null && sd.high != null ? `between ${esc(clock12(sd.low))} and ${esc(clock12(sd.high))}, ` : ""}from ${esc(sd.src)}${a.clock ? `, alert raised ${esc(clock12(toMin(a.clock)))}` : ""}</p>`
      : `<p class="gn-sd">${esc(a.consequence || (unknown.length ? `no shutdown time: no fresh reading from ${unknown.map(H.nice).join(", ")}. treat it as unknown, not safe.` : "no shutdown forecast for this alert."))}</p>`;
    let fixHtml;
    if (plan && plan.total_usd != null) {
      const first = (String(plan.summary || "").split(/(?<=\.)\s/)[0] || "").replace(/\.$/, "");
      fixHtml = `<p class="gn-fix"><span class="gn-k">cheapest fix</span>${esc(first || "the plan")}: <b class="gn-usd">${esc(usd(plan.total_usd))}</b>${wait != null ? ` vs ${esc(usd(wait))} if nobody acts` : ""}.${plan.expires_clock ? ` start by ${esc(plan.expires_clock)}.` : ""}</p>
        <p class="gn-src">from ${b.source === "brief" ? "the live brief" : "the shift plans"}. nothing is sent until a person holds to confirm.</p>`;
    } else if (Date.now() - cur.at < PLAN_WAIT_MS) {
      fixHtml = `<p class="gn-fix muted"><span class="gn-k">cheapest fix</span>checking the plans for this incident…</p>`;
    } else {
      fixHtml = `<p class="gn-fix muted"><span class="gn-k">cheapest fix</span>no plan keeps every well running yet. ask gaia for options.</p>`;
    }
    const html = `
      <div class="gn-top"><span class="cs-dot ${red ? "red" : "amber"}"></span><span>${red ? "alarm" : "warning"} · ${esc(prioOf(a))}${a.clock ? ` · ${esc(clock12(toMin(a.clock)))}` : ""}</span>
        <span class="gn-grow"></span><span class="gn-id">${esc(a.asset_id || "")}</span></div>
      <h2 id="gnHead" class="gn-head">${esc(headOf(a))}</h2>
      ${whatFailed(a) ? `<p class="gn-what">${esc(whatFailed(a))}</p>` : ""}
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
      width = Math.min(460, stage.width - (short ? 16 : 24));
      left = stage.left + (stage.width - width) / 2;
      const tb = H.stage.querySelector(".timebar");
      const need = Math.min(card.scrollHeight || 320, 380);
      const above = tb ? stage.top + tb.offsetTop - 10 - top : stage.bottom - 12 - top;
      // No room above the time bar for the card: the time bar steps down while the alert is up,
      // so the two never overlap.
      const hideTb = !!tb && above < need;
      document.body.classList.toggle("gn-tb-hide", hideTb);
      maxH = hideTb ? stage.bottom - 12 - top : above;
    } else {
      width = Math.min(460, innerWidth - 24);
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
    seen.add(a.id);
    cur = { a, at: Date.now(), via: opts.via || "alert" };
    card.hidden = false;
    card.classList.remove("in");
    card._html = "";
    render();
    void card.offsetWidth; // restart the slide-in
    card.classList.add("in");
    const sound = opts.sound === false ? false : tone(levelOf(a) === "red");
    startFlash(headOf(a).toLowerCase(), levelOf(a) === "red");
    if (H.log) H.log("alert", `Notified: ${a.message || a.id}`);
    phoneLater(a.id);
    return { shown: true, id: a.id, sound };
  }

  // The judge's phone (#163): the same words as the card, as one text and one voice call,
  // once per alert id (the backend keeps track). Waits for the plan line so the text carries
  // the cheapest fix and its cost. A no-op without a provider or a signed-up number.
  function phoneLater(id) {
    if (!window.gaiaPhone || !window.gaiaPhone.subscription()) return;
    setTimeout(() => {
      if (!cur || cur.a.id !== id) return;
      const t = (sel) => (card.querySelector(sel)?.textContent || "").replace(/\s+/g, " ").trim();
      const fix = t(".gn-fix").replace(/^cheapest fix/, "cheapest fix:").replace(/checking the plans.*$/, "");
      const text = [t(".gn-head"), t(".gn-sd"), fix, "hold to confirm in the console."].filter(Boolean).join(" ");
      window.gaiaPhone.send(id, text);
    }, 2500);
  }
  function dismiss(why = "dismissed") {
    if (!cur) return;
    stopFlash();
    card.classList.remove("in");
    card.hidden = true;
    document.body.classList.remove("gn-tb-hide");
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

  // The console's alert source: a new root alert, P1 or P2, active and not shelved. Once per id.
  function consider(a, event, becameP1) {
    if (!a || !a.id || seen.has(a.id)) return false;
    const root = !a.root_id || a.root_id === a.id;
    const p = prioOf(a);
    if (!root || a.active === false || a.shelved_until) return false;
    if (!((event === "raised" && (p === "P1" || p === "P2")) || becameP1)) return false;
    return show(a).shown;
  }

  // The alert a tour would show: the most urgent active root (P1 first, then the soonest shutdown).
  function top() {
    const list = (H.alerts() || []).filter((a) => a.active !== false && !a.shelved_until && (!a.root_id || a.root_id === a.id));
    list.sort((x, y) => (prioOf(x) === prioOf(y) ? (x.consequence_t ?? 1e9) - (y.consequence_t ?? 1e9) : prioOf(x) < prioOf(y) ? -1 : 1));
    return list[0] || null;
  }

  // Once a second: keep the minutes and the plan current, and drop it if the alert cleared.
  function tick() {
    if (!cur) return;
    const live = (H.alerts() || []).find((x) => x.id === cur.a.id);
    if (live) cur.a = { ...cur.a, ...live };
    if (cur.a.active === false) { dismiss("resolved"); return; }
    render();
  }
  addEventListener("keydown", (e) => { if (e.key === "Escape" && cur) dismiss("dismissed"); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && cur) place(); });

  return {
    consider, show, dismiss, top, tick, place,
    state: () => ({ shown: !!cur && !card.hidden, id: cur ? cur.a.id : null, seen: [...seen], flashing: flashT != null }),
  };
}
