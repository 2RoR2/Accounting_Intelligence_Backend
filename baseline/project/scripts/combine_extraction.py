"""Attach verified heading classification to a proposed v1 extraction result."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator
from extraction_v1 import ExtractionResultV1, StrictModel
from classify_document import classify_document

class ClassificationResult(StrictModel):
    classifier_version: Literal['0.1.0']
    method: Literal['explicit_heading_rules']
    value: Literal['invoice', 'bill', 'receipt', 'unknown']
    status: Literal['classified', 'unknown', 'ambiguous', 'unsupported']
    reason: str
    candidates: list[dict[str, Any]]
    unsupported_headings: list[dict[str, Any]]
    review_required: Literal[True]
    source_document_json: str
    source_json_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def consistent_status(self):
        if (self.status == 'classified') != (self.value != 'unknown'):
            raise ValueError('Only classified status can select a known document type')
        return self

class ClassificationAttachment(StrictModel):
    version: Literal['0.1.0'] = '0.1.0'
    parent_extraction_run_id: str
    extraction_result_path: str
    extraction_result_sha256: str
    classification_result_path: str
    classification_result_sha256: str
    source_identity_verified: Literal[True] = True
    heading_rules_rechecked: Literal[True] = True

class ClassifiedExtractionResult(ExtractionResultV1):
    schema_version: Literal['1.1.0-proposed'] = '1.1.0-proposed'
    document_type: ClassificationResult = Field(...)
    classification_attachment: ClassificationAttachment

    @model_validator(mode='after')
    def supported_field_schema(self):
        if self.document_type.value in {'bill', 'receipt'}:
            raise ValueError('Bill/receipt fields are not implemented; cannot attach to invoice schema')
        return self

def combine_files(extraction_path, classification_path, project_root):
    """Read immutable inputs; verify the shared source before constructing output."""
    root = Path(project_root).resolve()

    def checked_path(path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f'Input must be within the project: {resolved}')
        return resolved

    extraction_path = checked_path(extraction_path)
    classification_path = checked_path(classification_path)
    extraction_bytes = extraction_path.read_bytes()
    classification_bytes = classification_path.read_bytes()
    original = ExtractionResultV1.model_validate_json(extraction_bytes)
    classification = ClassificationResult.model_validate_json(classification_bytes)
    source = checked_path(classification.source_document_json)
    extraction_source = checked_path(original.provenance['source_document_json'])
    if source != extraction_source:
        raise ValueError('Classification and extraction source paths differ')
    if classification.source_json_sha256 != original.provenance['source_json_sha256']:
        raise ValueError('Classification and extraction source hashes differ')
    source_bytes = source.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != classification.source_json_sha256:
        raise ValueError('Source document.json changed after the recorded runs')

    # Cheap rule recheck, not a new Docling conversion or Qwen request. This
    # prevents accepting an altered classification merely because its hash
    # reference still points to the correct source document.
    expected = classify_document(json.loads(source_bytes))
    actual = classification.model_dump(exclude={'source_document_json', 'source_json_sha256'})
    if expected != actual:
        raise ValueError('Classification does not match the current rules and verified source')

    data = original.model_dump()
    data.update(
        schema_version='1.1.0-proposed',
        extraction_run_id='extraction-' + uuid4().hex,
        created_at=datetime.now(timezone.utc).isoformat(),
        document_type=classification.model_dump(),
        classification_attachment=ClassificationAttachment(
            parent_extraction_run_id=original.extraction_run_id,
            extraction_result_path=str(extraction_path),
            extraction_result_sha256=hashlib.sha256(extraction_bytes).hexdigest(),
            classification_result_path=str(classification_path),
            classification_result_sha256=hashlib.sha256(classification_bytes).hexdigest(),
        ).model_dump(),
    )
    return ClassifiedExtractionResult.model_validate(data)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('extraction_result', type=Path)
    parser.add_argument('classification_result', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        result = combine_files(args.extraction_result, args.classification_result, root)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        parser.error(str(error))
    out = root / 'runs' / result.extraction_run_id
    out.mkdir(parents=True, exist_ok=False)
    (out / 'result.json').write_text(result.model_dump_json(indent=2) + '\n', encoding='utf-8')
    (out / 'schema.json').write_text(json.dumps(result.model_json_schema(), indent=2) + '\n', encoding='utf-8')
    print('PASS: source paths and hashes verified; heading rules rechecked')
    print('Schema:', result.schema_version)
    print('Document ID:', result.document_id)
    print('Classification:', result.document_type.value, '/', result.document_type.status)
    print('Header fields:', len(result.fields), '| Line-item rows:', len(result.line_items))
    print('Saved:', out)
    print('Review required. Existing results unchanged; no Docling or Qwen inference performed.')

if __name__ == '__main__':
    main()


