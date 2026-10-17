// CellCheck frontend — plain ES modules, no build step.
import { api, MOCK } from './api.js';
import { renderTopdown, PANDA_REACH_M } from './scenemap.js';
import { icon, ORIGIN_ICON } from './icons.js';
import { openCornerModal, drawCornerOverlay } from './corners.js';

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clone = (o) => (o == null ? o : JSON.parse(JSON.stringify(o)));
const num = (v, d = 3) => (v == null || Number.isNaN(+v) ? '—' : (+v).toFixed(d));
const errMsg = (e) => e?.detail || e?.message || String(e);
const STAGES = ['compiling', 'settling', 'planning', 'executing', 'rendering'];
const STAGE_LABEL = { compiling: 'Compiling MJCF', settling: 'Settling physics', planning: 'Planning motion', executing: 'Executing pick & place', rendering: 'Rendering video' };
const STAGE_CHECK = { compiling: 'compiled', settling: 'settled', planning: 'reachable', executing: 'collisions', rendering: 'placement' };
const DEFAULT_CHECKS = [
  { id: 'compiled', label: 'Scene compiled' },
  { id: 'settled', label: 'Scene settled' },
  { id: 'reachable', label: 'Required targets reachable' },
  { id: 'collisions', label: 'No unexpected collisions' },
  { id: 'placement', label: 'Block placed in target bin' },
];
const PROV = {
  inferred: { label: 'inferred', tip: 'Inferred by the vision model from the video — an estimate' },
  measured: { label: 'measured', tip: 'Measured by you (calibration)' },
  user_confirmed: { label: 'confirmed', tip: 'Set or confirmed by you' },
  default: { label: 'default', tip: 'Default assumption — not observed' },
};
const ORIGIN_LABEL = { sample: 'Sample', extraction: 'Extraction', edit: 'Manual edit', correction: 'Language correction', calibration: 'Calibration', suggestion: 'Applied suggestion' };
const EXAMPLES = [
  'Move the target bin 20 cm toward the left edge',
  'Rotate the block 45°',
  'The table is 120 cm wide',
];

const state = {
  status: null,
  statusError: null,
  samples: [],
  projects: [],
  project: null,
  viewVersion: null,
  viewScene: null,
  viewSceneMissing: false,
  runs: {},
  selectedRunId: null,
  pollTimer: null,
  mediaTab: 'playback',
  draft: null,
  edits: new Set(),
  editorKey: null,
  saveError: null,
  lastCorrection: null,
  correctError: null,
  busy: {},
  upload: null, // {pct, phase}
  frameIndex: null,
  reach: {}, // `${pid}:${v}` -> grid | null (unavailable)
  draftRev: 0,
  calib: { width: '', depth: '', unit: 'cm', corners: null, frameIndex: null, pid: null },
};

// ------------------------------------------------------------------ toasts
function toast(msg, kind = 'info', ms = 4200) {
  const el = document.createElement('div');
  el.className = `toast toast-${kind}`;
  el.innerHTML = `${icon(kind === 'ok' ? 'check' : kind === 'err' ? 'warn' : 'info')}<span>${msg}</span>`;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 300); }, ms);
}

async function withBusy(key, fn) {
  if (state.busy[key]) return;
  state.busy[key] = true;
  renderAll();
  try { return await fn(); } finally { state.busy[key] = false; renderAll(); }
}

// ------------------------------------------------------------------ project / version / run state
const isRunning = (r) => r && (r.status === 'queued' || r.status === 'running');
const isReadOnly = () => state.project && state.viewVersion !== state.project.current_version;
const selectedRun = () => (state.selectedRunId ? state.runs[state.selectedRunId] : null);
const runSummaries = () => (state.project?.runs || []).slice();

function setUrlProject(pid) {
  const q = new URLSearchParams(location.search);
  if (pid) q.set('project', pid); else q.delete('project');
  history.replaceState(null, '', `${location.pathname}?${q.toString()}`.replace(/\?$/, ''));
}

function setProject(p, { selectRun = null } = {}) {
  const changedProject = !state.project || state.project.id !== p.id;
  if (!changedProject && p.current_version !== state.project.current_version) {
    requestAnimationFrame(() => document.querySelector('.col-sim')?.scrollTo({ top: 0, behavior: 'smooth' }));
  }
  state.project = p;
  state.viewVersion = p.current_version;
  state.viewScene = p.scene || null;
  state.viewSceneMissing = false;
  resetDraft();
  if (changedProject) {
    state.runs = {};
    state.selectedRunId = null;
    state.frameIndex = p.calibration?.frame_index ?? p.overview_frame ?? (p.frames?.[0]?.index ?? null);
    state.lastCorrection = null;
    state.correctError = null;
    const c = p.calibration;
    state.calib = {
      pid: p.id, unit: 'cm',
      width: c ? +(c.table_width * 100).toFixed(1) : '',
      depth: c ? +(c.table_depth * 100).toFixed(1) : '',
      corners: c?.corners_px || null,
      frameIndex: c?.frame_index ?? null,
    };
  }
  setUrlProject(p.id);
  const runs = runSummaries();
  if (selectRun) state.selectedRunId = selectRun;
  else if (!state.selectedRunId || !runs.some((r) => r.id === state.selectedRunId)) {
    const cur = runs.filter((r) => r.version === p.current_version);
    const pick = (cur.length ? cur : runs).slice(-1)[0];
    state.selectedRunId = pick?.id || null;
  }
  state.mediaTab = state.selectedRunId ? state.mediaTab : 'preview';
  ensureReach(p.current_version);
  renderAll();
  if (state.selectedRunId) ensureRun(state.selectedRunId);
  const active = runs.find((r) => isRunning(r));
  if (active) pollRun(active.id);
}

function resetDraft() {
  state.draftRev++;
  state.draft = clone(state.viewScene);
  state.edits = new Set();
  state.saveError = null;
  state.editorKey = null;
}

async function loadProject(pid, opts) {
  const p = await api.getProject(pid);
  setProject(p, opts);
  return p;
}

async function ensureRun(rid, force = false) {
  if (!force && state.runs[rid]?.checks && !isRunning(state.runs[rid])) { renderSim(); renderChecks(); return; }
  try {
    const r = await api.getRun(rid);
    state.runs[rid] = r;
    renderSim(); renderChecks(); renderTimeline();
    if (isRunning(r)) pollRun(rid);
  } catch (e) {
    toast(`Could not load run ${esc(rid)}: ${esc(errMsg(e))}`, 'err');
  }
}

function pollRun(rid) {
  if (state.pollTimer?.rid === rid) return;
  if (state.pollTimer) clearInterval(state.pollTimer.id);
  let failures = 0;
  const id = setInterval(async () => {
    try {
      const r = await api.getRun(rid);
      failures = 0;
      state.runs[rid] = r;
      if (!isRunning(r)) {
        clearInterval(id);
        state.pollTimer = null;
        try { state.project = await api.getProject(state.project.id); } catch { /* keep */ }
        if (r.status === 'done') toast(r.success ? `Run ${esc(r.id)}: placement succeeded` : `Run ${esc(r.id)}: task failed — see checks`, r.success ? 'ok' : 'err');
        else toast(`Run ${esc(r.id)} errored: ${esc(r.failure_reason || 'see details')}`, 'err');
        if (state.selectedRunId === rid) state.mediaTab = 'playback';
        renderAll();
      } else {
        renderSim(); renderChecks();
      }
    } catch (e) {
      if (++failures > 8) { clearInterval(id); state.pollTimer = null; toast(`Lost contact with run ${esc(rid)}: ${esc(errMsg(e))}`, 'err'); }
    }
  }, 1000);
  state.pollTimer = { id, rid };
}

async function runTask() {
  const p = state.project;
  if (!p?.current_version) return;
  if (state.viewVersion !== p.current_version) await viewVersion(p.current_version);
  await withBusy('run', async () => {
    try {
      const s = await api.startRun(p.id, p.current_version);
      state.runs[s.id] = { checks: [], ...s, version: s.version ?? p.current_version, status: s.status || 'queued' };
      state.project.runs = [...(state.project.runs || []), s];
      state.selectedRunId = s.id;
      state.mediaTab = 'playback';
      document.querySelector('.col-sim')?.scrollTo({ top: 0, behavior: 'smooth' });
      pollRun(s.id);
    } catch (e) {
      toast(`Could not start run: ${esc(errMsg(e))}`, 'err');
    }
  });
}

async function viewVersion(v) {
  const p = state.project;
  if (!p) return;
  state.viewVersion = v;
  if (v === p.current_version) {
    state.viewScene = p.scene;
    state.viewSceneMissing = false;
  } else {
    state.viewScene = null;
    state.viewSceneMissing = false;
    try {
      const res = await api.getVersion(p.id, v);
      state.viewScene = res?.scene || (res?.table ? res : null);
      state.viewSceneMissing = !state.viewScene;
    } catch {
      state.viewSceneMissing = true;
    }
  }
  resetDraft();
  ensureReach(v);
  const runs = runSummaries().filter((r) => r.version === v);
  if (runs.length) { state.selectedRunId = runs.slice(-1)[0].id; ensureRun(state.selectedRunId); }
  if (!runs.length && state.mediaTab === 'playback') state.mediaTab = 'preview';
  renderAll();
}

// ------------------------------------------------------------------ reach grid + client-side map
function ensureReach(v) {
  const p = state.project;
  if (!p || !v) return;
  const k = `${p.id}:${v}`;
  if (k in state.reach) return;
  state.reach[k] = undefined;
  api.reach(p.id, v).then((g) => { state.reach[k] = g; }).catch(() => { state.reach[k] = null; })
    .finally(() => { if (state.project?.id === p.id) { renderCellLive(); renderSim(); } });
}

function currentMapScene() {
  return state.draft || state.viewScene;
}

function mapSvg(px, draggable, fitH) {
  const scene = currentMapScene();
  if (!scene) return '';
  const p = state.project;
  const robotEdited = [...state.edits].some((e) => e.startsWith('robot.') || e.startsWith('table.'));
  const reach = robotEdited ? null : state.reach[`${p.id}:${state.viewVersion}`];
  const changed = new Set([...state.edits].filter((e) => e.startsWith('obj.')).map((e) => e.split('.')[1]));
  return renderTopdown(scene, { px, fitH, reach, ghost: state.edits.size ? state.viewScene : null, changed, draggable: draggable && !isReadOnly() });
}

function reachCaption() {
  const p = state.project;
  const robotEdited = [...state.edits].some((e) => e.startsWith('robot.') || e.startsWith('table.'));
  const g = state.reach[`${p.id}:${state.viewVersion}`];
  if (g && !robotEdited) return `<span class="reach-key"></span> reachable grasp positions (IK, ${Math.round(g.step * 100)} cm grid)`;
  if (robotEdited) return '<span style="color:#4cc2ff">dashed circle ≈ Panda reach</span> (reach grid updates after save)';
  return `<span style="color:#4cc2ff">dashed circle ≈ Panda reach ${PANDA_REACH_M} m</span>`;
}

function bindMapDrag(container) {
  let drag = null;
  const toWorld = (svg, e) => {
    const pt = svg.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const q = pt.matrixTransform(svg.getScreenCTM().inverse());
    return { x: q.x, y: -q.y };
  };
  const r3 = (v) => Math.round(v * 200) / 200; // 5 mm snap
  container.addEventListener('pointerdown', (e) => {
    const g = e.target.closest('.dragobj');
    if (!g || isReadOnly() || !state.draft || state.busy.save) return;
    const svg = g.ownerSVGElement;
    const o = state.draft.objects.find((x) => x.id === g.dataset.obj);
    if (!o) return;
    drag = { id: o.id, g, svg, start: toWorld(svg, e), ox: o.x, oy: o.y, nx: o.x, ny: o.y, moved: false, wrap: container };
    svg.setPointerCapture(e.pointerId);
    g.classList.add('dragging');
    e.preventDefault();
  });
  container.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const pt = toWorld(drag.svg, e);
    const T = state.draft.table;
    drag.nx = Math.max(0, Math.min(T.width, r3(drag.ox + pt.x - drag.start.x)));
    drag.ny = Math.max(0, Math.min(T.depth, r3(drag.oy + pt.y - drag.start.y)));
    const dx = drag.nx - drag.ox, dy = drag.ny - drag.oy;
    drag.moved = Math.abs(dx) + Math.abs(dy) > 0.004;
    drag.g.setAttribute('transform', `translate(${dx} ${-dy})`);
    let ro = container.querySelector('.drag-readout');
    if (!ro) { ro = document.createElement('div'); ro.className = 'drag-readout'; container.appendChild(ro); }
    ro.innerHTML = `<b>${esc(drag.id)}</b> x ${drag.nx.toFixed(3)} · y ${drag.ny.toFixed(3)} m <span class="muted">(Δ ${(dx * 100).toFixed(1)}, ${(dy * 100).toFixed(1)} cm)</span> — release to save as v${(state.project.current_version || 0) + 1}`;
  });
  const end = () => {
    if (!drag) return;
    const d = drag; drag = null;
    d.g.classList.remove('dragging');
    container.querySelector('.drag-readout')?.remove();
    if (!d.moved) { d.g.setAttribute('transform', 'translate(0 0)'); return; }
    for (const [fld, val, orig] of [['x', d.nx, d.ox], ['y', d.ny, d.oy]]) {
      const path = `obj.${d.id}.${fld}`;
      pathSet(state.draft, path, val);
      if (Math.abs(pathGet(state.viewScene, path) - val) > 1e-9 || Math.abs(val - orig) > 1e-9) state.edits.add(path);
    }
    saveEdits(`Dragged ${d.id} on the top-down map to (${d.nx.toFixed(3)}, ${d.ny.toFixed(3)})`);
  };
  container.addEventListener('pointerup', end);
  container.addEventListener('pointercancel', end);
}

// ------------------------------------------------------------------ header / timeline / footer
function renderHeader() {
  const s = state.status;
  const p = state.project;
  const pills = [];
  if (MOCK) pills.push(`<span class="pill pill-red" data-tip="All data on this page is fake (?mock=1)">${icon('warn')}MOCK DATA</span>`);
  if (state.statusError) pills.push(`<span class="pill pill-red" data-tip="${esc(state.statusError)}">${icon('warn')}Server offline</span>`);
  if (s) {
    pills.push(s.openai_configured
      ? `<span class="pill pill-green" data-tip="Vision extraction and LLM corrections available">${icon('spark')}OpenAI · ${esc(s.vision_model || 'configured')}</span>`
      : `<span class="pill pill-grey" data-tip="No OPENAI_API_KEY on the server: vision extraction is disabled and corrections use the rules parser">${icon('spark')}OpenAI not configured</span>`);
    pills.push(`<span class="pill pill-grey" data-tip="MuJoCo offscreen renderer">${icon('image')}Renderer · ${esc(s.renderer || '?')}</span>`);
  }
  const fixture = p ? (p.source === 'fixture' || p.source === 'sample' || p.extraction?.mode === 'fixture') : s?.fixture_mode;
  if (fixture) pills.push(`<span class="pill pill-amber" data-tip="${p ? `This scene comes from ${p.source === 'upload' ? 'a fixture extraction' : `a ${esc(p.source)}`}, not a live vision extraction` : 'Server is in fixture mode'}">${icon('warn')}FIXTURE MODE</span>`);
  $('#statusPills').innerHTML = pills.join('');
  $('#projectName').innerHTML = p
    ? `<div class="pn-label">Project</div><div class="pn-name">${esc(p.name)} <span class="mono muted">${esc(p.id)}</span> <span class="src src-${esc(p.source)}">${esc(p.source)}</span></div>`
    : '';
}

function renderTimeline() {
  const p = state.project;
  const bar = $('#versionBar');
  bar.classList.toggle('hidden', !p);
  if (!p) return;
  const runsBy = {};
  for (const r of p.runs || []) (runsBy[r.version] ||= []).push(r);
  const chips = (p.versions || []).map((v, i) => {
    const cur = v.version === p.current_version;
    const viewing = v.version === state.viewVersion;
    const rs = runsBy[v.version] || [];
    const last = rs[rs.length - 1];
    const full = last && state.runs[last.id];
    const ok = full ? full.success : last?.success;
    const runDot = last ? `<span class="vdot ${isRunning(full || last) ? 'run' : ok === true ? 'pass' : ok === false ? 'fail' : ''}"></span>` : '';
    const tip = `v${v.version} · ${ORIGIN_LABEL[v.origin] || v.origin}${v.manual ? ' (manual)' : ''}\n${v.summary || ''}\n${rs.length ? `${rs.length} run${rs.length > 1 ? 's' : ''}` : 'not run yet'}${cur ? ' · CURRENT' : ''}`;
    return `${i ? '<span class="vlink"></span>' : ''}<button class="vchip ${cur ? 'current' : ''} ${viewing ? 'viewing' : ''}" data-v="${v.version}" data-tip="${esc(tip)}">
      ${icon(ORIGIN_ICON[v.origin] || 'info')}<span class="vnum">v${v.version}</span>${runDot}${cur ? '<span class="vcur">current</span>' : ''}</button>`;
  });
  $('#timeline').innerHTML = chips.length ? chips.join('') : '<span class="muted small">No scene yet — run extraction in the Capture panel.</span>';
  const r = selectedRun();
  const dl = [];
  if (p.current_version) dl.push(`<a class="btn btn-sm" href="${esc(api.sceneBundleUrl(p.id, state.viewVersion || p.current_version))}" download>${icon('download')}Scene bundle v${state.viewVersion || p.current_version}</a>`);
  if (r && r.status === 'done') dl.push(`<a class="btn btn-sm" href="${esc(api.evidenceUrl(r))}" download>${icon('download')}Evidence ${esc(r.id)}</a>`);
  $('#downloads').innerHTML = dl.join('');
  // read-only banner
  const rb = $('#readonlyBanner');
  if (isReadOnly()) {
    rb.classList.remove('hidden');
    rb.innerHTML = `${icon('lock')}<span>Viewing <b>v${state.viewVersion}</b> read-only — current version is <b>v${p.current_version}</b>. Edits and corrections always apply to the current version.</span>
      <button class="btn btn-sm" id="backToCurrent">${icon('back')} Back to v${p.current_version}</button>`;
  } else rb.classList.add('hidden');
}

function renderFooter() {
  const p = state.project;
  const f = $('#footer');
  f.classList.toggle('hidden', !p);
  if (!p) return;
  const m = p.metrics || {};
  const runs = p.runs || [];
  const done = runs.map((r) => state.runs[r.id] || r).filter((r) => r.status === 'done');
  f.innerHTML = `
    <div class="metric"><span>Scene generation</span><b class="mono">${m.generation_s != null ? `${num(m.generation_s, 1)} s` : '—'}</b></div>
    <div class="metric"><span>Manual corrections</span><b class="mono">${m.manual_corrections ?? 0}</b></div>
    <div class="metric"><span>Versions</span><b class="mono">${(p.versions || []).length}</b></div>
    <div class="metric"><span>Runs</span><b class="mono">${runs.length}</b>${done.length ? `<span class="muted small">(${done.filter((r) => r.success).length} passed)</span>` : ''}</div>
    <div class="spacer"></div>
    <div class="disclaimer">${icon('info')} Approximate workcell from a phone video. Checks are of this simulated task only — not a real-world safety certification.</div>
    <div class="muted small mono">${esc(state.status?.app_version ? `v${state.status.app_version}` : '')}</div>`;
}

// ------------------------------------------------------------------ start screen
function renderStart() {
  const show = !state.project;
  $('#startView').classList.toggle('hidden', !show);
  $('#workspace').classList.toggle('hidden', show);
  if (!show) return;
  const up = state.upload;
  const samples = state.samples.map((s) => `
    <button class="sample" data-sample="${esc(s.id)}" ${state.busy.sample ? 'disabled' : ''}>
      <div class="sample-ic">${icon('sample')}</div>
      <div class="sample-text"><div class="sample-name">${esc(s.name || s.id)}</div><div class="sample-desc">${esc(s.description || '')}</div></div>
      <div class="sample-go">${state.busy.sample === s.id ? '<span class="spin"></span>' : icon('arrowRight')}</div>
    </button>`).join('') || `<div class="muted small">${state.statusError ? 'Server offline — samples unavailable.' : 'No samples available.'}</div>`;
  const recent = (state.projects || []).slice(0, 5).map((p) => `
    <button class="recent" data-project="${esc(p.id)}"><span class="mono">${esc(p.id)}</span><span class="recent-name">${esc(p.name || '')}</span>
    <span class="src src-${esc(p.source || '')}">${esc(p.source || '')}</span>${p.current_version ? `<span class="muted small">v${p.current_version}</span>` : ''}</button>`).join('');
  $('#startView').innerHTML = `
    <div class="hero">
      <div class="eyebrow">Physical AI · workcell check</div>
      <h1>Check a robot task on your bench —<br><span class="grad">from a 20-second phone video.</span></h1>
      <p class="lede">CellCheck turns a phone capture into an <b>approximate, editable MuJoCo workcell</b> (table, two bins, one block, a Franka Panda), runs a scripted pick-and-place in simulation and shows <b>visible pass/fail checks</b>. Every value is tagged with where it came from. Correct anything in plain language and re-run.</p>
      <div class="flow">
        <div class="flow-step"><span class="step">1</span><div><b>Capture</b><span>phone video → frames → scene</span></div></div>
        ${icon('arrowRight', 'flow-arrow')}
        <div class="flow-step"><span class="step">2</span><div><b>Simulate</b><span>Panda pick & place in MuJoCo</span></div></div>
        ${icon('arrowRight', 'flow-arrow')}
        <div class="flow-step"><span class="step">3</span><div><b>Correct</b><span>plain language → new version</span></div></div>
      </div>
    </div>
    <div class="start-grid">
      <div class="panel start-card">
        <div class="panel-head"><h2>${icon('upload')} Upload phone video</h2></div>
        <label class="dropzone ${up ? 'busy' : ''}" id="dropzone">
          <input type="file" id="fileInput" accept="video/*,.mp4,.mov,.m4v,.webm" ${up ? 'disabled' : ''}>
          ${up ? `
            <div class="dz-title">${up.phase === 'upload' ? `Uploading… ${Math.round(up.pct * 100)}%` : '<span class="spin"></span> Sampling frames with FFmpeg…'}</div>
            <div class="progress"><div style="width:${Math.round((up.phase === 'upload' ? up.pct : 1) * 100)}%"></div></div>
            <div class="dz-sub">${esc(up.name)}</div>` : `
            <div class="dz-icon">${icon('upload')}</div>
            <div class="dz-title">Drop a video here or <u>choose a file</u></div>
            <div class="dz-sub">Walk slowly around the table, ~15–30 s. MP4 / MOV.</div>`}
        </label>
      </div>
      <div class="panel start-card">
        <div class="panel-head"><h2>${icon('sample')} Or start from a sample</h2></div>
        <div class="samples">${samples}</div>
        ${recent ? `<div class="recent-head">Recent projects</div><div class="recents">${recent}</div>` : ''}
      </div>
    </div>
    <div class="start-foot">${icon('info')} Approximate reconstruction, not a digital twin. Checks apply to this simulated task only — not a real-world safety certification.</div>`;
}

async function startUpload(file) {
  if (!file) return;
  state.upload = { pct: 0, phase: 'upload', name: file.name };
  renderStart();
  try {
    const p = await api.upload(file, (pct) => {
      state.upload = { ...state.upload, pct, phase: pct >= 1 ? 'process' : 'upload' };
      renderStart();
    });
    state.upload = null;
    toast(`Uploaded — ${p.frames?.length || 0} frames sampled. Next: calibrate & extract.`, 'ok');
    setProject(p);
  } catch (e) {
    state.upload = null;
    renderStart();
    toast(`Upload failed: ${esc(errMsg(e))}`, 'err', 7000);
  }
}

// ------------------------------------------------------------------ capture column
function calibDims() {
  const k = state.calib.unit === 'cm' ? 0.01 : 1;
  const w = parseFloat(state.calib.width), d = parseFloat(state.calib.depth);
  return { width: Number.isFinite(w) ? +(w * k).toFixed(4) : null, depth: Number.isFinite(d) ? +(d * k).toFixed(4) : null };
}

function renderCapture() {
  const p = state.project;
  if (!p) return;
  const frames = p.frames || [];
  const fi = state.frameIndex;
  const frame = frames.find((f) => f.index === fi) || frames[0];
  const c = state.calib;
  const dims = calibDims();
  const dimsOk = dims.width > 0.2 && dims.width <= 3 && dims.depth > 0.2 && dims.depth <= 3;
  const ncorners = c.corners?.length || 0;
  const s = state.status;
  const visionOk = !!s?.openai_configured;
  const ro = isReadOnly();
  const ex = p.extraction;
  const hasScene = !!p.current_version;
  const showOverlay = frame && c.corners && (c.frameIndex ?? p.overview_frame) === frame.index;

  $('#colCapture').innerHTML = `
    <div class="panel">
      <div class="panel-head">
        <h2><span class="step">1</span>Capture</h2>
        <button class="btn btn-ghost btn-sm" id="newProject" title="Start a new project">${icon('plus')} New</button>
      </div>
      ${frames.length ? `
        <div class="frame-main">
          <div class="frame-wrap">
            <img id="mainFrame" src="${esc(frame.url)}" alt="Frame ${frame.index}">
            <svg id="mainOverlay" class="frame-overlay"></svg>
          </div>
          <div class="frame-cap"><span class="mono">frame ${String(frame.index).padStart(2, '0')} · t=${num(frame.t, 1)} s</span>
            ${frame.index === p.overview_frame ? '<span class="tag">overview</span>' : ''}${showOverlay ? '<span class="tag tag-blue">corners marked</span>' : ''}</div>
        </div>
        <div class="thumbs" id="thumbs">${frames.map((f) => `<button class="thumb ${f.index === frame.index ? 'sel' : ''}" data-frame="${f.index}" title="frame ${f.index} · t=${f.t}s"><img src="${esc(f.url)}" alt="" loading="lazy"></button>`).join('')}</div>
        <div class="muted small">${frames.length} frames sampled from the ${p.source === 'upload' ? 'uploaded video' : `${esc(p.source)} capture`}</div>
      ` : `<div class="empty-frames">${icon('film')}<div>No video frames for this project.</div><div class="muted small">Scene comes from the ${esc(p.source)} — corner marking needs frames.</div></div>`}
    </div>

    <div class="panel">
      <div class="panel-head"><h3>${icon('ruler')} Calibration</h3>${p.calibration ? '<span class="tag tag-blue">saved</span>' : '<span class="tag">optional</span>'}</div>
      <p class="muted small">Measure the tabletop with a tape. Measured values override the model's guess and are tagged <span class="prov prov-measured">measured</span>.</p>
      <div class="calib-grid">
        <label class="fld-lg"><span>Width <i>left→right</i></span><input id="calW" type="number" min="0" step="0.5" value="${esc(c.width)}" placeholder="${c.unit === 'cm' ? '100' : '1.00'}"></label>
        <label class="fld-lg"><span>Depth <i>front→back</i></span><input id="calD" type="number" min="0" step="0.5" value="${esc(c.depth)}" placeholder="${c.unit === 'cm' ? '70' : '0.70'}"></label>
        <div class="seg" id="calUnit"><button class="${c.unit === 'cm' ? 'on' : ''}" data-unit="cm">cm</button><button class="${c.unit === 'm' ? 'on' : ''}" data-unit="m">m</button></div>
      </div>
      <div class="calib-hint small ${dimsOk || (!c.width && !c.depth) ? 'muted' : 'err-text'}" id="calHint">${dimsOk ? `= ${dims.width.toFixed(3)} m × ${dims.depth.toFixed(3)} m` : c.width || c.depth ? 'Each side must be between 0.2 m and 3 m.' : 'Enter both sides.'}</div>
      <div class="calib-corners">
        <div class="cc-status">${ncorners === 4 ? `<span class="ok-text">${icon('check')} 4/4 corners marked</span> <span class="muted small">frame ${c.frameIndex ?? '?'}</span>` : `<span class="muted">${icon('corners')} Corners not marked <span class="small">(optional)</span></span>`}</div>
        <button class="btn btn-sm" id="markCorners" ${frames.length ? '' : 'disabled data-tip="Needs video frames"'}>${icon('corners')} ${ncorners === 4 ? 'Re-mark' : 'Mark corners'}</button>
      </div>
      <button class="btn btn-block" id="saveCalib" ${!dimsOk || ro || state.busy.calibrate ? 'disabled' : ''}>${state.busy.calibrate ? '<span class="spin"></span>' : icon('ruler')} Save calibration${hasScene ? ' → new version' : ''}</button>
    </div>

    <div class="panel">
      <div class="panel-head"><h3>${icon('extraction')} Scene extraction</h3>${ex ? `<span class="tag ${ex.mode === 'vision' ? 'tag-purple' : 'tag-amber'}">${esc(ex.mode)}</span>` : ''}</div>
      ${ex ? `<div class="kv-list small">
          <div class="kv"><span>Mode</span><b>${ex.mode === 'vision' ? `Vision · ${esc(ex.model || '')}` : 'Fixture (pre-recorded answer)'}</b></div>
          <div class="kv"><span>Time</span><b class="mono">${num(ex.elapsed_s, 1)} s</b></div>
          ${ex.raw_url && ex.raw_url !== '#' ? `<div class="kv"><span>Raw output</span><a href="${esc(ex.raw_url)}" target="_blank" rel="noopener">extraction.json</a></div>` : ''}
        </div>
        ${(ex.warnings || []).map((w) => `<div class="warn-text small">${icon('warn')} ${esc(w)}</div>`).join('')}` : `<p class="muted small">${frames.length ? 'Turn the frames into a scene: one table, two bins, one block, robot base.' : 'This project has no frames to extract from.'}</p>`}
      <div class="btn-col">
        <span class="tipwrap" ${!visionOk ? 'data-tip="OpenAI is not configured on the server (no OPENAI_API_KEY). Use fixture extraction instead."' : ''}>
          <button class="btn btn-block ${!hasScene && visionOk ? 'btn-primary' : ''}" id="extractVision" ${!visionOk || !frames.length || ro || state.busy.extract ? 'disabled' : ''}>
            ${state.busy.extract === 'vision' ? `<span class="spin"></span> Asking ${esc(s?.vision_model || 'model')} to read ${frames.length} frames…` : `${icon('spark')} Run vision extraction${s?.vision_model ? ` <span class="muted">· ${esc(s.vision_model)}</span>` : ''}`}
          </button>
        </span>
        <button class="btn btn-block btn-fixture" id="extractFixture" ${ro || state.busy.extract ? 'disabled' : ''} data-tip="Loads a pre-recorded extraction for this capture. Clearly labelled FIXTURE — no model call.">
          ${state.busy.extract === 'fixture' ? '<span class="spin"></span> Loading fixture…' : `${icon('sample')} Use fixture extraction <span class="tag tag-amber">fixture</span>`}
        </button>
      </div>
    </div>`;

  if (frame && showOverlay) {
    const img = $('#mainFrame'), svg = $('#mainOverlay');
    const draw = () => drawCornerOverlay(svg, img, c.corners, { big: false });
    if (img.complete) requestAnimationFrame(draw); else img.addEventListener('load', draw);
  }
}

function bindCapture() {
  const col = $('#colCapture');
  col.addEventListener('click', async (e) => {
    const t = e.target.closest('button');
    if (!t || t.disabled) return;
    if (t.dataset.frame != null) { state.frameIndex = +t.dataset.frame; renderCapture(); return; }
    if (t.dataset.unit) {
      const c = state.calib;
      if (t.dataset.unit !== c.unit) {
        const k = t.dataset.unit === 'm' ? 0.01 : 100;
        const conv = (v) => (v === '' || v == null || Number.isNaN(parseFloat(v)) ? v : +(parseFloat(v) * k).toFixed(t.dataset.unit === 'm' ? 3 : 1));
        c.width = conv(c.width); c.depth = conv(c.depth); c.unit = t.dataset.unit;
        renderCapture();
      }
      return;
    }
    switch (t.id) {
      case 'newProject': goStart(); break;
      case 'markCorners': openCorners(); break;
      case 'saveCalib': saveCalibration(); break;
      case 'extractVision': extract('vision'); break;
      case 'extractFixture': extract('fixture'); break;
    }
  });
  col.addEventListener('input', (e) => {
    if (e.target.id === 'calW') state.calib.width = e.target.value;
    else if (e.target.id === 'calD') state.calib.depth = e.target.value;
    else return;
    const dims = calibDims();
    const ok = dims.width > 0.2 && dims.width <= 3 && dims.depth > 0.2 && dims.depth <= 3;
    const hint = $('#calHint');
    hint.className = `calib-hint small ${ok || (!state.calib.width && !state.calib.depth) ? 'muted' : 'err-text'}`;
    hint.textContent = ok ? `= ${dims.width.toFixed(3)} m × ${dims.depth.toFixed(3)} m` : state.calib.width || state.calib.depth ? 'Each side must be between 0.2 m and 3 m.' : 'Enter both sides.';
    $('#saveCalib').disabled = !ok || isReadOnly();
  });
}

function openCorners() {
  const p = state.project;
  const dims = calibDims();
  openCornerModal({
    el: $('#cornerModal'),
    frames: p.frames,
    frameIndex: state.calib.frameIndex ?? state.frameIndex ?? p.overview_frame ?? 0,
    corners: state.calib.corners,
    dims: dims.width && dims.depth ? dims : null,
    onKeep: (corners, fi) => { state.calib.corners = corners; state.calib.frameIndex = fi; state.frameIndex = fi; renderCapture(); },
    onSave: (corners, fi) => { state.calib.corners = corners; state.calib.frameIndex = fi; state.frameIndex = fi; saveCalibration(); },
  });
}

async function saveCalibration() {
  const p = state.project;
  const dims = calibDims();
  const c = state.calib;
  await withBusy('calibrate', async () => {
    try {
      const np = await api.calibrate(p.id, {
        table_width: dims.width, table_depth: dims.depth,
        corners_px: c.corners?.length === 4 ? c.corners : null,
        frame_index: c.corners?.length === 4 ? c.frameIndex : (state.frameIndex ?? p.overview_frame ?? 0),
      });
      const newV = np.current_version !== p.current_version;
      setProject(np);
      toast(newV ? `Calibration saved → v${np.current_version}. Table is now <b>measured</b>.` : 'Calibration saved.', 'ok');
    } catch (e) {
      toast(`Calibration failed: ${esc(errMsg(e))}`, 'err', 7000);
    }
  });
}

async function extract(mode) {
  const p = state.project;
  state.busy.extract = mode;
  renderCapture();
  try {
    const np = await api.extract(p.id, mode);
    setProject(np);
    toast(`${mode === 'vision' ? 'Vision' : 'Fixture'} extraction → v${np.current_version}${np.extraction?.elapsed_s != null ? ` in ${num(np.extraction.elapsed_s, 1)} s` : ''}. Review the assumptions, then run the task.`, 'ok', 6000);
  } catch (e) {
    toast(`Extraction failed: ${esc(errMsg(e))}`, 'err', 8000);
  } finally {
    state.busy.extract = false;
    renderAll();
  }
}

// ------------------------------------------------------------------ simulation column
let mediaKey = null;

function renderSim() {
  const p = state.project;
  if (!p) return;
  const run = selectedRun();
  const v = state.viewVersion;
  const hasScene = !!v;


  // tabs
  const tabs = [['playback', 'film', 'Run playback'], ['preview', 'image', 'Scene preview'], ['topdown', 'map', 'Top-down map']];
  const mediaEl = $('#media');
  $('#mediaTabs').innerHTML = tabs.map(([id, ic, label]) => `<button class="tab ${state.mediaTab === id ? 'on' : ''}" data-tab="${id}">${icon(ic)}${label}${id !== 'playback' && v ? ` <span class="muted">v${v}</span>` : ''}</button>`).join('');

  // media content
  let key, html;
  const tab = state.mediaTab;
  if (!hasScene) {
    key = 'noscene';
    html = `<div class="media-empty">${icon('extraction')}<div class="me-title">No scene yet</div><div class="muted">Run vision extraction (or use the fixture) in the Capture panel to build the workcell.</div></div>`;
  } else if (tab === 'playback') {
    if (!run) {
      key = `norun:${v}`;
      html = `<div class="media-empty">${icon('play')}<div class="me-title">No run yet for v${v}</div><div class="muted">Press <b>Run task</b> to compile the scene and simulate the Panda pick-and-place.</div></div>`;
    } else if (isRunning(run)) {
      key = `running:${run.id}`;
      html = `<img class="media-img dim" src="${esc(api.previewUrl(p.id, run.version))}" alt="" onerror="this.style.display='none'">`;
    } else if (run.status === 'error') {
      key = `err:${run.id}`;
      html = `<div class="media-empty err">${icon('warn')}<div class="me-title">Run ${esc(run.id)} errored</div><div class="muted">${esc(run.failure_reason || 'The simulation did not complete.')}</div></div>`;
    } else if (run.video_url) {
      key = `vid:${run.id}:${run.video_url}`;
      html = `<video class="media-video" src="${esc(run.video_url)}" ${run.poster_url ? `poster="${esc(run.poster_url)}"` : ''} autoplay muted loop controls playsinline></video>`;
    } else {
      key = `poster:${run.id}`;
      html = `${run.poster_url ? `<img class="media-img" src="${esc(run.poster_url)}" alt="Run poster">` : ''}<div class="media-note">${icon('info')} No video recorded for this run${MOCK ? ' (mock data)' : ''} — showing the poster frame.</div>`;
    }
  } else if (tab === 'topdown') {
    const scn = currentMapScene();
    const rk = state.reach[`${p.id}:${v}`];
    key = `topdown:${p.id}:${v}:${state.draftRev}:${rk ? 'g' : rk === null ? 'n' : 'u'}:${isReadOnly()}`;
    html = scn
      ? `<div class="tab-map" id="tabMap">${mapSvg(Math.max(300, mediaEl.clientWidth - 32), true, Math.max(200, mediaEl.clientHeight - 84))}</div><div class="map-legend small">${reachCaption()}${isReadOnly() ? '' : ` · ${icon('edit')} drag a bin or the block to move it`}</div>`
      : `<img class="media-img contain-pad" src="${esc(api.topdownUrl(p.id, v))}" alt="Top-down map v${v}">`;
  } else {
    const url = api.previewUrl(p.id, v);
    key = `${tab}:${p.id}:${v}`;
    html = `<img class="media-img ${tab === 'topdown' ? 'contain-pad' : ''}" src="${esc(url)}" alt="${tab === 'preview' ? 'Scene preview' : 'Top-down map'} v${v}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'media-empty',innerHTML:'<div class=&quot;me-title&quot;>Render unavailable</div><div class=&quot;muted&quot;>The server could not render this view.</div>'}))">`;
  }
  const media = $('#media');
  if (key !== mediaKey) {
    media.innerHTML = `<div class="media-content">${html}</div><div class="media-badges" id="mediaBadges"></div><div class="media-overlay" id="mediaOverlay"></div>`;
    mediaKey = key;
  }

  // badges
  const badges = [];
  if (hasScene && tab === 'playback' && run) {
    if (isRunning(run)) badges.push(`<span class="badge badge-live"><span class="pulse"></span>SIMULATING… (${esc(run.status === 'queued' ? 'queued' : run.stage)})</span>`);
    else if (run.status === 'done') badges.push(`<span class="badge badge-rec">${icon('film')}RECORDED PLAYBACK · run ${esc(run.id)} · scene v${run.version}</span>`);
    if (!isRunning(run) && run.version !== p.current_version) badges.push(`<span class="badge badge-stale">${icon('warn')}STALE — scene changed since this run (now v${p.current_version})</span>`);
  } else if (hasScene && tab !== 'playback') {
    badges.push(`<span class="badge badge-rec">${icon(tab === 'preview' ? 'image' : 'map')}${tab === 'preview' ? 'SCENE PREVIEW · settled render' : 'TOP-DOWN MAP · table frame'} · v${v}</span>`);
    if (isReadOnly()) badges.push(`<span class="badge badge-stale">${icon('lock')}OLDER VERSION</span>`);
  }
  $('#mediaBadges').innerHTML = badges.join('');

  // overlay with stage progress
  const ov = $('#mediaOverlay');
  if (hasScene && tab === 'playback' && run && isRunning(run)) {
    ov.innerHTML = `<div class="sim-card">
      <div class="sim-title"><span class="spin lg"></span>Simulating scene v${run.version}</div>
      ${stageStepper(run)}
      <div class="muted small mono">run ${esc(run.id)} · ${num(run.elapsed_s, 1)} s elapsed</div></div>`;
    ov.classList.add('on');
  } else { ov.innerHTML = ''; ov.classList.remove('on'); }

  // run bar
  const busy = state.busy.run || runSummaries().some((r) => isRunning(state.runs[r.id] || r));
  const stale = run && run.version !== p.current_version;
  const nudge = p.current_version && !busy && (!run || stale || run.version !== p.current_version);
  const curRuns = runSummaries().filter((r) => r.version === p.current_version);
  $('#runbar').innerHTML = `
    <button class="btn btn-primary btn-run ${nudge ? 'nudge' : ''}" id="runBtn" ${!p.current_version || busy ? 'disabled' : ''}>
      ${busy ? '<span class="spin"></span> Simulating…' : `${icon('play')} Run task on v${p.current_version || '—'}`}</button>
    <div class="runbar-info">
      ${busy && run && isRunning(run) ? stageStepper(run, true) : p.current_version ? `<div class="task-line">${taskLine(p.scene)}</div>
      <div class="muted small">${curRuns.length ? `v${p.current_version} has ${curRuns.length} run${curRuns.length > 1 ? 's' : ''}` : `<span class="amber-text">v${p.current_version} has not been run yet</span>`} · Franka Panda · MuJoCo</div>` : '<div class="muted">Build a scene first.</div>'}
    </div>`;
}

function taskLine(scene) {
  if (!scene?.task) return '';
  const o = scene.objects.find((x) => x.id === scene.task.object_id);
  const t = scene.objects.find((x) => x.id === scene.task.target_id);
  return `<span class="muted">Task</span> Pick <b class="obj" title="${esc(o?.label || '')}">${esc(scene.task.object_id)}</b> ${icon('arrowRight')} place in <b class="obj tgt" title="${esc(t?.label || '')}">${esc(scene.task.target_id)}</b>`;
}

function stageStepper(run, compact = false) {
  const idx = run.status === 'queued' ? -1 : STAGES.indexOf(run.stage);
  return `<ol class="stepper ${compact ? 'compact' : ''}">${STAGES.map((s, i) => `<li class="${i < idx ? 'done' : i === idx ? 'active' : ''}"><span class="sdot">${i < idx ? icon('check') : ''}</span><span class="slabel">${s}</span></li>`).join('')}</ol>`;
}

function bindSim() {
  document.querySelector('.col-sim').addEventListener('click', (e) => {
    const t = e.target.closest('button');
    if (!t || t.disabled) return;
    if (t.dataset.tab) { state.mediaTab = t.dataset.tab; renderSim(); return; }
    if (t.dataset.run) { state.selectedRunId = t.dataset.run; state.mediaTab = 'playback'; renderSim(); renderChecks(); renderTimeline(); ensureRun(t.dataset.run); return; }
    if (t.id === 'runBtn') runTask();
    if (t.id === 'applySuggestion') applySuggestion();
  });
}

// ------------------------------------------------------------------ checks panel
function runPicker() {
  const p = state.project;
  const runs = runSummaries().slice().reverse().slice(0, 5);
  if (!runs.length) return '';
  return `<div class="run-picker"><span class="muted small">Runs</span>${runs.map((r) => {
    const full = state.runs[r.id] || r;
    const st = isRunning(full) ? 'run' : full.status === 'error' ? 'fail' : full.success === true ? 'pass' : full.success === false ? 'fail' : '';
    return `<button class="rchip ${r.id === state.selectedRunId ? 'sel' : ''}" data-run="${esc(r.id)}" data-tip="Run ${esc(r.id)} on scene v${r.version}${r.version !== p.current_version ? ' (stale)' : ''}"><span class="vdot ${st}"></span>v${r.version}<span class="mono muted">${esc(r.id.replace(/^r_/, ''))}</span></button>`;
  }).join('')}</div>`;
}

function renderChecks() {
  const p = state.project;
  if (!p) return;
  const run = selectedRun();
  const el = $('#checks');
  const head = `<div class="panel-head"><h2>${icon('check')} Task checks</h2>${runPicker()}</div>`;
  const disclaimer = `<div class="disclaimer small">${icon('info')} Checks of this simulated task only — not a real-world safety certification.</div>`;
  if (!run) {
    el.innerHTML = `${head}<div class="checks-empty">
      ${DEFAULT_CHECKS.map((c) => `<div class="check pending"><span class="cicon">${icon('dash')}</span><span class="clabel">${esc(c.label)}</span><span class="cdetail muted">not run</span></div>`).join('')}
      </div>${disclaimer}`;
    return;
  }
  const running = isRunning(run);
  const stale = run.version !== p.current_version;
  let verdict = '';
  if (running) verdict = `<div class="verdict running"><span class="spin"></span><div><div class="v-title">Simulating… ${esc(run.status === 'queued' ? 'queued' : STAGE_LABEL[run.stage] || run.stage)}</div><div class="v-sub">Results appear when the run finishes.</div></div></div>`;
  else if (run.status === 'error') verdict = `<div class="verdict fail">${icon('warn')}<div><div class="v-title">Run error</div><div class="v-sub">${esc(run.failure_reason || 'The simulation did not complete.')}</div></div></div>`;
  else if (run.success) verdict = `<div class="verdict pass">${icon('check')}<div><div class="v-title">Placement succeeded</div><div class="v-sub">${(run.checks || []).filter((c) => c.status === 'pass').length}/${(run.checks || []).length} checks passed for scene v${run.version}</div></div></div>`;
  else verdict = `<div class="verdict fail">${icon('x')}<div><div class="v-title">Task failed${run.failure_reason ? ':' : ''}</div><div class="v-sub">${esc(run.failure_reason || 'See failing checks below.')}</div></div></div>`;
  const staleNote = stale && !running ? `<div class="stale-note">${icon('warn')}<span>These results are for <b>v${run.version}</b>; the scene is now <b>v${p.current_version}</b>. Run the task again to check the current version.</span></div>` : '';
  const curCheck = running ? STAGE_CHECK[run.stage] : null;
  const stageIdx = running && run.status !== 'queued' ? STAGES.indexOf(run.stage) : -1;
  const checkIdx = (id) => STAGES.findIndex((st) => STAGE_CHECK[st] === id);
  const list = (run.checks?.length ? run.checks : DEFAULT_CHECKS.map((c) => ({ ...c, status: running ? 'pending' : 'skip', detail: running ? '' : '—' })))
    .map((c) => {
      const st = c.status;
      const ic = st === 'pass' ? 'check' : st === 'fail' ? 'x' : 'dash';
      const live = running && c.id === curCheck;
      const pendingText = live ? 'in progress…' : running && checkIdx(c.id) < stageIdx ? 'stage complete · result at end' : running ? 'waiting' : '';
      return `<div class="check ${esc(st)} ${live ? 'live' : ''}"><span class="cicon">${live ? '<span class="spin sm"></span>' : icon(ic)}</span><span class="clabel">${esc(c.label)}</span><span class="cdetail">${esc(c.detail || pendingText)}</span></div>`;
    }).join('');
  const m = run.metrics || {};
  const metrics = !running && run.status === 'done' ? `<div class="run-metrics">
    <div><span>Sim time</span><b class="mono">${num(m.sim_time_s, 1)} s</b></div>
    <div><span>Wall time</span><b class="mono">${num(run.elapsed_s, 1)} s</b></div>
    <div><span>Block final (x, y, z)</span><b class="mono">${m.block_final ? `(${m.block_final.map((x) => num(x, 3)).join(', ')}) m` : '—'}</b></div>
    ${m.max_contact_force != null ? `<div><span>Peak contact</span><b class="mono">${num(m.max_contact_force, 1)} N</b></div>` : ''}
    ${m.halted_before_stage ? `<div class="halted"><span>Halted before</span><b class="mono">${esc(m.halted_before_stage)}</b></div>` : ''}
  </div>` : '';
  const appliedV = run.suggestion ? suggestionAppliedIn(run) : null;
  const sug = run.suggestion && !running ? `<div class="suggestion ${appliedV ? 'applied' : ''}">
      <div class="sg-head">${icon('suggestion')} Suggested correction ${appliedV ? `<span class="tag tag-green">applied → v${appliedV}</span>` : '<span class="tag">not applied</span>'}</div>
      <div class="sg-text">“${esc(run.suggestion.text)}”</div>
      ${run.suggestion.patch?.ops?.length ? `<div class="sg-ops mono small">${run.suggestion.patch.ops.map((o) => !stale ? `${esc(o.target)}.${esc(o.field)}: ${esc(fmtVal(getField(p.scene, o.target, o.field)))} → ${o.op === 'add' ? esc(fmtVal(+(getField(p.scene, o.target, o.field) || 0) + +o.value)) : esc(fmtVal(o.value))}` : `${esc(o.target)}.${esc(o.field)} ${o.op === 'add' ? '+=' : '='} ${esc(fmtVal(o.value))}`).join(' · ')}${run.suggestion.moved_m != null ? ` <span class="muted">(moves ${(run.suggestion.moved_m * 100).toFixed(0)} cm)</span>` : ''}</div>` : ''}
      ${run.suggestion.verified ? `<div class="sg-verified small">${icon('check')} ${esc(run.suggestion.verified)}</div>` : ''}
      ${appliedV ? '' : `<div class="sg-actions">
        <button class="btn btn-primary btn-sm" id="applySuggestion" ${state.busy.correct || isReadOnly() || !run.suggestion.patch ? 'disabled' : ''}>${state.busy.correct === 'suggestion' ? '<span class="spin"></span>' : icon('suggestion')} Apply as new version</button>
        <span class="muted small">${!run.suggestion.patch ? 'No machine-applicable patch — use the correction box.' : stale ? `Computed for v${run.version} — will be applied to v${p.current_version}.` : 'Creates a new version; nothing changes until you apply it.'}</span>
      </div>`}</div>` : '';
  el.innerHTML = `${head}${verdict}${sug}${staleNote}<div class="check-list ${stale && !running ? 'is-stale' : ''}">${list}</div>${metrics}${disclaimer}`;
}

const appliedSuggestions = {}; // run id -> version
function suggestionAppliedIn(run) {
  if (appliedSuggestions[run.id]) return appliedSuggestions[run.id];
  const v = (state.project?.versions || []).find((x) => x.origin === 'suggestion' && x.version > run.version && x.summary && x.summary.includes(run.suggestion.text));
  return v ? v.version : null;
}

async function applySuggestion() {
  const run = selectedRun();
  const p = state.project;
  if (!run?.suggestion || !p) return;
  state.busy.correct = 'suggestion';
  renderChecks(); renderCorrection();
  try {
    const prev = clone(p.scene);
    const np = await api.applySuggestion(p.id, run.id);
    appliedSuggestions[run.id] = np.current_version;
    const patch = run.suggestion.patch || { ops: [] };
    state.lastCorrection = { text: run.suggestion.text, patch, explanation: run.suggestion.verified ? `Suggested by run ${run.id} after the failed check. Verified before suggesting: ${run.suggestion.verified}.` : `Suggested by run ${run.id}.`, parser: 'suggestion', diff: patchDiff(prev, np.scene, patch), from: p.current_version, to: np.current_version, kind: 'suggestion' };
    state.correctError = null;
    setProject(np);
    toast(`Suggestion applied → v${np.current_version}. Run the task to check it.`, 'ok');
  } catch (e) {
    toast(`Could not apply suggestion: ${esc(errMsg(e))}`, 'err', 7000);
  } finally {
    state.busy.correct = false;
    renderAll();
  }
}

// ------------------------------------------------------------------ correction
async function doCorrect(text, kind = 'text') {
  const p = state.project;
  if (!p?.current_version || !text.trim()) return;
  state.busy.correct = kind;
  state.correctError = null;
  renderCorrection(); renderChecks();
  try {
    const prev = clone(p.scene);
    const res = await api.correct(p.id, text.trim());
    const np = res.project;
    state.lastCorrection = { text: text.trim(), patch: res.patch, explanation: res.explanation, parser: res.parser, diff: patchDiff(prev, np.scene, res.patch), from: p.current_version, to: np.current_version, kind };
    if (kind === 'text') $('#correctInput').value = '';
    setProject(np);
    toast(`${kind === 'suggestion' ? 'Suggestion applied' : 'Correction applied'} → v${np.current_version}. Run the task to check it.`, 'ok');
  } catch (e) {
    state.correctError = { text, msg: errMsg(e), status: e.status };
    if (kind === 'suggestion') toast(`Could not apply suggestion: ${esc(errMsg(e))}`, 'err', 7000);
  } finally {
    state.busy.correct = false;
    renderAll();
  }
}

function getField(scene, target, field) {
  if (!scene) return undefined;
  const ent = target === 'table' ? scene.table : target === 'robot' ? scene.robot : target === 'task' ? scene.task : scene.objects?.find((o) => o.id === target);
  return ent ? ent[field] : undefined;
}
function fmtVal(v) {
  if (Array.isArray(v)) return v.map((x) => fmtVal(x)).join(' × ');
  if (typeof v === 'number') return (Math.round(v * 1000) / 1000).toString();
  return v == null ? '—' : String(v);
}
function patchDiff(prev, next, patch) {
  const ops = patch?.ops || [];
  const seen = new Set();
  return ops.filter((o) => { const k = `${o.target}.${o.field}`; if (seen.has(k)) return false; seen.add(k); return true; })
    .map((o) => ({ key: `${o.target}.${o.field}`, before: fmtVal(getField(prev, o.target, o.field)), after: fmtVal(getField(next, o.target, o.field)), op: o }));
}

function renderCorrection() {
  const p = state.project;
  if (!p) return;
  const ro = isReadOnly();
  const busy = state.busy.correct;
  const s = state.status;
  $('#correctInput').disabled = !p.current_version || ro || !!busy;
  $('#correctBtn').disabled = !p.current_version || ro || !!busy;
  $('#correctBtn').innerHTML = busy === 'text' ? '<span class="spin"></span>' : `Apply ${icon('arrowRight')}`;
  $('#parserHint').innerHTML = s ? (s.openai_configured ? `${icon('spark')} LLM parser · typed patch` : `${icon('info')} rules parser (no OpenAI)`) : '';
  $('#examples').innerHTML = !p.current_version || ro ? '' : EXAMPLES.map((x) => `<button class="ex" type="button" data-ex="${esc(x)}">${esc(x)}</button>`).join('');
  const out = [];
  if (state.correctError) {
    out.push(`<div class="correct-err">${icon('warn')}<div><b>${state.correctError.status === 422 ? 'Couldn’t interpret that' : 'Correction failed'}</b><div>${esc(state.correctError.msg)}</div><div class="muted small">Name an object (bin_b, the block, the target bin), a distance and an edge or direction.</div></div></div>`);
  }
  const lc = state.lastCorrection;
  if (lc) {
    out.push(`<div class="correct-res">
      <div class="cr-head"><span class="vchip mini current">${icon(lc.kind === 'suggestion' ? 'suggestion' : 'correction')}v${lc.from} → v${lc.to}</span>
        <span class="parser parser-${esc(lc.parser || 'rules')}">${lc.parser === 'llm' ? 'LLM parser' : lc.parser === 'suggestion' ? 'From run suggestion' : 'Rules parser'}</span></div>
      <div class="cr-quote">“${esc(lc.text)}”</div>
      ${lc.explanation ? `<div class="cr-expl">${esc(lc.explanation)}</div>` : ''}
      <div class="diff">${lc.diff.length ? lc.diff.map((d) => `<div class="diff-row"><span class="dk">${esc(d.key)}</span><span class="dv before">${esc(d.before)}</span>${icon('arrowRight')}<span class="dv after">${esc(d.after)}</span></div>`).join('') : '<div class="muted small">No field changes reported.</div>'}</div>
    </div>`);
  }
  $('#correctResult').innerHTML = out.join('');
}

function bindCorrection() {
  $('#correctForm').addEventListener('submit', (e) => {
    e.preventDefault();
    doCorrect($('#correctInput').value, 'text');
  });
  $('#correctInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doCorrect($('#correctInput').value, 'text'); }
  });
  $('#examples').addEventListener('click', (e) => {
    const b = e.target.closest('[data-ex]');
    if (!b) return;
    $('#correctInput').value = b.dataset.ex;
    $('#correctInput').focus();
  });
}

// ------------------------------------------------------------------ workcell editor
function pathGet(scene, path) {
  const [kind, a, b, c] = path.split('.');
  if (kind === 'table') return scene.table[a];
  if (kind === 'robot') return scene.robot[a];
  if (kind === 'task') return scene.task[a];
  const o = scene.objects.find((x) => x.id === a);
  if (!o) return undefined;
  return c != null ? o[b][+c] : o[b];
}
function pathSet(scene, path, val) {
  const [kind, a, b, c] = path.split('.');
  if (kind === 'table') scene.table[a] = val;
  else if (kind === 'robot') scene.robot[a] = val;
  else if (kind === 'task') scene.task[a] = val;
  else {
    const o = scene.objects.find((x) => x.id === a);
    if (c != null) { o[b] = [...o[b]]; o[b][+c] = val; } else o[b] = val;
  }
}
function provOf(scene, path) {
  const [kind, a, b] = path.split('.');
  if (kind === 'table') return scene.table.provenance?.[a];
  if (kind === 'robot') return scene.robot.provenance?.[a];
  if (kind === 'obj') return scene.objects.find((x) => x.id === a)?.provenance?.[b];
  return null;
}
function provChip(prov, pending = false) {
  const p = PROV[prov] || { label: prov || '—', tip: '' };
  return `<span class="prov prov-${esc(prov || 'none')} ${pending ? 'pending' : ''}" data-tip="${esc(pending ? 'Will be saved as user_confirmed' : p.tip)}">${esc(pending ? 'confirmed' : p.label)}</span>`;
}

function field(scene, path, name, { unit = 'm', step = 0.01, dec = 3, prov = null, ro = false, type = 'number' } = {}) {
  const v = pathGet(scene, path);
  const orig = state.viewScene ? pathGet(state.viewScene, path) : v;
  const edited = state.edits.has(path);
  const pv = prov ?? provOf(state.viewScene || scene, path);
  const val = type === 'number' ? (v == null ? '' : +(+v).toFixed(dec)) : v ?? '';
  return `<label class="fld ${edited ? 'edited' : ''}" data-path="${esc(path)}">
    <span class="fld-top"><span class="fld-name">${esc(name)}${unit ? ` <i>${esc(unit)}</i>` : ''}</span>${pv !== false ? provChip(pv, edited) : ''}</span>
    <input type="${type}" ${type === 'number' ? `step="${step}"` : ''} value="${esc(val)}" data-path="${esc(path)}" data-type="${type}" ${ro ? 'disabled' : ''} title="${edited ? `was ${esc(fmtVal(orig))}` : ''}">
  </label>`;
}

function renderCell(force = false) {
  const p = state.project;
  if (!p) return;
  const el = $('#cellPanel');
  const scene = state.draft;
  const ro = isReadOnly();
  const key = `${p.id}:${state.viewVersion}:${ro}:${!!scene}:${state.viewSceneMissing}`;
  if (!force && key === state.editorKey) { renderCellLive(); return; }
  state.editorKey = key;
  if (!scene) {
    el.innerHTML = `<div class="panel-head"><h2><span class="step">4</span>Workcell</h2></div>
      <div class="media-empty small-empty">${icon(state.viewSceneMissing ? 'lock' : 'extraction')}<div class="me-title">${state.viewSceneMissing ? `Scene values for v${state.viewVersion} not available` : 'No scene yet'}</div>
      <div class="muted small">${state.viewSceneMissing ? 'The server returned only renders for this older version. Use the preview tabs, or go back to the current version.' : 'Run extraction to get an editable workcell.'}</div></div>`;
    return;
  }
  const t = scene.table, r = scene.robot;
  const bins = scene.objects.filter((o) => o.kind === 'bin');
  const objCard = (o) => {
    const base = `obj.${o.id}`;
    const isT = o.id === scene.task.target_id, isB = o.id === scene.task.object_id;
    return `<div class="ent" data-ent="${esc(o.id)}">
      <div class="ent-head"><span class="ent-kind kind-${o.kind}">${esc(o.kind)}</span><span class="ent-id mono">${esc(o.id)}</span>
        ${isT ? '<span class="tag tag-green">target</span>' : ''}${isB ? '<span class="tag tag-amber">pick</span>' : ''}
        <input class="ent-label" type="text" value="${esc(o.label || '')}" data-path="${base}.label" data-type="text" placeholder="label" ${ro ? 'disabled' : ''} maxlength="60"></div>
      <div class="grid3">
        ${field(scene, `${base}.x`, 'x', { ro })}${field(scene, `${base}.y`, 'y', { ro })}${field(scene, `${base}.yaw_deg`, 'yaw', { unit: '°', step: 1, dec: 1, ro })}
      </div>
      <div class="grid3 size-row">
        ${field(scene, `${base}.size.0`, 'sx', { ro, step: 0.005 })}${field(scene, `${base}.size.1`, 'sy', { ro, step: 0.005, prov: false })}${field(scene, `${base}.size.2`, 'sz', { ro, step: 0.005, prov: false })}
      </div>
      ${o.kind === 'block' ? `<div class="grid3">${field(scene, `${base}.mass`, 'mass', { unit: 'kg', step: 0.01, dec: 3, ro })}${o.friction != null ? field(scene, `${base}.friction`, 'friction', { unit: 'μ', step: 0.05, dec: 2, ro }) : ''}</div>` : ''}
    </div>`;
  };
  el.innerHTML = `
    <div class="panel-head"><h2><span class="step">4</span>Workcell</h2><span class="tag ${ro ? 'tag-amber' : 'tag-blue'}">v${state.viewVersion}${ro ? ' · read-only' : ' · editable'}</span></div>
    <div class="legend"><span class="muted small">Where each value came from:</span>${['inferred', 'measured', 'user_confirmed', 'default'].map((k) => provChip(k)).join('')}</div>
    <div class="map-wrap"><div id="liveMap"></div><div class="map-cap muted small">Top-down · table frame · origin front-left${ro ? '' : ' · drag objects to move'}<br><span id="mapEdited"></span></div></div>
    <div class="task-edit">
      <span class="muted">Task</span> Pick <b class="obj">${esc(scene.task.object_id)}</b> ${icon('arrowRight')} place in
      <select id="targetSel" ${ro ? 'disabled' : ''}>${bins.map((b) => `<option value="${esc(b.id)}" ${b.id === scene.task.target_id ? 'selected' : ''}>${esc(b.id)}${b.label ? ` — ${esc(b.label)}` : ''}</option>`).join('')}</select>
    </div>
    <div class="ents" id="ents">
      <div class="ent">
        <div class="ent-head"><span class="ent-kind kind-table">table</span><span class="ent-id mono">table</span><span class="muted small">full extents, metres</span></div>
        <div class="grid3">${field(scene, 'table.width', 'width', { ro })}${field(scene, 'table.depth', 'depth', { ro })}${field(scene, 'table.height', 'height', { ro })}</div>
      </div>
      <div class="ent">
        <div class="ent-head"><span class="ent-kind kind-robot">robot</span><span class="ent-id mono">${esc(r.model || 'franka_panda')}</span><span class="muted small">base centre</span></div>
        <div class="grid3">${field(scene, 'robot.x', 'x', { ro })}${field(scene, 'robot.y', 'y', { ro })}${field(scene, 'robot.yaw_deg', 'yaw', { unit: '°', step: 1, dec: 1, ro })}</div>
      </div>
      ${scene.objects.map(objCard).join('')}
    </div>
    <div class="assumptions">
      <div class="as-head">${icon('info')} Assumptions <span class="muted small">(from the scene notes)</span></div>
      ${(scene.notes || []).length ? `<ul>${scene.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>` : '<div class="muted small">No notes.</div>'}
    </div>
    <div class="savebar" id="savebar"></div>`;
  renderCellLive();
}

function renderCellLive() {
  const scene = state.draft;
  if (!scene) return;
  const map = $('#liveMap');
  if (map) map.innerHTML = mapSvg(Math.max(300, map.clientWidth || 400), true, 260);
  const me = $('#mapEdited');
  if (me) me.innerHTML = `${reachCaption()}${state.edits.size ? ' · <span class="amber-text">dashed amber = before edit</span>' : ''}`;
  if (state.mediaTab === 'topdown') renderSim();
  const sb = $('#savebar');
  if (!sb) return;
  const n = state.edits.size;
  sb.classList.toggle('on', n > 0 || !!state.saveError);
  sb.innerHTML = n || state.saveError ? `
    ${state.saveError ? `<div class="err-text small">${icon('warn')} ${esc(state.saveError)}</div>` : ''}
    <div class="sb-row"><span class="sb-count"><b>${n}</b> unsaved change${n === 1 ? '' : 's'}</span>
      <span class="muted small sb-list mono">${[...state.edits].slice(0, 4).map((p) => esc(p.replace(/^obj\./, '').replace('.size.0', '.sx').replace('.size.1', '.sy').replace('.size.2', '.sz'))).join(', ')}${n > 4 ? '…' : ''}</span>
      <button class="btn btn-sm" id="discardEdits">Discard</button>
      <button class="btn btn-primary btn-sm" id="saveEdits" ${state.busy.save || !n ? 'disabled' : ''}>${state.busy.save ? '<span class="spin"></span>' : icon('edit')} Save as v${(state.project.current_version || 0) + 1}</button></div>` : '';
}

function bindCell() {
  const el = $('#cellPanel');
  const onEdit = (e) => {
    const inp = e.target;
    const path = inp.dataset.path;
    if (!path || !state.draft || isReadOnly()) return;
    const orig = pathGet(state.viewScene, path);
    let val;
    if (inp.dataset.type === 'number') {
      val = parseFloat(inp.value);
      if (!Number.isFinite(val)) { inp.classList.add('invalid'); return; }
      inp.classList.remove('invalid');
    } else val = inp.value;
    pathSet(state.draft, path, val);
    const same = typeof orig === 'number' ? Math.abs(orig - val) < 1e-9 : orig === val;
    if (same) state.edits.delete(path); else state.edits.add(path);
    const lab = inp.closest('.fld');
    if (lab) {
      lab.classList.toggle('edited', !same);
      const chip = lab.querySelector('.prov');
      if (chip) chip.outerHTML = provChip(provOf(state.viewScene, path), !same);
    }
    state.saveError = null;
    state.draftRev++;
    renderCellLive();
  };
  el.addEventListener('input', onEdit);
  el.addEventListener('change', (e) => {
    if (e.target.id === 'targetSel') {
      const v = e.target.value;
      pathSet(state.draft, 'task.target_id', v);
      if (v === state.viewScene.task.target_id) state.edits.delete('task.target_id'); else state.edits.add('task.target_id');
      state.editorKey = null;
      renderCell();
    }
  });
  el.addEventListener('click', (e) => {
    const t = e.target.closest('button');
    if (!t || t.disabled) return;
    if (t.id === 'discardEdits') { resetDraft(); renderCell(true); }
    if (t.id === 'saveEdits') saveEdits();
  });
}

async function saveEdits(summaryOverride) {
  const p = state.project;
  const scene = clone(state.draft);
  for (const path of state.edits) {
    const [kind, a, b] = path.split('.');
    if (kind === 'table') (scene.table.provenance ||= {})[a] = 'user_confirmed';
    else if (kind === 'robot') (scene.robot.provenance ||= {})[a] = 'user_confirmed';
    else if (kind === 'obj' && b !== 'label') { const o = scene.objects.find((x) => x.id === a); (o.provenance ||= {})[b] = 'user_confirmed'; }
  }
  const names = [...state.edits].map((p2) => p2.replace(/^obj\./, '').replace(/\.size\.\d/, '.size'));
  const summary = typeof summaryOverride === 'string' ? summaryOverride : `Edited in workcell editor: ${[...new Set(names)].join(', ')}`;
  state.busy.save = true;
  renderCellLive();
  try {
    const np = await api.putScene(p.id, scene, summary);
    setProject(np);
    toast(`Saved as v${np.current_version}. Edited values are now <b>user_confirmed</b>.`, 'ok');
  } catch (e) {
    state.saveError = errMsg(e);
    renderCellLive();
  } finally {
    state.busy.save = false;
    renderCellLive();
  }
}

// ------------------------------------------------------------------ top-level render & init
function renderAll() {
  renderHeader();
  renderStart();
  renderTimeline();
  renderFooter();
  if (!state.project) return;
  renderCapture();
  renderSim();
  renderChecks();
  renderCorrection();
  renderCell();
}

function goStart() {
  if (state.pollTimer) { clearInterval(state.pollTimer.id); state.pollTimer = null; }
  state.project = null;
  mediaKey = null;
  setUrlProject(null);
  refreshStartData();
  renderAll();
}

async function refreshStartData() {
  try { state.samples = (await api.samples()).samples || []; } catch { state.samples = []; }
  try { state.projects = (await api.listProjects()).projects || []; } catch { state.projects = []; }
  if (!state.project) renderStart();
}

function bindGlobal() {
  $('#brand').addEventListener('click', () => { if (state.project) goStart(); });
  $('#timeline').addEventListener('click', (e) => {
    const c = e.target.closest('[data-v]');
    if (c) viewVersion(+c.dataset.v);
  });
  $('#readonlyBanner').addEventListener('click', (e) => { if (e.target.closest('#backToCurrent')) viewVersion(state.project.current_version); });
  const sv = $('#startView');
  sv.addEventListener('click', async (e) => {
    const s = e.target.closest('[data-sample]');
    if (s && !s.disabled) {
      state.busy.sample = s.dataset.sample;
      renderStart();
      try { setProject(await api.createFromSample(s.dataset.sample)); toast(`Sample “${esc(s.dataset.sample)}” loaded as v1.`, 'ok'); }
      catch (err) { toast(`Could not load sample: ${esc(errMsg(err))}`, 'err'); }
      finally { state.busy.sample = false; renderAll(); }
      return;
    }
    const r = e.target.closest('[data-project]');
    if (r) { try { await loadProject(r.dataset.project); } catch (err) { toast(`Could not open project: ${esc(errMsg(err))}`, 'err'); } }
  });
  sv.addEventListener('change', (e) => { if (e.target.id === 'fileInput') startUpload(e.target.files[0]); });
  sv.addEventListener('dragover', (e) => { const dz = e.target.closest('#dropzone'); if (dz) { e.preventDefault(); dz.classList.add('drag'); } });
  sv.addEventListener('dragleave', (e) => { const dz = e.target.closest('#dropzone'); if (dz) dz.classList.remove('drag'); });
  sv.addEventListener('drop', (e) => {
    const dz = e.target.closest('#dropzone');
    if (!dz) return;
    e.preventDefault();
    dz.classList.remove('drag');
    const f = e.dataTransfer.files?.[0];
    if (f) startUpload(f);
  });
}

async function init() {
  bindGlobal(); bindCapture(); bindSim(); bindCorrection(); bindCell();
  bindMapDrag($('#media')); bindMapDrag($('#cellPanel'));
  try { state.status = await api.status(); } catch (e) { state.statusError = errMsg(e); }
  renderHeader();
  const q = new URLSearchParams(location.search);
  let pid = q.get('project');
  if (MOCK && pid === 'demo') pid = api.mockSeedId();
  if (pid) {
    try { await loadProject(pid); } catch (e) { toast(`Project ${esc(pid)} not found — starting fresh.`, 'err'); setUrlProject(null); }
  }
  await refreshStartData();
  renderAll();
}

init();
