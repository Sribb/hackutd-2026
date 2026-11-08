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

export default defineConfig({
  plugins: [fieldGlb(), mediapipeWasm()],
  server: { host: "127.0.0.1", port: 5173 },
  build: { chunkSizeWarningLimit: 1200 },
});
