// Jarvis console, spec §7 Track B checks (docs/JARVIS_CONSOLE.md). Runs in file order against
// the servers playwright.config.js starts. Screenshots land in web/console-screens/.
import { test, expect } from "@playwright/test";
import fs from "node:fs";
import { PORTS, PIN } from "../playwright.config.js";

const API = `http://127.0.0.1:${PORTS.api}`;
const API_PIN = `http://127.0.0.1:${PORTS.pin}`;
const API_CLEAN = `http://127.0.0.1:${PORTS.clean}`;
const WEB_PIN = `http://127.0.0.1:${PORTS.webPin}`;
const WEB_CLEAN = `http://127.0.0.1:${PORTS.webClean}`;
const SHOTS = new URL("../console-screens/", import.meta.url).pathname;
fs.mkdirSync(SHOTS, { recursive: true });

test.describe.configure({ mode: "serial" });

async function boot(page, url = "/") {
  await page.goto(url);
  await page.waitForFunction(() => !document.getElementById("loading") && window.__console, null, { timeout: 60000 });
  // The agents post once the field data and the first poll are in.
  await expect(page.locator("#csStatus #csForecast .v")).not.toBeEmpty();
}
const usd = (n) => "$" + Math.round(n).toLocaleString("en-US");
const toMin = (c) => { const m = /(\d{1,2}):(\d{2})\s*(AM|PM)?/i.exec(c || ""); if (!m) return null; let h = Number(m[1]); if (m[3]) { h %= 12; if (/pm/i.test(m[3])) h += 12; } return h * 60 + Number(m[2]); };
async function holdOn(page, locator, ms) {
  await expect(page.locator("#csFix [data-loading]")).toHaveCount(0); // the card has settled
  await page.waitForTimeout(150);
  const box = await locator.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.waitForTimeout(ms);
  await page.mouse.up();
}
async function ask(page, text) {
  await page.locator("#csInput").fill(text);
  await page.locator("#csInput").press("Enter");
}

// ---------------------------------------------------------------- 1 ----
test("1. default load: full-bleed canvas, status line, Jarvis closed, J opens it, classic keeps #side", async ({ page, request }) => {
  // The team scenario trips Pump 3 at minute 3. Let that happen before the page loads: an alarm
  // that was already there at load never pops Jarvis open (only a new one does, check 2).
  await request.post(`${API}/api/live/sim`, { data: { advance_min: 5 } });
  await boot(page);
  const box = await page.evaluate(() => { const r = document.getElementById("scene").getBoundingClientRect(); return { w: r.width, h: r.height, vw: innerWidth, vh: innerHeight }; });
  expect(box.w).toBe(box.vw);
  expect(box.h).toBe(box.vh - 32);
  await expect(page.locator("#side")).toBeHidden();
  for (const id of ["csFeed", "csWatch", "csForecast", "csPlan"]) await expect(page.locator(`#${id}`)).toBeVisible();
  await expect(page.locator("#csWatch")).toContainText("WATCH");
  await expect(page.locator("#csForecast")).toContainText("FORECAST");
  await expect(page.locator("#csPlan")).toContainText("PLAN");
  await expect(page.locator("#csJarvis")).toBeHidden();
  await expect(page.locator("#modelNote")).toBeHidden(); // no debug "Model:" label
  await expect(page.locator(".timebar")).not.toBeInViewport(); // hidden until time != now
  await page.screenshot({ path: `${SHOTS}jarvis-closed.png` });
  const t0 = Date.now();
  await page.keyboard.press("j");
  await expect(page.locator("#csJarvis")).toBeVisible({ timeout: 200 });
  expect(Date.now() - t0).toBeLessThan(1000);
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${SHOTS}jarvis-open.png` });
  await page.goto("/?layout=classic");
  await expect(page.locator("#side")).toBeVisible();
  await expect(page.locator("#csStatus")).toHaveCount(0);
});

// ---------------------------------------------------------------- 2 ----
test("2. a new root-cause P1 auto-opens Jarvis once; Watch bubble after typing dots; chip lights the asset", async ({ page, request }) => {
  await boot(page, WEB_CLEAN);
  await expect(page.locator("#csJarvis")).toBeHidden();
  await page.evaluate(() => {
    window.__seen = [];
    new MutationObserver((ms) => { for (const m of ms) for (const n of m.addedNodes) if (n.classList) window.__seen.push(n.className); })
      .observe(document.getElementById("csStream"), { childList: true });
  });
  expect((await request.post(`${API_CLEAN}/api/live/faults`, { data: { asset_id: "PUMP_3", type: "TRIP" } })).ok()).toBeTruthy();
  for (let i = 0; i < 3; i++) await request.post(`${API_CLEAN}/api/live/sim`, { data: { advance_min: 1 } });
  await expect(page.locator("#csJarvis")).toBeVisible();
  const watch = page.locator("#csStream li.a-W.live");
  await expect(watch.locator(".cs-dot.red")).toBeVisible();
  const chip = watch.locator('.cs-chip[data-asset="PUMP_3"]');
  await expect(chip).toHaveText("Pump 3");
  const seen = await page.evaluate(() => window.__seen);
  const iTyping = seen.findIndex((c) => /typing/.test(c) && /a-W/.test(c));
  const iBubble = seen.findIndex((c, i) => i > iTyping && /a-W/.test(c) && /live/.test(c) && !/typing/.test(c));
  expect(iTyping).toBeGreaterThanOrEqual(0);
  expect(iBubble).toBeGreaterThan(iTyping);
  await chip.click();
  await expect.poll(() => page.evaluate(() => window.__freeboardHL.state().sources.jarvis)).toEqual(["PUMP_3"]);
  // Once: closing it and more readings of the same alarm don't open it again.
  await page.locator("#csClose").click();
  for (let i = 0; i < 3; i++) await request.post(`${API_CLEAN}/api/live/sim`, { data: { advance_min: 2 } });
  await page.waitForTimeout(1500);
  await expect(page.locator("#csJarvis")).toBeHidden();
});

// ---------------------------------------------------------------- 3a ----
test("3a. one shutdown clock everywhere: status line, Forecast bubble and Jarvis agree", async ({ page }) => {
  await boot(page);
  const cd = await page.evaluate(() => window.__console.countdown());
  expect(cd).not.toBeNull();
  const title = await page.locator("#csForecast").getAttribute("title");
  expect(title).toContain(cd.clock);
  await page.keyboard.press("j");
  await expect(page.locator("#csStream li.a-F.live")).toContainText(cd.clock);
  // Jarvis's own "time left" (rules, the backend's live forecast) is within 1 min of the console.
  await ask(page, "time left");
  const reply = page.locator("#csStream li.a-J").last();
  await expect(reply.locator(".cs-text")).toContainText(/\d{1,2}:\d{2} [AP]M/);
  const said = /(\d{1,2}:\d{2} [AP]M)/.exec(await reply.locator(".cs-text").innerText())[1];
  const now = await page.evaluate(() => window.__console.countdown());
  expect(Math.abs(toMin(said) - toMin(now.clock))).toBeLessThanOrEqual(1);
  // The minutes on the status line come from that same clock.
  const left = /(\d+) min/.exec(await page.locator("#csForecast .t.full").innerText());
  if (left) {
    const nowMin = await page.evaluate(() => window.__console.now());
    expect(Math.abs(Number(left[1]) - (toMin(now.clock) - nowMin))).toBeLessThanOrEqual(1);
  }
});

// ---------------------------------------------------------------- 4 ----
test("4. Planner bars show the cheapest robust plan and the do-nothing cost from the brief", async ({ page }) => {
  await boot(page);
  const b = await page.evaluate(() => window.__console.brief());
  expect(b.plans.robust).not.toBeNull();
  if (b.source === "fallback") {
    const plans = await (await page.request.post(`${API}/api/plans`, { data: {} })).json();
    const robust = plans.plans.find((p) => p.robust && p.keeps_running !== false);
    expect(b.plans.robust.total_usd).toBe(robust.cost.total_usd);
  }
  await page.keyboard.press("j");
  const p = page.locator("#csStream li.a-P.live");
  await expect(p.locator('[data-v="fix"]')).toHaveText(usd(b.plans.robust.total_usd));
  await expect(p.locator('[data-v="wait"]')).toHaveText(usd(b.do_nothing_usd));
  await expect(page.locator("#csPlan")).toContainText(`${usd(b.plans.robust.total_usd)} vs ${usd(b.do_nothing_usd)}`);
});

// ---------------------------------------------------------------- 6 ----
test("6. a confirm that comes back UNCERTAIN says so, with no retry button", async ({ page, request }) => {
  await boot(page, WEB_CLEAN);
  await page.route("**/api/actions/*/confirm", async (route) => {
    const id = route.request().url().split("/").slice(-2)[0];
    await route.fulfill({ json: { id, status: "UNCERTAIN", result: "The RTU took the command but never acknowledged it.", decided_by: "console" } });
  });
  await page.keyboard.press("j");
  await ask(page, "turn off pump 2");
  const card = page.locator("#csFix");
  await expect(card).toHaveAttribute("data-status", "PENDING");
  await holdOn(page, card.locator("[data-hold]"), 1700);
  await expect(card).toContainText("Sent, not confirmed");
  await expect(card).toHaveAttribute("data-status", "UNCERTAIN");
  await expect(card.locator("[data-hold]")).toHaveCount(0);
  await expect(card.getByRole("button", { name: /retry|try again/i })).toHaveCount(0);
  // Leave the backend clean: the mocked confirm never reached it.
  const id = await card.getAttribute("data-id");
  await request.post(`${API_CLEAN}/api/actions/${id}/reject`, { data: { by: "test" } });
});

// ---------------------------------------------------------------- 8 ----
test("8. view intents work with /api/jarvis down, and never call it", async ({ page }) => {
  await boot(page);
  const calls = [];
  page.on("request", (r) => { if (/\/api\/jarvis$/.test(new URL(r.url()).pathname)) calls.push(r.url()); });
  await page.route(/\/api\/jarvis$/, (route) => route.abort());
  await page.keyboard.press("j");
  await ask(page, "open pump 3");
  await expect(page.locator("#csStream li.a-J").last()).toContainText("Pump 3");
  await expect.poll(() => page.evaluate(() => window.__freeboardHL.state().sources.jarvis)).toContain("PUMP_3");
  await ask(page, "show tank 3's sensors");
  await expect(page.locator("#csStream li.a-J").last()).toContainText("Tank 3");
  await expect.poll(() => page.evaluate(() => window.__freeboardHL.state().sources.sensors || [])).toContain("TANK_3_lt");
  await ask(page, "reset");
  await expect(page.locator("#csStream li.a-J").last()).toContainText(/reset/i);
  await expect.poll(() => page.evaluate(() => window.__freeboardHL.state().sources.jarvis || [])).toEqual([]);
  expect(calls).toEqual([]);
});

// ---------------------------------------------------------------- 5 ----
test("5. fix card: one card; 800 ms sends nothing; 1600 ms sends exactly one; SENDING then CONFIRMED; no echo card", async ({ page }) => {
  await boot(page);
  const confirms = [];
  page.on("request", (r) => { if (/\/confirm$/.test(r.url()) && r.method() === "POST") confirms.push(r.url()); });
  await page.keyboard.press("j");
  await ask(page, "open the crossover");
  const card = page.locator("#csFix");
  await expect(card).toHaveAttribute("data-status", "PENDING");
  await expect(page.locator(".cs-fix")).toHaveCount(1);
  await expect(card).toContainText(/crossover|XO 34/i);
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOTS}fix-card.png` });
  await holdOn(page, card.locator("[data-hold]"), 800);
  await page.waitForTimeout(500);
  expect(confirms).toHaveLength(0);
  await expect(card).toHaveAttribute("data-status", "PENDING");
  await holdOn(page, card.locator("[data-hold]"), 1600);
  await expect.poll(() => page.evaluate(() => window.__console.cardLog().map((x) => x.status))).toEqual(expect.arrayContaining(["SENDING", "CONFIRMED"]));
  const log = await page.evaluate(() => window.__console.cardLog().map((x) => x.status));
  expect(log.indexOf("SENDING")).toBeLessThan(log.indexOf("CONFIRMED"));
  await expect(card).toContainText("Done, confirmed by console");
  await page.waitForTimeout(1500); // the ws echo of the same proposal
  expect(confirms).toHaveLength(1);
  await expect(page.locator(".cs-fix")).toHaveCount(1);
});

// ---------------------------------------------------------------- 7 ----
test("7. with OPERATOR_PIN: hold, PIN prompt, wrong PIN says nothing was sent and the audit has no confirm", async ({ page, request }) => {
  await boot(page, WEB_PIN);
  await page.keyboard.press("j");
  // The proposal comes from another operator screen (with the PIN), so it reaches this one by /ws.
  const made = await request.post(`${API_PIN}/api/actions`, { headers: { "X-Freeboard-Token": PIN }, data: { type: "SET_PUMP", asset_id: "PUMP_2", value: "OFF", reason: "test" } });
  expect(made.ok()).toBeTruthy();
  const p = await made.json();
  const card = page.locator("#csFix");
  await expect(card).toHaveAttribute("data-id", p.id);
  await holdOn(page, card.locator("[data-hold]"), 1700);
  const pin = page.locator(".pin-ask input");
  await expect(pin).toBeVisible();
  await pin.fill("0000");
  await pin.press("Enter");
  await expect(page.locator("#csJarvis")).toContainText("Wrong PIN. Nothing was sent.");
  await expect(card).toHaveAttribute("data-status", "PENDING");
  const audit = await (await request.get(`${API_PIN}/api/audit`)).json();
  expect(audit.entries.filter((e) => e.event === "confirmed" && (e.proposal_id === p.id || e.action_id === p.id || JSON.stringify(e).includes(p.id)))).toHaveLength(0);
  // The right PIN, on the next hold, goes through once.
  await holdOn(page, card.locator("[data-hold]"), 1700);
  await expect(pin).toBeVisible();
  await pin.fill(PIN);
  await pin.press("Enter");
  await expect(card).toContainText("Done, confirmed by console");
});

// ---------------------------------------------------------------- 3b ----
test("3b. a stale tank: hatched Forecast bar that starts with Can't vouch, and never an all clear", async ({ page, request }) => {
  await boot(page);
  expect((await request.post(`${API}/api/live/faults`, { data: { asset_id: "TANK_5", type: "SENSOR_STALE" } })).ok()).toBeTruthy();
  await request.post(`${API}/api/live/sim`, { data: { advance_min: 6 } });
  await expect(page.locator("#csForecast .cs-cd.hatched")).toBeVisible();
  await expect(page.locator("#csForecast .t.full")).toHaveText(/^Can't vouch for Tank 5/);
  await page.keyboard.press("j");
  await ask(page, "what's failing");
  await ask(page, "time left");
  await expect(page.locator("#csStream li.a-J")).toHaveCount(2);
  await expect(page.locator("#csStream li.a-F.live")).toContainText("Can't vouch for Tank 5");
  await page.waitForTimeout(600);
  await page.screenshot({ path: `${SHOTS}stale-tank.png` });
  const body = (await page.locator("body").innerText()).toLowerCase();
  expect(body).not.toContain("all clear");
  expect(body).not.toContain("all quiet");
  expect(body).not.toContain("every tank is safe");
});

// ---------------------------------------------------------------- 9 ----
test("9. control token: taken from ?token= once, stripped from the URL, on every POST, and sent on /ws", async ({ page }) => {
  const sockets = [];
  page.on("websocket", (ws) => {
    const rec = { url: ws.url(), sent: [] };
    sockets.push(rec);
    ws.on("framesent", (f) => rec.sent.push(String(f.payload)));
  });
  const posts = [];
  page.on("request", (r) => { if (r.method() === "POST" && r.url().startsWith(API)) posts.push(r.headers()["x-freeboard-token"] || ""); });
  await boot(page, "/?token=tok-123");
  expect(new URL(page.url()).searchParams.get("token")).toBeNull();
  expect(await page.evaluate(() => sessionStorage.getItem("freeboard.token"))).toBe("tok-123");
  await expect.poll(() => posts.length).toBeGreaterThan(0);
  expect(posts.every((t) => t === "tok-123")).toBeTruthy();
  // The socket carries the token in its first message ({"type":"auth"}), or in ?token= after a
  // server that only reads the query refuses it (1008/4401).
  await expect.poll(() => sockets.some((s) => s.sent.some((m) => /"type":"auth"/.test(m) && /tok-123/.test(m)) || /token=tok-123/.test(s.url))).toBeTruthy();
});

// --------------------------------------------------------------- 10 ----
test("10. backend gone: Watch says alerts are NOT being received, never no alarms", async ({ page }) => {
  await boot(page);
  await page.route(/127\.0\.0\.1:8921\/(api|ws)/, (route) => route.abort());
  await expect(page.locator("#csWatch")).toContainText("Can't reach the backend", { timeout: 30000 });
  await expect(page.locator("#csPlan")).not.toContainText("No fix needed");
  const body = (await page.locator("body").innerText()).toLowerCase();
  expect(body).not.toContain("all clear");
});
