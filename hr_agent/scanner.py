import hashlib
import json
import time
from .database import audit,dirty
from .drive_connector import content_version,DriveError

FOLDER = 'application/vnd.google-apps.folder'
FOLDERS = ['Incoming CVs','Processed CVs','Needs Review','Reports']

class Scanner:
    def __init__(self,config,db,drive):
        self.config,self.db,self.drive = config,db,drive

    def folders(self,role,children):
        mapping = json.loads(role['folders'])
        for name in FOLDERS:
            found = [f for f in children if f['name']==name and f['mimeType']==FOLDER]
            if len(found)>1:
                raise ValueError(f'Duplicate {name} folders need HR resolution')
            if found:
                mapping[name] = found[0]['id']
            elif name not in mapping:
                mapping[name] = self.drive.reserve_id()
                # Save ID before any remote create, including uncertain success.
                self.db.execute('UPDATE roles SET folders=? WHERE id=?',(json.dumps(mapping),role['id']))
                self.drive.create_folder(role['id'],name,mapping[name])
            else:
                self.drive.create_folder(role['id'],name,mapping[name])
        self.db.execute('UPDATE roles SET folders=? WHERE id=?',(json.dumps(mapping),role['id']))
        return mapping

    def discover(self,role,file):
        version = content_version(file)
        with self.db.tx() as conn:
            app = conn.execute('SELECT * FROM applications WHERE role_id=? AND file_id=?',(role['id'],file['id'])).fetchone()
            if app and app['version']==version and app['active']:
                conn.execute('UPDATE applications SET filename=? WHERE id=?',(file['name'],app['id']))
                return
            now = time.time()
            if app:
                app_id = app['id']
                conn.execute('''UPDATE applications SET version=?,filename=?,hash=NULL,source_path=NULL,sections=NULL,
                contact='{}',active=1,duplicate_of=NULL,status='queued',review_reason=NULL,review_evidence=NULL,
                review_dismissed=0,index_status='pending',index_attempts=0,index_error=NULL,updated=? WHERE id=?''',
                             (version,file['name'],now,app_id))
                conn.execute('DELETE FROM chunks WHERE application_id=?',(app_id,))
                conn.execute("UPDATE jobs SET state='superseded' WHERE application_id=?",(app_id,))
                dirty(conn,role['id'])
            else:
                app_id = conn.execute('INSERT INTO applications(role_id,file_id,filename,version,created,updated) VALUES(?,?,?,?,?,?)',
                                      (role['id'],file['id'],file['name'],version,now,now)).lastrowid
            conn.execute('''INSERT INTO jobs(application_id,version,rubric_id,updated) VALUES(?,?,?,?)
              ON CONFLICT(application_id,version,rubric_id) DO UPDATE SET state='queued',step='download',
              attempts=0,next_try=0,agent_state='{}',content_hash='',generation=jobs.generation+1,updated=excluded.updated''',
                         (app_id,version,role['rubric_id'] or 0,now))
            audit(conn,'source_discovered',app_id,{'version':version})

    def run(self):
        with self.db.lease('scanner',60) as acquired:
            if not acquired:
                return False
            started = time.time()
            self.db.set('scan_started',started)
            try:
                remote_roles = [f for f in self.drive.list(self.config.root) if f['mimeType']==FOLDER]
                seen_roles = set()
                jd_errors = []
                for remote in remote_roles:
                    role_id = remote['id']
                    seen_roles.add(role_id)
                    self.db.execute('''INSERT INTO roles(id,name) VALUES(?,?) ON CONFLICT(id)
                     DO UPDATE SET name=excluded.name,active=1''',(role_id,remote['name']))
                    role = self.db.one('SELECT * FROM roles WHERE id=?',(role_id,))
                    children = list(self.drive.list(role_id))
                    mapping = self.folders(role,children)
                    try:
                        jds = [f for f in children if f['name'].lower()=='job_description.txt']
                        if len(jds)>1:
                            raise ValueError('Multiple job_description.txt files in a role')
                        jd = ''
                        if jds:
                            path = self.config.data/'sources'/('jd-'+hashlib.sha256(role_id.encode()).hexdigest()+'.txt')
                            self.drive.download(jds[0]['id'],path,content_version(jds[0]))
                            if path.stat().st_size>20000:
                                raise ValueError('Job description exceeds 20,000 bytes')
                            jd = path.read_text(encoding='utf-8-sig').strip()
                        jd_hash = hashlib.sha256(jd.encode()).hexdigest()
                        if role['jd_hash'] != jd_hash:
                            with self.db.tx() as conn:
                                conn.execute('UPDATE roles SET jd=?,jd_hash=?,paused=1 WHERE id=?',(jd,jd_hash,role_id))
                                dirty(conn,role_id)
                                audit(conn,'job_description_changed',role_id,{'jd_hash':jd_hash})
                        self.db.execute('UPDATE roles SET jd_error=NULL WHERE id=?',(role_id,))
                    except Exception as exc:
                        with self.db.tx() as conn:
                            conn.execute('UPDATE roles SET paused=1,jd_error=? WHERE id=?',(type(exc).__name__,role_id))
                            dirty(conn,role_id)
                            audit(conn,'job_description_unreadable',role_id,{'error_type':type(exc).__name__})
                        # Keep discovering CVs for this paused role and the other roles.
                        # Report the scan failure after all roles have had their turn.
                        jd_errors.append(exc)
                    role = self.db.one('SELECT * FROM roles WHERE id=?',(role_id,))
                    incoming = list(self.drive.list(mapping['Incoming CVs']))
                    if self.db.setting('poc_mode',False):
                        from .rubric import activate_poc
                        activate_poc(self.db,role_id)
                        role = self.db.one('SELECT * FROM roles WHERE id=?',(role_id,))
                    known = {a['file_id']:a for a in self.db.rows('SELECT * FROM applications WHERE role_id=? AND active=1',(role_id,))}
                    current_ids = set()
                    for file in children+incoming:
                        if file['mimeType']==FOLDER or file['name'].lower() in {'job_description.txt','candidates.xlsx','candidate_ranking.xlsx'}:
                            continue
                        if file['id']==role['report_id'] or file.get('appProperties',{}).get('hrRole'):
                            continue
                        current_ids.add(file['id'])
                        self.discover(role,file)
                    # Inspect only known files in archive/review; never discover those as new CVs.
                    for file_id,app in known.items():
                        if file_id in current_ids:
                            continue
                        try:
                            metadata = self.drive.get(file_id)
                        except DriveError as exc:
                            if 'HTTP 404' not in str(exc):
                                raise
                            metadata = {'trashed':True}
                        parents = set(metadata.get('parents',[]))
                        allowed = {role_id,*mapping.values()}
                        if metadata.get('trashed') or not parents.intersection(allowed):
                            with self.db.tx() as conn:
                                conn.execute("UPDATE applications SET active=0,status='removed' WHERE id=?",(app['id'],))
                                conn.execute('DELETE FROM chunks WHERE application_id=?',(app['id'],))
                                conn.execute("UPDATE jobs SET state='superseded' WHERE application_id=?",(app['id'],))
                                dirty(conn,role_id)
                                audit(conn,'source_removed',app['id'],{})
                        elif content_version(metadata)!=app['version']:
                            self.discover(role,metadata)
                for role in self.db.rows('SELECT id FROM roles WHERE active=1'):
                    if role['id'] not in seen_roles:
                        with self.db.tx() as conn:
                            conn.execute('UPDATE roles SET active=0,paused=1 WHERE id=?',(role['id'],))
                            conn.execute('DELETE FROM chunks WHERE role_id=?',(role['id'],))
                            audit(conn,'role_removed',role['id'],{})
                with self.db.tx() as conn:
                    orphans=conn.execute("""SELECT a.*,r.rubric_id FROM applications a JOIN roles r ON r.id=a.role_id
                      JOIN applications original ON original.id=a.duplicate_of WHERE a.active=1
                      AND (original.active=0 OR original.hash IS NULL OR original.hash!=a.hash)""").fetchall()
                    for app in orphans:
                        conn.execute("UPDATE applications SET duplicate_of=NULL,status='queued' WHERE id=?",(app['id'],))
                        conn.execute("UPDATE jobs SET state='queued',step=?,attempts=0,next_try=0,agent_state='{}',generation=generation+1 WHERE application_id=? AND version=? AND rubric_id=?",
                                     ('assess' if app['sections'] else 'extract',app['id'],app['version'],app['rubric_id'] or 0))
                        dirty(conn,app['role_id'])
                        audit(conn,'duplicate_promoted',app['id'],{})
                if jd_errors:
                    raise jd_errors[0]
                self.db.set('last_successful_scan',time.time())
                self.db.set('scan_error',None)
                self.db.set('next_scan',time.time()+self.config.scan_seconds)
                return True
            except Exception as exc:
                self.db.set('scan_error',{'type':type(exc).__name__,'message':str(exc)[:300],'at':time.time()})
                raise

    def setup(self):
        existing = {f['name'].strip().lower():f for f in self.drive.list(self.config.root) if f['mimeType']==FOLDER}
        if 'acountant' in existing:
            existing['accountant']=existing['acountant']
        for name in ['Software Engineer','AI Engineer','Accountant']:
            if name.lower() not in existing:
                key = 'setup_folder:'+name
                file_id = self.db.setting(key)
                if not file_id:
                    file_id = self.drive.reserve_id()
                    self.db.set(key,file_id)
                self.drive.create_folder(self.config.root,name,file_id)
        self.run()
