// The gaia page layout, ChatGPT style: threads on the left (collapsible), the conversation in
// the middle, and the live 3D field as the canvas pane on the right (open by default; it can go
// full width, which hides the chat, or close, which gives the chat the room).
//
// Body classes: cs-nav-off (threads hidden), cs-canvas-full (field full width, chat hidden),
// cs-canvas-off (field pane closed). Every change re-sizes the renderer through `resize`.

import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/great-vibes/400.css";

export function makeLayout() {
  const b = document.body;
  const resize = () => requestAnimationFrame(() => dispatchEvent(new Event("resize")));
  if (innerWidth < 1100) b.classList.add("cs-nav-off");
  return {
    nav(v) { b.classList.toggle("cs-nav-off", !v); resize(); },
    navOpen: () => !b.classList.contains("cs-nav-off"),
    // chat shown (split or chat-only) vs the field full width
    chat(v) { b.classList.toggle("cs-canvas-full", !v); if (v === false) b.classList.remove("cs-canvas-off"); resize(); },
    field(v) { b.classList.toggle("cs-canvas-off", !v); if (v === false) b.classList.remove("cs-canvas-full"); resize(); },
    full: () => b.classList.contains("cs-canvas-full"),
    fieldOpen: () => !b.classList.contains("cs-canvas-off"),
  };
}
