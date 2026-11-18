"""Offline field-level comparison for frozen predictions and labelled samples.

This module does not read mutable accountant current values. The benchmark
preview endpoint adapts immutable extraction results to this evaluator; ordinary
uploads without matching gold labels receive validation results, not accuracy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Iterable

from ai.evaluation.address_policy import ADDRESS_FIELDS, same_address
from ai.evaluation.artifact_contracts import Denominators, FieldOutcome, FrozenPrediction
from ai.evaluation.dataset_contract import GoldLabel, Sample
from ai.evaluation.field_dictionary import ALIASES, FIELDS, allowed_keys


@dataclass(frozen=True)
class FieldAccuracyResult:
    """Document-level outcomes and denominators for one frozen prediction."""

    sample_id: str
    field_outcomes: tuple[FieldOutcome, ...]
    denominators: Denominators
    # Same fields and denominator; only address separators are compared under ADDRESS_POLICY.
    address_normalized_denominators: Denominators | None = None
    address_normalized_fields: tuple[str, ...] = ()

    @property
    def accuracy(self) -> str | None:
        """Exact-match accuracy over eligible PRESENT fields; None if none qualify."""
        if not self.denominators.eligible_present:
            return None
        return _ratio(self.denominators.correct, self.denominators.eligible_present)

    @property
    def address_normalized_accuracy(self) -> str | None:
        counts = self.address_normalized_denominators
        return _ratio(counts.correct, counts.eligible_present) if counts and counts.eligible_present else None

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
    address_normalized_denominators: Denominators | None = None

    @property
    def accuracy(self) -> str | None:
        eligible = self.denominators.eligible_present
        return _ratio(self.denominators.correct, eligible) if eligible else None

    @property
    def address_normalized_accuracy(self) -> str | None:
        counts = self.address_normalized_denominators
        return _ratio(counts.correct, counts.eligible_present) if counts and counts.eligible_present else None

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
    address_matches: list[str] = []

    for label in sample.fields:
        cell = predicted.pop(label.field_key, None)
        outcome = _compare(sample.sample_id, label, cell)
        outcomes.append(outcome)
        if (outcome.outcome == 'INCORRECT' and label.field_key in ADDRESS_FIELDS and label.value_type == 'text'
                and same_address(label.normalized_value if label.normalized_value is not None else label.value, cell.value)):
            address_matches.append(label.field_key)

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

    shifted = len(address_matches)
    normalized_counts = {**counts, 'correct': counts['correct'] + shifted, 'incorrect': counts['incorrect'] - shifted}
    return FieldAccuracyResult(
        sample_id=sample.sample_id,
        field_outcomes=tuple(outcomes),
        denominators=Denominators(**counts),
        address_normalized_denominators=Denominators(**normalized_counts),
        address_normalized_fields=tuple(address_matches),
    )


ROLE_POLICY = 'printed-role-confirmed/1'
ORIGINAL_ROLE_POLICY = 'alias-only/0'
# The bill mapper stores any printed bill identifier as bill_reference. The field
# dictionary allows scoring it as bill_number only when the printed label confirms
# that role; a generic "Reference" never qualifies. Labels are never changed.
_PRIMARY_BILL_NUMBER_LABEL = re.compile(
    r'(?:tax\s+)?(?:bill|invoice|invois|bil)\s*(?:no\.?|number|num\.?|#)|no\.?\s*(?:bil|invois)', re.I)


def _printed_primary_number_label(raw_cell: dict) -> bool:
    raw, value = raw_cell.get('raw_text'), raw_cell.get('value')
    if not isinstance(raw, str) or not isinstance(value, str) or not value or value not in raw:
        return False
    label = raw.replace(value, '', 1).strip().rstrip(':').strip()
    return '\n' not in label and bool(_PRIMARY_BILL_NUMBER_LABEL.fullmatch(label))


def prediction_view_from_public_result(sample: Sample, public_result: dict, *,
                                       role_policy: str = ROLE_POLICY) -> SimpleNamespace:
    """Adapt an immutable M2 public result to dataset keys for benchmark scoring.

    The database field_predictions table uses legacy short keys. The evaluator
    reads raw_extractions.raw_json instead, so reviewer corrections in
    current_value can never change benchmark scores. role_policy=ORIGINAL_ROLE_POLICY
    reproduces scoring from before printed-role confirmation, for side-by-side reports.
    """
    if not isinstance(public_result, dict) or not isinstance(public_result.get('fields'), dict):
        raise ValueError('Extraction result has no field map')
    if role_policy not in {ROLE_POLICY, ORIGINAL_ROLE_POLICY}:
        raise ValueError('Unknown role policy')
    allowed = allowed_keys(sample.document_type, 'document')
    cells = []
    seen = set()
    role_confirmed = []
    for source_key, raw_cell in public_result['fields'].items():
        field_key = _canonical_field_key(source_key, sample.document_type)
        if (role_policy == ROLE_POLICY and field_key == 'bill_reference' and 'bill_number' in allowed
                and 'bill_number' not in public_result['fields'] and isinstance(raw_cell, dict)
                and _printed_primary_number_label(raw_cell)):
            field_key = 'bill_number'
            role_confirmed.append(source_key)
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
        role_policy=role_policy,
        role_confirmed_fields=tuple(role_confirmed),
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
    normalized = None
    if result_list and all(result.address_normalized_denominators is not None for result in result_list):
        normalized = Denominators(**{
            field: sum(getattr(result.address_normalized_denominators, field) for result in result_list)
            for field in count_fields
        })
    return FieldAccuracySummary(denominators=Denominators(**totals), address_normalized_denominators=normalized)


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
