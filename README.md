# CV Screening

A local HR screening assistant for discovering CVs from Google Drive, extracting candidate data, assessing applicants, indexing them for search, and producing reviewable reports.

The application is a proof of concept. Assessment output and automatic draft scores must be reviewed by a human before any hiring decision.

## What the application does

- Watches a Google Drive folder for new CVs and processes PDF, DOCX, and TXT files.
- Extracts structured candidate data and preserves the source text and source link.
- Assesses applicants with either local Ollama or the Agent Router Codex CLI path.
- Stores candidates, assessment results, indexing state, usage data, and job state in the configured data directory.
- Provides keyword and semantic applicant search.
- Provides evidence-grounded role answers. Retrieved CV passages are shown with the answer, while stored assessment scores remain authoritative.
- Generates XLSX and CSV reports.
- Supports local Ollama embeddings or opt-in Voyage hosted embeddings, selectable from the dashboard.

The default deployment is local-only. It is designed for sensitive CV data and has no public-user authentication layer.

## Quick start

From the project directory:

~~~bash
cd "/home/qn/Documents/Agentic Ai project"
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
~~~

Run the dashboard and background services with:

~~~bash
scripts/dev.sh ollama
scripts/dev.sh web
scripts/dev.sh service
~~~

The dashboard is available at http://127.0.0.1:8787.

On a host with the user systemd units installed, the equivalent commands are:

~~~bash
systemctl --user enable --now hr-ollama.service hr-web.service hr-scanner.service
systemctl --user status hr-web.service hr-scanner.service
~~~

The dashboard's **Auto process** switch controls whether queued Drive work is processed. **Check Drive now** queues an immediate scan. A scan runs only while the scanner service is running.

## Google Drive authentication

Set the Google OAuth client path and Drive folder IDs in .env as described in .env.example, then authorize:

~~~bash
.venv/bin/python -m hr_agent.cli auth
~~~

The authorization token is stored locally in token.json (or the configured token path). It is ignored by Git.

If the dashboard shows:

~~~text
invalid_grant: Token has been expired or revoked.
~~~

run the auth command again, complete the browser flow, restart the scanner, and press **Check Drive now**:

~~~bash
systemctl --user restart hr-scanner.service
~~~

If Drive is unavailable, the worker can appear idle because no scan job can complete. Check the service log:

~~~bash
journalctl --user -u hr-scanner.service -n 100 --no-pager
~~~

## Dashboard controls

The dashboard has three independent controls:

- **Assessment provider/model** chooses local Ollama or Agent Router for candidate assessment.
- **Embedding provider** chooses Local Ollama or Voyage hosted embeddings.
- **Auto process** enables or pauses queued processing.

Changing the embedding provider marks completed applications as pending for reindexing. The next Auto process run reindexes them; it does not rescore candidates. The provider shown in **Settings & usage** is the effective runtime provider, including a dashboard selection stored in the database.

Voyage is offered in the selector only when both HR_EMBED_HOSTED_MODEL and a Voyage key are configured. Selecting Voyage requires an explicit confirmation because CV text is sent to the hosted provider.

## Configuration

The main settings are in .env:

~~~dotenv
# Assessment
HR_MODEL_PROVIDER=ollama              # ollama or agentrouter
HR_MODEL=qwen2.5:7b
HR_OLLAMA_URL=http://127.0.0.1:11434

# Embeddings
HR_EMBED_PROVIDER=ollama               # default/fallback: ollama or voyage
HR_EMBED_MODEL=nomic-embed-text
HR_EMBED_URL=http://127.0.0.1:11434
HR_EMBED_HOSTED_MODEL=                  # set to voyage-3.5-lite or voyage-3 for Voyage

# Processing
HR_SCAN_SECONDS=300
HR_DATA_DIR=./data
~~~

The dashboard provider selection overrides HR_EMBED_PROVIDER at runtime. Leaving the environment value as ollama keeps local embeddings as the safe default while still allowing an authorized user to select Voyage from the dashboard.

Additional settings for Google Drive, Agent Router, PostgreSQL, and the report folders are documented in .env.example.

## Testing Voyage embeddings

1. Put the key in the ignored file VOYAGE_API_KEY or VOYAGE_API_KEY.txt, with only the key on the first line:

   ~~~bash
   chmod 600 VOYAGE_API_KEY
   ~~~

2. Set a hosted model in .env:

   ~~~dotenv
   HR_EMBED_HOSTED_MODEL=voyage-3.5-lite
   ~~~

   If that model is unavailable for the account, try voyage-3.

3. Restart the web and scanner services:

   ~~~bash
   systemctl --user restart hr-web.service hr-scanner.service
   ~~~

4. Open **Settings & usage**, choose **Voyage hosted**, confirm the change, and let Auto process reindex the pending applications.

The app records embedding events, section counts, characters, and provider-reported usage when the provider returns it. Voyage plan limits and quota errors are enforced by Voyage; the free plan does not change the setup steps. Because CV text leaves the machine, use the hosted option only when that data handling is acceptable.

## Assessment modes

### Local Ollama

Ollama runs on the local machine. Pull the configured model before starting the service:

~~~bash
ollama pull qwen2.5:7b
ollama pull nomic-embed-text
~~~

Use a model available on the machine, or change HR_MODEL and HR_EMBED_MODEL in .env.

### Agent Router through Codex CLI

Set:

~~~dotenv
HR_MODEL_PROVIDER=agentrouter
HR_MODEL=deepseek-v4-flash
~~~

Put the Agent Router key in the ignored AGENTROUTER_API_KEY.txt file. The Agent Router assessment path uses the installed Codex CLI with a restricted tool protocol. It is intended for a proof-of-concept evaluation; automatic draft scoring and model output are unvalidated and must be reviewed.

Agent Router assessment is separate from the embedding provider. For example, it is valid to use Agent Router for assessment and Voyage for embeddings.

## Grounded answers

The role answer endpoint is:

~~~text
GET /api/roles/<role_id>/answer
~~~

The dashboard's **Grounded answer** action retrieves relevant passages from indexed CVs and asks the selected assessment model to answer from those passages. The response includes evidence passages and source links. If generation is unavailable, the retrieval result and an error are returned instead of silently showing an ungrounded answer.

Grounded answers do not change assessment scores, ranking, or report data. Treat them as a review aid and verify the cited CV evidence.

## Drive folders and processing flow

Configure separate Drive folders for incoming CVs and generated reports. A normal flow is:

1. The scanner discovers a new file.
2. The worker downloads and extracts its text.
3. The candidate is assessed and stored.
4. The candidate is embedded and indexed.
5. The source CV is moved to the processed location, if configured.
6. Reports can be regenerated from the stored results.

CV uploads use the existing sharing settings of the Incoming CV folder; the application does not require that folder to be private or domain-restricted. Generated report uploads remain protected by the report-sharing checks.

## Reports and search

Reports are generated from stored database results, so regenerating a report does not call the model again. The workbook contains candidate data, assessment fields, score breakdowns, and review metadata. CSV export is available for simple downstream analysis.

Search combines lexical matching with semantic retrieval when embeddings are available. Reindexing is safe to repeat. Changing the embedding provider reindexes applications but does not rescore them.

## Optional PostgreSQL and pgvector mirror

SQLite remains the authoritative application store. PostgreSQL/pgvector is an optional mirror for vector search experiments:

~~~bash
docker compose up -d postgres
POSTGRES_URL=postgresql://hr:hr@127.0.0.1:5432/hr \
  .venv/bin/python -m hr_agent.cli pgvector-sync
~~~

Keep POSTGRES_URL.txt local and ignored. A failed mirror sync does not replace the SQLite data.

## Security and private files

Never commit or paste these files into source control:

- .env
- credentials.json
- token.json
- AGENTROUTER_API_KEY.txt
- VOYAGE_API_KEY or VOYAGE_API_KEY.txt
- POSTGRES_URL.txt
- data/ and runtime database/usage files

The dashboard binds to 127.0.0.1 and does not provide public authentication. Do not expose it with a Cloudflare Quick Tunnel or another public tunnel while it contains real CVs. Use a proper authenticated deployment before making it reachable by other users.

Check the ignore rules before adding files:

~~~bash
git check-ignore -v .env token.json VOYAGE_API_KEY VOYAGE_API_KEY.txt data/
git ls-files | rg '(^|/)(\.env|credentials\.json|token\.json|VOYAGE_API_KEY|AGENTROUTER_API_KEY|POSTGRES_URL)'
~~~

The second command should return no credential files.

## Troubleshooting

Inspect the application health and configuration:

~~~bash
.venv/bin/python -m hr_agent.cli doctor
~~~

Useful service commands:

~~~bash
systemctl --user status hr-ollama.service hr-web.service hr-scanner.service
journalctl --user -u hr-web.service -n 100 --no-pager
journalctl --user -u hr-scanner.service -n 100 --no-pager
~~~

Common fixes:

- **Worker idle:** enable **Auto process**, press **Check Drive now**, and confirm the scanner service is running.
- **invalid_grant:** re-run hr_agent.cli auth, then restart hr-scanner.service.
- **Voyage unavailable in the selector:** set HR_EMBED_HOSTED_MODEL, verify the key file name/content, and restart the web service.
- **Embedding errors:** check the selected provider, model name, provider quota, and the usage panel.
- **Grounded answer has no result:** inspect the returned error, confirm that the role has indexed applications, and verify that the selected model is available.
- **Ollama errors:** confirm Ollama is running and that the configured model has been pulled.

## Tests

Run the test suite from the project directory:

~~~bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  .test-venv/bin/python -m pytest -q -p no:cacheprovider
~~~

The suite covers Drive sharing behavior, scanner state, assessment providers, embedding provider selection, Voyage usage handling, RAG retrieval, grounded-answer transport, report generation, and dashboard endpoints.

## Project structure

~~~text
hr_agent/
  api.py                 FastAPI application and dashboard endpoints
  applicant_search.py    indexing, retrieval, and grounded-answer context
  dashboard.py           dashboard state and controls
  drive_connector.py     Google Drive discovery, download, and upload
  embedding_client.py    local and Voyage embedding clients
  grounded_answer.py     evidence-grounded answer orchestration
  worker.py              queued scan/process jobs
  reports.py             XLSX and CSV generation
  config.py              environment configuration
templates/               HTML templates
static/                  dashboard JavaScript and CSS
scripts/                 local development and service helpers
systemd/                 user service unit templates
tests/                  automated tests
data/                   local runtime state (ignored)
~~~

