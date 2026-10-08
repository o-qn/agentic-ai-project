# Project wrap-up: HR screening application.
from hr_agent.dashboard import create_app


def test_review_form_and_saved_actor(system):
    config,db,drive,model,scanner,_=system
    drive.add('review-cv','review.txt','role-1','Name: Example\nPython')
    scanner.run()
    app_id=db.one('SELECT id FROM applications')['id']
    web=create_app(config,db,model);client=web.test_client()
    page=client.get('/')
    assert b'id="review-actor"' in page.data
    with client.session_transaction() as session:csrf=session['csrf']
    headers={'X-CSRF-Token':csrf}
    response=client.post(f'/api/review/{app_id}',json={'action':'retry','reason':'Try again','actor':''},headers=headers)
    assert response.status_code==400 and 'your name' in response.json['error']
    response=client.post(f'/api/review/{app_id}',json={'action':'retry','reason':' Try again ','actor':' Student '},headers=headers)
    assert response.status_code==200
    assert db.one('SELECT actor,reason FROM review_decisions')=={'actor':'Student','reason':'Try again'}


def test_state_and_role_jobs_include_review_reason(system):
    config,db,drive,model,scanner,_=system
    drive.add('review-state','review-state.txt','role-1','Name: Example Person\nPython')
    scanner.run()
    app=db.one("SELECT * FROM applications WHERE file_id='review-state'")
    db.execute("UPDATE applications SET status='review',review_reason=? WHERE id=?",
               ('Unrelated document; HR review required',app['id']))
    web=create_app(config,db,model)
    client=web.test_client()
    status=client.get('/api/status').get_json()
    review=next(item for item in status['reviews'] if item['application_id']==app['id'])
    assert review['review_reason']=='Unrelated document; HR review required'
    detail=client.get('/api/roles/role-1').get_json()
    job=next(item for item in detail['jobs'] if item['application_id']==app['id'])
    assert job['application_status']=='review'
    assert job['review_reason']=='Unrelated document; HR review required'
