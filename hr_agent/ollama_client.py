import fcntl
import json
import time
import hashlib
import requests

class Ollama:
    def __init__(self,config):
        self.config = config

    def identity(self,name):
        if not name:
            raise ValueError('Local model name is not configured')
        response=requests.get(self.config.ollama+'/api/tags',timeout=(3,5))
        response.raise_for_status()
        names={name,name+':latest'}
        for model in response.json().get('models',[]):
            if model.get('name') in names and model.get('digest'):
                return name+'@'+model['digest']
        raise ValueError('Configured model is not installed locally')

    def validation_key(self):
        from .screening_agent import PROMPT_VERSION
        fingerprint={'model':self.identity(self.config.model),'embedding':self.identity(self.config.embed_model),
                     'prompt':PROMPT_VERSION,'context':self.config.context,'output':self.config.output_tokens,
                     'threads':self.config.threads,'num_gpu':0,'think':False}
        return 'model_acceptance:'+hashlib.sha256(json.dumps(fingerprint,sort_keys=True).encode()).hexdigest()

    def is_validated(self,db):
        try:
            return db.setting(self.validation_key(),False) is True
        except (ValueError,requests.RequestException):
            return False

    def request(self,endpoint,body,timeout=None):
        config = self.config
        if not body.get('model'):
            raise ValueError('Set an installed local model in .env and run model-check')
        with (config.data/'inference.lock').open('a') as lock:
            start = time.monotonic()
            budget = timeout or config.timeout
            while True:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()-start>budget:
                        raise TimeoutError('Local inference is busy; retry later')
                    time.sleep(.1)
            body.update(stream=False,keep_alive=0)
            if endpoint=='/api/chat':
                body['think']=False
            body['options'] = {'num_gpu':0,'num_thread':config.threads,'num_ctx':config.context,
                               'num_predict':config.output_tokens,'temperature':0,'seed':17}
            response = requests.post(config.ollama+endpoint,json=body,timeout=(10,max(1,budget-(time.monotonic()-start))))
            response.raise_for_status()
            result = response.json()
            # keep_alive=0 unloads each request so embedding/chat models never remain loaded together.
            return result

    def chat(self,messages,tools,timeout=None):
        from .tool_protocol import envelope_schema,PROTOCOL
        messages=[dict(message) for message in messages]
        messages[0]['content'] += '\n'+PROTOCOL
        allowed=[tool['function']['name'] for tool in tools] if tools else None
        if allowed is not None:
            messages[0]['content']+='\nTools available for this turn: '+', '.join(allowed)+'.'
        payload={'model':self.config.model,'messages':messages,'format':envelope_schema(allowed)}
        # Schema grammar is not prompt text. Bound the actual serialized messages conservatively.
        if len(json.dumps(messages).encode())>self.config.context-self.config.output_tokens-512:
            raise ValueError('Prompt cannot fit conservative context budget; HR review required')
        result=self.request('/api/chat',payload,timeout)
        if result.get('done_reason')=='length':
            raise ValueError('Model output exceeded limit')
        value=json.loads(result['message']['content'])
        if not isinstance(value,dict) or not isinstance(value.get('tool_calls'),list):
            raise ValueError('Invalid structured tool envelope')
        return value

    def structured(self,prompt,schema):
        payload = {'model':self.config.model,'messages':[{'role':'user','content':prompt}],'format':schema}
        if len(json.dumps(payload).encode())>self.config.context-self.config.output_tokens-512:
            raise ValueError('Structured request exceeds context budget')
        result = self.request('/api/chat',payload)
        if result.get('done_reason')=='length':
            raise ValueError('Structured output exceeded limit')
        return json.loads(result['message']['content'])

    def embed(self,text,purpose="document"):
        if self.config.embed_model.startswith("nomic-embed-text"):
            text=("search_query: " if purpose=="query" else "search_document: ")+text
        result = self.request('/api/embed',{'model':self.config.embed_model,'input':text,'truncate':False})
        return result['embeddings'][0]
