from pathlib import Path
import pytest
from hr_agent.config import Config
from hr_agent.database import DB
from hr_agent.demo import MemoryDrive,SyntheticModel,seed
from hr_agent.job_queue import Worker

@pytest.fixture
def system(tmp_path):
    config=Config(data=tmp_path,credentials=tmp_path/'credentials.json',root='root',model='synthetic-test-only',embed_model='synthetic-embedding-only')
    db=DB(config.data)
    drive=MemoryDrive()
    model=SyntheticModel()
    scanner=seed(config,db,drive,model)
    worker=Worker(config,db,drive,model)
    return config,db,drive,model,scanner,worker
