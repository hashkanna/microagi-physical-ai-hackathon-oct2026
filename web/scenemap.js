// Client-side top-down map of a CellCheck scene (table frame), rendered as an SVG string.
// Table frame: origin = front-left corner, +x toward RIGHT edge, +y toward BACK edge, metres.
// On screen the FRONT edge (camera side) is at the bottom, so screen-y = -y.

export const PANDA_REACH_M = 0.855;

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const f = (n) => (Math.round(n * 10000) / 10000).toString();

function rgb(c, fallback) {
  if (!c) return fallback;
  return `rgb(${c.map((v) => Math.round(Math.max(0, Math.min(1, v)) * 255)).join(',')})`;
}

/**
 * @param {object} scene  cellcheck.scene/v1
 * @param {object} opts   { px: approx display width, ghost: scene to draw as dashed "before", changed: Set of object ids,
 *                          standalone: include width/height + background for use as an image }
 */
export function renderTopdown(scene, opts = {}) {
  if (!scene) return '';
  const { table, robot, objects = [], task = {} } = scene;
  const W = table.width, D = table.depth;
  const pad = 0.16;
  let minx = -pad, maxx = W + pad, miny = -pad - 0.06, maxy = D + pad;
  const grow = (x, y, r = 0.14) => {
    minx = Math.min(minx, x - r); maxx = Math.max(maxx, x + r);
    miny = Math.min(miny, y - r); maxy = Math.max(maxy, y + r);
  };
  if (robot) grow(robot.x, robot.y, 0.16);
  for (const o of objects) grow(o.x, o.y, Math.max(o.size[0], o.size[1]) / 2 + 0.05);
  const spanX = maxx - minx, spanY = maxy - miny;
  let px = opts.px || 420;
  if (opts.fitH) px = Math.min(px, (opts.fitH * spanX) / spanY);
  const m = spanX / px; // metres per CSS pixel (approx.)
  const fs = (p) => f(p * m);

  const out = [];
  const vb = `${f(minx)} ${f(-maxy)} ${f(spanX)} ${f(spanY)}`;
  const sizeAttr = opts.standalone ? `width="${Math.round(px)}" height="${Math.round(px * spanY / spanX)}"` : '';
  out.push(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="${vb}" ${sizeAttr} class="topdown" font-family="Inter, system-ui, sans-serif">`);
  out.push(`<defs>
    <pattern id="grid10" width="0.1" height="0.1" patternUnits="userSpaceOnUse"><path d="M0.1 0H0V0.1" fill="none" stroke="#223041" stroke-width="${fs(0.6)}"/></pattern>
    <marker id="arr" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="currentColor"/></marker>
    <clipPath id="vbclip"><rect x="${f(minx)}" y="${f(-maxy)}" width="${f(spanX)}" height="${f(spanY)}"/></clipPath>
  </defs>`);
  if (opts.standalone) out.push(`<rect x="${f(minx)}" y="${f(-maxy)}" width="${f(spanX)}" height="${f(spanY)}" fill="#0b1016"/>`);
  out.push(`<rect x="${f(minx)}" y="${f(-maxy)}" width="${f(spanX)}" height="${f(spanY)}" fill="url(#grid10)" opacity="0.7"/>`);

  // Table
  out.push(`<rect x="0" y="${f(-D)}" width="${f(W)}" height="${f(D)}" fill="#18212c" stroke="#5b6b7d" stroke-width="${fs(1.4)}"/>`);

  // Reach region: server-computed IK grid when available, else an approximate circle.
  const rg = opts.reach;
  if (rg && Array.isArray(rg.reachable)) {
    const st = rg.step, cells = [];
    rg.reachable.forEach((row, j) => {
      const cy = rg.y0 + j * st;
      let i = 0;
      while (i < row.length) {
        if (!row[i]) { i++; continue; }
        let k = i;
        while (k + 1 < row.length && row[k + 1]) k++;
        const x0 = rg.x0 + i * st - st / 2, x1 = rg.x0 + k * st + st / 2;
        cells.push(`<rect x="${f(x0)}" y="${f(-(cy + st / 2))}" width="${f(x1 - x0)}" height="${f(st)}"/>`);
        i = k + 1;
      }
    });
    out.push(`<g clip-path="url(#vbclip)" fill="rgba(63,185,80,0.20)" stroke="none">${cells.join('')}</g>`);
  } else if (robot) {
    out.push(`<g clip-path="url(#vbclip)"><circle cx="${f(robot.x)}" cy="${f(-robot.y)}" r="${PANDA_REACH_M}" fill="rgba(76,194,255,0.06)" stroke="#4cc2ff" stroke-opacity="0.55" stroke-width="${fs(1)}" stroke-dasharray="${fs(5)} ${fs(4)}"/></g>`);
  }
  // Edge labels
  const lab = (x, y, t, anchor = 'middle', rot = 0, fill = '#7d8b99') =>
    `<text x="${f(x)}" y="${f(y)}" font-size="${fs(9.5)}" fill="${fill}" text-anchor="${anchor}" letter-spacing="${fs(0.8)}" ${rot ? `transform="rotate(${rot} ${f(x)} ${f(y)})"` : ''} font-weight="600">${esc(t)}</text>`;
  out.push(lab(W / 2, fs(16) * 1, 'FRONT EDGE · CAMERA SIDE'));
  out.push(lab(W / 2, -D - fs(6) * 1, 'BACK EDGE'));
  out.push(lab(-fs(7) * 1, -D / 2, 'LEFT', 'middle', -90));
  out.push(lab(W + fs(7) * 1, -D / 2, 'RIGHT', 'middle', 90));

  // Axes at origin
  const al = Math.min(0.14, W * 0.18);
  out.push(`<g stroke-width="${fs(1.6)}">
    <line x1="0" y1="0" x2="${f(al)}" y2="0" stroke="#ff6b6b" color="#ff6b6b" marker-end="url(#arr)"/>
    <line x1="0" y1="0" x2="0" y2="${f(-al)}" stroke="#4ade80" color="#4ade80" marker-end="url(#arr)"/>
  </g>`);
  out.push(`<text x="${f(al + fs(3) * 1)}" y="${fs(3.5)}" font-size="${fs(9)}" fill="#ff8a8a" font-weight="700">+x</text>`);
  out.push(`<text x="${fs(3)}" y="${f(-al - fs(2) * 1)}" font-size="${fs(9)}" fill="#6ee79a" font-weight="700">+y</text>`);
  out.push(`<circle cx="0" cy="0" r="${fs(2.5)}" fill="#e6edf3"/>`);

  // Ghost (before) objects
  if (opts.ghost) {
    for (const g of opts.ghost.objects || []) {
      const cur = objects.find((o) => o.id === g.id);
      if (cur && cur.x === g.x && cur.y === g.y && cur.yaw_deg === g.yaw_deg && String(cur.size) === String(g.size)) continue;
      out.push(`<g transform="translate(${f(g.x)} ${f(-g.y)}) rotate(${f(-g.yaw_deg)})"><rect x="${f(-g.size[0] / 2)}" y="${f(-g.size[1] / 2)}" width="${f(g.size[0])}" height="${f(g.size[1])}" fill="none" stroke="#f0b429" stroke-opacity="0.75" stroke-width="${fs(1)}" stroke-dasharray="${fs(3)} ${fs(3)}"/></g>`);
    }
  }

  // Task arrow
  const blk = objects.find((o) => o.id === task.object_id);
  const tgt = objects.find((o) => o.id === task.target_id);
  if (blk && tgt) {
    out.push(`<line x1="${f(blk.x)}" y1="${f(-blk.y)}" x2="${f(tgt.x)}" y2="${f(-tgt.y)}" stroke="#f0b429" color="#f0b429" stroke-width="${fs(1.4)}" stroke-dasharray="${fs(4)} ${fs(3)}" marker-end="url(#arr)" opacity="0.9"/>`);
  }

  // Objects
  const changed = opts.changed || new Set();
  for (const o of objects) {
    const [sx, sy] = o.size;
    const isTarget = o.id === task.target_id;
    const hl = changed.has(o.id);
    const drag = opts.draggable ? ` data-obj="${esc(o.id)}" class="dragobj"` : '';
    out.push(`<g${drag} transform="translate(0 0)">`);
    const g = [`<g transform="translate(${f(o.x)} ${f(-o.y)}) rotate(${f(-o.yaw_deg)})">`];
    if (o.kind === 'bin') {
      const wt = o.wall_thickness || 0.008;
      const stroke = hl ? '#f0b429' : isTarget ? '#3fb950' : '#8aa0b6';
      g.push(`<rect x="${f(-sx / 2)}" y="${f(-sy / 2)}" width="${f(sx)}" height="${f(sy)}" fill="${rgb(o.color, '#2b3a4a')}" fill-opacity="0.55" stroke="${stroke}" stroke-width="${fs(isTarget || hl ? 2 : 1.2)}"/>`);
      g.push(`<rect x="${f(-sx / 2 + wt)}" y="${f(-sy / 2 + wt)}" width="${f(Math.max(0, sx - 2 * wt))}" height="${f(Math.max(0, sy - 2 * wt))}" fill="#0d141c" fill-opacity="0.65"/>`);
    } else {
      g.push(`<rect x="${f(-sx / 2)}" y="${f(-sy / 2)}" width="${f(sx)}" height="${f(sy)}" fill="${rgb(o.color, '#e5484d')}" stroke="${hl ? '#f0b429' : '#ffd0d0'}" stroke-width="${fs(hl ? 2 : 1)}"/>`);
    }
    g.push('</g>');
    out.push(g.join(''));
    const ly = o.kind === 'bin' ? -o.y + Math.max(sx, sy) / 2 * 0 + fs(3.5) * 1 : -o.y - sy / 2 - fs(4) * 1;
    const label = o.id + (isTarget ? ' · TARGET' : '');
    out.push(`<text x="${f(o.x)}" y="${f(ly)}" font-size="${fs(9.5)}" fill="${isTarget ? '#7ee787' : '#d6dee6'}" text-anchor="middle" font-weight="700" paint-order="stroke" stroke="#0b1016" stroke-width="${fs(3)}" pointer-events="none">${esc(label)}</text>`);
    out.push('</g>');
  }

  // Robot
  if (robot) {
    const yaw = (robot.yaw_deg * Math.PI) / 180;
    const dx = Math.cos(yaw) * 0.13, dy = Math.sin(yaw) * 0.13;
    out.push(`<line x1="${f(robot.x)}" y1="${f(-robot.y)}" x2="${f(robot.x + dx)}" y2="${f(-(robot.y + dy))}" stroke="#4cc2ff" color="#4cc2ff" stroke-width="${fs(2)}" marker-end="url(#arr)"/>`);
    out.push(`<circle cx="${f(robot.x)}" cy="${f(-robot.y)}" r="0.075" fill="#d8e1ea" stroke="#4cc2ff" stroke-width="${fs(2)}"/>`);
    out.push(`<circle cx="${f(robot.x)}" cy="${f(-robot.y)}" r="0.03" fill="#4b5563"/>`);
    out.push(`<text x="${f(robot.x)}" y="${f(-robot.y + 0.075 + fs(11) * 1)}" font-size="${fs(9.5)}" fill="#8fd8ff" text-anchor="middle" font-weight="700" paint-order="stroke" stroke="#0b1016" stroke-width="${fs(3)}">PANDA</text>`);
  }
  out.push('</svg>');
  return out.join('');
}

export function svgDataUrl(svg) {
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}
