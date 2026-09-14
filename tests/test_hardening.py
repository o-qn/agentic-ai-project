import json
import time
import threading
from unittest.mock import Mock
import pytest
from hr_agent.demo import RUBRIC,drain
from hr_agent.scoring import ranked
from hr_agent import applicant_search,review
from hr_agent.drive_connector import Drive,DriveError
from hr_agent.ollama_client import Ollama
from hr_agent.rubric import approve

CV='Name: Test Applicant\nBuilt a Python application.\nBuilt a SQL database.\n'

def test_changed_unreadable_jd_pauses_old_ranking(system):
    _,db,drive,_,scanner,worker=system
    drive.add('a','a.txt','role-1',CV);scanner.run();drain(worker)
    drive.add('jd-1','job_description.txt','role-1',binary=b'\xff\x00')
    with pytest.raises(UnicodeError):scanner.run()
    assert not ranked(db,'role-1')
    role=db.one("SELECT * FROM roles WHERE id='role-1'")
    assert role['paused'] and role['jd_error']
    with pytest.raises(ValueError):approve(db,'role-1',RUBRIC,'HR',role['jd_hash'],'reassess_all')

def test_orphan_duplicate_promoted(system):
    _,db,drive,_,scanner,worker=system
    drive.add('a','a.txt','role-1',CV);drive.add('b','b.txt','role-1',CV)
    scanner.run();drain(worker)
    drive.files['a']['trashed']=True
    scanner.run();drain(worker)
    assert [app['file_id'] for app in ranked(db,'role-1')]==['b']

def test_reverting_source_reuses_committed_assessment(system):
    _,db,drive,model,scanner,worker=system
    drive.add('a','a.txt','role-1',CV);scanner.run();drain(worker)
    drive.add('a','a.txt','role-1','Name: Test Applicant\nPython');scanner.run();drain(worker)
    calls=model.calls
    drive.add('a','a.txt','role-1',CV);scanner.run();drain(worker)
    assert ranked(db,'role-1')[0]['score']==100
    assert model.calls==calls
    assert db.one('SELECT COUNT(*) n FROM assessments')['n']==2

def test_ties_skip_second_place_honestly(system):
    config,db,drive,model,scanner,worker=system
    drive.add('a','a.txt','role-1',CV);drive.add('b','b.txt','role-1',CV.replace('Test','Other'))
    scanner.run();drain(worker)
    result=applicant_search.answer(config,db,model,'role-1','Why ranked second?')
    assert result['candidates']==[]

def test_index_yields_after_one_section_and_resumes(system):
    config,db,drive,model,scanner,worker=system
    drive.add('a','a.txt','role-1',CV);scanner.run();drain(worker)
    app=db.one('SELECT * FROM applications')
    sections=json.loads(app['sections'])
    sections.append({'id':'second','location':'second section','text':'Further project evidence'})
    db.execute('UPDATE applications SET sections=? WHERE id=?',(json.dumps(sections),app['id']))
    applicant_search.index_one(config,db,model)
    assert model.embedding_calls==1
    assert db.one('SELECT index_status FROM applications')['index_status']=='pending'
    applicant_search.index_one(config,db,model)
    assert model.embedding_calls==2
    assert db.one('SELECT index_status FROM applications')['index_status']=='ready'

def test_report_exclusion_and_existing_accountant_spelling(system):
    _,db,drive,_,scanner,_=system
    drive.files['role-3']['name']='Acountant '
    drive.add('legacy-report','candidate_ranking.xlsx','role-1',binary=b'legacy report')
    scanner.setup()
    assert db.one('SELECT COUNT(*) n FROM roles')['n']==3
    assert not db.one("SELECT * FROM applications WHERE file_id='legacy-report'")

def test_permission_checks_reject_broad_or_unknown_access(system):
    config,*_=system
    adapter=Drive.__new__(Drive);adapter.config=config
    adapter.request=Mock()
    for permissions in [[],[{'type':'anyone','role':'reader'}],[{'type':'domain','role':'reader'}],
                        [{'type':'user','role':'writer','emailAddress':'unknown@example.invalid'}]]:
        adapter.request.return_value.json.return_value={'permissions':permissions}
        with pytest.raises(DriveError):adapter.assert_private('folder')
    config.hr_principals=('hr@example.invalid',)
    adapter.request.return_value.json.return_value={'permissions':[{'type':'user','role':'owner'},
       {'type':'group','role':'reader','emailAddress':'hr@example.invalid'}]}
    adapter.assert_private('folder')

def test_model_approval_changes_with_digest_and_prompt_config(system,monkeypatch):
    config,db,*_=system
    model=Ollama(config)
    response=Mock()
    response.json.return_value={'models':[{'name':config.model,'digest':'a'},{'name':config.embed_model,'digest':'b'}]}
    monkeypatch.setattr('hr_agent.ollama_client.requests.get',lambda *a,**k:response)
    db.set(model.validation_key(),True)
    assert model.is_validated(db)
    response.json.return_value['models'][0]['digest']='changed'
    assert not model.is_validated(db)

def test_same_job_hr_reassessment_invalidates_running_attempt(system):
    _,db,drive,model,scanner,worker=system
    drive.add('a','a.txt','role-1',CV);scanner.run();worker.tick();worker.tick()
    model.delay=.2
    thread=threading.Thread(target=worker.tick);thread.start();time.sleep(.08)
    app=db.one('SELECT * FROM applications')
    review.decide(db,app['id'],'dismiss','Reconsider current evidence','HR')
    thread.join(timeout=3)
    assert not ranked(db,'role-1')
    assert db.one('SELECT generation FROM jobs')['generation']==1
    drain(worker)
    assert len(ranked(db,'role-1'))==1

def test_approved_rubric_is_immutable(system):
    _,db,*_=system
    with pytest.raises(Exception):db.execute("UPDATE rubrics SET body='{}'")
