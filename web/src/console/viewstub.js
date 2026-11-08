// The `view` API (spec §6, the interface between Track B and Track C), stubbed with focus and
// highlight until web/src/console3d/view.js lands. Every call returns { ok, said } where `said`
// is the plain sentence Jarvis posts. When Track C ships, index.js uses window.__console3d.view
// (or hooks.view) instead and this file goes away.
//
//   focus(id) highlight(ids, seconds) explode(id) collapse() isolate(id)
//   showSensors(id | "ALL") tracePipes(id, direction) reset() state()

import { highlight } from "../highlight.js";
import * as liveFeed from "../live.js";

const VIEW_SOURCES = ["jarvis", "phone", "focus", "sensors", "trace", "open"];

export function makeViewStub(H) {
  const nice = (id) => H.nice(id);
  const has = (name) => !!(H.scene && H.scene.getObjectByName(name));
  let opened = null, isolated = null;

  const ago = (id) => {
    const m = liveFeed.staleMinutes(id);
    return m == null ? "" : m < 1 ? "under a minute ago" : `${m} min ago`;
  };
  function sensorNodes(id) {
    const out = [`SENSOR_${id}`, `${id}_lt`, `${id}_lshh`].filter(has);
    const tank = /^TANK_/.test(id) ? id : (H.tankOf && H.tankOf(id)) || null;
    const pad = tank ? `RTU_PAD_${tank.split("_").pop()}` : null;
    if (pad && has(pad)) out.push(pad);
    return out;
  }
  function readingLine(id) {
    const r = liveFeed.reading(id);
    if (!r) return `${nice(id)}: no live reading.`;
    if (r.status === "UNKNOWN") return `${nice(id)}: no reading ${ago(id) ? `since ${ago(id)}` : "now"}. UNKNOWN, not safe.`;
    const bits = [];
    if (r.level_pct != null) bits.push(`level ${Number(r.level_pct).toFixed(1)}%`);
    if (r.running != null) bits.push(r.running ? "running" : "stopped");
    if (r.position) bits.push(String(r.position).toLowerCase());
    if (r.flow_bbl_hr != null) bits.push(`${Math.round(r.flow_bbl_hr)} bbl/hr`);
    const when = ago(id);
    return `${nice(id)}: ${bits.join(", ") || r.status}${when ? `, ${when}` : ""}.`;
  }

  return {
    stub: true,
    focus(id) {
      if (!H.hasId(id)) return { ok: false, said: `I don't know ${id}.` };
      H.focus(id);
      return { ok: true, said: "" };
    },
    highlight(ids, seconds = 20) {
      const list = (ids || []).slice(0, 24);
      if (list.length) highlight.flash("jarvis", list, Math.max(1, seconds) * 1000);
      return { ok: true, said: "" };
    },
    explode(id) {
      if (!H.hasId(id)) return { ok: false, said: `I don't know ${id}.` };
      opened = id;
      H.focus(id);
      return { ok: true, said: `${nice(id)}. The parts breakdown comes with the 3D update; for now I've flown to it.` };
    },
    collapse() {
      const was = opened;
      opened = null;
      highlight.clear("open");
      return { ok: true, said: was ? `Put ${nice(was)} back.` : "Nothing is open." };
    },
    isolate(id) {
      if (!H.hasId(id)) return { ok: false, said: `I don't know ${id}.` };
      isolated = id;
      H.focus(id);
      return { ok: true, said: `Showing ${nice(id)}. Hiding the rest comes with the 3D update.` };
    },
    showSensors(id) {
      if (id === "ALL") {
        const tanks = H.tanks();
        const nodes = tanks.flatMap(sensorNodes);
        if (nodes.length) highlight.flash("sensors", nodes, 20000);
        const blind = tanks.filter((t) => { const r = liveFeed.reading(t); return !r || r.status === "UNKNOWN"; });
        const head = `${nodes.length} sensor node${nodes.length === 1 ? "" : "s"} in the model.`;
        return { ok: true, said: blind.length ? `${head} No reading from ${blind.map(nice).join(", ")}. Treat as unknown, not safe.` : `${head} Every tank level is reading.` };
      }
      if (!H.hasId(id)) return { ok: false, said: `I don't know ${id}.` };
      const nodes = sensorNodes(id);
      H.focus(id);
      if (nodes.length) highlight.flash("sensors", nodes, 20000);
      return { ok: true, said: `${nodes.length ? "" : "No sensor node for it in the model. "}${readingLine(id)}` };
    },
    tracePipes(id, direction = "both") {
      if (!H.hasId(id)) return { ok: false, said: `I don't know ${id}.` };
      const pipes = [];
      const re = new RegExp(`(^|_)${id}(_|$)`);
      H.scene.traverse((o) => { if (/^PIPE_/.test(o.name) && re.test(o.name.slice(5)) && !pipes.includes(o.name)) pipes.push(o.name); });
      H.focus(id);
      if (pipes.length) highlight.flash("trace", pipes, 20000);
      return { ok: true, said: pipes.length ? `${pipes.length} pipes touch ${nice(id)}. Following them hop by hop comes with the 3D update.` : `No pipes in the model touch ${nice(id)}.` };
    },
    reset() {
      opened = null;
      isolated = null;
      for (const s of VIEW_SOURCES) highlight.clear(s);
      H.select(null);
      H.setTime(0);
      H.flyHome();
      return { ok: true, said: "" };
    },
    state: () => ({ stub: true, open: opened, isolated }),
  };
}
