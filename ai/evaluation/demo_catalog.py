"""Portable demo catalog. Ground truth remains the separate v1.0.0 manifests."""
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ai.evaluation.dataset_contract import (Hash, Kind, Nonempty, StrictModel, relative_path,
                                         load_manifest, validate_collection)

COUNTS = {'invoice': 15, 'bill': 10, 'receipt': 10}
PREFIX = {'invoice': 'INV', 'bill': 'BIL', 'receipt': 'REC'}
MIME = {'jpg': 'image/jpeg', 'png': 'image/png', 'pdf': 'application/pdf'}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def within(root, path):
    relative_path(path)
    target = (root / path).resolve(strict=True)
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise ValueError('Bundle path escapes its root or is not a file')
    return target


class Cohort(StrictModel):
    original_split: Nonempty | None
    evaluation_split: Literal['development', 'calibration', 'held_out']
    related_source_group: Nonempty
    base_document_group_id: Nonempty
    template_group_id: Nonempty | None
    quality_variant: Nonempty | None


class DemoEntry(StrictModel):
    demo_code: Nonempty
    sample_id: Nonempty
    document_type: Kind
    source_dataset: Nonempty
    source_revision: Nonempty
    origin: Literal['REAL', 'SYNTHETIC']
    original_source_filename: Nonempty | None
    downloaded_source_filename: Nonempty
    relative_asset_path: Nonempty
    sha256: Hash
    mime_type: Literal['image/jpeg', 'image/png', 'application/pdf']
    source_document_format: Literal['jpg', 'png', 'pdf']
    source_annotation_version: Nonempty
    ground_truth_manifest_reference: Nonempty
    source_annotation_path: Nonempty
    cohort: Cohort
    licence: Nonempty
    licence_reference: Nonempty
    sharing_status: Literal['SHAREABLE_WITH_NOTICES', 'BLOCKED_REACQUIRE']
    processing_state: Literal['NOT_RUN']
    document_id: None
    document_code: None
    extraction_run_id: None

    @field_validator('relative_asset_path', 'source_annotation_path')
    @classmethod
    def safe_path(cls, value):
        return relative_path(value)


class DemoCatalog(StrictModel):
    catalog_version: Literal['1.0.0']
    dataset_id: Literal['cos40005-module2-shared-v1']
    benchmark_revision: Literal['1']
    schema_version: Literal['1.0.0']
    samples: list[DemoEntry] = Field(min_length=35, max_length=35)

    @model_validator(mode='after')
    def identity(self):
        if Counter(e.document_type for e in self.samples) != COUNTS:
            raise ValueError('Catalog requires 15 invoices, 10 bills and 10 receipts')
        for attribute in ('sample_id', 'demo_code', 'sha256', 'relative_asset_path'):
            values = [getattr(e, attribute) for e in self.samples]
            if len(set(values)) != 35:
                raise ValueError('Duplicate catalog identity: ' + attribute)
        expected = []
        for kind in COUNTS:
            group = sorted((e for e in self.samples if e.document_type == kind), key=lambda e: e.sample_id)
            for index, entry in enumerate(group, 1):
                code = f'{PREFIX[kind]}{index:05d}'
                if entry.demo_code != code or entry.mime_type != MIME[entry.source_document_format]:
                    raise ValueError('Unstable demo ordering or incorrect MIME')
                expected.append(entry.sample_id)
        if [e.sample_id for e in self.samples] != expected:
            raise ValueError('Catalog must be grouped by kind, then sorted by source sample ID')
        return self


def verify_bundle(root):
    root = Path(root).resolve(strict=True)
    catalog = DemoCatalog.model_validate_json((root / 'demo_catalog.json').read_text(encoding='utf-8'))
    manifests = validate_collection([root / 'manifests' / name for name in
                                    ('invoices.json', 'bills.json', 'receipts.json')], root)
    all_samples = {s.sample_id: s for m in manifests for s in m.samples}
    if set(all_samples) != {e.sample_id for e in catalog.samples}:
        raise ValueError('Catalog and gold manifests contain different sample IDs')
    from PIL import Image
    from pypdf import PdfReader
    for entry in catalog.samples:
        if entry.sharing_status != 'SHAREABLE_WITH_NOTICES':
            raise ValueError('Blocked assets must be reacquired before a full import')
        path = within(root, entry.relative_asset_path)
        if digest(path) != entry.sha256 or path.suffix != '.' + entry.source_document_format:
            raise ValueError('Catalog asset hash/extension mismatch')
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError('Source exceeds existing upload limit')
        if entry.source_document_format == 'pdf':
            if not path.read_bytes().startswith(b'%PDF-') or not PdfReader(path, strict=True).pages:
                raise ValueError('Invalid PDF')
        else:
            signature = b'\xff\xd8\xff' if entry.source_document_format == 'jpg' else b'\x89PNG\r\n\x1a\n'
            if not path.read_bytes().startswith(signature):
                raise ValueError('Invalid image signature')
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
        manifest_path, fragment = entry.ground_truth_manifest_reference.split('#', 1)
        data = json.loads(within(root, manifest_path).read_text(encoding='utf-8'))
        linked = data
        for part in fragment.strip('/').split('/'):
            linked = linked[int(part)] if isinstance(linked, list) else linked[part]
        if linked['sample_id'] != entry.sample_id or linked['source']['relative_path'] != entry.relative_asset_path:
            raise ValueError('Incorrect catalog-to-gold reference')
        if linked['source']['sha256'] != entry.sha256:
            raise ValueError('Gold and catalog source hashes differ')
        within(root, entry.source_annotation_path)
    hash_file = root / 'SHA256SUMS.json'
    hashes = json.loads(hash_file.read_text(encoding='utf-8'))
    for path, expected in hashes.items():
        if digest(within(root, path)) != expected:
            raise ValueError('Bundle checksum mismatch: ' + path)
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()
              and '__pycache__' not in p.parts and p.name != 'SHA256SUMS.json'}
    if actual != set(hashes):
        raise ValueError('Unexpected or missing files in frozen handoff')
    return catalog
