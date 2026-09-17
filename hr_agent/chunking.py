"""Structure-aware CV chunking that produces an exact, contiguous partition.

Three strategies share one output contract so they can be compared directly on
the same input (see scripts/benchmark_chunking.py):

  fixed      legacy fixed-width windows (retained for comparison/fallback)
  structure  split on recognised CV section headings, then size-bound each block
  hybrid     structure, with optional embedding-based splitting inside long blocks

Every strategy returns a list of section dicts:

  {'id', 'location', 'text', 'heading'}

The concatenation of every ``text``, in order, reproduces each page's text
exactly. A recognised heading line stays in the ``text`` of the first sub-chunk
of its block (never duplicated); the heading is ALSO surfaced separately in the
``heading`` field and the ``location`` string, so quoted evidence remains
byte-exact and added heading context is never mixed into a quotation.

Ids follow the historical ``p{page}-{offset}`` scheme, where ``offset`` is the
character position of the chunk within its page. This module never embeds or
reaches the network; semantic splitting is opt-in via an injected embedder and
is off during extraction (the extractor subprocess has no embedding access).
"""
import math
import re

# One place changes the version stamp when chunk boundaries change meaningfully.
LABELS = {'fixed': 'fixed-v1', 'structure': 'structure-v1', 'hybrid': 'hybrid-v1'}
DEFAULT_STRATEGY = 'structure'
STRATEGY = LABELS[DEFAULT_STRATEGY]

MAX_CHARS = 1600        # Hard upper bound per chunk; keeps the section budget comparable to legacy.
TARGET_CHARS = 1200     # Soft target above which hybrid attempts a semantic split.
MIN_CHARS = 200         # Smallest block hybrid tries to keep whole before merging.
SECTION_BUDGET = 160    # Maximum chunks per document (enforced by the caller).
SIM_THRESHOLD = 0.6     # Cosine similarity below which hybrid starts a new sub-chunk.

# Curated CV section titles (lower-cased). Singular 'project' is deliberately absent:
# a bare "Project" table cell or sentence must NOT be treated as a section heading.
KNOWN = {
    'summary', 'professional summary', 'profile', 'objective', 'career objective',
    'about', 'about me', 'personal statement',
    'experience', 'work experience', 'professional experience', 'employment',
    'employment history', 'work history', 'career history', 'relevant experience',
    'education', 'academic background', 'qualifications', 'academic qualifications',
    'skills', 'technical skills', 'core skills', 'key skills', 'core competencies',
    'competencies', 'technical competencies', 'skills summary',
    'projects', 'personal projects', 'selected projects', 'key projects', 'project experience',
    'certifications', 'certificates', 'licenses', 'licences', 'accreditations',
    'publications', 'awards', 'honors', 'honours', 'achievements', 'accomplishments',
    'languages', 'interests', 'hobbies', 'volunteering', 'volunteer experience',
    'references', 'contact', 'contact details', 'personal details', 'additional information',
}

_LETTER = re.compile(r'[A-Za-z]')


def strategy_label(strategy):
    if strategy not in LABELS:
        raise ValueError('Unknown chunk strategy: ' + str(strategy))
    return LABELS[strategy]


def chunk(pages, strategy=DEFAULT_STRATEGY, embedder=None):
    """Chunk ``pages`` (an iterable of ``(page_number, text)``) with ``strategy``."""
    if strategy == 'fixed':
        return fixed_chunks(pages)
    if strategy == 'structure':
        return structure_chunks(pages)
    if strategy == 'hybrid':
        return hybrid_chunks(pages, embedder)
    raise ValueError('Unknown chunk strategy: ' + str(strategy))


def _heading(line):
    """Return the heading text if ``line`` is a recognised CV section title, else None."""
    core = line.strip().rstrip(':').strip()
    if not core or len(core) > 60:
        return None
    low = core.lower()
    if low in KNOWN:
        return core
    # An ALL-CAPS banner that contains a known section word, e.g. "WORK EXPERIENCE".
    if core == core.upper() and _LETTER.search(core):
        words = re.findall(r'[a-z]+', low)
        if words and len(words) <= 5 and any(word in KNOWN for word in words):
            return core
    return None


def _line_spans(text):
    """Character spans of every line INCLUDING its trailing newline; covers text exactly."""
    spans = []
    index, length = 0, len(text)
    while index < length:
        newline = text.find('\n', index)
        if newline == -1:
            spans.append((index, length))
            break
        spans.append((index, newline + 1))
        index = newline + 1
    return spans


def _segments(pages):
    """Yield ``(page_number, text, start, end, heading)`` heading-delimited blocks.

    Segments partition each page's text contiguously: the heading line begins its
    own segment and every character belongs to exactly one segment.
    """
    for page_number, text in pages:
        spans = _line_spans(text)
        if not spans:
            continue
        start, heading = None, None
        for begin, finish in spans:
            found = _heading(text[begin:finish].rstrip('\n').rstrip('\r'))
            if found is not None:
                if start is not None:
                    yield (page_number, text, start, begin, heading)
                start, heading = begin, found
            elif start is None:
                start, heading = begin, None
        if start is not None:
            yield (page_number, text, start, len(text), heading)


def _chunk(page_number, text, start, end, heading):
    location = f'page/part {page_number}, characters {start}–{end}'
    if heading:
        location += f' · section: {heading}'
    return {'id': f'p{page_number}-{start}', 'location': location,
            'text': text[start:end], 'heading': heading}


def _next_cut(text, start, limit):
    """Longest span from ``start`` that fits MAX_CHARS, ending at a line boundary."""
    hard = min(start + MAX_CHARS, limit)
    if hard == limit:
        return limit
    newline = text.rfind('\n', start + 1, hard)
    if newline != -1:
        return newline + 1
    return hard


def _emit_size(page_number, text, start, end, heading, out):
    position = start
    while position < end:
        cut = _next_cut(text, position, end)
        out.append(_chunk(page_number, text, position, cut, heading))
        position = cut


def fixed_chunks(pages):
    """Legacy fixed-width windows, byte-for-byte compatible with the original splitter."""
    out = []
    for page_number, text in pages:
        for start in range(0, len(text), MAX_CHARS):
            part = text[start:start + MAX_CHARS]
            if part.strip():
                out.append(_chunk(page_number, text, start, start + len(part), None))
    return out


def structure_chunks(pages):
    out = []
    for page_number, text, start, end, heading in _segments(pages):
        _emit_size(page_number, text, start, end, heading, out)
    return out


def _paragraph_spans(text, start, end):
    """Split ``[start, end)`` at blank lines; blocks cover the range exactly."""
    blocks, block_start = [], start
    for begin, finish in _line_spans(text[start:end]):
        line_start, line_end = start + begin, start + finish
        if not text[line_start:line_end].strip():  # a blank line closes the current block
            blocks.append((block_start, line_end))
            block_start = line_end
    if block_start < end:
        blocks.append((block_start, end))
    return blocks or [(start, end)]


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    denom = math.sqrt(sum(v * v for v in a) * sum(v * v for v in b))
    return sum(x * y for x, y in zip(a, b)) / denom if denom else 0.0


def _emit_semantic(page_number, text, start, end, heading, embedder, out):
    blocks = _paragraph_spans(text, start, end)
    if len(blocks) <= 1:
        _emit_size(page_number, text, start, end, heading, out)
        return
    vectors = []
    for begin, finish in blocks:
        try:
            vectors.append(embedder.embed(text[begin:finish]) or [])
        except Exception:
            vectors.append([])
    groups = [[blocks[0]]]
    for index in range(1, len(blocks)):
        span = blocks[index][1] - groups[-1][0][0]
        similar = _cosine(vectors[index], vectors[index - 1]) >= SIM_THRESHOLD
        small = groups[-1][-1][1] - groups[-1][0][0] < MIN_CHARS
        if span <= MAX_CHARS and (similar or small):
            groups[-1].append(blocks[index])
        else:
            groups.append([blocks[index]])
    for group in groups:
        _emit_size(page_number, text, group[0][0], group[-1][1], heading, out)


def hybrid_chunks(pages, embedder=None):
    """Structure chunking, refined by embedding similarity inside long blocks.

    With no embedder (the default, and the case during extraction) this is exactly
    :func:`structure_chunks`.
    """
    if embedder is None:
        return structure_chunks(pages)
    out = []
    for page_number, text, start, end, heading in _segments(pages):
        if end - start > TARGET_CHARS:
            _emit_semantic(page_number, text, start, end, heading, embedder, out)
        else:
            _emit_size(page_number, text, start, end, heading, out)
    return out
