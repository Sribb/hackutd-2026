// Run the lab page in a fresh headless Chrome with the GPU on, print its measurements, save a screenshot.
// Node 22+ only (built-in WebSocket and fetch), no npm packages.
//   node blender/lab/measure.mjs <profile-dir> <out-prefix> <url> [<url> ...]
// Each URL writes <out-prefix>-<n>.png and one JSON line to stdout. <profile-dir> must be a new, empty
// folder: never point it at a real Chrome profile.
import { spawn } from "node:child_process";
import fs from "node:fs";

const [profile, prefix, ...urls] = process.argv.slice(2);
if (!profile || !prefix || !urls.length) { console.error("usage: measure.mjs <profile-dir> <out-prefix> <url>..."); process.exit(2); }
const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9300 + Math.floor(Math.random() * 500);
const W = +(process.env.LAB_W || 1600), H = +(process.env.LAB_H || 900);
const chrome = spawn(CHROME, [
  "--headless=new", `--user-data-dir=${profile}`, `--remote-debugging-port=${PORT}`,
  "--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist",
  "--disable-frame-rate-limit", "--no-first-run", "--no-default-browser-check", `--window-size=${W},${H}`, "about:blank",
], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let ws, nextId = 1;
const pending = new Map();
function send(method, params = {}, sessionId) {
  const id = nextId++;
  ws.send(JSON.stringify({ id, method, params, sessionId }));
  return new Promise((res, rej) => pending.set(id, { res, rej }));
}

try {
  let info;
  for (let i = 0; i < 50 && !info; i++) { await sleep(200); info = await fetch(`http://127.0.0.1:${PORT}/json/version`).then((r) => r.json()).catch(() => null); }
  ws = new WebSocket(info.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
  });
  for (const [n, url] of urls.entries()) {
    const { targetId } = await send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
    await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false }, sessionId);
    await send("Page.enable", {}, sessionId);
    await send("Page.navigate", { url }, sessionId);
    let out = null;
    for (let i = 0; i < 600 && !out; i++) {
      await sleep(250);
      const r = await send("Runtime.evaluate", { expression: "JSON.stringify(window.__lab || null)", returnByValue: true }, sessionId);
      out = JSON.parse(r.result.value || "null");
    }
    await sleep(300);
    const shot = await send("Page.captureScreenshot", { format: "png" }, sessionId);
    const file = `${prefix}-${n}.png`;
    fs.writeFileSync(file, Buffer.from(shot.data, "base64"));
    console.log(JSON.stringify({ url, png: file, ...(out || { error: "timeout" }) }));
    await send("Target.closeTarget", { targetId });
  }
} finally {
  chrome.kill();
}
