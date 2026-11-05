"""Separate future artifact contracts, not an evaluator or production review API.

Nothing in this module reads predictions from mutable accountant current values.
Artifact hashes reference frozen bytes; revisions are new artifacts, never edits.
"""
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ai.evaluation.dataset_contract import (
    Evidence, FieldKey, Hash, Kind, Nonempty, StrictModel, Value, ValueType,
    check_timestamp, check_value, unique,
)
from ai.evaluation.field_dictionary import FIELDS, allowed_keys

Count = Annotated[int, Field(ge=0)]


def prediction_scope(cells, scope):
    # Validate placement independently of predicted kind so wrong-kind predictions
    # remain observable, rather than disappearing before classification scoring.
    allowed = set().union(*(allowed_keys(kind, scope) for kind in ('invoice', 'bill', 'receipt')))
    if any(cell.field_key not in allowed for cell in cells):
        raise ValueError('Prediction field is outside its dictionary scope')


class PredictionCell(StrictModel):
    field_key: FieldKey
    value_type: ValueType
    value: Value
    raw_text: str | None
    state: Literal['resolved', 'missing', 'ambiguous', 'conflict', 'unsupported', 'unreadable']
    method: Literal['PARSER', 'OCR', 'VLM']
    model_name: Nonempty | None
    model_version: Nonempty | None
    evidence: list[Evidence]
    candidates: list['PredictionCandidate']

    @model_validator(mode='after')
    def value_state(self):
        if FIELDS[self.field_key]['value_type'] != self.value_type:
            raise ValueError('Prediction type differs from dictionary')
        if (self.state == 'resolved') != (self.value is not None):
            raise ValueError('Only resolved predictions contain selected values')
        if self.method == 'VLM' and (not self.model_name or not self.model_version):
            raise ValueError('VLM predictions require model identity and version')
        check_value(self.value_type, self.value)
        unique([c.candidate_id for c in self.candidates], 'prediction candidate')
        for candidate in self.candidates:
            check_value(self.value_type, candidate.value)
        return self


class PredictionCandidate(StrictModel):
    candidate_id: Nonempty
    value: str | bool
    raw_text: str | None
    evidence: list[Evidence]
    method: Literal['PARSER', 'OCR', 'VLM']
    model_name: Nonempty | None
    model_version: Nonempty | None

    @model_validator(mode='after')
    def model_identity(self):
        if self.method == 'VLM' and (not self.model_name or not self.model_version):
            raise ValueError('VLM candidates require model identity and version')
        return self


class PredictionRow(StrictModel):
    prediction_row_id: Nonempty
    order: Annotated[int, Field(gt=0)]
    cells: list[PredictionCell]


class PredictionGroup(StrictModel):
    prediction_group_id: Nonempty
    scope: Literal['tax_breakdown', 'charge_breakdown', 'meter_readings', 'additional_attributes']
    order: Annotated[int, Field(gt=0)]
    cells: list[PredictionCell]


class FrozenPrediction(StrictModel):
    schema_version: Literal['1.0.0']
    artifact_type: Literal['frozen_pre_review_prediction']
    sample_id: Nonempty
    source_sha256: Hash
    extraction_run_id: Nonempty
    captured_at: Nonempty
    extractor_version: Nonempty
    configuration_sha256: Hash
    predicted_document_type: Literal['invoice', 'bill', 'receipt', 'credit_note', 'debit_note', 'unknown']
    fields: list[PredictionCell]
    rows: list[PredictionRow]
    repeated_groups: list[PredictionGroup]

    @field_validator('captured_at')
    @classmethod
    def timestamp(cls, value):
        return check_timestamp(value)

    @model_validator(mode='after')
    def identity(self):
        unique([c.field_key for c in self.fields], 'prediction field')
        prediction_scope(self.fields, 'document')
        unique([r.prediction_row_id for r in self.rows], 'prediction row')
        unique([r.order for r in self.rows], 'prediction order')
        unique([g.prediction_group_id for g in self.repeated_groups], 'prediction group')
        for group in self.repeated_groups:
            unique([c.field_key for c in group.cells], 'prediction group cell')
            prediction_scope(group.cells, group.scope)
        for row in self.rows:
            unique([c.field_key for c in row.cells], 'prediction cell')
            prediction_scope(row.cells, 'line')
        return self


class FieldOutcome(StrictModel):
    sample_id: Nonempty
    field_key: FieldKey
    gold_row_id: Nonempty | None
    prediction_row_id: Nonempty | None
    group_id: Nonempty | None
    outcome: Literal['CORRECT', 'INCORRECT', 'MISSING', 'CORRECT_ABSENT', 'FALSE_FILL', 'EXCLUDED']
    exclusion_reason: Literal['UNLABELLED', 'AMBIGUOUS', 'UNREADABLE', 'UNVERIFIED', 'OUT_OF_SCOPE'] | None

    @model_validator(mode='after')
    def exclusion(self):
        if (self.outcome == 'EXCLUDED') != (self.exclusion_reason is not None):
            raise ValueError('Excluded results require exactly one explicit reason')
        return self


class Denominators(StrictModel):
    eligible_present: Count
    correct: Count
    incorrect: Count
    missing: Count
    eligible_absent: Count
    correct_absent: Count
    false_fills: Count
    excluded: Count
    evidence_available: Count

    @model_validator(mode='after')
    def sums(self):
        if self.eligible_present != self.correct + self.incorrect + self.missing:
            raise ValueError('Present denominator does not equal outcomes')
        if self.eligible_absent != self.correct_absent + self.false_fills:
            raise ValueError('Absent denominator does not equal outcomes')
        if self.evidence_available > self.eligible_present + self.eligible_absent:
            raise ValueError('Evidence count exceeds eligible fields')
        return self


class Breakdown(StrictModel):
    # Explicit composite cohort keys; null means aggregate over that dimension.
    dataset: Nonempty | None
    document_type: Kind | None
    origin: Literal['REAL', 'SYNTHETIC'] | None
    benchmark_split: Literal['development', 'calibration', 'held_out'] | None
    field_key: FieldKey | None
    scope: Literal['document', 'line', 'tax_breakdown', 'charge_breakdown', 'meter_readings', 'additional_attributes']
    counts: Denominators


class RowMatch(StrictModel):
    sample_id: Nonempty
    gold_row_id: Nonempty | None
    prediction_row_id: Nonempty | None
    outcome: Literal['MATCHED', 'MISSING_ROW', 'EXTRA_ROW', 'EXCLUDED']

    @model_validator(mode='after')
    def match(self):
        shape = (self.gold_row_id is not None, self.prediction_row_id is not None)
        expected = {'MATCHED': (True, True), 'MISSING_ROW': (True, False), 'EXTRA_ROW': (False, True)}
        if not any(shape) or self.outcome in expected and shape != expected[self.outcome]:
            raise ValueError('Malformed row match')
        return self


class ClassificationOutcome(StrictModel):
    sample_id: Nonempty
    expected: Kind
    predicted: Literal['invoice', 'bill', 'receipt', 'credit_note', 'debit_note', 'unknown']
    eligible: bool
    exclusion_reason: Nonempty | None

    @model_validator(mode='after')
    def eligibility(self):
        if self.eligible == (self.exclusion_reason is not None):
            raise ValueError('Only excluded classifications require an exclusion reason')
        return self


class EvaluationOutput(StrictModel):
    schema_version: Literal['1.0.0']
    artifact_type: Literal['evaluation_output']
    benchmark_id: Nonempty
    benchmark_revision: Nonempty
    ground_truth_manifest_sha256: Annotated[list[Hash], Field(min_length=3, max_length=3)]
    prediction_artifact_sha256: Annotated[list[Hash], Field(min_length=1)]
    evaluator_version: Nonempty
    comparison_policy_version: Nonempty
    row_matching_policy_version: Nonempty
    verification_policy_version: Nonempty
    created_at: Nonempty
    field_outcomes: list[FieldOutcome]
    classifications: list[ClassificationOutcome]
    row_matches: list[RowMatch]
    breakdowns: list[Breakdown]

    @field_validator('created_at')
    @classmethod
    def timestamp(cls, value):
        return check_timestamp(value)


class ReviewDecision(StrictModel):
    decision_id: Nonempty
    prior_decision_id: Nonempty | None
    actor_id: Nonempty
    decided_at: Nonempty
    rationale: Nonempty
    action: Literal['CORRECTION', 'VALIDATION', 'APPROVAL', 'REJECTION']
    field_key: FieldKey | None
    row_id: Nonempty | None
    previous_value: Value
    revised_value: Value
    rule_codes: list[Nonempty]

    @field_validator('decided_at')
    @classmethod
    def timestamp(cls, value):
        return check_timestamp(value)


class AccountingReviewState(StrictModel):
    schema_version: Literal['1.0.0']
    artifact_type: Literal['accounting_validation_and_review_state']
    sample_id: Nonempty
    frozen_prediction_sha256: Hash
    revision_id: Nonempty
    prior_revision_sha256: Hash | None
    validation_state: Literal['NOT_RUN', 'PASSED', 'FAILED', 'NOT_CHECKED']
    review_state: Literal['UNREVIEWED', 'IN_REVIEW', 'APPROVED', 'REJECTED']
    decisions: list[ReviewDecision]
