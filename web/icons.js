// Tiny inline SVG icon set (16×16, stroke = currentColor).
const P = {
  sample: '<path d="M2 4.8 8 2l6 2.8v6.4L8 14l-6-2.8z"/><path d="M2 4.8 8 7.6l6-2.8M8 7.6V14"/>',
  extraction: '<path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/><circle cx="8" cy="8" r="2"/>',
  edit: '<path d="M10.5 2.5l3 3L6 13H3v-3z"/><path d="M9 4l3 3"/>',
  correction: '<path d="M2 3h12v8H7l-3.5 3v-3H2z"/><path d="M5 6.2h6M5 8.4h4"/>',
  calibration: '<path d="M1.5 10.5l9-9 4 4-9 9z"/><path d="M4.5 7.5l1.5 1.5M6.5 5.5l1 1M8.5 3.5l1.5 1.5"/>',
  suggestion: '<path d="M6 12.5h4M6.5 14.5h3"/><path d="M8 1.5a4.3 4.3 0 0 0-2.6 7.8c.5.4.8 1 .8 1.6v.1h3.6v-.1c0-.6.3-1.2.8-1.6A4.3 4.3 0 0 0 8 1.5z"/>',
  play: '<path d="M4.5 2.5v11l9-5.5z" fill="currentColor" stroke="none"/>',
  check: '<path d="M3 8.5l3.2 3.2L13 4.8"/>',
  x: '<path d="M4 4l8 8M12 4l-8 8"/>',
  dash: '<path d="M4.5 8h7"/>',
  upload: '<path d="M8 10.5V2M4.5 5.5 8 2l3.5 3.5"/><path d="M2 10.5V14h12v-3.5"/>',
  download: '<path d="M8 2v8.5M4.5 7 8 10.5 11.5 7"/><path d="M2 12v2h12v-2"/>',
  film: '<rect x="1.5" y="3" width="13" height="10" rx="1.5"/><path d="M6.5 6v4l3.5-2z" fill="currentColor"/>',
  image: '<rect x="1.5" y="2.5" width="13" height="11" rx="1.5"/><circle cx="5.5" cy="6" r="1.2"/><path d="M2 12l4-4 3 3 2-2 3.5 3.5"/>',
  map: '<path d="M1.5 3.5 5.5 2l5 1.5 4-1.5v10.5l-4 1.5-5-1.5-4 1.5z"/><path d="M5.5 2v10.5M10.5 3.5V14"/>',
  warn: '<path d="M8 1.8 15 14H1z"/><path d="M8 6.2v3.6M8 11.6v.4"/>',
  info: '<circle cx="8" cy="8" r="6.5"/><path d="M8 7.2v4M8 4.6v.4"/>',
  corners: '<path d="M2 5V2h3M11 2h3v3M14 11v3h-3M5 14H2v-3"/><path d="M4.5 4.5l7 1-1 6-6.5-1z"/>',
  spark: '<path d="M8 1.5v3M8 11.5v3M1.5 8h3M11.5 8h3M3.5 3.5l2 2M10.5 10.5l2 2M12.5 3.5l-2 2M5.5 10.5l-2 2"/>',
  robot: '<rect x="3" y="5" width="10" height="7.5" rx="2"/><path d="M8 2.5V5M6 8.5h.01M10 8.5h.01M1.5 8v2M14.5 8v2"/>',
  bolt: '<path d="M9 1.5 3 9h4.5L7 14.5 13 7H8.5z"/>',
  arrowRight: '<path d="M2.5 8h11M9.5 4l4 4-4 4"/>',
  back: '<path d="M13.5 8h-11M6.5 4l-4 4 4 4"/>',
  lock: '<rect x="3" y="7" width="10" height="7" rx="1.5"/><path d="M5 7V5a3 3 0 0 1 6 0v2"/>',
  ruler: '<rect x="1.5" y="5" width="13" height="6" rx="1"/><path d="M4 5v2.5M6.5 5v1.5M9 5v2.5M11.5 5v1.5"/>',
  undo: '<path d="M4.5 6.5h6a3.5 3.5 0 0 1 0 7H6"/><path d="M7 3.5l-3 3 3 3"/>',
  plus: '<path d="M8 3v10M3 8h10"/>',
};

export function icon(name, cls = '') {
  return `<svg class="ic ${cls}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${P[name] || P.info}</svg>`;
}

export const ORIGIN_ICON = {
  sample: 'sample', extraction: 'extraction', edit: 'edit', correction: 'correction',
  calibration: 'calibration', suggestion: 'suggestion',
};
