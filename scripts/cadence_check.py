#!/usr/bin/env python3
"""Measure the real 300-second scheduler against synthetic Drive and slow model doubles.

No Google access or real inference. Requires about 5.5 minutes; data must be isolated.
"""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import signal
import threading
import time

from hr_agent.config import Config
from hr_agent.database import DB
from hr_agent.demo import MemoryDrive, SyntheticModel, seed
from hr_agent import service


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    config=Config(data=args.data,credentials=args.data/'unused.json',root='root',
                  model='synthetic-test-only',embed_model='synthetic-embedding-only')
    db=DB(config.data)
    if db.one('SELECT id FROM roles LIMIT 1'):
        raise SystemExit('Use an empty isolated data directory')
    scan_times=[]
    observations=[]
    class ProbeDrive(MemoryDrive):
        def list(self,parent):
            if parent=='root':
                scan_times.append(time.monotonic())
                observations.append(db.setting('active_job'))
                if len(scan_times)==2:
                    for i in range(1,4):
                        self.add(f'new-{i}',f'new-{i}.txt',f'role-{i}',f'Name: New {i}\nPython\n')
            return super().list(parent)
    drive=ProbeDrive();model=SyntheticModel()
    scanner=seed(config,db,drive,model)
    drive.add('slow','slow.txt','role-1','Name: Slow Test\nBuilt a Python project.\nBuilt a SQL database.\n')
    scanner.run()
    scan_times.clear();observations.clear()
    model.delay=165  # Two calls: the second scan occurs during the first assessment.
    db.set('automatic',True)
    service.Drive=lambda config:drive
    service.Ollama=lambda config:model
    # Linux fork shares these test factories with the one child worker.
    multiprocessing.set_start_method('fork',force=True)
    timer=threading.Timer(310,lambda:os.kill(os.getpid(),signal.SIGTERM))
    started=time.monotonic();timer.start()
    try:
        service.run(config)
    finally:
        timer.cancel()
    discovered=db.rows("SELECT role_id FROM applications WHERE file_id LIKE 'new-%'")
    interval=scan_times[1]-scan_times[0] if len(scan_times)>1 else None
    result={'mode':'real wall-clock scheduler; synthetic Drive and slow scripted inference',
            'scan_interval_seconds':round(interval,3) if interval else None,
            'discovered_roles':sorted(row['role_id'] for row in discovered),
            'worker_busy_at_second_scan':bool(len(observations)>1 and observations[1]),
            'elapsed_seconds':round(time.monotonic()-started,3),
            'all_passed':bool(interval and 299<=interval<=305 and len(discovered)==3 and observations[1]),
            'limits':'This validates the scheduler cadence and separate worker, not live Google Drive discovery.'}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result['all_passed'] else 1)


if __name__=='__main__':
    main()
