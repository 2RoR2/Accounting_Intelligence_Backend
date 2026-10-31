"""Offline v1 gold contract and file checks. No app, DB, queue or provider imports.

JSON Schema validates structure; Pydantic additionally validates dictionary scope,
label semantics, calendar dates, geometry, and collection identity invariants.
Use validate_collection for release checks, never schema-only validation.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai.evaluation.field_dictionary import FIELDS, allowed_keys

Nonempty = Annotated[str, Field(min_length=1, pattern=r'\S')]
PositiveInt = Annotated[int, Field(gt=0)]
Dimension = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Coordinate = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Kind = Literal['invoice', 'bill', 'receipt']
FieldKey = Literal[tuple(FIELDS)]
ValueType = Literal['text', 'decimal', 'date', 'time', 'currency', 'boolean']
Value = str | bool | None
LabelState = Literal['PRESENT', 'CONFIRMED_ABSENT', 'UNLABELLED', 'AMBIGUOUS', 'UNREADABLE']
DECIMAL_PATTERN = r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$'


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)


def check_timestamp(value: str) -> str:
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Timestamp requires a timezone')
    return value


def relative_path(value: str) -> str:
    """Portable local path/private object key, never a URL or platform escape."""
    if (not value or '\\' in value or ':' in value or '\x00' in value
            or value.startswith('/') or any(x in {'', '.', '..'} for x in value.split('/'))):
        raise ValueError('Use a safe relative POSIX path or private object key')
    return value


class Annotation(StrictModel):
    source_kind: Literal['PUBLISHED_ANNOTATION', 'INDEPENDENT_HUMAN', 'SYNTHETIC_FIXTURE']
    source_reference: Nonempty
    revision: Nonempty
    verification_state: Literal['PUBLISHED_AS_IS', 'INDEPENDENTLY_VERIFIED', 'UNVERIFIED', 'SCHEMA_TEST_ONLY']
    annotator_id: Nonempty | None
    verifier_id: Nonempty | None
    verified_at: Nonempty | None
    notes: Nonempty | None = None

    @model_validator(mode='after')
    def provenance(self):
        if (self.source_kind == 'SYNTHETIC_FIXTURE') != (self.verification_state == 'SCHEMA_TEST_ONLY'):
            raise ValueError('Fixture provenance must remain SCHEMA_TEST_ONLY')
        if self.verification_state == 'PUBLISHED_AS_IS' and self.source_kind != 'PUBLISHED_ANNOTATION':
            raise ValueError('Published status requires published annotation provenance')
        if self.source_kind == 'INDEPENDENT_HUMAN' and not self.annotator_id:
            raise ValueError('Human labels require a pseudonymous annotator identity')
        if self.verification_state == 'INDEPENDENTLY_VERIFIED':
            if not self.verifier_id or not self.verified_at or self.verifier_id == self.annotator_id:
                raise ValueError('Independent verification requires a distinct verifier and timestamp')
        if self.verified_at:
            check_timestamp(self.verified_at)
        return self


class Evidence(StrictModel):
    page: PositiveInt  # one-based, source page (not OCR chunk)
    source_reference: Nonempty
    printed_text: str | None
    bbox: Annotated[list[Coordinate], Field(min_length=4, max_length=4)] | None
    coordinate_convention: Literal['xyxy_top_left'] | None
    coordinate_units: Literal['pixel', 'pdf_point', 'normalized'] | None
    page_width: Dimension | None
    page_height: Dimension | None

    @model_validator(mode='after')
    def geometry(self):
        geometry = (self.coordinate_convention, self.coordinate_units, self.page_width, self.page_height)
        if self.bbox is None:
            if any(v is not None for v in geometry):
                raise ValueError('No bbox means coordinate metadata must be null')
        else:
            if any(v is None for v in geometry):
                raise ValueError('BBox requires convention, units and actual page dimensions')
            left, top, right, bottom = self.bbox
            if not left < right or not top < bottom:
                raise ValueError('BBox must have positive area')
            if self.coordinate_units == 'normalized':
                if right > 1 or bottom > 1:
                    raise ValueError('Normalized bbox must be within [0, 1]')
            elif right > self.page_width or bottom > self.page_height:
                raise ValueError('BBox exceeds page dimensions')
        return self


def check_value(value_type, value):
    if value is None:
        return
    if value_type == 'boolean':
        if type(value) is not bool:
            raise ValueError('Boolean label requires a JSON boolean')
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Non-boolean values require nonempty strings')
    patterns = {'decimal': DECIMAL_PATTERN, 'date': r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$',
                'time': r'^[0-9]{2}:[0-9]{2}(?::[0-9]{2})?$', 'currency': r'^[A-Z]{3}$'}
    if value_type in patterns and not re.fullmatch(patterns[value_type], value):
        raise ValueError('Malformed typed value: ' + value_type)
    if value_type == 'date':
        date.fromisoformat(value)
    if value_type == 'time':
        time.fromisoformat(value)


class Candidate(StrictModel):
    candidate_id: Nonempty
    value: str | bool
    normalized_value: str | bool
    printed_text: str
    evidence: Annotated[list[Evidence], Field(min_length=1)]
    annotation: Annotation


class GoldLabel(StrictModel):
    field_key: FieldKey
    value_type: ValueType
    label_state: LabelState
    value: Value
    printed_text: str | None
    normalized_value: Value
    evidence: list[Evidence]
    candidates: list[Candidate]
    annotation: Annotation

    @model_validator(mode='after')
    def semantics(self):
        if FIELDS[self.field_key]['value_type'] != self.value_type:
            raise ValueError('Value type does not match the shared dictionary')
        if self.label_state == 'PRESENT':
            if self.value is None or self.normalized_value is None:
                raise ValueError('PRESENT requires a value and a normalized value')
        elif self.value is not None or self.normalized_value is not None:
            raise ValueError('Absent, unknown, ambiguous and unreadable values must be null')
        if self.label_state != 'AMBIGUOUS' and self.candidates:
            raise ValueError('Only genuinely ambiguous labels carry alternative candidates')
        if self.label_state in {'CONFIRMED_ABSENT', 'UNLABELLED'} and (self.printed_text is not None or self.evidence):
            raise ValueError('Absent/unlabelled fields cannot claim printed field evidence')
        if self.label_state == 'CONFIRMED_ABSENT' and self.annotation.verification_state not in {
            'INDEPENDENTLY_VERIFIED', 'SCHEMA_TEST_ONLY'}:
            raise ValueError('Confirmed absence requires independent inspection or an explicit schema fixture')
        check_value(self.value_type, self.value)
        check_value(self.value_type, self.normalized_value)
        unique([c.candidate_id for c in self.candidates], 'candidate ID')
        for candidate in self.candidates:
            check_value(self.value_type, candidate.value)
            check_value(self.value_type, candidate.normalized_value)
        if self.candidates:
            values = [c.normalized_value for c in self.candidates]
            if self.value_type == 'decimal':
                values = [Decimal(value) for value in values]
            elif self.value_type == 'time':
                values = [time.fromisoformat(value) for value in values]
            if len(set(values)) < 2:
                raise ValueError('Ambiguous alternatives require distinct normalized values')
        return self


def unique(values, what):
    if len(values) != len(set(values)):
        raise ValueError('Duplicate ' + what)


def check_labels(labels, kind, scope):
    unique([label.field_key for label in labels], 'field key')
    if any(label.field_key not in allowed_keys(kind, scope) for label in labels):
        raise ValueError('Field not allowed for document type/scope')


class Row(StrictModel):
    row_id: Nonempty
    printed_order: PositiveInt
    page: PositiveInt
    row_evidence: Evidence | None
    cells: Annotated[list[GoldLabel], Field(min_length=1)]

    @model_validator(mode='after')
    def row_page(self):
        if self.row_evidence and self.row_evidence.page != self.page:
            raise ValueError('Row bbox page must equal row starting page')
        return self


class LineAnnotations(StrictModel):
    status: Literal['NOT_LABELLED', 'CONFIRMED_EMPTY', 'LABELLED_ROWS']
    # Partial rows support cell scoring but cannot supply extra-row denominators.
    row_coverage: Literal['UNKNOWN', 'PARTIAL', 'COMPLETE']
    annotation: Annotation
    rows: list[Row]

    @model_validator(mode='after')
    def coverage(self):
        if self.status == 'NOT_LABELLED':
            if self.rows or self.row_coverage != 'UNKNOWN':
                raise ValueError('NOT_LABELLED requires empty rows and UNKNOWN coverage')
        elif self.status == 'CONFIRMED_EMPTY':
            if self.rows or self.row_coverage != 'COMPLETE':
                raise ValueError('CONFIRMED_EMPTY requires empty rows and COMPLETE coverage')
            if self.annotation.verification_state not in {'INDEPENDENTLY_VERIFIED', 'SCHEMA_TEST_ONLY'}:
                raise ValueError('Empty rows require independent inspection')
        elif not self.rows or self.row_coverage == 'UNKNOWN':
            raise ValueError('LABELLED_ROWS requires rows and PARTIAL/COMPLETE coverage')
        unique([row.row_id for row in self.rows], 'row ID')
        unique([row.printed_order for row in self.rows], 'printed order')
        if [r.printed_order for r in self.rows] != sorted(r.printed_order for r in self.rows):
            raise ValueError('Rows must be in printed order')
        return self


class RepeatedGroup(StrictModel):
    group_id: Nonempty
    scope: Literal['tax_breakdown', 'charge_breakdown', 'meter_readings', 'additional_attributes']
    printed_order: PositiveInt
    labels: Annotated[list[GoldLabel], Field(min_length=1)]


class Page(StrictModel):
    page: PositiveInt
    width: Dimension
    height: Dimension
    units: Literal['pixel', 'pdf_point']
    image_width_px: PositiveInt | None
    image_height_px: PositiveInt | None
    dpi_x: Dimension | None = None
    dpi_y: Dimension | None = None

    @model_validator(mode='after')
    def pixels(self):
        if (self.image_width_px is None) != (self.image_height_px is None):
            raise ValueError('Image dimensions require both axes')
        if self.units == 'pixel' and (self.width != self.image_width_px or self.height != self.image_height_px):
            raise ValueError('Pixel dimensions must agree')
        return self


class Source(StrictModel):
    dataset: Nonempty
    dataset_revision: Nonempty | None
    original_split: Nonempty | None
    original_sample_id: Nonempty
    origin: Literal['REAL', 'SYNTHETIC']
    languages: list[Nonempty] | None
    country: Annotated[str, Field(pattern=r'^[A-Z]{2}$')] | None
    file_format: Literal['pdf', 'jpg', 'png']
    page_count: PositiveInt | None
    pages: list[Page]
    relative_path: Nonempty
    private_object_key: Nonempty | None
    sha256: Hash
    licence: Nonempty | None
    licence_reference: Nonempty | None
    annotation_provenance: Nonempty
    related_source_group: Nonempty
    base_document_group_id: Nonempty
    template_group_id: Nonempty | None
    quality_variant: Nonempty | None

    @field_validator('relative_path', 'private_object_key')
    @classmethod
    def safe_path(cls, value):
        return relative_path(value) if value is not None else value

    @model_validator(mode='after')
    def pagination(self):
        unique([p.page for p in self.pages], 'source page')
        if self.page_count is not None and any(p.page > self.page_count for p in self.pages):
            raise ValueError('Metadata page exceeds page count')
        return self


class Sample(StrictModel):
    sample_id: Nonempty
    document_type: Kind
    classification_annotation: Annotation
    bill_subtype: Literal['Electricity', 'Water', 'Gas', 'Telecom', 'Internet', 'Restaurant', 'Service', 'Other'] | None
    benchmark_split: Literal['development', 'calibration', 'held_out']
    source: Source
    fields: list[GoldLabel]
    line_items: LineAnnotations
    repeated_groups: list[RepeatedGroup]

    def labels(self):
        return [*self.fields, *(c for row in self.line_items.rows for c in row.cells),
                *(c for group in self.repeated_groups for c in group.labels)]

    def annotations(self):
        return [self.classification_annotation, self.line_items.annotation,
                *(label.annotation for label in self.labels()),
                *(candidate.annotation for label in self.labels() for candidate in label.candidates)]

    def evidence(self):
        return [*(e for label in self.labels() for e in label.evidence),
                *(e for label in self.labels() for c in label.candidates for e in c.evidence),
                *(row.row_evidence for row in self.line_items.rows if row.row_evidence)]

    @model_validator(mode='after')
    def consistent_sample(self):
        if self.document_type != 'bill' and self.bill_subtype is not None:
            raise ValueError('Bill subtype only belongs to bills')
        check_labels(self.fields, self.document_type, 'document')
        for row in self.line_items.rows:
            check_labels(row.cells, self.document_type, 'line')
        unique([g.group_id for g in self.repeated_groups], 'repeated group ID')
        unique([(g.scope, g.printed_order) for g in self.repeated_groups], 'group printed order within scope')
        for group in self.repeated_groups:
            check_labels(group.labels, self.document_type, group.scope)
        page_limit = self.source.page_count
        if page_limit and any(row.page > page_limit for row in self.line_items.rows):
            raise ValueError('Row page exceeds source')
        for evidence in self.evidence():
            if page_limit and evidence.page > page_limit:
                raise ValueError('Evidence page exceeds source')
            page = next((p for p in self.source.pages if p.page == evidence.page), None)
            if evidence.bbox and page:
                if evidence.page_width != page.width or evidence.page_height != page.height:
                    raise ValueError('Evidence dimensions differ from source page')
                if evidence.coordinate_units not in {'normalized', page.units}:
                    raise ValueError('Evidence units differ from source page')
        return self


class GroundTruthManifest(StrictModel):
    schema_version: Literal['1.0.0']
    artifact_type: Literal['frozen_ground_truth']
    field_dictionary_version: Literal['1.0.0']
    benchmark_id: Nonempty
    benchmark_revision: Nonempty
    purpose: Literal['SCHEMA_TEST_ONLY', 'BENCHMARK']
    document_type: Kind
    samples: list[Sample]

    @model_validator(mode='after')
    def manifest(self):
        unique([s.sample_id for s in self.samples], 'sample ID')
        if any(s.document_type != self.document_type for s in self.samples):
            raise ValueError('Manifest must contain exactly its declared document type')
        for sample in self.samples:
            annotations = sample.annotations()
            fixtures = any(a.source_kind == 'SYNTHETIC_FIXTURE' for a in annotations)
            if self.purpose == 'SCHEMA_TEST_ONLY':
                if sample.source.origin != 'SYNTHETIC' or not all(a.source_kind == 'SYNTHETIC_FIXTURE' for a in annotations):
                    raise ValueError('Schema-test manifests require explicit synthetic fixture provenance')
            elif fixtures or not sample.source.dataset_revision or not sample.source.licence:
                raise ValueError('Benchmarks require pinned, licensed sources without schema fixtures')
        return self


def load_manifest(path: Path) -> GroundTruthManifest:
    # Reject duplicate JSON object keys rather than silently retaining the last.
    def pairs(items):
        unique([key for key, _ in items], 'JSON object key')
        return dict(items)
    return GroundTruthManifest.model_validate(json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs))


def validate_files(manifest: GroundTruthManifest, source_root: Path) -> int:
    """Read only: containment, existence, SHA-256, content format/pages/dimensions.

    Optional inspection dependencies are imported here, never during app startup.
    No storage keys are fetched and no files are uploaded.
    """
    root = source_root.resolve(strict=True)
    for sample in manifest.samples:
        source = sample.source
        path = (root / PurePosixPath(source.relative_path)).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Source escapes root or is not a file: ' + sample.sample_id)
        if path.suffix.lower() != '.' + source.file_format:
            raise ValueError('Source extension mismatch: ' + sample.sample_id)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != source.sha256:
            raise ValueError('Source SHA-256 mismatch: ' + sample.sample_id)
        if source.file_format == 'pdf':
            from pypdf import PdfReader
            if not path.read_bytes().startswith(b'%PDF-'):
                raise ValueError('Invalid PDF signature')
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted:
                raise ValueError('Encrypted fixture/source requires an explicit import policy')
            sizes = [(float(p.mediabox.width), float(p.mediabox.height), 'pdf_point') for p in reader.pages]
        else:
            from PIL import Image
            with Image.open(path) as image:
                if image.format != {'png': 'PNG', 'jpg': 'JPEG'}[source.file_format]:
                    raise ValueError('Source content format mismatch')
                sizes = [(float(image.width), float(image.height), 'pixel')]
                image.verify()
        if source.page_count is not None and source.page_count != len(sizes):
            raise ValueError('Source page count mismatch')
        for page in source.pages:
            if page.page > len(sizes) or sizes[page.page - 1] != (page.width, page.height, page.units):
                raise ValueError('Actual source page dimensions mismatch')
        for evidence in sample.evidence():
            if evidence.page > len(sizes):
                raise ValueError('Actual source evidence page mismatch')
            if evidence.bbox and sizes[evidence.page - 1][:2] != (evidence.page_width, evidence.page_height):
                raise ValueError('Actual source evidence dimensions mismatch')
            if evidence.bbox and evidence.coordinate_units not in {'normalized', sizes[evidence.page - 1][2]}:
                raise ValueError('Actual source evidence units mismatch')
        if any(row.page > len(sizes) for row in sample.line_items.rows):
            raise ValueError('Actual source row page mismatch')
    return len(manifest.samples)


def validate_collection(paths: list[Path], source_root: Path) -> list[GroundTruthManifest]:
    manifests = [load_manifest(path) for path in paths]
    if len(manifests) != 3 or {m.document_type for m in manifests} != {'invoice', 'bill', 'receipt'}:
        raise ValueError('Provide exactly the three invoice/bill/receipt manifests')
    if len({(m.benchmark_id, m.benchmark_revision, m.purpose) for m in manifests}) != 1:
        raise ValueError('Manifests must share benchmark identity, revision and purpose')
    samples = [s for m in manifests for s in m.samples]
    unique([s.sample_id for s in samples], 'collection sample ID')
    split_by_group = {}
    for sample in samples:
        # Same hash/base/template across mirrors or manifest kinds cannot leak.
        groups = [('sha256', sample.source.sha256), ('base', sample.source.base_document_group_id)]
        if sample.source.template_group_id:
            groups.append(('template', sample.source.template_group_id))
        for group in groups:
            if split_by_group.setdefault(group, sample.benchmark_split) != sample.benchmark_split:
                raise ValueError('Duplicate/base/template group leaks across benchmark splits')
    for manifest in manifests:
        validate_files(manifest, source_root)
    return manifests
