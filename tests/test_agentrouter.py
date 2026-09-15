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
