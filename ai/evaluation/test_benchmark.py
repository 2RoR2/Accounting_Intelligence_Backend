"""Synthetic evaluator/boundary checks only; never evidence of OCR accuracy."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ai.evaluation import benchmark as b


def label(key="total", value="10.00", state="PRESENT", value_type="decimal"):
    return {"field_key": key, "value": value, "normalized_value": value,
            "label_state": state, "value_type": value_type}


def cell(value, state=None):
    return {"value": value, "state": state or ("resolved" if value is not None else "missing"),
            "raw_text": value, "evidence": []}


def test_wrong_value_is_not_missing():
    assert b.compare_cell(label(), cell("11.00"))["normalized"] == "incorrect"
    assert b.compare_cell(label(), cell(None))["normalized"] == "missing"


def test_absent_false_fill_and_correct_abstention():
    absent = label(value=None, state="CONFIRMED_ABSENT")
    assert b.compare_cell(absent, cell("0"))["normalized"] == "false_fill"
    assert b.compare_cell(absent, None)["normalized"] == "correct_abstention"


def test_zero_is_a_real_value():
    assert b.compare_cell(label(value="0.00"), cell("0"))["normalized"] == "correct"
    assert b.compare_cell(label(value="0.00"), cell("0"))["exact"] == "incorrect"
    assert b.compare_cell(label(value="0.00"), None)["normalized"] == "missing"


@pytest.mark.parametrize("state", ["UNLABELLED", "AMBIGUOUS", "UNREADABLE"])
def test_excluded_labels_are_never_correct_or_incorrect(state):
    result = b.compare_cell(label(value=None, state=state), cell("10.00"))
    assert result["normalized"] == "excluded"
    assert b.metrics([result], "normalized")["accuracy"]["denominator"] == 0


def test_missing_and_unlabelled_are_different():
    missing = b.compare_cell(label(), None)
    unlabelled = b.compare_cell(label(value=None, state="UNLABELLED"), None)
    m = b.metrics([missing, unlabelled], "normalized")
    assert (m["missing"], m["excluded"], m["eligible_present"]) == (1, 1, 1)


def test_candidate_is_not_selected_prediction():
    unresolved = {**cell(None, "ambiguous"), "candidates": [{"value": "10.00"}]}
    assert b.compare_cell(label(), unresolved)["normalized"] == "missing"


def test_alias_collision_does_not_overwrite():
    native = {"tax": cell("1.00"), "tax_amount": cell("2.00")}
    mapped, collisions, _ = b.canonical_cells(native, "invoice")
    assert mapped["tax"]["state"] == "conflict"
    assert collisions == [{"field_key": "tax", "source_keys": ["tax", "tax_amount"]}]
    assert native["tax"]["value"] == "1.00"


def test_distinct_amount_meanings_are_not_aliased():
    native = {key: cell(value) for key, value in {
        "total_amount": "10", "amount_due": "8", "current_charges": "6",
        "amount_paid": "2", "subtotal": "9", "tax_amount": "1", "discount": "0"}.items()}
    mapped, collisions, _ = b.canonical_cells(native, "bill")
    assert not collisions
    assert {k: mapped[k]["value"] for k in ("total", "amount_due", "current_charges", "amount_paid")} == {
        "total": "10", "amount_due": "8", "current_charges": "6", "amount_paid": "2"}
    assert b.compare_cell(label("amount_due", "8"), mapped.get("total"))["normalized"] == "incorrect"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "", "1,23", True, 0])
def test_invalid_numeric_predictions_are_incorrect(value):
    assert b.compare_cell(label(), cell(value))["normalized"] == "incorrect"


def test_exact_decimal_comparison_has_no_rounding_tolerance():
    assert b.compare_cell(label(value="10.00"), cell("10.001"))["normalized"] == "incorrect"
    assert b.normalise("1,000.00", "decimal") == b.normalise("1000", "decimal")


def test_text_normalization_is_explicit():
    result = b.compare_cell(label("supplier_name", "Example  LTD", value_type="text"), cell(" example ltd "))
    assert (result["exact"], result["normalized"]) == ("incorrect", "correct")


@pytest.mark.parametrize("key", sorted(b.ADDRESS_FIELDS))
def test_address_separator_policy_is_additional_and_address_only(key):
    original = "10 Source Road\nExample City, AB 01234"
    gold = label(key, "10 Source Road, Example City, AB 01234", value_type="text")
    prediction = cell(original)
    result = b.compare_cell(gold, prediction)
    assert result["normalized"] == "incorrect"
    assert result["formatting_tolerant"] == "correct"
    assert prediction["value"] == original
    assert gold["value"] == "10 Source Road, Example City, AB 01234"


@pytest.mark.parametrize("predicted,reference", [
    ("10 MainStreet\nTown 01234", "10 Main Street, Town 01234"),
    ("10 Main Street\nTown 01234", "11 Main Street, Town 01234"),
    ("10 Main Street\nTown 01234", "10 Main Street, Town 1234"),
    ("10 Main Street\nTwon 01234", "10 Main Street, Town 01234"),
    ("Unit A-1\n10 Main Street", "Unit A1, 10 Main Street"),
    ("Unit 1/2\n10 Main Street", "Unit 12, 10 Main Street"),
    ("Town 01234\n10 Main Street", "10 Main Street, Town 01234"),
    ("1,234 Main Street", "1 234 Main Street"),
    ("St. Example Road", "St Example Road"),
])
def test_address_policy_does_not_hide_content_or_identifier_errors(predicted, reference):
    result=b.compare_cell(label("supplier_address",reference,value_type="text"),cell(predicted))
    assert result["formatting_tolerant"] == "incorrect"


def test_separator_tolerance_does_not_apply_to_names_amounts_or_unlabelled_fields():
    for key in ("supplier_name","customer_name","description","supplier_tax_id"):
        result=b.compare_cell(label(key,"A, B",value_type="text"),cell("A B"))
        assert result["normalized"] == result["formatting_tolerant"] == "incorrect"
    for state in ("UNLABELLED","AMBIGUOUS","UNREADABLE"):
        result=b.compare_cell(label("supplier_address",None,state,value_type="text"),cell("10 Road"))
        assert result["formatting_tolerant"] == "excluded"
    assert b.compare_cell(label("supplier_address","10 Road",value_type="text"),cell(None,"ambiguous"))["formatting_tolerant"] == "missing"
    assert b.compare_cell(label("supplier_address",None,"CONFIRMED_ABSENT",value_type="text"),cell("10 Road"))["formatting_tolerant"] == "false_fill"


def test_address_policy_keeps_original_denominators_and_counts():
    comparisons=[b.compare_cell(label("supplier_address","10 Road, Town",value_type="text"),cell("10 Road\nTown")),
                 b.compare_cell(label("customer_address","20 Road, Town",value_type="text"),cell("21 Road\nTown")),
                 b.compare_cell(label("supplier_address","30 Road",value_type="text"),None)]
    original=b.metrics(comparisons,"normalized"); tolerant=b.metrics(comparisons,"formatting_tolerant")
    assert original["accuracy"] == b.ratio(0,3)
    assert tolerant["accuracy"] == b.ratio(1,3)
    assert tolerant["incorrect"] == tolerant["missing"] == 1


def test_denominators_and_precision():
    comparisons = [b.compare_cell(label(), cell("10.00")), b.compare_cell(label(), cell("11")),
        b.compare_cell(label(), None), b.compare_cell(label(value=None, state="CONFIRMED_ABSENT"), cell("0")),
        b.compare_cell(label(value=None, state="CONFIRMED_ABSENT"), None),
        b.compare_cell(label(value=None, state="UNLABELLED"), cell("12"))]
    m = b.metrics(comparisons, "normalized")
    assert (m["eligible_present"], m["eligible_absent"], m["excluded"]) == (3, 2, 1)
    assert m["accuracy"] == b.ratio(1, 3)
    assert m["precision"] == b.ratio(1, 3)
    assert m["recall"] == b.ratio(1, 3)
    assert m["extraction_coverage"] == b.ratio(2, 3)
    assert b.ratio(0, 0)["value"] is None


def test_failed_prediction_counts_as_missing_not_perfect_abstention():
    failed = {"status": "FAILED", "native_result": None, "error": "TIMEOUT"}
    mapped, _, _ = b.canonical_cells((failed["native_result"] or {}).get("fields", {}), "invoice")
    result = b.compare_cell(label(), mapped.get("total"))
    assert b.metrics([result], "normalized")["accuracy"] == b.ratio(0, 1)
    assert failed["error"] == "TIMEOUT"


def row(description, quantity, price, total):
    return {"cells": [label("description", description, value_type="text"),
        label("quantity", quantity), label("unit_price", price), label("line_total", total)]}


def predicted_row(description, quantity, price, total):
    return {"description": cell(description), "quantity": cell(quantity),
            "unit_price": cell(price), "line_total": cell(total)}


def test_one_to_one_rows_keep_missing_and_extra():
    gold = [row("Paper", "2", "10", "20"), row("Pens", "3", "7", "21")]
    prediction = [predicted_row("Coffee", "1", "8", "8"), predicted_row("Paper", "2", "10", "20")]
    assert b.matched_rows(gold, prediction) == ([(0, 1)], [1], [0])


def test_row_cannot_be_reused_for_duplicate_gold_rows():
    pairs, missing, extra = b.matched_rows([row("Paper", "2", "10", "20")] * 2,
                                         [predicted_row("Paper", "2", "10", "20")])
    assert len(pairs) == 1 and len(missing) == 1 and extra == []


def test_empty_rows():
    assert b.matched_rows([], [predicted_row("Paper", "2", "10", "20")]) == ([], [], [0])
    assert b.matched_rows([row("Paper", "2", "10", "20")], []) == ([], [0], [])


def test_frozen_prediction_cannot_be_rewritten(tmp_path):
    target = tmp_path / "prediction.json"
    b.write_new(target, {"value": "10"})
    with pytest.raises(FileExistsError):
        b.write_new(target, {"value": "20"})
    assert b.read_json(target) == {"value": "10"}


def test_seal_detects_tampering(tmp_path):
    target = tmp_path / "prediction.json"
    b.write_new(target, {"value": "10"}, freeze=False)
    b.write_new(tmp_path / "prediction-seal.json", {"files": {"prediction.json": b.digest(target)}})
    b.verify_frozen(tmp_path)
    target.write_text('{}', encoding="utf-8")
    with pytest.raises(ValueError, match="FROZEN_PREDICTION_CHANGED"):
        b.verify_frozen(tmp_path)


def test_child_environment_does_not_inherit_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "synthetic-secret-must-not-be-inherited")
    monkeypatch.setenv("AUTH_SECRET", "synthetic-secret-must-not-be-inherited")
    env = b.child_environment(tmp_path, tmp_path)
    assert "DATABASE_URL" not in env and "AUTH_SECRET" not in env
    assert "OLLAMA_HOST" not in env and "PYTHONPATH" not in env
    assert env["PYTHON_DOTENV_DISABLED"] == "1" and env["HF_HUB_OFFLINE"] == "1"


def test_boundary_denies_connections_secrets_labels_and_database_imports(tmp_path):
    code = f"""
import sys, pathlib, socket, json, os, importlib.util
sys.path.insert(0, {str(ROOT)!r})
from ai.evaluation.benchmark import install_boundary, AI
facts = install_boundary({str(tmp_path)!r}, worker=True)
# Availability checks must not become artificial OCR failures.
importlib.util.find_spec('boto3')
with open(os.devnull, 'w') as discard:
 discard.write('discarded test output')
attempts = [lambda: socket.create_connection(('127.0.0.1', 1)),
 lambda: pathlib.Path({str(tmp_path / '.env')!r}).read_text(),
 lambda: (AI/'manifests/invoices.json').read_text(), lambda: __import__('database.connection'),
 lambda: __import__('boto3')]
blocked = 0
for attempt in attempts:
 try: attempt()
 except (PermissionError, ImportError): blocked += 1
assert blocked == 5
print(json.dumps(facts))
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code],
        env=b.child_environment(tmp_path, tmp_path), capture_output=True, text=True, check=True)
    facts = json.loads(result.stdout)
    assert facts["network_attempts"] >= 1
    assert facts["secret_read_attempts"] == 1 and facts["ground_truth_read_attempts"] == 1


def test_latency_nearest_rank_and_empty():
    assert b.latency(range(1, 21))["p95_nearest_rank"] == 19
    assert b.latency([])["mean"] is None
