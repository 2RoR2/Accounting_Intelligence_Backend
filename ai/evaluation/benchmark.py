"""Offline, frozen Module 2 development benchmark; never calls application APIs.

Run with an isolated CPU Python: benchmark.py run --ocr-models PATH.
Outputs are private/ignored; evaluate accepts only a sealed completed prediction run.
The worker receives source metadata, never reference labels. No extractor is patched.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
import hashlib
import importlib.abc
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time as clock
import traceback
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
AI = ROOT / "ai"
VERSION = "module2-offline-baseline/1.1.0"
ORIGINAL_POLICY = "typed-exact-decimal-text-nfkc-whitespace-casefold/1"
ADDRESS_POLICY = "address-comma-line-separators/1"
POLICY = "dual-original-v1-address-separators-v1/2"
ADDRESS_FIELDS = {"supplier_address", "customer_address", "provider_address", "merchant_address",
                  "billing_address", "shipping_address", "service_address"}
ROW_POLICY = "maximum-weight-one-to-one-desc-ge-0.5-or-two-numeric/1"
sys.dont_write_bytecode = True
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Evaluation aliases only: no amount arithmetic, totals substitution or field recovery.
# Source family is catalog metadata, not the predicted classification or label values.
COMMON = {"tax_amount": "tax", "total_amount": "total", "buyer_name": "customer_name"}
ALIASES = {
    "invoice": {**COMMON, "document_allowances": "allowance_total",
                "document_charges": "charge_total", "total_excluding_tax": "tax_exclusive_total",
                "total_including_tax": "tax_inclusive_total", "rounding_amount": "rounding"},
    "bill": {**COMMON, "issuer": "provider_name", "supplier_name": "provider_name",
             "invoice_number": "bill_number", "bill_reference": "bill_number",
             "invoice_date": "bill_date", "account_number": "customer_account_number"},
    "receipt": {**COMMON, "merchant": "merchant_name"},
}
COUNTS = {"invoice": 15, "bill": 10, "receipt": 10}
PREFIXES = {"invoice": "INV", "bill": "BIL", "receipt": "REC"}
OUTCOMES = ("correct", "incorrect", "missing", "correct_abstention", "false_fill", "excluded")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, value, *, freeze=True):
    """Exclusive creation; prediction artifacts are never updated or reused."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)
        handle.write("\n")
    if freeze:
        path.chmod(0o444)
    return digest(path)


def within(path, root):
    path = Path(path).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError("PATH_OUTSIDE_ALLOWED_ROOT")
    return path


class OfflineImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + ".") for p in (
            "main", "database", "security", "validation", "services.queue",
            "services.routes", "services.notifications",
        )):
            raise ImportError("OFFLINE_FORBIDDEN_IMPORT:" + fullname)
        return None


def install_boundary(output, *, worker=False):
    """Deny sockets, secrets, writes outside the run, and worker label access."""
    output = Path(output).resolve()
    sys.meta_path.insert(0, OfflineImports())
    facts = {"network_attempts": 0, "secret_read_attempts": 0,
             "ground_truth_read_attempts": 0, "forbidden_write_attempts": 0,
             "forbidden_imports_loaded": [], "worker_subprocess_attempts": 0, "blocked_events": []}

    def blocked(event, detail):
        if len(facts["blocked_events"]) < 30:
            facts["blocked_events"].append({"event": event, "detail": detail,
                "stack": [f"{f.filename}:{f.lineno}:{f.name}" for f in traceback.extract_stack(limit=10)]})

    def audit(event, args):
        # Optional dependency find_spec probes are harmless; actual service imports are denied.
        if event == "import" and str(args[0]).split(".")[0] in {"psycopg", "redis", "rq", "boto3"}:
            blocked(event, str(args[0]))
            raise ImportError("OFFLINE_SERVICE_IMPORT_DENIED:" + str(args[0]))
        if event.startswith("socket.") and event in {
            "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
            "socket.sendto", "socket.gethostbyaddr",
        }:
            facts["network_attempts"] += 1
            blocked(event, "socket operation denied")
            raise PermissionError("OFFLINE_NETWORK_DENIED")
        if event == "subprocess.Popen" and worker:
            facts["worker_subprocess_attempts"] += 1
            blocked(event, "subprocess denied")
            raise PermissionError("OFFLINE_SUBPROCESS_DENIED")
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.name == ".env" or path.name.startswith(".env."):
                facts["secret_read_attempts"] += 1
                raise PermissionError("OFFLINE_ENV_FILE_DENIED")
            if worker and any(path.is_relative_to(AI / name) for name in (
                "manifests", "annotations", "integration", "provenance", "schemas",
            )):
                facts["ground_truth_read_attempts"] += 1
                raise PermissionError("WORKER_REFERENCE_READ_DENIED")
            mode, flags = args[1:3]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and path != Path(os.devnull).resolve() and not path.is_relative_to(output):
                facts["forbidden_write_attempts"] += 1
                blocked(event, str(path))
                raise PermissionError("OFFLINE_WRITE_OUTSIDE_RUN")
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.rename"}:
            for item in args[:2] if event == "os.rename" else args[:1]:
                if isinstance(item, (str, bytes, os.PathLike)) and not Path(os.fsdecode(item)).resolve().is_relative_to(output):
                    facts["forbidden_write_attempts"] += 1
                    blocked(event, str(item))
                    raise PermissionError("OFFLINE_MUTATION_OUTSIDE_RUN")

    sys.addaudithook(audit)
    return facts


def child_environment(output, ocr_models):
    """Allowlist only; never copy credentials or configuration from the parent."""
    output = Path(output).resolve()
    return {
        "PATH": os.defpath, "HOME": str(output / "cache"),
        "TMPDIR": str(output / "tmp"), "XDG_CACHE_HOME": str(output / "cache"),
        "HF_HOME": str(output / "cache" / "huggingface"), "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYTHON_DOTENV_DISABLED": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1", "TOKENIZERS_PARALLELISM": "false",
        "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "4", "MKL_NUM_THREADS": "4",
        "OPENBLAS_NUM_THREADS": "4", "ENABLE_IMAGE_OCR": "false",
        "RAPIDOCR_MODELS_DIR": str(Path(ocr_models).resolve()), "OCR_LANGUAGE": "en",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "AWS_EC2_METADATA_DISABLED": "true", "DO_NOT_TRACK": "1",
    }


def versions():
    names = ("docling", "docling-slim", "docling-core", "docling-ibm-models", "docling-parse",
             "rapidocr", "onnxruntime", "Pillow", "torch", "torchvision", "pydantic", "pypdf",
             "pypdfium2", "python-dotenv", "scipy", "transformers", "pytest")
    result = {"python": platform.python_version(), "platform": platform.platform(),
              "cpu_count": os.cpu_count(), "executable": sys.executable}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def model_inventory(models, ocr_models):
    paths = sorted(Path(models).rglob("*.safetensors")) + sorted(Path(models).rglob("*.onnx"))
    paths += [Path(ocr_models) / n for n in (
        "PP-OCRv6_det_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx", "PP-OCRv6_rec_small.onnx")]
    if len(paths) < 7:
        raise ValueError("LOCAL_MODEL_INVENTORY_INCOMPLETE")
    result = []
    for path in paths:
        if not path.is_file() or path.stat().st_size < 1024:
            raise ValueError("LOCAL_MODEL_MISSING:" + path.name)
        with path.open("rb") as handle:
            if handle.read(80).startswith(b"version https://git-lfs"):
                raise ValueError("MODEL_IS_LFS_POINTER:" + path.name)
        result.append({"path": str(path.resolve()), "bytes": path.stat().st_size, "sha256": digest(path)})
    return result


def verified_sources(output):
    """Read published metadata and verify originals; only verified bytes are staged."""
    from ai.evaluation.demo_catalog import DemoCatalog
    from ai.evaluation.dataset_contract import load_manifest
    from PIL import Image
    from pypdf import PdfReader

    catalog = DemoCatalog.model_validate_json((AI / "demo_catalog.json").read_text(encoding="utf-8"))
    manifests = [load_manifest(AI / "manifests" / name) for name in ("invoices.json", "bills.json", "receipts.json")]
    samples = {s.sample_id: s for manifest in manifests for s in manifest.samples}
    if set(samples) != {e.sample_id for e in catalog.samples}:
        raise ValueError("CATALOG_GOLD_IDENTITY_MISMATCH")
    sources = []
    for entry in catalog.samples:
        item = entry.model_dump(mode="json")
        path = within(AI / entry.relative_asset_path, AI / "assets")
        item.update(verified=False, blocked_reason=None, staging_method="working_tree")
        try:
            data = path.read_bytes()
            item["working_tree_sha256"] = hashlib.sha256(data).hexdigest()
            if item["working_tree_sha256"] != entry.sha256:
                data = subprocess.check_output(["git", "show", "HEAD:ai/" + entry.relative_asset_path], cwd=ROOT)
                if hashlib.sha256(data).hexdigest() != entry.sha256:
                    raise ValueError("SOURCE_HASH_MISMATCH_IN_WORKTREE_AND_GIT")
                item["staging_method"] = "verified_git_blob"
            if hashlib.sha256(data).hexdigest() != entry.sha256:
                raise ValueError("SOURCE_HASH_MISMATCH")
            if path.suffix == ".pdf":
                if not data.startswith(b"%PDF-"):
                    raise ValueError("PDF_SIGNATURE")
                pdf = PdfReader(io.BytesIO(data), strict=True)
                if pdf.is_encrypted or len(pdf.pages) == 0:
                    raise ValueError("PDF_UNREADABLE")
                sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in pdf.pages]
            else:
                if not data.startswith(b"\xff\xd8\xff"):
                    raise ValueError("JPEG_SIGNATURE")
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    image.load()
                    sizes = [(float(image.width), float(image.height))]
            sample = samples[entry.sample_id]
            if sample.source.sha256 != entry.sha256 or sample.source.relative_path != entry.relative_asset_path:
                raise ValueError("CATALOG_GOLD_SOURCE_MISMATCH")
            if sample.source.page_count is not None and sample.source.page_count != len(sizes):
                raise ValueError("PAGE_COUNT_MISMATCH")
            if any((p.width, p.height) != sizes[p.page - 1] for p in sample.source.pages):
                raise ValueError("PAGE_GEOMETRY_MISMATCH")
            staged = output / "sources" / (entry.demo_code + path.suffix)
            staged.parent.mkdir(parents=True, exist_ok=True)
            with staged.open("xb") as handle:
                handle.write(data)
            staged.chmod(0o444)
            item.update(verified=True, source_path=str(staged), pages=len(sizes))
        except Exception as error:
            item["blocked_reason"] = type(error).__name__ + ":" + str(error)
        sources.append(item)
    if Counter(s["document_type"] for s in sources) != COUNTS:
        raise ValueError("INCORRECT_DATASET_COUNTS")
    return catalog, sources


def worker(job_path):
    # Only this explicit source job is readable; no catalog or gold is passed.
    job = read_json(job_path)
    output = Path(job["output"]).resolve()
    facts = install_boundary(output, worker=True)
    source = Path(job["source_path"])
    if digest(source) != job["sha256"]:
        raise ValueError("STAGED_SOURCE_CHANGED")
    begin = clock.perf_counter()
    native = None
    execution = {}
    try:
        from services.extraction.workflow.execution import execute_document_extraction
        pipeline_begin = clock.perf_counter()
        execution = execute_document_extraction(
            document_id=job["demo_code"], file_path=source,
            project_root=Path(job["project_root"]), models_dir=Path(job["models"]),
            allow_vlm_inference=False,
        )
        pipeline_seconds = clock.perf_counter() - pipeline_begin
        if execution.get("public_result_path"):
            native = read_json(execution["public_result_path"])
        status = "SUCCEEDED" if execution.get("execution_status") == "EXTRACTED" and native is not None else "FAILED"
    except Exception as error:
        pipeline_seconds = None
        status = "FAILED"
        execution = {"execution_status": "UNCAUGHT_RUNTIME_ERROR", "error": type(error).__name__ + ":" + str(error)}
    facts["forbidden_imports_loaded"] = [n for n in sys.modules if n in {
        "database.connection", "services.queue.jobs", "validation.documents.document_validation", "main"}]
    artifacts = {}
    project = Path(job["project_root"])
    if project.exists():
        for path in sorted(project.rglob("*")):
            if path.is_file():
                artifacts[str(path.relative_to(output))] = digest(path)
                path.chmod(0o444)
    record = {
        "schema_version": "1.0.0", "artifact_type": "offline_native_prediction_envelope",
        "benchmark_id": job["demo_code"], "identity_kind": "LOCAL_DEMO_ALIAS_NOT_DATABASE_UUID",
        "demo_code": job["demo_code"], "sample_id": job["sample_id"], "source_sha256": job["sha256"],
        "source_kind": job["source_kind"], "origin": job["origin"], "benchmark_split": job["benchmark_split"],
        "attempted": True, "status": status, "captured_at": now(), "execution": execution,
        "runtime": versions(), "worker_seconds": clock.perf_counter() - begin,
        "pipeline_seconds": pipeline_seconds, "native_result": native,
        "native_artifact_hashes": artifacts, "boundary": facts,
        "qwen_enabled": False, "database_persistence": False, "accounting_validation": False,
    }
    write_new(output / "predictions" / (job["demo_code"] + ".json"), record)


def verify_frozen(output):
    output = Path(output)
    seal = read_json(output / "prediction-seal.json")
    for name, expected in seal["files"].items():
        path = within(output / name, output)
        if digest(path) != expected:
            raise ValueError("FROZEN_PREDICTION_CHANGED:" + name)
    return seal


def run(output, models, ocr_models, timeout):
    output = within(output, AI / "runs")
    if not output.name.startswith("benchmark_v1_"):
        raise ValueError("EXPECTED_BENCHMARK_V1_RUN_DIRECTORY")
    output.mkdir(parents=True, exist_ok=False)
    for name in ("cache", "tmp", "predictions", "jobs", "logs", "documents"):
        (output / name).mkdir()
    facts = install_boundary(output)
    # Parent is offline too. Environment changes affect this private process only.
    os.environ.clear()
    os.environ.update(child_environment(output, ocr_models))
    catalog, sources = verified_sources(output)
    extraction_files = sorted((ROOT / "services" / "extraction").rglob("*.py"))
    config = {
        "version": VERSION, "comparison_policy": POLICY, "row_policy": ROW_POLICY,
        "aliases": ALIASES, "created_at": now(), "runtime": versions(),
        "models": model_inventory(models, ocr_models), "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_code_hashes": {str(p.relative_to(ROOT)): digest(p) for p in extraction_files},
        "benchmark_code_sha256": digest(__file__),
        "ground_truth_hashes": {p.name: digest(p) for p in sorted((AI / "manifests").glob("*.json"))},
        "sources": sources, "timeout_seconds": timeout, "database_uploads": False,
        "timing_policy": "sequential fresh process per document; wall includes imports, model initialization, extraction and freezing",
        "project_root_policy": "per-document private subtree of ai/runs; explicit models outside subtree, all writes inside run",
    }
    write_new(output / "preflight.json", config)
    durations = {}
    for source in sources:
        code = source["demo_code"]
        if not source["verified"]:
            write_new(output / "predictions" / (code + ".json"), {
                "demo_code": code, "sample_id": source["sample_id"], "attempted": False,
                "status": "BLOCKED", "error": source["blocked_reason"], "native_result": None})
            print(code, "BLOCKED", flush=True)
            continue
        project = output / "documents" / code
        project.mkdir()
        job = {"output": str(output), "project_root": str(project), "models": str(Path(models).resolve()),
               "demo_code": code, "sample_id": source["sample_id"], "sha256": source["sha256"],
               "source_path": source["source_path"], "source_kind": source["document_type"],
               "origin": source["origin"], "benchmark_split": source["cohort"]["evaluation_split"]}
        job_path = output / "jobs" / (code + ".json")
        write_new(job_path, job)
        started = clock.perf_counter()
        failure = None
        log = output / "logs" / (code + ".log")
        with log.open("xb") as handle:
            try:
                result = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                    "worker", str(job_path)], cwd=ROOT, env=child_environment(output, ocr_models),
                    stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout, check=False)
                if result.returncode:
                    failure = "WORKER_EXIT_" + str(result.returncode)
            except subprocess.TimeoutExpired:
                failure = "DOCUMENT_TIMEOUT"
        durations[code] = clock.perf_counter() - started
        prediction = output / "predictions" / (code + ".json")
        if not prediction.exists():
            write_new(prediction, {**job, "attempted": True, "status": "FAILED", "native_result": None,
                "error": failure or "WORKER_DID_NOT_FREEZE_RESULT", "captured_at": now()})
        log.chmod(0o444)
        print(code, read_json(prediction)["status"], f"{durations[code]:.3f}s", flush=True)
    sealed = {}
    for path in sorted((output / "predictions").glob("*.json")):
        sealed[str(path.relative_to(output))] = digest(path)
        for name, expected in read_json(path).get("native_artifact_hashes", {}).items():
            sealed[name] = expected
    write_new(output / "prediction-seal.json", {"created_at": now(), "files": sealed,
        "evaluation_started": False, "benchmark_code_sha256": digest(__file__)})
    write_new(output / "run-manifest.json", {"dataset_id": catalog.dataset_id,
        "benchmark_revision": catalog.benchmark_revision, "durations_seconds": durations,
        "predictions": {s["demo_code"]: {"path": "predictions/" + s["demo_code"] + ".json",
            "sha256": digest(output / "predictions" / (s["demo_code"] + ".json")),
            "source_sha256": s["sha256"], "sample_id": s["sample_id"]} for s in sources}, "boundary": facts})
    verify_frozen(output)
    evaluate(output)


def normalise(value, value_type):
    if value is None:
        return None
    if value_type == "boolean":
        if type(value) is not bool:
            raise ValueError("INVALID_BOOLEAN")
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("INVALID_VALUE_TYPE")
    text = unicodedata.normalize("NFKC", value).strip()
    if value_type == "decimal":
        if not re.fullmatch(r"-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", text):
            raise ValueError("INVALID_DECIMAL")
        number = Decimal(text.replace(",", ""))
        if not number.is_finite():
            raise ValueError("INVALID_DECIMAL")
        return number  # exact numeric equality: no rounding or implicit zero
    if value_type == "date":
        return date.fromisoformat(text)
    if value_type == "time":
        return time.fromisoformat(text)
    if value_type == "currency":
        return text.upper()
    return " ".join(text.split()).casefold()


def canonical_cells(native_fields, kind, *, line=False):
    from ai.evaluation.field_dictionary import FIELDS
    grouped = defaultdict(list)
    unsupported = []
    for key, cell in native_fields.items():
        target = key if line else ALIASES[kind].get(key, key)
        if target not in FIELDS:
            unsupported.append(key)
            continue
        grouped[target].append((key, cell))
    cells, collisions = {}, []
    for key, members in grouped.items():
        if len(members) != 1:
            collisions.append({"field_key": key, "source_keys": [name for name, _ in members]})
            cells[key] = {"value": None, "state": "conflict", "evidence": []}
        else:
            cells[key] = members[0][1]
    return cells, collisions, unsupported


def selected(cell):
    return cell.get("value") if cell and cell.get("state") == "resolved" else None


def _strict_compare_cell(gold, cell):
    state = gold["label_state"]
    prediction = selected(cell)
    base = {"field_key": gold["field_key"], "value_type": gold["value_type"],
            "reference_state": state, "reference_value": gold["value"],
            "reference_normalized_value": gold["normalized_value"], "prediction_value": prediction,
            "prediction_state": cell.get("state") if cell else "not_emitted",
            "prediction_raw_text": cell.get("raw_text") if cell else None,
            "prediction_evidence": cell.get("evidence", []) if cell else []}
    if state in {"UNLABELLED", "AMBIGUOUS", "UNREADABLE"}:
        return {**base, "exact": "excluded", "normalized": "excluded", "exclusion_reason": state}
    if state == "CONFIRMED_ABSENT":
        outcome = "correct_abstention" if prediction is None else "false_fill"
        return {**base, "exact": outcome, "normalized": outcome, "exclusion_reason": None}
    if state != "PRESENT":
        raise ValueError("UNKNOWN_REFERENCE_STATE")
    if prediction is None:
        return {**base, "exact": "missing", "normalized": "missing", "exclusion_reason": None}
    exact = type(prediction) is type(gold["value"]) and prediction == gold["value"]
    try:
        normalized = normalise(prediction, gold["value_type"]) == normalise(
            gold["normalized_value"] if gold["normalized_value"] is not None else gold["value"], gold["value_type"])
    except (ValueError, TypeError, InvalidOperation):
        normalized = False
    return {**base, "exact": "correct" if exact else "incorrect",
            "normalized": "correct" if normalized else "incorrect", "exclusion_reason": None}


def normalise_address(value):
    """Address-only separator equivalence; never fuzzy content matching.

    In addition to original NFKC/case/whitespace rules, commas between address
    components equal a space or line break. Preserve commas embedded between
    digits, all other punctuation, word boundaries, digits and component order.
    This never splits joined OCR words, corrects spelling or changes predictions.
    """
    text = normalise(value, "text")
    if text is None:
        raise ValueError("MISSING_ADDRESS")
    return " ".join(re.sub(r"(?<!\d),|,(?!\d)", " ", text).split())


def compare_cell(gold, cell):
    result = _strict_compare_cell(gold, cell)
    outcome = result["normalized"]
    if (gold["field_key"] in ADDRESS_FIELDS and gold["value_type"] == "text"
            and gold["label_state"] == "PRESENT" and selected(cell) is not None):
        try:
            reference = gold["normalized_value"] if gold["normalized_value"] is not None else gold["value"]
            equal = normalise_address(selected(cell)) == normalise_address(reference)
        except (ValueError, TypeError):
            equal = False
        outcome = "correct" if equal else "incorrect"
    return {**result, "formatting_tolerant": outcome}


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def metrics(comparisons, mode):
    counts = Counter(c[mode] for c in comparisons)
    result = {key: counts[key] for key in OUTCOMES}
    present = counts["correct"] + counts["incorrect"] + counts["missing"]
    result.update(eligible_present=present, eligible_absent=counts["correct_abstention"] + counts["false_fill"],
        accuracy=ratio(counts["correct"], present),
        precision=ratio(counts["correct"], counts["correct"] + counts["incorrect"] + counts["false_fill"]),
        recall=ratio(counts["correct"], present),
        extraction_coverage=ratio(counts["correct"] + counts["incorrect"], present),
        unmeasurable_by_state=dict(Counter(c["exclusion_reason"] for c in comparisons if c[mode] == "excluded")))
    return result


def matched_rows(gold_rows, prediction_rows):
    """Maximum-weight bipartite matching with explicit unmatched dummy vertices."""
    from scipy.optimize import linear_sum_assignment
    n, m = len(gold_rows), len(prediction_rows)
    if not n or not m:
        return [], list(range(n)), list(range(m))
    weights = [[0.0] * (n + m) for _ in range(n + m)]
    for i, row in enumerate(gold_rows):
        gold = {c["field_key"]: c for c in row["cells"]}
        for j, prediction in enumerate(prediction_rows):
            desc = selected(prediction.get("description"))
            reference = gold.get("description", {})
            expected_desc = reference.get("normalized_value") or reference.get("value")
            similarity = SequenceMatcher(None, normalise(expected_desc, "text"), normalise(desc, "text")).ratio() if desc and expected_desc else 0
            equal_numeric = sum(compare_cell(gold[key], prediction.get(key))["normalized"] == "correct"
                for key in ("quantity", "unit_price", "line_total") if key in gold and gold[key]["label_state"] == "PRESENT")
            eligible = similarity >= 0.5 or equal_numeric >= 2
            weights[i][j] = similarity * 100 + equal_numeric * 10 + 0.0001 / (1 + abs(i - j)) if eligible else -1
    rows, cols = linear_sum_assignment(weights, maximize=True)
    pairs = [(int(i), int(j)) for i, j in zip(rows, cols) if i < n and j < m and weights[i][j] > 0]
    return pairs, [i for i in range(n) if i not in {p[0] for p in pairs}], [j for j in range(m) if j not in {p[1] for p in pairs}]


def latency(values):
    ordered = sorted(values)
    return {"sample_size": len(ordered), "mean": statistics.mean(ordered) if ordered else None,
            "median": statistics.median(ordered) if ordered else None,
            "p95_nearest_rank": ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None,
            "maximum": max(ordered) if ordered else None,
            "within_20_seconds": sum(v <= 20 for v in ordered)}


def evaluate(output):
    from ai.evaluation.dataset_contract import load_manifest
    from ai.evaluation.artifact_contracts import EvaluationOutput
    from services.extraction.parsing.map_invoice import PROFILE_FIELDS, INVOICE_OPTIONAL, RECEIPT_OPTIONAL

    output = Path(output).resolve()
    seal = verify_frozen(output)
    config, run_manifest = read_json(output / "preflight.json"), read_json(output / "run-manifest.json")
    if config["benchmark_code_sha256"] != digest(__file__) or seal["benchmark_code_sha256"] != digest(__file__):
        raise ValueError("BENCHMARK_CODE_CHANGED_SINCE_FREEZE")
    for name, expected in config["ground_truth_hashes"].items():
        if digest(AI / "manifests" / name) != expected:
            raise ValueError("GROUND_TRUTH_CHANGED_SINCE_PREFLIGHT")
    samples = {s.sample_id: s.model_dump(mode="json") for name in config["ground_truth_hashes"]
               for s in load_manifest(AI / "manifests" / name).samples}
    all_fields, all_lines, classes, row_outcomes, documents = [], [], [], [], []
    by_type, by_field, by_origin, by_split = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    native_issues, execution_states, collision_counts = Counter(), Counter(), Counter()
    supported = Counter()
    failures = []
    for source in config["sources"]:
        code, kind = source["demo_code"], source["document_type"]
        prediction = read_json(output / "predictions" / (code + ".json"))
        gold = samples[source["sample_id"]]
        native = prediction.get("native_result") or {}
        cells, collisions, unknown_keys = canonical_cells(native.get("fields", {}), kind)
        predicted_kind = (native.get("document_type") or {}).get("value") or "unknown"
        classification_eligible = gold["classification_annotation"]["verification_state"] in {"PUBLISHED_AS_IS", "INDEPENDENTLY_VERIFIED"}
        classification = {"sample_id": source["sample_id"], "expected": kind, "predicted": predicted_kind,
            "eligible": classification_eligible, "exclusion_reason": None if classification_eligible else "UNVERIFIED"}
        classes.append(classification)
        potential = PROFILE_FIELDS[kind] | (INVOICE_OPTIONAL if kind == "invoice" else set())
        if kind == "receipt":
            potential |= RECEIPT_OPTIONAL  # Support metadata only; scoring policies/denominators are unchanged.
        # Recognize the invoice-profile vocabulary on published synthetic bills as well.
        if kind == "bill":
            potential |= PROFILE_FIELDS["invoice"]
        supported_keys = {ALIASES[kind].get(k, k) for k in potential}
        fields = []
        for label in gold["fields"]:
            comparison = compare_cell(label, cells.get(label["field_key"]))
            comparison.update(demo_code=code, sample_id=source["sample_id"], document_type=kind,
                              origin=source["origin"], benchmark_split=source["cohort"]["evaluation_split"],
                              supported=label["field_key"] in supported_keys, gold_row_id=None, prediction_row_id=None)
            if label["label_state"] == "PRESENT":
                supported["present"] += 1
                supported["supported_present"] += comparison["supported"]
            fields.append(comparison)
        all_fields.extend(fields)
        by_type[kind].extend(fields)
        by_origin[source["origin"]].extend(fields)
        by_split[source["cohort"]["evaluation_split"]].extend(fields)
        for c in fields:
            by_field[c["field_key"]].append(c)
        line_comparisons, row_matches = [], []
        native_rows = [canonical_cells(row, kind, line=True)[0] for row in native.get("line_items", [])]
        if gold["line_items"]["status"] == "NOT_LABELLED":
            line_status = "NOT_MEASURABLE"
        else:
            line_status = "EVALUATED"
            gold_rows = gold["line_items"]["rows"]
            pairs, missing, extra = matched_rows(gold_rows, native_rows)
            linked = dict(pairs)
            for i, row in enumerate(gold_rows):
                j = linked.get(i)
                row_matches.append({"sample_id": source["sample_id"], "gold_row_id": row["row_id"],
                    "prediction_row_id": f"{code}:row:{j + 1}" if j is not None else None,
                    "outcome": "MATCHED" if j is not None else "MISSING_ROW"})
                for label in row["cells"]:
                    c = compare_cell(label, native_rows[j].get(label["field_key"]) if j is not None else None)
                    c.update(demo_code=code, sample_id=source["sample_id"], document_type=kind,
                        origin=source["origin"], gold_row_id=row["row_id"],
                        prediction_row_id=f"{code}:row:{j + 1}" if j is not None else None)
                    line_comparisons.append(c)
            for j in extra:
                row_matches.append({"sample_id": source["sample_id"], "gold_row_id": None,
                    "prediction_row_id": f"{code}:row:{j + 1}", "outcome": "EXTRA_ROW"})
        all_lines.extend(line_comparisons)
        row_outcomes.extend(row_matches)
        native_issues.update(i.get("code", "UNSPECIFIED") for i in native.get("issues", []))
        execution_states[prediction["status"]] += 1
        collision_counts.update(c["field_key"] for c in collisions)
        if prediction["status"] != "SUCCEEDED":
            failures.append({"demo_code": code, "status": prediction["status"],
                "error": prediction.get("error") or prediction.get("execution", {}).get("error")})
        document = {"demo_code": code, "sample_id": source["sample_id"], "origin": source["origin"],
            "document_type": kind, "benchmark_split": source["cohort"]["evaluation_split"],
            "execution_status": prediction["status"], "classification": classification,
            "source_sha256": source["sha256"], "prediction_sha256": run_manifest["predictions"][code]["sha256"],
            "fields": fields, "alias_collisions": collisions, "unsupported_native_keys": unknown_keys,
            "line_item_status": line_status, "line_cells": line_comparisons, "row_matches": row_matches,
            "line_row_coverage": gold["line_items"]["row_coverage"],
            "exact": metrics(fields, "exact"), "normalized": metrics(fields, "normalized"),
            "formatting_tolerant": metrics(fields, "formatting_tolerant"),
            "wall_seconds": run_manifest["durations_seconds"].get(code), "pipeline_seconds": prediction.get("pipeline_seconds")}
        write_new(output / "comparisons" / (code + ".json"), document)
        documents.append(document)
    outcome_names = {"correct": "CORRECT", "incorrect": "INCORRECT", "missing": "MISSING",
                     "correct_abstention": "CORRECT_ABSENT", "false_fill": "FALSE_FILL", "excluded": "EXCLUDED"}
    def denominator(cells):
        m = metrics(cells, "normalized")
        return {"eligible_present": m["eligible_present"], "correct": m["correct"], "incorrect": m["incorrect"],
            "missing": m["missing"], "eligible_absent": m["eligible_absent"],
            "correct_absent": m["correct_abstention"], "false_fills": m["false_fill"], "excluded": m["excluded"],
            "evidence_available": sum(bool(c["prediction_evidence"]) for c in cells if c["normalized"] != "excluded")}
    evaluation = {"schema_version": "1.0.0", "artifact_type": "evaluation_output",
        "benchmark_id": "module2-third-party-pilot-v1", "benchmark_revision": "1",
        "ground_truth_manifest_sha256": list(config["ground_truth_hashes"].values()),
        "prediction_artifact_sha256": [v["sha256"] for v in run_manifest["predictions"].values()],
        "evaluator_version": VERSION, "comparison_policy_version": ORIGINAL_POLICY,
        "row_matching_policy_version": ROW_POLICY, "verification_policy_version": "published-as-is-development/1",
        "created_at": now(), "classifications": classes, "row_matches": row_outcomes,
        "field_outcomes": [{"sample_id": c["sample_id"], "field_key": c["field_key"],
            "gold_row_id": c["gold_row_id"], "prediction_row_id": c["prediction_row_id"], "group_id": None,
            "outcome": outcome_names[c["normalized"]], "exclusion_reason": c["exclusion_reason"]}
            for c in all_fields + all_lines],
        "breakdowns": [{"dataset": None, "document_type": kind, "origin": None, "benchmark_split": None,
            "field_key": None, "scope": scope, "counts": denominator(cells)}
            for scope, cells in (("document", all_fields), ("line", all_lines)) for kind in (None,)]}
    EvaluationOutput.model_validate(evaluation)
    write_new(output / "evaluation.json", evaluation)
    def dual(cells):
        return {"exact": metrics(cells, "exact"), "normalized": metrics(cells, "normalized"),
                "formatting_tolerant": metrics(cells, "formatting_tolerant")}
    eligible_classes = [c for c in classes if c["eligible"]]
    row_counts = dict(Counter(r["outcome"] for r in row_outcomes))
    summary = {"version": VERSION, "created_at": now(),
        "comparison_policies": {"version": POLICY, "original_normalized": ORIGINAL_POLICY,
            "formatting_tolerant": ADDRESS_POLICY, "address_fields": sorted(ADDRESS_FIELDS),
            "rule": "Address commas/line breaks are separators; preserve numeric commas, other punctuation, word boundaries, digits and order. No fuzzy matching.",
            "evaluation_json_outcomes": "original_normalized"},
        "attempted": sum(p["attempted"] for p in (
        read_json(output / "predictions" / (s["demo_code"] + ".json")) for s in config["sources"])),
        "execution_status_counts": dict(execution_states), "failures": failures,
        "overall": dual(all_fields), "per_type": {k: dual(v) for k, v in by_type.items()},
        "per_origin": {k: dual(v) for k, v in by_origin.items()},
        "per_split": {k: dual(v) for k, v in by_split.items()},
        "per_field": {k: dual(v) for k, v in sorted(by_field.items())},
        "supported_field_coverage": ratio(supported["supported_present"], supported["present"]),
        "supported_fields_only": dual([c for c in all_fields if c["supported"]]),
        "classification": {"accuracy": ratio(sum(c["expected"] == c["predicted"] for c in eligible_classes), len(eligible_classes)),
            "confusion": dict(Counter(c["expected"] + "->" + c["predicted"] for c in eligible_classes))},
        "line_items": {"status": "EVALUATED_FOR_LABELLED_INVOICES_AND_BILLS", **dual(all_lines), "rows": row_counts,
            "row_recall": ratio(row_counts.get("MATCHED", 0), row_counts.get("MATCHED", 0) + row_counts.get("MISSING_ROW", 0)),
            "row_precision": ratio(row_counts.get("MATCHED", 0), row_counts.get("MATCHED", 0) + row_counts.get("EXTRA_ROW", 0)),
            "receipt_rows": "NOT_MEASURABLE", "per_type": {k: dual([c for c in all_lines if c["document_type"] == k]) for k in COUNTS}},
        "latency_wall_seconds": latency(run_manifest["durations_seconds"].values()),
        "latency_pipeline_seconds": latency(d["pipeline_seconds"] for d in documents if d["pipeline_seconds"] is not None),
        "error_categories": {"missing_fields": dict(Counter(c["field_key"] for c in all_fields if c["normalized"] == "missing")),
            "incorrect_fields": dict(Counter(c["field_key"] for c in all_fields if c["normalized"] == "incorrect")),
            "alias_collisions": dict(collision_counts), "native_issues": dict(native_issues)},
        "limitations": ["Development evidence; published labels are not independently team-verified",
            "All invoice/bill sources synthetic; authentic receipts are a distinct held-out cohort",
            "Unsupported PRESENT fields remain misses in all-field accuracy",
            "False fills scored only against CONFIRMED_ABSENT; unlabelled fields excluded",
            "No amount_due/total/current_charges substitution; no financial arithmetic in evaluator",
            "Extra rows penalize row precision; unlabelled extra-row cells have no invented gold labels",
            "Line row coverage is published/possibly partial; receipt rows not measurable",
            "No shared services, accounting validation, corrections, approval or Qwen inference"]}
    verify_frozen(output)
    write_new(output / "summary.json", summary)
    print(json.dumps({k: summary[k] for k in ("attempted", "execution_status_counts", "classification", "latency_wall_seconds")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    running = commands.add_parser("run")
    running.add_argument("--output", type=Path, default=AI / "runs" / ("benchmark_v1_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    running.add_argument("--models", type=Path, default=AI / "models" / "docling")
    running.add_argument("--ocr-models", type=Path, required=True)
    running.add_argument("--timeout", type=int, default=300)
    commands.add_parser("worker").add_argument("job", type=Path)
    commands.add_parser("evaluate").add_argument("output", type=Path)
    profiling = commands.add_parser("profile", help="Offline cold/warm stage timings; no scoring")
    profiling.add_argument("--output", type=Path, required=True)
    profiling.add_argument("--models", type=Path, default=AI / "models" / "docling")
    profiling.add_argument("--ocr-models", type=Path, required=True)
    profiling.add_argument("--codes", nargs="+", default=[
        "INV00001", "INV00006", "INV00014", "BIL00001", "BIL00007", "REC00007", "REC00010"])
    profiling.add_argument("--warm-only", action="store_true")
    commands.add_parser("profile-worker").add_argument("job", type=Path)
    args = parser.parse_args()
    if args.command == "run":
        run(args.output, args.models, args.ocr_models, args.timeout)
    elif args.command == "worker":
        worker(args.job)
    elif args.command == "profile":
        profile_run(args.output, args.models, args.ocr_models, args.codes, args.warm_only)
    elif args.command == "profile-worker":
        profile_worker(args.job)
    else:
        install_boundary(args.output)
        evaluate(args.output)


def semantic_prediction(native):
    """Compare complete field/evidence/state contracts, excluding run identity/paths.

    Paths, timestamps and hashes of run-local artifacts necessarily change on a
    rerun. Classification, raw values, source references/geometry, confidence,
    issues and all review/approval guards remain part of this comparison.
    """
    return {key: native.get(key) for key in (
        "schema_version", "document_id", "document_type", "field_schema", "fields",
        "line_items", "issues", "mode", "review_required", "accounting_approval_performed",
        "safety", "status")}


def profile_worker(job_path):
    """Instrument the genuine pipeline; Docling timers may overlap across stages.

    Warm-up is retained and timed, never silently removed from batch cost. Cold
    means a fresh interpreter/models, not a flushed OS disk cache. Instrumentation
    imports are explicitly timed and included in the parent process wall time.
    """
    from functools import wraps
    job = read_json(job_path)
    output = Path(job["output"])
    facts = install_boundary(output, worker=True)
    started = clock.perf_counter()
    from services.extraction.workflow.execution import execute_document_extraction
    import services.extraction.workflow.execution as execution_module
    import services.extraction.workflow.document_extraction_service as service_module
    import services.extraction.parsing.baseline_parser as parser_module
    from docling.document_converter import DocumentConverter
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
    from docling.datamodel.settings import settings
    imports_seconds = clock.perf_counter() - started
    settings.debug.profile_pipeline_timings = True
    stages, docling = {}, {}

    def instrument(owner, name, key, capture=False):
        original = getattr(owner, name)
        @wraps(original)
        def measured(*args, **kwargs):
            begin = clock.perf_counter()
            try:
                result = original(*args, **kwargs)
                if capture:
                    for label, item in result.timings.items():
                        docling[label] = {"seconds": sum(item.times), "calls": item.count,
                                          "samples": list(item.times), "scope": str(item.scope)}
                return result
            finally:
                stages[key] = stages.get(key, 0.0) + clock.perf_counter() - begin
        setattr(owner, name, measured)

    instrument(StandardPdfPipeline, "_init_models", "model_initialization")
    instrument(DocumentConverter, "convert", "docling_convert", capture=True)
    instrument(DocumentConverter, "__init__", "converter_constructor")
    instrument(parser_module, "recover_party_spacing", "party_spacing_retry")
    instrument(service_module, "parse_document", "parser_total")
    for method, key in (("_save_mapping", "mapping"), ("_save_extraction", "adaptation"),
                        ("_save_classification", "classification"), ("_save_combined", "combination"),
                        ("_save_routing", "routing"), ("process_document", "service_total")):
        instrument(service_module.DocumentIntelligenceService, method, key)
    instrument(execution_module, "build_public_result", "public_result")
    for index, item in enumerate(job["jobs"]):
        stages.clear(); docling.clear()
        source = Path(item["source_path"])
        if digest(source) != item["sha256"]:
            raise ValueError("STAGED_SOURCE_CHANGED")
        begin = clock.perf_counter()
        native = None
        try:
            execution = execute_document_extraction(
                document_id=item["demo_code"], file_path=source,
                project_root=Path(item["project_root"]), models_dir=Path(job["models"]),
                allow_vlm_inference=False)
            extraction_seconds = clock.perf_counter() - begin
            if execution.get("public_result_path"):
                native = read_json(execution["public_result_path"])
            status = "SUCCEEDED" if execution.get("execution_status") == "EXTRACTED" and native else "FAILED"
        except Exception as error:
            extraction_seconds = clock.perf_counter() - begin
            execution = {"error": type(error).__name__ + ":" + str(error)}
            status = "FAILED"
        freeze_begin = clock.perf_counter()
        artifacts = {}
        for path in sorted(Path(item["project_root"]).rglob("*")):
            if path.is_file():
                artifacts[str(path.relative_to(output))] = digest(path)
                path.chmod(0o444)
        record = {**item, "status": status, "native_result": native,
            "execution": execution, "source_sha256": item["sha256"],
            "pipeline_seconds": extraction_seconds, "stages_seconds": dict(stages),
            "docling_timings": dict(docling), "artifact_hashes": artifacts,
            "hash_freeze_seconds": clock.perf_counter() - freeze_begin,
            "bootstrap_imports_seconds": imports_seconds if index == 0 else 0.0,
            "boundary": dict(facts), "qwen_enabled": False,
            "semantic_sha256": hashlib.sha256(json.dumps(semantic_prediction(native), sort_keys=True).encode()).hexdigest() if native else None}
        write_new(output / "predictions" / (item["id"] + ".json"), record)
        print(item["id"], status, round(extraction_seconds, 3), flush=True)


def profile_run(output, models, ocr_models, codes, warm_only=False):
    """Same ordered sources, fresh cold workers and one sequential warm worker.

    The extra first warm-up conversion has a separate immutable prediction.
    Parent wall includes process bootstrap/shutdown, instrumentation, extraction,
    and all prediction/artifact writes. Per-document warm times are pipeline wall
    (with separately reported freeze cost); batch wall includes everything.
    No ground-truth labels are read after source verification or used for timing.
    """
    output = within(output, AI / "runs")
    output.mkdir(parents=True, exist_ok=False)
    for name in ("cache", "tmp", "jobs", "logs", "predictions", "documents"):
        (output / name).mkdir()
    boundary = install_boundary(output)
    os.environ.clear(); os.environ.update(child_environment(output, ocr_models))
    _, sources = verified_sources(output)
    by_code = {s["demo_code"]: s for s in sources}
    if len(set(codes)) != len(codes) or any(c not in by_code or not by_code[c]["verified"] for c in codes):
        raise ValueError("UNVERIFIED_OR_DUPLICATE_PROFILE_SOURCE")
    write_new(output / "preflight.json", {"created_at": now(), "runtime": versions(),
        "models": model_inventory(models, ocr_models), "codes": codes,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "code_hashes": {str(p.relative_to(ROOT)): digest(p) for p in
            sorted((ROOT / "services" / "extraction").rglob("*.py")) + [Path(__file__)]},
        "loadavg": os.getloadavg(), "cpuinfo": Path("/proc/cpuinfo").read_text(),
        "meminfo": Path("/proc/meminfo").read_text(),
        "timing_policy": "fresh interpreter cold; sequential same-process warm; timed warm-up; OS cache not flushed; Docling stage times overlap"})
    batches = []
    def item(code, phase):
        s = by_code[code]; identity = phase + "_" + code
        project = output / "documents" / identity; project.mkdir()
        return {"id": identity, "phase": phase, "demo_code": code,
                "source_path": s["source_path"], "sha256": s["sha256"],
                "project_root": str(project)}
    if not warm_only:
        batches.extend([[item(code, "cold")] for code in codes])
    batches.append([item(codes[0], "warmup")] + [item(code, "warm") for code in codes])
    timings = {}
    for number, items in enumerate(batches):
        job = output / "jobs" / f"{number:03d}.json"
        write_new(job, {"output": str(output), "models": str(Path(models).resolve()), "jobs": items})
        start = clock.perf_counter()
        with (output / "logs" / f"{number:03d}.log").open("xb") as log:
            process = subprocess.run([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                "profile-worker", str(job)], cwd=ROOT, env=child_environment(output, ocr_models),
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                timeout=300 * len(items), check=False)
        timings[items[0]["id"]] = {"wall_seconds": clock.perf_counter() - start,
                                   "returncode": process.returncode, "items": [i["id"] for i in items]}
        print(items[0]["id"], timings[items[0]["id"]], flush=True)
    sealed = {}
    for path in sorted((output / "predictions").glob("*.json")):
        sealed[str(path.relative_to(output))] = digest(path)
        sealed.update(read_json(path)["artifact_hashes"])
    write_new(output / "prediction-seal.json", {"created_at": now(), "files": sealed, "evaluation_started": False})
    write_new(output / "profile-manifest.json", {"batch_timings": timings, "boundary": boundary})
    verify_frozen(output)


if __name__ == "__main__":
    main()
