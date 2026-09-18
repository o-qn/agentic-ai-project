"""Hosted embedding client (#1): ready-to-activate, inert until a key is configured.

These tests use an injected fake transport — no network, no real key. They prove
the factory stays on local Ollama until a hosted provider, model AND key are all
present, and that once activated the hosted embedder batches, caches, rate-limits,
records provider-reported tokens separately, and versions the index so a model
change rebuilds search WITHOUT rescoring.
"""
import json
import pytest
from hr_agent import embedding, usage, applicant_search
from hr_agent.config import Config
from hr_agent.demo import SyntheticModel, drain


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload


class FakeVoyage:
    """Deterministic stand-in for the hosted embeddings API. Records every call."""
    def __init__(self, status=200):
        self.calls, self.batch_sizes, self.status = [], [], status

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.calls.append({'url': url, 'headers': headers, 'json': json})
        inputs = json['input']
        self.batch_sizes.append(len(inputs))
        data = [{'embedding': self._vec(text), 'index': i} for i, text in enumerate(inputs)]
        return FakeResponse({'data': data, 'usage': {'total_tokens': sum(len(t.split()) for t in inputs)}}, self.status)

    @staticmethod
    def _vec(text):
        # Same shape as the synthetic local model, so cosine search stays comparable.
        return [float(text.lower().count(k)) for k in ['python', 'sql', 'excel']] + [1.0]


def hosted_config(tmp_path, **kw):
    kw.setdefault('embed_hosted_model', 'voyage-3')
    kw.setdefault('embed_key', 'sk-test')
    return Config(data=tmp_path, credentials=tmp_path/'c.json', root='root',
                  embed_provider='voyage', **kw)


def test_make_embedder_stays_local_until_hosted_fully_configured(tmp_path):
    local = Config(data=tmp_path, credentials=tmp_path/'c.json', root='root')
    assert isinstance(embedding.make_embedder(local, SyntheticModel()), embedding.LocalEmbedder)
    # Provider + model set but NO key => inert, still local (cannot trigger paid embedding).
    nokey = Config(data=tmp_path, credentials=tmp_path/'c.json', root='root',
                   embed_provider='voyage', embed_hosted_model='voyage-3')
    assert isinstance(embedding.make_embedder(nokey, SyntheticModel()), embedding.LocalEmbedder)
    # Provider + model + key => hosted.
    assert isinstance(embedding.make_embedder(hosted_config(tmp_path), SyntheticModel()), embedding.HostedEmbedder)


def test_hosted_batches_caches_and_records_tokens_separately(tmp_path):
    config = hosted_config(tmp_path, embed_batch=2, embed_rpm=1000)
    fake = FakeVoyage()
    emb = embedding.HostedEmbedder(config, http=fake)

    vectors = emb.embed_batch(['python', 'sql', 'excel'])          # 3 texts, batch 2 -> 2 requests
    assert len(vectors) == 3 and all(len(v) == 4 for v in vectors)
    assert fake.batch_sizes == [2, 1]
    assert fake.calls[0]['headers']['Authorization'] == 'Bearer sk-test'   # bearer token, not a spoofed client
    assert fake.calls[0]['json']['input_type'] == 'document'

    calls_before = len(fake.calls)
    emb.embed_batch(['python', 'sql', 'excel'])                    # identical -> served from cache
    assert len(fake.calls) == calls_before

    summary = usage.embedding_summary(config)
    assert summary['providers'] == ['voyage'] and summary['sections'] == 3
    assert summary['tokens_reported'] is not None                  # provider-reported, not invented
    assert 'voyage:voyage-3' in dict(summary['by_model'])


def test_hosted_identity_versions_on_model_change(tmp_path):
    small = embedding.HostedEmbedder(hosted_config(tmp_path, embed_hosted_model='voyage-3'))
    large = embedding.HostedEmbedder(hosted_config(tmp_path, embed_hosted_model='voyage-3-large'))
    assert small.identity() == 'voyage:voyage-3'
    assert small.identity() != large.identity()                    # a model change is a new index version


def test_hosted_errors_never_leak_the_key(tmp_path):
    config = hosted_config(tmp_path, embed_rpm=1000)
    emb = embedding.HostedEmbedder(config, http=FakeVoyage(status=401))
    with pytest.raises(ValueError) as caught:
        emb.embed('python')
    assert 'sk-test' not in str(caught.value) and '401' in str(caught.value)
    # A malformed response (wrong row count) is rejected, not silently trusted.
    mismatch = embedding.HostedEmbedder(config, http=lambda url, headers=None, json=None, timeout=None: FakeResponse({'data': [], 'usage': {}}))
    with pytest.raises(ValueError):
        mismatch.embed('python')
    assert usage.embedding_summary(config)['events'] == 0          # failures record no usage


def test_hosted_rate_limit_throttles(tmp_path):
    config = hosted_config(tmp_path, embed_rpm=2, embed_batch=1)
    now, slept = [0.0], []
    def clock():
        return now[0]
    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds                                          # advance time as if we waited
    emb = embedding.HostedEmbedder(config, http=FakeVoyage(), sleep=sleep, clock=clock)
    for i in range(3):                                             # distinct texts -> 3 real requests; 3rd exceeds 2/min
        emb.embed(f'python project {i}')
    assert slept and slept[0] > 0


def test_activating_hosted_embeddings_reindexes_without_rescoring(system, monkeypatch):
    config, db, drive, model, scanner, worker = system
    role = db.one('SELECT * FROM roles WHERE active=1 ORDER BY id')
    incoming = json.loads(role['folders'])['Incoming CVs']
    drive.add('cv-hosted', 'hosted.txt', incoming,
              'Name: Hosted Person\nBuilt a Python application.\nBuilt a SQL database.\n')
    scanner.run()
    drain(worker)
    while applicant_search.index_one(config, db, model):          # local index first
        pass
    app = db.one("SELECT * FROM applications WHERE filename='hosted.txt' AND active=1")
    score_before = db.one('SELECT score FROM assessments WHERE application_id=?', (app['id'],))['score']
    local_identity = app['index_model']
    assert app['index_status'] == 'ready' and local_identity != 'voyage:voyage-3'

    # Activate hosted embeddings and inject the fake transport into the indexer's factory.
    config.embed_provider, config.embed_hosted_model, config.embed_key = 'voyage', 'voyage-3', 'sk-test'
    fake = FakeVoyage()
    monkeypatch.setattr(applicant_search, 'make_embedder',
                        lambda cfg, client: embedding.make_embedder(cfg, client, http=fake))

    while applicant_search.index_one(config, db, model):          # identity change -> rebuild
        pass
    reindexed = db.one("SELECT * FROM applications WHERE id=?", (app['id'],))
    assert reindexed['index_status'] == 'ready'
    assert reindexed['index_model'] == 'voyage:voyage-3' != local_identity
    assert fake.calls                                             # the hosted provider was actually used

    # Rubric scores are untouched by the embedding change; only the search index moved.
    assert db.one('SELECT score FROM assessments WHERE application_id=?', (app['id'],))['score'] == score_before
    models = {row['model'] for row in db.rows('SELECT DISTINCT model FROM chunks WHERE application_id=?', (app['id'],))}
    assert models == {'voyage:voyage-3'}                          # stale local chunks removed

    emb = usage.embedding_summary(config)
    assert 'voyage' in emb['providers'] and emb['tokens_reported'] is not None
