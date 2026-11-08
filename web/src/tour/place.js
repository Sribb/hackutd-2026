// Where the tour card goes, and the rings around what it explains (#158).
//
//   visible(targets)          the targets (selectors or functions) that are on screen
//   drawRings(rings, els)     one soft ring per element (rings is the tour's pool of divs)
//   placeCard(card, softEls)  never on the time bar (where it sits when it is up, even while it
//                             is tucked away, and its on-screen box), the composer, or #154's
//                             alert card and monitoring strip; preferably not on softEls. With no room anywhere it
//                             folds to its title and buttons (.mini).

const $ = (sel) => document.querySelector(sel);

export function visible(targets) {
  return targets.map((t) => (typeof t === "function" ? t() : $(t))).filter((el) => {
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && getComputedStyle(el).visibility !== "hidden";
  });
}

const clipToView = (r) => { const left = Math.max(2, r.left), top = Math.max(2, r.top); return { left, top, width: Math.min(innerWidth - 2, r.right) - left, height: Math.min(innerHeight - 2, r.bottom) - top }; };

export function drawRings(rings, list) {
  while (rings.length < list.length) { const r = document.createElement("div"); r.className = "gt-ring"; r.setAttribute("aria-hidden", "true"); document.body.appendChild(r); rings.push(r); }
  rings.forEach((ring, k) => {
    const el = list[k];
    ring.hidden = !el;
    if (!el) return;
    const r = clipToView(el.getBoundingClientRect());
    Object.assign(ring.style, { left: `${r.left - 4}px`, top: `${r.top - 4}px`, width: `${r.width + 8}px`, height: `${r.height + 8}px` });
  });
}

function hardRects() {
  const out = [];
  const tb = $(".timebar"), st = $("#stage");
  if (tb && st) {
    const s = st.getBoundingClientRect();
    if (s.width > 0) {
      out.push({ left: s.left + tb.offsetLeft, top: s.top + tb.offsetTop, right: s.left + tb.offsetLeft + tb.offsetWidth, bottom: s.top + tb.offsetTop + tb.offsetHeight });
      const b = tb.getBoundingClientRect(); // its on-screen box, clipped to the stage it lives in
      const c = { left: Math.max(b.left, s.left), top: Math.max(b.top, s.top), right: Math.min(b.right, s.right), bottom: Math.min(b.bottom, s.bottom) };
      if (c.right > c.left && c.bottom > c.top) out.push(c);
    }
  }
  for (const sel of [".cs-dock", "#csAsk", "#gaiaNotify:not([hidden])", ".gm-strip"]) {
    const el = $(sel);
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && getComputedStyle(el).display !== "none") out.push(r);
  }
  return out;
}
const hits = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

export function placeCard(card, softEls) {
  if (card.classList.contains("pill")) { Object.assign(card.style, { left: "8px", top: "8px", maxHeight: "" }); card.classList.remove("mini"); return; }
  card.classList.remove("mini");
  let best = spot(card, softEls, 110);
  // A screen with no room left (a phone with the alert card up): just the title and the buttons.
  if (!best) { card.classList.add("mini"); best = spot(card, softEls, 0); }
  if (!best) best = { x: Math.max(8, (innerWidth - card.offsetWidth) / 2), y: 8, room: Math.max(110, innerHeight - 16) };
  Object.assign(card.style, { left: `${Math.round(best.x)}px`, top: `${Math.round(best.y)}px`, maxHeight: `${Math.floor(Math.min(best.room, innerHeight - 16))}px` });
}

// The best spot with at least `min` px of room (0: room for the whole card), or null.
function spot(card, softEls, min) {
  card.style.maxHeight = "none";
  const w = card.offsetWidth, h = card.offsetHeight, vw = innerWidth, vh = innerHeight;
  const need = min || h;
  const hard = hardRects();
  const softR = softEls.map((el) => el.getBoundingClientRect()).filter((r) => r.width * r.height < vw * vh * 0.3);
  const st = $("#stage") && $("#stage").getBoundingClientRect();
  const chat = $("#csJarvis") && $("#csJarvis").getBoundingClientRect();
  const spots = [];
  // In the field, under the status line and #154's monitoring strip.
  const strip = $(".gm-strip"), sb = strip && strip.getBoundingClientRect();
  const fy = Math.max(st ? st.top + 58 : 0, sb && sb.height > 0 && getComputedStyle(strip).display !== "none" ? sb.bottom + 10 : 0);
  if (st && st.width > w * 0.8) spots.push([st.right - w - 14, fy], [st.left + 14, fy]);
  if (chat && chat.width > 0) spots.push([chat.left + (chat.width - w) / 2, chat.top + 56], [chat.left + (chat.width - w) / 2, chat.top + 8]);
  spots.push([(vw - w) / 2, 10], [vw - w - 10, 10], [10, 10]);
  let best = null;
  for (const [x0, y0] of spots) {
    const x = Math.max(8, Math.min(vw - w - 8, x0)), y = Math.max(8, Math.min(vh - 120, y0));
    // Room down to the first hard box under this spot (or the bottom of the window).
    let room = vh - 8 - y;
    let blocked = false;
    for (const r of hard) {
      if (!(x < r.right && r.left < x + w)) continue;
      if (r.top <= y && r.bottom > y) blocked = true;
      else if (r.top > y) room = Math.min(room, r.top - 8 - y);
    }
    if (blocked || room < need) continue;
    const fits = room >= h;
    const box = { left: x, top: y, right: x + w, bottom: y + Math.min(h, room) };
    const clean = !softR.some((r) => hits(box, r));
    const score = (fits ? 0 : 2) + (clean ? 0 : 1);
    if (!best || score < best.score || (score === best.score && room > best.room && !fits)) best = { x, y, room, score };
    if (score === 0) break;
  }
  return best;
}
