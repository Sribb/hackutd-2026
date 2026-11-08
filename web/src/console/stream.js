// The agent message stream (spec §2). Four agents in one chat-style list:
//   W Watch · F Forecast · P Planner (templates over data, never an LLM) · J Jarvis (answers)
// plus the operator's own lines ("you") and plain notes.
//
// Each agent's latest bubble is live: say() with news posts a new bubble after typing dots
// (350 ms, 150 ms when urgent) and freezes the old one; say() without news, or within 20 s of
// the agent's last post, updates the live bubble in place. Urgent posts (an ALARM, feed lost)
// never wait. The stream keeps 50 bubbles; the shift log keeps everything.

const AGENTS = { W: "Watch", F: "Forecast", P: "Planner", J: "Jarvis" };
const KEEP = 50, RATE_MS = 20000;
const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

export function makeStream(list, H) {
  const live = new Map();     // agent -> { li, data, at }
  const typing = new Map();   // agent -> li
  const pending = new Map();  // agent -> timer for a post waiting behind its dots
  let onPost = () => {};

  // Asset chips: IDs the reply named, plus any nice name ("Tank 3", "Valve XO 34") in the text.
  function chipsFor(text, ids = []) {
    const out = [...ids.filter((x) => H.hasId(x))];
    const t = String(text || "");
    const names = H.ids().map((id) => [id, H.nice(id)]).sort((a, b) => b[1].length - a[1].length);
    let rest = t;
    for (const [id, name] of names) {
      const re = new RegExp(`\\b${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i");
      if (re.test(rest)) { if (!out.includes(id)) out.push(id); rest = rest.replace(new RegExp(re.source, "gi"), " "); }
    }
    return out.slice(0, 6);
  }

  function html(agent, d) {
    const who = AGENTS[agent];
    const chips = d.chips || chipsFor(d.text, d.ids || []);
    const head = who
      ? `<div class="cs-bh"><span class="cs-letter ${agent}">${agent}</span><b>${who.toUpperCase()}</b><time>${esc(d.clock || H.clockNow())}</time>${d.tag ? `<span class="cs-tag ${esc(d.tag)}">${esc(d.tag)}</span>` : ""}</div>`
      : agent === "you" ? `<div class="cs-bh"><b>YOU</b></div>` : "";
    return `${head}
      ${d.visual ? `<div class="cs-vis">${d.visual}</div>` : ""}
      <p class="cs-text">${esc(d.text)}</p>
      ${chips.length ? `<div class="cs-chips">${chips.map((id) => `<button type="button" class="cs-chip" data-asset="${esc(id)}">${esc(H.nice(id))}</button>`).join("")}</div>` : ""}
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
  const scroll = () => { list.scrollTop = list.scrollHeight; };

  function showTyping(agent, label) {
    let li = typing.get(agent);
    if (!li) {
      li = document.createElement("li");
      li.className = `cs-b typing a-${agent}`;
      li.dataset.agent = agent;
      typing.set(agent, li);
      list.appendChild(li);
    }
    li.innerHTML = `<div class="cs-bh"><span class="cs-letter ${agent}">${agent}</span><b>${(AGENTS[agent] || "").toUpperCase()}</b></div>
      <p class="cs-dots" aria-label="${esc(label || "typing")}">${reduced ? "…" : "<i></i><i></i><i></i>"}${label ? ` <span>${esc(label)}</span>` : ""}</p>`;
    scroll();
    return li;
  }
  function hideTyping(agent) {
    const li = typing.get(agent);
    if (li) { li.remove(); typing.delete(agent); }
  }

  function place(agent, d) {
    hideTyping(agent);
    const prev = live.get(agent);
    if (prev) { prev.li.classList.add("frozen"); prev.li.classList.remove("live"); }
    const li = document.createElement("li");
    li.className = `cs-b live a-${agent}${d.cls ? ` ${d.cls}` : ""}`;
    li.dataset.agent = agent;
    if (d.key) li.dataset.key = d.key;
    li.innerHTML = html(agent, d);
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
    cur.li.className = `cs-b live a-${agent}${cur.data.cls ? ` ${cur.data.cls}` : ""}`;
    cur.li.innerHTML = html(agent, cur.data);
    wire(cur.li, cur.data);
    return cur.li;
  }

  // say(agent, data, { news, urgent }): news posts a new bubble (rate-limited unless urgent).
  function say(agent, d, { news = true, urgent = false } = {}) {
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

  // Lines that aren't an agent: the operator's question, a plain note.
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
