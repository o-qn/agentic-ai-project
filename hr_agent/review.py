import json
import time
from .database import audit,dirty

ACTIONS = {'confirm_spam','dismiss','restore','retry'}

def decide(db,app_id,action,reason,actor,expiry_days=90):
    if action not in ACTIONS:
        raise ValueError('Choose a valid review action')
    if not isinstance(actor,str) or not actor.strip():
        raise ValueError('Enter your name in the review form')
    if not isinstance(reason,str) or not reason.strip():
        raise ValueError('Enter a reason for this decision')
    actor,reason=actor.strip(),reason.strip()
    if isinstance(expiry_days,bool) or not isinstance(expiry_days,int) or not 1<=expiry_days<=365:
        raise ValueError('Blacklist review/expiry must be within 1–365 days')
    with db.tx() as conn:
        app = conn.execute('SELECT * FROM applications WHERE id=? AND active=1',(app_id,)).fetchone()
        if not app:
            raise ValueError('Unknown current application')
        role = conn.execute('SELECT * FROM roles WHERE id=?',(app['role_id'],)).fetchone()
        if action=='confirm_spam':
            if not app['hash'] or not app['review_evidence'] or not json.loads(app['review_evidence']):
                raise ValueError('Confirming spam requires a downloaded document and cited source evidence')
            conn.execute("INSERT INTO blacklist(kind,identifier,reason,evidence,actor,created,expires) VALUES('document',?,?,?,?,?,?)",
                         (app['hash'],reason,app['review_evidence'],actor,time.time(),time.time()+expiry_days*86400))
            conn.execute("UPDATE jobs SET generation=generation+1,state='queued',step='report' WHERE application_id=? AND state!='superseded'",(app_id,))
            conn.execute("UPDATE applications SET status='review',review_reason=? WHERE id=?",('HR-confirmed spam: '+reason,app_id))
        else:
            if action=='restore':
                conn.execute("UPDATE blacklist SET active=0 WHERE kind='document' AND identifier=?",(app['hash'],))
            # Every non-spam decision must leave the document actionable. In
            # particular, a review job is normally already `done` after its
            # move to Needs Review, so retrying only failed jobs is a no-op.
            conn.execute("UPDATE applications SET status='queued',review_dismissed=1,review_reason=NULL,duplicate_of=NULL WHERE id=?",(app_id,))
            conn.execute("UPDATE applications SET index_status='pending',index_attempts=0,index_next=0 WHERE id=?",(app_id,))
            # Preserve a completed assessment for dismiss/restore, but retry
            # still resumes at the current durable checkpoint when possible.
            existing = conn.execute('SELECT id FROM assessments WHERE application_id=? AND version=? AND rubric_id=?',
                                    (app_id,app['version'],role['rubric_id'])).fetchone()
            step = 'report' if existing else ('assess' if app['sections'] else 'download')
            if existing:
                conn.execute("UPDATE applications SET status='completed' WHERE id=?",(app_id,))
            conn.execute('''INSERT INTO jobs(application_id,version,rubric_id,step,updated) VALUES(?,?,?,?,?)
             ON CONFLICT(application_id,version,rubric_id) DO UPDATE SET state='queued',step=excluded.step,
             attempts=0,next_try=0,error=NULL,error_at=NULL,agent_state='{}',generation=jobs.generation+1,updated=excluded.updated''',
                         (app_id,app['version'],role['rubric_id'] or 0,step,time.time()))
        revision = dirty(conn,app['role_id'])
        conn.execute('UPDATE applications SET required_revision=? WHERE id=?',(revision,app_id))
        conn.execute('INSERT INTO review_decisions(application_id,action,reason,actor,created) VALUES(?,?,?,?,?)',
                     (app_id,action,reason,actor,time.time()))
        audit(conn,'hr_review_decision',app_id,{'action':action,'actor':actor,'reason':reason})

def restore_entry(db,entry_id,actor):
    if not isinstance(actor,str) or not actor.strip():
        raise ValueError('HR actor required')
    with db.tx() as conn:
        entry = conn.execute('SELECT * FROM blacklist WHERE id=?',(entry_id,)).fetchone()
        if not entry:
            raise ValueError('Unknown blacklist entry')
        conn.execute('UPDATE blacklist SET active=0 WHERE id=?',(entry_id,))
        audit(conn,'blacklist_restored',entry_id,{'actor':actor})
