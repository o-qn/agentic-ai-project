# CV Screening

A local CV-screening application that reads CVs from Google Drive, evaluates them against job descriptions, and displays the results in a lightweight dashboard.

The application uses Ollama to run language models locally.

## Start the app

The Python environment, application dependencies, and required models should already be available in the installation.

From the project directory, start the services:

```bash
systemctl --user start hr-ollama hr-web hr-scanner
```

Open the dashboard at:

```text
http://127.0.0.1:8787
```

Enable **Auto process** to automatically process queued CVs.

Google Drive is checked periodically for new files. The default scan interval is five minutes. Use **Check Drive now** in the dashboard to trigger an additional scan immediately.

The progress indicator displays the current file and processing stage. During model inference, it shows elapsed time and model turns rather than an estimated completion percentage.

## Google sign-in

Google authentication tokens are stored locally under `data/`.

If authentication expires or you need to sign in again, run:

```bash
.venv/bin/python -m hr_agent.cli auth
```

Follow the browser sign-in process.

The OAuth client credentials file is configured using `HR_CREDENTIALS` in `.env`.

Do not commit authentication tokens, OAuth credentials, or `.env` files to source control.

## Stop or restart

Stop all services with:

```bash
systemctl --user stop hr-scanner hr-web hr-ollama
```

After changing application code or `.env`, restart the services:

```bash
systemctl --user restart hr-ollama hr-web hr-scanner
```

## Run manually

Stop the systemd services before running the application manually to avoid starting multiple workers.

Open three terminals in the project directory and run one command in each.

```bash
# Terminal 1: Ollama
./scripts/dev.sh ollama

# Terminal 2: dashboard
./scripts/dev.sh web

# Terminal 3: Drive scanner and CV worker
./scripts/dev.sh service
```

Press `Ctrl+C` in each terminal to stop the processes.

## Model settings

Model and runtime configuration is stored in `.env`.

Key settings include:

* `HR_MODEL`: Language model used for CV assessment. Default: `qwen3:4b`
* `HR_EMBED_MODEL`: Embedding model used for applicant search. Default: `nomic-embed-text:v1.5`
* `HR_SCAN_SECONDS`: Interval between scheduled Google Drive scans. Default: `300`
* `HR_OLLAMA_BINARY`: Path to the Ollama executable
* `OLLAMA_MODELS`: Path to the local Ollama model files

The application can run entirely on CPU if GPU inference is unavailable or disabled.

## Screening mode

The application may be configured to run in proof-of-concept mode.

In this mode, CV assessments can use draft scoring rules and may run without the full production acceptance checks.

Screening results should therefore be reviewed before they are used for hiring decisions.

The system is intended to assist with CV review rather than make final hiring decisions automatically.

## Reports

Generated reports are available through the dashboard.

If uploading a report to Google Drive is unavailable, download the report directly from the dashboard instead.

## Troubleshooting

Check the status of the application services:

```bash
systemctl --user status hr-ollama hr-web hr-scanner
```

View recent scanner logs:

```bash
journalctl --user -u hr-scanner -n 60 --no-pager
```

View recent Ollama logs:

```bash
journalctl --user -u hr-ollama -n 60 --no-pager
```

Run the built-in diagnostics:

```bash
.venv/bin/python -m hr_agent.cli doctor
```

To retry files that previously failed after fixing the underlying issue:

```bash
.venv/bin/python -m hr_agent.cli retry
```

## Project structure

```text
hr_agent/                 Python application
  dashboard.py            Dashboard routes and API
  templates/
    dashboard.html        Dashboard interface
  static/
    dashboard.css         Dashboard styles
    dashboard.js          Dashboard client-side logic
  screening_agent.py      Model-driven CV assessment
  ollama_client.py        Local Ollama model integration
  applicant_search.py     Search indexing and applicant queries
  scanner.py              Google Drive discovery
  job_queue.py            CV processing steps and retries
  database.py             SQLite database access
  service.py              Scanner and worker lifecycle
  migrations/             Database schema migrations

scripts/                  Setup and service commands
role-requirements/        Job descriptions and scoring rules
tests/                    Automated tests and fixtures
data/                     CVs, reports, tokens, and database data
runtime/                  Ollama executable and downloaded models
.venv/                    Application Python environment
.test-venv/               Test Python environment
docs/
  technical-notes.md      Implementation and technical notes
```

## Tests

Run the automated test suite with:

```bash
.test-venv/bin/python -m pytest -q
```

## Private files

The following files and directories may contain private information or credentials and should not be committed to source control:

```text
.env
data/
OAuth client credentials
```

Make sure these paths are covered by `.gitignore` before publishing or sharing the repository.

CV files, generated reports, authentication tokens, and the application database may contain personal or sensitive information and should be handled accordingly.
