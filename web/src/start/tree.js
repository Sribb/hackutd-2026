// gaia's tree: a small procedural tree in white line art that draws itself in over about
// 30 s, branch by branch, then gets a few soft green leaves. Same tree every time (seeded).
//
//   const tree = mountTree(hostEl);   draws it, still hidden
//   tree.grow()                       starts the growth (no-op the second time)
//
// Each branch is one SVG path with pathLength="1", drawn with stroke-dashoffset 1 -> 0. A
// branch starts when its parent ends. Under prefers-reduced-motion it is shown grown.

const NS = "http://www.w3.org/2000/svg";
const DEPTH = 7;
const TOTAL_S = 30;        // trunk start to the last twig
const LEAF_FADE_S = 5;

function rng(seed) {       // mulberry32
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function branches() {
  const rand = rng(1147);
  const out = [];
  const levelS = TOTAL_S / DEPTH;
  const walk = (x, y, ang, len, depth, t0) => {
    const x1 = x + Math.cos(ang) * len, y1 = y + Math.sin(ang) * len;
    // A slight bow, so no branch is a ruler line.
    const bow = (rand() - 0.5) * len * 0.22;
    const cx = (x + x1) / 2 + Math.cos(ang + Math.PI / 2) * bow, cy = (y + y1) / 2 + Math.sin(ang + Math.PI / 2) * bow;
    const dur = levelS * (0.8 + rand() * 0.4);
    out.push({ d: `M${x.toFixed(1)} ${y.toFixed(1)}Q${cx.toFixed(1)} ${cy.toFixed(1)} ${x1.toFixed(1)} ${y1.toFixed(1)}`,
      w: Math.max(0.5, 3.4 * (1 - depth / DEPTH) ** 1.3 + 0.4), t0, dur, depth, tip: depth === DEPTH - 1 ? [x1, y1] : null });
    if (depth === DEPTH - 1) return;
    const kids = depth > 1 && rand() < 0.28 ? 3 : 2;
    const spread = 0.42 + rand() * 0.18;
    for (let k = 0; k < kids; k++) {
      const f = kids === 1 ? 0 : k / (kids - 1) - 0.5;
      const a = ang + f * spread * 2 + (rand() - 0.5) * 0.22 - 0.03 * Math.sign(Math.cos(ang));
      walk(x1, y1, a, len * (0.7 + rand() * 0.1), depth + 1, t0 + dur);
    }
  };
  walk(120, 200, -Math.PI / 2 + 0.02, 44, 0, 0);
  return out;
}

export function mountTree(host) {
  const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 240 204");
  svg.setAttribute("class", `s-tree${reduced ? " grown" : ""}`);
  svg.setAttribute("aria-hidden", "true");
  const gB = document.createElementNS(NS, "g"), gL = document.createElementNS(NS, "g");
  gB.setAttribute("class", "s-branches"); gL.setAttribute("class", "s-leaves");
  svg.append(gB, gL);
  const rand = rng(77);
  for (const b of branches()) {
    const p = document.createElementNS(NS, "path");
    p.setAttribute("d", b.d);
    p.setAttribute("pathLength", "1");
    p.setAttribute("stroke-width", b.w.toFixed(2));
    p.style.animationDuration = `${b.dur.toFixed(2)}s`;
    p.style.animationDelay = `${b.t0.toFixed(2)}s`;
    gB.appendChild(p);
    if (b.tip && rand() < 0.75) {
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", (b.tip[0] + (rand() - 0.5) * 3).toFixed(1));
      c.setAttribute("cy", (b.tip[1] + (rand() - 0.5) * 3).toFixed(1));
      c.setAttribute("r", (1.4 + rand() * 1.6).toFixed(1));
      c.style.animationDuration = `${LEAF_FADE_S}s`;
      c.style.animationDelay = `${(b.t0 + b.dur * 0.7 + rand() * 2).toFixed(2)}s`;
      gL.appendChild(c);
    }
  }
  host.appendChild(svg);
  let started = reduced;
  return {
    grow() { if (started) return; started = true; svg.classList.add("growing"); },
    el: svg,
  };
}
