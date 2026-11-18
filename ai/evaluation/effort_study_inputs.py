"""Authorised inputs for the SC2 manual-effort study. No database, tenant data or model call.

Documents: four labelled public benchmark files already in the repository (ai/assets, listed in
ai/manifests with dataset, licence and SHA-256): SROIE receipts (CC-BY-4.0) and synthetic Voxel51
invoices (ODbL). AI-assisted prefill: AccordAI's measured extraction of those exact files, from

  1. EFFORT_STUDY_RUN_DIR, a local `scripts/saic_evaluate.py run` directory, when set; otherwise
  2. the committed snapshot ai/evaluation/effort_study_prefill.json, built from such a run with
     `scripts/saic_evaluate.py study-snapshot RUN_DIR` and pinned to each source file's SHA-256.

Nothing is estimated: when neither source covers every study document, the study is unavailable
and StudyUnavailable says why.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / 'ai' / 'evaluation' / 'effort_study_prefill.json'
SNAPSHOT_FORMAT = 'effort-study-prefill/1'
# Two equivalent sets: one receipt and one invoice each. Participants alternate which set is manual.
SETS = {'A': ['REC00003', 'INV00008'], 'B': ['REC00007', 'INV00011']}
STUDY_FILES = tuple(stem for stems in SETS.values() for stem in stems)


class StudyUnavailable(RuntimeError):
    """The study cannot offer AI-assisted tasks on this server; the message is safe to show."""


def load_samples(root=ROOT):
    from ai.evaluation.dataset_contract import load_manifest
    return {Path(s.source.relative_path).stem: s for kind in ('invoices', 'bills', 'receipts')
            for s in load_manifest(Path(root) / f'ai/manifests/{kind}.json').samples}


def prefill_from_public_result(sample, public):
    """Resolved values exactly as the extraction stored them; unresolved fields stay empty."""
    from ai.evaluation.field_accuracy import prediction_view_from_public_result
    view = prediction_view_from_public_result(sample, public)
    return {cell.field_key: (str(cell.value) if cell.state == 'resolved' and cell.value is not None else None) for cell in view.fields}


def _run_result_paths(run_dir):
    summary = Path(run_dir) / 'summary.jsonl'
    if not summary.is_file():
        raise StudyUnavailable('the configured evaluation run has no summary.jsonl')
    paths = {}
    for line in summary.read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        if row.get('public_result_path'):
            paths[Path(row['file']).stem] = Path(row['public_result_path'])
    return paths


def prefill_from_run(run_dir, samples):
    paths = _run_result_paths(run_dir)
    missing = [stem for stem in STUDY_FILES if stem not in paths or not paths[stem].is_file()]
    if missing:
        raise StudyUnavailable('the configured evaluation run has no stored extraction for ' + ', '.join(missing))
    return {stem: prefill_from_public_result(samples[stem], json.loads(paths[stem].read_text(encoding='utf-8')))
            for stem in STUDY_FILES}


def build_snapshot(run_dir, root=ROOT):
    """Snapshot of a measured run for the study files only: values, hashes and licences, no paths."""
    samples = load_samples(root)
    paths = _run_result_paths(run_dir)
    prefill = prefill_from_run(run_dir, samples)
    documents = {}
    for stem in STUDY_FILES:
        source = samples[stem].source
        raw = (Path(root) / 'ai' / source.relative_path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.sha256:
            raise StudyUnavailable(f'{stem} does not match its manifest hash')
        documents[stem] = {'sourceSha256': source.sha256, 'dataset': source.dataset, 'origin': source.origin,
                           'licence': source.licence, 'licenceReference': source.licence_reference,
                           'resultSha256': hashlib.sha256(paths[stem].read_bytes()).hexdigest(), 'prefill': prefill[stem]}
    meta_file = Path(run_dir) / 'meta.json'
    meta = json.loads(meta_file.read_text(encoding='utf-8')) if meta_file.is_file() else {}
    return {'format': SNAPSHOT_FORMAT, 'run': Path(run_dir).name, 'qwen': meta.get('qwen', 'off'),
            'reproduce': f'python scripts/saic_evaluate.py run --qwen off --out runs/{Path(run_dir).name}; '
                         f'python scripts/saic_evaluate.py study-snapshot runs/{Path(run_dir).name}',
            'documents': documents}


def prefill_from_snapshot(samples, path=None):
    path = SNAPSHOT if path is None else path
    if not Path(path).is_file():
        raise StudyUnavailable('no measured extraction snapshot is installed with this backend')
    snapshot = json.loads(Path(path).read_text(encoding='utf-8'))
    if snapshot.get('format') != SNAPSHOT_FORMAT:
        raise StudyUnavailable('the installed extraction snapshot has an unsupported format')
    documents = snapshot.get('documents') or {}
    for stem in STUDY_FILES:
        entry = documents.get(stem)
        if not entry or not isinstance(entry.get('prefill'), dict):
            raise StudyUnavailable(f'the installed extraction snapshot does not cover {stem}')
        if entry.get('sourceSha256') != samples[stem].source.sha256:
            raise StudyUnavailable(f'the installed extraction snapshot was made for a different {stem}')
    return 'snapshot:' + str(snapshot.get('run')), {stem: documents[stem]['prefill'] for stem in STUDY_FILES}


def study_prefill(samples, environ=None):
    """(source name, {study file: prefill}); raises StudyUnavailable with a reason a person can act on."""
    environ = os.environ if environ is None else environ
    run_dir = (environ.get('EFFORT_STUDY_RUN_DIR') or '').strip()
    if run_dir:
        return 'run:' + Path(run_dir).name, prefill_from_run(run_dir, samples)
    return prefill_from_snapshot(samples)
