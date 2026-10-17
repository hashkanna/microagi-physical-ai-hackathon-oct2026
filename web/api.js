// CellCheck API client (docs/API.md). With ?mock=1 in the page URL every call is served by
// ./mock.js instead (in-memory fake backend for UI work without the server).

const params = new URLSearchParams(location.search);
export const MOCK = params.has('mock') && params.get('mock') !== '0';
const mock = MOCK ? await import('./mock.js') : null;

export class ApiError extends Error {
  constructor(status, detail, body) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

function detailOf(data) {
  if (!data) return null;
  const d = data.detail ?? data.message ?? data.error;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map((e) => (e.loc ? `${e.loc.filter((p) => p !== 'body').join('.')}: ` : '') + (e.msg || JSON.stringify(e))).join('; ');
  if (d) return JSON.stringify(d);
  return null;
}

async function req(method, path, body) {
  if (mock) return mock.handle(method, path, body);
  const opts = { method, headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new ApiError(0, 'Cannot reach the CellCheck server — is it running?');
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    throw new ApiError(res.status, detailOf(data) || `${res.status} ${res.statusText || 'error'}`, data);
  }
  return data;
}

const enc = encodeURIComponent;

export const api = {
  status: () => req('GET', '/api/status'),
  samples: () => req('GET', '/api/samples'),
  listProjects: () => req('GET', '/api/projects'),
  getProject: (pid) => req('GET', `/api/projects/${enc(pid)}`),
  createFromSample: (sample_id) => req('POST', '/api/projects', { source: 'sample', sample_id }),
  createFixture: () => req('POST', '/api/projects', { source: 'fixture' }),
  putScene: (pid, scene, summary) => req('PUT', `/api/projects/${enc(pid)}/scene`, { scene, summary }),
  correct: (pid, text) => req('POST', `/api/projects/${enc(pid)}/correct`, { text }),
  calibrate: (pid, body) => req('POST', `/api/projects/${enc(pid)}/calibrate`, body),
  extract: (pid, mode) => req('POST', `/api/projects/${enc(pid)}/extract`, { mode }),
  startRun: (pid, version) => req('POST', `/api/projects/${enc(pid)}/runs`, { version }),
  getRun: (rid) => req('GET', `/api/runs/${enc(rid)}`),
  applySuggestion: (pid, run_id) => req('POST', `/api/projects/${enc(pid)}/apply_suggestion`, { run_id }),
  reach: (pid, v) => req('GET', `/api/projects/${enc(pid)}/versions/${v}/reach`),
  // Scene JSON of any version (used to show an older version read-only).
  getVersion: (pid, v) => req('GET', `/api/projects/${enc(pid)}/versions/${v}/scene.json`),

  previewUrl: (pid, v) => (mock ? mock.previewUrl(pid, v) : `/api/projects/${enc(pid)}/versions/${v}/preview.png`),
  topdownUrl: (pid, v) => (mock ? mock.topdownUrl(pid, v) : `/api/projects/${enc(pid)}/versions/${v}/topdown.png`),
  sceneBundleUrl: (pid, v) => (mock ? '#mock-scene-bundle' : `/api/projects/${enc(pid)}/versions/${v}/bundle.zip`),
  mockSeedId: () => (mock ? mock.seedProjectId() : null),
  evidenceUrl: (run) => (mock ? '#mock-evidence-bundle' : run.evidence_url || `/api/runs/${enc(run.id)}/bundle.zip`),

  /** Multipart upload with progress callback (fraction 0..1). */
  upload(file, onProgress) {
    if (mock) return mock.upload(file, onProgress);
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/projects/upload');
      xhr.responseType = 'text';
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress?.(e.loaded / e.total); };
      xhr.upload.onload = () => onProgress?.(1);
      xhr.onerror = () => reject(new ApiError(0, 'Upload failed — cannot reach the server.'));
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new ApiError(xhr.status, detailOf(data) || `Upload failed (${xhr.status})`, data));
      };
      const fd = new FormData();
      fd.append('video', file);
      xhr.send(fd);
    });
  },
};
