"""Tests for Module 2 extraction execution boundary and parser injection.

Proves:
- Case A: Default parser is real Docling; fails closed when models missing.
- Case B: Controlled injected parser produces synthetic run artifacts.
- Case C: Service output is converted to strict public result.
- Case D: Identity, schema version, provenance, and source references are consistent.
- Case E: AI candidates never become approved fields.
- Case F: Missing/unresolved fields preserve review-required status.
- Case G: Parser/contract failure returns genuine failure, never an approved result.
- Case H: Offline safety: zero external network/database/model inference.
- Case K: Downstream adaptation test showing adapt_m2_to_m3 accepts complete and blocks unresolved.
"""

import json
import hashlib
from copy import deepcopy
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from services.extraction.contracts.adapter import adapt_m2_to_m3
from services.extraction.workflow.document_extraction_service import DocumentIntelligenceService
import services.extraction.workflow.document_extraction_service as dis
from services.extraction.workflow.execution import execute_document_extraction


def load_baseline_docling_doc():
    # Deterministic synthetic equivalent; no historical/genuine parser claim.
    from tests.extraction.synthetic_parser_fixture import document
    return document()


def make_injected_parser(document_dict, status="success"):
    def _parser(input_pdf, output_parent, project_root=None, verbose=False):
        output = output_parent / f"docling-test-{Path(input_pdf).stem}"
        output.mkdir(parents=True, exist_ok=True)
        (output / "document.json").write_text(json.dumps(document_dict), encoding="utf-8")
        (output / "run.json").write_text(json.dumps({
            "status": status,
            "mode": "test_injected_parser",
            "source": str(input_pdf),
            "source_sha256": hashlib.sha256(Path(input_pdf).read_bytes()).hexdigest(),
        }), encoding="utf-8")
        return output, {"status": status}
    return _parser


class TestExtractionExecutionBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runs").mkdir(parents=True, exist_ok=True)

        self.pdf_path = self.root / "test_invoice.pdf"
        from pypdf import PdfWriter
        writer=PdfWriter();writer.add_blank_page(width=595,height=842)
        with self.pdf_path.open('wb') as handle:writer.write(handle)
        self.doc_dict = load_baseline_docling_doc()

    def tearDown(self):
        self.tmp.cleanup()

    def test_case_a_default_real_parser_fails_when_models_missing(self):
        """Case A: Real Docling is the default and fails closed if models are missing."""
        service = DocumentIntelligenceService(project_root=self.root)
        # Models directory does not exist in root
        with self.assertRaises(ValueError) as ctx:
            service.process_document(
                document_id="DOC-FAIL-001",
                file_path=self.pdf_path,
            )
        self.assertIn("Docling model directory not found", str(ctx.exception))

    def test_case_b_controlled_injected_parser_produces_artifacts(self):
        """Case B: Injected parser generates expected synthetic run artifacts."""
        parser = make_injected_parser(self.doc_dict)
        service = DocumentIntelligenceService(
            project_root=self.root,
            parser=parser,
        )
        summary_path, summary = service.process_document(
            document_id="DOC-INJECT-001",
            file_path=self.pdf_path,
        )
        self.assertTrue(summary_path.is_file())
        self.assertEqual(summary["document_id"], "DOC-INJECT-001")
        self.assertEqual(summary["status"], "human_review_required")
        self.assertIn("SOURCE_ISSUE_REQUIRES_REVIEW:LAYOUT_LINE_RECOVERY",
                      {reason["code"] for reason in summary["routing"]["blocking_reasons"]})
        self.assertFalse(summary["ai_assistance"]["attempted"])
        self.assertTrue(Path(summary["artifacts"]["document_json"]).is_file())
        self.assertTrue(Path(summary["artifacts"]["mapped_result"]).is_file())
        self.assertTrue(Path(summary["artifacts"]["classified_extraction"]).is_file())

    def test_case_c_extraction_output_converted_to_strict_public_result(self):
        """Case C: Output is converted to strict public result via execute_document_extraction."""
        parser = make_injected_parser(self.doc_dict)
        result = execute_document_extraction(
            document_id="DOC-PUB-001",
            file_path=self.pdf_path,
            project_root=self.root,
            parser=parser,
        )
        self.assertEqual(result["execution_status"], "EXTRACTED")
        self.assertEqual(result["document_id"], "DOC-PUB-001")
        self.assertEqual(result["extraction_status"], "human_review_required")
        self.assertTrue(result["review_required"])
        self.assertFalse(result["accounting_approval_performed"])
        self.assertIsNone(result["error"])

        public_path = Path(result["public_result_path"])
        self.assertTrue(public_path.is_file())
        public_data = json.loads(public_path.read_text(encoding="utf-8"))
        self.assertEqual(public_data["mode"], "module2_public_result")
        self.assertEqual(public_data["schema_version"], "0.1.0-proposed")
        self.assertEqual(public_data["routing"]["decision"], "human_review")
        self.assertIn("SOURCE_ISSUE_REQUIRES_REVIEW:LAYOUT_LINE_RECOVERY",
                      {reason["code"] for reason in public_data["routing"]["blocking_reasons"]})

    def test_case_d_identity_and_provenance_consistency(self):
        """Case D: Document ID, schema version, provenance, and hashes remain consistent."""
        parser = make_injected_parser(self.doc_dict)
        result = execute_document_extraction(
            document_id="DOC-PROV-001",
            file_path=self.pdf_path,
            project_root=self.root,
            parser=parser,
        )
        public_data = json.loads(Path(result["public_result_path"]).read_text(encoding="utf-8"))

        self.assertEqual(public_data["document_id"], "DOC-PROV-001")
        self.assertEqual(public_data["schema_version"], "0.1.0-proposed")
        prov = public_data["provenance"]
        self.assertIn("service_run_id", prov)
        self.assertIn("classified_extraction_run_id", prov)
        self.assertEqual(len(prov["service_result_sha256"]), 64)
        self.assertEqual(len(prov["classified_extraction_sha256"]), 64)

    def test_case_e_ai_candidate_never_becomes_approved_field(self):
        """Case E: Publish a valid synthetic candidate without accepting its value.

        Reuse source-bound/reconciled contract fixtures. The injected Docling
        fixture has layout-recovery blockers and cannot legitimately trigger AI.
        This case isolates the execution/publication boundary, not live inference.
        """
        from tests.extraction.test_module2_public_result import Module2PublicResultTests
        fixtures = Module2PublicResultTests()
        fixtures.root, fixtures.runs = self.root, self.root / "runs"
        extraction_path, _ = fixtures.make_extraction(value=None, state="missing")
        reconciliation_path = fixtures.make_reconciliation(extraction_path, "INV-AI-99999")
        service_path = fixtures.make_service(extraction_path, "candidate_for_review",
                                             "vlm_candidate", reconciliation_path)
        service_payload = json.loads(service_path.read_text())
        with patch.object(DocumentIntelligenceService, "process_document",
                          return_value=(service_path, service_payload)) as process, \
             patch.object(dis, "run_attempt") as ai:
            result = execute_document_extraction(
                document_id="DOC-TEST",
                file_path=self.pdf_path,
                project_root=self.root,
                allow_vlm_inference=True,
            )
        process.assert_called_once()
        ai.assert_not_called()

        self.assertEqual(result["execution_status"], "EXTRACTED")
        self.assertEqual(result["extraction_status"], "candidate_for_review")
        public_data = json.loads(Path(result["public_result_path"]).read_text(encoding="utf-8"))

        # Safety checks: parser value must remain None; AI candidate is separate
        field = public_data["fields"]["invoice_number"]
        self.assertIsNone(field["value"])
        self.assertEqual(field["ai_candidate"], "INV-AI-99999")
        self.assertEqual(field["resolution"], "candidate_for_review")
        self.assertTrue(field["review_required"])
        self.assertFalse(public_data["safety"]["ai_candidate_auto_accepted"])
        self.assertFalse(public_data["safety"]["automatic_accounting_approval"])
        self.assertFalse(public_data["accounting_approval_performed"])

    def test_case_e1_layout_recovery_blocks_ai_even_when_enabled(self):
        """Layout recovery is a real review blocker, not an AI-eligible fixture."""
        modified_doc = json.loads(json.dumps(self.doc_dict))
        modified_doc["texts"] = [t for t in modified_doc["texts"]
            if not t.get("text", "").lower().startswith("invoice number:")]
        service = DocumentIntelligenceService(project_root=self.root,
            parser=make_injected_parser(modified_doc))
        with patch.object(dis, "run_attempt") as ai:
            _, summary = service.process_document(document_id="DOC-BLOCKED-AI",
                file_path=self.pdf_path, allow_vlm_inference=True)
        ai.assert_not_called()
        self.assertEqual(summary["status"], "human_review_required")
        self.assertIn("SOURCE_ISSUE_REQUIRES_REVIEW:LAYOUT_LINE_RECOVERY",
                      {reason["code"] for reason in summary["routing"]["blocking_reasons"]})
        self.assertIsNone(summary["reconciliation"])
        self.assertFalse(summary["accounting_approval_performed"])

    def test_case_e2_vlm_disabled_by_default_fails_closed_to_human_review(self):
        """Case E2: VLM candidate fails closed to human_review when allow_vlm_inference=False."""
        modified_doc = json.loads(json.dumps(self.doc_dict))
        modified_doc["texts"] = [
            t for t in modified_doc["texts"]
            if not t.get("text", "").lower().startswith("invoice number:")
        ]

        parser = make_injected_parser(modified_doc)

        with patch("services.extraction.workflow.document_extraction_service.run_attempt") as mock_attempt:
            # allow_vlm_inference defaults to False
            result = execute_document_extraction(
                document_id="DOC-VLM-DIS-001",
                file_path=self.pdf_path,
                project_root=self.root,
                parser=parser,
            )
            # Live VLM attempt must NOT be called
            mock_attempt.assert_not_called()

        self.assertEqual(result["execution_status"], "EXTRACTED")
        self.assertEqual(result["extraction_status"], "human_review_required")
        public_data = json.loads(Path(result["public_result_path"]).read_text(encoding="utf-8"))

        self.assertEqual(public_data["status"], "human_review_required")
        field = public_data["fields"]["invoice_number"]
        self.assertIsNone(field["value"])
        self.assertIsNone(field["ai_candidate"])
        self.assertEqual(field["resolution"], "unresolved")
        self.assertTrue(field["review_required"])
        self.assertFalse(public_data["safety"]["ai_candidate_auto_accepted"])
        self.assertFalse(public_data["safety"]["automatic_accounting_approval"])
        self.assertFalse(public_data["accounting_approval_performed"])

    def test_case_e3_enabled_vlm_with_layout_blocker_publishes_review(self):
        """Explicit permission survives a blocked route without inventing blockers."""
        modified_doc = json.loads(json.dumps(self.doc_dict))
        modified_doc["texts"] = [t for t in modified_doc["texts"]
            if not t.get("text", "").lower().startswith("invoice number:")]
        with patch.object(dis, "run_attempt") as ai:
            result = execute_document_extraction(document_id="DOC-BLOCKED-PUBLIC",
                file_path=self.pdf_path, project_root=self.root,
                parser=make_injected_parser(modified_doc), allow_vlm_inference=True)
        ai.assert_not_called()
        self.assertEqual(result["execution_status"], "EXTRACTED")
        self.assertEqual(result["extraction_status"], "human_review_required")
        self.assertTrue(result["review_required"])
        self.assertFalse(result["accounting_approval_performed"])

        public=json.loads(Path(result['public_result_path']).read_text())
        blockers={r['code'] for r in public['routing']['blocking_reasons']}
        self.assertIn('SOURCE_ISSUE_REQUIRES_REVIEW:LAYOUT_LINE_RECOVERY',blockers)
        self.assertNotIn('VLM_INFERENCE_DISABLED',blockers)

    def test_case_f_missing_unresolved_fields_require_review(self):
        """Case F: Missing/unresolved fields preserve review-required status."""
        parser = make_injected_parser(self.doc_dict)
        result = execute_document_extraction(
            document_id="DOC-REV-001",
            file_path=self.pdf_path,
            project_root=self.root,
            parser=parser,
        )
        self.assertTrue(result["review_required"])
        self.assertFalse(result["accounting_approval_performed"])

    def test_case_g_parser_or_contract_failure_returns_genuine_failure(self):
        """Case G: Infrastructure and structural failures return error status, never an approved result."""
        # 1. Non-existent file -> INFRASTRUCTURE_ERROR
        missing_file = self.root / "non_existent.pdf"
        res1 = execute_document_extraction("DOC-ERR-001", missing_file, project_root=self.root)
        self.assertEqual(res1["execution_status"], "INFRASTRUCTURE_ERROR")
        self.assertIn("not found", res1["error"])
        self.assertFalse(res1["accounting_approval_performed"])

        # 2. Blank document_id -> STRUCTURAL_FAILURE
        res2 = execute_document_extraction("   ", self.pdf_path, project_root=self.root)
        self.assertEqual(res2["execution_status"], "STRUCTURAL_FAILURE")
        self.assertIn("non-empty", res2["error"])

        # 3. Crashing parser -> INFRASTRUCTURE_ERROR
        def crashing_parser(*args, **kwargs):
            raise RuntimeError("Parser process out of memory")

        res3 = execute_document_extraction(
            "DOC-CRASH-001",
            self.pdf_path,
            project_root=self.root,
            parser=crashing_parser,
        )
        self.assertEqual(res3["execution_status"], "INFRASTRUCTURE_ERROR")
        self.assertIn("RuntimeError", res3["error"])

    def test_case_h_offline_safety(self):
        """Case H: Execution runs offline without socket or external service access."""
        parser = make_injected_parser(self.doc_dict)
        # Block network access via mock
        with patch("socket.socket") as mock_socket:
            result = execute_document_extraction(
                document_id="DOC-OFFLINE-001",
                file_path=self.pdf_path,
                project_root=self.root,
                parser=parser,
            )
            self.assertEqual(result["execution_status"], "EXTRACTED")
            mock_socket.assert_not_called()

    def test_case_k_adapter_accepts_complete_and_blocks_unresolved(self):
        """Case K: Downstream adapt_m2_to_m3 accepts complete invoice and blocks unresolved.

        Demonstrates that Rachel's downstream step can safely adapt the published extraction
        artifact without coupling the extraction worker to M3 validation.
        """
        parser = make_injected_parser(self.doc_dict)
        res = execute_document_extraction(
            document_id="DOC-ADAPT-001",
            file_path=self.pdf_path,
            project_root=self.root,
            parser=parser,
        )
        self.assertEqual(res["execution_status"], "EXTRACTED")
        public_data = json.loads(Path(res["public_result_path"]).read_text(encoding="utf-8"))

        # 1. Complete invoice adapts to READY_FOR_M3
        adapted = adapt_m2_to_m3(public_data)
        self.assertEqual(adapted["status"], "READY_FOR_M3")
        self.assertEqual(len(adapted["structural_issues"]), 0)
        m3_input = adapted["m3_input"]
        self.assertEqual(m3_input["vendor_name"], "Example Stationery (Synthetic)")
        self.assertEqual(m3_input["invoice_number"], "SYN-001")
        self.assertEqual(m3_input["currency"], "MYR")
        self.assertEqual(m3_input["total_amount"], Decimal("50.00"))

        # 2. Incomplete invoice (e.g. unresolved invoice_number) is blocked
        incomplete_public = json.loads(json.dumps(public_data))
        incomplete_public["fields"]["invoice_number"] = {
            "raw_text": None,
            "value": None,
            "state": "missing",
            "method": None,
            "evidence": [],
            "candidates": [],
            "ai_candidate": "INV-AI-99999",
            "resolution": "candidate_for_review",
            "review_required": True,
        }
        blocked = adapt_m2_to_m3(incomplete_public)
        self.assertEqual(blocked["status"], "STRUCTURAL_REVIEW_REQUIRED")
        self.assertIsNone(blocked["m3_input"])
        issue_fields = [iss["field"] for iss in blocked["structural_issues"]]
        self.assertIn("invoice_number", issue_fields)

    def test_case_l1_custom_models_dir_fails_closed_when_missing(self):
        """Case L1: Providing a non-existent models_dir fails closed with clear error."""
        missing_models = self.root / "nonexistent_models_dir"
        result = execute_document_extraction(
            document_id="DOC-FAIL-MODELS",
            file_path=self.pdf_path,
            project_root=self.root,
            models_dir=missing_models,
        )
        self.assertEqual(result["execution_status"], "INFRASTRUCTURE_ERROR")
        self.assertIn("Docling model directory not found", result["error"])

    def test_case_l2_models_dir_forwarding(self):
        """Case L2: models_dir parameter is forwarded through execution boundary to parser."""
        received_kwargs = {}

        def mock_parser_with_models(
            input_pdf, output_parent, project_root=None, verbose=False, models_dir=None
        ):
            received_kwargs["models_dir"] = models_dir
            output = output_parent / f"docling-test-{Path(input_pdf).stem}"
            output.mkdir(parents=True, exist_ok=True)
            (output / "document.json").write_text(
                json.dumps(self.doc_dict), encoding="utf-8"
            )
            (output / "run.json").write_text(
                json.dumps({
                    "status": "success",
                    "mode": "test_injected_parser",
                    "source": str(input_pdf),
                    "source_sha256": hashlib.sha256(Path(input_pdf).read_bytes()).hexdigest(),
                }),
                encoding="utf-8",
            )
            return output, {"status": "success"}

        custom_models = self.root / "custom_models"
        custom_models.mkdir()

        result = execute_document_extraction(
            document_id="DOC-MODELS-FWD",
            file_path=self.pdf_path,
            project_root=self.root,
            parser=mock_parser_with_models,
            models_dir=custom_models,
        )
        self.assertEqual(result["execution_status"], "EXTRACTED")
        self.assertEqual(
            received_kwargs.get("models_dir"),
            custom_models.resolve(),
        )

    def linked_source(self, suffix='.png', frames=1, orientation=1):
        """Actual conversion with explicitly injected OCR; no accuracy claim."""
        from PIL import Image
        import pypdfium2 as pdfium
        source=self.root/('original'+suffix)
        image=Image.new('RGB',(1240,1754),'white')
        try:
            if suffix in {'.tif','.tiff'}:
                image.save(source,save_all=True,append_images=[image.copy() for _ in range(frames-1)])
            else:
                exif=image.getexif();exif[274]=orientation;image.save(source,exif=exif)
        finally:image.close()
        doc=deepcopy(self.doc_dict)
        for item in doc['texts']:
            if item['text'].startswith('Date:'):item['text']='Date: 04/05/2025'
        def parser(**kwargs):
            with pdfium.PdfDocument(kwargs['input_pdf']) as pdf:
                doc['pages']={}
                for index in range(len(pdf)):
                    page=pdf[index]
                    try:width,height=page.get_size()
                    finally:page.close()
                    doc['pages'][str(index+1)]={'size':{'width':width,'height':height}}
            return make_injected_parser(doc)(**kwargs)
        with patch.object(dis,'run_attempt') as ai:
            result=execute_document_extraction('DOC-LINK',source,project_root=self.root,parser=parser)
        ai.assert_not_called();self.assertEqual(result['execution_status'],'EXTRACTED',result)
        public=json.loads(Path(result['public_result_path']).read_text())
        service=next(p for p in (self.root/'runs').glob('document-intelligence-*/service-result.json')
                     if p.parent.name==result['service_run_id'])
        summary=json.loads(service.read_text());ext=json.loads(Path(summary['artifacts']['classified_extraction']).read_text())
        record=json.loads(Path(ext['provenance']['source_transformation']['path']).read_text())
        return source,ext,record,public

    def test_new_image_provenance_survives_temp_cleanup_and_allows_read_only_plan(self):
        from services.extraction.ai.run_selective_qwen import verified_pdf
        from services.extraction.classification.route_extraction import plan_field_recovery
        source,ext,record,public=self.linked_source()
        pdf,data=verified_pdf(ext,self.root,source_path=source,source_access_authorized=True)
        self.assertTrue(pdf.is_file());self.assertNotEqual(record['original']['sha256'],record['parser_input']['sha256'])
        self.assertEqual(hashlib.sha256(data).hexdigest(),record['parser_input']['sha256'])
        self.assertEqual(record['pages'][0]['mapping_origin'],'TOPLEFT')
        self.assertEqual(list((self.root/'.extraction-tmp').iterdir()),[])
        plan=plan_field_recovery(ext,self.root,source_path=source,source_access_authorized=True,
                                 model_configuration={'model':'qwen2.5vl:7b'})
        self.assertTrue(plan['source_verified']);self.assertTrue(plan['fields']['invoice_date']['eligible'])
        self.assertFalse(plan['inference_authorized']);self.assertFalse(plan['ai_call_performed'])
        self.assertTrue(public['review_required']);self.assertFalse(public['accounting_approval_performed'])
        self.assertNotIn('source_transformation',public['provenance'])
        self.assertNotIn(str(source),json.dumps(public));self.assertNotIn('parser-input.private.pdf',json.dumps(public))

    def test_new_source_authorization_identity_and_tampering_fail_closed(self):
        from services.extraction.ai.run_selective_qwen import verified_pdf
        source,ext,record,_=self.linked_source()
        for authorized,path in ((False,source),(True,None)):
            with self.subTest(authorized=authorized,path=bool(path)),self.assertRaises(ValueError):
                verified_pdf(ext,self.root,source_path=path,source_access_authorized=authorized)
        for key in ('document_id','extraction_run_id'):
            changed=deepcopy(ext);changed[key]='wrong-identity'
            with self.subTest(key=key),self.assertRaises(ValueError):
                verified_pdf(changed,self.root,source_path=source,source_access_authorized=True)
        original=source.read_bytes();source.write_bytes(original+b'changed')
        with self.assertRaises(ValueError):verified_pdf(ext,self.root,source_path=source,source_access_authorized=True)
        source.write_bytes(original)
        derived=Path(record['parser_input']['path']);data=derived.read_bytes();derived.write_bytes(data+b'changed')
        with self.assertRaises(ValueError):verified_pdf(ext,self.root,source_path=source,source_access_authorized=True)
        derived.write_bytes(data);source.unlink()
        with self.assertRaises(OSError):verified_pdf(ext,self.root,source_path=source,source_access_authorized=True)

    def test_conversion_record_hash_and_geometry_changes_are_rejected(self):
        from services.extraction.ai.run_selective_qwen import verified_pdf
        source,ext,record,_=self.linked_source();path=Path(ext['provenance']['source_transformation']['path'])
        for change in ('metadata_hash','resolution','derived_hash','page_mapping','ocr_geometry'):
            with self.subTest(change=change),self.assertRaises(ValueError):
                altered=deepcopy(record);payload=deepcopy(ext)
                if change in {'metadata_hash','resolution'}:altered['conversion']['pdf_resolution_dpi']=151
                elif change=='derived_hash':altered['parser_input']['sha256']='0'*64
                elif change=='page_mapping':altered['pages'][0]['parser_page']=2
                else:altered['pages'][0]['pdf_size_points'][0]+=1
                path.write_text(json.dumps(altered))
                if change!='metadata_hash':payload['provenance']['source_transformation']['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
                verified_pdf(payload,self.root,source_path=source,source_access_authorized=True)

    def test_image_formats_exif_and_multiframe_page_mapping(self):
        from services.extraction.ai.run_selective_qwen import verified_pdf,render_single_page
        for suffix,frames,orientation in (('.jpg',1,6),('.jpeg',1,1),('.png',1,1),('.tif',2,1),('.tiff',2,1)):
            with self.subTest(suffix=suffix):
                source,ext,record,_=self.linked_source(suffix,frames,orientation)
                _,data=verified_pdf(ext,self.root,source_path=source,source_access_authorized=True)
                self.assertEqual(len(record['pages']),frames)
                self.assertEqual([p['source_frame_index'] for p in record['pages']],list(range(frames)))
                self.assertEqual(record['pages'][0]['source_orientation'],orientation)
                if frames>1:
                    with self.assertRaisesRegex(ValueError,'exactly one'):render_single_page(data,self.root/'no.png')

    def test_original_pdf_needs_no_retained_duplicate_and_authentication_is_separate(self):
        from services.extraction.ai.run_selective_qwen import verified_pdf
        result=execute_document_extraction('DOC-PDF-LINK',self.pdf_path,project_root=self.root,
                                            parser=make_injected_parser(self.doc_dict))
        self.assertEqual(result['execution_status'],'EXTRACTED',result)
        service=json.loads((self.root/'runs'/result['service_run_id']/'service-result.json').read_text())
        ext=json.loads(Path(service['artifacts']['classified_extraction']).read_text())
        self.assertFalse((self.root/'runs'/result['service_run_id']/'parser-input.private.pdf').exists())
        path,data=verified_pdf(ext,self.root,source_path=self.pdf_path,source_access_authorized=True)
        self.assertEqual(path,self.pdf_path);self.assertEqual(data,self.pdf_path.read_bytes())
        with self.assertRaises(ValueError):verified_pdf(ext,self.root,source_path=self.pdf_path)
