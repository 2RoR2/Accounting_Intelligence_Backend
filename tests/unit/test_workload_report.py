"""Workload report helpers: the first-pass status follows same-transaction chains, and medians/ranges are plain."""
from datetime import datetime, timedelta, timezone

import main  # noqa: F401  (registers the routes before the report module imports its helpers)
from services.routes.reporting.workload import _status_at, metrics, stats

T0 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)


def row(old, new, at):
    return {"old_status": old, "new_status": new, "created_at": at}


def test_same_transaction_chain_ends_at_the_validation_result_whatever_the_row_order():
    # The worker sets extracted and validation sets exception in one transaction: both rows share a timestamp.
    for history in ([row("processing", "extracted", T0), row("extracted", "exception", T0)],
                    [row("extracted", "exception", T0), row("processing", "extracted", T0)]):
        assert _status_at(history, T0 + timedelta(minutes=5), "in_review") == "exception"


def test_status_before_the_first_touch_not_after_it():
    history = [row("processing", "extracted", T0), row("extracted", "corrected", T0 + timedelta(minutes=2))]
    assert _status_at(history, T0 + timedelta(minutes=2), "corrected") == "extracted"


def test_inserted_status_is_used_when_the_first_change_is_the_touch_itself():
    # A document inserted already extracted (no history row) whose first change is the reviewer's submit.
    history = [row("extracted", "in_review", T0)]
    assert _status_at(history, T0, "validated") == "extracted"


def test_untouched_documents_use_the_current_status():
    assert _status_at([row("processing", "exception", T0)], None, "exception") == "exception"
    assert _status_at([], None, "processing") == "processing"


def test_stats_report_n_median_and_range_and_nothing_when_empty():
    assert stats([0, 2, 2, 5]) == {"n": 4, "median": 2.0, "min": 0, "max": 5}
    assert stats([1, 2]) == {"n": 2, "median": 1.5, "min": 1, "max": 2}
    assert stats([None]) == {"n": 0, "median": None, "min": None, "max": None}


def test_metrics_exclude_documents_without_a_result_and_mark_review_time_not_measured():
    base = {"fieldEdits": 0, "lineItemChanges": 0, "typeChoices": 0, "decisions": 0, "correctedFields": [],
            "reviewSeconds": None}
    docs = [
        {**base, "touches": 0, "firstPassException": False, "ready": True, "readyWithoutCorrection": True},
        {**base, "touches": 3, "fieldEdits": 1, "correctedFields": ["total"], "firstPassException": True,
         "ready": True, "readyWithoutCorrection": False},
        {**base, "touches": 0, "firstPassException": None, "ready": False, "readyWithoutCorrection": False},
    ]
    result = metrics(docs)
    assert result["firstPass"] == {"checked": 2, "exceptions": 1, "rate": 50.0, "excluded": 1}
    assert result["readyWithoutCorrection"] == {"ready": 2, "withoutCorrection": 1, "rate": 50.0, "notReady": 1}
    assert result["reviewTime"]["measured"] is False and result["reviewTime"]["n"] == 0
    assert result["touches"] == {"n": 3, "median": 0.0, "min": 0, "max": 3}
