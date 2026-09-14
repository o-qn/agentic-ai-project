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
