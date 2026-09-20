"""Usage accounting, with embedding usage tracked separately from assessment usage.

Two independent sources:
  * assessment usage comes from the committed ``assessments`` rows (and, for the
    hosted provider, its daily request budget);
  * embedding usage comes from an append-only ``embedding-usage.jsonl`` ledger
    written as sections are embedded.

Token counts are recorded only when a provider actually reports them; local CPU
embeddings carry no token figures. No dollar costs are ever invented here — a
cost appears only if a caller passes a provider-verified one, which nothing does
today.
"""
import json
import time
from pathlib import Path

EMBEDDING_LEDGER = 'embedding-usage.jsonl'
MAX_LEDGER_BYTES = 5_000_000  # bound the tail we read back; the ledger is POC-scale


def record_embedding(config, model, chars, sections=1, provider='local', tokens=None, purpose='document'):
    """Append one embedding-usage event. Never raises: usage logging must not break indexing."""
    event = {'at': time.time(), 'model': model or 'unknown', 'sections': int(sections),
             'chars': int(chars), 'provider': provider, 'purpose': purpose}
    if tokens is not None:
        event['tokens'] = int(tokens)  # provider-reported only; absent for local CPU embeddings
    try:
        with (Path(config.data)/EMBEDDING_LEDGER).open('a', encoding='utf-8') as out:
            out.write(json.dumps(event)+'\n')
    except OSError:
        pass


def _read_ledger(config):
    path = Path(config.data)/EMBEDDING_LEDGER
    try:
        if not path.exists():
            return []
        with path.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size-MAX_LEDGER_BYTES))
            raw = handle.read()
    except OSError:
        return []
    lines = raw.decode('utf-8', 'ignore').splitlines()
    if size > MAX_LEDGER_BYTES and lines:
        lines = lines[1:]  # a bounded tail may start mid-line; drop the partial head
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # a torn or corrupt line never breaks the summary
    return events


def embedding_summary(config):
    events = _read_ledger(config)
    reported = [int(e['tokens']) for e in events if isinstance(e.get('tokens'), (int, float))]
    by_model = {}
    for event in events:
        model = event.get('model') or 'unknown'
        by_model[model] = by_model.get(model, 0)+int(event.get('sections', 0) or 0)
    return {'events': len(events),
            'sections': sum(int(e.get('sections', 0) or 0) for e in events),
            'chars': sum(int(e.get('chars', 0) or 0) for e in events),
            'tokens_reported': sum(reported) if reported else None,
            'last': max((e.get('at', 0) for e in events), default=None),
            'by_model': sorted(by_model.items()),
            'providers': sorted({e.get('provider', 'local') for e in events})}


def assessment_summary(config, db, ollama):
    total = db.one('SELECT COUNT(*) AS n FROM assessments')['n']
    last = db.one('SELECT MAX(created) AS t FROM assessments')['t']
    last_24h = db.one('SELECT COUNT(*) AS n FROM assessments WHERE created>=?', (time.time()-86400,))['n']
    remaining = None
    if config.provider == 'agentrouter':
        try:
            remaining = ollama.budget_remaining()
        except Exception:
            remaining = None
    return {'provider': config.provider, 'model': config.model, 'total': total, 'last_24h': last_24h,
            'last': last, 'by_model': db.rows('SELECT model, COUNT(*) AS count FROM assessments GROUP BY model ORDER BY count DESC'),
            'daily_limit': (config.router_daily_requests if config.provider == 'agentrouter' and config.router_daily_requests else None),
            'remaining': remaining}


def failure_summary(db):
    return {'jobs_failed': db.one("SELECT COUNT(*) AS n FROM jobs WHERE state='failed'")['n'],
            'index_failed': db.one("SELECT COUNT(*) AS n FROM applications WHERE index_status='failed' AND active=1")['n'],
            'recent': db.rows("""SELECT application_id, step, error, error_at FROM jobs
              WHERE state='failed' AND error IS NOT NULL ORDER BY error_at DESC LIMIT 5""")}


def report(config, db, ollama):
    return {'assessment': assessment_summary(config, db, ollama),
            'embedding': embedding_summary(config),
            'failures': failure_summary(db)}
