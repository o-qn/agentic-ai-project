"""Pluggable text embedding. Local Ollama (CPU) is the default and stays the
default; a hosted embedder is a drop-in that activates only when a key and a
hosted model are configured.

Guarantees:
  * When no hosted key is configured the factory returns the local embedder, so
    indexing and search behave exactly as before (Ollama on the CPU).
  * Every embedder exposes a stable ``identity()``. Changing the active embedding
    model changes that identity, which the indexer treats as a new index version
    and rebuilds WITHOUT touching rubric scores (see applicant_search.index_one).
  * The hosted embedder batches, caches, rate-limits, and records its own usage —
    including provider-reported tokens — in the embedding ledger, separately from
    assessment usage. Ollama is retired only once a hosted embedder is proven.

Nothing here sends text to a hosted provider unless a key is present: the hosted
class is inert until deliberately configured, so this change cannot trigger paid
embedding on its own.
"""
import collections
import hashlib
import json
import math
import time
from pathlib import Path

from . import usage


def make_embedder(config, model_client, http=None):
    """Return the active embedder. Hosted only when a provider, model and key are all set;
    otherwise the local model client (unchanged Ollama/synthetic behaviour)."""
    provider = getattr(config, 'embed_provider', 'ollama')
    if provider and provider != 'ollama' and getattr(config, 'embed_hosted_model', '') and getattr(config, 'embed_key', '').strip():
        return HostedEmbedder(config, http=http)
    return LocalEmbedder(config, model_client)


def _valid_vector(vector):
    return bool(vector) and all(isinstance(v, (int, float)) and math.isfinite(v) for v in vector)


class LocalEmbedder:
    """Delegates to the local model client. Preserves the pre-existing CPU embedding path."""

    def __init__(self, config, model_client):
        self.config = config
        self.model = model_client
        self._identity = None

    def identity(self):
        # Cache the (network-derived) identity so per-section embed() calls need no extra lookups.
        if self._identity is None:
            self._identity = self.model.identity(self.config.embed_model)
        return self._identity

    def embed(self, text, purpose='document'):
        vector = self.model.embed(text, purpose=purpose)
        # Record against the resolved identity when known, else a stable local label;
        # never call identity() here, so a lookup failure cannot break embedding.
        model = self._identity or self.config.embed_model or 'local'
        usage.record_embedding(self.config, model, len(text), provider='local', purpose=purpose)
        return vector

    def embed_batch(self, texts, purpose='document'):
        return [self.embed(text, purpose=purpose) for text in texts]


class HostedEmbedder:
    """Hosted embedding API (e.g. Voyage) with batching, caching, rate limiting and usage.

    Inert unless config.embed_key is set. The key is read from config only and is
    sent as a bearer token; it is never logged, echoed, or placed in an exception.
    """

    def __init__(self, config, http=None, sleep=time.sleep, clock=time.monotonic):
        self.config = config
        self._http = http                       # injectable transport for tests; requests.post otherwise
        self._sleep = sleep
        self._clock = clock
        self._recent = collections.deque()       # monotonic timestamps of real requests, for RPM limiting
        self._cache_dir = Path(config.data)/'embed-cache'

    def identity(self):
        # Provider + model make the index version; a model change forces a rebuild, not a rescore.
        return f'{self.config.embed_provider}:{self.config.embed_hosted_model}'

    # -- caching -------------------------------------------------------------
    def _cache_path(self, purpose, text):
        digest = hashlib.sha256((self.identity()+'\x00'+purpose+'\x00'+text).encode('utf-8')).hexdigest()
        return self._cache_dir/f'{digest}.json'

    def _cache_get(self, purpose, text):
        try:
            vector = json.loads(self._cache_path(purpose, text).read_text())
            return vector if _valid_vector(vector) else None
        except (OSError, ValueError):
            return None

    def _cache_put(self, purpose, text, vector):
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._cache_path(purpose, text).write_text(json.dumps(vector))
        except OSError:
            pass  # a cache write failure only costs a future recompute; never fatal

    # -- rate limiting -------------------------------------------------------
    def _throttle(self):
        rpm = max(1, int(getattr(self.config, 'embed_rpm', 60)))
        while True:
            now = self._clock()
            while self._recent and now-self._recent[0] >= 60:
                self._recent.popleft()
            if len(self._recent) < rpm:
                self._recent.append(now)
                return
            self._sleep(max(0.0, 60-(now-self._recent[0])))

    # -- transport -----------------------------------------------------------
    def _post(self, inputs, input_type):
        headers = {'Authorization': 'Bearer '+self.config.embed_key,
                   'Content-Type': 'application/json',
                   'User-Agent': 'hr-agent-cv-screening/1.0'}   # truthful UA; not an approved-client spoof
        payload = {'model': self.config.embed_hosted_model, 'input': inputs, 'input_type': input_type}
        if self._http is not None:
            response = self._http(self.config.embed_url, headers=headers, json=payload, timeout=self.config.timeout)
        else:
            import requests
            response = requests.post(self.config.embed_url, headers=headers, json=payload, timeout=(10, self.config.timeout))
        status = getattr(response, 'status_code', 200)
        if status != 200:
            # Never surface headers or body; they can echo the key or applicant text.
            raise ValueError(f'Hosted embedding request failed (HTTP {status})')
        return response.json()

    def _embed_remote(self, texts, purpose):
        input_type = 'query' if purpose == 'query' else 'document'
        self._throttle()
        body = self._post(texts, input_type)
        rows = body.get('data') if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ValueError('Hosted embedding response did not match the request')
        vectors = [row.get('embedding') for row in rows]
        if not all(_valid_vector(v) for v in vectors):
            raise ValueError('Hosted embedding response contained an invalid vector')
        reported = None
        if isinstance(body.get('usage'), dict) and isinstance(body['usage'].get('total_tokens'), int):
            reported = body['usage']['total_tokens']       # provider-reported only; never invented
        usage.record_embedding(self.config, self.identity(), sum(len(t) for t in texts),
                               sections=len(texts), provider=self.config.embed_provider,
                               tokens=reported, purpose=purpose)
        return vectors

    # -- public API ----------------------------------------------------------
    def embed(self, text, purpose='document'):
        return self.embed_batch([text], purpose=purpose)[0]

    def embed_batch(self, texts, purpose='document'):
        texts = list(texts)
        results = [None]*len(texts)
        misses = []
        for i, text in enumerate(texts):
            cached = self._cache_get(purpose, text)
            if cached is not None:
                results[i] = cached
            else:
                misses.append(i)
        batch_size = max(1, int(getattr(self.config, 'embed_batch', 64)))
        for start in range(0, len(misses), batch_size):
            window = misses[start:start+batch_size]
            vectors = self._embed_remote([texts[i] for i in window], purpose)
            for i, vector in zip(window, vectors):
                results[i] = vector
                self._cache_put(purpose, texts[i], vector)
        return results
