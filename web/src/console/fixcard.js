// The ONE fix card (spec §2): sticky above the input, exactly one proposal id at a time.
//
//   PENDING    [Not now] (a plain click rejects) and [Hold to confirm] (fx.js holdButton, 1500 ms;
//              mouse, touch, or Space held; never a hand gesture)
//   SENDING    "Sending to CONN_SIM…", no buttons
//   CONFIRMED  "Done, confirmed by console 1:53 AM", folds into the stream after 5 s
//   FAILED     "Failed: <result>. Nothing changed." (a send that threw is shown as UNCERTAIN)
//   UNCERTAIN  "Sent, not confirmed. The device may have moved. Check the field before trying again."
//              No retry button.
//   REJECTED   "Not done."
//   409        "Already decided on another screen."
//
// This screen sends a confirm for a proposal id at most once. A request that left and never
// answered (timeout, dropped connection, 5xx) is UNCERTAIN, never "failed". A refusal before
// anything was sent (401 wrong PIN, 403, 422) says so and the card can be held again.

import { holdButton, afterHold, isHolding } from "../fx.js";
import { esc } from "./stream.js";
import { usd, clock12, toMin } from "./agents.js";

const HOLD_MS = 1500;
const OPEN = new Set(["PENDING", "SENDING", "UNCERTAIN", "FAILED", "CONFIRMED", "REJECTED", "DECIDED"]);

export function makeFixCard(box, H) {
  const proposals = new Map();
  const local = new Map();     // id -> { state, text?, at? }
  const sentOnce = new Set();
  const effects = new Map();   // id -> text | null (loading) | "" (none)
  const folded = new Set();    // ids whose final state already went to the stream
  const log = [];              // [{ id, status }] for tests: every state the card showed
  let current = null;
  const foldTimers = new Map();

  function take(p, quiet = false) {
    if (!p || !p.id) return;
    const before = proposals.get(p.id);
    proposals.set(p.id, { ...(before || {}), ...p });
    const l = local.get(p.id);
    if (l && l.state === "SENDING" && p.status !== "PENDING" && p.status !== "SENDING") local.delete(p.id);
    if (l && l.state === "NOTE" && p.status !== "PENDING") local.delete(p.id);
    if (!before && p.status === "PENDING" && !current) current = p.id;
    if (!quiet) draw();
  }

  // What the card shows for p: this screen's knowledge first, then the server's word.
  function stateOf(p) {
    const l = local.get(p.id);
    if (l && ["SENDING", "UNCERTAIN", "DECIDED"].includes(l.state)) return l.state;
    if (p.status === "FAILED" && /^Send failed/i.test(String(p.result || ""))) return "UNCERTAIN";
    return p.status;
  }
  const isPending = (p) => stateOf(p) === "PENDING";
  const showable = (p) => OPEN.has(stateOf(p)) && !folded.has(p.id) && (isPending(p) || stateOf(p) === "SENDING" || sentOnce.has(p.id) || local.has(p.id));
  const pendingList = () => [...proposals.values()].filter(isPending).sort((a, b) => ((a.created_t || "") > (b.created_t || "") ? 1 : -1));

  function pick() {
    const cur = current && proposals.get(current);
    if (cur && showable(cur)) return cur;
    const next = pendingList()[0] || [...proposals.values()].find(showable) || null;
    current = next ? next.id : null;
    return next;
  }

  function sourceOf(p) {
    if (p.step && p.steps) return `plan step ${p.step} of ${p.steps}`;
    if (p.source === "phone") return "from phone";
    if (p.source === "jarvis") return "from Jarvis";
    return p.source ? `from ${p.source}` : "";
  }

  function body(p, st) {
    const l = local.get(p.id) || {};
    const by = p.decided_by || "console";
    const when = l.at || H.clockNow();
    switch (st) {
      case "PENDING": {
        const more = pendingList().length - 1;
        return `${l.state === "NOTE" ? `<div class="cs-fx-note" role="alert">${esc(l.text)}</div>` : ""}
          <div class="cs-row">
            <button type="button" class="cs-notnow" data-reject>Not now</button>
            <button type="button" class="primary hold cs-hold" data-hold aria-label="Press and hold for 1.5 seconds to confirm: ${esc(p.text || "")}"><i></i><span>Hold to confirm</span></button>
          </div>
          ${more > 0 ? `<button type="button" class="link cs-more" data-more>+${more} more waiting</button>` : ""}`;
      }
      case "SENDING": return `<div class="cs-fx-state">Sending to ${esc(p.connector_id || "the field")}…</div>`;
      case "CONFIRMED": return `<div class="cs-fx-state good">Done, confirmed by ${esc(by)} ${esc(when)}.${p.result ? ` <span class="muted">${esc(String(p.result))}</span>` : ""}</div>`;
      case "FAILED": return `<div class="cs-fx-state bad">Failed: ${esc(String(p.result || "the connector said no"))}. Nothing changed.</div><div class="cs-row"><button type="button" data-dismiss>OK</button></div>`;
      case "UNCERTAIN": return `<div class="cs-fx-state warn"><b>UNCERTAIN</b> Sent, not confirmed. The device may have moved. Check the field before trying again.${p.result ? ` <span class="muted">${esc(String(p.result))}</span>` : ""}</div><div class="cs-row"><button type="button" data-dismiss>Got it</button></div>`;
      case "REJECTED": return `<div class="cs-fx-state">Not done.</div>`;
      case "DECIDED": return `<div class="cs-fx-state">Already decided on another screen.</div>`;
      default: return "";
    }
  }

  function draw() {
    if (isHolding()) { afterHold(draw); return; } // never swap the button under a held finger
    const p = pick();
    if (!p) { box.hidden = true; box.innerHTML = ""; box._html = ""; box.dataset.id = ""; box.dataset.status = ""; return; }
    const st = stateOf(p);
    if (box.dataset.id !== p.id || box.dataset.status !== st) log.push({ id: p.id, status: st });
    box.hidden = false;
    box.dataset.id = p.id;
    box.dataset.status = st;
    box.className = `cs-fix ${st}`;
    if (st === "PENDING" && !effects.has(p.id)) loadEffect(p); // marks it loading before the first render
    const eff = effects.get(p.id);
    const html = `<div class="cs-fx-top"><span>FIX</span><span>${esc(sourceOf(p))}</span><b>${esc(st === "DECIDED" ? "409" : st)}</b></div>
      <div class="cs-fx-text">${esc(p.text || "")}</div>
      ${p.reason ? `<div class="cs-fx-why">${esc(p.reason)}</div>` : ""}
      ${eff ? `<div class="cs-fx-eff">${esc(eff)}</div>` : eff === null || st === "PENDING" ? `<div class="cs-fx-eff muted"${eff === null ? " data-loading" : ""}>${eff === null ? "Working out the effect…" : ""}</div>` : ""}
      ${body(p, st)}`;
    // A ws echo that changes nothing must not rebuild the card (it would swap the hold button).
    if (box._html === html) return;
    box._html = html;
    box.innerHTML = html;
    const hold = box.querySelector("[data-hold]");
    if (hold) holdButton(hold, () => confirm(p.id), HOLD_MS);
    const rej = box.querySelector("[data-reject]");
    if (rej) rej.onclick = () => reject(p.id);
    const more = box.querySelector("[data-more]");
    if (more) more.onclick = () => { const list = pendingList(); const i = list.findIndex((x) => x.id === p.id); current = list[(i + 1) % list.length].id; draw(); };
    box.querySelectorAll("[data-dismiss]").forEach((b) => { b.onclick = () => fold(p.id, st); });
    if (["CONFIRMED", "REJECTED", "DECIDED"].includes(st) && !foldTimers.has(p.id)) {
      foldTimers.set(p.id, setTimeout(() => fold(p.id, st), st === "CONFIRMED" || st === "DECIDED" ? 5000 : 3000));
    }
  }

  // Into the stream: the final word on this fix, so the card can move on.
  function fold(id, st) {
    if (folded.has(id)) return;
    folded.add(id);
    const p = proposals.get(id);
    const t = p ? p.text || "" : "";
    if (st === "CONFIRMED") H.note(`Done: ${t} Confirmed by ${p.decided_by || "console"} ${(local.get(id) || {}).at || H.clockNow()}.`, [p && p.action && p.action.asset_id].filter(Boolean));
    else if (st === "REJECTED") H.note(`Not done: ${t}`);
    else if (st === "DECIDED") H.note(`Already decided on another screen: ${t}`);
    else if (st === "UNCERTAIN") H.note(`UNCERTAIN: ${t} Sent, not confirmed. Check the field.`);
    else if (st === "FAILED") H.note(`Failed: ${t} Nothing changed.`);
    if (current === id) current = null;
    draw();
  }

  async function loadEffect(p) {
    effects.set(p.id, null);
    const a = p.action;
    let text = "";
    try {
      const r = await H.post("/api/jarvis/preview", { action: a });
      if (r.ok) {
        const out = await r.json();
        text = out.text || "";
      } else if (r.status === 404 || r.status === 405) {
        text = await H.effectFromForecast(a);
      }
    } catch { text = ""; }
    effects.set(p.id, text || "");
    draw();
  }

  async function confirm(id) {
    const p = proposals.get(id);
    if (!p || stateOf(p) !== "PENDING" || sentOnce.has(id)) return;
    sentOnce.add(id);
    local.set(id, { state: "SENDING" });
    draw();
    let r = null;
    try {
      r = await H.post(`/api/actions/${encodeURIComponent(id)}/confirm`, { by: "console" }, 12000);
    } catch {
      // Left this screen and never answered: we can't tell whether it arrived.
      local.set(id, { state: "UNCERTAIN" });
      H.log("alert", `UNCERTAIN fix (sent, not confirmed): ${p.text}`);
      draw();
      return;
    }
    const out = await r.json().catch(() => ({}));
    if (r.ok) {
      // "AT" only remembers when it was decided here; the card shows the server's status.
      local.set(id, { state: "AT", at: clock12(toMin(H.liveClock()) ?? toMin(H.clockNow())) });
      take(out, true);
      if (out.status === "CONFIRMED") H.log("approve", `CONFIRMED fix: ${p.text}`);
      else if (out.status === "UNCERTAIN" || out.status === "FAILED") H.log("alert", `${out.status} fix: ${p.text}`);
    } else if (r.status === 409) {
      local.set(id, { state: "DECIDED" });
      H.refresh();
    } else if (r.status >= 500) {
      local.set(id, { state: "UNCERTAIN" });
      H.log("alert", `UNCERTAIN fix (server error after sending): ${p.text}`);
    } else {
      sentOnce.delete(id);
      const why = r.status === 401 ? "Wrong PIN. Nothing was sent." : r.status === 403 ? "This device can't do that. Nothing was sent."
        : r.status === 429 ? "Too many wrong PINs. Nothing was sent. Wait, then hold again."
        : `${typeof out.detail === "string" ? out.detail : `Refused (${r.status}).`} Nothing was sent.`;
      local.set(id, { state: "NOTE", text: why });
    }
    draw();
  }

  async function reject(id) {
    const p = proposals.get(id);
    if (!p || stateOf(p) !== "PENDING" || sentOnce.has(id)) return;
    try {
      const r = await H.post(`/api/actions/${encodeURIComponent(id)}/reject`, { by: "console" });
      const out = await r.json().catch(() => ({}));
      if (r.ok) { take(out, true); H.log("whatif", `Rejected fix: ${p.text}`); }
      else if (r.status === 409) { local.set(id, { state: "DECIDED" }); H.refresh(); }
      else local.set(id, { state: "NOTE", text: `${typeof out.detail === "string" ? out.detail : `Refused (${r.status}).`} Nothing was sent.` });
    } catch {
      local.set(id, { state: "NOTE", text: "Couldn't reach the backend. Nothing was sent." });
    }
    draw();
  }

  return {
    take, draw,
    pending: () => pendingList(),
    shown: () => (box.hidden ? null : { id: box.dataset.id, status: box.dataset.status }),
    log: () => log.slice(),
    has: (id) => proposals.has(id),
  };
}
