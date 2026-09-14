# CV screening

A personal project that reads CVs from Google Drive, scores them against job descriptions, and shows the results in a small local dashboard. Ollama runs the model on this computer.

## Start the app

The Python environment and models are already included in this local installation.

```bash
cd "/home/qn/Documents/Agentic Ai project"
systemctl --user start hr-ollama hr-web hr-scanner
```

Open http://127.0.0.1:8787. Turn on **Auto process** to process queued CVs. Drive is checked every five minutes; **Check Drive now** runs an extra check.

The progress bar shows the current file and stage. During model inference it shows elapsed time and model turns, since the model cannot give a reliable completion percentage.

## Google sign-in

Your existing login is kept in `data/token.json`. If it expires or you want to sign in again:

```bash
cd "/home/qn/Documents/Agentic Ai project"
.venv/bin/python -m hr_agent.cli auth
```

Follow the browser sign-in. The OAuth client file is configured by `HR_CREDENTIALS` in `.env`; on this computer it is `/home/qn/Documents/credentials.json`.

## Stop or restart

```bash
systemctl --user stop hr-scanner hr-web hr-ollama
```

After changing code or `.env`:

```bash
systemctl --user restart hr-ollama hr-web hr-scanner
```

## Run manually

Stop the services first to avoid running two workers. Open three terminals, change to the project folder in each, and run one command per terminal:

```bash
# Terminal 1: Ollama
./scripts/dev.sh ollama

# Terminal 2: dashboard
./scripts/dev.sh web

# Terminal 3: Drive scanner and CV worker
./scripts/dev.sh service
```

Press Ctrl+C in each terminal to stop it.

## Model settings

`.env` selects the models and stores local paths:

- `HR_MODEL`: `qwen3:4b` for CV assessment.
- `HR_EMBED_MODEL`: `nomic-embed-text:v1.5` for applicant search.
- `HR_SCAN_SECONDS`: `300` between scheduled Drive checks.
- `HR_OLLAMA_BINARY`: the Ollama executable under `runtime/ollama`.
- `OLLAMA_MODELS`: the model files under `runtime/ollama-models`.

This installation uses the CPU because GPU inference hung during setup. Proof-of-concept mode is already enabled. It uses draft scoring rules and allows the configured model to run without passing the full acceptance check. Results still need a quick look before you rely on them.

Drive sharing has been left as you requested. If Drive report uploads are blocked, download the report from the dashboard.

## Check a problem

```bash
systemctl --user status hr-ollama hr-web hr-scanner
journalctl --user -u hr-scanner -n 60 --no-pager
journalctl --user -u hr-ollama -n 60 --no-pager
.venv/bin/python -m hr_agent.cli doctor
```

To retry failed files after fixing the cause:

```bash
.venv/bin/python -m hr_agent.cli retry
```

## Files

```text
hr_agent/                 Python application
  dashboard.py            Dashboard routes and API
  templates/dashboard.html
  static/dashboard.css
  static/dashboard.js
  screening_agent.py      Model-driven CV assessment
  ollama_client.py        Calls to the local model
  applicant_search.py     Search indexing and applicant questions
  scanner.py              Google Drive discovery
  job_queue.py            CV processing steps and retries
  database.py             SQLite access
  service.py              Scanner and worker lifecycle
  migrations/             Database schema
scripts/                  Setup and service commands
role-requirements/        Job descriptions and scoring drafts
tests/                    Automated tests and fixtures
data/                     CVs, reports, login token and database (private)
runtime/                  Ollama executable and downloaded models
.venv/                    App dependencies
.test-venv/               Test dependencies
docs/technical-notes.md   Earlier implementation notes
```

Run tests with `.test-venv/bin/python -m pytest -q`.

Keep `data`, `.env`, and the OAuth client file out of source control. The old `Documents/Codex` folder is retained as a backup while you test this cleaned copy.
