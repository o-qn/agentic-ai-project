"""Direct CV upload (dashboard -> role's Incoming CVs -> ordinary discovery).

The upload only deposits a file and asks for a scan; it must not enable Auto
process or assess anything by itself. Assessment here is driven explicitly by
drain(worker), proving the deposited CV flows through the normal pipeline.
"""
import io
import json
from hr_agent.dashboard import create_app
from hr_agent.demo import drain


def client_with_csrf(config, db, model, drive):
    web = create_app(config, db, model, drive)
    client = web.test_client()
    assert client.get('/').status_code == 200  # seeds session['csrf']
    with client.session_transaction() as session:
        csrf = session['csrf']
    return client, csrf


def upload(client, csrf, role_id, filename, data):
    return client.post(f'/api/roles/{role_id}/upload',
                       data={'file': (io.BytesIO(data), filename)},
                       content_type='multipart/form-data',
                       headers={'X-CSRF-Token': csrf})


def test_direct_cv_upload_routes_through_discovery(system):
    config, db, drive, model, scanner, worker = system
    role = db.one('SELECT * FROM roles WHERE active=1 ORDER BY id')
    incoming = json.loads(role['folders'])['Incoming CVs']
    client, csrf = client_with_csrf(config, db, model, drive)

    cv = b'Name: Uploaded Person\nBuilt a Python application.\nBuilt a SQL database.\n'
    response = upload(client, csrf, role['id'], 'uploaded.txt', cv)
    assert response.status_code == 200
    assert response.get_json() == {'uploaded': True, 'filename': 'uploaded.txt'}

    # Landed in Incoming CVs, marked HR provenance, and discoverable (not a report).
    listed = [f for f in drive.list(incoming) if f['name'] == 'uploaded.txt']
    assert len(listed) == 1
    assert listed[0]['appProperties'].get('hrUpload') == '1'
    assert not listed[0]['appProperties'].get('hrRole')
    assert drive.cv_uploads == [(listed[0]['id'], 'uploaded.txt')]

    # Deposit asks for a scan but never flips Auto process on by itself.
    assert db.setting('check_now') is True
    assert db.setting('automatic', False) is False

    # The normal scan + worker pipeline turns it into a real, scored assessment.
    scanner.run()
    drain(worker)
    app_row = db.one("SELECT * FROM applications WHERE filename='uploaded.txt' AND active=1")
    assert app_row is not None and app_row['status'] == 'completed'
    assert db.one('SELECT score FROM assessments WHERE application_id=?', (app_row['id'],))['score'] == 100


def test_upload_rejects_bad_input(system):
    config, db, drive, model, scanner, worker = system
    role = db.one('SELECT * FROM roles WHERE active=1 ORDER BY id')
    client, csrf = client_with_csrf(config, db, model, drive)

    assert upload(client, csrf, role['id'], 'malware.exe', b'x').status_code == 400          # bad extension
    assert upload(client, csrf, role['id'], 'empty.txt', b'').status_code == 400              # empty file
    assert upload(client, csrf, 'no-such-role', 'cv.txt', b'x').status_code == 404            # unknown role
    missing = client.post(f"/api/roles/{role['id']}/upload", data={}, content_type='multipart/form-data',
                          headers={'X-CSRF-Token': csrf})
    assert missing.status_code == 400                                                          # no file part
    no_csrf = client.post(f"/api/roles/{role['id']}/upload",
                          data={'file': (io.BytesIO(b'x'), 'cv.txt')}, content_type='multipart/form-data')
    assert no_csrf.status_code == 403                                                          # CSRF enforced
    assert drive.cv_uploads == []                                                              # nothing reached Drive


def test_upload_lifts_body_cap_only_for_this_route(system):
    config, db, drive, model, scanner, worker = system
    role = db.one('SELECT * FROM roles WHERE active=1 ORDER BY id')
    client, csrf = client_with_csrf(config, db, model, drive)

    # A CV well above the 100 KB JSON cap is accepted on the upload route...
    big_cv = b'Name: Big Applicant\n' + b'Built a Python application.\n' * 6000  # ~168 KB
    assert len(big_cv) > 100_000
    assert upload(client, csrf, role['id'], 'big.txt', big_cv).status_code == 200

    # ...while an oversized body on a JSON endpoint is still refused (413).
    oversized_json = client.post('/api/automatic', data=b'{"enabled":true}' + b' ' * 200_000,
                                 content_type='application/json', headers={'X-CSRF-Token': csrf})
    assert oversized_json.status_code == 413


def test_dashboard_renders_upload_and_settings_controls(system):
    config, db, drive, model, scanner, worker = system
    body = create_app(config, db, model, drive).test_client().get('/').get_data(as_text=True)
    # The upload control (#5) and the collapsed Settings section (#4) are present.
    assert 'id="upload"' in body
    assert 'id="cv-file"' in body and 'accept=".pdf,.docx,.txt"' in body
    assert 'id="settings"' in body
