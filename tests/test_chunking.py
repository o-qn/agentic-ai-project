"""Contract for structure-aware chunking (hr_agent/chunking.py).

Guards the invariants the rest of the system relies on: an exact contiguous
partition per page, the historical ``p{page}-{offset}`` id scheme, heading
context kept out of quotation text, and the section budget.
"""
from hr_agent import chunking
from hr_agent.document_reader import extract_inner


def offsets(chunks):
    return [int(c['id'].split('-')[1]) for c in chunks]


def test_structure_is_an_exact_contiguous_partition():
    text = 'alpha\nbeta\ngamma\n' * 500  # multi-line and well over one chunk
    chunks = chunking.structure_chunks([(1, text)])
    assert ''.join(c['text'] for c in chunks) == text          # nothing dropped or added
    assert all(len(c['text']) <= chunking.MAX_CHARS for c in chunks)
    assert all(c['id'].startswith('p1-') for c in chunks)
    assert offsets(chunks) == sorted(set(offsets(chunks)))       # strictly increasing, unique


def test_short_cv_is_a_single_chunk_with_legacy_id():
    chunks = chunking.structure_chunks([(1, 'Name: Short Applicant\nPython\n')])
    assert len(chunks) == 1
    assert chunks[0]['id'] == 'p1-0'
    assert chunks[0]['text'] == 'Name: Short Applicant\nPython\n'
    assert chunks[0]['heading'] is None


def test_recognised_headings_start_new_sections():
    text = 'Summary\nExperienced engineer.\n\nExperience\nBuilt systems.\n\nEducation\nBSc.\n'
    chunks = chunking.structure_chunks([(1, text)])
    assert [c['heading'] for c in chunks] == ['Summary', 'Experience', 'Education']
    assert ''.join(c['text'] for c in chunks) == text            # partition preserved across splits
    assert chunks[0]['id'] == 'p1-0'
    assert chunks[1]['text'].startswith('Experience')
    assert all(c['heading'] in c['location'] for c in chunks)     # heading surfaced in context


def test_bare_project_word_is_not_a_heading():
    # Mirrors the docx table case: a "Project" cell must not split the section,
    # so 'SQL ledger' stays in the first section (see test_security_and_extraction).
    page = 'Name: Example Person\nBuilt a Python application.\nBuilt a SQL database.\n\nProject\nSQL ledger'
    chunks = chunking.structure_chunks([(1, page)])
    assert len(chunks) == 1
    assert 'SQL ledger' in chunks[0]['text']


def test_heading_text_appears_once_and_never_in_later_subchunks():
    text = 'Experience\n' + 'A line of work.\n' * 200          # one long heading-led section
    chunks = chunking.structure_chunks([(1, text)])
    assert len(chunks) > 1
    assert all(c['heading'] == 'Experience' for c in chunks)      # context on every sub-chunk
    assert chunks[0]['text'].startswith('Experience')
    assert 'Experience' not in ''.join(c['text'] for c in chunks[1:])
    assert ''.join(c['text'] for c in chunks) == text


def test_section_budget_is_exceeded_by_a_huge_unbroken_run():
    chunks = chunking.structure_chunks([(1, 'X' * 300000)])
    assert len(chunks) > chunking.SECTION_BUDGET


def test_multiple_pages_get_page_scoped_ids():
    chunks = chunking.structure_chunks([(1, 'Page one text.\n'), (2, 'Page two text.\n')])
    assert [c['id'] for c in chunks] == ['p1-0', 'p2-0']
    assert chunks[0]['text'] == 'Page one text.\n'
    assert chunks[1]['text'] == 'Page two text.\n'


def test_fixed_strategy_reproduces_legacy_windows():
    text = 'X' * 4000
    chunks = chunking.fixed_chunks([(1, text)])
    assert [c['id'] for c in chunks] == ['p1-0', 'p1-1600', 'p1-3200']
    assert chunks[0]['text'] == text[0:1600]
    assert chunks[2]['text'] == text[3200:4000]
    assert chunks[0]['location'].startswith('page/part 1, characters 0')


def test_hybrid_matches_structure_without_an_embedder():
    pages = [(1, 'Experience\n' + 'python sql work.\n' * 200)]
    assert chunking.hybrid_chunks(pages) == chunking.structure_chunks(pages)


def test_hybrid_with_embedder_stays_exact_and_bounded():
    pages = [(1, 'Experience\n' + ('python work\n\n' * 40) + ('sql ledgers\n\n' * 40))]

    class FakeEmbedder:
        def embed(self, text, purpose='document'):
            return [float(text.count('python')), float(text.count('sql')), 1.0]

    chunks = chunking.hybrid_chunks(pages, FakeEmbedder())
    assert ''.join(c['text'] for c in chunks) == pages[0][1]      # still an exact partition
    assert all(len(c['text']) <= chunking.MAX_CHARS for c in chunks)


def test_dispatcher_and_labels():
    pages = [(1, 'Name: A\nPython\n')]
    assert chunking.chunk(pages, 'structure') == chunking.structure_chunks(pages)
    assert chunking.chunk(pages, 'fixed') == chunking.fixed_chunks(pages)
    assert chunking.strategy_label('structure') == 'structure-v1'


def test_extract_inner_stamps_the_default_strategy(tmp_path):
    path = tmp_path / 'cv.txt'
    path.write_text('Name: Short Applicant\nPython\n')
    result = extract_inner(path, 40)
    assert result['strategy'] == 'structure-v1'
    assert len(result['sections']) == 1
    assert result['sections'][0]['id'] == 'p1-0'
