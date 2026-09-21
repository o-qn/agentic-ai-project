"""Offline transport and bounded-loop regression tests; no provider requests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import replace
import pytest
from hr_agent.agentrouter_client import AgentRouter
from hr_agent.schemas import TOOL_MODELS
from hr_agent.tool_protocol import envelope_schema


def client_for(system, **kwargs):
    config, *_ = system
    return AgentRouter(replace(config, provider='agentrouter', model='deepseek-v4-flash',
                               router_key='test-secret', **kwargs), max_requests=3)


def tool():
    return [{'type':'function','function':{'name':'get_cv_outline',
             'parameters':TOOL_MODELS['get_cv_outline'].model_json_schema()}}]


REPLY = {'tool_calls':[{'function':{'name':'get_cv_outline','arguments':{'application_id':1}}}]}


def fake_cli(monkeypatch, client, body=None, code=0, raw=None):
    if raw is None and isinstance(body or REPLY, dict) and 'tool_calls' in (body or REPLY):
        body={'actions':[{'operation':call['function']['name'],'input':call['function']['arguments']} for call in (body or REPLY)['tool_calls']]}
    commands=[]
    def command(directory):
        commands.append(directory)
        script = Path(directory)/'fake.py'
        script.write_text('import json,sys,os\nfrom pathlib import Path\n'
            'assert sys.stdin.read()\n'
            'assert os.environ["CODEX_GATEWAY_API_KEY"] == "test-secret"\n'
            'assert "UNRELATED_SECRET" not in os.environ\n'
            'assert os.environ["HOME"] == os.getcwd()\n'
            f'Path("reply.json").write_text({repr(raw if raw is not None else json.dumps(body or REPLY))})\n'
            'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":2,"output_tokens":5,"untrusted":"test-secret"}}))\n'
            'print("test-secret",file=sys.stderr)\n'
            f'sys.exit({code})\n')
        return [sys.executable,str(script)]
    monkeypatch.setattr(client,'_command',command)
    return commands


def test_command_isolated_and_secret_only_in_environment(system, monkeypatch, tmp_path):
    client=client_for(system)
    args=client._command(str(tmp_path))
    joined=' '.join(args)
    assert args[0]=='/usr/bin/bwrap'
    for flag in ['--die-with-parent','--unshare-all','--ignore-user-config','--ignore-rules',
                 '--ephemeral','--output-schema','--json']:
        assert flag in args
    assert 'test-secret' not in joined
    assert 'features.shell_tool=false' in args and 'features.plugins=false' in args
    assert 'model_providers.AgentRouter.request_max_retries=0' in args
    assert args[-1]=='-'
    monkeypatch.setenv('UNRELATED_SECRET','must-not-inherit')
    normal_home=tmp_path/'normal-codex';normal_home.mkdir()
    config_file=normal_home/'config.toml';config_file.write_text('sentinel')
    monkeypatch.setenv('CODEX_HOME',str(normal_home))
    directories=fake_cli(monkeypatch,client)
    assert client.chat([{'role':'user','content':'synthetic'}],tool()) == REPLY
    assert all(not Path(d).exists() for d in directories)
    assert config_file.read_text()=='sentinel'
    assert list(normal_home.iterdir())==[config_file]
    log=(client.config.data/'api-usage.jsonl').read_text()
    assert 'test-secret' not in log and 'synthetic' not in log
    assert json.loads(log)['usage']=={'input_tokens':10,'cached_input_tokens':2,'output_tokens':5}
    assert 'test-secret' not in repr(client.config)


@pytest.mark.parametrize('raw', ['not json','{}',json.dumps({'tool_calls':[]}),
    json.dumps({'tool_calls':[{'function':{'name':'delete_file','arguments':{}}}]}),
    json.dumps({'tool_calls':[{'function':{'name':'get_cv_outline','arguments':{'application_id':1,'extra':'x'}}}]})])
def test_output_schema_rejected(system,monkeypatch,raw):
    client=client_for(system);fake_cli(monkeypatch,client,raw=raw)
    with pytest.raises(ValueError,match='invalid JSON or schema'):
        client.chat([{'role':'user','content':'test'}],tool())


def test_nonzero_exit_is_sanitized(system,monkeypatch):
    client=client_for(system);fake_cli(monkeypatch,client,code=7)
    with pytest.raises(ValueError,match='exit 7') as error:
        client.chat([{'role':'user','content':'test'}],tool())
    assert 'test-secret' not in str(error.value)
    assert client.usage[-1]['error']=='request_failed'


def test_daily_budget_survives_new_client(system,monkeypatch):
    first=client_for(system,router_daily_requests=1);fake_cli(monkeypatch,first)
    first.chat([{'role':'user','content':'test'}],tool())
    second=client_for(system,router_daily_requests=1)
    monkeypatch.setattr(second,'_command',lambda _:pytest.fail('must stop before launch'))
    with pytest.raises(ValueError,match='daily request limit'):
        second.chat([],tool())


def test_trial_limit(system,monkeypatch):
    client=client_for(system);client.max_requests=0
    monkeypatch.setattr(client,'_command',lambda _:pytest.fail('must not launch'))
    with pytest.raises(ValueError,match='trial request limit'):client.chat([],tool())


def test_timeout_kills_child_process_group(system,monkeypatch,tmp_path):
    client=client_for(system)
    marker=tmp_path/'child-pid'
    def command(directory):
        script=Path(directory)/'sleep.py'
        script.write_text('import subprocess,sys,time\nfrom pathlib import Path\n'
            'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
            f'Path({str(marker)!r}).write_text(str(child.pid))\n'
            'time.sleep(60)\n')
        return [sys.executable,str(script)]
    monkeypatch.setattr(client,'_command',command)
    started=time.monotonic()
    with pytest.raises(ValueError,match='timed out'):
        client.chat([],tool(),timeout=.5)
    assert time.monotonic()-started<5
    pid=int(marker.read_text())
    # An exited child can briefly remain a zombie until adopted/reaped.
    stat=Path(f'/proc/{pid}/stat')
    for _ in range(100):
        if not stat.exists() or stat.read_text().split()[2]=='Z':break
        time.sleep(.01)
    else:pytest.fail('child survived timeout')


def test_embedding_delegation_and_cpu(system,monkeypatch):
    client=client_for(system)
    monkeypatch.setattr(client,'_complete',lambda *a,**k:pytest.fail('hosted embeddings'))
    requests=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'embeddings':[[1,2]]}
    monkeypatch.setattr('hr_agent.ollama_client.requests.post',
        lambda url,**kwargs: requests.append((url,kwargs)) or Response())
    assert client.embed('example')==[1,2]
    assert requests[0][0].endswith('/api/embed')
    assert requests[0][1]['json']['options']['num_gpu']==0


def test_structured_schema(system,monkeypatch):
    client=client_for(system);fake_cli(monkeypatch,client,body={'ok':True})
    assert client.structured('synthetic',{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']})=={'ok':True}


def test_json_encoded_arguments_still_strict(system,monkeypatch):
    client=client_for(system)
    body={'tool_calls':[{'function':{'name':'get_cv_outline','arguments':json.dumps({'application_id':1})}}]}
    fake_cli(monkeypatch,client,body=body)
    assert client.chat([],tool())==REPLY
    body['tool_calls'][0]['function']['arguments']=json.dumps({'application_id':1,'extra':'forbidden'})
    fake_cli(monkeypatch,client,body=body)
    with pytest.raises(ValueError,match='schema'):client.chat([],tool())


def test_hosted_scope_and_saved_turn_limit(system,monkeypatch):
    from hr_agent.screening_agent import ScreeningAgent
    from hr_agent.document_reader import NeedsReview, sections_from_pages
    from hr_agent.demo import RUBRIC
    config,db,drive,model,scanner,_=system
    drive.add('scope','scope.txt','role-1','Skills: Python, SQL.')
    scanner.run()
    app=db.one("SELECT * FROM applications WHERE file_id='scope'")
    app['sections']=json.dumps(sections_from_pages([(1,'Skills: Python, SQL.')]))
    job=db.one('SELECT * FROM jobs WHERE application_id=?',(app['id'],))
    client=client_for(system,router_max_turns=2)
    fake_cli(monkeypatch,client,body={'tool_calls':[{'function':{'name':'read_cv_sections',
             'arguments':{'application_id':999999,'section_ids':['p1-0']}}}]})
    agent=ScreeningAgent(db,client,client.config)
    with pytest.raises(NeedsReview,match='2 model turns'):agent.run(job,app,RUBRIC)
    saved=db.one('SELECT * FROM jobs WHERE id=?',(job['id'],))
    state=json.loads(saved['agent_state'])
    assert state['turns']==2 and state['seen']==[]
    assert 'scope violation' in state['result']['error']
    with pytest.raises(NeedsReview,match='2 model turns'):agent.run(saved,app,RUBRIC)
    assert client.requests_made==2


def test_hosted_status_and_budget(system,monkeypatch):
    from hr_agent.dashboard import create_app
    _,db,*_=system
    client=client_for(system,router_daily_requests=1)
    assert client.budget_remaining()==1
    client._reserve()
    assert client.budget_remaining()==0
    response=create_app(client.config,db,client).test_client().get('/api/status')
    assert response.status_code==200
    assert response.json['model_provider']=='agentrouter'
    assert response.json['model_turn_limit']==6
    assert response.json['hosted_requests_remaining']==0


def test_service_pauses_at_daily_limit(system,monkeypatch):
    from types import SimpleNamespace
    from hr_agent import service
    _,db,*_=system
    client=client_for(system,router_daily_requests=1)
    client._reserve();db.set('automatic',True)
    monkeypatch.setattr(service.signal,'signal',lambda *a:None)
    monkeypatch.setattr(service,'Drive',lambda c:None)
    monkeypatch.setattr(service,'create_model',lambda c:client)
    monkeypatch.setattr(service,'Worker',lambda *a:SimpleNamespace(ollama=client,tick=lambda:pytest.fail('must not process backlog')))
    stop=SimpleNamespace(is_set=lambda:not db.setting('automatic'),wait=lambda _:None)
    service.worker_process(client.config,stop)
    assert db.setting('automatic') is False


def test_missing_and_oversized_output(system,monkeypatch):
    client=client_for(system)
    monkeypatch.setattr(client,'_command',lambda d:[sys.executable,'-c','import sys;sys.stdin.read()'])
    with pytest.raises(ValueError,match='missing or exceeds'):client.chat([],tool())
    fake_cli(monkeypatch,client,raw='x'*65537)
    with pytest.raises(ValueError,match='missing or exceeds'):client.chat([],tool())


def test_unexpected_internal_tool_rejected(system,monkeypatch):
    client=client_for(system)
    def command(directory):
        program='import sys,json;from pathlib import Path;sys.stdin.read();Path("reply.json").write_text('+repr(json.dumps({'actions':[{'operation':'get_cv_outline','input':{'application_id':1}}]}))+');print(json.dumps({"type":"item.completed","item":{"type":"command_execution"}}))'
        return [sys.executable,'-c',program]
    monkeypatch.setattr(client,'_command',command)
    with pytest.raises(ValueError,match='unexpected internal tool'):client.chat([],tool())


def test_disallowed_known_operation_rejected(system,monkeypatch):
    client=client_for(system)
    fake_cli(monkeypatch,client,body={'tool_calls':[{'function':{'name':'get_role_rubric','arguments':{'role_id':'role-1'}}}]})
    with pytest.raises(ValueError,match='schema'):client.chat([],tool())


def test_grounded_answer_through_codex_transport(system, monkeypatch):
    from hr_agent.demo import drain
    from hr_agent.grounded_answer import grounded_answer
    from hr_agent.scoring import ranked
    _, db, drive, _, scanner, worker = system
    drive.add('grounded', 'grounded.txt', 'role-1',
              'Name: Synthetic Applicant\nBuilt a Python application.\n')
    scanner.run()
    drain(worker)
    before = [(r['id'], r['score'], r['rank']) for r in ranked(db, 'role-1')]
    client = client_for(system)
    monkeypatch.setattr(client, 'identity', lambda name: name)
    body = {'tool_calls': [{'function': {'name': 'submit_grounded_answer', 'arguments': {
        'answer': {'claims': [{'text': 'The CV describes a Python application.',
                               'citation_ids': ['c1']}], 'insufficient_evidence': False}}}}]}
    fake_cli(monkeypatch, client, body=body)
    original = client._complete
    def complete(prompt, schema, timeout=None):
        assert 'submit_grounded_answer' in prompt
        assert 'get_role_rubric:' not in prompt
        operations = schema['properties']['actions']['items']['anyOf']
        assert [b['properties']['operation']['enum'] for b in operations] == [['submit_grounded_answer']]
        return original(prompt, schema, timeout)
    monkeypatch.setattr(client, '_complete', complete)
    result = grounded_answer(client.config, db, client, 'role-1', 'python')
    claim = result['generated']['claims'][0]
    assert claim['citations'][0]['citation_id'] == 'c1'
    assert 'Python' in claim['citations'][0]['quote']
    assert [(r['id'], r['score'], r['rank']) for r in ranked(db, 'role-1')] == before
    assert client.requests_made == 1 and client.usage[-1]['ok']


@pytest.mark.parametrize('operation,arguments', [
    ('get_cv_outline', {'application_id': 1}),
    ('submit_grounded_answer', {'answer': {'claims': [], 'score': 100}}),
])
def test_grounded_transport_rejects_other_tools_and_extra_fields(system, monkeypatch, operation, arguments):
    from hr_agent.schemas import GroundedArgs
    client = client_for(system)
    fake_cli(monkeypatch, client, body={'tool_calls': [{'function': {
        'name': operation, 'arguments': arguments}}]})
    answer_tools = [{'type': 'function', 'function': {'name': 'submit_grounded_answer',
                    'parameters': GroundedArgs.model_json_schema()}}]
    with pytest.raises(ValueError, match='schema'):
        client.chat([], answer_tools)
