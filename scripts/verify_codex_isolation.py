"""Offline genuine-CLI probe: fake provider/key, no paid requests. Linux only."""
import sys, json, threading, tempfile, subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from hr_agent.config import Config
from hr_agent.agentrouter_client import AgentRouter
seen=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  self.send_response(200);self.end_headers();self.wfile.write(b'{"models":[]}')
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  seen.append({'tools':[t.get('name',t.get('type')) for t in body.get('tools',[])], 'instructions_chars':len(body.get('instructions','')), 'input_chars':len(json.dumps(body.get('input'))), 'tool_schema_chars':len(json.dumps(body.get('tools')))})
  self.send_response(400);self.end_headers();self.wfile.write(b'{"error":{"message":"synthetic probe stopped"}}')
server=HTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
with tempfile.TemporaryDirectory() as d:
 config=Config(data=Path(d)/'data',credentials=Path(d)/'none',root='synthetic',model='deepseek-v4-flash',router_key='synthetic-key',provider='agentrouter')
 client=AgentRouter(config,max_requests=1)
 original=client._command
 def command(folder):
  args=original(folder)
  args=[a.replace('https://agentrouter.org/v1',f'http://127.0.0.1:{server.server_port}/v1') for a in args]
  return args
 client._command=command
 try:client.structured('Synthetic test',{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False})
 except ValueError:pass
 assert len(seen)==1,client.usage
 assert seen[0]['tools']==['request_user_input'],seen
 prefix=original(d);prefix=prefix[:prefix.index(config.codex_binary)]
 check=subprocess.run(prefix+['/usr/bin/python3','-c','import os; assert not os.path.exists("/home/qn/Documents/Agentic Ai project"); assert not os.path.exists("/home/qn/.codex/config.toml"); print("filesystem isolation passed")'],capture_output=True,text=True)
 assert check.returncode==0,check.stderr
 print(check.stdout.strip());print(json.dumps(seen))
server.shutdown()
