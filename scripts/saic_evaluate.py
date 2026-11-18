"""Reproducible SAIC measurement on the labelled public benchmark. No database, Redis, storage or tenant data.

usage:
  python scripts/saic_evaluate.py run   [--qwen off|live] [--out runs/saic-eval-<stamp>]
  python scripts/saic_evaluate.py score RUN_DIR [--write-summary ../Frontend/public/evaluation/summary.json]
  python scripts/saic_evaluate.py study-snapshot RUN_DIR [--out ai/evaluation/effort_study_prefill.json]

`run` sends the 35 labelled public documents (ai/assets) through the production extraction path
(execute_document_extraction) with Qwen off, or with live selective recovery and risk-based second
opinions when --qwen live is given and a key is already in the process environment (never read here).
`score` uses the unchanged field evaluator: strict and address-normalised accuracy, the original and
printed-role-confirmed bill-number policies, latency, first-pass exceptions and Qwen candidate outcomes.
A criterion without a measurement is reported as NOT MEASURED, never estimated.
`study-snapshot` copies that run's stored extraction of the four manual-effort study files (values,
source and result hashes, licences; no paths) so a hosted backend can serve the study without runs/.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
KINDS = ('invoice', 'bill', 'receipt')
PRICE_IN, PRICE_OUT = Decimal('0.4'), Decimal('1.6')  # USD per 1M tokens, Singapore list price, input <=256k
EXTRACTION_CAUSES = {'MISSING_SOURCE_VALUE', 'CONFLICTING_VALUES', 'INVALID_FORMAT', 'CLASSIFICATION_MISMATCH'}


def _environment(qwen):
    from dotenv import dotenv_values
    for key, value in dotenv_values(ROOT / '.env.localhost').items():
        if value is not None and (key.startswith(('DOCLING_', 'RAPIDOCR_', 'IMAGE_OCR_')) or key in {'ENABLE_IMAGE_OCR', 'ENABLE_EXTRACTION_GRAPH'}):
            os.environ.setdefault(key, value)
    os.environ.update(PYTHON_DOTENV_DISABLED='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    for key in ('DATABASE_URL', 'REDIS_URL', 'TEST_DATABASE_URL', 'AWS_SECRET_ACCESS_KEY', 'BREVO_API_KEY'):
        os.environ.pop(key, None)
    if qwen == 'live':
        for key, value in dotenv_values(ROOT / '.env.qwen.local').items():
            if key.startswith('QWEN_') and 'KEY' not in key and value is not None:
                os.environ[key] = value
        if not (os.environ.get('QWEN_CLOUD_API_KEY') or os.environ.get('DASHSCOPE_API_KEY')):
            sys.exit('--qwen live needs the key already in the process environment (use the DPAPI wrapper).')
        os.environ.update(QWEN_RECOVERY_MODE='auto', QWEN_SECOND_OPINION_MODE='risk')
    else:
        os.environ.update(QWEN_RECOVERY_MODE='off', QWEN_PROVIDER='disabled', QWEN_SECOND_OPINION_MODE='off')


def run(args):
    _environment(args.qwen)
    from services.extraction.ai import cloud_qwen_provider as cqp
    from services.extraction.parsing.baseline_parser import warm_converter
    from services.extraction.workflow.execution import execute_document_extraction
    calls = []
    real_chat = cqp.AlibabaCloudQwenProvider.chat

    def counted_chat(self, payload):
        entry = {}
        calls.append(entry)
        try:
            return real_chat(self, payload)
        finally:
            entry.update(self.last_call or {})
    cqp.AlibabaCloudQwenProvider.chat = counted_chat

    out = Path(args.out or ROOT / 'runs' / ('saic-eval-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))).resolve()
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    warm = round(warm_converter(), 2)
    meta = {'started_at': datetime.now(timezone.utc).isoformat(), 'qwen': args.qwen, 'cold_start_warmup_seconds': warm, 'run_seconds': None}
    files = [p for kind in KINDS for p in sorted((ROOT / 'ai' / 'assets' / kind).glob('*.*'))]
    for source in files:
        work = out / source.stem
        work.mkdir(exist_ok=True)
        target = work / source.name
        shutil.copyfile(source, target)
        before = len(calls)
        t0 = time.perf_counter()
        result = execute_document_extraction(f'saic-{source.stem}', target, project_root=work,
                                             allow_vlm_inference=args.qwen == 'live', source_access_authorized=args.qwen == 'live')
        row = {'file': source.name, 'seconds': round(time.perf_counter() - t0, 3), 'status': result.get('execution_status'),
               'public_result_path': result.get('public_result_path'), 'calls': calls[before:]}
        service = next(work.rglob('service-result.json'), None)
        if service:
            s = json.loads(service.read_text(encoding='utf-8'))
            row['stages'] = {t['stage']: t['seconds'] for t in s.get('stage_trace', [])}
            row['reconciliation'] = {k: {'outcome': f.get('outcome'), 'candidate': (f.get('vlm') or {}).get('candidate'),
                                         'source_check': ((f.get('vlm') or {}).get('source_check') or {}).get('status')}
                                     for k, f in ((s.get('reconciliation') or {}).get('fields') or {}).items()}
        with open(out / 'summary.jsonl', 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, default=str) + '\n')
        print(json.dumps({k: row.get(k) for k in ('file', 'seconds', 'status')} | {'calls': len(row['calls'])}), flush=True)
    meta['run_seconds'] = round(time.perf_counter() - started, 1)
    (out / 'meta.json').write_text(json.dumps(meta, indent=2), encoding='utf-8')
    print(out)


def _p95(values):
    values = sorted(values)
    return values[max(0, -(-95 * len(values) // 100) - 1)]


def _score(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator, 'value': (numerator / denominator) if denominator else None}


def score(args):
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    from ai.evaluation.dataset_contract import load_manifest
    from ai.evaluation.field_accuracy import (ORIGINAL_ROLE_POLICY, ROLE_POLICY, _canonical_field_key, evaluate_document_fields,
                                              prediction_view_from_public_result, summarize_collection)
    from services.extraction.contracts.accounting_contract import map_m2_result
    from validation.documents.document_validation import evaluate_document
    run_dir = Path(args.run_dir).resolve()
    rows = [json.loads(line) for line in open(run_dir / 'summary.jsonl', encoding='utf-8')]
    meta = json.loads((run_dir / 'meta.json').read_text(encoding='utf-8')) if (run_dir / 'meta.json').is_file() else {}
    samples = {Path(s.source.relative_path).name: s for kind in ('invoices', 'bills', 'receipts')
               for s in load_manifest(ROOT / f'ai/manifests/{kind}.json').samples}
    results = {policy: defaultdict(list) for policy in (ORIGINAL_ROLE_POLICY, ROLE_POLICY)}
    field_rows = defaultdict(lambda: {'n': 0, 'correct': 0, 'incorrect': 0, 'missing': 0, 'normalized_ok': 0, 'errors': []})
    classification = Counter(); class_errors = []
    exception_docs = 0; causes = Counter(); extraction_docs = 0
    qwen = defaultdict(Counter)
    disagreements = []
    for row in rows:
        sample = samples.get(row['file'])
        if not sample or not row.get('public_result_path'):
            continue
        public = json.loads(Path(row['public_result_path']).read_text(encoding='utf-8'))
        kind = public['document_type'].get('value')
        classification['correct' if kind == sample.document_type else 'wrong'] += 1
        if kind != sample.document_type:
            class_errors.append(f"{Path(sample.source.relative_path).stem}: labelled {sample.document_type}, classified {kind or 'unknown'}.")
        for policy in results:
            res = evaluate_document_fields(sample, prediction_view_from_public_result(sample, public, role_policy=policy))
            results[policy][sample.document_type].append(res)
            if policy != ROLE_POLICY:
                continue
            for outcome in res.field_outcomes:
                if outcome.outcome not in {'CORRECT', 'INCORRECT', 'MISSING'}:
                    continue
                cell = field_rows[(sample.document_type, outcome.field_key)]
                cell['n'] += 1
                cell[outcome.outcome.lower()] += 1
                if outcome.outcome == 'INCORRECT' and outcome.field_key in res.address_normalized_fields:
                    cell['normalized_ok'] += 1
                if outcome.outcome != 'CORRECT':
                    cell['errors'].append(Path(sample.source.relative_path).stem)
        mapped = map_m2_result(public, public['document_id'])
        doc_kind = mapped['document_kind'].get('value') or sample.document_type
        errors = evaluate_document({'kind': doc_kind, 'category': 'accounts_payable'}, {'raw_json': public},
                                   mapped['fields'], mapped['line_items'])['errors']
        if errors:
            exception_docs += 1
            found = {(e.get('cause') or 'POLICY_REQUIREMENT', e.get('field')) for e in errors}
            if any(cause in EXTRACTION_CAUSES or (cause == 'UNCERTAIN_SOURCE_VALUE' and field != 'currency') for cause, field in found):
                extraction_docs += 1
            for cause, field in found:
                label = 'UNCERTAIN_SOURCE_VALUE (currency symbol only)' if cause == 'UNCERTAIN_SOURCE_VALUE' and field == 'currency' else cause
                causes[label] += 1
        for name, f in (row.get('reconciliation') or {}).items():
            qwen[sample.document_type][f.get('outcome') or 'NONE'] += 1
            if f.get('candidate') is not None:
                one = copy.deepcopy(public)
                one['fields'][name] = {**one['fields'].get(name, {}), 'state': 'resolved', 'value': f['candidate']}
                check = evaluate_document_fields(sample, prediction_view_from_public_result(sample, one))
                key = _canonical_field_key(name, sample.document_type)
                hit = next((o for o in check.field_outcomes if o.field_key == key), None)
                verdict = 'unlabelled' if hit is None or hit.outcome == 'EXCLUDED' else (
                    'agree' if hit.outcome == 'CORRECT' or key in check.address_normalized_fields else 'disagree')
                qwen[sample.document_type]['label_' + verdict] += 1
                if f.get('outcome') == 'PARSER_MODEL_CONFLICT':
                    disagreements.append({'document': Path(sample.source.relative_path).stem, 'field': name,
                                          'parser': (public['fields'].get(name) or {}).get('value'), 'model': f['candidate'],
                                          'sourceCheck': f.get('source_check'),
                                          'shownToReviewer': f.get('source_check') not in {'not_found_in_source', 'date_order_not_verifiable', 'placeholder_value'},
                                          'labelSaysModel': verdict})

    def type_rows(policy):
        out = []
        for kind in KINDS:
            summary = summarize_collection(results[policy][kind])
            d, n = summary.denominators, summary.address_normalized_denominators
            out.append({'type': kind.title(), 'documents': len(results[policy][kind]),
                        'exact': _score(d.correct, d.eligible_present), 'normalized': _score(d.correct, d.eligible_present),
                        'tolerant': _score(n.correct, n.eligible_present),
                        'correct': d.correct, 'incorrect': d.incorrect, 'missing': d.missing})
        return out
    by_type, original = type_rows(ROLE_POLICY), type_rows(ORIGINAL_ROLE_POLICY)
    invoice = by_type[0]
    secs = [r['seconds'] for r in rows]
    calls = [c for r in rows for c in r.get('calls', [])]
    tin = sum(c.get('prompt_tokens') or 0 for c in calls); tout = sum(c.get('completion_tokens') or 0 for c in calls)
    total = len([r for r in rows if samples.get(r['file'])])
    within = sum(s <= 20 for s in secs)
    pct = lambda s: f"{s['value'] * 100:.1f}% ({s['numerator']}/{s['denominator']})"
    qwen_note = (f"{len(calls)} live Qwen calls ({tin} input / {tout} output tokens, about USD "
                 f"{(Decimal(tin) * PRICE_IN + Decimal(tout) * PRICE_OUT) / Decimal(10**6):.4f}); suggestions are review-only."
                 if calls else 'Qwen was off for this run; parser-only results.')
    criteria = [
        {'name': 'SC1 Invoice field accuracy', 'target': 'At least 95%',
         'result': f"Strict {pct(invoice['exact'])}; address-normalised {pct(invoice['tolerant'])}",
         'status': 'MET' if invoice['exact']['value'] and invoice['exact']['value'] >= .95 else 'NOT MET',
         'note': 'Strict is the official figure. Address-normalised treats commas and line breaks between address parts as one separator, nothing else. '
                 '15 synthetic invoices used during development, so not an independent held-out result.'},
        {'name': 'SC2 Manual-effort reduction', 'target': 'At least 70%', 'result': 'Not measured', 'status': 'NOT MEASURED',
         'note': 'No timed manual-only and AI-assisted review sessions exist, so no reduction is claimed.'},
        {'name': 'SC3 Processing time per document', 'target': '20 s or less',
         'result': f"Mean {statistics.mean(secs):.1f} s, median {statistics.median(secs):.1f} s, P95 {_p95(secs):.1f} s, max {max(secs):.1f} s; {within}/{len(secs)} within 20 s",
         'status': 'MET' if within == len(secs) else 'NOT MET',
         'note': f"Warm worker, one document at a time on a CPU laptop; the cold model start ({meta.get('cold_start_warmup_seconds')} s) is paid once at worker start. "
                 'Queue wait is not included. ' + qwen_note},
        {'name': 'SC4 First-pass exception rate', 'target': 'Below 10%',
         'result': f"{exception_docs} of {total} documents ({exception_docs / total * 100:.0f}%) had a failed rule at first validation; "
                   f"{extraction_docs} of them involve an extraction-related cause",
         'status': 'MET' if exception_docs / total < .10 else 'NOT MET',
         'note': 'Offline validation without the database: supplier-list (EX-008) and duplicate checks do not run here, so the live rate is higher. '
                 'A bare "$" is kept for a person to confirm, which is correct. No exception was suppressed.'},
    ]
    fields = [{'type': kind.title(), 'field': key, 'n': cell['n'], 'exact': _score(cell['correct'], cell['n']),
               'normalized': _score(cell['correct'], cell['n']), 'tolerant': _score(cell['correct'] + cell['normalized_ok'], cell['n']),
               'correct': cell['correct'], 'incorrect': cell['incorrect'], 'missing': cell['missing'], 'errors': sorted(set(cell['errors']))}
              for (kind, key), cell in sorted(field_rows.items())]
    summary = {
        'generated': datetime.now().strftime('%d %b %Y %H:%M'),
        'dataset': [
            f"Benchmark module2-third-party-pilot-v1: {total} public documents (15 invoices, 10 bills, 10 receipts), published labels unchanged.",
            'Invoices and bills are synthetic; receipts are real ICDAR2019 SROIE scans. All were used during development, so this is not held-out evidence.',
            'Suspected label errors are left unchanged and count against us: REC00008 labels TED HENG for printed TEO HENG; REC00001 labels B1750 for '
            'postcode 81750; REC00002 labels capitals for a mixed-case merchant name.',
            "Bill numbers: the parser stores a printed 'INVOICE NO.' as bill_reference. The main table scores it as bill_number only when the printed "
            "label confirms that role; 'Earlier baseline' shows the original alias-only scoring. No label was changed.",
            "'Normalized' equals strict here; 'Formatting-tolerant' is the address-normalised figure.",
            f"Run {run_dir.name}; Qwen mode: {meta.get('qwen', 'off')}. Reproduce with scripts/saic_evaluate.py run, then score.",
        ],
        'criteria': criteria,
        'classification': {**_score(classification['correct'], total), 'errors': class_errors},
        'byType': by_type, 'baseline': original, 'lineItems': [], 'fields': fields,
        'notEvaluated': [{'field': 'Line items', 'reason': 'Line-item labels are not scored by this evaluator yet.'},
                         {'field': 'Manual effort', 'reason': 'Needs timed manual-only and AI-assisted review sessions.'}],
        'qwen': [{'type': kind.title(), 'documents': sum(1 for r in rows if samples.get(r['file']) and samples[r['file']].document_type == kind and r.get('calls')),
                  'requested': sum(v for k, v in qwen[kind].items() if not k.startswith('label_')),
                  'verified': qwen[kind]['SOURCE_VERIFIED_CANDIDATE'], 'unverified': qwen[kind]['UNVERIFIED_MODEL_SUGGESTION'],
                  'rejected': qwen[kind]['NO_RECOVERY'], 'labelledCorrect': qwen[kind]['label_agree'],
                  'labelledDisagree': qwen[kind]['label_disagree'], 'unlabelled': qwen[kind]['label_unlabelled']} for kind in KINDS] if calls else [],
        'latency': [{'measure': 'End-to-end extraction per document', 'n': len(secs), 'median': round(statistics.median(secs), 2),
                     'mean': round(statistics.mean(secs), 2), 'p95': round(_p95(secs), 2), 'max': round(max(secs), 2),
                     'note': f'{within}/{len(secs)} within 20 s. Warm worker; CPU laptop; queue wait not included.'},
                    {'measure': 'Cold model start (once per worker start)', 'n': 1, 'median': meta.get('cold_start_warmup_seconds'),
                     'mean': meta.get('cold_start_warmup_seconds'), 'p95': meta.get('cold_start_warmup_seconds'), 'max': meta.get('cold_start_warmup_seconds'),
                     'note': 'Paid at worker start-up by the warm-up, not by the first upload.'}],
        'exceptions': {'documents': exception_docs, 'total': total, 'reasons': [{'reason': k, 'count': v} for k, v in causes.most_common()]},
        'corrections': [],
        'qwenDisagreements': disagreements,
        'liveRun': {'statement': 'These figures come from a scripted offline run of the real extraction pipeline on public benchmark files, '
                                 'not from documents uploaded to this workspace.'},
    }
    if getattr(args, 'parser_run', None):
        base_rows = [json.loads(line) for line in open(Path(args.parser_run) / 'summary.jsonl', encoding='utf-8')]
        base = [r['seconds'] for r in base_rows]
        summary['latency'].insert(1, {'measure': 'Same files, parser only (Qwen off)', 'n': len(base), 'median': round(statistics.median(base), 2),
            'mean': round(statistics.mean(base), 2), 'p95': round(_p95(base), 2), 'max': round(max(base), 2),
            'note': f"{sum(s <= 20 for s in base)}/{len(base)} within 20 s. Separate run ({Path(args.parser_run).name}); machine load differs between runs."})
    (run_dir / 'saic-summary.json').write_text(json.dumps(summary, indent=1), encoding='utf-8')
    if args.write_summary:
        Path(args.write_summary).write_text(json.dumps(summary, indent=1), encoding='utf-8')
    print(json.dumps({'criteria': [{k: c[k] for k in ('name', 'result', 'status')} for c in criteria],
                      'bill_original': original[1]['exact'], 'bill_corrected': by_type[1]['exact']}, indent=1))


def study_snapshot(args):
    from ai.evaluation.effort_study_inputs import SNAPSHOT, build_snapshot
    out = Path(args.out) if args.out else SNAPSHOT
    out.write_text(json.dumps(build_snapshot(Path(args.run_dir), ROOT), indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'Wrote {out}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    r = sub.add_parser('run'); r.add_argument('--qwen', choices=('off', 'live'), default='off'); r.add_argument('--out')
    s = sub.add_parser('score'); s.add_argument('run_dir'); s.add_argument('--write-summary')
    s.add_argument('--parser-run', help='A Qwen-off run of the same files, to report parser-only latency beside it')
    t = sub.add_parser('study-snapshot'); t.add_argument('run_dir'); t.add_argument('--out')
    args = parser.parse_args()
    {'run': run, 'score': score, 'study-snapshot': study_snapshot}[args.command](args)


if __name__ == '__main__':
    main()
