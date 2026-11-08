#!/usr/bin/env node
// npm run demo: the whole demo in one command.
//
//   1. Starts the backend (backend/app/main.py) on 127.0.0.1:8000.
//   2. Starts the console on 0.0.0.0:5173. It proxies /api and /ws to the backend, so the
//      laptop and any phone on the same Wi-Fi talk to one origin (no CORS setup).
//   3. Prints the console URL, the phone URL, and a QR code for the phone.
//
// Ctrl+C stops both. If the backend can't start, the console still runs on recorded data
// and says so on screen.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import QRCode from "qrcode";

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = path.resolve(WEB, "..");
const API_PORT = Number(process.env.API_PORT || 8000);
const WEB_PORT = Number(process.env.WEB_PORT || 5173);

function lanIp() {
  for (const list of Object.values(os.networkInterfaces())) {
    for (const a of list || []) if (a.family === "IPv4" && !a.internal && !a.address.startsWith("169.254.")) return a.address;
  }
  return "127.0.0.1";
}

function python() {
  for (const p of [".venv/bin/python", ".venv/Scripts/python.exe", "backend/.venv/bin/python"]) {
    const full = path.join(ROOT, p);
    if (fs.existsSync(full)) return full;
  }
  return process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
}

async function up(url, ms) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    try { if ((await fetch(url)).ok) return true; } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

const kids = [];
function run(cmd, args, opts, tag) {
  const p = spawn(cmd, args, { ...opts, stdio: ["ignore", "pipe", "pipe"] });
  const out = (d) => { for (const line of String(d).split("\n")) if (line.trim()) process.stdout.write(`\x1b[2m[${tag}]\x1b[0m ${line}\n`); };
  p.stdout.on("data", out);
  p.stderr.on("data", out);
  kids.push(p);
  return p;
}
function stop() { for (const k of kids) { try { k.kill("SIGINT"); } catch { /* gone */ } } process.exit(0); }
process.on("SIGINT", stop);
process.on("SIGTERM", stop);

const ip = lanIp();
const api = `http://127.0.0.1:${API_PORT}`;
const consoleUrl = `http://${ip}:${WEB_PORT}/`;
const phoneUrl = `http://${ip}:${WEB_PORT}/phone`;

console.log("\nFREEBOARD demo\n");
let backend = true;
if (await up(`${api}/api/health`, 300)) {
  console.log(`Backend already running at ${api}, using it.`);
} else {
  const py = python();
  console.log(`Starting the backend with ${py} ...`);
  run(py, ["-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", String(API_PORT)], { cwd: ROOT, env: process.env }, "api");
  backend = await up(`${api}/api/health`, 30000);
  if (!backend) console.log("\nThe backend didn't come up in 30 s (see the [api] lines). The console will run on recorded data.\n  Fix: pip install -r backend/requirements.txt\n");
}

run(process.execPath, [path.join(WEB, "node_modules/vite/bin/vite.js"), "--host", "0.0.0.0", "--port", String(WEB_PORT), "--strictPort"], {
  cwd: WEB,
  env: {
    ...process.env,
    ...(backend ? { VITE_API: "same-origin", FREEBOARD_PROXY: api } : {}),
    VITE_PHONE_URL: phoneUrl,
  },
}, "web");

if (!(await up(`http://127.0.0.1:${WEB_PORT}/`, 30000))) {
  console.log("The console didn't start. See the [web] lines.");
  stop();
}

const qr = await QRCode.toString(phoneUrl, { type: "terminal", small: true });
console.log(`
  Console   ${consoleUrl}   (or http://127.0.0.1:${WEB_PORT}/ on this laptop)
  Phone     ${phoneUrl}     same Wi-Fi, scan below or the Pager button in the console
  Backend   ${backend ? api : "not running: recorded data"}

${qr}
  Demo controls (backend):
    curl -X POST ${api}/api/live/faults -H 'content-type: application/json' -d '{"asset_id":"PUMP_3","type":"TRIP"}'
    curl -X POST ${api}/api/live/sim    -H 'content-type: application/json' -d '{"speed":2}'

  No login on /phone: anyone on this Wi-Fi can confirm a fix. Demo only.
  Ctrl+C stops everything.
`);
