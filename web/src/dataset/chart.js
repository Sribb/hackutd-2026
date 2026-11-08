// Small SVG line charts for the dataset page. One y-axis per chart, never two: series
// that share a chart share a unit. Identity is never color alone: the second series is
// dashed and every series is named in the legend. A shared crosshair links a stack of
// charts so one time reads across all of them.

const NS = "http://www.w3.org/2000/svg";
const el = (tag, attrs = {}, parent) => {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
};

export const SERIES = ["var(--s1)", "var(--s2)"];
const DASH = ["", "5 4"];

export function niceTicks(lo, hi, count = 4) {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [];
  if (hi - lo < 1e-9) { hi = lo + 1; lo = lo - 1; }
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

const fmtNum = (v) => {
  const a = Math.abs(v);
  if (a >= 10000) return (v / 1000).toFixed(a >= 100000 ? 0 : 1) + "k";
  if (a >= 100) return Math.round(v).toLocaleString("en-US");
  if (a >= 10) return v.toFixed(1).replace(/\.0$/, "");
  return v.toFixed(2).replace(/\.?0+$/, "");
};

export const fmtTime = (t, withDay = false) => {
  const d = new Date(t * 1000);
  const hm = d.toISOString().slice(11, 16);
  if (!withDay) return hm;
  return `${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()]} ${d.toISOString().slice(5, 10)} ${hm}`;
};

// opts: { title, unit, from, to, series: [{ name, points: [[t, v]] }], refs: [{ y, label, tone }],
//         marks: [{ t, label, tone }], yMin, yMax, step: bool (status-like), onHover(t|null) }
export function lineChart(host, opts) {
  host.innerHTML = "";
  host.classList.add("chart");
  const head = document.createElement("div");
  head.className = "chart-head";
  const title = document.createElement("b");
  title.textContent = opts.title;
  head.appendChild(title);
  if (opts.unit) {
    const u = document.createElement("span");
    u.className = "chart-unit";
    u.textContent = opts.unit;
    head.appendChild(u);
  }
  const legend = document.createElement("span");
  legend.className = "chart-legend";
  if (opts.series.length > 1) {
    opts.series.forEach((s, i) => {
      const k = document.createElement("span");
      k.className = "key";
      k.innerHTML = `<svg width="18" height="6" aria-hidden="true"><line x1="0" y1="3" x2="18" y2="3" stroke="${SERIES[i % 2]}" stroke-width="2" stroke-dasharray="${DASH[i % 2]}"/></svg>`;
      k.appendChild(document.createTextNode(s.name));
      legend.appendChild(k);
    });
  }
  head.appendChild(legend);
  const readout = document.createElement("span");
  readout.className = "chart-read";
  head.appendChild(readout);
  host.appendChild(head);

  const W = Math.max(240, host.clientWidth || 600);
  const H = opts.height || 120;
  const m = { l: 44, r: 10, t: 6, b: 18 };
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img",
    "aria-label": `${opts.title} from ${fmtTime(opts.from, true)} to ${fmtTime(opts.to, true)} UTC` }, host);

  const all = opts.series.flatMap((s) => s.points.map((p) => p[1]));
  for (const r of opts.refs || []) all.push(r.y);
  let lo = opts.yMin ?? Math.min(...all);
  let hi = opts.yMax ?? Math.max(...all);
  if (!all.length) { lo = 0; hi = 1; }
  if (opts.yMin === undefined && lo > 0 && lo < (hi - lo) * 0.6) lo = 0;
  const pad = (hi - lo) * 0.06 || 1;
  if (opts.yMax === undefined) hi += pad;
  if (opts.yMin === undefined && lo !== 0) lo -= pad;
  const x = (t) => m.l + ((t - opts.from) / Math.max(1, opts.to - opts.from)) * (W - m.l - m.r);
  const y = (v) => H - m.b - ((v - lo) / Math.max(1e-9, hi - lo)) * (H - m.t - m.b);

  const grid = el("g", { class: "grid" }, svg);
  for (const v of niceTicks(lo, hi, 3)) {
    el("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }, grid);
    el("text", { x: m.l - 6, y: y(v) + 3, "text-anchor": "end" }, grid).textContent = fmtNum(v);
  }
  const span = opts.to - opts.from;
  const tStep = [300, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400].find((s) => span / s <= 7) || 86400;
  for (let t = Math.ceil(opts.from / tStep) * tStep; t <= opts.to; t += tStep) {
    el("line", { x1: x(t), x2: x(t), y1: m.t, y2: H - m.b, class: "vt" }, grid);
    el("text", { x: x(t), y: H - 4, "text-anchor": "middle" }, grid).textContent =
      tStep >= 86400 || new Date(t * 1000).getUTCHours() === 0 ? fmtTime(t, true).slice(0, 9) : fmtTime(t);
  }

  // Close reference lines (warning 85, shutdown 92) would print on top of each other, so
  // the higher label sits above its line and the lower one below.
  const refs = (opts.refs || []).filter((r) => r.y >= lo && r.y <= hi).sort((a, b) => b.y - a.y);
  refs.forEach((r, i) => {
    el("line", { x1: m.l, x2: W - m.r, y1: y(r.y), y2: y(r.y), class: `ref ${r.tone || ""}` }, svg);
    const below = i > 0 && Math.abs(y(r.y) - y(refs[i - 1].y)) < 12;
    el("text", { x: W - m.r - 2, y: y(r.y) + (below ? 10 : -3), "text-anchor": "end", class: `ref-label ${r.tone || ""}` }, svg).textContent = r.label;
  });
  for (const k of opts.marks || []) {
    if (k.t < opts.from || k.t > opts.to) continue;
    el("line", { x1: x(k.t), x2: x(k.t), y1: m.t, y2: H - m.b, class: `mark ${k.tone || ""}` }, svg);
    if (k.label) el("text", { x: x(k.t) + 3, y: m.t + 9, class: `mark-label ${k.tone || ""}` }, svg).textContent = k.label;
  }

  opts.series.forEach((s, i) => {
    if (!s.points.length) return;
    let d = "";
    let prev = null;
    for (const [t, v] of s.points) {
      const px = x(t).toFixed(1), py = y(v).toFixed(1);
      // Break the line across a gap in readings instead of drawing through it.
      const gap = prev && t - prev[0] > (opts.gapS || 900);
      if (!prev || gap) d += `M${px},${py}`;
      else if (opts.step) d += `H${px}V${py}`;
      else d += `L${px},${py}`;
      prev = [t, v];
    }
    el("path", { d, class: "line", stroke: s.color || SERIES[i % 2], "stroke-dasharray": s.dash ?? DASH[i % 2], fill: "none" }, svg);
  });

  if (!opts.series.some((s) => s.points.length)) {
    el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", class: "empty" }, svg).textContent = opts.empty || "No readings in this window";
  }

  const cross = el("line", { y1: m.t, y2: H - m.b, class: "cross", visibility: "hidden" }, svg);
  const dots = opts.series.map((s, i) => el("circle", { r: 3.5, class: "cross-dot", fill: s.color || SERIES[i % 2], visibility: "hidden" }, svg));
  const hit = el("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" }, svg);

  const nearest = (pts, t) => {
    let lo2 = 0, hi2 = pts.length - 1;
    if (hi2 < 0) return null;
    while (lo2 < hi2) { const mid = (lo2 + hi2) >> 1; if (pts[mid][0] < t) lo2 = mid + 1; else hi2 = mid; }
    const a = pts[Math.max(0, lo2 - 1)], b = pts[lo2];
    return Math.abs(a[0] - t) <= Math.abs(b[0] - t) ? a : b;
  };

  const api = {
    show(t) {
      if (t === null || t < opts.from || t > opts.to) {
        cross.setAttribute("visibility", "hidden");
        dots.forEach((d) => d.setAttribute("visibility", "hidden"));
        readout.textContent = "";
        return;
      }
      cross.setAttribute("x1", x(t)); cross.setAttribute("x2", x(t));
      cross.setAttribute("visibility", "visible");
      const parts = [fmtTime(t, true)];
      opts.series.forEach((s, i) => {
        const p = nearest(s.points, t);
        if (!p || Math.abs(p[0] - t) > (opts.gapS || 900)) { dots[i].setAttribute("visibility", "hidden"); return; }
        dots[i].setAttribute("cx", x(p[0])); dots[i].setAttribute("cy", y(p[1]));
        dots[i].setAttribute("visibility", "visible");
        parts.push(`${opts.series.length > 1 ? s.name + " " : ""}${opts.format ? opts.format(p[1]) : fmtNum(p[1])}`);
      });
      readout.textContent = parts.join("  ·  ");
    },
  };
  const toT = (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    return opts.from + ((px - m.l) / (W - m.l - m.r)) * (opts.to - opts.from);
  };
  hit.addEventListener("pointermove", (ev) => (opts.onHover ? opts.onHover(toT(ev)) : api.show(toT(ev))));
  hit.addEventListener("pointerleave", () => (opts.onHover ? opts.onHover(null) : api.show(null)));
  return api;
}
