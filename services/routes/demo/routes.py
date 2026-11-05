from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from main import *
from ai.evaluation.dataset_contract import load_manifest
from ai.evaluation.field_accuracy import (
    evaluate_document_fields,
    prediction_view_from_public_result,
    summarize_collection,
)


@lru_cache(maxsize=1)
def _benchmark_samples():
    root = Path(__file__).resolve().parents[3]
    samples = {}
    for name in ('invoices.json', 'bills.json', 'receipts.json'):
        manifest = load_manifest(root / 'ai' / 'manifests' / name)
        samples.update({sample.sample_id: sample for sample in manifest.samples})
    return samples


def _metric(numerator: int, denominator: int):
    return round(numerator / denominator, 4) if denominator else None

def _demo_user(request: Request):
    return require_workspace_user(request, {"accountant", "local_admin"})

def demo_catalog(dataset_id: str, request: Request):
    user = _demo_user(request)
    with connect() as db:
        rows = db.execute("""SELECT b.dataset_id,b.benchmark_revision,b.demo_code,b.sample_id,
                   b.source_sha256,b.gold_manifest_sha256,d.id,d.document_code,d.original_filename,
                   d.kind,d.category,d.status,d.uploaded_at
            FROM benchmark_demo_documents b
            JOIN benchmark_demo_tenants t ON t.tenant_id=b.tenant_id AND t.enabled=true
            JOIN documents d ON d.id=b.document_id AND d.tenant_id=b.tenant_id
            WHERE b.tenant_id=%s AND b.dataset_id=%s
            ORDER BY b.demo_code""", (user["tenant_id"], dataset_id)).fetchall()
    return {"datasetId": dataset_id, "documents": [{
        "demoCode": r["demo_code"], "sampleId": r["sample_id"], "benchmarkRevision": r["benchmark_revision"],
        "documentId": str(r["id"]), "documentCode": r["document_code"], "filename": r["original_filename"],
        "kind": r["kind"].upper(), "category": r["category"].upper(), "status": r["status"].upper(),
        "sourceSha256": r["source_sha256"], "goldManifestSha256": r["gold_manifest_sha256"],
        "uploadedAt": str(r["uploaded_at"]),
    } for r in rows]}

def demo_accuracy_preview(dataset_id: str, request: Request):
    user = _demo_user(request)
    with connect() as db:
        rows = db.execute("""SELECT b.sample_id,b.benchmark_revision,b.source_sha256,
                   d.id,d.file_hash_sha256,re.id AS extraction_id,re.raw_json
            FROM benchmark_demo_documents b
            JOIN benchmark_demo_tenants t ON t.tenant_id=b.tenant_id AND t.enabled=true
            JOIN documents d ON d.id=b.document_id AND d.tenant_id=b.tenant_id
            LEFT JOIN LATERAL (
                SELECT id,raw_json FROM raw_extractions
                WHERE document_id=d.id ORDER BY run_number DESC LIMIT 1
            ) re ON TRUE
            WHERE b.tenant_id=%s AND b.dataset_id=%s
            ORDER BY b.demo_code""", (user["tenant_id"], dataset_id)).fetchall()

    samples_by_id = _benchmark_samples()
    results = []
    by_kind = {}
    for row in rows:
        sample = samples_by_id.get(row['sample_id'])
        if (sample is None or row['benchmark_revision'] != '1'
                or row['source_sha256'] != sample.source.sha256
                or row['file_hash_sha256'] != sample.source.sha256):
            raise HTTPException(409, 'Registered demo document does not match the pinned benchmark sample.')
        raw = row['raw_json']
        if not isinstance(raw, dict):
            continue
        prediction = prediction_view_from_public_result(sample, raw)
        result = evaluate_document_fields(sample, prediction)
        results.append(result)
        by_kind.setdefault(sample.document_type, []).append(result)

    if not results:
        return {"datasetId": dataset_id, "status": "NOT_RUN", "documentCount": len(rows),
                "evaluatedDocumentCount": 0, "accuracy": None, "recall": None,
                "precision": None, "coverage": None, "breakdown": []}

    summary = summarize_collection(results)
    counts = summary.denominators
    resolved = counts.correct + counts.incorrect
    emitted = resolved + counts.false_fills
    breakdown = []
    for kind, kind_results in sorted(by_kind.items()):
        kind_counts = summarize_collection(kind_results).denominators
        kind_resolved = kind_counts.correct + kind_counts.incorrect
        kind_emitted = kind_resolved + kind_counts.false_fills
        breakdown.append({
            'documentType': kind,
            'documentCount': len(kind_results),
            'eligibleFields': kind_counts.eligible_present,
            'correct': kind_counts.correct,
            'incorrect': kind_counts.incorrect,
            'missing': kind_counts.missing,
            'excluded': kind_counts.excluded,
            'accuracy': _metric(kind_counts.correct, kind_counts.eligible_present),
            'coverage': _metric(kind_resolved, kind_counts.eligible_present),
            'precision': _metric(kind_counts.correct, kind_emitted),
            'recall': _metric(kind_counts.correct, kind_counts.eligible_present),
        })
    per_field = {}
    for result in results:
        for outcome in result.field_outcomes:
            field = per_field.setdefault(outcome.field_key, {
                'eligible': 0, 'correct': 0, 'incorrect': 0, 'missing': 0,
                'eligibleAbsent': 0, 'correctAbsent': 0, 'falseFills': 0, 'excluded': 0,
            })
            if outcome.outcome == 'EXCLUDED':
                field['excluded'] += 1
            elif outcome.outcome in {'CORRECT', 'INCORRECT', 'MISSING'}:
                field['eligible'] += 1
                field[outcome.outcome.lower()] += 1
            elif outcome.outcome == 'CORRECT_ABSENT':
                field['eligibleAbsent'] += 1
                field['correctAbsent'] += 1
            elif outcome.outcome == 'FALSE_FILL':
                field['eligibleAbsent'] += 1
                field['falseFills'] += 1
    per_field = [{
        'fieldKey': field_key,
        **counts_for_field,
        'accuracy': _metric(counts_for_field['correct'], counts_for_field['eligible']),
        'coverage': _metric(counts_for_field['correct'] + counts_for_field['incorrect'],
                            counts_for_field['eligible']),
    } for field_key, counts_for_field in sorted(per_field.items())]
    return {
        'datasetId': dataset_id,
        'status': 'EVALUATED',
        'documentCount': len(rows),
        'evaluatedDocumentCount': len(results),
        'accuracy': _metric(counts.correct, counts.eligible_present),
        'recall': _metric(counts.correct, counts.eligible_present),
        'precision': _metric(counts.correct, emitted),
        'coverage': _metric(resolved, counts.eligible_present),
        'eligibleFields': counts.eligible_present,
        'correct': counts.correct,
        'incorrect': counts.incorrect,
        'missing': counts.missing,
        'eligibleAbsentFields': counts.eligible_absent,
        'falseFills': counts.false_fills,
        'excluded': counts.excluded,
        'breakdown': breakdown,
        'perField': per_field,
    }

def register(router, handlers):
    router.add_api_route('/api/demo-datasets/{dataset_id}/catalog', demo_catalog, methods=['GET'])
    router.add_api_route('/api/demo-datasets/{dataset_id}/accuracy-preview', demo_accuracy_preview, methods=['GET'])
