#!/usr/bin/env python3
"""Compare fixed / structure / hybrid chunking on the same inputs, fully offline.

Chunking operates on page text, so this benchmarks on text fixtures (the .txt
files under tests/fixtures plus a few synthetic multi-section CVs). PDF/DOCX feed
the identical page-text pipeline after extraction, so text inputs are
representative for a boundary comparison.

Hybrid's optional semantic split needs an embedder. To keep this offline and
free of any paid or networked call, it uses a small deterministic bag-of-words
embedder defined here purely for illustration. It is NOT the production embedder
and must not be read as evidence about a hosted or Ollama model.

Run:  .test-venv/bin/python scripts/benchmark_chunking.py
"""
import argparse
import json
import re
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run directly from a checkout
from hr_agent import chunking

FIXTURES = Path(__file__).resolve().parent.parent / 'tests/fixtures'

SYNTHETIC = {
    'synthetic-structured.txt': (
        'Name: Jordan Sample\n\n'
        'Summary\nBackend engineer with five years of experience.\n\n'
        'Experience\n' + 'Built and operated Python services on a large team.\n' * 40 +
        '\nSkills\nPython, SQL, Docker, Kubernetes, CI/CD.\n\n'
        'Education\nBSc Computer Science.\n\n'
        'Projects\nInventory API; reporting pipeline.\n'
    ),
    'synthetic-flat.txt': 'Name: Pat Flat\n' + 'A single long paragraph of prose without headings. ' * 200 + '\n',
}


class IllustrativeEmbedder:
    """Deterministic, offline bag-of-words vectors. Illustration only, not production."""
    VOCAB = ('python', 'sql', 'docker', 'kubernetes', 'education', 'project', 'experience', 'skills')

    def embed(self, text, purpose='document'):
        low = text.lower()
        return [float(len(re.findall(r'(?<!\w)' + word + r'(?!\w)', low))) for word in self.VOCAB] + [1.0]


def load_inputs():
    inputs = {}
    for path in sorted(FIXTURES.glob('*.txt')):
        try:
            inputs[path.name] = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError):
            continue
    inputs.update(SYNTHETIC)
    return inputs


def measure(chunks, text):
    sizes = [len(c['text']) for c in chunks] or [0]
    return {
        'chunks': len(chunks),
        'exact_partition': ''.join(c['text'] for c in chunks) == text,
        'min_chars': min(sizes),
        'mean_chars': round(mean(sizes), 1),
        'max_chars': max(sizes),
        'over_max': sum(1 for s in sizes if s > chunking.MAX_CHARS),
        'headed_chunks': sum(1 for c in chunks if c.get('heading')),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('output/chunking-benchmark.json'))
    args = parser.parse_args()

    embedder = IllustrativeEmbedder()
    report = {'max_chars': chunking.MAX_CHARS, 'section_budget': chunking.SECTION_BUDGET,
              'note': 'hybrid uses an illustrative offline embedder, not the production embedder',
              'fixtures': {}}
    for name, text in load_inputs().items():
        pages = [(1, text)]
        report['fixtures'][name] = {
            'source_chars': len(text),
            'fixed': measure(chunking.fixed_chunks(pages), text),
            'structure': measure(chunking.structure_chunks(pages), text),
            'hybrid': measure(chunking.hybrid_chunks(pages, embedder), text),
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')

    header = f"{'fixture':<28}{'strategy':<12}{'chunks':>7}{'mean':>8}{'max':>7}{'headed':>8}{'exact':>7}"
    print(header)
    print('-' * len(header))
    for name, data in report['fixtures'].items():
        for strategy in ('fixed', 'structure', 'hybrid'):
            row = data[strategy]
            exact = 'ok' if row['exact_partition'] and row['over_max'] == 0 else 'FAIL'
            # 'fixed' is intentionally not a whitespace-preserving partition; skip its exact flag.
            if strategy == 'fixed':
                exact = '-'
            print(f"{name[:27]:<28}{strategy:<12}{row['chunks']:>7}{row['mean_chars']:>8.0f}"
                  f"{row['max_chars']:>7}{row['headed_chunks']:>8}{exact:>7}")
        print()
    print('Wrote', args.output)


if __name__ == '__main__':
    main()
