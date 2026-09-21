import json
import pytest
from hr_agent.ollama_client import Ollama
from hr_agent.tool_protocol import envelope_schema
from hr_agent.schemas import TOOL_MODELS

def test_schema_exposes_only_allowed_tools():
    schema=envelope_schema()
    branches=schema['properties']['tool_calls']['items']['properties']['function']['oneOf']
    assert {b['properties']['name']['const'] for b in branches}==set(TOOL_MODELS)
    assert schema['additionalProperties'] is False

def test_response_is_constrained_and_truncation_rejected(system,monkeypatch):
    config,*_=system
    model=Ollama(config)
    payloads=[]
    expected={'tool_calls':[{'function':{'name':'get_cv_outline','arguments':{'application_id':1}}}]}
    def request(endpoint,body,timeout=None):
        payloads.append(body)
        return {'message':{'content':json.dumps(expected)},'done_reason':'stop'}
    monkeypatch.setattr(model,'request',request)
    assert model.chat([{'role':'system','content':'Test'},{'role':'user','content':'Synthetic CV'}],[])==expected
    assert payloads[0]['format']==envelope_schema()
    assert 'tools' not in payloads[0]
    monkeypatch.setattr(model,'request',lambda *a,**k:{'done_reason':'length'})
    with pytest.raises(ValueError,match='exceeded'):model.chat([{'role':'system','content':'Test'}],[])


def test_tool_families_are_separate():
    from hr_agent.tool_protocol import models_for
    assert 'submit_grounded_answer' not in models_for()
    assert set(models_for(['submit_grounded_answer'])) == {'submit_grounded_answer'}
    for names in ([], ['delete_file'], ['submit_grounded_answer', 'submit_assessment']):
        with pytest.raises(ValueError, match='unavailable'):
            models_for(names)


def test_grounded_answer_through_local_transport(system, monkeypatch):
    from jsonschema import Draft202012Validator
    from hr_agent.demo import drain
    from hr_agent.grounded_answer import grounded_answer
    config, db, drive, _, scanner, worker = system
    drive.add('local-answer', 'local.txt', 'role-1',
              'Name: Local Applicant\nBuilt a Python application.\n')
    scanner.run()
    drain(worker)
    client = Ollama(config)
    monkeypatch.setattr(client, 'identity', lambda name: name)
    calls = []
    def request(endpoint, payload, timeout=None):
        calls.append(payload)
        assert endpoint == '/api/chat'
        assert 'get_role_rubric:' not in payload['messages'][0]['content']
        branches = payload['format']['properties']['tool_calls']['items']['properties']['function']['oneOf']
        assert [b['properties']['name']['const'] for b in branches] == ['submit_grounded_answer']
        value = {'tool_calls': [{'function': {'name': 'submit_grounded_answer', 'arguments': {
            'answer': {'claims': [{'text': 'The CV describes Python work.', 'citation_ids': ['c1']}],
                       'insufficient_evidence': False}}}}]}
        Draft202012Validator.check_schema(payload['format'])
        Draft202012Validator(payload['format']).validate(value)
        return {'message': {'content': json.dumps(value)}, 'done_reason': 'stop'}
    monkeypatch.setattr(client, 'request', request)
    result = grounded_answer(config, db, client, 'role-1', 'python')
    assert result['generated']['claims'][0]['citations'][0]['citation_id'] == 'c1'
    assert len(calls) == 1
