// In-memory fake CellCheck backend, used only when the page URL has ?mock=1.
// Delete this file (and the import in api.js) to remove it. Images are generated SVG data URIs.
import { renderTopdown, svgDataUrl, PANDA_REACH_M } from './scenemap.js';

const qs = new URLSearchParams(location.search);
const OPENAI = qs.get('openai') !== '0';
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const clone = (o) => JSON.parse(JSON.stringify(o));
const now = () => new Date().toISOString().replace(/\.\d+Z$/, 'Z');
const rid = (p) => p + Math.random().toString(16).slice(2, 6);

class MockError extends Error {
  constructor(status, detail) { super(detail); this.status = status; this.detail = detail; }
}

// ---------------------------------------------------------------- scenes
function baseScene(variant = 'baseline') {
  const P = (keys, tag) => Object.fromEntries(keys.map((k) => [k, tag]));
  const s = {
    schema_version: 'cellcheck.scene/v1',
    name: variant === 'far_bin' ? 'wide bench' : 'demo bench',
    notes: [
      'Table height assumed 0.75 m (not visible in frames).',
      'Bin wall thickness defaults to 8 mm.',
      'Block mass 0.10 kg and friction 1.0 are defaults, not measured.',
      'Robot base placed at the back edge, facing the front edge (assumed mount).',
    ],
    table: { width: 1.0, depth: 0.7, height: 0.75, thickness: 0.04, friction: 0.8, provenance: { width: 'inferred', depth: 'inferred', height: 'default' } },
    robot: { model: 'franka_panda', x: 0.5, y: 0.62, yaw_deg: -90, provenance: P(['x', 'y', 'yaw_deg'], 'default') },
    objects: [
      { id: 'block_a', kind: 'block', label: 'red cube', x: 0.46, y: 0.2, yaw_deg: 12, size: [0.05, 0.05, 0.05], mass: 0.1, friction: 1.0, wall_thickness: null, color: [0.86, 0.22, 0.24], provenance: { x: 'inferred', y: 'inferred', yaw_deg: 'inferred', size: 'inferred', mass: 'default', friction: 'default' } },
      { id: 'bin_a', kind: 'bin', label: 'grey tray', x: 0.18, y: 0.3, yaw_deg: 0, size: [0.22, 0.16, 0.07], mass: null, friction: null, wall_thickness: 0.008, color: [0.45, 0.5, 0.56], provenance: { x: 'inferred', y: 'inferred', yaw_deg: 'inferred', size: 'inferred', wall_thickness: 'default' } },
      { id: 'bin_b', kind: 'bin', label: 'blue tray', x: 0.78, y: 0.3, yaw_deg: -8, size: [0.2, 0.15, 0.08], mass: null, friction: null, wall_thickness: 0.008, color: [0.2, 0.42, 0.85], provenance: { x: 'inferred', y: 'inferred', yaw_deg: 'inferred', size: 'inferred', wall_thickness: 'default' } },
    ],
    task: { type: 'pick_and_place', object_id: 'block_a', target_id: 'bin_b' },
  };
  if (variant === 'far_bin') {
    s.table.width = 1.4; s.table.depth = 0.8;
    s.robot.x = 0.22; s.robot.y = 0.72;
    s.objects[0].x = 0.42; s.objects[0].y = 0.3;
    s.objects[1].x = 0.2; s.objects[1].y = 0.22;
    s.objects[2].x = 1.22; s.objects[2].y = 0.18;
  }
  return s;
}

// ---------------------------------------------------------------- tiny 3D renderer (SVG)
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (a) => { const l = Math.hypot(...a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };

function camera(eye, target, fov, W, H) {
  const fw = norm(sub(target, eye));
  const r = norm(cross(fw, [0, 0, 1]));
  const u = cross(r, fw);
  const foc = H / 2 / Math.tan((fov * Math.PI) / 360);
  return { eye, proj: (p) => { const d = sub(p, eye); const z = dot(d, fw); return [W / 2 + (foc * dot(d, r)) / z, H / 2 - (foc * dot(d, u)) / z, z]; } };
}

function shade(rgb, k) { return `rgb(${rgb.map((v) => Math.round(Math.min(255, v * 255 * k))).join(',')})`; }

function boxFaces(c, s, yawDeg, color, extra = {}) {
  const y = (yawDeg * Math.PI) / 180, cy = Math.cos(y), sy = Math.sin(y);
  const [hx, hy, hz] = s.map((v) => v / 2);
  const pt = (dx, dy, dz) => [c[0] + cy * dx - sy * dy, c[1] + sy * dx + cy * dy, c[2] + dz];
  const v = [pt(-hx, -hy, -hz), pt(hx, -hy, -hz), pt(hx, hy, -hz), pt(-hx, hy, -hz), pt(-hx, -hy, hz), pt(hx, -hy, hz), pt(hx, hy, hz), pt(-hx, hy, hz)];
  const faces = [
    { i: [4, 5, 6, 7], n: [0, 0, 1], k: 1.0, top: true },
    { i: [0, 1, 5, 4], n: [sy, -cy, 0], k: 0.78 },
    { i: [1, 2, 6, 5], n: [cy, sy, 0], k: 0.62 },
    { i: [2, 3, 7, 6], n: [-sy, cy, 0], k: 0.55 },
    { i: [3, 0, 4, 7], n: [-cy, -sy, 0], k: 0.7 },
  ];
  return faces.map((fc) => ({ pts: fc.i.map((j) => v[j]), n: fc.n, fill: shade(color, fc.k), top: fc.top, ...extra, inner: fc.top && extra.inner ? extra.inner : null }));
}

function render3d(scene, cam, W, H, { bg = true, label = '' } = {}) {
  const { table, robot, objects } = scene;
  const faces = [];
  const T = table.width, Dp = table.depth, Hh = table.height;
  faces.push(...boxFaces([T / 2, Dp / 2, -0.02], [T, Dp, 0.04], 0, [0.62, 0.5, 0.38]));
  for (const [lx, ly] of [[0.04, 0.04], [T - 0.04, 0.04], [T - 0.04, Dp - 0.04], [0.04, Dp - 0.04]]) faces.push(...boxFaces([lx, ly, -Hh / 2 - 0.02], [0.05, 0.05, Hh - 0.04], 0, [0.3, 0.3, 0.32]));
  for (const o of objects) {
    const col = o.color || (o.kind === 'bin' ? [0.4, 0.45, 0.5] : [0.85, 0.2, 0.2]);
    const inner = o.kind === 'bin' ? { wt: o.wall_thickness || 0.008, o } : null;
    faces.push(...boxFaces([o.x, o.y, o.size[2] / 2], o.size, o.yaw_deg, col, { inner }));
  }
  if (robot) {
    const yw = (robot.yaw_deg * Math.PI) / 180, fx = Math.cos(yw), fy = Math.sin(yw);
    const W8 = [0.93, 0.94, 0.95], G = [0.25, 0.27, 0.3];
    const yd = robot.yaw_deg;
    faces.push(...boxFaces([robot.x, robot.y, 0.03], [0.2, 0.2, 0.06], yd, G));
    faces.push(...boxFaces([robot.x, robot.y, 0.25], [0.12, 0.12, 0.38], yd, W8));
    faces.push(...boxFaces([robot.x + fx * 0.17, robot.y + fy * 0.17, 0.46], [0.36, 0.1, 0.1], yd, W8.map((v) => v * 0.97)));
    faces.push(...boxFaces([robot.x + fx * 0.33, robot.y + fy * 0.33, 0.3], [0.09, 0.09, 0.28], yd, W8));
    faces.push(...boxFaces([robot.x + fx * 0.33, robot.y + fy * 0.33, 0.13], [0.06, 0.16, 0.05], yd, G));
  }
  const vis = [];
  for (const fc of faces) {
    const cen = fc.pts.reduce((a, p) => [a[0] + p[0] / 4, a[1] + p[1] / 4, a[2] + p[2] / 4], [0, 0, 0]);
    if (dot(fc.n, sub(cam.eye, cen)) <= 0) continue;
    vis.push({ ...fc, d: Math.hypot(...sub(cam.eye, cen)) });
  }
  vis.sort((a, b) => b.d - a.d);
  const out = [`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" font-family="Inter, system-ui, sans-serif">`];
  if (bg) out.push(`<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3b3f45"/><stop offset="0.55" stop-color="#2a2d31"/><stop offset="0.56" stop-color="#4a4038"/><stop offset="1" stop-color="#2f2924"/></linearGradient></defs><rect width="${W}" height="${H}" fill="url(#g)"/>`);
  else out.push(`<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1b2633"/><stop offset="1" stop-color="#0c1117"/></linearGradient></defs><rect width="${W}" height="${H}" fill="url(#g)"/>`);
  const poly = (pts, fill, extra = '') => `<polygon points="${pts.map((p) => { const q = cam.proj(p); return q[0].toFixed(1) + ',' + q[1].toFixed(1); }).join(' ')}" fill="${fill}" ${extra}/>`;
  for (const fc of vis) {
    out.push(poly(fc.pts, fc.fill, 'stroke="rgba(0,0,0,0.35)" stroke-width="0.8" stroke-linejoin="round"'));
    if (fc.inner) {
      const { o, wt } = fc.inner;
      const y = (o.yaw_deg * Math.PI) / 180, c = Math.cos(y), s = Math.sin(y);
      const hx = o.size[0] / 2 - wt, hy = o.size[1] / 2 - wt, z = o.size[2];
      const pts = [[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy]].map(([dx, dy]) => [o.x + c * dx - s * dy, o.y + s * dx + c * dy, z]);
      out.push(poly(pts, 'rgba(10,14,20,0.72)'));
    }
  }
  if (label) out.push(`<text x="16" y="${H - 16}" font-size="15" fill="rgba(255,255,255,0.75)" font-weight="600">${label}</text>`);
  out.push('</svg>');
  return out.join('');
}

function frameSvg(scene, i, n) {
  const W = 960, H = 540;
  const t = i / Math.max(1, n - 1);
  const T = scene.table.width, D = scene.table.depth;
  const cam = camera([T / 2 + (t - 0.5) * 0.7, -0.62 - 0.08 * Math.sin(t * 3), 0.62 + 0.05 * t], [T / 2, D * 0.45, -0.05], 62, W, H);
  return render3d(scene, cam, W, H, { label: `MOCK FRAME ${String(i).padStart(2, '0')} · t=${(i * 1.5).toFixed(1)} s` });
}

function previewSvg(scene, v) {
  const W = 960, H = 540, T = scene.table.width, D = scene.table.depth;
  const cam = camera([T / 2 - 0.55, -0.85, 1.05], [T / 2, D * 0.55, 0.05], 55, W, H);
  return render3d(scene, cam, W, H, { bg: false, label: `MOCK RENDER · scene v${v}` });
}

// ---------------------------------------------------------------- state
const db = { projects: {}, runs: {} };

function newProject({ source, name, scene, frames = 6, origin = 'sample', summary }) {
  const id = rid('p_') + rid('');
  const p = {
    id, name, created_at: now(), source,
    frameScene: scene || baseScene(),
    frames: [], overview_frame: 2, extraction: null, calibration: null,
    versions: [], scenes: {}, current_version: null, runs: [],
    metrics: { generation_s: null, manual_corrections: 0 },
  };
  for (let i = 0; i < frames; i++) p.frames.push({ index: i, url: svgDataUrl(frameSvg(p.frameScene, i, frames)), t: +(i * 1.5).toFixed(1) });
  if (scene) addVersion(p, scene, origin, summary || 'Initial scene', false);
  db.projects[id] = p;
  return p;
}

function addVersion(p, scene, origin, summary, manual) {
  const v = (p.current_version || 0) + 1;
  p.versions.push({ version: v, created_at: now(), origin, summary, manual });
  p.scenes[v] = clone(scene);
  p.current_version = v;
  if (manual) p.metrics.manual_corrections += 1;
  return v;
}

function projectView(p) {
  const out = clone({ ...p, scene: p.current_version ? p.scenes[p.current_version] : null });
  delete out.scenes; delete out.frameScene;
  out.runs = p.runs.map((r) => runSummary(db.runs[r]));
  return out;
}
function runSummary(r) {
  return { id: r.id, version: r.version, status: r.status, stage: r.stage, success: r.success, created_at: r.created_at, is_stale: r.version !== db.projects[r.project_id].current_version };
}

// Seed: a demo project with history (v1 extraction, v2 correction, v3 edit) and runs.
function seed() {
  const s1 = baseScene('baseline');
  s1.objects[2].x = 1.0; s1.objects[2].y = 0.08; // v1: bin too far
  s1.table.width = 1.2;
  s1.robot.x = 0.25;
  const p = newProject({ source: 'fixture', name: 'kitchen-bench capture', scene: null, frames: 8 });
  p.frameScene = s1;
  p.frames = p.frames.map((fr, i) => ({ ...fr, url: svgDataUrl(frameSvg(s1, i, 8)) }));
  p.extraction = { mode: 'fixture', model: null, elapsed_s: 0.4, raw_url: '#', warnings: ['Table back-right corner partly occluded in frames 5–7.'] };
  addVersion(p, s1, 'extraction', 'Initial scene from fixture extraction', false);
  p.metrics.generation_s = 8.2;
  const s2 = clone(s1); s2.objects[2].x = 0.8; s2.objects[2].y = 0.22; s2.objects[2].provenance.x = 'user_confirmed'; s2.objects[2].provenance.y = 'user_confirmed';
  addVersion(p, s2, 'correction', "Move bin_b 20 cm toward the table's left edge", true);
  const s3 = clone(s2); s3.table.width = 1.0; s3.table.depth = 0.7; s3.table.provenance.width = 'measured'; s3.table.provenance.depth = 'measured';
  s3.robot.x = 0.5;
  addVersion(p, s3, 'calibration', 'Measured table 100 × 70 cm, 4 corners marked', false);
  p.calibration = { corners_px: [[248, 392], [716, 392], [628, 228], [334, 228]], frame_index: 2, table_width: 1.0, table_depth: 0.7 };
  makeDoneRun(p, 1, -60);
  makeDoneRun(p, 3, -10);
  db.seedId = p.id;
}

// ---------------------------------------------------------------- run simulation
const STAGES = ['compiling', 'settling', 'planning', 'executing', 'rendering'];

function evaluate(scene) {
  const blk = scene.objects.find((o) => o.id === scene.task.object_id);
  const tgt = scene.objects.find((o) => o.id === scene.task.target_id);
  const r = scene.robot;
  const dT = Math.hypot(tgt.x - r.x, tgt.y - r.y);
  const dB = Math.hypot(blk.x - r.x, blk.y - r.y);
  const limit = 0.8;
  const checks = [
    { id: 'compiled', label: 'Scene compiled', status: 'pass', detail: 'MJCF compiled: 15 bodies, 42 geoms, 9 actuators' },
    { id: 'settled', label: 'Scene settled', status: 'pass', detail: 'max drift 0.4 mm after 1.0 s' },
  ];
  if (dT > limit || dB > limit) {
    const far = dT > limit ? tgt : blk;
    const d = Math.max(dT, dB);
    checks.push({ id: 'reachable', label: 'Required targets reachable', status: 'fail', detail: `5/8 waypoints; place approach above ${far.id} is ${((d - limit) * 100).toFixed(0)} cm beyond the reach envelope` });
    checks.push({ id: 'collisions', label: 'No unexpected collisions', status: 'skip', detail: 'Not evaluated — execution aborted at planning' });
    checks.push({ id: 'placement', label: 'Block placed in target bin', status: 'fail', detail: `${blk.id} still on table at (${blk.x.toFixed(2)}, ${blk.y.toFixed(2)})` });
    const dx = r.x - far.x, dy = r.y - far.y;
    const k = (d - 0.62) / d;
    const mx = +(dx * k).toFixed(3), my = +(dy * k).toFixed(3);
    const parts = [];
    if (Math.abs(mx) >= 0.01) parts.push(`${Math.round(Math.abs(mx) * 100)} cm toward the ${mx < 0 ? 'left' : 'right'} edge`);
    if (Math.abs(my) >= 0.01) parts.push(`${Math.round(Math.abs(my) * 100)} cm toward the ${my > 0 ? 'back' : 'front'} edge`);
    const text = `Move ${far.id}${far.label ? ` (${far.label})` : ''} ${parts.join(' and ')}`;
    const ops = [{ target: far.id, field: 'x', op: 'set', value: +(far.x + mx).toFixed(3) }, { target: far.id, field: 'y', op: 'set', value: +(far.y + my).toFixed(3) }];
    return {
      checks, success: false,
      failure_reason: `${far.id} centre is ${d.toFixed(2)} m from the robot base; Panda reach ≈ ${PANDA_REACH_M.toFixed(2)} m minus approach margin`,
      suggestion: { text, patch: { ops, summary: text }, object_id: far.id, moved_m: +Math.hypot(mx, my).toFixed(3), verified: 'IK plan reachable for all 6 motion targets' },
      halted: 'executing',
      block_final: [blk.x, blk.y, blk.size[2] / 2],
    };
  }
  checks.push({ id: 'reachable', label: 'Required targets reachable', status: 'pass', detail: '8/8 waypoints, worst IK error 1.2 mm' });
  checks.push({ id: 'collisions', label: 'No unexpected collisions', status: 'pass', detail: 'Only expected gripper–block contacts; peak force 11.8 N' });
  checks.push({ id: 'placement', label: 'Block placed in target bin', status: 'pass', detail: `Block centre 3.4 cm inside ${tgt.id} walls, resting on bin floor` });
  return { checks, success: true, failure_reason: null, suggestion: null, block_final: [tgt.x + 0.004, tgt.y - 0.006, 0.033] };
}

function makeDoneRun(p, version, ageMin = 0) {
  const r = { id: rid('r_'), project_id: p.id, version, status: 'done', stage: 'done', created_at: new Date(Date.now() + ageMin * 60000).toISOString().replace(/\.\d+Z$/, 'Z') };
  finishRun(r, p);
  db.runs[r.id] = r;
  p.runs.push(r.id);
  return r;
}

function finishRun(r, p) {
  const scene = p.scenes[r.version];
  const ev = evaluate(scene);
  Object.assign(r, {
    status: 'done', stage: 'done', elapsed_s: +(12 + Math.random() * 5).toFixed(1),
    checks: ev.checks, success: ev.success, failure_reason: ev.failure_reason, suggestion: ev.suggestion,
    video_url: null, poster_url: previewUrl(p.id, r.version),
    metrics: { sim_time_s: ev.success ? 9.5 : 3.2, block_final: ev.block_final.map((v) => +v.toFixed(3)), max_contact_force: ev.success ? 11.8 : 0, ...(ev.halted ? { halted_before_stage: ev.halted } : {}) },
    evidence_url: `/api/runs/${r.id}/bundle.zip`,
  });
}

function startRun(p, version) {
  const r = { id: rid('r_'), project_id: p.id, version, status: 'queued', stage: 'compiling', created_at: now(), elapsed_s: 0, checks: [], success: null, failure_reason: null, suggestion: null, video_url: null, poster_url: null, metrics: null };
  db.runs[r.id] = r;
  p.runs.push(r.id);
  const t0 = Date.now();
  let i = 0;
  const tick = () => {
    r.elapsed_s = +((Date.now() - t0) / 1000).toFixed(1);
    if (i < STAGES.length) { r.status = 'running'; r.stage = STAGES[i++]; setTimeout(tick, 900); }
    else finishRun(r, p);
  };
  setTimeout(tick, 500);
  return r;
}

// ---------------------------------------------------------------- correction "parser" (rules)
function parseCorrection(scene, text) {
  const t = text.toLowerCase();
  const bins = scene.objects.filter((o) => o.kind === 'bin');
  const block = scene.objects.find((o) => o.kind === 'block');
  let target = null;
  for (const o of scene.objects) if (t.includes(o.id.replace('_', ' ')) || t.includes(o.id) || (o.label && t.includes(o.label))) target = o;
  if (!target && /target bin|destination/.test(t)) target = scene.objects.find((o) => o.id === scene.task.target_id);
  if (!target && /other bin|source bin|first bin/.test(t)) target = bins.find((b) => b.id !== scene.task.target_id);
  if (!target && /block|cube/.test(t)) target = block;
  const m = t.match(/(-?\d+(?:\.\d+)?)\s*(cm|centimet(?:er|re)s?|mm|m\b|met(?:er|re)s?)/);
  const deg = t.match(/(-?\d+(?:\.\d+)?)\s*(?:°|deg)/);
  if (/table/.test(t) && /(wide|width|deep|depth)/.test(t) && m) {
    const val = unit(m);
    return { ops: [{ target: 'table', field: /(wide|width)/.test(t) ? 'width' : 'depth', op: 'set', value: val }], explanation: `Set the table ${/(wide|width)/.test(t) ? 'width' : 'depth'} to ${val.toFixed(2)} m (measured).` };
  }
  if (!target) return null;
  if (/rotat|turn/.test(t) && deg) {
    const v = parseFloat(deg[1]);
    return { ops: [{ target: target.id, field: 'yaw_deg', op: 'add', value: v }], explanation: `Rotate ${target.id} by ${v}° about the vertical axis.` };
  }
  if (!m) return null;
  const d = unit(m);
  const dir = [['left', 'x', -1], ['right', 'x', 1], ['front', 'y', -1], ['back', 'y', 1], ['robot', null, 0]].find(([w]) => t.includes(w));
  if (!dir || !dir[1]) return null;
  return {
    ops: [{ target: target.id, field: dir[1], op: 'add', value: dir[2] * d }],
    explanation: `"Toward the ${dir[0]} edge" is ${dir[2] < 0 ? '−' : '+'}${dir[1]} in the table frame, so ${target.id}.${dir[1]} changes by ${(dir[2] * d * 100).toFixed(0)} cm. Nothing else is modified.`,
  };
  function unit(mm) {
    const v = parseFloat(mm[1]);
    if (mm[2].startsWith('mm')) return v / 1000;
    if (mm[2].startsWith('c')) return v / 100;
    return v;
  }
}

function applyOps(scene, ops) {
  const s = clone(scene);
  for (const op of ops) {
    const ent = op.target === 'table' ? s.table : op.target === 'robot' ? s.robot : op.target === 'task' ? s.task : s.objects.find((o) => o.id === op.target);
    if (!ent) throw new MockError(422, `Unknown object '${op.target}'`);
    const cur = ent[op.field];
    ent[op.field] = op.op === 'add' ? +(cur + op.value).toFixed(4) : op.value;
    if (op.field === 'yaw_deg') ent.yaw_deg = ((ent.yaw_deg + 180) % 360 + 360) % 360 - 180;
    if (ent.provenance) ent.provenance[op.field] = 'user_confirmed';
  }
  return s;
}

// ---------------------------------------------------------------- images
const imgCache = {};
export function previewUrl(pid, v) {
  const k = `p:${pid}:${v}`;
  const p = db.projects[pid];
  if (!p || !p.scenes[v]) return '';
  return (imgCache[k] ||= svgDataUrl(previewSvg(p.scenes[v], v)));
}
export function topdownUrl(pid, v) {
  const k = `t:${pid}:${v}`;
  const p = db.projects[pid];
  if (!p || !p.scenes[v]) return '';
  return (imgCache[k] ||= svgDataUrl(renderTopdown(p.scenes[v], { px: 900, standalone: true })));
}

// ---------------------------------------------------------------- router
export async function upload(file, onProgress) {
  for (let i = 1; i <= 20; i++) { await delay(60); onProgress?.(i / 20); }
  await delay(900);
  const p = newProject({ source: 'upload', name: (file?.name || 'phone-video.mp4').replace(/\.[^.]+$/, ''), scene: null, frames: 8 });
  return projectView(p);
}

const routes = [
  ['GET', /^\/api\/status$/, async () => ({ openai_configured: OPENAI, vision_model: 'gpt-5.1', renderer: 'egl', fixture_mode: !OPENAI, app_version: '0.1.0-mock' })],
  ['GET', /^\/api\/samples$/, async () => ({ samples: [
    { id: 'baseline', name: 'Baseline bench', description: 'Reachable layout — the pick-and-place should succeed.' },
    { id: 'far_bin', name: 'Far target bin', description: 'Target bin sits outside the Panda reach envelope.' },
  ] })],
  ['GET', /^\/api\/projects$/, async () => ({ projects: Object.values(db.projects).map((p) => ({ id: p.id, name: p.name, source: p.source, created_at: p.created_at, current_version: p.current_version, runs: p.runs.length })).reverse() })],
  ['POST', /^\/api\/projects$/, async (m, b) => {
    await delay(500);
    if (b.source === 'fixture') {
      const p = newProject({ source: 'fixture', name: 'fixture capture', scene: null, frames: 8 });
      p.extraction = { mode: 'fixture', model: 'fixture', elapsed_s: 0.6, raw_url: '#', warnings: [], fixture: true };
      addVersion(p, clone(p.frameScene), 'extraction', 'Draft scene from fixture extraction (canned model response)', false);
      p.metrics.generation_s = 0.6;
      return projectView(p);
    }
    const scene = baseScene(b.sample_id);
    const p = newProject({ source: 'sample', name: b.sample_id, scene, frames: 6, origin: 'sample', summary: `Initial scene from sample '${b.sample_id}'` });
    p.metrics.generation_s = 0.3;
    return projectView(p);
  }],
  ['GET', /^\/api\/projects\/([^/]+)$/, async (m) => projectView(proj(m[1]))],
  ['GET', /^\/api\/projects\/([^/]+)\/versions\/(\d+)\/scene\.json$/, async (m) => {
    const p = proj(m[1]); const v = +m[2];
    if (!p.scenes[v]) throw new MockError(404, 'No such version');
    return clone(p.scenes[v]);
  }],
  ['PUT', /^\/api\/projects\/([^/]+)\/scene$/, async (m, b) => {
    await delay(350);
    const p = proj(m[1]);
    addVersion(p, b.scene, 'edit', b.summary || 'Edited in workcell editor', true);
    return projectView(p);
  }],
  ['POST', /^\/api\/projects\/([^/]+)\/correct$/, async (m, b) => {
    await delay(700);
    const p = proj(m[1]);
    if (!p.current_version) throw new MockError(400, 'No scene yet — run extraction first.');
    const scene = p.scenes[p.current_version];
    const parsed = parseCorrection(scene, b.text || '');
    if (!parsed) throw new MockError(422, `Could not interpret "${b.text}". Try naming an object (e.g. bin_b, the block, the target bin), a distance and an edge: "Move bin_b 10 cm toward the back edge".`);
    const next = applyOps(scene, parsed.ops);
    const fromSuggestion = p.runs.some((r) => db.runs[r].suggestion?.text === b.text);
    addVersion(p, next, fromSuggestion ? 'suggestion' : 'correction', b.text, true);
    return { project: projectView(p), patch: { ops: parsed.ops, summary: b.text }, explanation: parsed.explanation, parser: OPENAI ? 'llm' : 'rules' };
  }],
  ['POST', /^\/api\/projects\/([^/]+)\/apply_suggestion$/, async (m, b) => {
    await delay(500);
    const p = proj(m[1]);
    const r = db.runs[b.run_id];
    if (!r?.suggestion?.patch) throw new MockError(422, 'This run has no applicable suggestion.');
    addVersion(p, applyOps(p.scenes[p.current_version], r.suggestion.patch.ops), 'suggestion', r.suggestion.text, true);
    return projectView(p);
  }],
  ['GET', /^\/api\/projects\/([^/]+)\/versions\/(\d+)\/reach$/, async (m) => {
    const p = proj(m[1]); const s = p.scenes[+m[2]];
    if (!s) throw new MockError(404, 'No such version');
    const step = 0.05, nx = Math.round(s.table.width / step), ny = Math.round(s.table.depth / step);
    const reachable = [];
    for (let j = 0; j < ny; j++) {
      const row = [];
      for (let i = 0; i < nx; i++) {
        const d = Math.hypot(0.025 + i * step - s.robot.x, 0.025 + j * step - s.robot.y);
        row.push(d > 0.22 && d < 0.78 ? 1 : 0);
      }
      reachable.push(row);
    }
    return { x0: 0.025, y0: 0.025, step, nx, ny, low_z: 0.06, high_z: 0.25, reachable };
  }],
  ['POST', /^\/api\/projects\/([^/]+)\/calibrate$/, async (m, b) => {
    await delay(500);
    const p = proj(m[1]);
    p.calibration = { corners_px: b.corners_px, frame_index: b.frame_index, table_width: b.table_width, table_depth: b.table_depth };
    if (p.current_version) {
      const s = clone(p.scenes[p.current_version]);
      const kx = b.table_width / s.table.width, ky = b.table_depth / s.table.depth;
      s.table.width = b.table_width; s.table.depth = b.table_depth;
      s.table.provenance.width = 'measured'; s.table.provenance.depth = 'measured';
      for (const o of s.objects) { o.x = +(o.x * kx).toFixed(4); o.y = +(o.y * ky).toFixed(4); }
      addVersion(p, s, 'calibration', `Measured table ${Math.round(b.table_width * 100)} × ${Math.round(b.table_depth * 100)} cm${b.corners_px ? ', 4 corners marked' : ''}`, false);
    }
    return projectView(p);
  }],
  ['POST', /^\/api\/projects\/([^/]+)\/extract$/, async (m, b) => {
    const p = proj(m[1]);
    if (b.mode === 'vision' && !OPENAI) throw new MockError(400, 'Vision extraction needs OPENAI_API_KEY on the server.');
    const t0 = Date.now();
    await delay(b.mode === 'vision' ? 2600 : 700);
    const s = clone(p.frameScene);
    p.extraction = { mode: b.mode, model: b.mode === 'vision' ? 'gpt-5.1' : null, elapsed_s: +((Date.now() - t0) / 1000).toFixed(1), raw_url: '#', warnings: b.mode === 'vision' ? ['Block size estimated from relative scale only.'] : [] };
    addVersion(p, s, 'extraction', b.mode === 'vision' ? 'Scene extracted by gpt-5.1 from 8 frames' : 'Scene from fixture extraction', false);
    p.metrics.generation_s = p.extraction.elapsed_s;
    return projectView(p);
  }],
  ['POST', /^\/api\/projects\/([^/]+)\/runs$/, async (m, b) => {
    const p = proj(m[1]);
    const r = startRun(p, b.version || p.current_version);
    return runSummary(r);
  }],
  ['GET', /^\/api\/runs\/([^/]+)$/, async (m) => {
    const r = db.runs[m[1]];
    if (!r) throw new MockError(404, 'No such run');
    return clone({ ...r, is_stale: r.version !== db.projects[r.project_id].current_version });
  }],
];

function proj(id) {
  const p = db.projects[id];
  if (!p) throw new MockError(404, `Project ${id} not found`);
  return p;
}

export async function handle(method, path, body) {
  await delay(80);
  for (const [m, re, fn] of routes) {
    const mm = m === method && path.match(re);
    if (mm) {
      try { return await fn(mm, body || {}); } catch (e) { if (e instanceof MockError) throw Object.assign(new Error(e.detail), { status: e.status, detail: e.detail }); throw e; }
    }
  }
  throw Object.assign(new Error('Not found'), { status: 404, detail: `Mock: no route for ${method} ${path}` });
}

export const seedProjectId = () => db.seedId;
seed();
