"""Today page summary (plan v2, task B3): what an accountant or local admin has to do today.

One read-only endpoint, `GET /api/home/summary`, built from existing tables and status meanings only (see lanes.py).
Each tile carries the list it opens (`link`: the frontend page segment and query). A figure the current schema cannot
give is returned as `{"available": false, "reason": ...}`, never as an estimate. Amounts are decimal strings in RM;
records in another currency are counted but left out of the RM total (`otherCurrencyCount`).

Scope: document tiles follow the document visibility rule (an accountant sees documents they own or are assigned);
money, ledger and month-end tiles are company-wide, as the existing bank, records and ledger endpoints are.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from main import *  # noqa: F401,F403  (require_workspace_user, connect, HTTPException, Request)

from services.routes.home.lanes import (
    ACCOUNTANT_DOCUMENT_VISIBILITY, APPROVED_RECORD, BANK_LINE_CLASSIFIED, BANK_LINE_TO_MATCH, DOCUMENT_LANES,
    OPEN_ITEM_TABLES, OUTSTANDING, POSTED_JOURNAL_FOR_RECORD, company_today, due_window, open_item_base, records_to_post,
)

ROLES = {'accountant', 'local_admin'}
HOME_CURRENCY = 'MYR'
DUE_SOON_DAYS = 7


def _money(value) -> str:
    return str(Decimal(value or 0).quantize(Decimal('0.01')))


def _link(page: str, query: str | None = None) -> dict:
    return {'page': page, 'query': query}


def unavailable(reason: str, link: dict | None = None, **extra) -> dict:
    return {'available': False, 'reason': reason, 'link': link, **extra}


def _table_exists(db, name: str) -> bool:
    return bool(db.execute('SELECT to_regclass(%s) IS NOT NULL AS ok', (name,)).fetchone()['ok'])


def _document_scope(user) -> tuple[str, list]:
    clauses, params = ['d.tenant_id=%s', 'd.excluded_at IS NULL'], [user['tenant_id']]
    if user['role'] == 'accountant':
        clauses.append(ACCOUNTANT_DOCUMENT_VISIBILITY)
        params += [user['id'], user['id']]
    return ' AND '.join(clauses), params


def document_tiles(db, user) -> dict:
    scope, params = _document_scope(user)
    counts = db.execute(f"""SELECT
            count(*) FILTER (WHERE {DOCUMENT_LANES['to-review']}) AS to_review,
            count(*) FILTER (WHERE {DOCUMENT_LANES['ready']}) AS ready,
            count(*) FILTER (WHERE {DOCUMENT_LANES['needs-fixing']}) AS needs_fixing,
            count(*) FILTER (WHERE {DOCUMENT_LANES['waiting-approval']}) AS waiting_approval
        FROM documents d WHERE {scope}""", params).fetchone()
    reasons = db.execute(f"""SELECT regexp_replace(e.type, '^VALIDATION:', '') AS code, count(DISTINCT e.document_id) AS documents
        FROM exceptions e JOIN documents d ON d.id=e.document_id AND d.tenant_id=e.tenant_id
        WHERE {scope} AND e.status <> 'resolved'
        GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 3""", params).fetchall()
    approvals_page = _link('approvals') if user['role'] == 'local_admin' else _link('documents', 'lane=waiting-approval')
    return {
        'toReview': {'available': True, 'scope': 'yours', 'count': counts['to_review'], 'link': _link('documents', 'lane=to-review')},
        'readyToApprove': {'available': True, 'scope': 'yours', 'count': counts['ready'], 'link': _link('documents', 'lane=ready')},
        'needsFixing': {'available': True, 'scope': 'yours', 'count': counts['needs_fixing'],
                        'topReasons': [{'code': r['code'], 'documents': r['documents']} for r in reasons],
                        # The documents list uses the same visibility and predicate as this count; the Exceptions
                        # page does not show enabled benchmark-demo documents, so it could show less than the tile.
                        'link': _link('documents', 'lane=needs-fixing')},
        'waitingApproval': {'available': True, 'scope': 'yours', 'count': counts['waiting_approval'], 'link': approvals_page},
    }


def _bank_predicates(db) -> tuple[str, str]:
    """(still to match, explained) for alias t. Without the bank-coding tables nothing can be classified yet."""
    if _table_exists(db, 'current_bank_coding'):
        return BANK_LINE_TO_MATCH, f"(t.status IN ('matched','ignored') OR {BANK_LINE_CLASSIFIED})"
    return "t.status IN ('imported','needs_review','proposed')", "t.status IN ('matched','ignored')"


def bank_tile(db, user) -> dict:
    link = _link('reconciliation', 'status=unmatched')
    if not _table_exists(db, 'bank_transactions'):
        return unavailable('Bank statements are not set up on this server yet.', link)
    to_match, _ = _bank_predicates(db)
    row = db.execute(f"""SELECT count(*) AS lines,
            count(*) FILTER (WHERE t.direction='money_in') AS money_in,
            count(*) FILTER (WHERE t.direction='money_out') AS money_out
        FROM bank_transactions t JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
        WHERE i.status='confirmed' AND t.tenant_id=%s AND {to_match}""",
                     (user['tenant_id'],)).fetchone()
    return {'available': True, 'scope': 'company', 'count': row['lines'], 'moneyIn': row['money_in'],
            'moneyOut': row['money_out'], 'link': link}


def open_item_tiles(db, user, category: str, today: date) -> dict:
    """Due soon and overdue for one side (payables or receivables) in one query."""
    # The list carries the same date the tile was counted on, so a summary for another day opens that day's list.
    category_query = 'category=' + category.upper()
    due_query = lambda window: f"{category_query}&due={window}&asOf={today.isoformat()}"  # noqa: E731
    missing = [name for name in OPEN_ITEM_TABLES if not _table_exists(db, name)]
    if missing:
        reason = 'Approved records and payments are not set up on this server yet, so open bills and invoices cannot be counted.'
        return {'soon': unavailable(reason, _link('records', due_query('next-7-days'))),
                'overdue': unavailable(reason, _link('records', due_query('overdue')))}
    base, params = open_item_base(category)
    soon_sql, soon_params = due_window('next-7-days', today)
    late_sql, late_params = due_window('overdue', today)
    row = db.execute(f"""SELECT
            count(*) FILTER (WHERE {soon_sql}) AS soon_count,
            COALESCE(sum({OUTSTANDING}) FILTER (WHERE {soon_sql} AND r.currency=%s), 0) AS soon_amount,
            count(*) FILTER (WHERE {soon_sql} AND r.currency<>%s) AS soon_other,
            count(*) FILTER (WHERE {late_sql}) AS late_count,
            COALESCE(sum({OUTSTANDING}) FILTER (WHERE {late_sql} AND r.currency=%s), 0) AS late_amount,
            count(*) FILTER (WHERE {late_sql} AND r.currency<>%s) AS late_other,
            count(*) FILTER (WHERE r.due_date IS NULL) AS no_due_date
        FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id AND d.excluded_at IS NULL
        WHERE r.tenant_id=%s AND {base}""",
                     [*soon_params, *soon_params, HOME_CURRENCY, *soon_params, HOME_CURRENCY,
                      *late_params, *late_params, HOME_CURRENCY, *late_params, HOME_CURRENCY,
                      user['tenant_id'], *params]).fetchone()
    row = dict(row)
    typed = typed_open_items(db, user, category, today, soon_sql, soon_params, late_sql, late_params)
    for key in ('soon_count', 'soon_amount', 'soon_other', 'late_count', 'late_amount', 'late_other', 'no_due_date'):
        row[key] = (row[key] or 0) + (typed[key] or 0)
    return {
        'soon': {'available': True, 'scope': 'company', 'count': row['soon_count'], 'amount': _money(row['soon_amount']),
                 'currency': HOME_CURRENCY, 'otherCurrencyCount': row['soon_other'], 'days': DUE_SOON_DAYS,
                 'noDueDate': row['no_due_date'], 'link': _link('records', due_query('next-7-days'))},
        'overdue': {'available': True, 'scope': 'company', 'count': row['late_count'], 'amount': _money(row['late_amount']),
                    'currency': HOME_CURRENCY, 'otherCurrencyCount': row['late_other'], 'noDueDate': row['no_due_date'],
                    'link': _link('records', due_query('overdue'))},
    }


def typed_open_items(db, user, category: str, today: date, soon_sql, soon_params, late_sql, late_params) -> dict:
    """Plan L4: typed invoices/bills that are ready (posted), less what payments and credit notes settled.
    Same windows as the scanned records; the tiles add both so they agree with the ageing report."""
    if not _table_exists(db, 'erp_document_headers'):
        return {}
    document_type = ['bill', 'purchase-debit-note'] if category == 'accounts_payable' else ['invoice', 'debit-note']
    balance = """GREATEST(h.total_amount-COALESCE((SELECT SUM(a.amount) FROM erp_document_allocations a
        WHERE a.tenant_id=h.tenant_id AND a.target_document_id=h.id AND a.status='active'),0),0)"""
    return db.execute(f"""SELECT
            count(*) FILTER (WHERE {soon_sql}) AS soon_count,
            COALESCE(sum(r.balance) FILTER (WHERE {soon_sql} AND r.currency=%s), 0) AS soon_amount,
            count(*) FILTER (WHERE {soon_sql} AND r.currency<>%s) AS soon_other,
            count(*) FILTER (WHERE {late_sql}) AS late_count,
            COALESCE(sum(r.balance) FILTER (WHERE {late_sql} AND r.currency=%s), 0) AS late_amount,
            count(*) FILTER (WHERE {late_sql} AND r.currency<>%s) AS late_other,
            count(*) FILTER (WHERE r.due_date IS NULL) AS no_due_date
        FROM (SELECT h.due_date,h.currency,{balance} AS balance FROM erp_document_headers h
              WHERE h.tenant_id=%s AND h.document_type=ANY(%s) AND h.status='ready') r
        WHERE r.balance > 0""",
                      [*soon_params, *soon_params, HOME_CURRENCY, *soon_params, HOME_CURRENCY,
                       *late_params, *late_params, HOME_CURRENCY, *late_params, HOME_CURRENCY,
                       user['tenant_id'], document_type]).fetchone() or {}


def journals_tile(db, user) -> dict:
    link = _link('records', 'posting=to-post')
    if not _table_exists(db, 'journal_headers'):
        return unavailable('The ledger is not set up on this server yet, so nothing can be posted.', link)
    row = db.execute(f"""SELECT count(*) AS records,
            (SELECT count(*) FROM journal_headers WHERE tenant_id=%s AND status='draft') AS drafts
        FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id AND d.excluded_at IS NULL
        WHERE r.tenant_id=%s AND {records_to_post()}""",
                     (user['tenant_id'], user['tenant_id'])).fetchone()
    return {'available': True, 'scope': 'company', 'count': row['records'], 'draftJournals': row['drafts'], 'link': link}


def bank_payments_tile(db, user) -> dict:
    """Bank lines matched to bills or invoices whose payment is not in the ledger yet (a local admin posts it)."""
    from services.routes.accounting.routes import UNPOSTED_BANK_PAYMENT

    link = _link('reconciliation', 'status=to-post')
    if not _table_exists(db, 'bank_transactions') or not _table_exists(db, 'journal_headers'):
        return unavailable('Bank statements or the ledger are not set up on this server yet.', link)
    row = db.execute(f"""SELECT count(*) AS payment_lines FROM bank_transactions t
        JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
        WHERE i.status='confirmed' AND t.tenant_id=%s AND {UNPOSTED_BANK_PAYMENT}""", (user['tenant_id'],)).fetchone()
    return {'available': True, 'scope': 'company', 'count': row['payment_lines'], 'link': link}


def current_period(db, user, today: date) -> dict:
    """The open accounting period that contains today, otherwise the calendar month."""
    if _table_exists(db, 'accounting_periods'):
        row = db.execute("""SELECT period_code, starts_on, ends_on, status FROM accounting_periods
            WHERE tenant_id=%s AND starts_on <= %s AND ends_on >= %s ORDER BY starts_on DESC LIMIT 1""",
                         (user['tenant_id'], today, today)).fetchone()
        if row:
            return {'code': row['period_code'], 'startsOn': row['starts_on'], 'endsOn': row['ends_on'],
                    'status': row['status'], 'source': 'accounting_period'}
    start = today.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return {'code': start.strftime('%Y-%m'), 'startsOn': start, 'endsOn': end, 'status': None, 'source': 'calendar_month'}


def _step(key: str, done: int, total: int, link: dict, basis: str) -> dict:
    return {'key': key, 'available': True, 'done': done, 'total': total, 'basis': basis, 'link': link}


def month_end(db, user, today: date) -> dict:
    period = current_period(db, user, today)
    start, end, tenant = period['startsOn'], period['endsOn'], user['tenant_id']
    steps = []
    docs = db.execute("""SELECT count(*) AS total,
            count(*) FILTER (WHERE d.status IN ('in_review','validated','standardised','completed')) AS done
        FROM documents d WHERE d.tenant_id=%s AND d.excluded_at IS NULL AND d.uploaded_at::date BETWEEN %s AND %s""", (tenant, start, end)).fetchone()
    steps.append(_step('reviewed', docs['done'], docs['total'], _link('documents', 'lane=to-review'),
                       'Documents uploaded in this period that were checked and submitted or approved.'))
    if _table_exists(db, 'bank_transactions'):
        _, explained = _bank_predicates(db)
        bank = db.execute(f"""SELECT count(*) AS total, count(*) FILTER (WHERE {explained}) AS done
            FROM bank_transactions t JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
            WHERE i.status='confirmed' AND t.tenant_id=%s AND t.posted_date BETWEEN %s AND %s""", (tenant, start, end)).fetchone()
        steps.append(_step('matched', bank['done'], bank['total'], _link('reconciliation', 'status=unmatched'),
                           'Bank lines dated in this period that are matched, classified (fees, interest) or ignored.'))
    else:
        steps.append({'key': 'matched', **unavailable('Bank statements are not set up on this server yet.', _link('reconciliation'))})
    if _table_exists(db, 'journal_headers'):
        posted = db.execute(f"""SELECT count(*) AS total, count(*) FILTER (WHERE {POSTED_JOURNAL_FOR_RECORD}) AS done
            FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id AND d.excluded_at IS NULL
            WHERE r.tenant_id=%s AND {APPROVED_RECORD}
              AND COALESCE(r.invoice_date, r.created_at::date) BETWEEN %s AND %s""", (tenant, start, end)).fetchone()
        steps.append(_step('posted', posted['done'], posted['total'], _link('records', 'posting=to-post'),
                           'Approved records dated in this period that have a posted journal.'))
    else:
        steps.append({'key': 'posted', **unavailable('The ledger is not set up on this server yet.', _link('ledger'))})
    reconciled = None
    if _table_exists(db, 'bank_reconciliation_periods'):
        reconciled = db.execute("""SELECT count(*) AS total, count(*) FILTER (WHERE status='completed') AS done
            FROM bank_reconciliation_periods WHERE tenant_id=%s AND starts_on <= %s AND ends_on >= %s""",
                                (tenant, end, start)).fetchone()
    if reconciled and reconciled['total']:
        steps.append(_step('reconciled', reconciled['done'], reconciled['total'], _link('reconciliation'),
                           'Bank reconciliation periods overlapping this period that are completed.'))
    else:
        steps.append({'key': 'reconciled', **unavailable(
            'No bank reconciliation period covers this month yet. Start one on the Reconcile page.', _link('reconciliation'))})
    return {'available': True, 'scope': 'company',
            'period': {**period, 'startsOn': str(start), 'endsOn': str(end)}, 'steps': steps}


def home_summary(request: Request, asOf: date | None = None):
    user = require_workspace_user(request, ROLES)
    with connect() as db:
        today = asOf or company_today(db, user['tenant_id'])
        payables = open_item_tiles(db, user, 'accounts_payable', today)
        receivables = open_item_tiles(db, user, 'accounts_receivable', today)
        tiles = {
            **document_tiles(db, user),
            'bankToMatch': bank_tile(db, user),
            'billsDueSoon': payables['soon'],
            'billsOverdue': payables['overdue'],
            'invoicesOverdue': receivables['overdue'],
            'journalsToPost': journals_tile(db, user),
            'paymentsToPost': bank_payments_tile(db, user),
        }
        month = month_end(db, user, today)
    return {'asOf': str(today), 'currency': HOME_CURRENCY, 'role': user['role'].upper(), 'tiles': tiles, 'monthEnd': month}


def register(router, handlers):
    router.add_api_route('/api/home/summary', home_summary, methods=['GET'])
