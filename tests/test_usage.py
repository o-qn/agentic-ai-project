"""Usage panel (#7): embedding usage tracked separately from assessment usage.

Assessment usage is read from committed assessments; embedding usage is read from
its own append-only ledger. Local CPU embeddings never carry token or dollar
figures, and the reporter never invents a cost.
"""
import json
import types
from hr_agent import usage, applicant_search
from hr_agent.dashboard import create_app
from hr_agent.demo import drain


def test_usage_report_separates_assessment_and_embedding(system):
    config, db, drive, model, scanner, worker = system
    role = db.one('SELECT * FROM roles WHERE active=1 ORDER BY id')
    incoming = json.loads(role['folders'])['Incoming CVs']
    drive.add('cv-usage', 'usage.txt', incoming,
              'Name: Usage Person\nBuilt a Python application.\nBuilt a SQL database.\n')
    scanner.run()
    drain(worker)                                   # assessment usage (assessments row)
    while applicant_search.index_one(config, db, model):
        pass                                        # embedding usage (ledger)

    report = create_app(config, db, model, drive).test_client().get('/api/usage').get_json()

    # Assessment usage: from the assessments table, local provider, no hosted limit.
    assert report['assessment']['total'] >= 1
    assert report['assessment']['provider'] == 'ollama'
    assert report['assessment']['daily_limit'] is None
    assert any(row['model'] == 'synthetic-test-only' for row in report['assessment']['by_model'])

    # Embedding usage: a SEPARATE section, populated by indexing, with no token/dollar figure.
    embedding = report['embedding']
    assert embedding['events'] >= 1 and embedding['sections'] >= 1 and embedding['chars'] > 0
    assert embedding['tokens_reported'] is None            # local CPU: nothing billed
    assert embedding['providers'] == ['local']
    assert 'synthetic-embedding-only' in dict(embedding['by_model'])
    assert 'cost' not in json.dumps(report).lower()        # no invented dollar cost anywhere

    # The ledger persists JSON lines with characters but no token field for local runs.
    ledger = config.data/'embedding-usage.jsonl'
    assert ledger.exists()
    first = json.loads(ledger.read_text().splitlines()[0])
    assert first['provider'] == 'local' and 'chars' in first and 'tokens' not in first


def test_record_embedding_is_defensive_and_summarises(tmp_path):
    cfg = types.SimpleNamespace(data=tmp_path)

    # Local runs: no token figure; provider-reported tokens are summed and labelled.
    usage.record_embedding(cfg, 'nomic@abc', 120, sections=1, provider='local')
    usage.record_embedding(cfg, 'voyage-3', 80, sections=2, provider='voyage', tokens=42)
    # A torn/corrupt line must never break the summary.
    with (tmp_path/usage.EMBEDDING_LEDGER).open('a', encoding='utf-8') as out:
        out.write('{not valid json\n')

    summary = usage.embedding_summary(cfg)
    assert summary['events'] == 2 and summary['sections'] == 3 and summary['chars'] == 200
    assert summary['tokens_reported'] == 42                 # only the provider-reported event counts
    assert summary['providers'] == ['local', 'voyage']

    # Logging must never raise, even when the target directory does not exist.
    usage.record_embedding(types.SimpleNamespace(data=tmp_path/'missing'), 'm', 10)
    empty = usage.embedding_summary(types.SimpleNamespace(data=tmp_path/'missing'))
    assert empty == {'events': 0, 'sections': 0, 'chars': 0, 'tokens_reported': None,
                     'last': None, 'by_model': [], 'providers': []}
