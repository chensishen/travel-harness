#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ] || [ ! -d frontend/dist ]; then
  echo 'Run ./scripts/setup.sh first.' >&2
  exit 1
fi
if [ "${SKIP_DOCKER:-0}" != "1" ]; then
  docker compose --env-file .env -f docker/docker-compose.yml up -d --wait --wait-timeout 180
fi
exec .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port "${PORT:-8000}"
