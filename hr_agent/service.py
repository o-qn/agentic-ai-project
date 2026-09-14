import fcntl
import multiprocessing
import signal
import time
from .database import DB
from .drive_connector import Drive
from .scanner import Scanner
from .job_queue import Worker
from .ollama_client import Ollama
from . import applicant_search,reports

def following_tick(scheduled,now,interval):
    """Keep the discovery cadence anchored; coalesce ticks missed during a slow scan."""
    return scheduled + (max(0,int((now-scheduled)//interval))+1)*interval

def worker_process(config,stop):
    # Forked workers must not inherit the scheduler's SIGTERM handler: that handler
    # only sets an event and cannot interrupt a hung request during forced shutdown.
    signal.signal(signal.SIGTERM,signal.SIG_DFL)
    db = DB(config.data)
    worker = Worker(config,db,Drive(config),Ollama(config))
    while not stop.is_set():
        db.set('worker_heartbeat',time.time())
        if not db.setting('automatic',False):
            stop.wait(1)
            continue
        if worker.tick():
            continue
        if applicant_search.index_one(config,db,worker.ollama):
            continue
        # Rubric changes, deletions and review decisions can dirty reports without a CV job.
        for role in db.rows('SELECT id FROM roles WHERE active=1 AND revision>synced_revision'):
            key='report_retry:'+role['id']
            retry=db.setting(key,{'attempts':0,'next':0})
            if retry['next']>time.time() or retry['attempts']>=5:
                continue
            try:
                reports.sync(config,db,worker.drive,role['id'])
                db.set(key,{'attempts':0,'next':0})
            except Exception as exc:
                n=retry['attempts']+1
                db.set(key,{'attempts':n,'next':time.time()+min(3600,30*2**n),'error':type(exc).__name__,'at':time.time()})
        stop.wait(1)

def run(config):
    # Lifetime lock: exactly one scheduler and its worker per data directory.
    with (config.data/'scheduler.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('A scheduler already owns this data directory') from exc
        db=DB(config.data)
        scanner=Scanner(config,db,Drive(config))
        stop=multiprocessing.Event()
        for sig in (signal.SIGTERM,signal.SIGINT):
            signal.signal(sig,lambda *_:stop.set())
        process=multiprocessing.Process(target=worker_process,args=(config,stop),daemon=False)
        process.start()
        next_tick=time.monotonic()
        try:
            while not stop.is_set():
                db.set('service_heartbeat',time.time())
                check=db.setting('check_now',False)
                if time.monotonic()>=next_tick or check:
                    db.set('check_now',False)
                    try:
                        scanner.run()
                    except Exception:
                        pass  # Scanner persists a timestamped diagnostic.
                    now=time.monotonic()
                    if now>=next_tick:
                        next_tick=following_tick(next_tick,now,config.scan_seconds)
                    db.set('next_scan',time.time()+next_tick-now)
                if not process.is_alive():
                    raise RuntimeError('Worker process exited; systemd will restart the service')
                stop.wait(1)
        finally:
            stop.set()
            process.join(timeout=min(config.timeout+15,150))
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            db.set('active_job',None)
