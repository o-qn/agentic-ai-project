import json
from hr_agent.rubric import enable_poc
from hr_agent.demo import drain

def test_poc_skips_validation_without_falsifying_model_approval(system):
    _,db,drive,model,scanner,worker=system
    db.set(model.validation_key(),False)
    drive.add('cv','cv.txt','role-1','Name: Test\nPython\n')
    scanner.run();drain(worker)
    assert db.one('SELECT state FROM jobs')['state']=='waiting_model'
    enable_poc(db);drain(worker)
    assert db.one('SELECT score FROM assessments')['score']==30
    assert not model.is_validated(db)

def test_poc_activates_rules_on_new_or_changed_jd(system):
    _,db,drive,_,scanner,_=system
    enable_poc(db)
    drive.add('jd-1','job_description.txt','role-1','Prototype role requires Python projects.')
    scanner.run()
    role=db.one("SELECT * FROM roles WHERE id='role-1'")
    assert not role['paused']
    rubric=db.one('SELECT * FROM rubrics WHERE id=?',(role['rubric_id'],))
    assert rubric['approved_by']=='POC automatic draft — not human reviewed'
    assert 'Python projects' in json.loads(rubric['body'])['criteria'][0]['description']
    original=role['rubric_id'];scanner.run()
    assert db.one("SELECT rubric_id FROM roles WHERE id='role-1'")['rubric_id']==original
