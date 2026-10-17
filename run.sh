#!/usr/bin/env bash
# One-command launch: creates a venv, installs pinned deps, picks a working headless renderer, starts the app.
set -euo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -d .venv ]; then
  "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
fi
.venv/bin/pip install -q -r requirements.txt
command -v ffmpeg >/dev/null || { echo "ffmpeg is required (apt-get install ffmpeg / brew install ffmpeg)"; exit 1; }
[ -f .env ] || cp .env.example .env
if [ -z "${MUJOCO_GL:-}" ]; then
  if [ "$(uname)" = "Darwin" ]; then export MUJOCO_GL=cgl;
  elif .venv/bin/python scripts/check_render.py egl >/dev/null 2>&1; then export MUJOCO_GL=egl;
  else export MUJOCO_GL=osmesa; fi
fi
echo "CellCheck → http://localhost:${PORT:-8000}  (renderer: $MUJOCO_GL)"
exec .venv/bin/python -m cellcheck
