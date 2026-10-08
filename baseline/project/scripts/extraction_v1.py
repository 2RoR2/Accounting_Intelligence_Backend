"""Proposed v1 adapter for the verified draft-0.2 invoice mapper; no AI calls."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

HEADERS = {'supplier_name', 'invoice_number', 'invoice_date', 'currency',
           'subtotal', 'tax_amount', 'total_amount'}
ROW_FIELDS = {'description', 'quantity', 'unit_price', 'line_total'}

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Candidate(StrictModel):
    raw_text: str | None
    value: str | None
    # Preserve the complete legacy location object, including nulls and both
    # coordinate origins. Geometry validation is not claimed by this adapter.
    evidence: list[dict[str, Any]]

class FieldResult(StrictModel):
    raw_text: str | None
    value: str | None
    evidence: list[dict[str, Any]]
    candidates: list[Candidate]
    state: Literal['resolved', 'missing', 'ambiguous', 'conflict', 'unsupported', 'unreadable']
    method: Literal['parser_mapping'] = 'parser_mapping'
    legacy_mapping_status: str
    review_required: Literal[True] = True

    @model_validator(mode='after')
    def validate_state(self):
        if self.state == 'resolved':
            if self.value is None or not self.value.strip():
                raise ValueError('resolved requires a non-empty string value')
        elif self.value is not None:
            raise ValueError('unresolved fields must have value=null; retain candidates')
        if self.state == 'conflict':
            values = {c.value for c in self.candidates if c.value is not None and c.value.strip()}
            if len(values) < 2:
                raise ValueError('conflict requires at least two distinct candidate values')
        return self

class Classification(StrictModel):
    value: Literal['unknown'] = 'unknown'
    status: Literal['not_run'] = 'not_run'
    reason: str = 'Document classification has not been implemented in this adapter.'

class ExtractionResultV1(StrictModel):
    schema_version: Literal['1.0.0-proposed'] = '1.0.0-proposed'
    mode: Literal['real_parser_mapping'] = 'real_parser_mapping'
    document_id: str = Field(min_length=1)
    extraction_run_id: str = Field(min_length=1)
    created_at: str
    document_type: Classification = Field(default_factory=Classification)
    field_schema: Literal['invoice_headers_and_simple_rows_v1'] = 'invoice_headers_and_simple_rows_v1'
    status: Literal['needs_review'] = 'needs_review'
    review_required: Literal[True] = True
    fields: dict[str, FieldResult]
    line_items: list[dict[str, FieldResult]]
    issues: list[dict[str, Any]]
    provenance: dict[str, Any]
    legacy_document_id: str | None
    legacy_run_id: str
    adapter: dict[str, str]

    @model_validator(mode='after')
    def check_shape(self):
        if set(self.fields) != HEADERS:
            raise ValueError('Expected all seven invoice header wrappers')
        for row in self.line_items:
            if set(row) != ROW_FIELDS:
                raise ValueError('Expected all four wrappers in each line-item row')
        if not self.line_items and not any(i.get('code') == 'NO_MAPPED_LINE_ITEMS' for i in self.issues):
            raise ValueError('Empty line items require NO_MAPPED_LINE_ITEMS explanation')
        return self

def adapt_field(field):
    allowed = {'raw_text', 'value', 'evidence', 'mapping_status', 'review_required', 'candidates'}
    if set(field) != allowed:
        raise ValueError('Unexpected legacy field shape; stop rather than discard information')
    states = {'mapped': 'resolved', 'missing': 'missing', 'ambiguous': 'ambiguous'}
    legacy_status = field['mapping_status']
    if legacy_status not in states:
        raise ValueError(f'Unsupported legacy status: {legacy_status!r}')
    if field['review_required'] is not True:
        raise ValueError('Expected review_required=true in legacy field')
    data = deepcopy(field)
    data['legacy_mapping_status'] = data.pop('mapping_status')
    data['state'] = states[legacy_status]
    return FieldResult.model_validate(data)

def adapt_result(legacy, document_id, source_path, source_sha256):
    allowed = {'schema_version', 'mode', 'document_id', 'status', 'fields',
               'line_items', 'issues', 'provenance', 'run_id'}
    if set(legacy) != allowed:
        raise ValueError('Unexpected legacy result shape; review before adapting')
    if legacy['schema_version'] != 'draft-0.2' or legacy['mode'] != 'real_parser_mapping':
        raise ValueError('Only real_parser_mapping draft-0.2 is supported')
    if legacy['status'] != 'needs_review':
        raise ValueError('Expected legacy needs_review status')
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError('Provide an explicit non-empty document ID')
    return ExtractionResultV1(
        document_id=document_id,
        extraction_run_id='extraction-' + uuid4().hex,
        created_at=datetime.now(timezone.utc).isoformat(),
        fields={k: adapt_field(v) for k, v in legacy['fields'].items()},
        line_items=[{k: adapt_field(v) for k, v in row.items()} for row in legacy['line_items']],
        issues=deepcopy(legacy['issues']),
        provenance=deepcopy(legacy['provenance']),
        legacy_document_id=legacy['document_id'],
        legacy_run_id=legacy['run_id'],
        adapter={'version': '0.1.0', 'source_schema_version': 'draft-0.2',
                 'source_result_path': str(source_path), 'source_result_sha256': source_sha256},
    )

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mapped_result', type=Path)
    parser.add_argument('--document-id', required=True,
                        help='Backend document ID, or an explicit synthetic test ID')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    source = args.mapped_result.resolve()
    if not source.is_relative_to(root):
        parser.error('Input must be inside the SSD project')
    try:
        raw = source.read_bytes()
        result = adapt_result(json.loads(raw), args.document_id, source, hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    out = root / 'runs' / result.extraction_run_id
    out.mkdir(parents=True, exist_ok=False)
    (out / 'result.json').write_text(result.model_dump_json(indent=2) + '\n', encoding='utf-8')
    (out / 'schema.json').write_text(json.dumps(ExtractionResultV1.model_json_schema(), indent=2) + '\n', encoding='utf-8')
    print('Schema: 1.0.0-proposed')
    print('Document ID:', result.document_id)
    print('Classification: unknown / not_run')
    print('Saved:', out)
    print('Review required. No AI call, classification or accounting approval performed.')

if __name__ == '__main__':
    main()



