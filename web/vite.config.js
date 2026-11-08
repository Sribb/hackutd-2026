import { defineConfig } from "vite";
import fs from "node:fs";
import path from "node:path";

// The model lives in blender/ (Sriyan's lane). Serve it at /field.glb in dev and
// copy it into dist/ on build, so web/ never keeps its own copy.
const GLB = path.resolve(__dirname, "../blender/field.glb");

function fieldGlb() {
  return {
    name: "field-glb",
    configureServer(server) {
      server.middlewares.use("/field.glb", (req, res) => {
        if (!fs.existsSync(GLB)) {
          res.statusCode = 404;
          res.end("blender/field.glb not in repo yet");
          return;
        }
        res.setHeader("Content-Type", "model/gltf-binary");
        res.setHeader("Cache-Control", "no-store");
        fs.createReadStream(GLB).pipe(res);
      });
    },
    writeBundle(options) {
      if (fs.existsSync(GLB)) fs.copyFileSync(GLB, path.join(options.dir, "field.glb"));
    },
  };
}

// MediaPipe's WASM runtime ships inside the npm package. Serve it from /mediapipe/wasm so
// hand control works with no CDN, and copy it into dist/ on build.
const MP_WASM = path.resolve(__dirname, "node_modules/@mediapipe/tasks-vision/wasm");

function mediapipeWasm() {
  return {
    name: "mediapipe-wasm",
    configureServer(server) {
      server.middlewares.use("/mediapipe/wasm", (req, res, next) => {
        const file = path.join(MP_WASM, path.basename(req.url.split("?")[0]));
        if (!fs.existsSync(file)) return next();
        res.setHeader("Content-Type", file.endsWith(".wasm") ? "application/wasm" : "text/javascript");
        fs.createReadStream(file).pipe(res);
      });
    },
    writeBundle(options) {
      const out = path.join(options.dir, "mediapipe", "wasm");
      fs.mkdirSync(out, { recursive: true });
      for (const f of fs.readdirSync(MP_WASM)) fs.copyFileSync(path.join(MP_WASM, f), path.join(out, f));
    },
  };
}

// three.js's Draco decoder, for a Draco-compressed field.glb (build_field.py --compress, #97).
// Served from /draco/ out of node_modules and copied into dist/ on build, like MediaPipe's.
// The loader only fetches it when a model actually uses Draco.
const DRACO = path.resolve(__dirname, "node_modules/three/examples/jsm/libs/draco/gltf");
const DRACO_FILES = ["draco_decoder.wasm", "draco_wasm_wrapper.js", "draco_decoder.js"];

function dracoDecoder() {
  return {
    name: "draco-decoder",
    configureServer(server) {
      server.middlewares.use("/draco", (req, res, next) => {
        const name = path.basename(req.url.split("?")[0]);
        if (!DRACO_FILES.includes(name)) return next();
        res.setHeader("Content-Type", name.endsWith(".wasm") ? "application/wasm" : "text/javascript");
        fs.createReadStream(path.join(DRACO, name)).pipe(res);
      });
    },
    writeBundle(options) {
      const out = path.join(options.dir, "draco");
      fs.mkdirSync(out, { recursive: true });
      for (const f of DRACO_FILES) if (fs.existsSync(path.join(DRACO, f))) fs.copyFileSync(path.join(DRACO, f), path.join(out, f));
    },
  };
}

// The phone view (#55) lives at /phone.
function phoneRoute() {
  return {
    name: "phone-route",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (/^\/phone\/?(\?|$)/.test(req.url)) req.url = req.url.replace(/^\/phone\/?/, "/phone.html");
        next();
      });
    },
  };
}

const SECRET_FILES = [".env", ".env.*", "*.{pem,key,crt,p12}", "**/.git/**", "**/.venv/**", "**/*.sqlite*"];

export default defineConfig({
  preview: { cors: false },
  plugins: [fieldGlb(), mediapipeWasm(), dracoDecoder(), phoneRoute()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    // #104: no cross-origin reads of the dev server, and it serves nothing outside web/
    // (Vite's own client and node_modules excepted). Secrets are denied by name even inside it.
    cors: false,
    fs: { strict: true, allow: [__dirname], deny: SECRET_FILES },
    // npm run demo sets FREEBOARD_PROXY so the page, /api and /ws share one origin.
    proxy: process.env.FREEBOARD_PROXY ? {
      "/api": { target: process.env.FREEBOARD_PROXY, changeOrigin: true },
      "/ws": { target: process.env.FREEBOARD_PROXY.replace(/^http/, "ws"), ws: true, changeOrigin: true },
    } : undefined,
  },
  build: {
    chunkSizeWarningLimit: 1200,
    rollupOptions: { input: { main: path.resolve(__dirname, "index.html"), phone: path.resolve(__dirname, "phone.html") } },
  },
});
