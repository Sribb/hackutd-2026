// Headless run of the Remote layout's demo path (#114). Not part of the build.
//
//   API=http://127.0.0.1:8731 WEB=http://127.0.0.1:5181 node web/remote-screens/drive.mjs
//
// Needs a backend from the live layer (#38) and `VITE_API=$API npm run dev` serving WEB.
// Playwright comes from wherever you installed it (PLAYWRIGHT=/path/to/node_modules/playwright);
// CHROME points at a Chromium binary if Playwright's own isn't installed.
// Path: load, trip Pump 3, alert shows, ask Jarvis, proposal card, short hold does nothing,
// full hold confirms, the crossover reads OPEN, Classic shows the old panels.

import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT || "playwright");
const API = process.env.API || "http://127.0.0.1:8731";
const WEB = process.env.WEB || "http://127.0.0.1:5181";
const OUT = path.dirname(fileURLToPath(import.meta.url));

const post = async (p, body) => {
  const r = await fetch(API + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return r.json();
};
const ok = (cond, msg) => { if (!cond) throw new Error(`FAIL: ${msg}`); console.log(`PASS  ${msg}`); };

const browser = await chromium.launch({
  executablePath: process.env.CHROME || undefined,
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const posts = [];
page.on("request", (r) => { if (r.method() === "POST") posts.push({ url: r.url(), token: r.headers()["x-freeboard-token"] || null }); });
const sockets = [];
page.on("websocket", (ws) => sockets.push(ws.url()));
page.on("pageerror", (e) => console.log("pageerror:", e.message));

// 1. Load with a control token in the fragment. Remote is the default screen.
await page.goto(`${WEB}/#k=demo-token-123`);
await page.waitForSelector("#remote", { state: "visible", timeout: 60000 });
await page.waitForFunction(() => !document.getElementById("loading"), null, { timeout: 90000 });
ok(await page.evaluate(() => document.body.classList.contains("fb-remote")), "Remote is the default layout");
ok(await page.evaluate(() => getComputedStyle(document.getElementById("side")).display === "none"), "classic side panel is hidden");
ok(await page.evaluate(() => !location.hash.includes("k=") && sessionStorage.getItem("freeboard.token") === "demo-token-123"), "token kept in sessionStorage, stripped from the URL");

// 2. Trip Pump 3 in the simulator.
console.log("fault:", JSON.stringify(await post("/api/live/faults", { asset_id: "PUMP_3", type: "TRIP" })));
await page.waitForSelector("#rmAlerts .rm-alert:has-text('Pump 3')", { timeout: 30000 });
ok(true, "Pump 3 alert shows in the Remote alerts");
const prio = await page.textContent("#rmAlerts .rm-alert:has-text('Pump 3') .rm-prio");
ok(prio.trim() === "P1", `the Pump 3 alert is ${prio.trim()}`);
await page.waitForFunction(() => / min$/.test(document.getElementById("rmBig").textContent), null, { timeout: 20000 });
ok(true, `countdown shows: ${(await page.textContent("#rmBig")).trim()} | ${(await page.textContent("#rmSub")).trim()}`);

// 3. Ask Jarvis.
await page.fill("#rmSay", "Why is Tank 3 rising?");
await page.press("#rmSay", "Enter");
await page.waitForSelector("#rmChat li.m-jarvis p:not(.jv-wait)", { timeout: 30000 });
const reply = await page.textContent("#rmChat li.m-jarvis p");
ok(/Tank 3/.test(reply), `Jarvis answers: ${reply.slice(0, 90)}...`);
ok(await page.locator("#rmChat .rm-chip").count() > 0, "the reply has asset chips");
await page.locator("#rmChat .rm-chip").first().click();

// The rules engine answers "why" without proposing, so ask for the fix it names.
let card = page.locator("#rmFixes .rm-fix.PENDING");
if (!(await card.count())) {
  await page.fill("#rmSay", "Open the Tank 3 to 4 crossover");
  await page.press("#rmSay", "Enter");
}
await page.waitForSelector("#rmFixes .rm-fix.PENDING", { timeout: 30000 });
card = page.locator("#rmFixes .rm-fix.PENDING").first();
const pid = await card.getAttribute("data-id");
ok(!!pid, `proposal card ${pid}: ${(await card.locator(".rm-fix-text").textContent()).trim()}`);
await page.waitForTimeout(1500); // let the camera finish its flight
await page.screenshot({ path: path.join(OUT, "remote.png") });

// 4. A short hold (500 ms) must not confirm.
const btn = page.locator(`#rmFixes [data-hold="${pid}"]`);
const box = await btn.boundingBox();
await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
await page.mouse.down();
await page.waitForTimeout(500);
await page.mouse.up();
await page.waitForTimeout(800);
let st = (await (await fetch(`${API}/api/actions`)).json()).proposals.find((p) => p.id === pid);
ok(st.status === "PENDING", "a 0.5 s hold sends nothing (still PENDING)");

// A hold that leaves the button is cancelled too.
await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
await page.mouse.down();
await page.waitForTimeout(400);
await page.mouse.move(box.x - 200, box.y - 200);
await page.waitForTimeout(1200);
await page.mouse.up();
st = (await (await fetch(`${API}/api/actions`)).json()).proposals.find((p) => p.id === pid);
ok(st.status === "PENDING", "a hold that slides off the button sends nothing");

// 5. The full hold confirms.
const b2 = await page.locator(`#rmFixes [data-hold="${pid}"]`).boundingBox();
await page.mouse.move(b2.x + b2.width / 2, b2.y + b2.height / 2);
await page.mouse.down();
await page.waitForTimeout(1500);
await page.mouse.up();
await page.waitForSelector(`#rmFixes .rm-fix.CONFIRMED[data-id="${pid}"]`, { timeout: 15000 });
st = (await (await fetch(`${API}/api/actions`)).json()).proposals.find((p) => p.id === pid);
ok(st.status === "CONFIRMED", `the full hold confirmed ${pid} (decided_by ${st.decided_by})`);
const confirms = posts.filter((p) => p.url.includes(`/api/actions/${pid}/confirm`));
ok(confirms.length === 1, `exactly one confirm POST for ${pid}`);
await page.waitForTimeout(2500);
const valve = (await (await fetch(`${API}/api/live/state`)).json()).assets.VALVE_XO_34;
ok(valve.position === "OPEN", `VALVE_XO_34 reads ${valve.position}`);

// 6. The token went on every POST and on the socket.
ok(posts.length > 0 && posts.every((p) => p.token === "demo-token-123"), `X-Freeboard-Token on all ${posts.length} POSTs`);
const feeds = sockets.filter((u) => new URL(u).pathname === "/ws"); // the other one is Vite's own dev socket
ok(feeds.length > 0 && feeds.every((u) => /[?&]token=demo-token-123/.test(u)), `?token= on /ws (${feeds.length} socket)`);
ok(feeds.length === 1, "one /ws socket");

// 7. A tank that stops reporting is never vouched for.
console.log("fault:", JSON.stringify(await post("/api/live/faults", { asset_id: "TANK_5", type: "SENSOR_STALE" })));
await page.waitForFunction(() => /Can't vouch for[^]*no fresh reading for[^]*Tank 5/.test(document.getElementById("rmCount").innerText), null, { timeout: 120000 });
ok(true, `blind tank: ${(await page.locator("#rmCount").innerText()).replace(/\s+/g, " ").trim()}`);
ok(!/All clear/.test(await page.locator("#rmCount").innerText()), "no all-clear while a tank is UNKNOWN");

// 8. Classic shows the old panels, and the choice is remembered.
await page.click("#rmClassic");
await page.waitForTimeout(400);
ok(await page.evaluate(() => getComputedStyle(document.getElementById("side")).display !== "none" && getComputedStyle(document.getElementById("remote")).display === "none"), "Classic shows the old panels");
ok(await page.evaluate(() => localStorage.getItem("freeboard.layout") === "classic"), "Classic is remembered");
await page.screenshot({ path: path.join(OUT, "classic.png") });
await page.reload();
await page.waitForSelector("#side", { state: "visible", timeout: 60000 });
ok(await page.evaluate(() => document.body.classList.contains("fb-classic")), "Classic survives a reload");
await page.click("#rmBack");
ok(await page.evaluate(() => document.body.classList.contains("fb-remote")), "Remote button goes back");

await browser.close();
console.log("ALL PASS");
