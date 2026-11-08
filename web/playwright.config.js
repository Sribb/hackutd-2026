// Playwright for the Jarvis console (docs/JARVIS_CONSOLE.md §7 Track B): npm run test:e2e
//
// It starts its own servers, so tests never share state with a demo that's running:
//   :8921 backend, the team scenario            -> Vite :5291  (most checks)
//   :8922 backend, OPERATOR_PIN=2468            -> Vite :5292  (the PIN check)
//   :8923 backend, the scenario with no faults  -> Vite :5293  (a fresh alarm after load)
// Backend code: FREEBOARD_BACKEND_DIR (a checkout of origin/38-jarvis-iot or later; default the
// repo root). Python: FREEBOARD_PY (default python3) with backend/requirements.txt installed.
// Set FB_EXTERNAL=1 to skip starting servers and use ones you started on the same ports.
import { defineConfig } from "@playwright/test";
import path from "node:path";
import os from "node:os";

const DIR = path.resolve(process.env.FREEBOARD_BACKEND_DIR || "..");
const PY = process.env.FREEBOARD_PY || "python3";
const CLEAN = path.join(os.tmpdir(), "freeboard-clean-scenario.json");
export const PORTS = { api: 8921, pin: 8922, clean: 8923, web: 5291, webPin: 5292, webClean: 5293 };
export const PIN = "2468";

const backend = (port, env) => ({
  command: `cd "${DIR}" && env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u FREEBOARD_TOKEN -u FREEBOARD_PHONE_TOKEN -u OPERATOR_PIN -u FREEBOARD_SCENARIO JARVIS_MODE=auto ${env} "${PY}" -m uvicorn backend.app.main:app --host 127.0.0.1 --port ${port}`,
  url: `http://127.0.0.1:${port}/api/health`, reuseExistingServer: false, timeout: 60000,
});
const web = (port, api) => ({
  command: `VITE_API=http://127.0.0.1:${api} npx vite --host 127.0.0.1 --port ${port} --strictPort`,
  url: `http://127.0.0.1:${port}/`, reuseExistingServer: false, timeout: 60000,
});

export default defineConfig({
  testDir: "tests",
  // Only the browser specs. tests/*.test.mjs are the node checks (npm test); one of them
  // calls process.exit on import, which ended discovery with 0 tests and exit 0.
  testMatch: "**/*.spec.js",
  timeout: 120000,
  expect: { timeout: 20000 },
  workers: 1,           // the checks change backend state, in file order
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORTS.web}`,
    viewport: { width: 1440, height: 900 },
    launchOptions: { args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] },
  },
  webServer: process.env.FB_EXTERNAL ? undefined : [
    backend(PORTS.api, ""),
    backend(PORTS.pin, `OPERATOR_PIN=${PIN}`),
    { ...backend(PORTS.clean, `FREEBOARD_SCENARIO="${CLEAN}"`), command: `node tests/support/clean-scenario.mjs "${DIR}" "${CLEAN}" && ${backend(PORTS.clean, `FREEBOARD_SCENARIO="${CLEAN}"`).command}` },
    web(PORTS.web, PORTS.api),
    web(PORTS.webPin, PORTS.pin),
    web(PORTS.webClean, PORTS.clean),
  ],
});
