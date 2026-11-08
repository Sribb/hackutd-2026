# 3D quality: what moves it, what it costs, what to do tonight

Issue #111. Short answer: yes, the field can look far better, and most of the gain is not more triangles. It comes from light (an environment map, tone mapping, fixed shadows, bloom on the status lamps), from shading the geometry we already have (smooth normals, baked ambient occlusion), and from shipping it smaller. Every number below was measured on our own `field.glb` with the lab in `blender/lab/`. Nothing here changes the production build.

## Where we start

- `blender/field.glb`: 263,085 triangles, 19.02 MB, 899 mesh nodes, 772 primitives, 32 materials, 22 images (9.43 MB of them).
- 691 of the 772 primitives have no UVs at all, so almost all equipment is flat color with no texture and nothing to bake onto yet.
- The equipment is flat shaded: pipes, elbows and bollards read as octagons up close.
- The console (`web/src/main.js`) renders with no environment map, no tone mapping, and a 2048 PCF soft shadow with no bias. The shadow has acne: concentric rings on every pad and fine stripes on the tank shells (see `blender/lab/shots/1_pad_before_console.png`).
- Metal materials (`M_STEEL` 0.35, `M_PIPE` 0.30 metallic) have nothing to reflect, so they read dull.

## How we measured

- `blender/lab/index.html` loads a glb, copies the console's night look (`indexModel`, the `paint()` lamp values, `frame()`), then switches on one upgrade at a time from URL parameters. `blender/lab/measure.mjs` runs it in a fresh headless Chrome (new, empty profile, `--use-angle=metal`), prints JSON, and saves a screenshot.
- GPU: Apple M5 through ANGLE Metal. 1600 x 900 canvas at pixel ratio 1. three.js 0.170.0 (the vendored core).
- Frame time is one render plus a 1 pixel `readPixels`, which waits for the GPU to finish. We report the median of 120 frames. An almost empty scene gives a floor of 2.8 ms. Run to run noise is about 1 to 2 ms.
- Blender 5.2.3 headless with `--factory-startup` for every Blender step.

### Frame time, whole field (overview camera, as the console opens)

| Setup | Median ms | p95 ms | Draw calls | Triangles drawn |
|---|---|---|---|---|
| Console today | 17.0 | 25.7 | 1,806 | 526k |
| + shadow bias fix | 16.8 | 19.5 | 1,806 | 526k |
| + IBL (PMREM of the CC0 moonlit HDRI) + Neutral tone mapping | 18.3 | 27.6 | 1,806 | 526k |
| + EffectComposer with OutputPass and SMAA | 20.0 | 32.2 | 1,810 | 526k |
| + UnrealBloomPass | 19.5 | 30.2 | 1,823 | 526k |
| + GTAOPass | 32.5 | 44.2 | 3,633 | 1.05M |
| pmndrs `postprocessing` 6.39.5: bloom + SMAA + tone map in one EffectPass | 21.4 | 27.2 | 1,825 | 526k |
| pmndrs + SSAO (NormalPass) | 28.8 | 43.7 | 2,730 | 789k |

Close-up of pad 1 (`view=pad`): console 10.4 ms, the light and post upgrade without GTAO 15.7 ms, with GTAO 25.4 ms.

What this says:
- **Draw calls are the budget.** 1,806 calls is 899 mesh draws plus 899 again for the shadow map. That alone costs about 14 ms over the 2.8 ms floor. Instancing and merging (perf) is what buys room for quality.
- **Light and post are cheap.** IBL, Neutral tone mapping, bloom and SMAA together cost about 3 ms.
- **Screen-space AO is not cheap here.** GTAO re-renders the whole scene for normals and depth: +13 ms and twice the draws. pmndrs SSAO is +7 ms. Baked AO costs nothing per frame.
- **pmndrs `postprocessing` was not faster for us** (21.4 vs 19.5 ms for the same bloom, SMAA and tone map), and it is a new dependency. Stay with three's own `EffectComposer`, which ships in `three@0.170.0`.
- First frame after load (shader compile) is 0.4 to 1.6 s today and 1.4 to 3.6 s with post. Use `renderer.compileAsync(scene, camera)` behind the loading screen.

## The methods

Each method: what it is, what we measured on our assets, what it needs, and who builds it.

### 1. Shadow acne fix (console)
- **What:** `moon.shadow.bias = -0.0005; moon.shadow.normalBias = 0.04;` Acne is the shadow map shadowing its own surface.
- **Measured:** 16.8 ms vs 17.0 ms (no cost), and p95 improved from 25.7 to 19.5 ms. The rings on the pads and the stripes on the tank shells are gone (`2_pad_after_light_post_flow.png`). Scale `normalBias` with the shadow box if `frame()` grows it for big fields.
- **Needs:** nothing. **Who:** 3D control panel (web/, Ronak).

### 2. Image-based light and tone mapping (console)
- **What:** `PMREMGenerator.fromEquirectangular()` on `blender/assets/hdri/rogland_moonlit_night_2k.hdr` (Poly Haven, CC0, already in the repo) as `scene.environment`, with `scene.environmentIntensity` about 0.3 for night, the hemisphere light down to 0.15, and `renderer.toneMapping = THREE.NeutralToneMapping`.
- **Measured:** +1.3 ms. PMREM takes 669 ms once, including fetching the 6.8 MB HDR. Steel and pipes now show their shape through reflections.
- **Tone mapper choice:** Khronos PBR Neutral keeps base color hue and saturation 1:1 up to a set brightness, which is what ISA-101 status colors need. AgX looks more filmic but desaturates bright colors, so an ALARM red drifts toward pink. ACES shifts hues. Use Neutral in the console; AgX is fine for hero renders.
- **Honest note:** at intensity 0.5 the tanks read close to daylight white, which works against the night look. Keep it near 0.3 and `toneMappingExposure` near 0.9, then tune by eye.
- **Needs:** `RGBELoader` (in three addons, no new npm dependency). The 1k version of the same HDRI would cut the download to about a quarter; `RoomEnvironment` costs zero bytes but has no night mood. **Who:** panel; cinema picks the final HDRI and intensity.

### 3. Bloom on status lamps, with HDR lamp values (console)
- **What:** `EffectComposer` (half float targets by default) with `RenderPass`, `UnrealBloomPass(strength 0.9, radius 0.35, threshold 1.2)`, `OutputPass` (applies tone mapping once) and `SMAAPass` last. Push only abnormal lamps above the threshold: in `paint()`, multiply ALARM and WARNING lamp emissive by about 2.5 (ALARM 7.5, WARNING 3.5) and keep OK at 1.4 so it never glows.
- **Measured:** composer + SMAA + OutputPass +1.7 ms; bloom on top is inside the noise. With the old threshold of 0.9 and IBL on, bright metal specular bloomed too (the first try washed the whole field out), so the threshold and lamp values must be set together.
- **Needs:** addons only, no new npm dependency. Turn off `antialias` on the renderer when SMAA is on. **Who:** panel.

### 4. Baked ambient occlusion on a second UV (Blender pipeline)
- **What:** `blender/lab/bake_ao.py` takes every static mesh within 30 m of `TANK_1` (no site, scenery or moving parts), gives each a placeholder `UVMap` (TEXCOORD_0, the slot lookdev's tiling UVs will fill) and a unique `AO_UV` (TEXCOORD_1), unwraps them into one shared atlas (Smart UV Project, average island scale, pack), bakes Cycles AO (64 samples, 1.5 m rays) into a 2048 px atlas, and wires it into a `glTF Material Output` group's Occlusion socket. The exporter writes `occlusionTexture.texCoord = 1`, and three.js 0.170's `GLTFLoader` sets `aoMap.channel = 1` from it with no console code (GLTFLoader.js line 3356).
- **Measured:** 52 objects and 11,612 triangles (8 already-textured objects skipped). Unwrap 3.5 s. Bake 718 s on CPU at 2048 px and 64 samples, sharing the CPU with five other Blender jobs. The glb grew 19.02 to 20.28 MB (+1.26 MB: a 779 KB JPEG atlas plus two UV sets). All 52 meshes loaded with `aoMap` on channel 1 and no console code. Frame time: tank close-up 11.3 to 11.9 ms, overview 19.7 to 20.7 ms, both inside the noise.
- **What we saw:** contact darkening where the shell meets its base ring, under the pipe elbow on the shell, and under the girth rings (`5_tank_before_no_ao.png` vs `6_tank_after_baked_ao.png`). It is subtle, because AO only darkens indirect light and the moon is the key light. It matters most on the shadowed sides and once textures are in.
- **Make it cheaper:** bake once per template (one pumpjack, one pump skid, one tank) instead of per pad, at 1024 px and 32 samples, and reuse it on every copy, which also suits instancing. That is about one eighth of the pixels times samples of this test.
- **Needs:** nothing in the console. AO only shows once there is indirect light, so ship it with method 2.
- **Who:** lookdev owns UV1 and the material wiring in `look.py`; cinema owns the bake settings.
- **Gotcha:** the same bake on the GPU sat for 26 minutes in Metal kernel compilation (`MTLCompilerService`) without rendering a sample, and we stopped it. Bake on CPU tonight, or compile the Metal kernels once well before the demo.

### 5. Screen-space AO (GTAO) (console, close-ups only)
- **What:** `GTAOPass` in the composer.
- **Measured:** +13 ms on the overview, +10 ms on the pad close-up. It does add contact darkening between parts, but not enough to justify one third of the frame. Use baked AO for static parts; if a hero close-up wants GTAO, enable it only while the camera is close, at half resolution.
- **Who:** panel, optional.

### 6. Smooth normals, then bevels (Blender pipeline)
- **What:** `blender/lab/geometry_lab.py --mode bevel`. Three variants on the 50 to 55 hard-surface objects around `TANK_1`:

| Variant | Triangles in the region | glb | Pump close-up |
|---|---|---|---|
| Today (flat) | 11,980 | 19.02 MB | 11.4 ms |
| Smooth by angle 50 degrees + Weighted Normal, no bevel | 11,980 (+0) | 18.95 MB | 8.6 ms |
| Bevel 1 segment, parts of 0.5 m or more, harden normals + Weighted Normal | 34,592 (2.9x) | 19.63 MB | 12.9 ms |
| Bevel 2 segments, all parts | 76,180 (6.4x) | 20.38 MB | not rendered |

- **What we saw:** the free variant is the win. Pipes, elbows and bollards go from octagons to round (`3_pump_before_flat_normals.png` vs `4_pump_after_smooth_normals.png`), boxes keep crisp edges, and the file gets slightly smaller because smooth vertices share. A 30 degree angle did nothing, because our 8-sided pipes meet at 45 degrees; 50 degrees catches them. Real bevels look good but would cost 6 pads x 23k = about 136k triangles, which the 300k budget cannot take. If a hero part needs a bevel, use 1 segment on its silhouette edges only.
- **Who:** hero (equipment geometry). Run smooth by angle and Weighted Normal at build time; raise pipes 4 inches and up to 12 radial segments.

### 7. Compression: Draco and WebP (Blender pipeline + console)
- **What:** Blender's exporter can write `KHR_draco_mesh_compression` and `EXT_texture_webp` with no extra tool (`geometry_lab.py --mode compress`).
- **Measured:**

| Export | glb | Load (parse + decode) in Chrome |
|---|---|---|
| Today | 19.02 MB | 458 ms |
| WebP textures only | 12.93 MB | not measured |
| Draco only (position 14 bit, normal 10, UV 12) | 11.33 MB | not measured |
| Draco + WebP | **5.24 MB (-72%)** | 1,163 ms |

- The extra 0.7 s of decode is paid once; the 13.8 MB saved is about 2 s on 50 Mbit venue Wi-Fi. Frame time does not change (18.8 vs 19.0 ms).
- **Meshopt** (`EXT_meshopt_compression`) usually decodes faster than Draco and needs only `meshopt_decoder.module.js` (no wasm files to host), but Blender cannot write it. It needs `gltfpack` or `gltf-transform`, which this lab did not install. Worth a later test.
- **KTX2 / Basis** cuts GPU memory, because JPEG and WebP still decode to full RGBA in VRAM. Encoding needs KTX-Software (`toktx`) or `gltf-transform`, so it is not proven here. The console would add `KTX2Loader`, `setTranscoderPath()` pointing at `basis_transcoder.js` and `.wasm` served from `web/public/basis/`, and `detectSupport(renderer)` before loading. ETC1S for color and roughness, UASTC for normal maps.
- **Needs in the console:** `DRACOLoader` with `setDecoderPath("/draco/")` and the files from `node_modules/three/examples/jsm/libs/draco/gltf/` copied to `web/public/draco/` (about 300 KB). WebP needs nothing.
- **Who:** perf adds the export option in `optimize.py` behind a flag; panel adds `DRACOLoader`. Ship both in the same merge, or the console cannot open the file.

### 8. Flowing pipes (console)
- **What:** an `onBeforeCompile` patch on each `PIPE_*` material adds a cyan stripe that scrolls along the pipe's long axis in world space, so it needs no UVs (in `index.html`, `addFlow()`).
- **Measured:** included in the pad close-up upgrade (15.7 ms); no separate cost showed. Visible in `2_pad_after_light_post_flow.png`.
- **Who:** panel. Drive `uTime` only for pipes whose pump is running, and stop it on a FAILED pump: flow is information, not decoration.

## Research: what else moves quality

**Baked lighting.** Cycles bakes AO, full lightmaps, curvature and edge wear. glTF has a slot for AO (`occlusionTexture`) but none for a lightmap, so a lightmap travels in the emissive slot or as a separate texture assigned to `material.lightMap` after load. A full lightmap freezes the lighting, which suits the fixed moon but not a camera-relative light. Vertex-color AO needs no UVs, but our boxes have 8 to 24 vertices each, so it would smear. Curvature and edge wear baked into the base color or roughness is the cheap way to make edges read worn without geometry.

**PBR texturing at scale.** Trim sheets (one texture, many parts mapped to strips), a tiling detail texture plus a low-frequency macro variation to break repeats, decals for labels, stains and rust, atlases to share one material, ORM packing (occlusion, roughness, metal in one image). Budget: 1k for props, 2k for ground, one 2k trim sheet for all steel. The biggest single image today is the ground normal map at 2.7 MB.

**Geometry.** Smooth normals first (free), bevels only on silhouettes, LODs for the overview (the console starts zoomed out), and `EXT_mesh_gpu_instancing` for repeated wells, pumps and plants, which `GLTFLoader` turns into `InstancedMesh`.

**Lighting and post.** Three.js 0.170 ships, with no new npm dependency: PMREM, the `RGBELoader`, the `GTAOPass`, `SAOPass`, `SSAOPass`, `UnrealBloomPass`, `SMAAPass`, `TAARenderPass`, `OutputPass`, `LUTPass` for color grading, the `Sky` addon, and `CSM` for cascaded shadows (its docs warn it renders the geometry once per cascade, which at our draw count is expensive). Tone mapping options are Neutral, AgX and ACES. Fog: `scene.fog` today; height fog needs a small shader patch. New dependencies would be pmndrs `postprocessing` (no gain measured) and `n8ao` (not tested).

**Animation.** The contract already gives code-driven moving parts (pivots and one property each), which is the right call for live status. Baked glTF clips suit cosmetic loops; morph targets suit a tank roof or a gauge face; particles (instanced sprites) suit flare flame, heater-treater steam and road dust; canvas textures suit live gauge faces; camera moves should ease (smoothstep or damped springs) rather than lerp linearly.

**AI-assisted assets.** TRELLIS.2 now runs on Apple Silicon through a community MPS port: at least 24 GB of unified memory, about 3.5 minutes per mesh on an M4 Pro, vertex colors only, and meshes can have holes. Hunyuan3D 2.x runs on a Mac through ComfyUI with patches; its community license excludes the EU, the UK and South Korea. Meshy and Rodin are cloud services with credits, and their free-tier commercial terms conflict between sources, so check the vendor's own page first. Verdict: not for contract assets. Our equipment needs named parts, pivots at the joints, and clean low-poly shapes, which these tools do not produce. At most, use them for a one-off scenery prop, and check the hackathon's rules on AI-generated content first. Poly Haven's CC0 models and HDRIs (already our source) carry no such risk.

**What the best industrial twins do.** Cognite Reveal is a three.js WebGL2 viewer for CAD and point clouds that caps render resolution to keep frame rate. AVEVA's high-end twin renders on RTX GPUs and streams to the browser over Omniverse WebRTC, and NVIDIA's own browser path for Omniverse is server-side streaming (Kit App Streaming), not client WebGL. So the photoreal twins are video streams from a GPU server. In a browser at 60 fps, the realistic target is ours: clean PBR, an environment map, baked AO, tone mapping, and bloom on what matters, inside about 300k triangles and a few hundred draw calls.

## Ranked plan for tonight

Ranked by quality gained per hour and per millisecond. Numbers are from the tables above.

**Blender pipeline (`blender/`, Sriyan's agents)**
1. **Smooth by angle 50 degrees + Weighted Normal on all hard-surface parts** (hero). +0 triangles, -0.07 MB, pipes and bollards turn round.
2. **Bake AO once per template onto `AO_UV` (TEXCOORD_1) and wire it to glTF occlusion** (lookdev for UVs and materials, cinema for bake settings). Frame time within noise, +1.26 MB for one pad at 2k, and the console needs no code. Bake on CPU at 1k and 32 samples.
3. **Export with Draco + WebP behind a flag** (perf, in `optimize.py`, shipped with the console's `DRACOLoader`). 19.02 to 5.24 MB.
4. **Texture budget: trim sheet + macro variation, ORM packing, ground normal map to 1k or WebP** (lookdev, terrain). Cuts the 2.7 MB ground normal and makes UV-less steel look like steel.

**Console (`web/`, 3D control panel and Ronak)**. Vinny's Jarvis console direction (board, 2026-11-08) asks for no big `main.js` layout changes. Items 5 to 8 touch only the renderer setup, `paint()` and the render call, so they fit as small hooks or one new file (for example `web/src/look.js`), and they keep the rule of color only for state.
5. **Shadow bias fix** (2 lines). 0 ms, removes acne everywhere.
6. **IBL from the moonlit HDRI + `NeutralToneMapping`** at intensity about 0.3. +1.3 ms.
7. **EffectComposer: RenderPass, UnrealBloomPass (threshold 1.2), OutputPass, SMAAPass, with ALARM and WARNING lamps pushed to HDR**. +1.7 ms, status lamps glow and nothing else does.
8. **Flowing pipes tied to pump state**, plus `compileAsync` behind the loading screen. About 0 ms.

Not tonight: GTAO on the overview (+13 ms), real bevels everywhere (+136k triangles), AI-generated equipment, KTX2 until someone can run `toktx`.

## Reproduce

```bash
# from the repo root
python3 -m http.server 8765 &
B=/Applications/Blender.app/Contents/MacOS/Blender
$B --background --factory-startup --python blender/lab/bake_ao.py -- --blend blender/field.blend --out blender/out/lab --device CPU
$B --background --factory-startup --python blender/lab/geometry_lab.py -- --blend blender/field.blend --out blender/out/lab --mode compress
$B --background --factory-startup --python blender/lab/geometry_lab.py -- --blend blender/field.blend --out blender/out/lab --mode bevel --segments 0 --angle 50
mkdir -p /tmp/lab-profile   # a new, empty folder: never a real Chrome profile
node blender/lab/measure.mjs /tmp/lab-profile /tmp/lab "http://127.0.0.1:8765/blender/lab/?look=console" \
  "http://127.0.0.1:8765/blender/lab/?look=ibl&tone=neutral&shadowfix=1&post=three&ao=0&lampx=2.5&thr=1.2&envi=0.3&flow=1"
```

Outputs land in `blender/out/lab/`, which git ignores. The page loads the post-processing addons and the Draco decoder from jsDelivr (`three@0.170.0`), so it needs network.

## Sources

- three.js tone mapping overview: https://discourse.threejs.org/t/tone-mapping-overview/75204
- Khronos PBR Neutral announcement: https://gamedev.net/news/khronos-pbr-neutral-tone-mapper-released-for-true-to-life-color-rendering-of-3d-objects-r2733
- Blender PBR Neutral proposal (names in three.js and Filament): https://projects.blender.org/blender/blender/issues/118824
- texture.channel and second UVs in three.js: https://discourse.threejs.org/t/documentation-what-exactly-is-texture-channel-for/81168 and https://discourse.threejs.org/t/lightmap-uses-wrong-uv-in-loaded-gltf-model/57434
- glTF carries AO but not lightmaps: https://discourse.threejs.org/t/ambient-oclusion-vs-lightmaps/31333
- Baking AO and lightmaps in Blender for three.js: https://www.educative.io/courses/learn-threejs-for-computer-graphics/blender-to-bake-lightmaps-and-ambient-occlusion
- KTX2Loader docs: https://threejs.org/docs/pages/KTX2Loader.html and the Basis transcoder README: https://cdn.jsdelivr.net/npm/three@0.184.0/examples/jsm/libs/basis/README.md
- Web texture formats (ETC1S vs UASTC, VRAM): https://www.donmccurdy.com/2024/02/11/web-texture-formats/
- Meshopt vs Draco: https://gltf-transform.dev/modules/extensions/classes/EXTMeshoptCompression and https://svilenkovic.com/3d/draco-vs-meshopt
- pmndrs postprocessing (EffectPass merging, renderer settings): https://github.com/pmndrs/postprocessing
- Post-processing performance reports: https://forum.needle.tools/t/low-performance-when-using-post-processing-in-comparison-to-vanilla-threejs/2340 and https://discourse.threejs.org/t/new-ambient-occlusion-example-hbao-vs-n8ao/58847
- three.js CSM docs: https://threejs.org/docs/pages/CSM.html and the cascade cost discussion: https://discourse.threejs.org/t/cascaded-shadow-maps/7242
- onBeforeCompile shader patching: https://tympanus.net/codrops/2021/08/02/magical-marbles-in-three-js/ and TSL: https://github.com/mrdoob/three.js/wiki/Three.js-Shading-Language
- TRELLIS.2 on Apple Silicon: https://lilting.ch/articles/trellis2-apple-silicon-mps-cuda-free and https://news.lavx.hu/article/trellis-2-now-runs-natively-on-apple-silicon-microsoft-s-state-of-the-art-image-to-3d-model-ported-to-mac
- Hunyuan3D 2 license: https://huggingface.co/tencent/Hunyuan3D-2/blob/main/LICENSE; on a Mac through ComfyUI: https://zenn.dev/renue/articles/144cd47fbe9d7a
- AI 3D licensing (Meshy, Rodin, Tripo): https://www.tripo3d.ai/blog/commercial-use-ai-3d-models
- Poly Haven CC0 assets: https://polyhaven.com/a/industrial_pipe_and_valve_01
- Cognite Reveal: https://cognitedata.github.io/reveal-docs/docs and https://cognitedata.github.io/reveal-docs/docs/3.x/examples/pointcloud
- AVEVA twin streamed through Omniverse: https://www.nvidia.com/en-au/case-studies/aveva/
- Omniverse browser streaming: https://docs.nvidia.com/omniverse/index.html and https://www.nvidia.com/en-in/data-center/omniverse-dgx-cloud
