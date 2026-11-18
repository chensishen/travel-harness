#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -d .venv ]; then
  "${PYTHON_BIN:-python3.12}" -m venv .venv
fi
.venv/bin/python -c 'import sys; assert sys.version_info[:3] == (3,12,11), "This project uses Python 3.12.11"'
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
if [ ! -f .env ]; then cp .env.example .env; fi
if [ ! -f data/fonts/SourceHanSansSC-Regular.otf ]; then
  mkdir -p data/fonts
  curl -fsSL --max-time 120 https://raw.githubusercontent.com/adobe-fonts/source-han-sans/release/OTF/SimplifiedChinese/SourceHanSansSC-Regular.otf -o data/fonts/SourceHanSansSC-Regular.otf
  curl -fsSL --max-time 30 https://raw.githubusercontent.com/adobe-fonts/source-han-sans/release/LICENSE.txt -o data/fonts/LICENSE.txt
fi
(cd frontend && npm ci && npm run build)
echo 'Setup complete. Add model keys to .env, then run ./scripts/start.sh'
