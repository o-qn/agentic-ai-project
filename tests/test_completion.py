import json
import os
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hr_agent.demo import RUBRIC, drain
from hr_agent.drive_connector import Drive, DriveError, save_token
from hr_agent.service import following_tick
from hr_agent import applicant_search


def test_parallel_oauth_refresh_writes_whole_private_tokens(tmp_path):
    target = tmp_path / 'token.json'
    def write(i):
        payload = json.dumps({'synthetic_token': str(i) * 10000})
        save_token(target, SimpleNamespace(to_json=lambda: payload))
        json.loads(target.read_text())
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [target]


def test_scan_cadence_coalesces_slow_scans_without_drift():
    assert following_tick(100, 105, 300) == 400
    assert following_tick(100, 401, 300) == 700
    assert following_tick(100, 1000, 300) == 1300


def test_move_checks_hr_access_before_changing_parents():
    drive = Drive.__new__(Drive)
    drive.assert_private = Mock(side_effect=[None, DriveError('Public CV')])
    drive.request = Mock()
    drive.get = Mock()
    with pytest.raises(DriveError):
        drive.move('cv', 'archive')
    assert [c.args for c in drive.assert_private.call_args_list] == [('archive',), ('cv',)]
    drive.request.assert_not_called()


def test_validation_correction_keeps_original_cv_available(system):
    _, db, drive, model, scanner, worker = system
    drive.add('cv', 'cv.txt', 'role-1', 'Name: Short Applicant\nPython\n')
    scanner.run()
    original = model.chat
    turns = []
    def chat(messages, tools, timeout=None):
        ctx = json.loads(messages[-1]['content'])
        turns.append(ctx)
        if len(turns) == 2:
            return {'tool_calls':[{'function':{'name':'check_requirement_evidence','arguments':{
                'assessment':{'findings':[{'criterion_id':'python','level':'partial',
                 'evidence':[{'section_id':'p1-0','quote':'Python'}],
                 'missing_information':'Project evidence absent','explanation':'Lists Python'}],
                 'covered_sections':['p1-0']}}}}]}
        if len(turns) == 3:
            assert ctx['current_sections'][0]['text'] == 'Name: Short Applicant\nPython\n'
            assert ctx['last_tool_result']['validation_errors']
            draft = ctx['draft']
            draft['findings'].append({'criterion_id':'sql','level':'not_demonstrated','evidence':[],
                'missing_information':'SQL not stated','explanation':'No SQL evidence'})
            return {'tool_calls':[{'function':{'name':'submit_assessment','arguments':{'assessment':draft}}}]}
        return original(messages, tools, timeout)
    model.chat = chat
    drain(worker)
    assert db.one('SELECT score FROM assessments')['score'] == 30
    assert len(turns) == 3


def test_candidate_search_combines_lists_levels_and_scope(system):
    config, db, drive, model, scanner, worker = system
    text='Name: Alex Example\nBuilt a Python project.\nBuilt a SQL database.\n'
    drive.add('full','full.txt','role-1',text)
    drive.add('duplicate','copy.txt','role-1',text)
    drive.add('short','short.txt','role-1','Name: Sam Sample\nPython\n')
    drive.add('other','other.txt','role-2',text)
    scanner.run(); drain(worker)
    def query(q):
        return applicant_search.answer(config,db,model,'role-1',q)
    assert len(query('List all candidates')['candidates']) == 3
    assert len(query('Show duplicate applications')['candidates']) == 1
    assert len(query('Who has Python or Excel experience?')['candidates']) == 2
    assert not query('Who has Python and Excel experience?')['candidates']
    assert len(query('Show candidates with supported Python evidence')['candidates']) == 1
    assert query('Who has partial Python evidence?')['candidates'][0]['name'] == 'Sam Sample'
    assert query('Who has SQL not demonstrated?')['candidates'][0]['name'] == 'Sam Sample'
    comparison=query('Compare Alex and Sam')
    assert {c['name'] for c in comparison['candidates']} == {'Alex Example','Sam Sample'}
    assert not query('Compare these two applicants')['candidates']
    assert 'Specify two' in query('Compare these two applicants')['notice']


def test_valid_draft_limits_next_turn_to_finalization(system):
    _, db, drive, model, scanner, worker = system
    drive.add('cv','cv.txt','role-1','Name: Test\nPython\n')
    scanner.run()
    original=model.chat
    calls=0
    def chat(messages,tools,timeout=None):
        nonlocal calls
        calls+=1
        if calls==3:
            assert {t['function']['name'] for t in tools} == {'submit_assessment','request_hr_review'}
            ctx=json.loads(messages[-1]['content'])
            return {'tool_calls':[{'function':{'name':'submit_assessment','arguments':{'assessment':ctx['draft']}}}]}
        result=original(messages,tools,timeout)
        if calls==2:
            result['tool_calls'][0]['function']['name']='check_requirement_evidence'
        return result
    model.chat=chat
    drain(worker)
    assert db.one('SELECT score FROM assessments')['score']==30


def test_bad_job_description_does_not_starve_other_roles(system):
    _, db, drive, _, scanner, _ = system
    drive.add('jd-1','job_description.txt','role-1',binary=b'\xff')
    drive.add('later-role','cv.txt','role-3','Name: Applicant\nPython\n')
    with pytest.raises(UnicodeError):
        scanner.run()
    assert db.one("SELECT paused FROM roles WHERE id='role-1'")['paused']
    assert db.one("SELECT id FROM applications WHERE file_id='later-role'")


def test_service_renderer_preserves_venv_path_and_configured_port(tmp_path):
    project=Path(__file__).resolve().parents[1]
    python=tmp_path/'venv space/bin/python'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    output=tmp_path/'units'
    subprocess.run([sys.executable,str(project/'scripts/render_services.py'),
                    '--python',str(python),'--output',str(output)],
                   check=True,env={**os.environ,'HR_PORT':'8899'})
    scanner=(output/'hr-scanner.service').read_text()
    assert f'ExecStart="{python}" -m hr_agent.cli service' in scanner
    assert '127.0.0.1:8899' in (output/'hr-web.service').read_text()
    assert 'EnvironmentFile=' in (output/'hr-ollama.service').read_text()
