"""PostgreSQL + pgvector mirror (#2): ready-to-activate, inert until a DSN is configured.

SQLite stays the LIVE, AUTHORITATIVE store. This module builds a *parallel* copy of the
relational data in Postgres, with the chunk embeddings held in a real ``vector`` column so
similarity search can run in the database instead of in Python. Nothing here writes to
SQLite, and nothing reads from Postgres on the live path until an operator has run the
migration, passed validation, and thereby set the ``pg_search_enabled`` flag.

Guarantees:
  * Repeatable: the mirror lives in a dedicated schema; ``rollback`` drops it wholesale so a
    re-migration always starts clean. ``migrate`` refuses to load a target that already holds
    assessments unless explicitly reset, so a copy is never doubled up.
  * Never overwrites scores: migration only READS SQLite; ``validate`` proves that every
    (application, version, rubric) score in Postgres equals SQLite exactly.
  * Filtered retrieval: ``search`` scopes by role, applicant, active flag, current version and
    embedding-model identity — and returns passage matches only. Rubric scores stay in the
    relational ``ranked`` path; similarity and scoring are never conflated here.

``psycopg`` is imported lazily inside ``connect`` so importing this module never requires
Postgres to be installed. Tests inject a functional in-memory double in place of a live
connection; a real pgvector instance must still be verified before production use.
"""
import json
import math
import re
import time

# Transient/lock tables are runtime state, not data to preserve.
SKIP_TABLES = {'leases'}
VECTOR_COLUMNS = {('chunks', 'embedding')}
# Column list returned by ``search`` (kept in sync with the SELECT below).
_MATCH_COLUMNS = ('id', 'application_id', 'section_id', 'location', 'text')


def pg_configured(config):
    """True when a Postgres DSN is present. Empty DSN => the whole path stays inert."""
    return bool(config.pg_url)


def enabled(config, db):
    """True only when Postgres is configured AND a passing validation flipped the flag."""
    return pg_configured(config) and db.setting('pg_search_enabled', False) is True


def connect(config):
    """Open a live Postgres connection. Imported lazily; never logs the DSN."""
    if not pg_configured(config):
        raise ValueError('Set HR_POSTGRES_URL (or POSTGRES_URL.txt) before using Postgres')
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError('psycopg is required for the Postgres mirror; install psycopg[binary]') from exc
    return psycopg.connect(config.pg_url)


# --- schema planning -------------------------------------------------------

def _pg_type(table, column, sqlite_type):
    if (table, column) in VECTOR_COLUMNS:
        return 'vector'
    declared = (sqlite_type or '').upper()
    if 'INT' in declared:
        return 'bigint'
    if any(token in declared for token in ('REAL', 'FLOA', 'DOUB')):
        return 'double precision'
    return 'text'


def plan(db):
    """Introspect the live SQLite schema so the mirror tracks it without hand-maintenance.

    Returns one entry per migrated table in creation (dependency-safe) order.
    """
    tables = [row['name'] for row in db.rows(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    entries = []
    for table in tables:
        if table in SKIP_TABLES:
            continue
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', table):
            raise ValueError(f'Unexpected table name: {table!r}')
        info = db.rows(f'PRAGMA table_info("{table}")')
        columns = [col['name'] for col in info]
        pk = [col['name'] for col in sorted((c for c in info if c['pk']), key=lambda c: c['pk'])]
        types = {col['name']: _pg_type(table, col['name'], col['type']) for col in info}
        entries.append({'table': table, 'columns': columns, 'pk': pk, 'types': types})
    return entries


def _create_table_sql(schema, entry):
    cols = [f'{name} {entry["types"][name]}' for name in entry['columns']]
    if entry['pk']:
        cols.append(f'PRIMARY KEY ({",".join(entry["pk"])})')
    return f'CREATE TABLE IF NOT EXISTS {schema}.{entry["table"]} ({", ".join(cols)})'


def _insert_sql(schema, entry):
    placeholders = ['%s::vector' if (entry['table'], name) in VECTOR_COLUMNS else '%s'
                    for name in entry['columns']]
    return (f'INSERT INTO {schema}.{entry["table"]} ({",".join(entry["columns"])}) '
            f'VALUES ({",".join(placeholders)})')


# --- migration / validation / rollback -------------------------------------

def create_schema(config, db, conn):
    """Idempotently create the mirror schema, the vector extension and every table."""
    schema = config.pg_schema
    with conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS vector')
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS {schema}')
        for entry in plan(db):
            cur.execute(_create_table_sql(schema, entry))
    conn.commit()


def _count(cur, schema, table):
    cur.execute(f'SELECT count(*) FROM {schema}.{table}')
    return cur.fetchone()[0]


def migrate(config, db, conn, reset=False):
    """Copy all relational data SQLite -> Postgres. Read-only on SQLite; transactional on PG.

    Refuses to load a target that already holds assessments unless ``reset`` is set (which
    drops and recreates the schema first), so scores are never duplicated or clobbered.
    """
    schema = config.pg_schema
    if reset:
        rollback(config, conn)
    create_schema(config, db, conn)
    with conn.cursor() as cur:
        if not reset and _count(cur, schema, 'assessments') > 0:
            raise ValueError('Postgres mirror already populated; run pg-rollback before re-migrating')
    copied = {}
    with conn.cursor() as cur:
        for entry in plan(db):
            table, columns = entry['table'], entry['columns']
            statement = _insert_sql(schema, entry)
            rows = db.rows(f'SELECT {",".join(columns)} FROM {table}')
            for row in rows:
                cur.execute(statement, [row[name] for name in columns])
            copied[table] = len(rows)
    conn.commit()
    return {'schema': schema, 'copied': copied, 'tables': len(copied),
            'rows': sum(copied.values()), 'at': time.time()}


def _score_rows_sqlite(db):
    return sorted((r['application_id'], r['version'], r['rubric_id'], r['score'])
                  for r in db.rows('SELECT application_id,version,rubric_id,score FROM assessments'))


def _score_rows_pg(cur, schema):
    cur.execute(f'SELECT application_id,version,rubric_id,score FROM {schema}.assessments')
    return sorted((r[0], r[1], r[2], r[3]) for r in cur.fetchall())


def validate(config, db, conn):
    """Compare the mirror against SQLite: per-table row counts and exact score equality.

    Read-only on both stores. Returns a structured report; ``ok`` gates enabling live reads.
    """
    schema = config.pg_schema
    tables, ok = [], True
    with conn.cursor() as cur:
        for entry in plan(db):
            table = entry['table']
            source = db.one(f'SELECT count(*) AS n FROM {table}')['n']
            target = _count(cur, schema, table)
            match = source == target
            ok = ok and match
            tables.append({'table': table, 'sqlite': source, 'postgres': target, 'match': match})
        sqlite_scores = _score_rows_sqlite(db)
        pg_scores = _score_rows_pg(cur, schema)
    scores_match = sqlite_scores == pg_scores
    ok = ok and scores_match
    mismatches = [list(row) for row in sqlite_scores if row not in set(pg_scores)][:20]
    return {'ok': ok, 'schema': schema, 'tables': tables,
            'scores': {'sqlite_rows': len(sqlite_scores), 'postgres_rows': len(pg_scores),
                       'match': scores_match, 'mismatches': mismatches},
            'at': time.time()}


def rollback(config, conn):
    """Drop the mirror schema. SQLite is untouched; a re-migration starts clean."""
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS {config.pg_schema} CASCADE')
    conn.commit()


# --- retrieval -------------------------------------------------------------

def _vector_literal(vector):
    return '[' + ','.join(str(float(x)) for x in vector) + ']'


def search(config, conn, role_id, query_vector, embedding_identity, application_ids=None, k=8):
    """Role/applicant/active/version/model-scoped nearest-passage search.

    Returns passage matches (no scores): similarity ranking only. Filtering mirrors the SQLite
    retrieval path exactly, pushed into the database via the pgvector cosine-distance operator.
    """
    schema = config.pg_schema
    sql = (f'SELECT c.id,c.application_id,c.section_id,c.location,c.text '
           f'FROM {schema}.chunks c JOIN {schema}.applications a ON a.id=c.application_id '
           f'WHERE c.role_id=%s AND c.version=a.version AND a.active=1 '
           f'AND a.duplicate_of IS NULL AND c.model=%s')
    params = [role_id, embedding_identity]
    if application_ids is not None:
        sql += ' AND c.application_id = ANY(%s)'
        params.append(list(application_ids))
    sql += ' ORDER BY c.embedding <=> %s::vector LIMIT %s'
    params += [_vector_literal(query_vector), int(k)]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(zip(_MATCH_COLUMNS, row)) for row in cur.fetchall()]


def semantic_search(config, db, role_id, query_vector, embedding_identity, application_ids=None, k=8):
    """Live-path wrapper: open a connection, run the filtered search, always close it."""
    conn = connect(config)
    try:
        return search(config, conn, role_id, query_vector, embedding_identity, application_ids, k)
    finally:
        conn.close()


# --- CLI conveniences (open a real connection, run one operation) ----------

def provision(config, db):
    if not pg_configured(config):
        raise ValueError('Set HR_POSTGRES_URL (or POSTGRES_URL.txt) before running Postgres commands')
    conn = connect(config)
    try:
        return migrate(config, db, conn)
    finally:
        conn.close()


def verify(config, db):
    if not pg_configured(config):
        raise ValueError('Set HR_POSTGRES_URL (or POSTGRES_URL.txt) before running Postgres commands')
    conn = connect(config)
    try:
        return validate(config, db, conn)
    finally:
        conn.close()


def teardown(config, db):
    if not pg_configured(config):
        raise ValueError('Set HR_POSTGRES_URL (or POSTGRES_URL.txt) before running Postgres commands')
    conn = connect(config)
    try:
        rollback(config, conn)
    finally:
        conn.close()
