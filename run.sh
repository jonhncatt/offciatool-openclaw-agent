#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "WARN: OPENAI_API_KEY is not set. Server will start, but /api/chat requests will fail until key is configured." >&2
fi

if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  exec "$ROOT_DIR/.venv/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
fi

if command -v python3 >/dev/null 2>&1; then
  exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
