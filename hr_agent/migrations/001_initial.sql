CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO settings VALUES('automatic','false');
CREATE TABLE leases(name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE roles(id TEXT PRIMARY KEY, name TEXT NOT NULL, folders TEXT NOT NULL DEFAULT '{}',
 jd TEXT NOT NULL DEFAULT '', jd_hash TEXT NOT NULL DEFAULT '', rubric_id INTEGER,
 paused INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 0,
 synced_revision INTEGER NOT NULL DEFAULT 0, report_id TEXT, uploaded_at REAL,
 active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE rubrics(id INTEGER PRIMARY KEY, role_id TEXT NOT NULL REFERENCES roles(id),
 jd_hash TEXT NOT NULL, body TEXT NOT NULL, approved_by TEXT, approved_at REAL, created REAL NOT NULL);
CREATE TABLE applications(id INTEGER PRIMARY KEY, role_id TEXT NOT NULL REFERENCES roles(id),
 file_id TEXT NOT NULL, filename TEXT NOT NULL, version TEXT NOT NULL,
 hash TEXT, source_path TEXT, sections TEXT, contact TEXT NOT NULL DEFAULT '{}',
 active INTEGER NOT NULL DEFAULT 1, duplicate_of INTEGER REFERENCES applications(id),
 status TEXT NOT NULL DEFAULT 'queued', review_reason TEXT, review_evidence TEXT,
 review_dismissed INTEGER NOT NULL DEFAULT 0, required_revision INTEGER NOT NULL DEFAULT 0,
 index_status TEXT NOT NULL DEFAULT 'pending', index_model TEXT, index_attempts INTEGER NOT NULL DEFAULT 0,
 index_next REAL NOT NULL DEFAULT 0, index_error TEXT, created REAL NOT NULL, updated REAL NOT NULL,
 UNIQUE(role_id,file_id));
CREATE TABLE source_versions(id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
 version TEXT NOT NULL, hash TEXT NOT NULL, path TEXT NOT NULL, created REAL NOT NULL,
 UNIQUE(application_id,version,hash));
CREATE TABLE jobs(id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
 version TEXT NOT NULL, rubric_id INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL DEFAULT '',
 step TEXT NOT NULL DEFAULT 'download', state TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
 error TEXT, error_at REAL, started REAL, updated REAL NOT NULL, agent_state TEXT NOT NULL DEFAULT '{}',
 UNIQUE(application_id,version,rubric_id));
CREATE UNIQUE INDEX processing_key ON jobs(application_id,content_hash,rubric_id) WHERE content_hash != '' AND state != 'superseded';
CREATE TABLE assessments(id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
 version TEXT NOT NULL, rubric_id INTEGER NOT NULL REFERENCES rubrics(id),
 body TEXT NOT NULL, score REAL NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
 created REAL NOT NULL, UNIQUE(application_id,version,rubric_id));
CREATE TABLE criterion_evidence(id INTEGER PRIMARY KEY, assessment_id INTEGER NOT NULL REFERENCES assessments(id),
 criterion_id TEXT NOT NULL, weight REAL NOT NULL, points REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE report_revisions(role_id TEXT NOT NULL REFERENCES roles(id), revision INTEGER NOT NULL,
 state TEXT NOT NULL, path TEXT, sha256 TEXT, uploaded_at REAL, PRIMARY KEY(role_id,revision));
CREATE TABLE chunks(id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
 role_id TEXT NOT NULL REFERENCES roles(id), version TEXT NOT NULL, section_id TEXT NOT NULL,
 location TEXT NOT NULL, text TEXT NOT NULL, model TEXT NOT NULL, embedding TEXT,
 UNIQUE(application_id,version,section_id,model));
CREATE TABLE review_decisions(id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
 action TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE blacklist(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('document','person')),
 identifier TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL, actor TEXT NOT NULL,
 created REAL NOT NULL, expires REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE audit(id INTEGER PRIMARY KEY, event TEXT NOT NULL, entity TEXT NOT NULL,
 detail TEXT NOT NULL, created REAL NOT NULL);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'Audit events are append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'Audit events are append-only'); END;
INSERT INTO schema_version VALUES(1);
