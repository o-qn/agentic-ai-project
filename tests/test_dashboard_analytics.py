# Project wrap-up: HR screening application.
"""Job analytics must describe current comparable CVs without altering results."""
import json
import time
from dataclasses import replace

from hr_agent.analytics import role_summary
from hr_agent.dashboard import create_app
from hr_agent.demo import RUBRIC, drain
from hr_agent.rubric import approve
from hr_agent import usage


def test_score_boundaries_and_stage_counts():
    scores = [0, 19.99, 20, 39.99, 40, 59.99, 60, 79.99, 80, 100]
    applications = [{'id': index, 'status': 'completed'} for index in range(10)]
    applications += [{'id': 10, 'status': 'queued'}, {'id': 11, 'status': 'queued'},
                     {'id': 12, 'status': 'review'}, {'id': 13, 'status': 'duplicate'}]
    rankings = [{'score': value, 'body': json.dumps({'findings': [
        {'criterion_id': 'python', 'level': 'supported'},
        {'criterion_id': 'sql', 'level': 'partial'}]})} for value in scores]
    # Failed historical attempts on a completed candidate do not add a failure;
    # the latest queued job replaces a historical failure on another application.
    jobs = [{'id': 1, 'application_id': 0, 'state': 'failed'},
            {'id': 2, 'application_id': 10, 'state': 'failed'},
            {'id': 3, 'application_id': 11, 'state': 'failed'},
            {'id': 4, 'application_id': 11, 'state': 'queued'}]
    summary = role_summary(applications, rankings, {'body': json.dumps(RUBRIC)}, jobs)
    assert [bucket['count'] for bucket in summary['score_distribution']] == [2, 2, 2, 2, 2]
    assert summary['pipeline'] == dict(pending=1, completed=10, review=1, duplicate=1, failed=1)
    assert sum(summary['pipeline'].values()) == len(applications)
    assert summary['criteria'][0]['supported'] == 10
    assert summary['criteria'][1]['partial'] == 10


def test_job_charts_exclude_other_roles_old_versions_and_paused_rankings(system):
    config, db, drive, model, scanner, worker = system
    drive.add('chart-a', 'a.txt', 'role-1', 'Name: Alpha\nBuilt a Python application.\nBuilt a SQL database.')
    drive.add('chart-b', 'b.txt', 'role-2', 'Name: Beta\nPython, SQL')
    scanner.run()
    drain(worker)
    client = create_app(config, db, model, drive).test_client()
    summary = client.get('/api/roles/role-1').get_json()['analytics']
    assert summary['total'] == 1 and summary['comparable'] == 1 and summary['average_score'] == 100
    assert summary['criteria'][0]['supported'] == 1
    role = db.one("SELECT * FROM roles WHERE id='role-1'")
    approve(db, 'role-1', RUBRIC, 'HR reviewer', role['jd_hash'], 'reassess_all')
    summary = client.get('/api/roles/role-1').get_json()['analytics']
    assert summary['comparable'] == 0 and summary['average_score'] is None
    assert all(bucket['count'] == 0 for bucket in summary['score_distribution'])
    drain(worker)
    db.execute("UPDATE roles SET paused=1 WHERE id='role-1'")
    summary = client.get('/api/roles/role-1').get_json()['analytics']
    assert summary['comparable'] == 0
    assert client.get('/api/roles/not-a-job').status_code == 404


def test_hr_and_owner_pages_are_separate_and_keep_local_boundary(system):
    config, db, drive, model, *_ = system
    client = create_app(config, db, model, drive).test_client()
    hr = client.get('/').get_data(as_text=True)
    owner = client.get('/owner')
    assert owner.status_code == 200
    assert 'id="pipeline-chart"' in hr and 'id="criteria-editor"' in hr
    assert 'id="embedding-provider"' not in hr and 'id="rubric-json"' not in hr
    assert 'id="reset"' not in hr and 'id="embedding-provider"' in owner.get_data(as_text=True)
    assert client.get('/owner', environ_base={'REMOTE_ADDR': '192.0.2.1'}).status_code == 403
    assert client.get('/owner', headers={'Host': 'attacker.example'}).status_code == 400
    assert client.post('/api/embedding-provider', json={'provider': 'ollama'}).status_code == 403
    with client.session_transaction() as session:
        assert session['csrf']


def test_usage_displays_effective_provider_and_separates_history(system):
    config, db, drive, model, *_ = system
    config = replace(config, embed_hosted_model='voyage-test', embed_key='test-only-key')
    db.set('embedding_provider', 'voyage')
    usage.record_embedding(config, 'local-history', 15, provider='local')
    usage.record_embedding(config, 'voyage-test', 20, provider='voyage', tokens=12)
    report = create_app(config, db, model, drive).test_client().get('/api/usage').get_json()
    assert report['configuration']['embedding_provider'] == 'voyage'
    assert report['configuration']['embedding_model'] == 'voyage-test'
    assert report['embedding']['tokens_reported'] == 12
    assert report['activity'][-1]['embedding_events'] == 2
    assert len(report['activity']) == 7
    assert report['activity'][-1]['date'] == time.strftime('%Y-%m-%d', time.gmtime())
    fallback = usage.report(replace(config, embed_key=''), db, model)['configuration']
    assert fallback['embedding_provider'] == 'ollama' and fallback['embedding_fallback'] is True
    assert fallback['embedding_model'] == config.embed_model
