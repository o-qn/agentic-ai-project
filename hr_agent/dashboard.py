import csv
import io
import json
import os
import secrets
import time
from pathlib import Path
from flask import Flask,abort,jsonify,render_template,request,send_file,session
from .config import Config
from .database import DB
from .model_client import create_model
from .scoring import ranked
from . import applicant_search,grounded_answer,rubric,review,reports

def create_app(config=None,db=None,ollama=None,drive=None):
    config = config or Config.env()
    db = db or DB(config.data)
    if ollama is None:
        if db.setting('demo',False):
            from .demo import SyntheticModel
            ollama = SyntheticModel()
        else:
            ollama = create_model(config)

    def get_drive():
        # Built lazily and only when a mutating route needs Drive, so the read-only
        # dashboard still serves without Google credentials (and tests inject a double).
        nonlocal drive
        if drive is None:
            from .drive_connector import Drive
            drive = Drive(config)
        return drive
    app = Flask(__name__)
    secret_file = config.data/'session.key'
    try:
        fd = os.open(secret_file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as out:
            out.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    app.secret_key = secret_file.read_text()
    app.config.update(MAX_CONTENT_LENGTH=100_000,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',
                      TRUSTED_HOSTS=['localhost','127.0.0.1','[::1]'])

    @app.before_request
    def local_boundary():
        if request.remote_addr not in {'127.0.0.1','::1',None}:
            abort(403,'Local access only')
        if request.method not in {'GET','HEAD','OPTIONS'}:
            if not session.get('csrf') or not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),session['csrf']):
                abort(403,'Invalid CSRF token')
            origin = request.headers.get('Origin')
            if origin and origin!=request.host_url.rstrip('/'):
                abort(403,'Cross-origin mutation denied')
        # CV uploads carry a real file; lift the tight JSON body cap for that route only,
        # to the configured file-size limit plus a small margin for the multipart envelope.
        if request.endpoint=='upload_cv':
            request.max_content_length = config.max_file_mb*1024*1024 + 8192

    @app.after_request
    def headers(response):
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Cache-Control']='no-store'
        return response

    @app.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(error=str(exc)[:600]),400

    @app.errorhandler(Exception)
    def error(exc):
        from werkzeug.exceptions import HTTPException
        if isinstance(exc,HTTPException):
            return jsonify(error=exc.description),exc.code
        return jsonify(error=type(exc).__name__+'; inspect processing status or service logs'),500

    @app.get('/')
    def home():
        session.setdefault('csrf',secrets.token_urlsafe(32))
        return render_template('dashboard.html',csrf=session['csrf'])

    @app.get('/api/status')
    def status():
        active=db.setting('active_job')
        if active:
            job=db.one('SELECT agent_state FROM jobs WHERE id=?',(active['job_id'],))
            source=db.one('SELECT filename FROM applications WHERE id=?',(active['application_id'],))
            state=json.loads(job['agent_state']) if job else {}
            active={**active,'filename':source['filename'] if source else None,
                    'turns':state.get('turns',0),'sections_read':len(state.get('seen',[]))}
        return jsonify(roles=db.rows('SELECT * FROM roles WHERE active=1 ORDER BY name'),
          automatic=db.setting('automatic',False),poc_mode=db.setting('poc_mode',False),last_scan=db.setting('last_successful_scan'),
          next_scan=db.setting('next_scan'),scan_error=db.setting('scan_error'),active_job=active,
          active_index=db.setting('active_index'),
          service_heartbeat=db.setting('service_heartbeat'),worker_heartbeat=db.setting('worker_heartbeat'),
          missed_scan=bool(db.setting('last_successful_scan') and time.time()-db.setting('last_successful_scan')>config.scan_seconds*2),
          model=config.model,model_provider=config.provider,model_ready=ollama.is_validated(db),
          model_turn_limit=config.router_max_turns if config.provider=='agentrouter' else 12,
          hosted_requests_remaining=ollama.budget_remaining() if config.provider=='agentrouter' else None,
          drive_authorized=(config.data/'token.json').exists(),demo=db.setting('demo',False),
          jobs=db.rows('SELECT state,COUNT(*) AS count FROM jobs WHERE state!=\'superseded\' GROUP BY state'))

    @app.get('/api/usage')
    def usage_report():
        # Read-only. Embedding usage (from its own ledger) is reported separately
        # from assessment usage; no dollar costs are synthesised.
        from . import usage
        return jsonify(usage.report(config,db,ollama))

    @app.post('/api/check')
    def check():
        db.set('check_now',True)
        return jsonify(queued=True)

    @app.post('/api/automatic')
    def automatic():
        enabled = request.json.get('enabled')
        if not isinstance(enabled,bool):
            raise ValueError('enabled must be true or false')
        db.set('automatic',enabled)
        return jsonify(automatic=enabled)

    @app.get('/api/roles/<role_id>')
    def role_detail(role_id):
        role = db.one('SELECT * FROM roles WHERE id=? AND active=1',(role_id,))
        if not role:
            abort(404)
        rows = db.rows('SELECT * FROM applications WHERE role_id=? AND active=1 ORDER BY id',(role_id,))
        ranking = ranked(db,role_id)
        for row in rows+ranking:
            row['contact'] = json.loads(row['contact'])
            row.pop('source_path',None)
            row.pop('sections',None)
        selected = db.one('SELECT * FROM rubrics WHERE id=?',(role['rubric_id'],))
        return jsonify(report_retry=db.setting('report_retry:'+role_id),role=role,applications=rows,ranked=ranking,top=ranking[:3],rubric=selected,
                       jobs=db.rows('''SELECT j.* FROM jobs j JOIN applications a ON a.id=j.application_id
                         WHERE a.role_id=? AND j.state!='superseded' ORDER BY j.id DESC''',(role_id,)))

    @app.get('/api/applications/<int:app_id>')
    def application(app_id):
        row = db.one('SELECT * FROM applications WHERE id=? AND active=1',(app_id,))
        if not row:
            abort(404)
        row.pop('source_path',None)
        row['assessments'] = db.rows('SELECT * FROM assessments WHERE application_id=? ORDER BY id DESC',(app_id,))
        row['actions'] = db.rows('SELECT * FROM audit WHERE entity=? ORDER BY id DESC LIMIT 100',(str(app_id),))
        return jsonify(row)

    @app.post('/api/roles/<role_id>/draft')
    def draft(role_id):
        return jsonify(rubric.draft(db,role_id,ollama))

    @app.post('/api/roles/<role_id>/approve')
    def approve(role_id):
        body = request.json
        if body.get('confirm_job_relevance') is not True:
            raise ValueError('HR must confirm criteria are job-relevant and exclude protected attributes')
        result = rubric.approve(db,role_id,body['rubric'],body['actor'],body['jd_hash'],body['plan'])
        return jsonify(rubric_id=result)

    @app.post('/api/review/<int:app_id>')
    def decision(app_id):
        body = request.json
        review.decide(db,app_id,body['action'],body['reason'],body['actor'],int(body.get('expiry_days',90)))
        return jsonify(saved=True)

    @app.get('/api/blacklist')
    def blacklist():
        return jsonify(entries=db.rows('SELECT * FROM blacklist ORDER BY id DESC'))

    @app.post('/api/blacklist/<int:entry_id>/restore')
    def restore(entry_id):
        review.restore_entry(db,entry_id,request.json['actor'])
        return jsonify(restored=True)

    @app.post('/api/roles/<role_id>/chat')
    def chat(role_id):
        return jsonify(applicant_search.answer(config,db,ollama,role_id,request.json['question'],request.json.get('application_ids')))

    @app.post('/api/roles/<role_id>/answer')
    def grounded(role_id):
        # Evidence-grounded generated answer. Retrieval stays authoritative and is returned
        # separately; every generated claim is validated against a retrieved passage, and retrieved
        # CV text is treated as untrusted data (it can never move a score or invent an applicant).
        return jsonify(grounded_answer.grounded_answer(config,db,ollama,role_id,request.json['question'],request.json.get('application_ids')))

    @app.post('/api/roles/<role_id>/upload')
    def upload_cv(role_id):
        # Deposit an HR-supplied CV into the role's Incoming CVs folder and let the
        # normal scan discover it. Discovery is cheap; assessment still waits for Auto
        # process, so this never triggers paid inference or bulk processing on its own.
        role = db.one('SELECT * FROM roles WHERE id=? AND active=1',(role_id,))
        if not role:
            abort(404)
        upload = request.files.get('file')
        if not upload or not upload.filename:
            raise ValueError('Choose a CV file to upload')
        name = Path(upload.filename).name
        if Path(name).suffix.lower() not in {'.pdf','.docx','.txt'}:
            raise ValueError('Unsupported CV format; upload a PDF, DOCX or UTF-8 TXT file')
        limit = config.max_file_mb*1024*1024
        data = upload.read(limit+1)
        if len(data)>limit:
            raise ValueError('File exceeds the configured size limit')
        if not data:
            raise ValueError('The uploaded file is empty')
        folders = json.loads(role['folders'] or '{}')
        incoming = folders.get('Incoming CVs')
        if not incoming:
            raise ValueError('This role has no Incoming CVs folder yet; run a scan first')
        from .drive_connector import DriveError
        try:
            get_drive().upload_cv(incoming,name,data)
        except DriveError as exc:
            # Sign-in required, or a folder that is not HR-private: surface the reason.
            raise ValueError(str(exc))
        db.set('check_now',True)
        return jsonify(uploaded=True,filename=name)

    @app.get('/api/applications/<int:app_id>/source')
    def local_source(app_id):
        row=db.one('SELECT source_path,filename FROM applications WHERE id=? AND active=1',(app_id,))
        if not row or not row['source_path']:
            abort(404)
        from pathlib import Path
        path=Path(row['source_path']).resolve()
        if not path.is_relative_to(config.data/'sources'):
            abort(403)
        return send_file(path,as_attachment=True,download_name=row['filename'])

    @app.get('/api/roles/<role_id>/xlsx')
    def local_report(role_id):
        row=db.one("SELECT path FROM report_revisions WHERE role_id=? ORDER BY revision DESC LIMIT 1",(role_id,))
        if not row or not row['path']:
            abort(404)
        from pathlib import Path
        path=Path(row['path']).resolve()
        if not path.is_relative_to(config.data/'reports'):
            abort(403)
        return send_file(path,as_attachment=True,download_name='candidates.xlsx')

    @app.get('/api/roles/<role_id>/csv')
    def csv_download(role_id):
        if not db.one('SELECT id FROM roles WHERE id=?',(role_id,)):
            abort(404)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(reports.TABS['Ranked Candidates'])
        writer.writerows([[reports.safe(v) for v in row] for row in reports.report_rows(db,role_id)['Ranked Candidates']])
        return app.response_class(buffer.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=candidates.csv'})
    return app
