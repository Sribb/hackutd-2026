#!/usr/bin/env node
// Headless walk through the guided judge tour (#158) on a running demo (npm run demo).
// DOM reads only, no screenshots. Exits 1 on the first failed check.
//
//   TOUR_URL='<the console link npm run demo prints, with its #k= part>' node scripts/tour-check.mjs
//
// Options: --size=1440x900 (default) · --notify-stub (define window.gaiaNotify and check the
// tour calls it) · --layout-only (next through every step, checking placement only) ·
// --reduced (prefers-reduced-motion: step 2 must not fly on its own) ·
// --dataset (a backend with FREEBOARD_EOG_DIR: step 4 replays the recording, step 6 approves).
// Each step: the card renders, never touches the time bar (where it sits when up, and its
// on-screen box) or the composer, stays in the window, and the page never scrolls sideways.

import { chromium } from "playwright";

const url = process.env.TOUR_URL;
if (!url) { console.error("Set TOUR_URL to the console link from npm run demo."); process.exit(2); }
const arg = (k) => process.argv.find((a) => a.startsWith(`--${k}`));
const [W, H] = (arg("size") ? arg("size").split("=")[1] : "1440x900").split("x").map(Number);
const STUB = !!arg("notify-stub");
const LAYOUT = !!arg("layout-only");
const REDUCED = !!arg("reduced");
const DATASET = !!arg("dataset");
const u = new URL(url);
const open = `${u.origin}/?tour=1&fresh${u.hash}`;

const log = (...a) => console.log(`[${W}x${H}]`, ...a);
let failed = 0;
const check = (ok, what) => { log(ok ? "ok  " : "FAIL", what); if (!ok) failed++; };

const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage({ viewport: { width: W, height: H }, reducedMotion: REDUCED ? "reduce" : "no-preference" });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e.message || e)));
const posts = [];
const t0 = Date.now();
page.on("response", (r) => { if (r.request().method() === "POST" && /\/api\/(live\/faults|actions|eog\/clock|plans\/[^/]+\/approve)/.test(r.url())) posts.push({ path: new URL(r.url()).pathname, status: r.status(), ms: Date.now() - t0 }); });
if (STUB) await page.addInitScript(() => { window.gaiaNotify = (a) => { (window.__notifyCalls = window.__notifyCalls || []).push(a && a.id); }; });

const geo = () => page.evaluate(() => {
  const card = document.querySelector(".gt-card");
  if (!card) return null;
  const r = card.getBoundingClientRect();
  const st = document.querySelector("#stage").getBoundingClientRect();
  const tb = document.querySelector(".timebar");
  const lay = { left: st.left + tb.offsetLeft, top: st.top + tb.offsetTop, right: st.left + tb.offsetLeft + tb.offsetWidth, bottom: st.top + tb.offsetTop + tb.offsetHeight };
  const v = tb.getBoundingClientRect();
  const vis = { left: Math.max(v.left, st.left), top: Math.max(v.top, st.top), right: Math.min(v.right, st.right), bottom: Math.min(v.bottom, st.bottom) };
  const hit = (a, b) => !!b && a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  const box = (sel) => { const e = document.querySelector(sel); if (!e) return null; const b = e.getBoundingClientRect(); return b.width && b.height ? b : null; };
  return {
    step: window.__tour.step(), title: card.querySelector(".gt-title").textContent, body: card.querySelector(".gt-body").textContent.replace(/\s+/g, " ").trim(),
    extra: card.querySelector(".gt-extra").textContent.replace(/\s+/g, " ").trim(), pill: card.classList.contains("pill"),
    hitTimebar: hit(r, lay) || (vis.right > vis.left && vis.bottom > vis.top && hit(r, vis)), hitDock: hit(r, box(".cs-dock")), hitAsk: hit(r, box("#csAsk")), hitNotify: hit(r, box("#gaiaNotify:not([hidden])")), hitStrip: getComputedStyle(document.querySelector(".gm-strip") || document.body).display !== "none" && hit(r, box(".gm-strip")),
    inView: r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight, visible: getComputedStyle(card).opacity !== "0" || card.classList.contains("in"),
    overflowX: document.documentElement.scrollWidth > innerWidth, rings: [...document.querySelectorAll(".gt-ring:not([hidden])")].length,
    dashes: /[\u2013\u2014]/.test(card.textContent)
  };
});
async function placed(label) {
  await page.waitForTimeout(450);
  const g = await geo();
  check(!!g, `${label}: card is on screen`);
  if (!g) return g;
  check(!g.hitTimebar, `${label}: card clear of the time bar`);
  check(!g.hitDock && !g.hitAsk, `${label}: card clear of the composer`);
  check(!g.hitNotify, `${label}: card clear of the alert notification`);
  check(!g.hitStrip, `${label}: card clear of the monitoring strip`);
  check(g.inView, `${label}: card inside the window`);
  check(!g.overflowX, `${label}: no sideways scroll`);
  check(!g.dashes, `${label}: no em or en dashes`);
  return g;
}
const step = () => page.evaluate(() => window.__tour && window.__tour.step());
const waitStep = (id, ms = 20000) => page.waitForFunction((x) => window.__tour && window.__tour.step() === x, id, { timeout: ms });
const next = async () => { await page.click(".gt-card [data-next]"); };
const bodyHas = (re, ms = 20000) => page.waitForFunction((s) => { const b = document.querySelector(".gt-card"); return b && new RegExp(s, "i").test(b.textContent); }, re, { timeout: ms });

try {
  await page.goto(open, { waitUntil: "domcontentloaded", timeout: 90000 });
  await waitStep("field", 90000);
  check(!new URL(page.url()).searchParams.has("tour"), "?tour=1 is dropped from the address bar");
  let g = await placed("1 field");
  check(/night-shift operator/.test(g.title) && /3d/.test(g.body), "1 field: title and body");

  // This machine can be slow enough that the whole camera tour runs between two polls: record it.
  await page.evaluate(() => {
    const seen = window.__seen = { on: false, sel: false, bodies: [] };
    new MutationObserver(() => {
      const c = document.querySelector(".gt-card");
      if (!c || c.dataset.step !== "look") return;
      if (c.querySelector(".gt-btn.on")) seen.on = true;
      if (!document.querySelector("#csSel").hidden) seen.sel = true;
      const t = c.querySelector(".gt-body").textContent.trim();
      if (t && !seen.bodies.includes(t)) seen.bodies.push(t);
    }).observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true });
  });
  await next(); await waitStep("look");
  await placed("2 look (start)");
  if (REDUCED) {
    await page.waitForFunction(() => document.querySelectorAll(".gt-card [data-stop]").length > 0, null, { timeout: 20000 });
    await page.waitForTimeout(4000);
    check(await page.evaluate(() => !document.querySelector(".gt-card .gt-btn.on") && document.querySelector("#csSel").hidden), "2 look (reduced motion): no flight until a stop is picked");
    await page.click(".gt-card [data-stop='0']");
    await page.waitForFunction(() => !document.querySelector("#csSel").hidden, null, { timeout: 20000 });
    check(true, "2 look (reduced motion): picking a stop flies there");
  } else if (!LAYOUT) {
    await bodyHas("whole field", 40000);
    const seen = await page.evaluate(() => window.__seen);
    const stops = seen.bodies.filter((t) => /^[a-z0-9 ]+: /.test(t)).map((t) => t.split(":")[0]);
    check(seen.on && stops.length === 3 && /tank/.test(stops[0]) && /pump/.test(stops[1]), `2 look: flew to a tank, a pump and the disposal well (${stops.join(", ")})`);
    check(seen.sel, "2 look: the camera tour selected each asset on the way");
    check(await page.evaluate(() => document.querySelector("#csSel").hidden), "2 look: the camera tour finished and flew home (selection cleared)");
    await placed("2 look (done)");
  }

  await next(); await waitStep("gaia");
  g = await placed("3 gaia");
  await page.click(".gt-card [data-back]"); await waitStep("look");
  check(true, "back works (3 -> 2)");
  await next(); await waitStep("gaia");
  check(true, "next works (2 -> 3)");
  if (!LAYOUT) {
    const n0 = await page.locator('#csStream li[data-agent="J"]:not(.typing)').count();
    await page.click(".gt-card [data-ask='0']");
    await page.waitForFunction((n) => document.querySelectorAll('#csStream li[data-agent="J"]:not(.typing)').length > n, n0, { timeout: 20000 });
    const you = await page.locator("#csStream li.a-you").last().textContent();
    check(/what's failing/i.test(you), "3 gaia: the question went through the real composer");
    await bodyHas("gaia answered", 5000);
    await placed("3 gaia (answered)");
  }

  // Count how many times #154's notification card slides in (a double alert would be two).
  await page.evaluate(() => {
    window.__shows = 0;
    let was = true;
    new MutationObserver(() => { const c = document.querySelector("#gaiaNotify"); const on = !!c && !c.hidden; if (on && !was) window.__shows++; was = on; })
      .observe(document.body, { subtree: true, attributes: true, attributeFilter: ["hidden"], childList: true });
    const c = document.querySelector("#gaiaNotify"); was = !!c && !c.hidden;
  });
  await next(); await waitStep("alarm");
  if (DATASET && !LAYOUT) {
    await bodyHas("real week from eog", 20000);
    check(true, "4 alarm (dataset): says it replays a real week of eog data");
    await bodyHas("raised it", 90000);
    g = await placed("4 alarm (dataset)");
    const n = await page.evaluate(() => window.__tour.notified.slice());
    check(n.length >= 1, `4 alarm (dataset): the replayed failure's alarm came in by itself (${JSON.stringify(n)})`);
    const clk = posts.filter((x) => x.path === "/api/eog/clock");
    check(clk.length === 1 && clk[0].status === 200, `4 alarm (dataset): one POST /api/eog/clock, 200 (${JSON.stringify(clk)})`);
    check(!posts.some((x) => x.path === "/api/live/faults"), "4 alarm (dataset): nothing tripped live");
    const nt = await page.evaluate(() => ({ via: window.__tour.notified[0].via, at: window.__tour.notified[0].at, n: window.__console.notify ? window.__console.notify() : null, now: Date.now() }));
    const clockAt = clk[0] && clk[0].ms;
    const alarmAt = nt.at - (nt.now - (Date.now() - t0));
    log(`     clock POST at +${(clockAt / 1000).toFixed(1)} s, alarm on screen at +${(alarmAt / 1000).toFixed(1)} s (${((alarmAt - clockAt) / 1000).toFixed(1)} s after), shown by ${nt.via}; notifier ${JSON.stringify(nt.n && { shown: nt.n.shown, id: nt.n.id, seen: nt.n.seen })}`);
    check(alarmAt > clockAt, "4 alarm (dataset): the alarm arrived after the clock POST");
    await page.waitForTimeout(2000);
    const shows = await page.evaluate(() => window.__shows);
    check(shows === 1, `4 alarm (dataset): the big notification slid in once, not twice (${shows})`);
    log("     card:", g.body.slice(0, 200));
  } else if (!LAYOUT) {
    await bodyHas("tripped|raised it", 30000);
    g = await placed("4 alarm");
    const n = await page.evaluate(() => window.__tour.notified.slice());
    check(n.length >= 1, `4 alarm: an incident (${JSON.stringify(n)})`);
    if (STUB) check(await page.evaluate(() => (window.__notifyCalls || []).length === 1), "4 alarm: window.gaiaNotify(alert) was called once");
    else if (await page.evaluate(() => typeof window.gaiaNotify === "function")) check(["console", "gaiaNotify"].includes(n[0].via) && await page.evaluate(() => !!document.querySelector("#gaiaNotify")), `4 alarm: #154's notification showed the alert (via ${n[0].via})`);
    else check(await page.evaluate((id) => window.__console.thread() === id, n[0] && n[0].id), "4 alarm: no gaiaNotify, so the incident opened in the chat");
    log("     fault and action posts so far:", JSON.stringify(posts));
  } else await placed("4 alarm");

  await next(); await waitStep("clock");
  g = await placed("5 clock");
  // Headless swiftshader draws about 1 frame a second, so the time bar's slide takes a few seconds here.
  check(await page.waitForFunction(() => { const t = document.querySelector(".timebar").getBoundingClientRect(), s = document.querySelector("#stage").getBoundingClientRect(); return t.bottom <= s.bottom + 1; }, null, { timeout: 20000 }).then(() => true, () => false), "5 clock: the time bar is up");
  check(/status line counts down/.test(g.body), `5 clock: reads the status line (${g.body.slice(0, 90)})`);
  if (!LAYOUT) {
    const box = await page.locator("#time").boundingBox();
    await page.mouse.move(box.x + 4, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.4, box.y + box.height / 2, { steps: 8 });
    await page.mouse.up();
    await bodyHas("the field at .* in ", 15000);
    check(true, "5 clock: dragging the slider updates the card");
    await placed("5 clock (dragged)");
    // A reload resumes the tour where it was.
    await page.reload({ waitUntil: "domcontentloaded", timeout: 90000 });
    await waitStep("clock", 90000);
    check(true, "reload resumes at step 5");
    await placed("5 clock (after reload)");
  }

  await next(); await waitStep("fix");
  g = await placed("6 fix");
  check(/planner's best plan costs \$[\d,]+, against \$[\d,]+/.test(g.body) || /no fix/.test(g.body), `6 fix: plan dollars (${g.body.slice(0, 80)})`);
  if (DATASET && !LAYOUT) {
    check(/recording/.test(g.body), "6 fix (dataset): says approving records the plan, nothing goes to equipment");
    await page.click(".gt-card [data-plans-open]");
    await page.waitForFunction(() => { const b = document.querySelector("#plans"); const d = b && b.closest(".cs-drawer"); return d && !d.hidden && b.querySelector("[data-approve]"); }, null, { timeout: 20000 });
    await placed("6 fix (dataset, plans open)");
    const hold = await page.locator("#plans [data-approve]").first().boundingBox();
    await page.mouse.move(hold.x + hold.width / 2, hold.y + hold.height / 2);
    await page.mouse.down(); await page.waitForTimeout(1900); await page.mouse.up();
    await page.waitForFunction(() => document.querySelector("#plans .approved-stamp"), null, { timeout: 30000 });
    await bodyHas("recorded for the shift", 8000);
    check(true, "6 fix (dataset): hold to approve recorded the plan");
    check(posts.some((x) => /\/approve$/.test(x.path) && x.status === 200), `6 fix (dataset): POST /api/plans/{id}/approve 200`);
    await placed("6 fix (dataset, approved)");
  } else if (!LAYOUT) {
    const brief = await page.evaluate(() => { const b = window.__console.brief(); const p = b.plans.robust || b.plans.cheapest; return { total: p && p.total_usd, wait: b.do_nothing_usd }; });
    const usd = (x) => "$" + Math.round(x).toLocaleString("en-US");
    check(g.body.includes(usd(brief.total)) && g.body.includes(usd(brief.wait)), "6 fix: the dollars are the brief's (the status line's) numbers");
    if (await page.locator(".gt-card [data-propose]").count()) await page.click(".gt-card [data-propose]");
    await page.waitForFunction(() => { const c = window.__console.card(); return c && c.status === "PENDING"; }, null, { timeout: 20000 });
    await bodyHas("hold to confirm", 5000);
    await placed("6 fix (pending)");
    await page.waitForFunction(() => !document.querySelector("#csFix [data-loading]"), null, { timeout: 20000 });
    const hold = await page.locator("#csFix [data-hold]").boundingBox();
    await page.mouse.move(hold.x + hold.width / 2, hold.y + hold.height / 2);
    await page.mouse.down(); await page.waitForTimeout(1900); await page.mouse.up();
    await page.waitForFunction(() => window.__console.cardLog().some((x) => x.status === "CONFIRMED"), null, { timeout: 20000 });
    check(true, "6 fix: hold to confirm sent it (CONFIRMED with the token)");
    await bodyHas("confirmed", 8000);
    await placed("6 fix (confirmed)");
  }

  await page.evaluate(() => sessionStorage.setItem("gaia.judge.phone", "+12145550134"));
  await next(); await waitStep("phone");
  g = await placed("7 phone");
  check(/\(214\) 555-0134|\*\*\*|texts and calls/.test(g.body), `7 phone: says where alerts go (${g.body.slice(0, 110)})`);
  await page.click(".gt-card [data-pager]");
  await page.waitForSelector(".pager .pager-qr svg", { timeout: 10000 });
  g = await placed("7 phone (pager open)");
  check(g.pill, "7 phone: the console's pager QR is open and the card folds to a pill");
  await page.click(".gt-card [data-unpager]");
  await page.waitForFunction(() => !document.querySelector(".pager"));
  g = await placed("7 phone (pager closed)");
  check(!g.pill, "7 phone: closing the pager brings the card back");

  await next(); await waitStep("done");
  g = await placed("8 done");
  check(/\/dataset/.test(g.body) && /test\//.test(g.body), "8 done: mentions /dataset and test/");
  check(await page.evaluate(() => !!document.querySelector('.gt-card a[href="/dataset"]')), "8 done: /dataset link");
  await page.click(".gt-card [data-next]");
  await page.waitForFunction(() => !document.querySelector(".gt-card"));
  check(await page.evaluate(() => JSON.parse(sessionStorage.getItem("gaia.tour")).done === true), "end tour: stored as done");
  if (DATASET) {
    await page.waitForTimeout(1500);
    // TOUR_API: the backend's origin when the page calls it cross-origin (a VITE_API build).
    const c = await page.evaluate(async (api) => (await fetch(`${api}/api/eog/clock`)).json(), process.env.TOUR_API || u.origin);
    check(c.playing === false, `end tour (dataset): the replay clock is paused (playing ${c.playing})`);
  }
  await page.reload({ waitUntil: "domcontentloaded", timeout: 90000 });
  await page.waitForFunction(() => window.__console && !document.getElementById("loading"), null, { timeout: 90000 });
  await page.waitForTimeout(2500);
  check(await page.evaluate(() => !document.querySelector(".gt-card")), "a reload after the end shows no tour");

  // ?tour=1 starts over; skip ends it.
  await page.goto(open, { waitUntil: "domcontentloaded", timeout: 90000 });
  await waitStep("field", 90000);
  check(true, "?tour=1 starts over at step 1");
  await page.click(".gt-card [data-skip]");
  await page.waitForFunction(() => !document.querySelector(".gt-card"));
  check(true, "skip tour closes it");
} catch (e) {
  check(false, `stopped: ${String(e.message || e).split("\n")[0]} (step ${await step().catch(() => "?")})`);
  log("     card:", JSON.stringify(await geo().catch(() => null)));
}
check(errors.length === 0, `no page errors${errors.length ? `: ${errors.slice(0, 3).join(" | ")}` : ""}`);
log(`posts: ${JSON.stringify(posts)}`);
await browser.close();
log(failed ? `${failed} FAILED` : "all checks passed");
process.exit(failed ? 1 : 0);
