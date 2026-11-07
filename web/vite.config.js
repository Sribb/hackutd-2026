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

export default defineConfig({
  plugins: [fieldGlb()],
  server: { host: "127.0.0.1", port: 5173 },
  build: { chunkSizeWarningLimit: 1200 },
});
