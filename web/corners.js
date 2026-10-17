// Corner-marking modal: user clicks the 4 table corners on a frame, in the order
// front-left, front-right, back-right, back-left. Coordinates are natural image pixels.
import { icon } from './icons.js';

export const CORNER_NAMES = ['FRONT-LEFT', 'FRONT-RIGHT', 'BACK-RIGHT', 'BACK-LEFT'];
const SHORT = ['FL', 'FR', 'BR', 'BL'];
const COLORS = ['#ff6b6b', '#f0b429', '#4ade80', '#4cc2ff'];

/** Draw corner markers + polygon into an <svg> laid exactly over an <img>. */
export function drawCornerOverlay(svg, img, corners, { hover = null, big = true } = {}) {
  const nw = img.naturalWidth, nh = img.naturalHeight;
  const dw = img.clientWidth || img.getBoundingClientRect().width;
  if (!nw || !nh || !dw) { svg.innerHTML = ''; return; }
  const k = nw / dw; // natural px per CSS px
  svg.setAttribute('viewBox', `0 0 ${nw} ${nh}`);
  svg.setAttribute('preserveAspectRatio', 'none');
  const pts = corners || [];
  const parts = [];
  if (pts.length >= 2) {
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0]} ${p[1]}`).join(' ') + (pts.length === 4 ? ' Z' : '');
    if (pts.length === 4) parts.push(`<path d="${d}" fill="rgba(76,194,255,0.14)" stroke="none"/>`);
    parts.push(`<path d="${d}" fill="none" stroke="#4cc2ff" stroke-width="${2 * k}" stroke-linejoin="round" stroke-dasharray="${pts.length === 4 ? 'none' : `${6 * k} ${4 * k}`}"/>`);
  }
  if (hover && pts.length > 0 && pts.length < 4) {
    const p = pts[pts.length - 1];
    parts.push(`<line x1="${p[0]}" y1="${p[1]}" x2="${hover[0]}" y2="${hover[1]}" stroke="#4cc2ff" stroke-opacity="0.6" stroke-width="${1.5 * k}" stroke-dasharray="${4 * k} ${4 * k}"/>`);
  }
  // front-edge hint
  if (pts.length >= 2) {
    const [a, b] = pts;
    const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
    parts.push(`<text x="${mx}" y="${my + 22 * k}" font-size="${(big ? 12 : 10) * k}" fill="#ffd27a" text-anchor="middle" font-weight="700" font-family="Inter, system-ui, sans-serif" paint-order="stroke" stroke="rgba(0,0,0,0.8)" stroke-width="${3 * k}">FRONT EDGE</text>`);
  }
  pts.forEach((p, i) => {
    const r = (big ? 11 : 7) * k;
    parts.push(`<circle cx="${p[0]}" cy="${p[1]}" r="${r}" fill="${COLORS[i]}" stroke="#0b1016" stroke-width="${2 * k}"/>`);
    parts.push(`<text x="${p[0]}" y="${p[1] + r * 0.36}" font-size="${r * 1.05}" text-anchor="middle" font-weight="800" fill="#0b1016" font-family="Inter, system-ui, sans-serif">${i + 1}</text>`);
    if (big) parts.push(`<text x="${p[0] + r + 4 * k}" y="${p[1] - r * 0.6}" font-size="${11 * k}" font-weight="700" fill="#fff" font-family="Inter, system-ui, sans-serif" paint-order="stroke" stroke="rgba(0,0,0,0.85)" stroke-width="${3 * k}">${SHORT[i]}</text>`);
  });
  if (hover && pts.length < 4) {
    const r = 14 * k;
    parts.push(`<circle cx="${hover[0]}" cy="${hover[1]}" r="${r}" fill="none" stroke="${COLORS[pts.length]}" stroke-width="${2 * k}"/><path d="M${hover[0] - r * 1.6} ${hover[1]}h${r * 1.1}M${hover[0] + r * 0.5} ${hover[1]}h${r * 1.1}M${hover[0]} ${hover[1] - r * 1.6}v${r * 1.1}M${hover[0]} ${hover[1] + r * 0.5}v${r * 1.1}" stroke="${COLORS[pts.length]}" stroke-width="${1.5 * k}"/>`);
  }
  svg.innerHTML = parts.join('');
}

function tableDiagram(active) {
  // Small schematic: table seen from the camera, front edge at the bottom.
  const P = [[22, 92], [158, 92], [134, 22], [46, 22]];
  const pts = P.map((p) => p.join(',')).join(' ');
  return `<svg viewBox="0 0 180 120" class="cm-diagram">
    <polygon points="${pts}" fill="#18212c" stroke="#5b6b7d" stroke-width="1.5"/>
    <text x="90" y="112" text-anchor="middle" font-size="9" fill="#ffd27a" font-weight="700" letter-spacing="1">FRONT EDGE · YOU / CAMERA</text>
    <text x="90" y="14" text-anchor="middle" font-size="9" fill="#7d8b99" font-weight="700" letter-spacing="1">BACK EDGE</text>
    ${P.map((p, i) => `<circle cx="${p[0]}" cy="${p[1]}" r="${i === active ? 10 : 7}" fill="${i < active ? COLORS[i] : i === active ? COLORS[i] : '#2b3644'}" stroke="${i === active ? '#fff' : '#0b1016'}" stroke-width="${i === active ? 2 : 1}"/><text x="${p[0]}" y="${p[1] + 3.5}" text-anchor="middle" font-size="${i === active ? 10 : 8}" font-weight="800" fill="#0b1016">${i + 1}</text>`).join('')}
  </svg>`;
}

/**
 * opts: { el, frames, frameIndex, corners, dims: {width, depth} | null, onSave(corners, frameIndex), onKeep(corners, frameIndex) }
 */
export function openCornerModal(opts) {
  const el = opts.el;
  let corners = (opts.corners || []).map((p) => [...p]);
  let frameIndex = opts.frameIndex ?? 0;
  let hover = null;

  const frame = () => opts.frames.find((f) => f.index === frameIndex) || opts.frames[0];

  function close() {
    el.classList.add('hidden');
    el.innerHTML = '';
    window.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', redraw);
  }
  function onKey(e) {
    if (e.key === 'Escape') close();
    if ((e.key === 'z' && (e.metaKey || e.ctrlKey)) || e.key === 'Backspace') { corners.pop(); update(); }
  }

  el.innerHTML = `
    <div class="modal-card cm">
      <div class="cm-head">
        <div>
          <div class="eyebrow">${icon('corners')} Calibration · mark table corners</div>
          <div class="cm-instr" id="cmInstr"></div>
        </div>
        <button class="btn btn-ghost" id="cmClose" title="Close (Esc)">${icon('x')}</button>
      </div>
      <div class="cm-body">
        <div class="cm-stage" id="cmStage">
          <div class="cm-imgwrap" id="cmWrap">
            <img id="cmImg" alt="Overview frame" draggable="false">
            <svg id="cmSvg" class="cm-svg"></svg>
          </div>
        </div>
        <aside class="cm-side">
          <div id="cmDiagram"></div>
          <ol class="cm-steps" id="cmSteps"></ol>
          <div class="cm-row">
            <button class="btn btn-sm" id="cmUndo">${icon('undo')} Undo</button>
            <button class="btn btn-sm" id="cmClear">Clear</button>
          </div>
          <div class="cm-frames-label muted small">Frame used for marking</div>
          <div class="cm-frames" id="cmFrames"></div>
          <div class="cm-dims" id="cmDims"></div>
          <div class="cm-actions">
            <button class="btn btn-primary btn-block" id="cmSave">${icon('ruler')} Save calibration → new version</button>
            <button class="btn btn-block" id="cmKeep">Keep corners, close</button>
          </div>
        </aside>
      </div>
    </div>`;
  el.classList.remove('hidden');

  const img = el.querySelector('#cmImg');
  const svg = el.querySelector('#cmSvg');
  const wrap = el.querySelector('#cmWrap');

  function redraw() { drawCornerOverlay(svg, img, corners, { hover }); }

  function update() {
    const n = corners.length;
    el.querySelector('#cmInstr').innerHTML = n < 4
      ? `Click corner <b>${n + 1} of 4</b> — <span class="cm-corner" style="color:${COLORS[n]}">${CORNER_NAMES[n]}</span> <span class="muted">(as seen standing at the front edge)</span>`
      : `<span class="ok-text">${icon('check')} All 4 corners marked.</span> <span class="muted">Undo to adjust, or save.</span>`;
    el.querySelector('#cmDiagram').innerHTML = tableDiagram(Math.min(n, 3) + (n >= 4 ? 1 : 0));
    el.querySelector('#cmSteps').innerHTML = CORNER_NAMES.map((nm, i) => `
      <li class="${i < n ? 'done' : i === n ? 'active' : ''}"><span class="dot" style="--c:${COLORS[i]}">${i + 1}</span>${nm}
      <span class="mono small muted">${corners[i] ? `${Math.round(corners[i][0])}, ${Math.round(corners[i][1])} px` : ''}</span></li>`).join('');
    const d = opts.dims;
    const dimsOk = d && d.width > 0.2 && d.depth > 0.2;
    el.querySelector('#cmDims').innerHTML = dimsOk
      ? `<div class="kv"><span>Measured table</span><b class="mono">${(d.width * 100).toFixed(0)} × ${(d.depth * 100).toFixed(0)} cm</b></div>`
      : `<div class="warn-text small">${icon('warn')} Enter measured table width & depth in the Calibration panel to save.</div>`;
    el.querySelector('#cmSave').disabled = !(n === 4 && dimsOk);
    el.querySelector('#cmUndo').disabled = n === 0;
    wrap.classList.toggle('complete', n === 4);
    redraw();
  }

  function setFrame(i) {
    frameIndex = i;
    img.src = frame().url;
    el.querySelectorAll('.cm-frames img').forEach((t) => t.classList.toggle('sel', +t.dataset.i === i));
  }
  el.querySelector('#cmFrames').innerHTML = opts.frames.map((f) => `<img src="${f.url}" data-i="${f.index}" alt="frame ${f.index}" title="frame ${f.index} · t=${f.t}s">`).join('');
  el.querySelector('#cmFrames').addEventListener('click', (e) => {
    const t = e.target.closest('img');
    if (!t) return;
    if (+t.dataset.i !== frameIndex && corners.length && !confirm('Changing the frame clears the marked corners. Continue?')) return;
    if (+t.dataset.i !== frameIndex) corners = [];
    setFrame(+t.dataset.i);
    update();
  });

  const toNatural = (e) => {
    const r = img.getBoundingClientRect();
    const u = ((e.clientX - r.left) / r.width) * img.naturalWidth;
    const v = ((e.clientY - r.top) / r.height) * img.naturalHeight;
    if (u < 0 || v < 0 || u > img.naturalWidth || v > img.naturalHeight) return null;
    return [Math.round(u * 10) / 10, Math.round(v * 10) / 10];
  };
  wrap.addEventListener('click', (e) => {
    if (corners.length >= 4) return;
    const p = toNatural(e);
    if (!p) return;
    corners.push(p);
    update();
  });
  wrap.addEventListener('mousemove', (e) => { hover = toNatural(e); redraw(); });
  wrap.addEventListener('mouseleave', () => { hover = null; redraw(); });
  img.addEventListener('load', redraw);

  el.querySelector('#cmUndo').onclick = () => { corners.pop(); update(); };
  el.querySelector('#cmClear').onclick = () => { corners = []; update(); };
  el.querySelector('#cmClose').onclick = close;
  el.querySelector('#cmKeep').onclick = () => { opts.onKeep?.(corners.length === 4 ? corners : null, frameIndex); close(); };
  el.querySelector('#cmSave').onclick = () => { opts.onSave?.(corners, frameIndex); close(); };
  el.addEventListener('click', (e) => { if (e.target === el) close(); });
  window.addEventListener('keydown', onKey);
  window.addEventListener('resize', redraw);

  setFrame(frameIndex);
  update();
  return { close };
}
