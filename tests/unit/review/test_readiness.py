"""Ready conditions and their named reasons (task B5a, spec P3.1). Synthetic rows only."""
import ast
import inspect
import textwrap
from datetime import datetime, timezone

import pytest

import main  # noqa: F401 - initialise the route registry before importing route modules.
from services.review import readiness
from services.review.readiness import REASON_CODES, assess, lane_for, required_fields

TEXT = [{'from_ocr': False, 'page': 1, 'bbox': [0, 0, 1, 1], 'source_ref': 'p1'}]
LOW_OCR = [{'from_ocr': True, 'engine_score': 0.5, 'page': 1, 'bbox': [0, 0, 1, 1], 'source_ref': 'p1'}]
VENDORS = [{'id': 'v1', 'vendor_name': 'Acme Supplies Sdn Bhd'}]


def field(key, value, evidence=TEXT, **extra):
    return {'field_key': key, 'current_value': None, 'normalized_value': value, 'raw_value': value,
            'extraction_state': 'resolved', 'resolution': 'parser_preserved', 'evidence': evidence,
            'candidates': [], 'corrected_by': None, 'corrected_at': None, **extra}


def receipt_fields(**overrides):
    rows = {'supplier': field('supplier', 'Acme Supplies Sdn Bhd'), 'date': field('date', '2026-11-02'),
            'currency': field('currency', 'MYR'), 'total': field('total', '22.60')}
    rows.update(overrides)
    return [row for row in rows.values() if row is not None]


DOCUMENT = {'id': 'd1', 'tenant_id': 't1', 'kind': 'receipt', 'category': 'accounts_payable'}


def check(**changes):
    args = dict(has_extraction=True, fields=receipt_fields(), validation={'total': 5, 'failures': []},
                open_exceptions=[], catalogue=VENDORS, requires_known_supplier=True)
    args.update(changes)
    document = args.pop('document', DOCUMENT)
    return assess(document, **args)


def codes(reasons):
    return [r['code'] for r in reasons]


def test_a_clean_document_is_ready():
    assert check() == []
    assert lane_for([]) == 'ready'


@pytest.mark.parametrize('changes, expected', [
    ({'has_extraction': False}, 'extraction_not_succeeded'),
    ({'validation': {'total': 0, 'failures': []}}, 'validation_not_run'),
    ({'validation': {'total': 5, 'failures': ['EX-005']}}, 'validation_failed'),
    ({'open_exceptions': [{'type': 'VENDOR_HOLD'}]}, 'open_exception'),
    ({'fields': receipt_fields(total=None)}, 'field_missing'),
    ({'fields': receipt_fields(total=field('total', '22.60', evidence=LOW_OCR))}, 'field_needs_check'),
    ({'catalogue': [{'id': 'v2', 'vendor_name': 'Someone Else Trading'}]}, 'supplier_not_known'),
    ({'validation': {'total': 5, 'failures': ['EX-007']}}, 'duplicate_candidate'),
    ({'approver_id': 'u1', 'last_submitted_by': 'u1'}, 'own_submission'),
])
def test_each_unmet_condition_has_its_own_reason_and_plain_text(changes, expected):
    reasons = check(**changes)
    assert codes(reasons) == [expected]
    assert reasons[0]['message'] and not reasons[0]['message'].startswith(expected)


def test_every_reason_code_is_covered_and_listed():
    assert set(REASON_CODES) == {'extraction_not_succeeded', 'validation_not_run', 'validation_failed', 'open_exception',
                                 'field_missing', 'field_needs_check', 'supplier_not_known', 'duplicate_candidate',
                                 'own_submission'}


def test_all_unmet_conditions_are_returned_together_in_a_fixed_order():
    reasons = check(has_extraction=False, validation={'total': 0, 'failures': []}, open_exceptions=[{'type': 'X'}],
                    fields=receipt_fields(supplier=None, total=field('total', '1.00', evidence=LOW_OCR)))
    assert codes(reasons) == ['extraction_not_succeeded', 'validation_not_run', 'open_exception', 'field_missing',
                              'field_needs_check', 'supplier_not_known']
    assert reasons[3]['details']['fields'] == ['supplier'] and 'supplier' in reasons[3]['message']
    assert reasons[4]['details']['fields'] == ['total']


def test_a_field_the_accountant_corrected_counts_as_verified():
    corrected = field('total', None, evidence=LOW_OCR, current_value='22.60', corrected_by='u1',
                      corrected_at=datetime(2026, 11, 17, tzinfo=timezone.utc))
    assert check(fields=receipt_fields(total=corrected)) == []


def test_a_suggestion_only_field_is_missing_not_ready():
    suggested = field('total', None, extraction_state='missing', resolution='unresolved_missing',
                      candidates=[{'source': 'ai', 'value': '22.60'}])
    assert codes(check(fields=receipt_fields(total=suggested))) == ['field_missing']


def test_duplicate_open_exception_is_a_duplicate_reason_and_an_exceptions_lane():
    reasons = check(open_exceptions=[{'type': 'VALIDATION:EX-007'}])
    assert codes(reasons) == ['open_exception', 'duplicate_candidate']
    assert lane_for(reasons) == 'exceptions'


def test_lanes_split_exceptions_from_documents_to_check():
    assert lane_for(check(open_exceptions=[{'type': 'X'}])) == 'exceptions'
    assert lane_for(check(fields=receipt_fields(total=None))) == 'to_check'


def test_supplier_policy_off_drops_only_the_supplier_condition():
    unknown = [{'id': 'v2', 'vendor_name': 'Someone Else Trading'}]
    assert check(catalogue=unknown, requires_known_supplier=False) == []
    assert codes(check(catalogue=None)) == ['supplier_not_known']  # An unreadable list is never ready.


def test_close_supplier_name_says_which_name_is_closest():
    reasons = check(catalogue=[{'id': 'v1', 'vendor_name': 'Acme Supplies Sdn Bhd.'}],
                    fields=receipt_fields(supplier=field('supplier', 'Acme Suplies Sdn Bhd')))
    assert codes(reasons) == ['supplier_not_known']


def test_receivables_match_the_customer_list():
    document = {**DOCUMENT, 'kind': 'invoice', 'category': 'accounts_receivable'}
    rows = receipt_fields(invoice=field('invoice', 'INV-1'), customer=field('customer', 'Beta Retail'))
    assert check(document=document, fields=rows, catalogue=[{'id': 'c1', 'vendor_name': 'Beta Retail'}]) == []
    reasons = check(document=document, fields=rows, catalogue=VENDORS)
    assert codes(reasons) == ['supplier_not_known'] and reasons[0]['details']['role'] == 'customer'


def test_bill_reference_falls_back_like_standardise():
    document = {**DOCUMENT, 'kind': 'bill'}
    assert codes(check(document=document)) == ['field_missing']
    assert check(document=document, fields=receipt_fields(account=field('account_number', 'ACC-9'))) == []


def test_required_fields_follow_company_validation_settings():
    rules = {'require_supplier': False, 'require_invoice_number_for_invoice': False,
             'require_reference_for_bill': True, 'require_receipt_identifier': True}
    assert required_fields({'kind': 'invoice', 'category': 'accounts_payable'}, rules) == ['date', 'currency', 'total']
    assert required_fields({'kind': 'receipt'}, rules)[-1] == 'reference'
    assert 'customer' in required_fields({'kind': 'invoice', 'category': 'accounts_receivable'})


def test_required_fields_match_standardise_document():
    from services.routes.documents import routes
    source = textwrap.dedent(inspect.getsource(routes.standardise_document))
    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', None) == 'required_by_kind')
    standardise = {}
    for key, value in zip(node.value.keys, node.value.values):
        target = value.left if isinstance(value, ast.BinOp) else value  # invoice adds customer for receivables
        standardise[ast.literal_eval(key)] = ast.literal_eval(target)
    assert standardise == readiness.REQUIRED_FIELDS_BY_KIND


def test_lane_visibility_matches_list_documents():
    from services.routes.documents import routes
    from services.routes.review import lanes
    assert lanes.ACCOUNTANT_DOCUMENT_VISIBILITY in inspect.getsource(routes.list_documents)


class PolicyDB:
    def __init__(self, column, value):
        self.column, self.value = column, value

    def execute(self, sql, params=()):
        self.result = ({'?column?': 1} if self.column else None) if 'information_schema' in sql else \
            ({'value': self.value} if self.value is not None else None)
        return self

    def fetchone(self):
        return self.result


@pytest.mark.parametrize('column, value, expected', [(False, None, True), (True, None, True),
                                                     (True, False, False), (True, True, True)])
def test_supplier_policy_defaults_to_true_and_reads_workspace_settings(column, value, expected):
    assert readiness.ready_lane_requires_known_supplier(PolicyDB(column, value), 't1') is expected


def test_an_assumed_date_order_is_not_verified_until_a_person_confirms_it():
    # 03/04/2026 read as day/month: text-layer evidence alone would say verified.
    issues = readiness.field_issue_codes([{'code': 'DATE_ORDER_ASSUMED', 'field': 'date', 'detail': 'read as day/month.'}])
    assert issues == {'date': {'DATE_ORDER_ASSUMED'}}
    reasons = check(issues=issues)
    assert codes(reasons) == ['field_needs_check'] and lane_for(reasons) == 'to_check'
    assert reasons[0]['details'] == {'fields': ['date'], 'assumed': {'date': ['DATE_ORDER_ASSUMED']}}
    assert 'day and month order was assumed' in reasons[0]['message']
    confirmed = field('date', None, current_value='2026-04-03', corrected_by='u1',
                      corrected_at=datetime(2026, 11, 18, tzinfo=timezone.utc))
    assert check(issues=issues, fields=receipt_fields(date=confirmed)) == []


def test_extraction_issues_map_onto_canonical_fields():
    assert readiness.field_issue_codes([
        {'code': 'DEFAULT_CURRENCY_APPLIED', 'field': 'currency'},
        {'code': 'DATE_ORDER_ASSUMED', 'detail': 'invoice_date: day and month both 12 or less'},
        {'code': 'OCR_REQUIRES_REVIEW', 'detail': 'OCR spelling needs review'}, 'not an issue', {'field': 'total'},
    ]) == {'currency': {'DEFAULT_CURRENCY_APPLIED'}, 'date': {'DATE_ORDER_ASSUMED'}}
    assert codes(check(issues={'currency': {'DEFAULT_CURRENCY_APPLIED'}})) == ['field_needs_check']
    assert check(issues={'total': {'OCR_REQUIRES_REVIEW'}}) == []  # Only assumptions block a verified field.


@pytest.mark.parametrize('kind, expected', [('credit_note', 'CN-7'), ('debit_note', 'CN-7'), ('invoice', 'INV-1')])
def test_the_number_shown_is_the_documents_own_number(kind, expected):
    rows = receipt_fields(original=field('original_invoice_number', 'INV-1'), own=field('credit_note_number', 'CN-7'))
    assert readiness.summary_values({**DOCUMENT, 'kind': kind}, rows)['number'] == expected
