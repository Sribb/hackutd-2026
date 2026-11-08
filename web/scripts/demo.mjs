#!/usr/bin/env node
// npm run demo: the whole demo in one command.
//
//   1. Makes two control tokens for this run (TASK #104): an operator token for the console
//      and a phone token (confirm, reject, ack only) for the pager QR. The backend gets them
//      as FREEBOARD_TOKEN and FREEBOARD_PHONE_TOKEN, so every write needs one.
//   2. Starts the backend (backend/app/main.py) on 127.0.0.1:8000.
//   3. Starts the console. It proxies /api and /ws to the backend, so the laptop and phones
//      talk to one origin. It listens on 127.0.0.1 only, unless you pass --lan
//      (npm run demo:lan), which opens it to the Wi-Fi for the phone. --tunnel
//      (npm run demo:tunnel) also gives a public https link through a Cloudflare quick
//      tunnel (no account; needs cloudflared).
//   4. Prints the console URL and the phone URL. Each carries its token in the #fragment,
//      which the page stores and strips; fragments never go to a server or a Referer.
//
// Ctrl+C stops both. If the backend can't start, the console still runs on recorded data
// and says so on screen.

import { spawn } from "node:child_process";
import crypto from "node:crypto";
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

// cloudflared from where winget puts it, a downloaded %LOCALAPPDATA%\cloudflared\cloudflared.exe, or PATH.
function cloudflared() {
  const spots = [process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "cloudflared", "cloudflared.exe"),
    "C:\\Program Files (x86)\\cloudflared\\cloudflared.exe", "C:\\Program Files\\cloudflared\\cloudflared.exe"];
  for (const p of spots) if (p && fs.existsSync(p)) return p;
  return "cloudflared";
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

const LAN = process.argv.includes("--lan");
const TUNNEL = process.argv.includes("--tunnel");
const host = LAN ? lanIp() : "127.0.0.1";
const api = `http://127.0.0.1:${API_PORT}`;
const mint = () => crypto.randomBytes(18).toString("base64url");
const OP = process.env.FREEBOARD_TOKEN || mint();
const PH = process.env.FREEBOARD_PHONE_TOKEN || mint();
// --tunnel: a public https link through a Cloudflare quick tunnel to the console, which
// proxies /api and /ws, so one origin still serves everything. Only the tunnel's one random
// *.trycloudflare.com name is added: to the backend's origin guard (FREEBOARD_ALLOWED_HOSTS)
// and to Vite's allowedHosts (FREEBOARD_TUNNEL_HOST).
let tunnelUrl = "";
if (TUNNEL) {
  const cf = process.env.CLOUDFLARED || cloudflared();
  console.log(`Starting a Cloudflare quick tunnel with ${cf} ...`);
  const t = run(cf, ["tunnel", "--no-autoupdate", "--url", `http://127.0.0.1:${WEB_PORT}`], { cwd: ROOT }, "tunnel");
  tunnelUrl = await new Promise((resolve) => {
    const timer = setTimeout(() => resolve(""), 45000);
    const done = (url) => { clearTimeout(timer); resolve(url); };
    const look = (d) => { const m = String(d).match(/https:\/\/(?!api\.)[a-z0-9-]+\.trycloudflare\.com/); if (m) done(m[0]); };
    t.stdout.on("data", look);
    t.stderr.on("data", look);
    t.on("error", (e) => { console.log(`cloudflared didn't start (${e.code || e.message}). Install it with  winget install --id Cloudflare.cloudflared  or set CLOUDFLARED to cloudflared.exe.`); done(""); });
    t.on("exit", () => done(""));
  });
  if (!tunnelUrl) { console.log("No tunnel link came up (see the [tunnel] lines)."); stop(); }
}
const tunnelHost = tunnelUrl ? new URL(tunnelUrl).hostname : "";
const base = tunnelUrl || `http://${host}:${WEB_PORT}`;
console.log("\nGAIA demo\n");
let backend = true;
let ownTokens = true;
if (await up(`${api}/api/health`, 300)) {
  ownTokens = !!process.env.FREEBOARD_TOKEN;
  console.log(`Backend already running at ${api}, using it.${ownTokens ? "" : "\n  It keeps whatever tokens it started with (maybe none). Stop it and rerun this for per-run tokens."}`);
  if (tunnelHost) console.log(`  It refuses writes from ${tunnelHost} unless it was started with that name in FREEBOARD_ALLOWED_HOSTS. Stop it, or rerun with API_PORT=8001.`);
} else {
  const py = python();
  console.log(`Starting the backend with ${py} ...`);
  run(py, ["-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", String(API_PORT)],
    // --lan: pages load from the Wi-Fi IP, but the proxy (changeOrigin) hands the backend
    // Host 127.0.0.1, so its origin guard (#115) would refuse every write. Allow that IP.
    { cwd: ROOT, env: { ...process.env, FREEBOARD_TOKEN: OP, FREEBOARD_PHONE_TOKEN: PH,
      ...(LAN || tunnelHost ? { FREEBOARD_ALLOWED_HOSTS: [process.env.FREEBOARD_ALLOWED_HOSTS, LAN && host, tunnelHost].filter(Boolean).join(",") } : {}) } }, "api");
  backend = await up(`${api}/api/health`, 30000);
  if (!backend) console.log("\nThe backend didn't come up in 30 s (see the [api] lines). The console will run on recorded data.\n  Fix: pip install -r backend/requirements.txt\n");
}

run(process.execPath, [path.join(WEB, "node_modules/vite/bin/vite.js"), "--host", LAN ? "0.0.0.0" : "127.0.0.1", "--port", String(WEB_PORT), "--strictPort"], {
  cwd: WEB,
  env: {
    ...process.env,
    ...(backend ? { VITE_API: "same-origin", FREEBOARD_PROXY: api } : {}),
    VITE_PHONE_URL: `${base}/phone`,
    ...(tunnelHost ? { FREEBOARD_TUNNEL_HOST: tunnelHost } : {}),
  },
}, "web");

if (!(await up(`http://127.0.0.1:${WEB_PORT}/`, 30000))) {
  console.log("The console didn't start. See the [web] lines.");
  stop();
}

const frag = ownTokens ? `#k=${OP}&p=${PH}` : "";
// ?fresh: each demo run starts a new shift log and crew list (a rehearsal's entries stay out).
// The console drops it from the address bar once loaded, so a reload keeps the log.
const consoleUrl = `${base}/?fresh${frag}`;
const phoneUrl = `${base}/phone${ownTokens ? `#k=${PH}` : ""}`;
const qr = LAN || TUNNEL ? await QRCode.toString(phoneUrl, { type: "terminal", small: true }) : "";
console.log(`
  Console   ${consoleUrl}
            open this exact link: the #k= part unlocks control in that tab only
  Phone     ${LAN || TUNNEL ? `scan below, or the Pager button in the console` : "phones need the Wi-Fi: rerun with  npm run demo:lan"}
  Backend   ${backend ? api : "not running: recorded data"}
${qr}
  Demo controls (backend, operator token):
    FT='${ownTokens ? OP : "<your FREEBOARD_TOKEN>"}'
    curl -X POST ${api}/api/live/faults -H 'content-type: application/json' -H "X-Freeboard-Token: $FT" -d '{"asset_id":"PUMP_3","type":"TRIP"}'
    curl -X POST ${api}/api/live/sim    -H 'content-type: application/json' -H "X-Freeboard-Token: $FT" -d '{"speed":2}'

  ${TUNNEL ? "Public link: anyone who has it can open the page. Control still needs the #k= token, and the phone token can only confirm, reject or acknowledge." : LAN ? "On the Wi-Fi: only the phone that scans the QR can confirm, and only confirm, reject or acknowledge." : "Local only: nothing listens on the Wi-Fi."}
  Tokens are new every run. Ctrl+C stops everything.
`);
