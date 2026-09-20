from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time
import uuid
import threading
import fcntl

class DB:
    def __init__(self, data):
        self.path = Path(data) / 'hr.sqlite3'
        with (Path(data)/'schema.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            conn=self.connect()
            try:
                conn.execute('PRAGMA journal_mode=WAL')
                exists=conn.execute("SELECT name FROM sqlite_master WHERE name='schema_version'").fetchone()
                version=conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] if exists else 0
                for migration in sorted((Path(__file__).parent/'migrations').glob('*.sql')):
                    number=int(migration.name.split('_')[0])
                    if number>version:
                        conn.executescript('BEGIN IMMEDIATE;\n'+migration.read_text()+'\nCOMMIT;')
            finally:
                conn.close()
        self.path.chmod(0o600)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=30000')
        return conn

    @contextmanager
    def tx(self):
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def rows(self, sql, args=()):
        conn = self.connect()
        try:
            return [dict(r) for r in conn.execute(sql,args)]
        finally:
            conn.close()

    def one(self, sql, args=()):
        rows = self.rows(sql,args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.tx() as conn:
            return conn.execute(sql,args).lastrowid

    def setting(self,key,default=None):
        row = self.one('SELECT value FROM settings WHERE key=?',(key,))
        return json.loads(row['value']) if row else default

    def set(self,key,value):
        self.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,json.dumps(value)))

    def reset_terminal_jobs(self):
        """Remove terminal processing data from SQLite without touching source files or Drive."""
        with self.tx() as conn:
            ids = [row[0] for row in conn.execute("""
                SELECT DISTINCT a.id
                FROM applications a JOIN jobs j ON j.application_id=a.id
                WHERE j.state IN ('done','failed')
                  AND NOT EXISTS (
                    SELECT 1 FROM jobs pending
                    WHERE pending.application_id=a.id
                      AND pending.state NOT IN ('done','failed','superseded')
                  )""")]
            if not ids:
                return {'applications': 0, 'jobs': 0}
            marks = ','.join('?' for _ in ids)
            job_count = conn.execute(f'SELECT COUNT(*) FROM jobs WHERE application_id IN ({marks})', ids).fetchone()[0]
            # A live duplicate may still point at a terminal original. Break that
            # reference before deleting the original so the foreign key remains
            # valid and let its queued job continue as the canonical application.
            conn.execute(f'''UPDATE applications
                SET duplicate_of=NULL,
                    status=CASE WHEN status='duplicate' THEN 'queued' ELSE status END
                WHERE duplicate_of IN ({marks})''', ids)
            assessment_ids = [row[0] for row in conn.execute(
                f'SELECT id FROM assessments WHERE application_id IN ({marks})', ids)]
            if assessment_ids:
                assessment_marks = ','.join('?' for _ in assessment_ids)
                conn.execute(f'DELETE FROM criterion_evidence WHERE assessment_id IN ({assessment_marks})', assessment_ids)
                conn.execute(f'DELETE FROM assessments WHERE id IN ({assessment_marks})', assessment_ids)
            for table in ('chunks','source_versions','review_decisions','jobs'):
                conn.execute(f'DELETE FROM {table} WHERE application_id IN ({marks})', ids)
            conn.execute(f'UPDATE applications SET duplicate_of=NULL WHERE id IN ({marks})', ids)
            conn.execute(f'DELETE FROM applications WHERE id IN ({marks})', ids)
            audit(conn,'terminal_jobs_reset','database',{'applications':len(ids),'jobs':job_count})
            return {'applications': len(ids), 'jobs': job_count}

    @contextmanager
    def lease(self, name, ttl=60):
        owner = uuid.uuid4().hex
        with self.tx() as conn:
            conn.execute('DELETE FROM leases WHERE expires<?',(time.time(),))
            cur = conn.execute('INSERT OR IGNORE INTO leases VALUES(?,?,?)',(name,owner,time.time()+ttl))
            acquired = cur.rowcount == 1
        if not acquired:
            yield False
            return
        stop = threading.Event()
        def renew():
            while not stop.wait(ttl/3):
                self.execute('UPDATE leases SET expires=? WHERE name=? AND owner=?',(time.time()+ttl,name,owner))
        thread = threading.Thread(target=renew,daemon=True)
        thread.start()
        try:
            yield True
        finally:
            stop.set()
            thread.join(timeout=5)
            self.execute('DELETE FROM leases WHERE name=? AND owner=?',(name,owner))

def audit(conn,event,entity,detail):
    conn.execute('INSERT INTO audit(event,entity,detail,created) VALUES(?,?,?,?)',
                 (event,str(entity),json.dumps(detail),time.time()))

def dirty(conn,role_id):
    conn.execute('UPDATE roles SET revision=revision+1 WHERE id=?',(role_id,))
    return conn.execute('SELECT revision FROM roles WHERE id=?',(role_id,)).fetchone()[0]
