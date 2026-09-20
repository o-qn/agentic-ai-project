"""PostgreSQL + pgvector mirror (#2): ready-to-activate, inert until a DSN is configured.

These tests use an in-memory ``FakePg`` in place of a live connection — no Postgres, no
psycopg. The fake actually stores the copied rows and computes cosine ordering, so migration,
validation (row counts + exact scores), rollback and filtered search are exercised for real.
A live pgvector instance must still be verified before production use; that is out of scope
here and gated behind an operator running pg-migrate/pg-validate.

The tests prove the properties the spec requires:
  * inert until a DSN is set AND a passing validation enabled it (SQLite stays authoritative);
  * migration copies every table and preserves scores exactly, and refuses to clobber;
  * validation catches any score drift (guards "never overwrite scores");
  * rollback drops the mirror without touching SQLite;
  * retrieval filters by role/applicant/active/version/model and returns passages only.
"""
import json
import math
from dataclasses import replace

import pytest
from hr_agent import pgvector, applicant_search, embedding
from hr_agent.demo import drain

DSN = 'postgresql://hr:pw@127.0.0.1:5432/hr'


# --- in-memory Postgres double ---------------------------------------------

def _cos(a, b):
    if len(a) != len(b):
        return -1.0
    denom = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / denom if denom else 0.0


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, list(params) if params else []))
        low = ' '.join(sql.lower().split())
        if low.startswith('create extension'):
            self.result = []
        elif low.startswith('create schema'):
            self.result = []
        elif low.startswith('drop schema'):
            self.conn.store.clear()
            self.result = []
        elif low.startswith('create table'):
            self.conn.store.setdefault(self._table(sql), [])
            self.result = []
        elif low.startswith('insert into'):
            table = self._table(sql)
            cols = [c.strip() for c in sql[sql.index('(') + 1:sql.index(')')].split(',')]
            self.conn.store.setdefault(table, []).append(dict(zip(cols, params)))
            self.result = []
        elif low.startswith('select count(*)'):
            self.result = [(len(self.conn.store.get(self._table(sql), [])),)]
        elif '.chunks c join' in low and 'order by' in low:
            self.result = self._search(sql, params)
        elif low.startswith('select application_id') and '.assessments' in low:
            self.result = [(r['application_id'], r['version'], r['rubric_id'], r['score'])
                           for r in self.conn.store.get('assessments', [])]
        else:
            self.result = []

    @staticmethod
    def _table(sql):
        # First "<schema>.<table>" occurrence; every statement we emit is schema-qualified.
        import re
        return re.search(r'\b\w+\.(\w+)', sql).group(1)

    def _search(self, sql, params):
        params = list(params)
        role, model = params[0], params[1]
        limit, vector = int(params[-1]), json.loads(params[-2])
        allowed = set(params[2]) if 'any(' in sql.lower() else None
        apps = {a['id']: a for a in self.conn.store.get('applications', [])}
        hits = []
        for chunk in self.conn.store.get('chunks', []):
            app = apps.get(chunk['application_id'])
            if not app or chunk['role_id'] != role or chunk['model'] != model:
                continue
            if chunk['version'] != app['version'] or app['active'] != 1 or app['duplicate_of'] is not None:
                continue
            if allowed is not None and chunk['application_id'] not in allowed:
                continue
            hits.append(chunk)
        hits.sort(key=lambda c: -_cos(vector, json.loads(c['embedding'])))
        return [(c['id'], c['application_id'], c['section_id'], c['location'], c['text'])
                for c in hits[:limit]]

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)


class FakePg:
    def __init__(self):
        self.store = {}
        self.executed = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


# --- helpers ---------------------------------------------------------------

def _populate(system):
    """Seed a role, index one CV locally so SQLite holds chunks + a score."""
    config, db, drive, model, scanner, worker = system
    role = db.one('SELECT * FROM roles WHERE active=1 ORDER BY id')
    incoming = json.loads(role['folders'])['Incoming CVs']
    drive.add('cv-a', 'a.txt', incoming, 'Name: A Candidate\nBuilt a Python application.\nBuilt a SQL database.\n')
    scanner.run()
    drain(worker)
    while applicant_search.index_one(config, db, model):
        pass
    identity = embedding.make_embedder(config, model).identity()
    return role['id'], identity


# --- tests -----------------------------------------------------------------

def test_pgvector_stays_inert_until_configured_and_verified(system):
    config, db = system[0], system[1]
    # No DSN => nothing is configured or enabled, whatever the stored flag says.
    assert not pgvector.pg_configured(config)
    db.set('pg_search_enabled', True)
    assert not pgvector.enabled(config, db)
    # DSN present but no passing validation => still not enabled; SQLite stays the search path.
    configured = replace(config, pg_url=DSN)
    assert pgvector.pg_configured(configured)
    db.set('pg_search_enabled', False)
    assert not pgvector.enabled(configured, db)
    db.set('pg_search_enabled', True)
    assert pgvector.enabled(configured, db)


def test_migrate_then_validate_copies_every_table_and_preserves_scores(system):
    config, db = system[0], system[1]
    _populate(system)
    score_before = db.one('SELECT score FROM assessments LIMIT 1')['score']
    fake = FakePg()

    report = pgvector.migrate(config, db, fake)
    assert report['copied']['chunks'] >= 1 and report['copied']['assessments'] >= 1
    assert 'leases' not in report['copied']                       # transient lock table is not mirrored
    # Every mirrored chunk carries a vector value (the embedding survived the copy).
    assert all(row.get('embedding') for row in fake.store['chunks'])

    result = pgvector.validate(config, db, fake)
    assert result['ok'] is True
    assert result['scores']['match'] and result['scores']['mismatches'] == []
    assert all(t['match'] for t in result['tables'])
    # SQLite is never written during migration/validation: the score is exactly as before.
    assert db.one('SELECT score FROM assessments LIMIT 1')['score'] == score_before


def test_migrate_refuses_to_clobber_and_reset_reloads(system):
    config, db = system[0], system[1]
    _populate(system)
    fake = FakePg()
    pgvector.migrate(config, db, fake)
    loaded = len(fake.store['assessments'])
    # A second load without reset must refuse rather than double up rows or overwrite scores.
    with pytest.raises(ValueError):
        pgvector.migrate(config, db, fake)
    # reset drops and reloads cleanly: counts are identical, not doubled.
    again = pgvector.migrate(config, db, fake, reset=True)
    assert len(fake.store['assessments']) == loaded == again['copied']['assessments']


def test_validate_detects_score_drift(system):
    config, db = system[0], system[1]
    _populate(system)
    fake = FakePg()
    pgvector.migrate(config, db, fake)
    # Simulate any corruption of a score in the mirror: validation must fail and name it.
    fake.store['assessments'][0]['score'] = fake.store['assessments'][0]['score'] + 1
    result = pgvector.validate(config, db, fake)
    assert result['ok'] is False
    assert result['scores']['match'] is False and result['scores']['mismatches']


def test_rollback_drops_mirror_without_touching_sqlite(system):
    config, db = system[0], system[1]
    _populate(system)
    fake = FakePg()
    pgvector.migrate(config, db, fake)
    assert fake.store.get('assessments')
    pgvector.rollback(config, fake)
    assert fake.store == {}                                       # mirror gone
    # SQLite is untouched and can be re-migrated from scratch.
    assert db.one('SELECT COUNT(*) AS n FROM assessments')['n'] >= 1
    pgvector.migrate(config, db, fake)
    assert fake.store.get('assessments')


def test_search_filters_by_role_active_version_model_and_applicant(system):
    config, db, model = system[0], system[1], system[3]
    role_id, identity = _populate(system)
    fake = FakePg()
    pgvector.migrate(config, db, fake)
    app_id = db.one("SELECT id FROM applications WHERE filename='a.txt'")['id']
    query = model.embed('python sql')

    matches = pgvector.search(config, fake, role_id, query, identity)
    assert matches and all(m['application_id'] == app_id for m in matches)
    assert set(matches[0]) == {'id', 'application_id', 'section_id', 'location', 'text'}  # passages, no score

    # The filters are pushed into SQL, not applied after the fact.
    search_sql = next(sql for sql, _ in fake.executed if 'order by' in sql.lower() and '.chunks' in sql.lower())
    for clause in ('c.role_id=%s', 'c.version=a.version', 'a.active=1', 'a.duplicate_of is null',
                   'c.model=%s', 'c.embedding <=> %s::vector'):
        assert clause in search_sql.lower()

    assert pgvector.search(config, fake, role_id, query, 'voyage:voyage-3') == []      # model identity mismatch
    assert pgvector.search(config, fake, 'role-2', query, identity) == []              # other role
    assert pgvector.search(config, fake, role_id, query, identity, application_ids=[app_id])
    assert pgvector.search(config, fake, role_id, query, identity, application_ids=[app_id + 999]) == []


def test_answer_uses_pgvector_when_enabled_and_falls_back_on_error(system, monkeypatch):
    config, db, model = system[0], system[1], system[3]
    role_id, _ = _populate(system)
    configured = replace(config, pg_url=DSN)
    fake = FakePg()
    pgvector.migrate(configured, db, fake)
    db.set('pg_search_enabled', True)

    # A purely semantic question (no lexical/rank/list/status hits) routes through retrieval.
    question = 'Which contributor has cloud infrastructure exposure?'
    monkeypatch.setattr(pgvector, 'connect', lambda cfg: fake)
    enabled = applicant_search.answer(configured, db, model, role_id, question)
    assert 'semantic' in enabled['mode'] and enabled['candidates']
    assert any('order by' in sql.lower() and '.chunks' in sql.lower() for sql, _ in fake.executed)

    # If Postgres errors, the live SQLite cosine path still answers and the error is recorded.
    def boom(cfg):
        raise RuntimeError('postgres down')
    monkeypatch.setattr(pgvector, 'connect', boom)
    fallback = applicant_search.answer(configured, db, model, role_id, question)
    assert 'semantic' in fallback['mode'] and fallback['candidates']
    assert db.setting('pg_search_error')
