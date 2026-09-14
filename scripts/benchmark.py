#!/usr/bin/env python3
"""Measure synthetic workflow; --real uses local Ollama, otherwise labelled test doubles."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import resource
import tempfile
import time
from hr_agent.config import Config
from hr_agent.database import DB
from hr_agent.demo import MemoryDrive,SyntheticModel,seed,drain,RUBRIC
from hr_agent.job_queue import Worker
from hr_agent.ollama_client import Ollama
from hr_agent.scoring import validate_evidence
parser=argparse.ArgumentParser()
parser.add_argument('--real',action='store_true')
parser.add_argument('--repeat',type=int,default=2)
parser.add_argument('--output',type=Path,default=Path('deployment/acceptance-results.json'))
args=parser.parse_args()
fixtures=Path(__file__).resolve().parent.parent/'tests/fixtures'
expected=json.loads((fixtures/'expected.json').read_text())
config=Config.env();results=[];started=time.monotonic()
for repeat in range(args.repeat):
    with tempfile.TemporaryDirectory(prefix='acceptance-',dir=config.data) as folder:
        test_config=replace(config,data=Path(folder),root='root')
        db=DB(test_config.data);drive=MemoryDrive()
        model=Ollama(test_config) if args.real else SyntheticModel()
        scanner=seed(test_config,db,drive,model,count_roles=1)
        for index,(filename,truth) in enumerate(expected.items()):
            drive.add('fixture-'+str(index),filename,'role-1',binary=(fixtures/filename).read_bytes())
        scanner.run();worker=Worker(test_config,db,drive,model);drain(worker)
        for app in db.rows('SELECT * FROM applications ORDER BY id'):
            truth=expected[app['filename']]
            assessment=db.one('SELECT * FROM assessments WHERE application_id=?',(app['id'],))
            statuses=truth['expected_status'];statuses=statuses if isinstance(statuses,list) else [statuses]
            score=assessment['score'] if assessment else None
            errors=[]
            if assessment:
                body=json.loads(assessment['body']);sections=json.loads(app['sections'])
                errors=validate_evidence(body,RUBRIC,sections,{s['id'] for s in sections})
            passed=app['status'] in statuses and not errors and (truth.get('expected_score') is None or score==truth['expected_score'])
            results.append({'repeat':repeat+1,'fixture':app['filename'],'status':app['status'],'score':score,
                            'passed':passed,'evidence_validation_errors':errors,'review_reason':app['review_reason']})
report={'mode':'real local Ollama' if args.real else 'scripted model/in-memory Drive; actual extraction, OCR and reports',
        'model':config.model if args.real else 'SyntheticModel','repeats':args.repeat,
        'elapsed_seconds':round(time.monotonic()-started,2),'process_peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        'extractor_peak_rss_kib':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        'all_passed':all(r['passed'] for r in results),'results':results,
        'limits':'RSS measures are not cgroup enforcement. Real Drive and target-host systemd acceptance are separate.'}
args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='results'},indent=2))
raise SystemExit(0 if report['all_passed'] else 1)
