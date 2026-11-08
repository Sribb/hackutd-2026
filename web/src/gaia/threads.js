// The left sidebar (ChatGPT style): the cursive gaia wordmark, "new chat", "field overview",
// "shift log", one thread per incident ("pump 3 tripped · 1:48 am"), the person's own chats,
// and a quiet "classic view" link at the bottom.
//
// Threads are views over the ONE agent stream (stream.js). Every <li> that lands in the stream
// gets a data-thread: Watch, Forecast and Planner post into the active incident thread (or
// "field" when nothing is open); gaia's replies, the person's lines and notes go to the thread
// being viewed. Only the viewed thread's items are shown. The fix card is not in any thread:
// it shows wherever you are, so a pending fix is never hidden.
//
//   const threads = mountThreads(app, side, H)
//   threads.alert(a)        register an alert (a root alert opens an incident thread)
//   threads.view(id)        show a thread ("new", "field", an alert id, "chat-N")
//   threads.active()        the thread the monitoring agents post into
//   threads.viewing()       the thread on screen

import { esc } from "../console/stream.js";
import { ICON } from "./icons.js";

const AGENT = new Set(["W", "F", "P"]);

export function mountThreads(app, side, H) {
  const nav = document.createElement("nav");
  nav.id = "csNav";
  nav.className = "cs-nav";
  nav.setAttribute("aria-label", "Threads");
  nav.innerHTML = `
    <div class="cs-nav-top"><span class="cs-wordmark" aria-label="gaia">gaia</span><span class="cs-grow"></span>
      <button type="button" class="cs-icon" id="csNavClose" title="Hide threads" aria-label="Hide threads">${ICON.sidebar}</button></div>
    <button type="button" class="cs-navrow" data-new>${ICON.compose}<span>new chat</span></button>
    <button type="button" class="cs-navrow" data-thread="field">${ICON.field}<span>field overview</span><i class="cs-unread" hidden></i></button>
    <button type="button" class="cs-navrow" data-log>${ICON.log}<span>shift log</span></button>
    <div class="cs-nav-label">incidents</div>
    <div class="cs-nav-list" id="csIncidents"><p class="cs-nav-none">none yet</p></div>
    <div class="cs-nav-label" id="csChatsLabel" hidden>chats</div>
    <div class="cs-nav-list" id="csChats"></div>
    <span class="cs-grow"></span>
    <a class="cs-navrow quiet" href="/dataset">datasets</a>
    <a class="cs-navrow quiet" id="csClassic" href="?layout=classic">classic view</a>`;
  app.prepend(nav);
  const $ = (id) => nav.querySelector(`#${id}`);
  const list = side.stream;
  const threads = new Map([["field", { id: "field", kind: "field", title: "field overview" }]]);
  const alertThread = new Map(); // alert id -> thread id
  let viewing = "new", chatN = 0;

  nav.querySelector("[data-new]").onclick = () => newChat();
  nav.querySelector("[data-log]").onclick = () => H.onLog();
  nav.querySelector('[data-thread="field"]').onclick = () => { view("field"); H.onOverview(); };
  $("csNavClose").onclick = () => H.onNav(false);
  $("csClassic").href = (() => { const u = new URL(location.href); u.searchParams.set("layout", "classic"); u.searchParams.delete("intro"); return u.pathname + u.search; })();

  function newChat() { view("new"); H.onNew(); }

  function title(a) {
    const msg = String(a.message || a.kind || "alert").split(/[:.]/)[0].trim().toLowerCase();
    return `${msg}${a.clock ? ` · ${H.clock(a.clock)}` : ""}`;
  }

  function alert(a) {
    if (!a || !a.id) return;
    const root = a.root_id && a.root_id !== a.id ? a.root_id : null;
    if (root) { alertThread.set(a.id, alertThread.get(root) || root); return; }
    alertThread.set(a.id, a.id);
    const t = threads.get(a.id) || { id: a.id, kind: "incident", at: Date.now() };
    t.title = title(a);
    t.active = a.active !== false;
    t.level = a.severity === "ALARM" || a.priority === "P1" ? "red" : a.severity === "WARNING" ? "amber" : "gray";
    threads.set(a.id, t);
    drawRows();
  }

  // The newest active incident is where the monitoring agents post; "field" when none is open.
  function active() {
    const inc = [...threads.values()].filter((t) => t.kind === "incident" && t.active).sort((x, y) => y.at - x.at);
    return inc.length ? inc[0].id : "field";
  }

  function rowHtml(t) {
    return `<button type="button" class="cs-navrow${t.id === viewing ? " on" : ""}" data-thread="${esc(t.id)}">${t.kind === "incident" ? `<span class="cs-dot ${t.active ? t.level : "gray"}"></span>` : ICON.chat}<span>${esc(t.title)}</span><i class="cs-unread"${t.unread ? "" : " hidden"}></i></button>`;
  }
  function drawRows() {
    const inc = [...threads.values()].filter((t) => t.kind === "incident").sort((x, y) => y.at - x.at);
    const chats = [...threads.values()].filter((t) => t.kind === "chat").sort((x, y) => y.at - x.at);
    const ih = inc.length ? inc.map(rowHtml).join("") : `<p class="cs-nav-none">none yet</p>`;
    const ch = chats.map(rowHtml).join("");
    if ($("csIncidents")._h !== ih) { $("csIncidents").innerHTML = ih; $("csIncidents")._h = ih; }
    if ($("csChats")._h !== ch) { $("csChats").innerHTML = ch; $("csChats")._h = ch; }
    $("csChatsLabel").hidden = !chats.length;
    nav.querySelectorAll(".cs-nav-list [data-thread]").forEach((b) => { b.onclick = () => view(b.dataset.thread); });
    const f = nav.querySelector('.cs-navrow[data-thread="field"]');
    f.classList.toggle("on", viewing === "field");
    f.querySelector(".cs-unread").hidden = !threads.get("field").unread;
  }

  function view(id) {
    if (id !== "new" && !threads.has(id)) id = "field";
    viewing = id;
    // A new chat starts empty: loose lines left in the last one go to the field overview.
    if (id === "new") for (const li of list.children) if (li.dataset.thread === "new") li.dataset.thread = "field";
    const t = threads.get(id);
    if (t) t.unread = false;
    for (const li of list.children) li.hidden = li.dataset.thread !== id;
    side.setTitle(t ? t.title : "new chat");
    drawRows();
    refresh();
    side.scroll.scrollTop = side.scroll.scrollHeight;
    H.onView(id);
  }

  // Every new <li> in the stream: which thread it belongs to, and whether it shows.
  function assign(li) {
    if (!li.dataset.thread) {
      const agent = li.dataset.agent;
      let id;
      if (AGENT.has(agent)) id = (li.dataset.key && alertThread.get(li.dataset.key)) || active();
      else id = viewing;
      // The person's first line in a new chat starts a chat thread named after it.
      if (id === "new" && li.classList.contains("a-you")) {
        chatN += 1;
        const text = (li.textContent || "").trim().toLowerCase();
        id = `chat-${chatN}`;
        threads.set(id, { id, kind: "chat", title: text.length > 34 ? `${text.slice(0, 33)}…` : text || "new chat", at: Date.now() });
        viewing = id;
        side.setTitle(threads.get(id).title);
        drawRows();
      }
      li.dataset.thread = id;
    }
    li.hidden = li.dataset.thread !== viewing;
    const t = threads.get(li.dataset.thread);
    if (li.hidden && t && !li.classList.contains("typing")) { t.unread = true; drawRows(); }
  }
  new MutationObserver((muts) => {
    for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1 && n.tagName === "LI") assign(n);
    refresh();
  }).observe(list, { childList: true });
  new MutationObserver(refresh).observe(side.fix, { attributes: true, attributeFilter: ["hidden"] });

  // ChatGPT's empty state: nothing in this thread and no fix card -> greeting and suggestions.
  function refresh() {
    const drawer = side.el.querySelector(".cs-drawer");
    const any = [...list.children].some((li) => !li.hidden) || !side.fix.hidden || (drawer && !drawer.hidden);
    side.el.classList.toggle("cs-empty", !any);
    side.setHello(H.greeting());
  }

  return {
    el: nav, alert, view, active, refresh, newChat,
    viewing: () => viewing,
    threadOf: (li) => (li && li.dataset.thread) || null,
    list: () => [...threads.values()].map((t) => ({ id: t.id, kind: t.kind, title: t.title, active: !!t.active, unread: !!t.unread })),
  };
}
