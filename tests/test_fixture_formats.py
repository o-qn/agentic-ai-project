from pathlib import Path
import pytest
from hr_agent.document_reader import extract,NeedsReview
FIXTURES=Path(__file__).parent/'fixtures'
@pytest.mark.parametrize('filename',['normal.txt','normal.pdf','normal.docx','scanned.pdf','mixed.pdf'])
def test_qualification_evidence_survives_formats(system,filename):
    config,*_=system
    data=extract(FIXTURES/filename,config)
    text='\n'.join(s['text'] for s in data['sections'])
    assert 'Python inventory application' in text
    assert 'SQL database' in text
@pytest.mark.parametrize('filename',['blank.pdf','corrupt.docx'])
def test_bad_format_is_reviewed(system,filename):
    config,*_=system
    with pytest.raises(NeedsReview):extract(FIXTURES/filename,config)
