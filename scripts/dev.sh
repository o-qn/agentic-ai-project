#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
case "${1:-web}" in
  web) exec .venv/bin/python -m hr_agent.cli serve ;;
  service) exec .venv/bin/python -m hr_agent.cli service ;;
  ollama) exec .venv/bin/python -c 'from dotenv import load_dotenv; import os; load_dotenv(".env"); os.execv("scripts/ollama-cpu.sh", ["scripts/ollama-cpu.sh"])' ;;
  *) echo 'Usage: scripts/dev.sh web|service|ollama' >&2; exit 2 ;;
esac
