// Route L (spec §5): view intents the browser answers itself, with no network and no AI.
// match(text, ctx) -> null (send it to the backend: rules, then Claude) or
//   { action, asset_id?, args?, ask?: true }   ask: also send the text to the backend for words
//   { hands: "on" | "off" }  |  { clearWhatif: true }
// Equipment never moves from here: "open the crossover" or "close valve d 3" stays route R,
// where it becomes one PENDING proposal that a person must hold to confirm.

export const NUM_WORDS = { one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10,
  eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18,
  nineteen: 19, twenty: 20, thirty: 30, forty: 40, fifty: 50, sixty: 60, ninety: 90 };

export function normalize(text) {
  let t = " " + String(text || "").toLowerCase().replace(/[?!.,]/g, " ").replace(/-/g, " ").replace(/\s+/g, " ") + " ";
  t = t.replace(/\btwenty (one|two|three|four)\b/g, (_, w) => String(20 + NUM_WORDS[w]));
  for (const [w, n] of Object.entries(NUM_WORDS)) t = t.replace(new RegExp(`\\b${w}\\b`, "g"), String(n));
  return t.replace(/\s+/g, " ");
}

// The asset a phrase names, or the selection for "it/this/that". Only IDs the field has.
export function assetIn(t, ctx) {
  const has = (id) => ctx.hasId(id);
  const tries = [];
  let m;
  if (/\b(crossover|xo)\b/.test(t)) {
    m = /\bxo ?(\d+)\b/.exec(t);
    const xo = m ? `VALVE_XO_${m[1]}` : ctx.ids().find((id) => /^VALVE_XO_/.test(id));
    if (xo) tries.push(xo);
  }
  if ((m = /\bvalve ([a-z]{1,2}) ?(\d+)\b/.exec(t))) tries.push(`VALVE_${m[1].toUpperCase()}_${m[2]}`);
  if ((m = /\b(?:disposal well|swd) ?(\d+)\b/.exec(t))) tries.push(`SWD_${m[1]}`);
  if ((m = /\b(tank|pump|well|truck|yard)s? ?(\d+)\b/.exec(t))) tries.push(`${m[1].toUpperCase()}_${m[2]}`);
  const hit = tries.find(has);
  if (hit) return hit;
  if (/\b(it|its|it's|this|that|this one|that one|the selection|selected)\b/.test(t) && ctx.selected()) return ctx.selected();
  return null;
}

const kindOf = (id) => String(id || "").split("_")[0];

export function match(text, ctx) {
  const t = normalize(text);
  const raw = t.trim();
  if (!raw) return null;
  const id = assetIn(t, ctx);

  // Hands.
  let m = /^(?:turn )?hands? (on|off)$|^turn (on|off) (?:the )?hands?$/.exec(raw);
  if (m) return { hands: m[1] || m[2] };

  // Reset (never ends an approved plan walk or clears a what-if).
  if (/^(?:jarvis )?(reset(?: the)?(?: view)?|home|go home|overview|start over|show (?:me )?everything)$/.test(raw)) return { action: "reset_view" };
  if (/^clear (?:the |all |every )?what ?ifs?$/.test(raw)) return { clearWhatif: true };

  // Time.
  if (/^(?:back to )?(?:now|live)$|^back to now\b|^go live$/.test(raw)) return { action: "set_time", args: { t_min: 0 } };
  m = /^(?:show (?:me )?|go to |jump to )?(?:in |\+ ?)(\d+(?:\.\d+)?) ?(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)(?: from now)?$/.exec(raw);
  if (m) {
    const n = Number(m[1]) * (/^h/.test(m[2]) ? 60 : 1);
    return { action: "set_time", args: { t_min: n } };
  }
  m = /^(?:show (?:me )?|go to |jump to )?at (\d{1,2})(?::(\d{2}))? ?(am|pm|a m|p m)?$/.exec(raw);
  if (m) {
    let h = Number(m[1]) % 12;
    if (m[3] && /p/.test(m[3])) h += 12;
    if (!m[3] && Number(m[1]) === 12) h = 0;
    return { action: "set_time", args: { clock: h * 60 + Number(m[2] || 0) } };
  }

  // Sensors. "Which sensors are stale" needs ages from the backend: route R.
  if (/\bwhich sensors\b|\bsensor ages?\b|\bstale sensors?\b|\bsensors? (?:are )?stale\b/.test(t)) return null;
  if (/\b(?:all|every) (?:the )?sensors?\b|^(?:show )?sensors$/.test(raw)) return { action: "show_sensors", asset_id: "ALL" };
  if (/\bsensors?\b/.test(t) && id) return { action: "show_sensors", asset_id: id };

  // Isolate.
  if (/^(?:isolate|only|just)(?: show)?\b/.test(raw) && id) return { action: "isolate", asset_id: id };

  // Trace pipes.
  if (id && (/\btrace\b/.test(t) || /\bfollow\b/.test(t) || /\bwhere does .* (drain|go|flow)\b/.test(t) || /\bpipes?\b/.test(t))) {
    const direction = /\b(drain|downstream|go|flow)\b/.test(t) ? "downstream" : /\b(feeds?|fills?|upstream|comes? from)\b/.test(t) ? "upstream" : "both";
    return { action: "trace_pipes", asset_id: id, args: { direction } };
  }

  // Collapse. With a valve selected and nothing opened, "close it" is a command: route R.
  if (/^(?:close it|close that|close this|put it back|put that back|collapse(?: it)?)$/.test(raw)) {
    const sel = ctx.selected();
    if (!ctx.opened() && kindOf(sel) === "VALVE" && /^close/.test(raw)) return null;
    return { action: "collapse" };
  }

  // Open (explode). Open or close plus a valve, crossover or XO is equipment: route R.
  m = /^(?:please |jarvis )?(open|close|break down|take apart|explode|show (?:me )?(?:the )?parts(?: of)?)\b/.exec(raw);
  if (m) {
    const verb = m[1];
    if ((verb === "open" || verb === "close") && (kindOf(id) === "VALVE" || /\b(crossover|xo|valve)\b/.test(t))) return null;
    if (verb === "close") return id ? null : { action: "collapse" };
    if (id) return { action: "explode", asset_id: id };
    return null;
  }

  // Show / go to / where is: focus here at once, and ask the backend for the words.
  if (id && /^(?:show(?: me)?|go to|fly to|zoom (?:in )?(?:on|to)|focus(?: on)?|where is|where's|find|look at)\b/.test(raw)) {
    return { action: "focus", asset_id: id, ask: true };
  }
  return null;
}
