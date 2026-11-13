"""Field accuracy and speed of the lite extraction engine on the labelled public set.

usage:
  python scripts/lite_benchmark.py [--types invoice bill receipt] [--limit N]
                                   [--date-order DMY|MDY] [--ai off|text]
                                   [--manifest ai/synthetic/output/tuning]
                                   [--output runs/lite-benchmark-<UTC>.json]

Runs services.extraction.lite.pipeline.execute_lite_extraction on each sample in
ai/manifests/{invoices,bills,receipts}.json and scores the public result with the
unchanged field evaluator (ai/evaluation/field_accuracy.py). No database, Redis or
storage is used. With --ai text the configured provider is called (key from the
environment, never read from a file here); without a key the model step fails and
the documents fall back to rules, which the report shows as AI errors.

Numbers are measured, never estimated. Accuracy is exact match over labelled
PRESENT fields; coverage is the share of those fields that received any value.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MANIFESTS = {'invoice': 'invoices.json', 'bill': 'bills.json', 'receipt': 'receipts.json'}
# Only model-provider settings are read from --env-file; nothing else (database, storage, mail).
ENV_PREFIXES = ('AI_',)
ENV_NAMES = {'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'DASHSCOPE_API_KEY', 'QWEN_CLOUD_API_KEY', 'OPENROUTER_API_KEY'}
# A provider that answers with one of these will not succeed on the next document either.
FATAL_AI_ERRORS = {'AI_AUTH_REJECTED', 'AI_BILLING_REQUIRED', 'AI_RATE_LIMITED', 'AI_MODEL_OR_ENDPOINT_NOT_FOUND',
                   'AI_BASE_URL_INVALID', 'AI_API_KEY_MISSING', 'AI_MODEL_MISSING', 'AI_PROVIDER_UNKNOWN',
                   'AI_PROVIDERS_INVALID', 'AI_EXTRA_BODY_INVALID', 'AI_BAD_REQUEST'}


def load_provider_env(path):
    """Copy model-provider variables from an env file into this process, without printing them.

    Variables already set in the environment (for example on the command line) win, so a
    run can override AI_PROVIDERS or blank AI_BASE_URL. Returns the number of names loaded.
    """
    from dotenv import dotenv_values
    loaded = 0
    for name, value in dotenv_values(path).items():
        if value is None or not (name.startswith(ENV_PREFIXES) or name in ENV_NAMES) or name in os.environ:
            continue
        os.environ[name] = value
        loaded += 1
    return loaded


def _ratio(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _seconds(values):
    if not values:
        return None
    ordered = sorted(values)
    return {'mean': round(statistics.mean(ordered), 3), 'median': round(statistics.median(ordered), 3),
            'max': round(ordered[-1], 3), 'p90': round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 3)}


def _counts(summary):
    d = summary.denominators
    normalized = summary.address_normalized_accuracy
    return {'eligible': d.eligible_present, 'correct': d.correct, 'incorrect': d.incorrect, 'missing': d.missing,
            'false_fills': d.false_fills, 'accuracy': _ratio(d.correct, d.eligible_present),
            'coverage': _ratio(d.correct + d.incorrect, d.eligible_present),
            'address_normalized_accuracy': float(normalized) if normalized is not None else None}


def unmapped_keys(public, kind):
    """Lite source keys the evaluator cannot place in this document type's dictionary."""
    from ai.evaluation.field_accuracy import _canonical_field_key
    from ai.evaluation.field_dictionary import allowed_keys
    allowed = allowed_keys(kind, 'document')
    return sorted(key for key in public.get('fields', {}) if _canonical_field_key(key, kind) not in allowed)


def run(args):
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    if args.ai:
        os.environ['AI_EXTRACTION'] = args.ai
    if args.env_file:
        print(f'provider variables loaded from env file: {load_provider_env(args.env_file)}')
    os.environ['DATE_ORDER'] = args.date_order
    from ai.evaluation.dataset_contract import load_manifest
    from ai.evaluation.field_accuracy import (evaluate_document_fields, prediction_view_from_public_result,
                                              summarize_collection)
    from services.extraction.ai.text_llm import ai_mode
    from services.extraction.lite import pages
    from services.extraction.lite.pipeline import execute_lite_extraction

    mode = ai_mode()
    warm = pages.warm_engine()  # Load OCR weights once so the first document is not penalised.
    documents, results = [], defaultdict(list)
    fields = defaultdict(lambda: {'eligible': 0, 'correct': 0, 'incorrect': 0, 'missing': 0, 'not_correct': []})
    outcomes, statuses, ai_errors, unmapped = Counter(), Counter(), Counter(), defaultdict(set)
    discards, models = Counter(), Counter()
    tokens_used, stopped = 0, None
    # Public set: manifests in ai/manifests, files relative to ai/. A generated set
    # (--manifest DIR, e.g. ai/synthetic/output/tuning) keeps both in one folder.
    manifest_dir = Path(args.manifest).resolve() if args.manifest else ROOT / 'ai' / 'manifests'
    source_root = manifest_dir if args.manifest else ROOT / 'ai'
    with tempfile.TemporaryDirectory(prefix='lite-benchmark-') as scratch:
        for kind in args.types:
            samples = load_manifest(manifest_dir / MANIFESTS[kind]).samples
            for sample in samples[:args.limit] if args.limit else samples:
                if stopped:
                    break
                done = [d['tokens'] for d in documents if d.get('tokens')]
                if args.token_budget and done and tokens_used + max(done) > args.token_budget:
                    stopped = f'TOKEN_BUDGET ({tokens_used} of {args.token_budget} used)'
                    break
                path = source_root / sample.source.relative_path
                name = Path(sample.source.relative_path).stem
                if args.pace and documents:
                    # Stay under a provider's per-minute token limit instead of hitting it.
                    time.sleep(max(0.0, args.pace - (time.perf_counter() - last_start)))
                last_start = started = time.perf_counter()
                result = execute_lite_extraction(sample.sample_id, path, project_root=scratch)
                wall = time.perf_counter() - started
                row = {'file': name, 'type': kind, 'status': result['execution_status'],
                       'extraction_status': result.get('extraction_status'), 'error': result.get('error'),
                       'seconds': round(wall, 3)}
                statuses[result.get('extraction_status') or result['execution_status']] += 1
                if result['execution_status'] != 'EXTRACTED':
                    evaluation = evaluate_document_fields(sample, None)
                else:
                    public = json.loads(Path(result['public_result_path']).read_text(encoding='utf-8'))
                    provenance = public['provenance']
                    ai = provenance.get('ai') or {}
                    row.update(read_seconds=provenance.get('read_seconds'), model_seconds=ai.get('seconds'),
                               read_from=sorted({page['read_from'] for page in provenance.get('pages', [])}),
                               classified_as=public['document_type'].get('value'),
                               ai_outcomes=ai.get('outcomes'), ai_error=ai.get('error'))
                    for key, count in (ai.get('outcomes') or {}).items():
                        outcomes[key] += count
                    tokens = (ai.get('prompt_tokens') or 0) + (ai.get('completion_tokens') or 0)
                    row.update(tokens=tokens or None, prompt_tokens=ai.get('prompt_tokens'),
                               completion_tokens=ai.get('completion_tokens'), model_attempts=ai.get('attempts'))
                    tokens_used += tokens
                    if ai.get('model'):
                        models[f"{ai.get('provider')}/{ai.get('model')}"] += 1
                    for issue in public.get('issues', []):
                        if issue.get('code') == 'AI_VALUE_DISCARDED':
                            discards[f"{issue.get('field')}:{issue.get('reason')}"] += 1
                    if ai.get('error'):
                        ai_errors[ai['error']] += 1
                        if ai['error'] in FATAL_AI_ERRORS:
                            stopped = 'PROVIDER_ERROR ' + ai['error']
                    for key in unmapped_keys(public, kind):
                        unmapped[kind].add(key)
                    view = prediction_view_from_public_result(sample, public)
                    evaluation = evaluate_document_fields(sample, view)
                    # Gold and predicted values of the fields that were not right (labels only, no OCR text).
                    predicted = {cell.field_key: cell.value for cell in view.fields}
                    gold = {label.field_key: label.normalized_value for label in sample.fields}
                    row['errors'] = [{'field': o.field_key, 'outcome': o.outcome, 'gold': gold.get(o.field_key),
                                      'predicted': predicted.get(o.field_key)}
                                     for o in evaluation.field_outcomes if o.outcome in {'INCORRECT', 'MISSING'}]
                results[kind].append(evaluation)
                d = evaluation.denominators
                row.update(eligible=d.eligible_present, correct=d.correct, incorrect=d.incorrect, missing=d.missing)
                for outcome in evaluation.field_outcomes:
                    if outcome.outcome not in {'CORRECT', 'INCORRECT', 'MISSING'}:
                        continue
                    cell = fields[f'{kind}.{outcome.field_key}']
                    cell['eligible'] += 1
                    cell[outcome.outcome.lower()] += 1
                    if outcome.outcome != 'CORRECT':
                        cell['not_correct'].append(name)
                documents.append(row)
                print(f"{name:10} {row['status']:18} {row.get('extraction_status') or row.get('error') or '':30} "
                      f"{d.correct}/{d.eligible_present} correct  {row['seconds']:.2f}s", flush=True)

    every = [r for kind in results for r in results[kind]]
    report = {
        'benchmark': 'lite-extraction', 'created_at': datetime.now(timezone.utc).isoformat(),
        'settings': {'AI_EXTRACTION': mode, 'AI_PROVIDERS': os.getenv('AI_PROVIDERS', 'gemini') if mode == 'text' else None,
                     'AI_PREFILL': os.getenv('AI_PREFILL', 'verified'), 'DATE_ORDER': args.date_order,
                     'LITE_OCR_MAX_SIDE': os.getenv('LITE_OCR_MAX_SIDE', '1280'), 'OCR_THREADS': os.getenv('OCR_THREADS', '2'),
                     'types': args.types, 'limit': args.limit,
                     'manifest_dir': str(manifest_dir.relative_to(ROOT)) if manifest_dir.is_relative_to(ROOT) else str(manifest_dir)},
        'ocr_warm_seconds': warm,
        'overall': _counts(summarize_collection(every)),
        'by_type': {kind: {'documents': len(results[kind]), **_counts(summarize_collection(results[kind]))} for kind in results},
        'by_field': {key: {**{k: v for k, v in cell.items() if k != 'not_correct'},
                           'accuracy': _ratio(cell['correct'], cell['eligible']), 'not_correct': cell['not_correct']}
                     for key, cell in sorted(fields.items())},
        'seconds_per_document': {'total': _seconds([r['seconds'] for r in documents]),
                                 'read': _seconds([r['read_seconds'] for r in documents if r.get('read_seconds') is not None]),
                                 'model': _seconds([r['model_seconds'] for r in documents if r.get('model_seconds') is not None])},
        'statuses': dict(statuses), 'ai_value_outcomes': dict(outcomes), 'ai_errors': dict(ai_errors),
        'models': dict(models), 'stopped': stopped, 'tokens_total': tokens_used,
        'tokens_per_document': _seconds([r['tokens'] for r in documents if r.get('tokens')]),
        'discarded_by_field_reason': dict(discards.most_common()),
        'unmapped_source_keys': {kind: sorted(keys) for kind, keys in unmapped.items()},
        'documents': documents,
    }
    output = Path(args.output) if args.output else ROOT / 'runs' / (
        'lite-benchmark-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    o = report['overall']
    print(f"\nAI_EXTRACTION={mode}  documents={len(documents)}  accuracy={o['accuracy']}  coverage={o['coverage']}  "
          f"({o['correct']}/{o['eligible']} correct, {o['incorrect']} incorrect, {o['missing']} missing)")
    for kind, cell in report['by_type'].items():
        print(f"  {kind:8} docs={cell['documents']:2}  accuracy={cell['accuracy']}  coverage={cell['coverage']}")
    print('seconds per document:', report['seconds_per_document'])
    print('models:', report['models'], ' ai errors:', report['ai_errors'], ' stopped:', stopped)
    print('values filled/candidate/discarded/missing:', report['ai_value_outcomes'],
          ' tokens per document:', report['tokens_per_document'], ' total tokens:', tokens_used)
    print('unmapped source keys:', report['unmapped_source_keys'])
    print('report:', output)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--types', nargs='+', choices=list(MANIFESTS), default=list(MANIFESTS))
    parser.add_argument('--limit', type=int, default=0, help='samples per type (0 = all)')
    parser.add_argument('--date-order', choices=['DMY', 'MDY'], default=os.getenv('DATE_ORDER', 'DMY') or 'DMY',
                        help='sets DATE_ORDER; the invoice set is US-style (MDY)')
    parser.add_argument('--ai', choices=['off', 'text'], help='overrides AI_EXTRACTION for this run')
    parser.add_argument('--manifest', help='folder with invoices.json, bills.json, receipts.json and the files they '
                                           'reference, e.g. ai/synthetic/output/tuning (default: the public set)')
    parser.add_argument('--output', help='JSON report path (default runs/lite-benchmark-<UTC>.json)')
    parser.add_argument('--env-file', help='read model-provider variables (AI_*, provider keys) from this file; '
                                           'values are never printed and command-line variables win')
    parser.add_argument('--pace', type=float, default=0,
                        help='minimum seconds between document starts, for per-minute provider limits')
    parser.add_argument('--token-budget', type=int, default=0,
                        help='stop before a document that could take the run past this many tokens (0 = no limit)')
    run(parser.parse_args(argv))


if __name__ == '__main__':
    main()
