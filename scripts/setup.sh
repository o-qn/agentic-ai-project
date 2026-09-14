#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ $(uname -s) == Linux ]] || { echo 'Linux is required.' >&2; exit 1; }
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --require-hashes -r build-requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
[[ -e .env ]] || cp .env.example .env
chmod 600 .env
.venv/bin/python -m hr_agent.cli init
.venv/bin/python -m hr_agent.cli doctor
for tool in pdftoppm tesseract; do
  command -v "$tool" >/dev/null || { echo "Missing $tool. Install your distribution's poppler-utils/poppler and tesseract packages." >&2; exit 1; }
done
echo 'Python setup complete. Review .env, authenticate Drive, configure local models, then run model-check.'
