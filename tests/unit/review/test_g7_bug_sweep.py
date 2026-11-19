"""G7 bug sweep rules that need no database: display names, read-again input and outcomes, OCR dates."""
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import main  # noqa: F401 - initialise the application's route registry before route imports.
from services.extraction.ocr.ocr_documents import _date
from services.queue import attempts
from services.review import reread_failed
from services.review.reread_failed import ReadAgainInput
from services.routes.documents.routes import _display_filename


@pytest.mark.parametrize('args, expected', [
    (('images (2).png', 'INV-1023', 'Syarikat Maju Jaya'), 'Syarikat Maju Jaya INV-1023'),
    (('images (2).png', 'INV-1023', None), 'INV-1023.png'),                 # number only: as before
    (('images (2).png', None, 'Syarikat Maju Jaya'), 'images (2).png'),     # no number: the filename stays
    (('images (2).png', 'unknown', 'Syarikat Maju Jaya'), 'images (2).png'),
    (('scan.pdf', ' INV-7\n', '  Kedai   Runcit\tAh Seng '), 'Kedai Runcit Ah Seng INV-7'),
    (('scan.pdf', 'TNB-2026-10', 'TNB-2026-10 Tenaga'), 'TNB-2026-10.pdf'),  # number already in the name
    ((None, None, None), 'document'),
])
def test_display_name_prefers_party_and_number(args, expected):
    assert _display_filename(*args) == expected


def test_display_name_is_shortened_not_overflowing():
    name = _display_filename('a.pdf', 'INV-1', 'X' * 200)
    assert name.endswith(' INV-1') and len(name) <= 70


def test_read_again_needs_exactly_one_selection():
    for body in ({}, {'documentIds': ['a'], 'allFailed': True}, {'documentIds': []}):
        with pytest.raises(ValidationError):
            ReadAgainInput(**body)
    with pytest.raises(ValidationError):
        ReadAgainInput(documentIds=['x'] * (reread_failed.MAX_DOCUMENTS + 1))


def test_read_again_reports_each_document_and_continues_after_a_refusal():
    user = {'id': 'u', 'tenant_id': 't', 'role': 'local_admin'}
    rows = [{'id': n, 'document_code': f'BILL_{n}', 'original_filename': 'x.png', 'status': status}
            for n, status in (('1', 'failed'), ('2', 'failed'), ('3', 'extracted'), ('4', 'failed'))]

    def queue(_user, document):
        if document['id'] == '2':
            raise attempts.AttemptRejected('ATTEMPT_BUDGET_USED')
        if document['id'] == '4':
            raise RuntimeError('synthetic database error')
        return 'job-' + document['id']

    with patch.object(reread_failed, '_candidates', return_value=(rows, ['missing'], 2)), \
            patch.object(reread_failed, '_queue_one', side_effect=queue), \
            patch.object(reread_failed, '_wake_dispatcher') as wake:
        result = reread_failed.read_again(user, ReadAgainInput(allFailed=True))
    outcomes = [(r['id'], r['outcome'], r.get('reasonCode')) for r in result['results']]
    assert outcomes == [('missing', 'skipped', 'NOT_FOUND'), ('1', 'queued', None), ('2', 'skipped', 'ATTEMPT_BUDGET_USED'),
                        ('3', 'skipped', 'NOT_FAILED'), ('4', 'error', 'ERROR')]
    assert (result['requested'], result['queued'], result['skipped'], result['budgetUsed']) == (5, 1, 4, 2)
    assert all(r.get('reason') for r in result['results'] if r['outcome'] != 'queued')
    wake.assert_called_once()


def test_read_again_without_queued_documents_does_not_wake_the_dispatcher():
    with patch.object(reread_failed, '_candidates', return_value=([], ['x'], 0)), \
            patch.object(reread_failed, '_wake_dispatcher') as wake:
        result = reread_failed.read_again({'id': 'u', 'tenant_id': 't', 'role': 'accountant'}, ReadAgainInput(documentIds=['x']))
    assert result['queued'] == 0
    wake.assert_not_called()


@pytest.mark.parametrize('user', [{'id': 'u', 'tenant_id': None, 'role': 'super_admin'},
                                  {'id': 'u', 'tenant_id': 't', 'role': 'viewer'}])
def test_read_again_refuses_other_roles(user):
    with patch.object(reread_failed, '_candidates', side_effect=AssertionError('no database access')):
        with pytest.raises(HTTPException) as denied:
            reread_failed.read_again(user, ReadAgainInput(allFailed=True))
    assert denied.value.status_code == 403


@pytest.mark.parametrize('text, expected', [
    ('14-10-2026', '2026-10-14'), ('14.10.2026', '2026-10-14'), ('14/10/2026', '2026-10-14'),
    ('31-12-2026 10:15', '2026-12-31'), ('2026-10-14', '2026-10-14'),
    ('01-09-2026', None),    # day and month both 12 or less: never guessed
    ('14-10/2026', None),    # mixed separators
    ('24-03-18', None),      # two-digit year: never guessed
    ('31-02-2026', None),
])
def test_ocr_date_accepts_dash_and_dot_separators(text, expected):
    assert _date(text) == expected


def _boxed_dates(due_text):
    """Labels and values in separate OCR boxes, as on the demo pack's scanned invoices."""
    from tests.extraction.test_ocr_documents import invoice
    art = invoice()
    art['rows'][3].update(text='Date:', box_polygon_pixels=[[20, 135], [80, 135], [80, 155], [20, 155]])
    for text, (l, t, r, b) in (('14/09/2026', (100, 135, 290, 155)), ('Due Date:', (20, 165, 100, 185)),
                               (due_text, (120, 165, 290, 185))):
        art['rows'].append({'order': len(art['rows']), 'text': text, 'engine_score': .97,
                            'box_polygon_pixels': [[l, t], [r, t], [r, b], [l, b]]})
    return art


@pytest.mark.parametrize('due_text', ['14-10-2026', '14/10/2026'])
def test_due_date_row_is_not_also_an_unlabelled_invoice_date(due_text):
    from tests.extraction.test_ocr_documents import run
    fields = run(_boxed_dates(due_text))['fields']
    assert fields['due_date']['value'] == '2026-10-14'
    assert fields['invoice_date']['value'] == '2026-09-14'
