#!/usr/bin/env python3
"""Render reviewable user-systemd units without installing or starting them."""
import argparse
from pathlib import Path
import shutil
import os
from dotenv import load_dotenv
parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,default=Path('deployment/systemd'))
parser.add_argument('--python',type=Path)
parser.add_argument('--ollama',type=Path)
args=parser.parse_args()
project=Path(__file__).resolve().parent.parent
load_dotenv(project/'.env')
# Resolving a venv's python symlink selects the system interpreter and loses its packages.
python=args.python.absolute() if args.python else project/'.venv/bin/python'
ollama=str(args.ollama.absolute()) if args.ollama else os.getenv('HR_OLLAMA_BINARY') or shutil.which('ollama') or '/usr/local/bin/ollama'
port=int(os.getenv('HR_PORT','8787'))
if not 1<=port<=65535:
    raise ValueError('HR_PORT must be 1–65535')
def escape(value):
    return str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')
values={'PROJECT':escape(project),'PYTHON':escape(python),'GUNICORN':escape(python.parent/'gunicorn'),'OLLAMA':escape(ollama),'PORT':str(port)}
args.output.mkdir(parents=True,exist_ok=True)
for source in (project/'scripts/systemd').iterdir():
    text=source.read_text()
    # Quote full values containing a project prefix, including suffixes in template paths.
    output=[]
    for line in text.splitlines():
        for key,value in values.items():line=line.replace('@'+key+'@',value)
        if line.startswith('ExecStart='):
            raw=line[len('ExecStart='):]
            for executable in [values['PYTHON'],values['GUNICORN'],values['PROJECT']+'/scripts/ollama-cpu.sh']:
                if raw.startswith(executable):
                    raw='"'+executable+'"'+raw[len(executable):];break
            line='ExecStart='+raw
        elif line.startswith('Environment=HR_OLLAMA_BINARY='):
            line='Environment="'+line[len('Environment='):]+'"'
        output.append(line)
    (args.output/source.name.removesuffix('.in')).write_text('\n'.join(output)+'\n')
print('Rendered units to',args.output.resolve())
