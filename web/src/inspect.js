// Picking, hover tooltips, keyboard and the highlight sources the console owns (#86).
//
// - Every mesh resolves to its asset and its part (parts.js). Hover outlines the asset and
//   gives the part a stronger highlight. The tooltip says what it is, its ID, its status and
//   the live number for that part: "Tank 3 level gauge: 71%, ALARM".
// - Small things are easy to hit: an asset under 28 px on screen is hit anywhere in a 28 px
//   box around it, and a part under 14 px anywhere in a 14 px box.
// - Click selects (main.js select(), so Jarvis stays in sync). Selection lightly outlines what
//   the asset connects to in /api/field and dims the rest. Escape clears, Tab and Shift+Tab
//   cycle through assets, double-click frames one. Scenery, ground and roads are never picked.
// - Sources driven here: hover, part, selection, related, trying, alarm, jarvis, phone.
//
// Test hooks: window.__freeboardHL (see the bottom of this file).

import * as THREE from "three";
import { highlight, bindHighlight, applyHighlight, drawOutlines, outlineCoverage } from "./highlight.js";
import { buildIndex, describe, linkGraph, relatedTo, nice, NEVER } from "./parts.js";
import * as liveFeed from "./live.js";

const SMALL_ASSET_PX = 28, SMALL_PART_PX = 14;
// Parts that are a thing in their own right, not a material group like "steel" or "concrete".
// LEVEL (the brine inside the shell) is left out on purpose: it's named when the ray hits it
// through the cutaway, but it never takes the hover from the shell in front of it.
const FEATURE = /^(wheel|light|beacon|gauge|lt|lshh|rotor|beam|crank|gate|lamps|headlights|axle_\d+|loadout|sensor|rtu)$/;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// A /ws `ui` focus (a phone's Show) reaches main.js through jarvis.js's focus hook. This module
// subscribes first, at import time, so pointed() can tell it from Jarvis's own reply.
let socketFocus = { id: null, t: -1e9 };
liveFeed.on("ui", (msg) => { if (msg && msg.action === "focus") socketFocus = { id: msg.asset_id, t: performance.now() }; });

// hooks: { scene, camera, renderer, canvas, controls, tag, assets, ids(), idFor, view(), field(),
//          selected(), select(id, focus), actions(), equipment(id) -> {name, label}|null,
//          fuse(id) -> "2:15 AM"|null, staleMin(id) }
export function mountInspect(h) {
  const { scene, camera, canvas, controls, tag } = h;
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2(), v3 = new THREE.Vector3(), tmpBox = new THREE.Box3();
  let index = null, pickables = [], byName = new Map(), graph = new Map(), order = [];
  const boxes = new Map();
  let hov = null, ptr = null, down = null, lastTag = 0, tween = null;
  let keys = { sel: undefined, rel: "", trying: "", alarm: "" };

  // ------------------------------------------------------------ index ----
  function ready(root) {
    index = buildIndex(root, { ids: h.ids(), idFor: h.idFor, assetRoot: (id) => (h.assets.get(id) || {}).root || null });
    pickables = index.pickable();
    byName = new Map();
    root.traverse((o) => { if (o.name && !byName.has(o.name)) byName.set(o.name, o); });
    graph = linkGraph(h.field() || {});
    order = [...h.ids()].filter((id) => index.meshes(id).length);
    boxes.clear();
    const dimmable = pickables.filter((m) => m.userData.base);
    bindHighlight({ renderer: h.renderer, scene, camera, resolve, dimmable: () => dimmable });
  }
  const blocked = (node) => { for (let o = node; o; o = o.parent) if (NEVER.test(o.name) || (o.userData && o.userData.scenery)) return true; return false; };
  function meshesUnder(node) {
    if (blocked(node)) return [];
    const out = [];
    node.traverse((o) => { if (o.isMesh && !NEVER.test(o.name)) out.push(o); });
    return out;
  }
  function resolve(x) {
    if (!index || x == null) return [];
    if (x.isObject3D) return meshesUnder(x);
    const own = index.meshes(String(x));
    if (own.length) return own;
    const node = byName.get(String(x));
    return node ? meshesUnder(node) : [];
  }
  function boxOf(id, fresh = false) {
    if (fresh || /^TRUCK_/.test(id) || !boxes.has(id)) {
      const b = new THREE.Box3();
      for (const m of index.meshes(id)) if (m.visible) b.expandByObject(m);
      boxes.set(id, b);
    }
    return boxes.get(id);
  }

  // ---------------------------------------------------------- picking ----
  function rayAt(x, y) {
    const r = canvas.getBoundingClientRect();
    ndc.set(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    return r;
  }
  // The client-pixel rectangle a world box covers, or null if any of it is behind the camera.
  function rectOf(box, r) {
    if (box.isEmpty()) return null;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let i = 0; i < 8; i++) {
      v3.set(i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z).project(camera);
      if (v3.z > 1 || v3.z < -1) return null;
      const px = r.left + ((v3.x + 1) / 2) * r.width, py = r.top + ((1 - v3.y) / 2) * r.height;
      x0 = Math.min(x0, px); x1 = Math.max(x1, px); y0 = Math.min(y0, py); y1 = Math.max(y1, py);
    }
    return { x0, y0, x1, y1, w: x1 - x0, h: y1 - y0 };
  }
  const inside = (x, y, q, pad) => x >= q.x0 - pad && x <= q.x1 + pad && y >= q.y0 - pad && y <= q.y1 + pad;

  // What's under a screen point: { id, part: { node, key } | null } or null.
  function pickAt(x, y) {
    if (!index) return null;
    const r = rayAt(x, y);
    let hit = null;
    for (const it of ray.intersectObjects(pickables, false)) {
      if (!it.object.visible) continue;
      const info = index.info(it.object);
      if (info) { hit = { ...info, dist: it.distance }; break; }
    }
    // The ray is right on a wheel, light or gauge: that's the answer.
    if (hit && FEATURE.test(hit.key)) return { id: hit.id, part: { node: hit.part, key: hit.key } };
    // Small assets: their screen box (grown to SMALL_ASSET_PX) is the target. With a precise hit
    // on something else, a small asset still wins when the pointer is right on it and in front.
    const cands = [];
    for (const id of order) {
      const box = boxOf(id), q = rectOf(box, r);
      if (!q) continue;
      const big = Math.max(q.w, q.h);
      if (big >= SMALL_ASSET_PX || (hit && hit.id === id)) continue;
      if (!inside(x, y, q, hit ? 0 : (SMALL_ASSET_PX - big) / 2 + 2)) continue;
      if (hit && camera.position.distanceTo(box.getCenter(v3)) >= hit.dist) continue;
      cands.push(id);
    }
    if (hit && !cands.length) return { id: hit.id, part: partAt(hit.id, x, y, r, hit).part };
    // Otherwise the asset with a part closest to the pointer wins (ties go to the smaller part):
    // a pump light beside a valve picks the pump, because the light is right under the pointer.
    let best = null;
    for (const id of cands) {
      const s = nearest(id, x, y, r);
      if (s != null && (!best || s < best.s)) best = { id, s };
    }
    if (!best) return hit ? { id: hit.id, part: partAt(hit.id, x, y, r, hit).part } : null;
    const p = partAt(best.id, x, y, r, null, true);
    return { id: best.id, part: p ? p.part : null };
  }
  function nearest(id, x, y, r) {
    let s = null;
    for (const p of index.parts(id)) {
      tmpBox.makeEmpty();
      for (const m of p.meshes) if (m.visible) tmpBox.expandByObject(m);
      const q = rectOf(tmpBox, r);
      if (!q) continue;
      const v = Math.hypot(Math.max(q.x0 - x, 0, x - q.x1), Math.max(q.y0 - y, 0, y - q.y1)) + 1e-4 * (q.w + 1) * (q.h + 1);
      if (s == null || v < s) s = v;
    }
    return s;
  }
  // The part under the pointer. A feature (wheel, light, gauge...) the ray hit exactly wins.
  // Otherwise the closest feature: a small one anywhere in its grown box, a big one only with the
  // pointer inside it. The score is pixels from the pointer to the part's box, plus 4 px for a
  // plain part like "steel" (only scored with `any`, when nothing was hit exactly).
  function partAt(id, x, y, r, hit, any = false) {
    const exact = hit ? { node: hit.part, key: hit.key } : null;
    if (exact && FEATURE.test(exact.key)) return { part: exact, score: 0 };
    let best = null;
    for (const p of index.parts(id)) {
      const feature = FEATURE.test(p.key);
      if ((!feature && !any) || (exact && p.node === exact.node)) continue;
      tmpBox.makeEmpty();
      for (const m of p.meshes) if (m.visible) tmpBox.expandByObject(m);
      const q = rectOf(tmpBox, r);
      if (!q) continue;
      const big = Math.max(q.w, q.h);
      const d = Math.hypot(Math.max(q.x0 - x, 0, x - q.x1), Math.max(q.y0 - y, 0, y - q.y1));
      if (d > Math.max(any ? (SMALL_ASSET_PX - big) / 2 + 2 : 0, (SMALL_PART_PX - big) / 2)) continue;
      const score = d + (feature ? 0 : 4) + 0.01 * Math.hypot(x - (q.x0 + q.x1) / 2, y - (q.y0 + q.y1) / 2);
      if (!best || score < best.score) best = { part: { node: p.node, key: p.key }, score };
    }
    return best || (exact ? { part: exact, score: 0 } : null);
  }

  // ------------------------------------------------------------ hover ----
  function hover(x, y) {
    if (x == null) { ptr = null; show(null); return null; }
    ptr = { x, y };
    const dragging = down && Math.hypot(x - down.x, y - down.y) > 5;
    const p = dragging ? null : pickAt(x, y);
    show(p);
    return p ? p.id : null;
  }
  function show(p) {
    canvas.classList.toggle("pointing", !!p);
    if (!p) {
      if (hov) { highlight.clear("hover"); highlight.clear("part"); }
      hov = null;
      tag.hidden = true;
      return;
    }
    if (!hov || hov.id !== p.id) highlight.set("hover", [p.id]);
    if (!p.part) highlight.clear("part");
    else if (!hov || !hov.part || hov.part.node !== p.part.node) highlight.set("part", [p.part.node]);
    hov = p;
    renderTag();
  }
  function renderTag() {
    if (!hov || !ptr) return;
    const id = hov.id, view = (h.view() || {})[id] || {};
    const act = (h.actions() || []).find((a) => a.asset_id === id);
    const d = describe(id, hov.part && hov.part.key, { view, field: h.field(), equipment: h.equipment, staleMin: h.staleMin(id), trying: act ? "what-if on" : "" });
    const notes = [...highlight.notes(id)];
    const fuse = h.fuse(id);
    if (fuse) notes.unshift(`Can help until ${fuse}.`);
    tag.className = "tag rich";
    tag.innerHTML = `<b>${esc(d.text)}</b><span class="tag-id">${esc(id)}${hov.part && hov.part.node.name !== id ? ` · ${esc(hov.part.node.name)}` : ""}</span>`
      + (d.what ? `<span class="tag-what">${esc(d.what)}</span>` : "") + notes.map((n) => `<span class="tag-note">${esc(n)}</span>`).join("");
    const r = canvas.getBoundingClientRect();
    const flip = ptr.x - r.left > r.width - 340;
    tag.classList.toggle("flip", flip);
    tag.style.left = `${ptr.x - r.left}px`;
    tag.style.top = `${ptr.y - r.top}px`;
    tag.hidden = false;
    lastTag = performance.now();
  }
  canvas.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY }; tween = null; });
  addEventListener("pointerup", () => { down = null; });
  canvas.addEventListener("pointerleave", () => { if (!down) hover(null); });
  canvas.addEventListener("wheel", () => { tween = null; }, { passive: true });

  // -------------------------------------------------- keyboard, framing ----
  canvas.addEventListener("dblclick", (e) => {
    const p = pickAt(e.clientX, e.clientY);
    if (!p) return;
    if (h.selected() !== p.id) h.select(p.id);
    frame(p.id);
  });
  function frame(id) {
    const box = boxOf(id, true);
    if (box.isEmpty()) return;
    const s = box.getBoundingSphere(new THREE.Sphere());
    const dist = Math.max(controls.minDistance * 1.05, (s.radius / Math.sin(THREE.MathUtils.degToRad(camera.fov) / 2)) * 1.6);
    const dir = camera.position.clone().sub(controls.target).normalize();
    tween = { t0: performance.now(), dur: 700, fromT: controls.target.clone(), toT: s.center.clone(), fromP: camera.position.clone(), toP: s.center.clone().addScaledVector(dir, dist) };
  }
  addEventListener("keydown", (e) => {
    const el = document.activeElement;
    const free = !el || el === document.body || el === canvas;
    if (e.key === "Escape") { highlight.clear("jarvis"); highlight.clear("phone"); tween = null; return; }
    if (e.key !== "Tab" || !free || !order.length || e.altKey || e.ctrlKey || e.metaKey) return;
    e.preventDefault();
    const i = order.indexOf(h.selected());
    const next = order[(i < 0 ? (e.shiftKey ? order.length - 1 : 0) : i + (e.shiftKey ? -1 : 1) + order.length) % order.length];
    h.select(next, true);
  });

  // Jarvis or a phone pointed the console at an asset (main.js's Jarvis focus hook).
  function pointed(id, from) {
    const src = from || (socketFocus.id === id && performance.now() - socketFocus.t < 400 ? "phone" : "jarvis");
    highlight.clear(src === "phone" ? "jarvis" : "phone");
    highlight.flash(src, [id], 8000);
  }

  // ------------------------------------------------------------ frame ----
  // Built-in sources follow the console's state. Each is only re-sent when it changes.
  function sync() {
    const sel = h.selected() || null;
    const acts = h.actions() || [];
    const tryKey = acts.map((a) => `${a.asset_id}>${a.target_id || ""}`).join("|");
    if (sel !== keys.sel || tryKey !== keys.trying) {
      if (sel) highlight.set("selection", [sel]); else highlight.clear("selection");
      const rel = sel ? relatedTo(graph, sel) : new Set();
      for (const a of acts) if (a.type === "SEND_TRUCK" && (a.asset_id === sel || a.target_id === sel)) rel.add(a.asset_id === sel ? a.target_id : a.asset_id);
      rel.delete(sel);
      const pipes = sel ? index.pipes.filter((p) => p.ids.size && [...p.ids].every((x) => x === sel || rel.has(x))
        && [...p.ids].some((x) => x === sel || (/^VALVE_/.test(x) && (graph.get(sel) || new Set()).has(x)))).map((p) => p.node) : [];
      if (rel.size || pipes.length) highlight.set("related", [...rel, ...pipes]); else highlight.clear("related");
      const ids = acts.map((a) => a.asset_id);
      if (ids.length) highlight.set("trying", ids); else highlight.clear("trying");
      keys = { ...keys, sel, trying: tryKey };
    }
    const view = h.view() || {};
    const alarm = order.filter((id) => (view[id] || {}).status === "ALARM").join(",");
    if (alarm !== keys.alarm) { keys.alarm = alarm; if (alarm) highlight.set("alarm", alarm.split(",")); else highlight.clear("alarm"); }
  }
  // CPU time spent here per frame, for the frame-time checks. `off` skips everything (A/B tests).
  const perf = { update: 0, overlay: 0, frames: 0, off: false, cover: 0 };
  function update() {
    if (!index || perf.off) return;
    const t0 = performance.now();
    if (tween) {
      const k = Math.min(1, (performance.now() - tween.t0) / tween.dur), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      controls.target.lerpVectors(tween.fromT, tween.toT, e);
      camera.position.lerpVectors(tween.fromP, tween.toP, e);
      if (k === 1) tween = null;
    }
    sync();
    // Keep the tooltip true while the camera or the slider moves under a still pointer.
    if (ptr && performance.now() - lastTag > 200) { if (!down) hover(ptr.x, ptr.y); else renderTag(); }
    applyHighlight();
    perf.update += performance.now() - t0;
    perf.frames++;
  }
  function overlay(t) {
    if (perf.off) return;
    const t0 = performance.now();
    drawOutlines(t);
    perf.overlay += performance.now() - t0;
    perf.cover = outlineCoverage();
  }

  // Test hooks. project(name) gives the client point at the center of a node or asset.
  window.__freeboardHL = {
    highlight, pick: (x, y) => { const p = pickAt(x, y); return p && { id: p.id, part: p.part && p.part.node.name, key: p.part && p.part.key }; },
    hover, frame, pointed, tooltip: () => (tag.hidden ? null : tag.textContent),
    project(name) {
      const r = canvas.getBoundingClientRect();
      const b = index.meshes(name).length ? boxOf(name, true) : new THREE.Box3().setFromObject(byName.get(name) || scene.getObjectByName(name));
      const c = b.getCenter(new THREE.Vector3()).project(camera);
      return { x: r.left + ((c.x + 1) / 2) * r.width, y: r.top + ((1 - c.y) / 2) * r.height };
    },
    parts: (id) => index.parts(id).map((p) => ({ name: p.node.name, key: p.key, feature: FEATURE.test(p.key) })),
    // Every mesh in the model: indexed to an asset, a pipe, or deliberately never pickable.
    coverage() {
      const out = { indexed: 0, pipe: 0, never: 0, other: [] };
      scene.traverse((o) => {
        if (!o.isMesh || o.parent === scene) return;
        if (index.info(o)) out.indexed++;
        else if (index.pipes.some((p) => { for (let n = o; n; n = n.parent) if (n === p.node) return true; return false; })) out.pipe++;
        else if (blocked(o)) out.never++;
        else out.other.push(o.name);
      });
      return out;
    },
    rect: (name) => rectOf(index.meshes(name).length ? boxOf(name, true) : new THREE.Box3().setFromObject(byName.get(name)), canvas.getBoundingClientRect()),
    state: () => ({ sources: Object.fromEntries(highlight.sources().map((s) => [s, highlight.get(s)])), hover: hov && { id: hov.id, part: hov.part && hov.part.node.name }, pointing: canvas.classList.contains("pointing") }),
    winner: (name) => { const n = byName.get(name); const m = n && (n.isMesh ? n : resolve(n)[0]); return m ? highlight.winner(m) : null; },
    color: (name) => { const n = byName.get(name); return n && n.material ? n.material.color.toArray() : null; },
    order: () => order.slice(),
    perf,
  };
  return { ready, pick: (x, y) => { const p = pickAt(x, y); return p ? p.id : null; }, hover, pointed, update, overlay, nice };
}
