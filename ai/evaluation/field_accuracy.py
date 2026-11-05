"""Offline field-level comparison for frozen predictions and labelled samples.

This module does not read mutable accountant current values. The benchmark
preview endpoint adapts immutable extraction results to this evaluator; ordinary
uploads without matching gold labels receive validation results, not accuracy.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Iterable

from ai.evaluation.artifact_contracts import Denominators, FieldOutcome, FrozenPrediction
from ai.evaluation.dataset_contract import GoldLabel, Sample
from ai.evaluation.field_dictionary import ALIASES, FIELDS, allowed_keys


@dataclass(frozen=True)
class FieldAccuracyResult:
    """Document-level outcomes and denominators for one frozen prediction."""

    sample_id: str
    field_outcomes: tuple[FieldOutcome, ...]
    denominators: Denominators

    @property
    def accuracy(self) -> str | None:
        """Exact-match accuracy over eligible PRESENT fields; None if none qualify."""
        if not self.denominators.eligible_present:
            return None
        return _ratio(self.denominators.correct, self.denominators.eligible_present)

    @property
    def extraction_coverage(self) -> str | None:
        """Resolved eligible PRESENT predictions / eligible PRESENT references."""
        eligible = self.denominators.eligible_present
        if not eligible:
            return None
        resolved = self.denominators.correct + self.denominators.incorrect
        return _ratio(resolved, eligible)


@dataclass(frozen=True)
class FieldAccuracySummary:
    """Aggregate field counts and ratios for a collection of evaluated samples."""

    denominators: Denominators

    @property
    def accuracy(self) -> str | None:
        eligible = self.denominators.eligible_present
        return _ratio(self.denominators.correct, eligible) if eligible else None

    @property
    def extraction_coverage(self) -> str | None:
        eligible = self.denominators.eligible_present
        resolved = self.denominators.correct + self.denominators.incorrect
        return _ratio(resolved, eligible) if eligible else None


def evaluate_document_fields(sample: Sample, prediction: FrozenPrediction | None) -> FieldAccuracyResult:
    """Compare one frozen prediction against a sample's document-level gold fields.

    Unlabelled predictions are excluded instead of treated as false positives.
    Ambiguous, unreadable and explicitly unverified labels are also excluded.
    Line items and repeated groups are intentionally outside this first evaluator.
    """
    if prediction is not None and sample.sample_id != prediction.sample_id:
        raise ValueError('Prediction and gold sample IDs differ')
    if prediction is not None and sample.source.sha256 != prediction.source_sha256:
        raise ValueError('Prediction source hash differs from the gold sample')

    predicted = {cell.field_key: cell for cell in prediction.fields} if prediction else {}
    outcomes: list[FieldOutcome] = []

    for label in sample.fields:
        cell = predicted.pop(label.field_key, None)
        outcomes.append(_compare(sample.sample_id, label, cell))

    # The annotation may cover only a subset of dictionary fields. A prediction
    # without a corresponding gold label has no truth value and is not penalized.
    for field_key in predicted:
        outcomes.append(FieldOutcome(
            sample_id=sample.sample_id,
            field_key=field_key,
            gold_row_id=None,
            prediction_row_id=None,
            group_id=None,
            outcome='EXCLUDED',
            exclusion_reason='UNLABELLED',
        ))

    counts = {
        'eligible_present': 0,
        'correct': 0,
        'incorrect': 0,
        'missing': 0,
        'eligible_absent': 0,
        'correct_absent': 0,
        'false_fills': 0,
        'excluded': 0,
        'evidence_available': 0,
    }
    for outcome in outcomes:
        if outcome.outcome == 'EXCLUDED':
            counts['excluded'] += 1
            continue
        if outcome.outcome in {'CORRECT', 'INCORRECT', 'MISSING'}:
            counts['eligible_present'] += 1
            counts[outcome.outcome.lower()] += 1
        elif outcome.outcome in {'CORRECT_ABSENT', 'FALSE_FILL'}:
            counts['eligible_absent'] += 1
            counts['correct_absent' if outcome.outcome == 'CORRECT_ABSENT' else 'false_fills'] += 1
        # Only prediction evidence is counted as available extraction evidence.
        cell = next((c for c in prediction.fields if c.field_key == outcome.field_key), None) if prediction else None
        if cell is not None and cell.evidence:
            counts['evidence_available'] += 1

    return FieldAccuracyResult(
        sample_id=sample.sample_id,
        field_outcomes=tuple(outcomes),
        denominators=Denominators(**counts),
    )


def prediction_view_from_public_result(sample: Sample, public_result: dict) -> SimpleNamespace:
    """Adapt an immutable M2 public result to dataset keys for benchmark scoring.

    The database field_predictions table uses legacy short keys. The evaluator
    reads raw_extractions.raw_json instead, so reviewer corrections in
    current_value can never change benchmark scores.
    """
    if not isinstance(public_result, dict) or not isinstance(public_result.get('fields'), dict):
        raise ValueError('Extraction result has no field map')
    allowed = allowed_keys(sample.document_type, 'document')
    cells = []
    seen = set()
    for source_key, raw_cell in public_result['fields'].items():
        field_key = _canonical_field_key(source_key, sample.document_type)
        if field_key not in allowed or not isinstance(raw_cell, dict):
            continue
        if field_key in seen:
            raise ValueError('Extraction result contains conflicting aliases for ' + field_key)
        state = raw_cell.get('state')
        if state not in {'resolved', 'missing', 'ambiguous', 'conflict', 'unsupported', 'unreadable'}:
            continue
        value = raw_cell.get('value') if state == 'resolved' else None
        if FIELDS[field_key]['value_type'] == 'boolean' and state == 'resolved':
            if value is True or value == 'true':
                value = True
            elif value is False or value == 'false':
                value = False
            else:
                # Do not coerce an unrecognized boolean string into a score.
                continue
        if state == 'resolved' and value is None:
            continue
        evidence = raw_cell.get('evidence')
        cells.append(SimpleNamespace(
            field_key=field_key,
            state=state,
            value=value,
            evidence=evidence if isinstance(evidence, list) else [],
        ))
        seen.add(field_key)
    provenance = public_result.get('provenance') or {}
    return SimpleNamespace(
        sample_id=sample.sample_id,
        source_sha256=sample.source.sha256,
        extraction_run_id=provenance.get('service_run_id'),
        fields=cells,
    )


def evaluate_document_collection(
    samples: Iterable[Sample], predictions: Iterable[FrozenPrediction]
) -> list[FieldAccuracyResult]:
    """Evaluate matching sample/prediction artifacts, rejecting missing or duplicate IDs."""
    sample_list = list(samples)
    prediction_list = list(predictions)
    sample_by_id = {sample.sample_id: sample for sample in sample_list}
    prediction_by_id = {prediction.sample_id: prediction for prediction in prediction_list}
    if len(sample_by_id) == 0:
        raise ValueError('No gold samples supplied')
    if len(sample_by_id) != len(sample_list):
        raise ValueError('Duplicate gold sample ID')
    if len(prediction_by_id) != len(prediction_list):
        raise ValueError('Duplicate prediction sample ID')
    unknown = set(prediction_by_id) - set(sample_by_id)
    if unknown:
        raise ValueError('Prediction has no matching gold sample: ' + sorted(unknown)[0])
    return [
        evaluate_document_fields(sample, prediction_by_id.get(sample_id))
        for sample_id, sample in sample_by_id.items()
    ]


def summarize_collection(results: Iterable[FieldAccuracyResult]) -> FieldAccuracySummary:
    """Sum validated field counts without mixing excluded labels into accuracy."""
    result_list = list(results)
    count_fields = Denominators.model_fields.keys()
    totals = {
        field: sum(getattr(result.denominators, field) for result in result_list)
        for field in count_fields
    }
    return FieldAccuracySummary(denominators=Denominators(**totals))


def _compare(sample_id: str, label: GoldLabel, cell) -> FieldOutcome:
    state = label.label_state
    if state == 'UNLABELLED':
        return _excluded(sample_id, label.field_key, 'UNLABELLED')
    if state == 'AMBIGUOUS':
        return _excluded(sample_id, label.field_key, 'AMBIGUOUS')
    if state == 'UNREADABLE':
        return _excluded(sample_id, label.field_key, 'UNREADABLE')
    if label.annotation.verification_state == 'UNVERIFIED':
        return _excluded(sample_id, label.field_key, 'UNVERIFIED')

    resolved = cell is not None and cell.state == 'resolved'
    if state == 'CONFIRMED_ABSENT':
        outcome = 'FALSE_FILL' if resolved else 'CORRECT_ABSENT'
    elif not resolved:
        outcome = 'MISSING'
    else:
        outcome = 'CORRECT' if _same_value(label.value_type, label.normalized_value, cell.value) else 'INCORRECT'

    return FieldOutcome(
        sample_id=sample_id,
        field_key=label.field_key,
        gold_row_id=None,
        prediction_row_id=None,
        group_id=None,
        outcome=outcome,
        exclusion_reason=None,
    )


def _excluded(sample_id: str, field_key: str, reason: str) -> FieldOutcome:
    return FieldOutcome(
        sample_id=sample_id,
        field_key=field_key,
        gold_row_id=None,
        prediction_row_id=None,
        group_id=None,
        outcome='EXCLUDED',
        exclusion_reason=reason,
    )


def _same_value(value_type: str, expected, actual) -> bool:
    if value_type == 'decimal':
        return Decimal(expected) == Decimal(actual)
    if value_type == 'boolean':
        return type(expected) is bool and type(actual) is bool and expected is actual
    # Text, date, time and currency use exact normalized-value comparison. No
    # case-folding, date coercion, or monetary tolerance is assumed here.
    return expected == actual


def _canonical_field_key(source_key: str, kind: str) -> str:
    """Apply only explicit, context-specific aliases in the shared dictionary."""
    allowed = allowed_keys(kind, 'document')
    if source_key in allowed:
        return source_key
    for alias in ALIASES:
        if alias['status'] == 'adapter_required':
            continue
        sources = alias['production'].split(' / ')
        if source_key not in sources:
            continue
        targets = alias['evaluation'].get(kind)
        if targets is None:
            continue
        if isinstance(targets, list):
            position = sources.index(source_key)
            if position >= len(targets):
                continue
            target = targets[position]
        else:
            target = targets
        if isinstance(target, str) and target in allowed:
            return target
    return source_key


def _ratio(numerator: int, denominator: int) -> str:
    # Avoid binary floating-point in reported ratios.
    return str((Decimal(numerator) / Decimal(denominator)).quantize(Decimal('0.0001')))
