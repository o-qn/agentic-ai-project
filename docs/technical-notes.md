# Fieldwork — local HR screening assistant

## Quick personal proof of concept

Run `.venv/bin/python -m hr_agent.cli poc` to use the installed model without capability approval and activate draft scoring rules automatically from the current job descriptions. The dashboard labels this mode explicitly; results may be wrong. It does not claim human rubric approval or successful model validation. Source citation/schema checks and Drive privacy checks still apply. After Google login (`.venv/bin/python -m hr_agent.cli auth`), start the installed services with `systemctl --user restart hr-ollama hr-web hr-scanner`.

While Drive sharing stays public, reports remain available locally from `/api/roles/ROLE_ID/xlsx` after generation; uploads and dependent CV moves remain pending. POC mode does not change sharing.

Fieldwork discovers CVs in Google Drive, assesses documented qualifications against an HR-approved rubric, maintains cumulative Excel reports, and provides a local dashboard with cited candidate search. HR makes hiring, rejection and interview decisions.

**Current release:** source and local synthetic workflow are implemented. See `deployment/ACCEPTANCE.md` for what has actually been tested and what still requires live acceptance. Live processing starts disabled. The configured Recruitment root was verified on 14 September 2026 to have an **anyone-with-link reader permission**; report uploads are blocked until HR-only access is verified.

## Quick start on Linux

Requirements: Python 3.11 or newer, cgroup v2/systemd for normal deployment, Poppler (`pdftoppm`), Tesseract, and a local Ollama installation. Python 3.14 is the tested interpreter. On Debian/Ubuntu, the OS packages are normally `python3-venv poppler-utils tesseract-ocr`; other distributions use different names. Runtime Python packages, build tools, and tests have separate hash-pinned lock files.

```bash
./scripts/setup.sh
# Review .env. The supplied OAuth client file is referenced by path, not copied.
.venv/bin/hr-agent auth
.venv/bin/hr-agent scan
```

`auth` opens Google's desktop OAuth flow with a loopback callback. Sign in to the account that owns/manages Recruitment and review Google's consent screen. The service needs Drive access to read existing arbitrary CVs, create/update reports and move files; the limited `drive.file` scope cannot discover every pre-existing file. Tokens are saved under the private data directory with mode 0600. Do not put tokens or OAuth client secrets in Git, shared reports or logs. Access granted to a Codex connector does not authorize this standalone service.

Configure installed local model names in `.env`, then:

```bash
./scripts/dev.sh ollama
# In a second terminal:
.venv/bin/hr-agent model-check
./scripts/dev.sh web
# In a third terminal:
./scripts/dev.sh service
```

The web UI is at `http://127.0.0.1:8787`. Foreground launchers are useful for development; use the systemd units for ordinary operation. The scheduler scans on startup and every 300 seconds even while automatic processing is off; the toggle controls processing. **Check Drive now** requests an immediate scan from the running scheduler.

No inference request uses a paid or cloud fallback. `scripts/ollama-cpu.sh` disables Ollama cloud features and hides GPU devices. Every model request also specifies CPU-only inference and at most six threads. Screening and embeddings share an inference lock; `keep_alive=0` unloads after each request so only one model remains loaded at a time. This trades throughput for predictable memory use.

## Existing Drive layout and job descriptions

Root ID: `14lhL4U0mzSOSvPUXIV6Sv20Kfx1aeTbr`.

The real role IDs, downloaded descriptions and **unapproved** rubric drafts are in `role-requirements/manifest.json`. The accountant folder is currently named `Acountant `; it remains identified by its existing ID. The setup command recognizes this spelling instead of creating a second accountant folder.

```text
Recruitment/
  Role/
    job_description.txt
    Incoming CVs/
    Processed CVs/
    Needs Review/
    Reports/candidates.xlsx
```

The scanner discovers role folders, ensures the four workflow subfolders exist, and reads `job_description.txt`. It accepts PDF, DOCX and UTF-8 TXT in Incoming CVs or directly in the role folder. Unsupported submissions enter review. It excludes job descriptions and both `candidates.xlsx` and legacy `candidate_ranking.xlsx` reports. It never discovers new applications from Processed CVs or Reports; it rechecks only already-known files there for changes/deletion.

`hr-agent setup-drive` creates missing initial role folders. `scan` uses whatever role IDs already exist. Neither command changes sharing permissions.

Keep the root, reports, review folders and CV archive restricted to HR. Applicant uploads need a separate controlled collection channel; do not give applicants access to the whole recruitment tree. Before each report upload, the adapter verifies root, report folder and existing report permissions. It rejects `anyone`, domain-wide access, and additional users/groups that are not explicitly listed in `HR_ALLOWED_PRINCIPALS`. Owners are allowed. If access cannot be verified, upload fails closed and the checkpoint remains retryable.

## Rubrics and rankings

Review the actual job description, use **Draft from job description**, edit the draft and approve it with an HR actor name. The saved JSON drafts in `role-requirements/` are also available for review. All three current descriptions require two years of relevant professional experience and explicitly require no degree/certification; those drafts assign education/certification no weight.

Each criterion defines its weight and the evidence for supported, partial and not-demonstrated levels. Weights total 100. Code calculates `weight × 1`, `weight × 0.5` or `weight × 0`; the model cannot supply a final numerical score. Positive matches need exact quotes from inspected source sections. Every criterion and extracted section must be covered before submission. These checks validate citation existence and completeness, not whether the model interpreted every statement correctly.

Approved rubric versions are immutable. A changed, missing or unreadable job description pauses ranking. HR must approve a new version and choose the supported `reassess_all` plan. Old assessments remain auditable but do not mix with the current ranking. Names, photos, age, gender, religion, nationality, disability and other irrelevant personal attributes are excluded from scoring instructions. HR must inspect criteria for job relevance at approval.

Only completed, current, comparable assessments rank. Scores tie without an additional merit tiebreaker. The first three cards are a display limit; a visible indicator points to ties extending beyond them. “Not demonstrated” means missing CV evidence, not absence of a skill. Scores are not predictions or probabilities of job performance.

## Processing and recovery

SQLite stores source versions, durable jobs, evidence, assessments, report revisions, embeddings, review decisions and append-only action records. A scheduler lifetime lock and renewable database scan lease prevent duplicate scheduling; one worker lock prevents overlapping processing. New scans do not wait for inference.

Jobs checkpoint at `download → extract → assess → report → move → done`. Report upload must be confirmed before the associated CV moves. New or revised source contents invalidate old rankings and chunks immediately on discovery. Exact content duplicates within a role retain their association but receive no second ranking place. If the canonical source is removed or changes, a remaining duplicate is promoted for assessment. Applications in different roles are independent.

Downloads, uploads and moves retry up to five times with capped exponential backoff. Reserved Drive file IDs make uncertain report creation retryable without creating a second report. Existing report revisions and current parents are checked before retrying uploads/moves. Failed uploads/moves do not discard assessments. Index work checkpoints independently, one embedding per worker tick, and never forces rescoring.

```bash
.venv/bin/hr-agent doctor
.venv/bin/hr-agent retry
```

`retry` requeues failed stages at their saved checkpoints, failed indexing, and paused model jobs. It does not erase successful assessments. The dashboard also supports per-application retry. Timestamped historical errors remain visible. Pending report revisions mean local results are saved but Drive is not synchronized.

Back up the private data directory with services stopped, or use SQLite's backup API while live. Do not copy just the database file during WAL activity. Keep original source snapshots and the database together. Schema migrations run under a process lock and apply transactionally; never delete the database to recover a failed upload.

## Extraction and resource boundaries

- Maximum download: 20 MiB by default, with connect/read and total download limits.
- Maximum PDF: 40 pages; maximum 160 extracted sections. Exceeding a complete-review budget routes to HR rather than truncating.
- Extraction runs in a separate process with a 1.5 GiB address-space limit, CPU/file-size limits and a maximum 180-second wall time. Timeout kills its process group, including OCR children.
- PDF image pages and mixed image/text pages use local OCR. Low confidence or unreadable content goes to review. `HR_OCR_LANGUAGE=eng` is default; install the matching Tesseract language packs before selecting another language.
- DOCX includes nested tables and header/footer text. Unsupported drawings, text boxes, embedded objects, notes or tracked changes require manual verification.
- The agent permits only six validated evidence/review tools, at most 12 model turns, fixed prompt/output sizes, request timeout and a 900-second total assessment budget. Persisted turn/time budgets survive retries.
- CV text is untrusted data. It cannot change the rubric, execute a shell command, select a different applicant or invoke arbitrary Drive operations.

Contact details are stored separately. Only explicitly labelled names are extracted automatically in this release; otherwise the UI says “Name not provided.” A CV name/email is never treated as verified identity.

## Review and document holds

Suspected manipulation/non-applications and extraction ambiguity stay unranked. Short CVs, formatting, language, gaps or weak qualifications alone are not spam. HR can dismiss a flag, retry, confirm a document-spam finding with cited evidence, or restore it. Confirmed document holds store a reason, creator, source evidence and expiry (90 days by default, configurable 1–365 days). Future exact document matches are held for review, never silently discarded.

This release implements **document-hash holds**. It deliberately does not create or match person-level blacklists without a trustworthy external identity source. The schema reserves that concept, but names and CV emails do not supply verified identity. The read-only chatbot cannot create holds or reject applicants. Detailed hold records stay in the restricted application database and HR screen, not the XLSX report.

## Reports and candidate search

Each role gets one cumulative `Reports/candidates.xlsx` containing Ranked Candidates, Score Evidence, Needs Review and Processing Log. Updates use the same reserved Drive ID. Spreadsheet formula-like values are escaped. CSV exports include the ranked table. The evidence screen shows criterion weights/awarded points, explanations and exact quoted text with source locations.

Candidate search uses database rankings/statuses for exact facts, scans all current extracted CV sections for requested terms, and optionally retrieves local semantic suggestions. “Who has Python experience?” searches the selected role. “Who has SQL and Excel?” uses AND matching. “Compare Alex and Sam” or explicit application IDs retrieves their evidence and exact scores. Lists and ranking questions use database records. If ties leave no rank two, the result is empty rather than substituting another applicant.

Lexical matching can miss synonyms or return literal mentions that require interpretation; semantic suggestions are explicitly partial. Answers cite CV excerpts, identify missing extraction/indexing, and never describe CV claims as independently verified proficiency. There is no unrestricted generative answer engine. This deliberate constrained interface needs user testing for broader conversational phrasing. Deleted/changed sources invalidate their old chunks. Changing the embedding model name or digest rebuilds the index without rescoring.

## Normal operation with systemd

```bash
.venv/bin/python scripts/render_services.py
systemd-analyze --user verify deployment/systemd/*.service
./scripts/install-services.sh
systemctl --user start hr-ollama hr-web hr-scanner
```

Generated units can be inspected before installation. The installer writes user units and enables them; it does not start them. An administrator must enable user lingering for services to start at boot without an interactive login. The user bus and cgroup v2 must be available. The Codex sandbox used for development cannot connect to the host user service bus, so host installation/enforcement must be demonstrated separately.

`hragent.slice` sets a combined 4 GiB limit for web, scanner, worker, extraction and OCR. `hr-ollama.service` has a separate 16 GiB limit, 600% CPU quota, one loaded model/request, and private devices. Other applications remain outside these limits. Foreground runs do not apply these cgroup limits; use them for development, not proof of the deployment budget.

```bash
systemctl --user status hr-ollama hr-web hr-scanner
systemctl --user show hragent.slice -p MemoryMax -p MemoryCurrent
systemctl --user show hr-ollama.service -p MemoryMax -p MemoryCurrent
journalctl --user -u hr-scanner -u hr-web -u hr-ollama
```

The five-minute interval describes discovery while the host is awake, connected and running. Processing completion depends on queue length and CPU inference speed. Missed intervals are shown, and the service catches up on startup. An always-on Linux host is needed for uninterrupted operation.

## Tests and acceptance

```bash
.venv/bin/python -m pip install --require-hashes -r test-requirements.lock
.venv/bin/python -m pytest -q
.venv/bin/python scripts/benchmark.py --repeat 2
# With configured local models:
.venv/bin/hr-agent model-check
.venv/bin/python scripts/benchmark.py --real --repeat 2
```

The ordinary benchmark uses a scripted model/in-memory Drive but real extraction, OCR, SQLite and XLSX generation; the output is clearly labelled. `model-check` exercises local structured output, tool use, citation validation, short-CV scoring and prompt-injection routing twice. Approval is tied to model/embedding digests, prompt version and runtime budgets. It does not substitute for a representative HR review of evidence interpretation, languages or live Drive/systemd behavior. Synthetic fixtures and expected outcomes are checked in under `tests/fixtures/`.

The dashboard is local-only and rejects remote clients/untrusted Host headers. It uses CSRF tokens, strict same-site cookies and safe text rendering. The current authorization boundary is the local Linux user, who can access every configured role. Do not expose or reverse-proxy it remotely; authentication and multi-user role authorization must be added first.

## Technical references

[Ollama chat/tool API](https://docs.ollama.com/api/chat), [local embeddings](https://docs.ollama.com/api/embed), [Ollama CPU/GPU selection](https://docs.ollama.com/gpu), [Ollama Linux installation](https://docs.ollama.com/linux), [Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app), [Drive file updates](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/update).
