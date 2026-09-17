"""Durable processing state machine. One worker; no inference in the scanner."""
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time
from .database import audit,dirty
from .document_reader import extract,NeedsReview
from .drive_connector import SourceChanged
from .screening_agent import ScreeningAgent,PROMPT_VERSION
from .scoring import score,FACTORS
from . import reports

class Worker:
    def __init__(self,config,db,drive,ollama):
        self.config,self.db,self.drive,self.ollama = config,db,drive,ollama
        self.agent = ScreeningAgent(db,ollama,config)

    def current(self,conn,job):
        row = conn.execute('''SELECT a.*,r.rubric_id,r.paused,j.generation AS job_generation FROM applications a JOIN roles r ON r.id=a.role_id
            JOIN jobs j ON j.application_id=a.id WHERE j.id=? AND j.state!='superseded' AND a.active=1''',(job['id'],)).fetchone()
        if not row or row['version']!=job['version'] or row['job_generation']!=job['generation']:
            return None
        if job['step']=='assess' and (row['paused'] or row['rubric_id']!=job['rubric_id']):
            return None
        return row

    def checkpoint(self,job,step):
        self.db.execute("UPDATE jobs SET step=?,state='queued',attempts=0,next_try=0,updated=? WHERE id=? AND state!='superseded' AND generation=?",
                        (step,time.time(),job['id'],job['generation']))

    def review(self,job,reason):
        saved = self.db.one('SELECT agent_state FROM jobs WHERE id=?',(job['id'],))
        evidence = json.loads(saved['agent_state']).get('review',{}).get('evidence',[])
        with self.db.tx() as conn:
            app = self.current(conn,job)
            if not app:
                return
            revision = dirty(conn,app['role_id'])
            conn.execute("UPDATE applications SET status='review',review_reason=?,review_evidence=?,required_revision=?,updated=? WHERE id=?",
                         (reason,json.dumps(evidence),revision,time.time(),app['id']))
            conn.execute("UPDATE jobs SET step='report',state='queued',attempts=0,updated=? WHERE id=?",(time.time(),job['id']))
            audit(conn,'review_requested',app['id'],{'reason':reason})

    def process(self,job):
        with self.db.tx() as conn:
            current=self.current(conn,{**job,'step':'preflight'})
            if not current:
                return
        app = self.db.one('SELECT * FROM applications WHERE id=?',(job['application_id'],))
        role = self.db.one('SELECT * FROM roles WHERE id=?',(app['role_id'],))
        if job['step']=='download':
            suffix = Path(app['filename']).suffix.lower()
            if suffix not in {'.pdf','.docx','.txt'}:
                raise NeedsReview('Unsupported CV format; HR review required')
            path = self.config.data/'sources'/(str(app['id'])+'-'+hashlib.sha256(app['version'].encode()).hexdigest()[:20]+suffix)
            if not path.exists():
                self.drive.download(app['file_id'],path,app['version'])
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.db.tx() as conn:
                if not self.current(conn,job):
                    return
                other = conn.execute('''SELECT id FROM applications WHERE role_id=? AND hash=? AND id!=? AND active=1
                     AND duplicate_of IS NULL ORDER BY id LIMIT 1''',(app['role_id'],digest,app['id'])).fetchone()
                conn.execute('UPDATE applications SET hash=?,source_path=?,duplicate_of=? WHERE id=?',
                             (digest,str(path),other['id'] if other else None,app['id']))
                conn.execute('INSERT OR IGNORE INTO source_versions(application_id,version,hash,path,created) VALUES(?,?,?,?,?)',
                             (app['id'],app['version'],digest,str(path),time.time()))
                conn.execute('UPDATE jobs SET content_hash=? WHERE id=?',(digest,job['id']))
            block = self.db.one("SELECT id FROM blacklist WHERE kind='document' AND identifier=? AND active=1 AND expires>?",(digest,time.time()))
            if block:
                raise NeedsReview('Matches an HR-confirmed document blacklist entry; review required')
            if other:
                with self.db.tx() as conn:
                    if not self.current(conn,job):
                        return
                    revision = dirty(conn,app['role_id'])
                    conn.execute("UPDATE applications SET status='duplicate',required_revision=? WHERE id=?",(revision,app['id']))
                self.checkpoint(job,'report')
            else:
                self.checkpoint(job,'extract')
        elif job['step']=='extract':
            result = extract(app['source_path'],self.config)
            with self.db.tx() as conn:
                if not self.current(conn,job):
                    return
                conn.execute('UPDATE applications SET sections=?,contact=?,chunk_strategy=? WHERE id=?',
                             (json.dumps(result['sections']),json.dumps(result['contact']),result.get('strategy'),app['id']))
            self.checkpoint(job,'assess')
        elif job['step']=='assess':
            if role['paused'] or not role['rubric_id']:
                self.db.execute("UPDATE jobs SET state='waiting_rubric' WHERE id=? AND generation=?",(job['id'],job['generation']))
                return
            if not self.db.setting('poc_mode',False) and not self.ollama.is_validated(self.db):
                self.db.execute("UPDATE jobs SET state='waiting_model' WHERE id=? AND generation=?",(job['id'],job['generation']))
                return
            # Reassessments also deduplicate, and re-check active blacklist holds.
            other = self.db.one('''SELECT id FROM applications WHERE role_id=? AND hash=? AND id<? AND active=1
              AND duplicate_of IS NULL ORDER BY id LIMIT 1''',(app['role_id'],app['hash'],app['id']))
            if other:
                with self.db.tx() as conn:
                    if not self.current(conn,job):
                        return
                    revision=dirty(conn,app['role_id'])
                    conn.execute("UPDATE applications SET status='duplicate',duplicate_of=?,required_revision=? WHERE id=?",(other['id'],revision,app['id']))
                self.checkpoint(job,'report')
                return
            block = self.db.one("SELECT id FROM blacklist WHERE kind='document' AND identifier=? AND active=1 AND expires>?",(app['hash'],time.time()))
            if block:
                raise NeedsReview('Matches an HR-confirmed document blacklist entry; review required')
            rubric_row = self.db.one('SELECT body FROM rubrics WHERE id=?',(job['rubric_id'],))
            rubric = json.loads(rubric_row['body'])
            saved = self.db.one('SELECT * FROM assessments WHERE application_id=? AND version=? AND rubric_id=?',
                                (app['id'],app['version'],job['rubric_id']))
            if saved:
                with self.db.tx() as conn:
                    if not self.current(conn,job):
                        return
                    revision=dirty(conn,app['role_id'])
                    conn.execute("UPDATE applications SET status='completed',required_revision=?,review_reason=NULL WHERE id=?",(revision,app['id']))
                self.checkpoint(job,'report')
                return
            body = self.agent.run(job,app,rubric)
            points = score(body,rubric)
            with self.db.tx() as conn:
                if not self.current(conn,job):
                    return
                cur = conn.execute('''INSERT INTO assessments(application_id,version,rubric_id,body,score,model,prompt_version,created)
                VALUES(?,?,?,?,?,?,?,?)''',(app['id'],app['version'],job['rubric_id'],json.dumps(body),points,
                                           self.ollama.identity(self.config.model),PROMPT_VERSION,time.time()))
                criteria = {c['id']:c for c in rubric['criteria']}
                for finding in body['findings']:
                    weight = criteria[finding['criterion_id']]['weight']
                    conn.execute('INSERT INTO criterion_evidence(assessment_id,criterion_id,weight,points,body) VALUES(?,?,?,?,?)',
                                 (cur.lastrowid,finding['criterion_id'],weight,weight*FACTORS[finding['level']],json.dumps(finding)))
                revision = dirty(conn,app['role_id'])
                conn.execute("UPDATE applications SET status='completed',review_reason=NULL,required_revision=?,index_status='pending',updated=? WHERE id=?",
                             (revision,time.time(),app['id']))
                conn.execute("UPDATE jobs SET step='report',state='queued',attempts=0,updated=? WHERE id=?",(time.time(),job['id']))
                audit(conn,'assessment_committed',app['id'],{'score':points,'rubric_id':job['rubric_id']})
        elif job['step']=='report':
            reports.sync(self.config,self.db,self.drive,app['role_id'])
            self.checkpoint(job,'move')
        elif job['step']=='move':
            role = self.db.one('SELECT * FROM roles WHERE id=?',(app['role_id'],))
            if role['synced_revision']<app['required_revision']:
                self.checkpoint(job,'report')
                return
            if role['paused'] and app['status']=='completed':
                self.db.execute("UPDATE jobs SET state='waiting_rubric' WHERE id=? AND generation=?",(job['id'],job['generation']))
                return
            if self.drive.get(app['file_id']).get('trashed'):
                raise SourceChanged('Source was deleted before move')
            from .drive_connector import content_version
            if content_version(self.drive.get(app['file_id']))!=app['version']:
                raise SourceChanged('Source changed before move; next scan will refresh')
            folders = json.loads(role['folders'])
            self.drive.move(app['file_id'],folders['Needs Review' if app['status']=='review' else 'Processed CVs'])
            with self.db.tx() as conn:
                if not self.current(conn,job):
                    return
                conn.execute("UPDATE jobs SET step='done',state='done',updated=? WHERE id=?",(time.time(),job['id']))
                audit(conn,'source_moved',app['id'],{'report_revision':app['required_revision']})

    def tick(self):
        with (self.config.data/'worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            # The OS lock proves no other worker is alive; reclaim interrupted running steps.
            self.db.execute("UPDATE jobs SET state='queued' WHERE state='running'")
            job = self.db.one('''SELECT j.* FROM jobs j JOIN applications a ON a.id=j.application_id
              JOIN roles r ON r.id=a.role_id WHERE j.state IN ('queued','retry') AND j.next_try<=?
              AND a.active=1 AND r.active=1 ORDER BY j.next_try,j.id LIMIT 1''',(time.time(),))
            if not job:
                return False
            with self.db.tx() as conn:
                claimed=conn.execute("UPDATE jobs SET state='running',started=COALESCE(started,?),updated=? WHERE id=? AND generation=? AND state IN ('queued','retry')",
                                     (time.time(),time.time(),job['id'],job['generation'])).rowcount
                if not claimed:
                    return True
            self.db.set('active_job',{'job_id':job['id'],'application_id':job['application_id'],'step':job['step'],'started':time.time()})
            try:
                self.process(job)
            except NeedsReview as exc:
                self.review(job,str(exc))
            except SourceChanged:
                self.db.execute("UPDATE jobs SET state='superseded',updated=? WHERE id=? AND generation=?",(time.time(),job['id'],job['generation']))
                self.db.set('check_now',True)
            except Exception as exc:
                attempts = job['attempts']+1
                self.db.execute("""UPDATE jobs SET state=?,attempts=?,next_try=?,error=?,error_at=?,updated=?
                  WHERE id=? AND state!='superseded' AND generation=?""",('failed' if attempts>=5 else 'retry',attempts,
                  time.time()+min(3600,15*2**attempts),type(exc).__name__,time.time(),time.time(),job['id'],job['generation']))
                with self.db.tx() as conn:
                    audit(conn,'job_error',job['application_id'],{'step':job['step'],'error_type':type(exc).__name__,'attempt':attempts})
            finally:
                self.db.set('active_job',None)
            return True
