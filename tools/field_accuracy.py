"""Calculate reproducible field-level extraction metrics.

Input JSON is a list of objects with ``expected`` and ``predicted`` mappings.
Values are compared after whitespace/case normalization; numeric strings are
compared to two decimal places. Missing predictions count as errors.
"""
import json
import sys
import statistics
from datetime import datetime
from decimal import Decimal, InvalidOperation


def norm(value):
    if value is None or str(value).strip() == "":
        return None
    text = " ".join(str(value).strip().lower().split())
    try:
        return f"{Decimal(text.replace(',', '')).quantize(Decimal('0.01'))}"
    except (InvalidOperation, ValueError):
        return text


def calculate(rows):
    fields = sorted({key for row in rows for key in set(row.get('expected', {})) | set(row.get('predicted', {}))})
    result = {"documents": len(rows), "fields": {}, "macro_exact_match": 0.0}
    scores = []
    for field in fields:
        tp = fp = fn = 0
        for row in rows:
            expected, predicted = norm(row.get('expected', {}).get(field)), norm(row.get('predicted', {}).get(field))
            if expected is not None and predicted == expected: tp += 1
            elif expected is not None and predicted is None: fn += 1
            elif predicted is not None: fp += 1
            elif expected is not None: fn += 1
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        exact = tp / len(rows) if rows else 0.0
        result['fields'][field] = {"exact_match": round(exact, 4), "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4), "tp": tp, "fp": fp, "fn": fn}
        scores.append(exact)
    result['macro_exact_match'] = round(sum(scores) / len(scores), 4) if scores else 0.0
    critical = {'supplier_name', 'invoice_number', 'invoice_date', 'date', 'currency', 'total_amount', 'total'}
    critical_scores = [v['exact_match'] for k, v in result['fields'].items() if k in critical]
    result['critical_field_accuracy'] = round(sum(critical_scores) / len(critical_scores), 4) if critical_scores else 0.0
    result['classification_accuracy'] = _accuracy(rows, 'document_type')
    result['line_item_accuracy'] = _line_accuracy(rows)
    result['duplicate_metrics'] = _binary_metrics(rows, 'duplicate')
    result['arithmetic_validation_correctness'] = _accuracy(rows, 'arithmetic_valid')
    durations = sorted(float(r['processing_seconds']) for r in rows if r.get('processing_seconds') is not None)
    result['processing_seconds'] = {'p50': _percentile(durations, .50), 'p95': _percentile(durations, .95)}
    result['human_correction_rate'] = round(sum(bool(r.get('corrected_fields')) for r in rows) / len(rows), 4) if rows else 0.0
    result['manual_workload_reduction'] = round(sum(float(r.get('baseline_manual_seconds', 0)) - float(r.get('manual_seconds', 0)) for r in rows) /
                                                sum(float(r.get('baseline_manual_seconds', 0)) for r in rows), 4) if rows and sum(float(r.get('baseline_manual_seconds', 0)) for r in rows) else None
    return result

def _accuracy(rows, key):
    applicable = [r for r in rows if r.get('expected', {}).get(key) is not None or r.get('predicted', {}).get(key) is not None]
    return round(sum(norm(r.get('expected', {}).get(key)) == norm(r.get('predicted', {}).get(key)) for r in applicable) / len(applicable), 4) if applicable else None

def _line_accuracy(rows):
    total = correct = 0
    for row in rows:
        for expected, predicted in zip(row.get('expected_line_items', []), row.get('predicted_line_items', [])):
            for key in set(expected) | set(predicted):
                total += 1; correct += norm(expected.get(key)) == norm(predicted.get(key))
    return round(correct / total, 4) if total else None

def _binary_metrics(rows, key):
    tp = fp = fn = 0
    for row in rows:
        expected, predicted = bool(row.get('expected', {}).get(key)), bool(row.get('predicted', {}).get(key))
        tp += expected and predicted; fp += not expected and predicted; fn += expected and not predicted
    return {'precision': round(tp/(tp+fp), 4) if tp+fp else 0.0, 'recall': round(tp/(tp+fn), 4) if tp+fn else 0.0, 'tp': tp, 'fp': fp, 'fn': fn}

def _percentile(values, q):
    if not values: return None
    return round(values[min(len(values)-1, max(0, int((len(values)-1)*q)))], 3)


if __name__ == '__main__':
    with open(sys.argv[1], encoding='utf-8') as fh:
        print(json.dumps(calculate(json.load(fh)), indent=2))
