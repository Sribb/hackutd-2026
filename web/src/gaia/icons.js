// Small line icons for the gaia console (inline SVG, 18 px, currentColor). No icon font, no CDN.
const svg = (d, extra = "") => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${extra}>${d}</svg>`;

export const ICON = {
  sidebar: svg('<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/>'),
  compose: svg('<path d="M12 20h8"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>'),
  panel: svg('<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M14 4v16"/>'),
  expand: svg('<path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/>'),
  shrink: svg('<path d="M4 14h6v6"/><path d="M20 10h-6V4"/><path d="M14 10l7-7"/><path d="M3 21l7-7"/>'),
  close: svg('<path d="M6 6l12 12"/><path d="M18 6L6 18"/>'),
  mic: svg('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/>'),
  up: svg('<path d="M12 19V5"/><path d="M5 12l7-7 7 7"/>', ' stroke-width="2.2"'),
  log: svg('<path d="M5 4h14v16H5z"/><path d="M9 9h6"/><path d="M9 13h6"/><path d="M9 17h3"/>'),
  field: svg('<path d="M3 18l5-8 4 5 3-4 6 7z"/><circle cx="17" cy="6" r="2"/>'),
  chat: svg('<path d="M4 5h16v11H8l-4 4z"/>'),
};
