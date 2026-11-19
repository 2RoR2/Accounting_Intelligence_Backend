"""reread-documents without a database: which documents may be read again, the local-only guard, the summary."""
import json

import pytest

from services.admin import reread

LOCAL = {"APP_ENV": "development", "DATABASE_URL": "postgresql://postgres:x@127.0.0.1:5433/accounting_db"}


def facts(status="exception", **flags):
    row = {"id": "doc", "document_code": "DOC-1", "original_filename": "a.pdf", "status": status,
           "field_corrections": False, "line_corrections": False, "review_submissions": False,
           "review_decisions": False, "standardised": False, "open_job": False}
    row.update(flags)
    return row


@pytest.mark.parametrize("status", reread.DEFAULT_STATUSES)
def test_finished_unreviewed_documents_may_be_read_again(status):
    assert reread.skip_reason(facts(status), reread.DEFAULT_STATUSES) is None


@pytest.mark.parametrize("status", ["corrected", "in_review", "validated", "standardised", "completed"])
def test_documents_past_review_are_skipped(status):
    assert reread.skip_reason(facts(status), reread.DEFAULT_STATUSES) == f"past review ({status})"


@pytest.mark.parametrize("flag, reason", [
    ("field_corrections", "has reviewed corrections"),
    ("line_corrections", "has reviewed corrections"),
    ("review_submissions", "has review submissions or decisions"),
    ("review_decisions", "has review submissions or decisions"),
    ("standardised", "has an approved record"),
    ("open_job", "already has an open job"),
])
def test_corrected_reviewed_or_busy_documents_are_skipped(flag, reason):
    assert reread.skip_reason(facts("exception", **{flag: True}), reread.DEFAULT_STATUSES) == reason


def test_status_filter_and_waiting_statuses():
    assert reread.skip_reason(facts("extracted"), ("exception",)) == "status extracted not selected"
    for status in ("uploaded", "queued", "processing", "validating"):
        assert reread.skip_reason(facts(status), reread.DEFAULT_STATUSES) == f"status {status} not selected"


def test_the_strongest_reason_wins():
    row = facts("completed", field_corrections=True, open_job=True)
    assert reread.skip_reason(row, reread.DEFAULT_STATUSES) == "past review (completed)"


def test_limit_counts_only_documents_that_would_be_read():
    rows = [facts("exception"), facts("completed"), facts("exception"), facts("failed")]
    actions = [action for _row, action in reread.plan(rows, reread.DEFAULT_STATUSES, limit=2)]
    assert actions == ["reread", "skip: past review (completed)", "reread", "skip: over --limit"]


def test_status_option_accepts_only_rereadable_statuses():
    assert reread.parse_statuses("Exception, failed,exception") == ("exception", "failed")
    for bad in ("", " , ", "completed", "exception,in_review"):
        with pytest.raises(reread.RereadError):
            reread.parse_statuses(bad)


def _environment(monkeypatch, **change):
    for key in ("APP_ENV", "DATABASE_URL", "S3_BUCKET", "PGHOST", "PGHOSTADDR", "PGSERVICE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**LOCAL, **change}.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("change, fragment", [
    ({"APP_ENV": "production"}, "APP_ENV must be development"),
    ({"DATABASE_URL": "postgresql://u:p@ep-shared.neon.tech/db"}, "only a database on this machine"),
    ({"DATABASE_URL": "postgresql://u:p@127.0.0.1/db?host=db.example.com"}, "only a database on this machine"),
    ({"S3_BUCKET": "shared-bucket"}, "S3_BUCKET is set"),
])
def test_refuses_outside_local_development_before_connecting(monkeypatch, change, fragment):
    _environment(monkeypatch, **change)

    def never(*_args):
        raise AssertionError("must not connect")

    with pytest.raises(reread.RereadError, match=fragment):
        reread.reread(company="Any", connect=never)


def test_pack_names_the_company_and_must_agree_with_company(monkeypatch, tmp_path):
    _environment(monkeypatch)
    (tmp_path / "trading").mkdir()
    (tmp_path / "trading" / "pack.json").write_text(json.dumps({"company": {"name": "Synthetic Trading"}}))
    assert reread.pack_company_name("trading", tmp_path) == "Synthetic Trading"
    with pytest.raises(reread.RereadError, match="is not the trading pack's company"):
        reread.reread(company="Other Company", pack="trading", pack_root=tmp_path)
    with pytest.raises(reread.RereadError, match="seed the company first"):
        reread.reread(pack="services", pack_root=tmp_path)
    with pytest.raises(reread.RereadError, match="--company"):
        reread.reread()
    with pytest.raises(reread.RereadError, match="--limit"):
        reread.reread(company="Any", limit=0)


def test_summary_counts_statuses_and_times_finished_documents():
    rows = [{"status": "extracted", "processing_seconds": seconds} for seconds in range(1, 21)]
    rows += [{"status": "exception", "processing_seconds": 4.0}, {"status": "queued", "processing_seconds": 99},
             {"status": "failed", "processing_seconds": None}]
    result = reread.summarize(rows)
    assert result["by_status"] == {"exception": 1, "extracted": 20, "failed": 1, "queued": 1}
    assert result["timed"] == 21
    assert result["average_seconds"] == round((210 + 4) / 21, 1)
    assert result["p95_seconds"] == 19.0  # nearest rank: the 20th of the 21 timed values
    assert reread.percentile([5.0], 0.95) == 5.0
    assert reread.summarize([])["average_seconds"] is None
