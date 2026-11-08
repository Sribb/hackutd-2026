// The agent message stream (spec §2), drawn ChatGPT style. Four agents in one list:
//   watch · forecast · planner (templates over data, never an LLM) · gaia (answers)
// plus the person's own lines (right-aligned bubbles) and plain notes. Replies are plain text
// with a small mark, no bubble. Asset names in the text become inline chips.
//
// Each agent's latest bubble is live: say() with news posts a new bubble after typing dots
// (350 ms, 150 ms when urgent) and freezes the old one; the new text then streams in word by
// word (static under prefers-reduced-motion). say() without news, or within 20 s of the agent's
// last post, updates the live bubble in place. Urgent posts (an ALARM, feed lost) never wait.
// The stream keeps 50 bubbles; the shift log keeps everything. gaia/threads.js decides which
// thread each bubble shows in.

const AGENTS = { W: "watch", F: "forecast", P: "planner", J: "gaia" };
const KEEP = 50, RATE_MS = 20000;
const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const mark = (agent) => (agent === "J" ? `<span class="cs-mark g" aria-hidden="true">g</span>` : `<span class="cs-mark a-${agent}" aria-hidden="true"></span>`);

// A rounded rate reads "0", never "-0" or "+0" ("changing -0 barrels an hour").
const noSignedZero = (d) => (d && typeof d.text === "string" ? { ...d, text: d.text.replace(/(^|[^\w.:/-])[-−+]0(?![.\d]|,\d)/g, "$10") } : d);

export function makeStream(list, H) {
  const live = new Map();     // agent -> { li, data, at }
  const typing = new Map();   // agent -> li
  const pending = new Map();  // agent -> timer for a post waiting behind its dots
  const scroller = () => list.closest(".cs-scroll") || list;
  let onPost = () => {};

  // Asset chips: IDs the reply named, plus any nice name ("Tank 3", "Valve XO 34") in the text.
  function chipsFor(text, ids = []) {
    const out = [...ids.filter((x) => H.hasId(x))];
    const t = String(text || "");
    const names = H.ids().map((id) => [id, H.nice(id)]).sort((a, b) => b[1].length - a[1].length);
    let rest = t;
    for (const [id, name] of names) {
      const re = new RegExp(`\\b${reEsc(name)}\\b`, "i");
      if (re.test(rest)) { if (!out.includes(id)) out.push(id); rest = rest.replace(new RegExp(re.source, "gi"), " "); }
    }
    return out.slice(0, 6);
  }

  // The text with each chip's name turned into an inline chip (first mention), and, when
  // streaming, every word in a span that fades in after the one before it.
  function textHtml(text, chips, streaming) {
    let s = esc(String(text ?? "").replace(/\b(jarvis|freeboard)\b/gi, "gaia"));
    const tokens = [];
    const used = new Set();
    for (const [id, name] of chips.map((id) => [id, H.nice(id)]).sort((a, b) => b[1].length - a[1].length)) {
      const re = new RegExp(`\\b${reEsc(esc(name))}\\b`, "i");
      if (!re.test(s)) continue;
      s = s.replace(re, (m) => { tokens.push(`<button type="button" class="cs-chip in" data-asset="${esc(id)}">${m}</button>`); return `\u0001${tokens.length - 1}\u0001`; });
      used.add(id);
    }
    const words = s.split(/(\s+)/);
    const n = words.filter((w) => w && !/^\s+$/.test(w)).length;
    const step = Math.min(24, 1100 / Math.max(1, n));
    let i = 0;
    const out = words.map((w) => {
      if (!w || /^\s+$/.test(w)) return w;
      const h = w.replace(/\u0001(\d+)\u0001/g, (_, k) => tokens[Number(k)]);
      return streaming ? `<span class="w" style="animation-delay:${Math.round(i++ * step)}ms">${h}</span>` : h;
    }).join("");
    return { html: out, rest: chips.filter((id) => !used.has(id)) };
  }

  function html(agent, d, streaming = false) {
    const who = AGENTS[agent];
    const chips = d.chips || chipsFor(d.text, d.ids || []);
    const head = who
      ? `<div class="cs-bh">${mark(agent)}<b>${who}</b><time>${esc(d.clock || H.clockNow())}</time>${d.tag ? `<span class="cs-tag ${esc(d.tag)}">${esc(d.tag)}</span>` : ""}</div>`
      : "";
    const t = textHtml(d.text, chips, streaming && !reduced);
    return `${head}
      <p class="cs-text${streaming && !reduced ? " streaming" : ""}">${t.html}</p>
      ${d.visual ? `<div class="cs-vis">${d.visual}</div>` : ""}
      ${t.rest.length ? `<div class="cs-chips">${t.rest.map((id) => `<button type="button" class="cs-chip" data-asset="${esc(id)}">${esc(H.nice(id))}</button>`).join("")}</div>` : ""}
      ${d.extra || ""}`;
  }

  function wire(li, d) {
    li.querySelectorAll("[data-asset]").forEach((b) => { b.onclick = () => H.chip(b.dataset.asset, b); });
    if (d.bind) d.bind(li);
  }

  function trim() {
    while (list.children.length > KEEP) {
      const first = list.firstElementChild;
      for (const [a, v] of live) if (v.li === first) live.delete(a);
      first.remove();
    }
  }
  const scroll = () => { const s = scroller(); s.scrollTop = s.scrollHeight; };

  function showTyping(agent, label) {
    let li = typing.get(agent);
    if (!li) {
      li = document.createElement("li");
      li.className = `cs-b typing a-${agent}`;
      li.dataset.agent = agent;
      typing.set(agent, li);
      list.appendChild(li);
    }
    li.innerHTML = `<div class="cs-bh">${mark(agent)}<b>${AGENTS[agent] || ""}</b></div>
      <p class="cs-dots" aria-label="${esc(label || "typing")}">${reduced ? "…" : "<i></i><i></i><i></i>"}${label ? ` <span>${esc(label)}</span>` : ""}</p>`;
    scroll();
    return li;
  }
  function hideTyping(agent) {
    const li = typing.get(agent);
    if (li) { li.remove(); typing.delete(agent); }
  }

  function place(agent, d) {
    d = noSignedZero(d);
    hideTyping(agent);
    const prev = live.get(agent);
    if (prev) { prev.li.classList.add("frozen"); prev.li.classList.remove("live"); }
    const li = document.createElement("li");
    li.className = `cs-b live a-${agent}${d.cls ? ` ${d.cls}` : ""}`;
    li.dataset.agent = agent;
    if (d.key) li.dataset.key = d.key;
    li.innerHTML = html(agent, d, true);
    li._h = html(agent, d);
    wire(li, d);
    list.appendChild(li);
    if (AGENTS[agent]) live.set(agent, { li, data: d, at: Date.now() });
    trim();
    scroll();
    onPost(agent, d, li);
    return li;
  }

  function update(agent, d) {
    const cur = live.get(agent);
    if (!cur) return place(agent, d);
    cur.data = { ...cur.data, ...d };
    const cls = `cs-b live a-${agent}${cur.data.cls ? ` ${cur.data.cls}` : ""}`;
    if (cur.li.className !== cls) cur.li.className = cls;
    const h = html(agent, cur.data);
    // Same words and visuals: leave the bubble alone (no flicker, no cut-off streaming).
    if (cur.li._h === h) return cur.li;
    cur.li._h = h;
    cur.li.innerHTML = h;
    wire(cur.li, cur.data);
    return cur.li;
  }

  // say(agent, data, { news, urgent }): news posts a new bubble (rate-limited unless urgent).
  function say(agent, d, { news = true, urgent = false } = {}) {
    d = noSignedZero(d);
    const cur = live.get(agent);
    const waiting = pending.get(agent);
    // A post is already behind its typing dots: it goes out with the newest words, once.
    if (waiting) { waiting.d = d; return null; }
    if (!news && cur) return update(agent, d);
    if (cur && !urgent && Date.now() - cur.at < RATE_MS) { const li = update(agent, d); onPost(agent, d, li, true); return li; }
    showTyping(agent);
    const ms = urgent ? 150 : 350;
    const job = { d };
    job.t = setTimeout(() => { pending.delete(agent); place(agent, job.d); }, ms);
    pending.set(agent, job);
    return null;
  }

  // Lines that aren't an agent: the person's question, a plain note.
  function line(kind, text, extra = {}) {
    const li = document.createElement("li");
    li.className = `cs-b a-${kind}`;
    li.innerHTML = html(kind, { text, chips: [], ...extra });
    wire(li, extra);
    list.appendChild(li);
    trim();
    scroll();
    return li;
  }

  return {
    say, update, line, showTyping, hideTyping, chipsFor, post: place,
    latest: (agent) => (live.get(agent) || {}).li || null,
    data: (agent) => (live.get(agent) || {}).data || null,
    onPost: (fn) => { onPost = fn; },
  };
}
