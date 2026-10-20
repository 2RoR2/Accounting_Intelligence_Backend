import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "project" / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from services.extraction.module2_public_result import build_public_result
from services.extraction.reconcile_selective import reconcile
from services.extraction.route_extraction import decide_route
from .test_route_extraction import fixture


def sha256(path: Path) -> str:
    return hashlib.sha256(
        path.read_bytes()
    ).hexdigest()


class Module2PublicResultTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        self.runs = self.root / "runs"
        self.runs.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def make_extraction(
        self,
        value,
        state,
        run_id="extraction-test",
    ):
        directory = self.runs / run_id
        directory.mkdir()

        path = directory / "result.json"

        extraction = fixture()
        extraction.update(document_id="DOC-TEST", extraction_run_id=run_id)
        extraction["fields"]["invoice_number"].update(
            raw_text=value, value=value, state=state,
            legacy_mapping_status="mapped" if state == "resolved" else state,
        )
        if state != "resolved":
            extraction["fields"]["invoice_number"]["evidence"] = []

        path.write_text(
            json.dumps(extraction),
            encoding="utf-8",
        )

        return path, extraction

    def make_service(
        self,
        extraction_path,
        status,
        decision,
        reconciliation_path=None,
    ):
        directory = self.runs / "service-test"
        directory.mkdir(exist_ok=True)

        path = directory / "service-result.json"

        artifacts = {
            "classified_extraction": str(
                extraction_path.resolve()
            ),
        }

        extraction = json.loads(extraction_path.read_text())
        routing = decide_route(extraction)
        routing.update(source_result_path=str(extraction_path.resolve()),
                       source_result_sha256=sha256(extraction_path))
        routing_path = directory / "routing.json"
        routing_path.write_text(json.dumps(routing), encoding="utf-8")
        artifacts["routing_result"] = str(routing_path)
        reconciliation = None
        ai = {"attempted": False, "response_received": False,
              "status": "skipped_" + decision, "prediction": None}

        if reconciliation_path is not None:
            artifacts[
                "reconciliation_result"
            ] = str(
                reconciliation_path.resolve()
            )
            reconciled = json.loads(reconciliation_path.read_text())
            artifacts["selective_run"] = str(self.runs / reconciled["selective_run_id"])
            reconciliation = {key: reconciled[key] for key in ("status", "review_required", "fields")}
            ai = {"attempted": True, "response_received": True,
                  "status": "response_structure_valid_needs_review",
                  "prediction": {key: field["vlm"]["candidate"] for key, field in reconciled["fields"].items()}}
        elif status == "ai_attempt_failed_needs_review":
            ai = {"attempted": True, "response_received": False, "status": "failed", "prediction": None}

        service = {
            "schema_version": "0.1.0-proposed",
            "service_run_id": "service-test",
            "document_id": "DOC-TEST",
            "status": status,
            "review_required": True,
            "accounting_approval_performed": False,
            "artifacts": artifacts,
            "document_type": extraction["document_type"],
            "routing": {key: routing[key] for key in
                        ("decision", "requested_fields", "candidate_reasons", "blocking_reasons")},
            "ai_assistance": ai,
            "reconciliation": reconciliation,
        }

        path.write_text(
            json.dumps(service),
            encoding="utf-8",
        )

        return path

    def make_reconciliation(
        self,
        extraction_path,
        candidate,
        source_hash=None,
    ):
        extraction = json.loads(extraction_path.read_text())
        directory = self.runs / "selective-test"
        directory.mkdir()
        routing = decide_route(extraction)
        run = {"status": "response_structure_valid_needs_review", "document_id": "DOC-TEST",
               "decision": "vlm_candidate", "ai_request_attempted": True, "ai_response_received": True,
               "source_result_path": str(extraction_path.resolve()), "source_result_sha256": sha256(extraction_path),
               "requested_fields": routing["requested_fields"], "model": "qwen2.5vl:7b"}
        for name, payload in (("run.json", run), ("routing.json", routing),
                              ("prediction.json", {"invoice_number": candidate})):
            (directory / name).write_text(json.dumps(payload), encoding="utf-8")
        path, reconciliation = reconcile(extraction_path, directory, self.runs)
        if source_hash is not None:
            reconciliation["source_extraction_sha256"] = source_hash
            path.write_text(json.dumps(reconciliation), encoding="utf-8")
        return path

    def test_parser_only_public_result(self):
        extraction_path, _ = (
            self.make_extraction(
                value="SYN-001",
                state="resolved",
            )
        )

        service_path = self.make_service(
            extraction_path,
            status="parser_only_needs_review",
            decision="parser_only",
        )

        _, result = build_public_result(
            service_path,
            project_root=self.root,
        )

        field = result["fields"][
            "invoice_number"
        ]

        self.assertEqual(
            field["value"],
            "SYN-001",
        )

        self.assertEqual(
            field["resolution"],
            "parser_resolved",
        )

        self.assertIsNone(
            field["ai_candidate"]
        )

        self.assertFalse(
            result["safety"][
                "ai_candidate_auto_accepted"
            ]
        )

        self.assertFalse(
            result["safety"][
                "automatic_accounting_approval"
            ]
        )

    def test_ai_candidate_remains_separate(self):
        extraction_path, _ = (
            self.make_extraction(
                value=None,
                state="missing",
            )
        )

        reconciliation_path = (
            self.make_reconciliation(
                extraction_path,
                candidate="INV-RECOVER-001",
            )
        )

        service_path = self.make_service(
            extraction_path,
            status="candidate_for_review",
            decision="vlm_candidate",
            reconciliation_path=(
                reconciliation_path
            ),
        )

        _, result = build_public_result(
            service_path,
            project_root=self.root,
        )

        field = result["fields"][
            "invoice_number"
        ]

        self.assertIsNone(
            field["value"]
        )

        self.assertEqual(
            field["ai_candidate"],
            "INV-RECOVER-001",
        )

        self.assertEqual(
            field["resolution"],
            "candidate_for_review",
        )

        self.assertEqual(
            result["provenance"][
                "selective_run_id"
            ],
            "selective-test",
        )

        self.assertNotIn(
            "reconciliation_run_id",
            result["provenance"],
        )

        self.assertEqual(
            result["provenance"][
                "reconciliation_result_sha256"
            ],
            sha256(
                reconciliation_path
            ),
        )

    def test_null_ai_candidate_remains_unresolved(self):
        extraction_path, _ = (
            self.make_extraction(
                value=None,
                state="missing",
            )
        )

        reconciliation_path = (
            self.make_reconciliation(
                extraction_path,
                candidate=None,
            )
        )

        service_path = self.make_service(
            extraction_path,
            status="unresolved_needs_review",
            decision="vlm_candidate",
            reconciliation_path=(
                reconciliation_path
            ),
        )

        _, result = build_public_result(
            service_path,
            project_root=self.root,
        )

        field = result["fields"][
            "invoice_number"
        ]

        self.assertIsNone(
            field["value"]
        )

        self.assertIsNone(
            field["ai_candidate"]
        )

        self.assertEqual(
            field["resolution"],
            "unresolved_missing",
        )

    def test_reconciliation_hash_mismatch_is_rejected(self):
        extraction_path, _ = (
            self.make_extraction(
                value=None,
                state="missing",
            )
        )

        reconciliation_path = (
            self.make_reconciliation(
                extraction_path,
                candidate="INV-RECOVER-001",
                source_hash="0" * 64,
            )
        )

        service_path = self.make_service(
            extraction_path,
            status="candidate_for_review",
            decision="vlm_candidate",
            reconciliation_path=(
                reconciliation_path
            ),
        )

        with self.assertRaisesRegex(
            ValueError,
            "SHA-256",
        ):
            build_public_result(
                service_path,
                project_root=self.root,
            )


if __name__ == "__main__":
    unittest.main()
