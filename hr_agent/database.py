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
