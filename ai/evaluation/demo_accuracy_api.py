"""Proposed API response models only. No accuracy evaluator or API router."""
from typing import Literal
from pydantic import Field, model_validator
from ai.evaluation.dataset_contract import StrictModel, Nonempty, Hash, FieldKey, ValueType, Value, Evidence

Outcome = Literal['CORRECT','INCORRECT','MISSING_PREDICTION','CORRECT_ABSTENTION','FALSE_FILL',
                  'NOT_MEASURABLE','AMBIGUOUS_REFERENCE','UNREADABLE_REFERENCE','NOT_RUN']


class Ratio(StrictModel):
    numerator: int | None = Field(default=None,ge=0)
    denominator: int | None = Field(default=None,ge=0)
    value: str | None = None


class Metrics(StrictModel):
    status: Literal['NOT_RUN','NOT_MEASURABLE','EVALUATED']
    correct: int | None = Field(default=None,ge=0)
    incorrect: int | None = Field(default=None,ge=0)
    missing_prediction: int | None = Field(default=None,ge=0)
    correct_abstention: int | None = Field(default=None,ge=0)
    false_fill: int | None = Field(default=None,ge=0)
    not_measurable: int | None = Field(default=None,ge=0)
    ambiguous_reference: int | None = Field(default=None,ge=0)
    unreadable_reference: int | None = Field(default=None,ge=0)
    accuracy: Ratio
    extraction_coverage: Ratio
    exact_precision: Ratio
    exact_recall: Ratio

    @model_validator(mode='after')
    def unrun_has_no_metrics(self):
        if self.status != 'EVALUATED':
            for key,value in self.model_dump().items():
                if key != 'status' and (isinstance(value,dict) and any(v is not None for v in value.values())
                                        or not isinstance(value,dict) and value is not None):
                    raise ValueError('Unrun/unmeasurable metrics must stay null')
        return self


class FieldComparison(StrictModel):
    field_key: FieldKey
    value_type: ValueType
    reference_value: Value
    predicted_value: Value
    gold_row_id: Nonempty | None
    prediction_row_id: Nonempty | None
    outcome: Outcome
    reason: Nonempty | None
    reference_state: Literal['PRESENT','CONFIRMED_ABSENT','UNLABELLED','AMBIGUOUS','UNREADABLE']
    reference_verification_state: Literal['PUBLISHED_AS_IS','INDEPENDENTLY_VERIFIED','UNVERIFIED','SCHEMA_TEST_ONLY']
    evidence_available: bool | None
    reference_evidence: list[Evidence]
    prediction_evidence: list[Evidence]
    method: Literal['PARSER','OCR','VLM'] | None
    model_version: Nonempty | None
    calibrated_confidence: None

    @model_validator(mode='after')
    def no_unrun_predictions(self):
        if self.outcome == 'NOT_RUN' and (self.predicted_value is not None or self.method is not None or self.prediction_evidence):
            raise ValueError('NOT_RUN cannot contain original predictions that have not happened')
        return self


class RowComparison(StrictModel):
    gold_row_id: Nonempty | None
    prediction_row_id: Nonempty | None
    outcome: Literal['MATCHED','MISSING_ROW','EXTRA_ROW','NOT_MEASURABLE']
    reason: Nonempty | None


class DemoComparison(StrictModel):
    demo_code: Nonempty
    sample_id: Nonempty
    document_id: Nonempty | None
    document_code: Nonempty | None
    raw_extraction_id: Nonempty | None
    extraction_run_id: Nonempty | None
    frozen_prediction_sha256: Hash | None
    ground_truth_manifest_sha256: Hash
    state: Literal['NOT_RUN','AWAITING_FROZEN_PREDICTION','EVALUATED']
    origin: Literal['REAL','SYNTHETIC']
    document_type: Literal['invoice','bill','receipt']
    source_dataset: Nonempty
    evaluation_split: Literal['development','calibration','held_out']
    predicted_document_type: Literal['invoice','bill','receipt','credit_note','debit_note','unknown'] | None
    line_annotation_status: Literal['NOT_LABELLED','CONFIRMED_EMPTY','LABELLED_ROWS']
    line_row_coverage: Literal['UNKNOWN','PARTIAL','COMPLETE']
    fields: list[FieldComparison]
    row_comparisons: list[RowComparison]
    metrics: Metrics
    line_item_metrics: Metrics
    classification_metrics: Metrics
    calibrated_confidence: None


class AccuracyPreview(StrictModel):
    schema_version: Literal['1.0.0']
    contract_status: Literal['PROPOSED_NOT_ROUTED']
    dataset_id: Nonempty
    benchmark_revision: Nonempty
    state: Literal['NOT_RUN','EVALUATED']
    comparison_policy_version: Nonempty | None
    verification_policy_version: Nonempty | None
    row_matching_policy_version: Nonempty | None
    documents: list[DemoComparison]
    overall: Metrics
    per_field: dict[str,Metrics]
    per_document_type: dict[str,Metrics]
    per_source_cohort: dict[str,Metrics]
    classification_metrics: Metrics
    calibrated_confidence: None
