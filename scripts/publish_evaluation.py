"""Publish a lite benchmark report to the tracked evaluation/ folder for the hosted Performance page.

    python scripts/publish_evaluation.py <runs/lite-benchmark-...json> [--label "..."]

`runs/` is git-ignored, so a deploy carries no benchmark result; GET /api/evaluation/latest falls back to
evaluation/. The published copy keeps the scores and per-document outcomes, adds a dataset label and
replaces every absolute machine path with a repository-relative path or a bare file name. Only a
completed run (not stopped, with documents) is published.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LABEL = ('Synthetic hold-out set (generated Malaysian invoices, notes, bills and receipts; '
                 'no company documents). Hosted settings: lite OCR and one text-model call per document.')
_ABSOLUTE = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\|/)')


def _clean_path(value: str) -> str:
    parts = [part for part in re.split(r'[\\/]+', value) if part]
    if 'ai' in parts:  # a data set inside the repository: ai/synthetic/output/<set>
        return '/'.join(parts[parts.index('ai'):])
    return parts[-1] if parts else ''


def sanitise(value):
    if isinstance(value, dict):
        return {key: sanitise(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitise(item) for item in value]
    if isinstance(value, str) and _ABSOLUTE.match(value):
        return _clean_path(value)
    return value


def publish(source: Path, label: str = DEFAULT_LABEL, folder: Path | None = None) -> Path:
    report = json.loads(Path(source).read_text(encoding='utf-8'))
    if report.get('benchmark') != 'lite-extraction' or report.get('stopped') or not report.get('documents'):
        raise ValueError('only a completed lite-extraction benchmark report can be published')
    published = sanitise(report)
    published['dataset'] = {'label': label, 'synthetic': True}
    folder = folder or ROOT / 'evaluation'
    folder.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r'[^0-9T]', '', report.get('created_at', '')[:19]) or 'undated'
    target = folder / f'lite-benchmark-{stamp}Z.json'
    target.write_text(json.dumps(published, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('report')
    parser.add_argument('--label', default=DEFAULT_LABEL)
    args = parser.parse_args(argv)
    try:
        print('published:', publish(Path(args.report), args.label).relative_to(ROOT).as_posix())
    except ValueError as error:
        sys.exit(str(error))


if __name__ == '__main__':
    main()
