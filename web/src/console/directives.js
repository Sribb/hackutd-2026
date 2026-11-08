// UI directives (spec §5): {type:"ui", id?, action, asset_id?, args?, source?, reason?}.
// The console applies directives from its own Jarvis reply at once and skips the /ws echo by id
// (the last 200 ids). Only whitelisted actions run; each one goes through `view`. Nothing here
// can move equipment: try_whatif changes the forecast on screen only.

const WHITELIST = new Set(["focus", "highlight", "explode", "collapse", "isolate", "show_sensors", "trace_pipes",
  "set_time", "reset_view", "open_panel", "try_whatif"]);
const NODE = /^(SENSOR|RTU|PIPE)_[A-Z0-9_]+$/;

export function makeDirectives(H, getView) {
  const seen = [];
  const seenSet = new Set();
  const remember = (id) => {
    if (!id || seenSet.has(id)) return;
    seen.push(id);
    seenSet.add(id);
    if (seen.length > 200) seenSet.delete(seen.shift());
  };

  // Returns the sentence the view said (for a Jarvis bubble), or "".
  function apply(d, { echo = false } = {}) {
    if (!d || !WHITELIST.has(d.action)) return "";
    if (d.id) {
      if (seenSet.has(d.id)) return "";
      remember(d.id);
    } else if (echo && d.action !== "focus") {
      return ""; // an id-less broadcast other than focus is not something we asked for
    }
    const view = getView();
    const args = d.args || {};
    const id = d.asset_id ? String(d.asset_id).toUpperCase() : "";
    const okId = (x) => H.hasId(x);
    let out = null;
    switch (d.action) {
      case "focus": if (okId(id)) out = view.focus(id); break;
      case "highlight": {
        const ids = (args.ids || d.ids || []).map((x) => String(x).toUpperCase()).filter((x) => okId(x) || NODE.test(x)).slice(0, 24);
        out = view.highlight(ids, Number(args.seconds) || 20);
        break;
      }
      case "explode": if (okId(id)) out = view.explode(id); break;
      case "collapse": out = view.collapse(); break;
      case "isolate": if (okId(id)) out = view.isolate(id); break;
      case "show_sensors": out = view.showSensors(id === "ALL" || !id ? "ALL" : id); break;
      case "trace_pipes": if (okId(id)) out = view.tracePipes(id, args.direction || "both"); break;
      case "set_time": out = H.setTimeFrom(args); break;
      case "reset_view": out = H.resetView(); break;
      case "open_panel": out = H.openPanel(args.panel || d.panel, id || null); break;
      case "try_whatif": if (Array.isArray(args.actions)) { H.setActions(args.actions); out = { ok: true, said: "Showing that what-if on the forecast. Nothing was sent." }; } break;
      default: break;
    }
    return (out && out.said) || "";
  }
  return { apply, remember, seen: () => seen.slice() };
}
