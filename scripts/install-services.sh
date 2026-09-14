#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
[[ -x .venv/bin/python ]] || { echo 'Run scripts/setup.sh first.' >&2; exit 1; }
.venv/bin/python - <<'PY'
import os, shutil
from dotenv import load_dotenv
load_dotenv('.env')
binary=os.getenv('HR_OLLAMA_BINARY','ollama')
if not shutil.which(binary):
    raise SystemExit('Set HR_OLLAMA_BINARY in .env or install Ollama on PATH.')
PY
[[ -f /sys/fs/cgroup/cgroup.controllers ]] || { echo 'Unified cgroup v2 is required for memory limits.' >&2; exit 1; }
.venv/bin/python scripts/render_services.py
systemd-analyze --user verify deployment/systemd/*.service
hr_unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$hr_unit_dir"
install -m 644 deployment/systemd/hragent.slice deployment/systemd/*.service "$hr_unit_dir/"
systemctl --user daemon-reload
echo 'Installed user units. No services have been started or enabled at login.'
echo 'Start the app: systemctl --user start hr-ollama hr-web hr-scanner'
echo 'For unattended boot operation, an administrator must enable lingering for your Linux user.'
