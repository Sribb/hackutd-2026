// Plan runs (#63/#75, CONTRACT.md "Plan runs"): an approved plan carried out step by step on
// the live field. Every step is an ordinary proposal that waits for a person's hold-to-confirm;
// the run never confirms anything. This panel shows the run and its steps with honest statuses,
// lets a person confirm or reject the step that is waiting, and handles the hard cases:
//
//   - a stale plan (409 on execute): says why and offers the backend's current plan, which can
//     be run with one more hold;
//   - a paused run (a step not seen to take effect, a tank rising faster than the replay, a
//     rejected or failed step): says why, and offers Jarvis's re-plan with a hold to start it;
//   - a step whose command is SENDING or UNCERTAIN: never shown as done, never retried.
//
// Fed by POST /api/plans/{id}/execute, GET /api/runs, /ws "run" and "proposal" events.
// Only used when the backend has /api/runs; otherwise the console keeps its static walk.

import { authFetch } from "./auth.js";
import * as liveFeed from "./live.js";
import { holdButton, isHolding, afterHold } from "./fx.js";

let base = "";
let hooks = null;
let box = null;
let supported = null;      // null unknown, true/false after the first GET /api/runs
let run = null;            // the run on screen
let offer = null;          // { kind: "stale"|"replan", text, plan?, run? }
let note = "";
const props = new Map();   // proposal id -> proposal (for SENDING / UNCERTAIN / FAILED)

// Lone-worker check-in. A step that sends a person out alone at night (the rover to a manual
// valve) starts a check-in clock once it is confirmed. NIOSH/OSHA (OSHA 3843, MMWR 65(1)):
// nine workers died at production tanks in 2010-2015, all working alone. Missed check-ins
// say so on the console and in the shift log. Field time (the live clock), so it runs at
// demo speed in a rehearsal.
const CHECK_MIN = 20;
const crew = new Map();    // `${run}:${n}` -> { who, asset, text, since, last, missed }
const returned = new Set(); // keys marked "Back in": never tracked again from a later snapshot
let lastStatus = "";
let wsAt = 0, wsRun = "";   // when the socket last brought the run on screen (an older GET loses)

// People out survive a reload: a reset clock would push a missed check-in out by up to 20 min.
// Kept 30 min after the page was last open (like the shift log); ?fresh starts empty.
const CREW_KEY = "freeboard.crew";
function saveCrew() {
  try { localStorage.setItem(CREW_KEY, JSON.stringify({ saved: Date.now(), crew: [...crew], returned: [...returned] })); } catch { /* private mode */ }
}
function loadCrew() {
  try {
    if (new URLSearchParams(location.search).has("fresh")) { localStorage.removeItem(CREW_KEY); return; }
    const j = JSON.parse(localStorage.getItem(CREW_KEY) || "null");
    if (!j || Date.now() - j.saved > 30 * 60000) return;
    for (const [k, c] of j.crew || []) crew.set(k, c);
    for (const k of j.returned || []) returned.add(k);
  } catch { /* nothing kept */ }
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

const ACTIVE = new Set(["RUNNING", "PAUSED", "CANCELLING"]);
const STEP_WORD = {
  WAITING: "waiting", PENDING: "needs you", VERIFYING: "checking", DONE: "done", MISMATCH: "not seen",
  REJECTED: "rejected", FAILED: "failed", CANCELLED: "cancelled",
};

async function call(path, body) {
  const r = await authFetch(`${base}${path}`, body === undefined ? undefined
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const out = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = out && out.detail;
    const msg = r.status === 401 ? "Not sent: control is locked without the operator PIN."
      : r.status === 403 ? "This device can't do that. Nothing was sent."
      : typeof d === "string" ? d : `HTTP ${r.status}`;
    throw Object.assign(new Error(msg), { status: r.status, body: out });
  }
  return out;
}

// True when this backend runs plans (GET /api/runs answers).
export async function runsSupported() {
  if (supported != null || !base) return !!supported;
  try {
    const r = await fetch(`${base}/api/runs`);
    // Only a 404 means "no plan runs here". A 5xx or a restart is asked again next time.
    supported = r.ok ? true : r.status === 404 ? false : null;
    if (r.ok) {
      const j = await r.json();
      const known = new Set((j.runs || []).map(runKey));
      let pruned = false;
      for (const [k, c] of crew) if (!c.run || !known.has(c.run)) { crew.delete(k); pruned = true; }
      for (const k of [...returned]) if (!known.has(k.slice(0, k.lastIndexOf(":")))) returned.delete(k);
      if (pruned) { saveCrew(); draw(); }
      const active = (j.runs || []).filter((x) => ACTIVE.has(x.status) && x.started !== false);
      if (active.length) setRun(active[active.length - 1]);
    }
  } catch { supported = null; }
  return !!supported;
}

// The backend's 409 text ends with "Execute plan_x to run it.", an instruction for API callers.
const plain = (t) => String(t || "").replace(/\s*Execute plan_\w+ to run it\.?/, "").trim();

// Same commands (type, asset, value, target), whatever their timing. A re-scored plan with the
// same commands is what the person already held to approve, so it can start without asking twice.
const key = (a) => `${a.type}|${a.asset_id}|${a.value ?? ""}|${a.target_id ?? ""}`;
// Same outcome too: still keeps every tank running, same robustness, same tanks it can't save,
// and a cost within 10%. Anything else is a different plan, and goes back to the person.
function sameOutcome(a, b) {
  const cost = (p) => (p.cost && typeof p.cost === "object" ? p.cost.total_usd : p.cost_usd);
  const ca = cost(a), cb = cost(b);
  return (a.keeps_running !== false) === (b.keeps_running !== false)
    && (a.robust === true) === (b.robust === true)
    && JSON.stringify((a.cannot_save || []).slice().sort()) === JSON.stringify((b.cannot_save || []).slice().sort())
    && typeof ca === "number" && typeof cb === "number" && Math.abs(cb - ca) <= 0.1 * Math.max(ca, 1);
}
function sameActions(a, b) {
  const x = ((a && a.actions) || (a && a.steps ? a.steps.map((s) => s.action) : [])).map(key).sort();
  const y = ((b && b.actions) || []).map(key).sort();
  return x.length > 0 && x.length === y.length && x.every((k, i) => k === y[i]);
}

function stepProposal(s) {
  return s.proposal_id ? props.get(s.proposal_id) : null;
}

// What a step's line says, from the step and its proposal. A command that was sent but not
// answered is UNCERTAIN: it may have reached the device, so it is never called done or failed.
function stepState(s) {
  const p = stepProposal(s);
  if (p && p.status === "SENDING") return { cls: "SENDING", word: "sending", detail: "Command on its way to the device." };
  if (p && p.status === "UNCERTAIN") return { cls: "UNCERTAIN", word: "uncertain", detail: `${p.result || "No answer from the device."} It may have moved. Check it in person. Not retried.` };
  const word = STEP_WORD[s.status] || String(s.status || "").toLowerCase();
  const detail = s.status === "MISMATCH" ? s.verify || s.result
    : s.status === "FAILED" ? s.result || "The connector refused the command."
    : s.status === "DONE" ? s.verify || s.result
    : s.status === "VERIFYING" ? `Confirmed. Watching the readings for the effect.`
    : s.status === "WAITING" ? ""
    : s.result || "";
  return { cls: s.status, word, detail };
}

function headline(r) {
  if (!r) return "";
  if (r.status === "RUNNING") {
    const waiting = r.steps.filter((s) => s.status === "PENDING").length;
    return waiting ? `${waiting} step${waiting > 1 ? "s" : ""} waiting for you.` : "Running. The next step is released on time.";
  }
  if (r.status === "PAUSED") return r.started === false ? "Offered, not started." : `Paused: ${r.reason || "no reason given"}`;
  if (r.status === "CANCELLING") return "Cancelling: a command already sent is finishing. Nothing new goes out.";
  if (r.status === "DONE") return "Every step is done.";
  if (r.status === "FAILED") return `Failed: ${r.reason || "a tank reached shutdown during the run."}`;
  if (r.status === "CANCELLED") return `Cancelled.${r.reason ? ` ${r.reason}` : ""}`;
  return r.status;
}

// The live minute now (carried on while the feed is lost), or null with no live layer.
const nowT = () => { const c = liveFeed.clockEstimate(); return c && typeof c.t === "number" ? c.t : null; };

const runKey = (r) => `${r.id}@${r.created_t || ""}`;

// Start a check-in clock for each confirmed step that sends someone out alone.
function trackCrew(r) {
  const t = nowT();
  if (t == null) return;
  for (const s of r.steps) {
    // A restarted backend numbers runs from run_1 again: created_t keeps them apart.
    const key = `${runKey(r)}:${s.n}`;
    if (crew.has(key) || returned.has(key) || !["VERIFYING", "DONE", "MISMATCH"].includes(s.status)) continue;
    const who = hooks.outside && hooks.outside(s.action);
    if (!who) continue;
    // The clock starts when the step was confirmed (live minutes), not when this screen saw it.
    const at = typeof s.confirmed_t === "number" && s.confirmed_t <= t ? s.confirmed_t : t;
    crew.set(key, { who, run: runKey(r), asset: s.action.asset_id, text: s.text, since: at, last: at, missed: false });
    saveCrew();
    hooks.log(`${who} out at ${hooks.nice(s.action.asset_id)}. Check in every ${CHECK_MIN} min.`);
  }
}

function crewHtml() {
  const t = nowT();
  if (!crew.size || t == null) return "";
  const html = `<div class="run-crew"><div class="run-offer-k">People out</div>${[...crew.entries()].map(([k, c]) => {
    if (c.last > t + 1) c.last = c.since = t; // the live clock went back (backend restart): count from now
    const due = c.last + CHECK_MIN, late = Math.floor(t - due);
    if (late >= 0 && !c.missed) { c.missed = true; hooks.log(`MISSED CHECK-IN: ${c.who} at ${hooks.nice(c.asset)}. Call them; send backup if no answer.`); }
    return `<div class="crew-row ${late >= 0 ? "late" : ""}"><span>${esc(c.who)} at ${esc(hooks.nice(c.asset))}</span>
      <span class="crew-due">${late >= 0 ? `MISSED CHECK-IN ${late < 1 ? "just now" : `${late} min ago`}. Call them.` : `check in by ${esc(hooks.clockIn(due - t))}`}</span>
      <span class="plan-btns"><button data-checkin="${esc(k)}">Checked in</button><button data-back="${esc(k)}">Back in</button></span></div>`;
  }).join("")}</div>`;
  saveCrew(); // after any MISSED CHECK-IN flag is set, so a reload doesn't log it twice
  return html;
}

function draw() {
  if (!box) return;
  if (isHolding()) { afterHold(draw); return; }
  if (!run && !offer && !note && !crew.size) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  const r = run;
  const steps = r ? r.steps : [];
  const done = steps.filter((s) => s.status === "DONE").length;
  box.innerHTML = `<div class="kicker">Plan run${r ? ` · ${esc(r.status)}` : ""}${r ? ` · ${done} of ${steps.length} done` : ""}</div>
    ${r ? `<div class="run-sum">${esc(r.summary.split(". ")[0])}. <b>${hooks.usd(r.cost_usd)}</b>${r.keeps_running === false ? ` <span class="bad">· does not keep every tank below shutdown</span>` : r.robust === false ? ` <span class="muted">· not enough if inflow runs 15% high</span>` : ""}</div>
      <div class="run-head ${esc(r.status)}">${esc(headline(r))}</div>
      <ol class="run-steps">${steps.map((s) => {
        const st = stepState(s);
        const pid = s.proposal_id && s.status === "PENDING" && !(stepProposal(s) && stepProposal(s).status !== "PENDING") ? s.proposal_id : null;
        return `<li class="${esc(st.cls)}" data-asset="${esc(s.action && s.action.asset_id)}">
          <div class="run-step"><span class="chip ${esc(st.cls)}">${esc(st.word)}</span><span class="run-text">${esc(s.text)}</span>${s.release_clock ? `<time>${esc(s.release_clock)}</time>` : ""}</div>
          ${st.detail ? `<div class="run-detail">${esc(st.detail)}</div>` : ""}
          ${pid ? `<div class="plan-btns"><button data-reject="${esc(pid)}">Reject</button><button class="primary hold" data-confirm="${esc(pid)}" title="Press and hold to send this command"><i></i><span>Hold to confirm</span></button></div>` : ""}
        </li>`;
      }).join("")}</ol>` : ""}
    ${offer ? `<div class="run-offer"><div class="run-offer-k">${offer.kind === "stale" ? "This plan is out of date" : "Re-plan offered"}</div>
        <p>${esc(offer.text)}</p>
        ${offer.plan || offer.run ? `<div class="plan-btns"><button class="primary hold" data-offer title="Press and hold to start it"><i></i><span>Hold to run it</span></button></div>` : ""}</div>` : ""}
    ${crewHtml()}
    ${note ? `<div class="run-note" role="alert">${esc(note)}</div>` : ""}
    ${r && ACTIVE.has(r.status) ? `<div class="plan-btns run-ctl">${r.status === "RUNNING" ? `<button data-pause>Pause</button>` : ""}${r.status === "PAUSED" && r.started !== false && !offer ? `<button data-resume>Resume</button>` : ""}${r.status !== "CANCELLING" ? `<button class="hold" data-cancel title="Press and hold to cancel the run"><i></i><span>Hold to cancel run</span></button>` : ""}</div>` : ""}`;

  box.querySelectorAll("[data-asset]").forEach((li) => {
    li.querySelector(".run-text").onclick = () => li.dataset.asset && hooks.select(li.dataset.asset);
  });
  box.querySelectorAll("[data-checkin]").forEach((b) => { b.onclick = () => {
    const c = crew.get(b.dataset.checkin), t = nowT();
    if (c && t != null) { c.last = t; c.missed = false; hooks.log(`${c.who} checked in at ${hooks.nice(c.asset)}.`); }
    saveCrew();
    draw();
  }; });
  box.querySelectorAll("[data-back]").forEach((b) => { b.onclick = () => {
    const c = crew.get(b.dataset.back);
    if (c) hooks.log(`${c.who} back in from ${hooks.nice(c.asset)}.`);
    crew.delete(b.dataset.back);
    returned.add(b.dataset.back);
    saveCrew();
    draw();
  }; });
  box.querySelectorAll("[data-reject]").forEach((b) => { b.onclick = () => decide(b.dataset.reject, "reject"); });
  box.querySelectorAll("[data-confirm]").forEach((b) => holdButton(b, () => decide(b.dataset.confirm, "confirm"), 1500));
  const ob = box.querySelector("[data-offer]");
  if (ob) holdButton(ob, takeOffer, 1500);
  const pb = box.querySelector("[data-pause]");
  if (pb) pb.onclick = () => control("pause");
  const rb = box.querySelector("[data-resume]");
  if (rb) rb.onclick = () => control("resume");
  const cb = box.querySelector("[data-cancel]");
  if (cb) holdButton(cb, () => control("cancel"), 800);
}

// opts.fresh: the answer to this screen's own execute/resume, never an older snapshot.
function setRun(r, opts = {}) {
  if (!r) return;
  // An offered re-plan (not started) never replaces the run on screen: it shows as an offer.
  if (run && r.id !== run.id && r.started === false) return;
  // Snapshots arrive out of order (a slow GET after a ws message). A run never leaves a final
  // status, and never goes back from CANCELLING to RUNNING: drop such an older snapshot.
  // Same run only: a backend restart numbers runs from run_1 again (created_t tells them apart).
  const sameRun = run && r.id === run.id && (!r.created_t || !run.created_t || r.created_t === run.created_t);
  if (sameRun && !opts.fresh) {
    const FINAL = new Set(["DONE", "FAILED", "CANCELLED"]);
    if (FINAL.has(run.status) && !FINAL.has(r.status)) return;
    if (run.status === "CANCELLING" && (r.status === "RUNNING" || r.status === "PAUSED")) return;
  }
  const was = run && run.id === r.id ? run.status : "";
  run = r;
  if (r.status !== was) {
    if (was && r.status !== lastStatus) hooks.log(`Plan run ${r.status.toLowerCase()}${r.reason ? `: ${String(r.reason).replace(/\.+$/, "")}` : ""}.`);
    lastStatus = r.status;
  }
  hooks.progress(r);
  trackCrew(r);
  if (r.status === "PAUSED" && r.replan_run_id && !(offer && offer.kind === "stale" && offer.from === r.replan_run_id)) loadReplan(r.replan_run_id);
  else if (!(offer && offer.kind === "stale")) offer = null;
  draw();
}

async function loadReplan(id) {
  if (offer && offer.run && offer.run.id === id) return;
  try {
    const nr = await call(`/api/runs/${encodeURIComponent(id)}`);
    // The answer may be late: only show it if it is still this run's offer and still on offer.
    if (!run || run.replan_run_id !== id || nr.status !== "PAUSED" || nr.started !== false) { draw(); return; }
    offer = { kind: "replan", run: nr, text: `${nr.summary.split(". ")[0]}. ${hooks.usd(nr.cost_usd)}${run && run.replan_source ? ` (picked by ${run.replan_source === "claude" ? "Jarvis" : "the rules: cheapest that holds"})` : ""}${run && run.excluded && run.excluded.length ? `. Avoids ${run.excluded.map(hooks.nice).join(", ")}.` : "."}${run && run.note ? ` ${run.note}` : ""}` };
  } catch { /* the paused run still shows its reason */ }
  draw();
}

async function takeOffer() {
  const o = offer;
  if (!o) return;
  note = "";
  try {
    if (o.run) {
      const nr = await call(`/api/runs/${encodeURIComponent(o.run.id)}/resume`, { by: "console" });
      offer = null;
      hooks.log(`Started the re-plan: ${nr.summary.split(". ")[0]} (${hooks.usd(nr.cost_usd)}).`);
      setRun(nr, { fresh: true });
    } else if (o.plan) {
      offer = null;
      await execute(o.plan, { approved: true });
      return;
    }
  } catch (e) {
    const fresh = e.body && e.body.plan;
    if (e.status === 409 && e.body && e.body.stale_plan_id && o.run) {
      // The offered re-plan went out of date while it waited. Same commands re-scored on the live
      // state: start that, since it is what the person just held to run. Otherwise offer it.
      if (fresh && sameActions(o.run, fresh) && sameOutcome(o.run, fresh)) { offer = null; await execute(fresh, { approved: true }); return; }
      offer = { kind: "stale", from: o.run.id, plan: fresh || null, text: plain(e.message) + (fresh ? "" : " Nothing needs doing now.") };
    } else note = plain(e.message);
  }
  draw();
}

async function control(verb) {
  if (!run) return;
  note = "";
  try {
    const r = await call(`/api/runs/${encodeURIComponent(run.id)}/${verb}`, { by: "console" });
    hooks.log(`Plan run: ${verb === "cancel" ? "cancelled by hand" : verb === "pause" ? "paused by hand" : "resumed"}.`);
    setRun(r);
  } catch (e) { note = e.status === 409 ? `Can't ${verb} it now: ${e.message}` : e.message; draw(); }
}

async function decide(pid, verb) {
  note = "";
  const s = run && run.steps.find((x) => x.proposal_id === pid);
  try {
    const p = await call(`/api/actions/${encodeURIComponent(pid)}/${verb}`, { by: "console" });
    props.set(p.id, p);
    if (s && verb === "confirm") hooks.log(`CONFIRMED step ${s.n}: ${s.text}`); // a rejection is logged from the run's step status
  } catch (e) {
    note = e.status === 409 ? "That step was already decided on another screen." : e.message;
  }
  refresh();
}

async function refresh() {
  if (!run) return draw();
  const asked = Date.now(), id = run.id;
  try {
    const r = await call(`/api/runs/${encodeURIComponent(id)}`);
    // Another run took the screen while the GET was out (a re-plan started), or the socket
    // brought a newer snapshot of this one: keep what is on screen.
    if (!run || run.id !== id || r.id !== id) return draw();
    if (wsRun === r.id && wsAt > asked) return draw();
    setRun(r);
  } catch { draw(); }
}

// Start a run for a plan. Returns the run, or null (the panel says why).
// opts.approved: the person already held to run exactly this plan (an offer they took).
export async function execute(plan, opts = {}) {
  note = "";
  try {
    const r = await call(`/api/plans/${encodeURIComponent(plan.id)}/execute`, { by: "console" });
    offer = null;
    hooks.log(`APPROVED and started: ${plan.summary.split(". ")[0]} (${hooks.usd(plan.cost ? plan.cost.total_usd : r.cost_usd)}). Each step waits for a confirm.`);
    setRun(r, { fresh: true });
    return r;
  } catch (e) {
    const fresh = e.body && e.body.plan;
    if (e.status === 409 && e.body && e.body.stale_plan_id) {
      // Plans on the list are scored at the scenario's start, so they age by the live minute. If
      // the live state still calls for the same commands, start that once: the person held to
      // approve exactly these. Different commands always go back to the person.
      if (fresh && !opts.retried && sameActions(plan, fresh) && sameOutcome(plan, fresh)) {
        hooks.log(`Re-scored on the live state: same steps, new times. Starting it.`);
        return execute(fresh, { ...opts, retried: true });
      }
      offer = { kind: "stale", plan: fresh || null, text: plain(e.message) + (fresh ? "" : " Nothing needs doing now.") };
      hooks.log(`Plan out of date, not started: ${plain(e.message).split(". From the live state")[0]}.`);
    } else {
      note = e.status === 409 ? e.message : `Not started: ${e.message}`;
      if (e.status === 409 && (!run || !ACTIVE.has(run.status))) {
        // Another run is already going: put it on screen so the person can see and steer it.
        try {
          const j = await call("/api/runs");
          const going = (j.runs || []).filter((x) => ACTIVE.has(x.status) && x.started !== false).pop();
          if (going) setRun(going);
        } catch { /* the note still says why */ }
      }
    }
    draw();
    return null;
  }
}

export function currentRun() { return run; }
// Synchronous: true once GET /api/runs has answered (approving then re-scores on the live state).
export const runsOn = () => supported === true;
// For the handoff: who is still out, and when they last checked in.
export function peopleOut() {
  const t = nowT();
  return [...crew.values()].map((c) => ({ who: c.who, asset: c.asset, lastAgo: t == null ? null : Math.max(0, Math.round(t - c.last)), missed: t != null && t >= c.last + CHECK_MIN }));
}

export function mountRuns(el, h) {
  box = el;
  hooks = h;
  base = h.base || "";
  if (!base) return;
  loadCrew();
  runsSupported();
  liveFeed.on("run", (msg) => {
    const r = msg && msg.run;
    if (!r) return;
    wsAt = Date.now(); wsRun = r.id;
    if (!run || r.id === run.id) setRun(r);
    else if (r.replaces === run.id && r.started !== false) { offer = null; setRun(r); } // the re-plan was started (here, on the phone, or by Jarvis)
    else if (run.replan_run_id === r.id) { offer = null; loadReplan(r.id); }
    else if (ACTIVE.has(r.status) && r.started !== false && !ACTIVE.has(run.status)) setRun(r); // a run started elsewhere (Jarvis)
  });
  liveFeed.on("proposal", (msg) => {
    const p = msg && msg.proposal;
    if (!p) return;
    props.set(p.id, p);
    if (run && run.steps.some((s) => s.proposal_id === p.id)) draw();
  });
  // Safety net when the socket is quiet: re-read the run every 10 s while it is active.
  setInterval(() => { if (run && ACTIVE.has(run.status)) refresh(); }, 10000);
  // Check-in clocks run on field time (15x real time at demo speed): redraw often enough that a
  // missed check-in shows within a field minute. Not under a hold (draw defers itself).
  setInterval(() => { if (crew.size) draw(); }, 4000);
}
