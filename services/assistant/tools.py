"""The allowlisted, read-only tools Ask AccordAI may call (G2).

Every tool:

* has a Pydantic argument model with ``extra='forbid'``: enums for kinds, dates within five years of today, string
  lengths capped. **No tool has a tenant, user, SQL or free-form filter argument**; tenant and role come from the
  session (ToolContext.user);
* wraps an existing report or Today-page query, so the assistant's numbers equal the numbers on those pages;
* adds facts to the question's FactBook instead of returning numbers in prose, and sends text that came from
  documents or bank lines through the extraction sanitiser (ToolContext.untrusted);
* runs inside a READ ONLY transaction (services/assistant/runner.py).

Report modules import ``main``; they are imported inside each tool so that importing this module never starts the
application (main imports the route registry, which imports the assistant).
"""
from __future__ import annotations

import calendar
import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from security.assistant_guard import untrusted_text
from services.assistant.facts import FactBook

MAX_YEARS = 5
HIDDEN_TEXT = '[text hidden: it reads like an instruction; open the record to see it]'
ZERO = Decimal('0.00')
APPROVED_DOCUMENT_STATUSES = ('validated', 'standardised', 'completed')
SALES_TYPES = ('quotation', 'sales-order', 'delivery-order', 'invoice', 'credit-note', 'debit-note', 'payment', 'refund')
PURCHASE_TYPES = ('purchase-order', 'goods-received', 'bill', 'purchase-credit-note', 'purchase-debit-note',
                  'purchase-payment', 'purchase-refund')


def _within_five_years(value: date) -> date:
    if abs((value - date.today()).days) > MAX_YEARS * 366:
        raise ValueError('date must be within five years of today')
    return value


def _month_within_five_years(value: str) -> str:
    year, month = (int(part) for part in value.split('-'))
    _within_five_years(date(year, month, 1))
    return value


BoundedDate = Annotated[date, AfterValidator(_within_five_years)]
Month = Annotated[str, StringConstraints(pattern=r'^\d{4}-(0[1-9]|1[0-2])$'), AfterValidator(_month_within_five_years)]
Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]


class Args(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class NoArgs(Args):
    pass


class DateRangeArgs(Args):
    from_date: BoundedDate
    to_date: BoundedDate

    @model_validator(mode='after')
    def _ordered(self):
        if self.to_date < self.from_date:
            raise ValueError('to_date must be on or after from_date')
        return self


class ContactBalanceArgs(Args):
    contact: Query
    side: Literal['owed_to_us', 'we_owe', 'either'] = 'either'
    as_of: BoundedDate | None = None


class DueSoonArgs(Args):
    kind: Literal['bills', 'invoices']
    days: int = Field(7, ge=1, le=60)
    include_overdue: bool = False


class TopContactsArgs(DateRangeArgs):
    kind: Literal['suppliers', 'customers']
    limit: int = Field(5, ge=1, le=10)


class BalanceSheetArgs(Args):
    as_of: BoundedDate


class ComparePeriodsArgs(Args):
    period_a: Month
    period_b: Month


class BankUnmatchedArgs(Args):
    limit: int = Field(5, ge=1, le=10)


class SearchRecordsArgs(Args):
    query: Query
    kind: Literal['any', 'sales', 'purchases'] = 'any'


class SearchTextArgs(Args):
    query: Query


class ExplainDocumentArgs(Args):
    document_id: UUID


@dataclass
class ToolContext:
    """What a tool may know about the caller. Built from the session, never from model output."""
    user: dict
    today: date
    facts: FactBook
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    document_text: bool = False

    @property
    def tenant(self):
        return self.user['tenant_id']

    @property
    def accountant(self) -> bool:
        return self.user['role'] == 'accountant'

    def untrusted(self, text: Any, max_chars: int = 80) -> str:
        """Sanitised text from a document or bank line. Text the injection scan flags is not repeated at all: a
        marker is shown instead (the person opens the record to read it), so an instruction or a number planted in
        a supplier name or a scan never reaches the model or the answer. The flag goes to the audit row."""
        cleaned, findings = untrusted_text(text, max_chars=max_chars)
        for finding in findings:
            if finding not in self.flags:
                self.flags.append(finding)
        return HIDDEN_TEXT if findings else cleaned

    def note(self, text: str) -> None:
        self.notes.append(text)


def _clean_schema(node):
    if isinstance(node, dict):
        return {key: _clean_schema(value) for key, value in node.items() if key not in ('title', 'default')}
    if isinstance(node, list):
        return [_clean_schema(value) for value in node]
    return node


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    args: type[Args]
    run: Callable[[Any, ToolContext, Any], None]

    def schema(self) -> dict:
        parameters = _clean_schema(self.args.model_json_schema())
        parameters.setdefault('properties', {})
        parameters['additionalProperties'] = False
        return {'type': 'function', 'function': {'name': self.name, 'description': self.description,
                                                  'parameters': parameters}}


def _amount(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def _norm(text: str | None) -> str:
    return re.sub(r'[^0-9a-z]+', '', (text or '').casefold())


def _month_bounds(month: str) -> tuple[date, date]:
    year, number = (int(part) for part in month.split('-'))
    return date(year, number, 1), date(year, number, calendar.monthrange(year, number)[1])


def _accountant_scope(ctx: ToolContext) -> str:
    from services.routes.home.lanes import ACCOUNTANT_DOCUMENT_VISIBILITY
    return ' AND ' + ACCOUNTANT_DOCUMENT_VISIBILITY.replace('%s', '%(user)s') if ctx.accountant else ''


def _ageing_item_href(ctx: ToolContext, row: dict, report_type: str) -> str | None:
    item = row.get('itemId')
    if not item:
        return None
    if row.get('source') == 'typed':
        return ctx.facts.link('sales' if report_type == 'receivables' else 'purchases', item=item)
    if row.get('source') == 'scanned':
        return ctx.facts.link('records', item=item)
    return None


def _ageing_source(ctx: ToolContext, report_type: str, as_of: date) -> tuple[str, str]:
    return (f'Ageing of {report_type}, as of {as_of.isoformat()}',
            ctx.facts.link('reports', {'report': 'ageing', 'type': report_type, 'asOf': as_of.isoformat()}))


def _pnl_source(ctx: ToolContext, start: date, end: date) -> tuple[str, str]:
    return (f'Profit and loss, {start.isoformat()} to {end.isoformat()}',
            ctx.facts.link('reports', {'report': 'profit-and-loss', 'fromDate': start.isoformat(),
                                       'toDate': end.isoformat()}))


# ---------------------------------------------------------------------------------------------------------------
# Tools


def contact_balance(db, ctx: ToolContext, args: ContactBalanceArgs) -> None:
    from services.routes.reporting.financial import _ageing

    as_of = args.as_of or ctx.today
    sides = {'owed_to_us': ['receivables'], 'we_owe': ['payables'], 'either': ['receivables', 'payables']}[args.side]
    needle = _norm(args.contact)
    found = False
    for report_type in sides:
        report = _ageing(db, ctx.tenant, as_of, report_type, ids=True)
        names = sorted({row['contactName'] or '' for row in report['rows'] if row['contactName']})
        matched = [name for name in names if needle and needle in _norm(name)]
        if not matched:
            folded = {name.casefold(): name for name in names}
            matched = [folded[key] for key in difflib.get_close_matches(args.contact.casefold(), list(folded), 3, 0.75)]
        source, source_href = _ageing_source(ctx, report_type, as_of)
        for name in matched[:3]:
            found = True
            rows = [row for row in report['rows'] if row['contactName'] == name]
            shown = ctx.untrusted(name)
            label = f'Owed to us by {shown}' if report_type == 'receivables' else f'We owe {shown}'
            ctx.facts.add(label, sum((_amount(r['outstanding']) for r in rows), ZERO), 'MYR',
                          source=source, href=source_href)
            ctx.facts.add(f'Open items for {shown}', len(rows), 'count', source=source, href=source_href)
            for row in rows[:3]:
                due = row['dueDate'] or row['documentDate']
                number = ctx.untrusted(row['documentNumber'] or 'no number', 40)
                ctx.facts.add(f'{shown}: {number}, due {due.isoformat()}', row['outstanding'], 'MYR',
                              source=source, source_href=source_href, href=_ageing_item_href(ctx, row, report_type))
    if not found:
        ctx.note(f'No open items for a contact matching that name as of {as_of.isoformat()}.')


def due_soon(db, ctx: ToolContext, args: DueSoonArgs) -> None:
    from services.routes.reporting.financial import _ageing

    report_type = 'payables' if args.kind == 'bills' else 'receivables'
    noun = 'Bills' if args.kind == 'bills' else 'Invoices'
    today, end = ctx.today, ctx.today + timedelta(days=args.days)
    rows = _ageing(db, ctx.tenant, today, report_type, ids=True)['rows']

    def due(row):
        return row['dueDate'] or row['documentDate']

    soon = sorted((row for row in rows if today <= due(row) <= end), key=due)
    source, source_href = _ageing_source(ctx, report_type, today)
    ctx.facts.add(f'{noun} due in the next {args.days} days', len(soon), 'count', source=source, href=source_href)
    ctx.facts.add(f'Total of {noun.lower()} due in the next {args.days} days',
                  sum((_amount(r['outstanding']) for r in soon), ZERO), 'MYR', source=source, href=source_href)
    for row in soon[:5]:
        contact = ctx.untrusted(row['contactName'] or 'No contact named')
        number = ctx.untrusted(row['documentNumber'] or 'no number', 40)
        ctx.facts.add(f'{contact} {number}, due {due(row).isoformat()}', row['outstanding'], 'MYR',
                      source=source, source_href=source_href, href=_ageing_item_href(ctx, row, report_type))
    if args.include_overdue:
        late = sorted((row for row in rows if due(row) < today), key=due)
        ctx.facts.add(f'{noun} overdue', len(late), 'count', source=source, href=source_href)
        ctx.facts.add(f'Total of {noun.lower()} overdue', sum((_amount(r['outstanding']) for r in late), ZERO), 'MYR',
                      source=source, href=source_href)
        for row in late[:3]:
            contact = ctx.untrusted(row['contactName'] or 'No contact named')
            number = ctx.untrusted(row['documentNumber'] or 'no number', 40)
            ctx.facts.add(f'Overdue: {contact} {number}, due {due(row).isoformat()}', row['outstanding'], 'MYR',
                          source=source, source_href=source_href, href=_ageing_item_href(ctx, row, report_type))


def top_contacts(db, ctx: ToolContext, args: TopContactsArgs) -> None:
    from services.routes.reporting.business_summary import _by_contact

    kind = 'expense' if args.kind == 'suppliers' else 'income'
    items = _by_contact(db, ctx.tenant, args.from_date, args.to_date, kind)
    if args.from_date.day == 1 and args.from_date.replace(day=calendar.monthrange(
            args.from_date.year, args.from_date.month)[1]) == args.to_date:
        source = f'Business summary, {args.from_date.strftime("%Y-%m")}'
        source_href = ctx.facts.link('reports', {'view': 'business-summary', 'month': args.from_date.strftime('%Y-%m')})
    else:
        source, source_href = _pnl_source(ctx, args.from_date, args.to_date)
    word = 'Spend with' if args.kind == 'suppliers' else 'Sales to'
    named = [item for item in items if item['named']]
    for item in named[:args.limit]:
        ctx.facts.add(f'{word} {ctx.untrusted(item["name"])}', item['amount'], 'MYR', source=source, href=source_href)
    other = [item for item in items if not item['named']]
    for item in other:
        ctx.facts.add(item['name'], item['amount'], 'MYR', source=source, href=source_href)
    ctx.facts.add(f'Total {"costs" if args.kind == "suppliers" else "sales"} in the period',
                  sum((_amount(i['amount']) for i in items), ZERO), 'MYR', source=source, href=source_href)
    if not items:
        ctx.note('Nothing was posted in that period.')


def profit_and_loss(db, ctx: ToolContext, args: DateRangeArgs) -> None:
    from services.routes.reporting.financial import _profit_and_loss

    report = _profit_and_loss(db, ctx.tenant, args.from_date, args.to_date)
    source, href = _pnl_source(ctx, args.from_date, args.to_date)
    ctx.facts.add('Period start', args.from_date, 'date', source=source, href=href)
    ctx.facts.add('Period end', args.to_date, 'date', source=source, href=href)
    ctx.facts.add('Total income', report['totalIncome'], 'MYR', source=source, href=href)
    ctx.facts.add('Total expenses', report['totalExpenses'], 'MYR', source=source, href=href)
    ctx.facts.add('Net profit (recorded tax excluded)', report['netProfit'], 'MYR', source=source, href=href)
    expenses = sorted(_expense_accounts(db, ctx.tenant, args.from_date, args.to_date).items(),
                      key=lambda item: (-item[1], item[0]))
    for (code, name), amount in expenses[:5]:
        ctx.facts.add(f'Expense account {code} {name}', amount, 'MYR', source=source, href=href)


def _expense_accounts(db, tenant, start: date, end: date) -> dict[tuple[str, str], Decimal]:
    """Posted expense by account for the period, read the way the profit and loss reads it (opening entries out)."""
    from services.routes.reporting.financial import _account_balances

    return {(row['code'], row['name']): _amount(row['debit']) - _amount(row['credit'])
            for row in _account_balances(db, tenant, end, start, include_opening=False)
            if row['account_type'] == 'expense'}


def balance_sheet(db, ctx: ToolContext, args: BalanceSheetArgs) -> None:
    from services.routes.reporting.financial import _balance_sheet

    report = _balance_sheet(db, ctx.tenant, args.as_of)
    source = f'Balance sheet, as of {args.as_of.isoformat()}'
    href = ctx.facts.link('reports', {'report': 'balance-sheet', 'asOf': args.as_of.isoformat()})
    totals = {section['key']: section for section in report['sections']}
    ctx.facts.add('Balance sheet date', args.as_of, 'date', source=source, href=href)
    ctx.facts.add('Total assets', report['totalAssets'], 'MYR', source=source, href=href)
    ctx.facts.add('Total liabilities', totals['liabilities']['total'], 'MYR', source=source, href=href)
    ctx.facts.add('Total equity', totals['equity']['total'], 'MYR', source=source, href=href)
    ctx.facts.add('Total liabilities and equity', report['totalLiabilitiesAndEquity'], 'MYR', source=source, href=href)
    ctx.facts.add('Balance sheet balances', 'yes' if report['balanced'] else 'no', 'text', source=source, href=href)
    for account in sorted(totals['assets']['accounts'], key=lambda a: -_amount(a['amount']))[:5]:
        ctx.facts.add(f'Asset account {account["code"]} {account["name"]}', account['amount'], 'MYR',
                      source=source, href=href)


def compare_periods(db, ctx: ToolContext, args: ComparePeriodsArgs) -> None:
    from services.routes.reporting.financial import _profit_and_loss

    if args.period_a > args.period_b:
        # Always earlier -> later, so every "change" fact is the later month minus the earlier one. A model that
        # passed the months the other way round otherwise read "expenses rose" for a month in which they fell.
        args = args.model_copy(update={'period_a': args.period_b, 'period_b': args.period_a})
    a_start, a_end = _month_bounds(args.period_a)
    b_start, b_end = _month_bounds(args.period_b)
    a = _profit_and_loss(db, ctx.tenant, a_start, a_end)
    b = _profit_and_loss(db, ctx.tenant, b_start, b_end)
    a_source, a_href = _pnl_source(ctx, a_start, a_end)
    b_source, b_href = _pnl_source(ctx, b_start, b_end)
    ctx.facts.add('First period', args.period_a, 'text', source=a_source, href=a_href)
    ctx.facts.add('Second period', args.period_b, 'text', source=b_source, href=b_href)
    for key, label in (('totalIncome', 'Income'), ('totalExpenses', 'Expenses'), ('netProfit', 'Net profit')):
        ctx.facts.add(f'{label}, {args.period_a}', a[key], 'MYR', source=a_source, href=a_href)
        ctx.facts.add(f'{label}, {args.period_b}', b[key], 'MYR', source=b_source, href=b_href)
        # A difference of two report totals (subtraction only; nothing is estimated and no tax is calculated).
        ctx.facts.add(f'Change in {label.lower()}, {args.period_a} to {args.period_b}',
                      _amount(b[key]) - _amount(a[key]), 'MYR', source=b_source, href=b_href)

    first = _expense_accounts(db, ctx.tenant, a_start, a_end)
    second = _expense_accounts(db, ctx.tenant, b_start, b_end)
    changes = sorted(((key, second.get(key, ZERO) - first.get(key, ZERO)) for key in set(first) | set(second)),
                     key=lambda item: (-abs(item[1]), item[0]))
    for (code, name), change in changes[:3]:
        if change:
            ctx.facts.add(f'Change in expense account {code} {name}, {args.period_a} to {args.period_b}', change, 'MYR',
                          source=b_source, href=b_href)


def document_queue(db, ctx: ToolContext, _args: NoArgs) -> None:
    from services.routes.home.routes import document_tiles

    tiles = document_tiles(db, ctx.user)
    source = 'Today page document counts'
    for key, label in (('toReview', 'Documents to review'), ('readyToApprove', 'Documents ready to submit or approve'),
                       ('needsFixing', 'Documents that need fixing'),
                       ('waitingApproval', 'Documents waiting for approval')):
        tile = tiles[key]
        href = ctx.facts.link(tile['link']['page'], _query(tile['link']['query']))
        ctx.facts.add(label, tile['count'], 'count', source=source, href=href, source_href=ctx.facts.link('today'))
    for item in tiles['needsFixing'].get('topReasons') or []:
        ctx.facts.add(f'Documents with an open {ctx.untrusted(item["code"], 40)} exception', item['documents'], 'count',
                      source=source, href=ctx.facts.link('exceptions'), source_href=ctx.facts.link('today'))
    if ctx.accountant:
        ctx.note('Counts include only documents you uploaded or that are assigned to you.')


def _query(text: str | None) -> dict | None:
    if not text:
        return None
    return dict(part.split('=', 1) for part in text.split('&') if '=' in part)


def month_end_status(db, ctx: ToolContext, _args: NoArgs) -> None:
    from services.routes.home.routes import month_end

    status = month_end(db, ctx.user, ctx.today)
    source = 'Today page month-end checklist'
    source_href = ctx.facts.link('today')
    ctx.facts.add('Month-end period', status['period']['code'], 'text', source=source, href=source_href)
    names = {'reviewed': 'Documents reviewed', 'matched': 'Bank lines explained',
             'posted': 'Approved records posted', 'reconciled': 'Bank reconciliation periods completed'}
    for step in status['steps']:
        label = names.get(step['key'], step['key'])
        link = step.get('link') or {}
        href = ctx.facts.link(link['page'], _query(link.get('query'))) if link.get('page') else source_href
        if not step.get('available'):
            ctx.note(f'{label}: {step.get("reason")}')
            continue
        ctx.facts.add(f'{label}: done', step['done'], 'count', source=source, href=href, source_href=source_href)
        ctx.facts.add(f'{label}: total', step['total'], 'count', source=source, href=href, source_href=source_href)


def bank_unmatched(db, ctx: ToolContext, args: BankUnmatchedArgs) -> None:
    from services.routes.home.routes import _bank_predicates, bank_tile

    tile = bank_tile(db, ctx.user)
    href = ctx.facts.link('reconciliation', {'status': 'unmatched'})
    source = 'Bank lines still to match'
    if not tile.get('available'):
        ctx.note(tile.get('reason') or 'Bank statements are not set up yet.')
        return
    ctx.facts.add('Bank lines still to match', tile['count'], 'count', source=source, href=href)
    ctx.facts.add('Money-in lines still to match', tile['moneyIn'], 'count', source=source, href=href)
    ctx.facts.add('Money-out lines still to match', tile['moneyOut'], 'count', source=source, href=href)
    to_match, _ = _bank_predicates(db)
    base = f"""FROM bank_transactions t JOIN bank_imports i ON i.id=t.import_id AND i.tenant_id=t.tenant_id
        WHERE i.status='confirmed' AND t.tenant_id=%(tenant)s AND {to_match}"""
    sums = db.execute(f"""SELECT
            COALESCE(SUM(t.signed_amount_decimal) FILTER (WHERE t.signed_amount_decimal>0 AND t.currency='MYR'),0) AS money_in,
            COALESCE(-SUM(t.signed_amount_decimal) FILTER (WHERE t.signed_amount_decimal<0 AND t.currency='MYR'),0) AS money_out
        {base}""", {'tenant': ctx.tenant}).fetchone()
    ctx.facts.add('Unmatched money in (RM lines)', sums['money_in'], 'MYR', source=source, href=href)
    ctx.facts.add('Unmatched money out (RM lines)', sums['money_out'], 'MYR', source=source, href=href)
    for row in db.execute(f"""SELECT t.posted_date,t.description,t.signed_amount_decimal,t.currency {base}
            ORDER BY t.posted_date NULLS LAST,t.created_at LIMIT %(limit)s""",
                          {'tenant': ctx.tenant, 'limit': args.limit}).fetchall():
        day = row['posted_date'].isoformat() if row['posted_date'] else 'no date'
        label = f'Bank line {day}: {ctx.untrusted(row["description"] or "no description", 60)}'
        if row['currency'] == 'MYR':
            ctx.facts.add(label, row['signed_amount_decimal'], 'MYR', source=source, href=href)
        else:
            ctx.facts.add(label, f'{row["currency"]} {_amount(row["signed_amount_decimal"]):.2f}', 'text',
                          source=source, href=href)


def search_records(db, ctx: ToolContext, args: SearchRecordsArgs) -> None:
    from services.routes.search.routes import like_pattern

    text = args.query.casefold()
    params = {'tenant': ctx.tenant, 'q': text, 'like': like_pattern(text),
              'category': {'sales': 'accounts_receivable', 'purchases': 'accounts_payable'}.get(args.kind)}
    haystack = ("lower(coalesce(r.supplier_name,'')||' '||coalesce(r.buyer_name,'')||' '||"
                "coalesce(r.invoice_number,'')||' '||coalesce(r.reference_number,''))")
    found = 0
    for row in db.execute(f"""SELECT r.id,r.record_code,r.invoice_number,r.supplier_name,r.buyer_name,r.category,
                r.invoice_date,r.total_amount,r.currency,d.kind,word_similarity(%(q)s,{haystack}) AS score
            FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
                AND d.excluded_at IS NULL
            WHERE r.tenant_id=%(tenant)s AND r.is_current AND r.validation_status='passed'
                AND (%(category)s::text IS NULL OR r.category=%(category)s)
                AND ({haystack} LIKE %(like)s ESCAPE '\\' OR word_similarity(%(q)s,{haystack}) > 0.45)
            ORDER BY score DESC, r.invoice_date DESC NULLS LAST LIMIT 5""", params).fetchall():
        found += 1
        party = row['buyer_name'] if row['category'] == 'accounts_receivable' else row['supplier_name']
        number = ctx.untrusted(row['invoice_number'] or row['record_code'], 40)
        day = row['invoice_date'].isoformat() if row['invoice_date'] else 'no date'
        href = ctx.facts.link('records', item=str(row['id']))
        _money_fact(ctx, f'Approved {row["kind"]} {number} from {ctx.untrusted(party or "no name")} ({day})',
                    row['total_amount'], row['currency'], source=f'Record {number}', href=href)
    if _table_exists(db, 'erp_document_headers'):
        types = {'sales': list(SALES_TYPES), 'purchases': list(PURCHASE_TYPES)}.get(args.kind,
                                                                                  list(SALES_TYPES + PURCHASE_TYPES))
        for row in db.execute("""SELECT id,document_type,document_number,contact_name,document_date,total_amount,
                    currency,status FROM erp_document_headers
                WHERE tenant_id=%(tenant)s AND document_type=ANY(%(types)s) AND (lower(document_number) LIKE %(like)s
                    ESCAPE '\\' OR lower(COALESCE(contact_name,'')) LIKE %(like)s ESCAPE '\\'
                    OR lower(COALESCE(reference_no,'')) LIKE %(like)s ESCAPE '\\')
                ORDER BY (lower(document_number)=%(q)s) DESC, document_date DESC, created_at DESC LIMIT 5""",
                              {**params, 'types': types}).fetchall():
            found += 1
            number = ctx.untrusted(row['document_number'], 40)
            page = 'sales' if row['document_type'] in SALES_TYPES else 'purchases'
            day = row['document_date'].isoformat() if row['document_date'] else 'no date'
            label = (f'{row["document_type"].replace("-", " ").capitalize()} {number}, '
                     f'{ctx.untrusted(row["contact_name"] or "no contact")} ({day}, {row["status"]})')
            _money_fact(ctx, label, row['total_amount'], row['currency'], source=f'{page.capitalize()} {number}',
                        href=ctx.facts.link(page, item=str(row['id'])))
    if not found:
        ctx.note('No approved record or typed document matches that search.')


def _money_fact(ctx: ToolContext, label, amount, currency, *, source, href) -> None:
    if (currency or 'MYR') == 'MYR':
        ctx.facts.add(label, amount, 'MYR', source=source, href=href)
    else:
        ctx.facts.add(label, f'{currency} {_amount(amount):.2f}', 'text', source=source, href=href)


def _table_exists(db, name: str) -> bool:
    return bool(db.execute('SELECT to_regclass(%s) IS NOT NULL AS ok', (name,)).fetchone()['ok'])


def search_document_text(db, ctx: ToolContext, args: SearchTextArgs) -> None:
    from services.routes.search.routes import like_pattern

    text = args.query.casefold()
    params = {'tenant': ctx.tenant, 'q': text, 'like': like_pattern(text), 'user': ctx.user['id'],
              'statuses': list(APPROVED_DOCUMENT_STATUSES)}
    scope = _accountant_scope(ctx)
    rows = db.execute(f"""WITH readings AS (
            SELECT d.id,d.document_code,d.kind,fp.field_label AS label,
                COALESCE(fp.current_value,fp.raw_value) AS body
            FROM field_predictions fp
            JOIN raw_extractions x ON x.id=fp.extraction_id AND x.is_current
            JOIN documents d ON d.id=fp.document_id
            WHERE d.tenant_id=%(tenant)s AND d.excluded_at IS NULL AND d.status=ANY(%(statuses)s){scope}
            UNION ALL
            SELECT d.id,d.document_code,d.kind,'Line description',li.description
            FROM line_items li
            JOIN raw_extractions x ON x.id=li.extraction_id AND x.is_current
            JOIN documents d ON d.id=li.document_id
            WHERE d.tenant_id=%(tenant)s AND d.excluded_at IS NULL AND d.status=ANY(%(statuses)s){scope}
        )
        SELECT id,document_code,kind,label,body,
            ts_rank(to_tsvector('simple',body),plainto_tsquery('simple',%(q)s)) AS rank,
            word_similarity(%(q)s,lower(body)) AS score
        FROM readings
        WHERE body IS NOT NULL AND btrim(body)<>'' AND (to_tsvector('simple',body) @@ plainto_tsquery('simple',%(q)s)
            OR lower(body) LIKE %(like)s ESCAPE '\\' OR word_similarity(%(q)s,lower(body)) > 0.5)
        ORDER BY rank DESC, score DESC, document_code LIMIT 6""", params).fetchall()
    for row in rows:
        ctx.document_text = True
        code = ctx.untrusted(row['document_code'], 40)
        ctx.facts.add(f'Text read from {row["kind"]} {code}, {ctx.untrusted(row["label"], 40)}',
                      ctx.untrusted(row['body'], 120), 'text', source=f'Document {code}',
                      href=ctx.facts.link('documents', item=str(row['id'])))
    if not rows:
        ctx.note('No approved document contains that text.')


def explain_document(db, ctx: ToolContext, args: ExplainDocumentArgs) -> None:
    from services.review.readiness import _value, assess_documents, field_is_verified
    from services.review.risk import risk

    params = {'tenant': ctx.tenant, 'id': str(args.document_id), 'user': ctx.user['id']}
    document = db.execute(f"""SELECT d.* FROM documents d WHERE d.id=%(id)s AND d.tenant_id=%(tenant)s
        AND d.excluded_at IS NULL{_accountant_scope(ctx)}""", params).fetchone()
    if not document:
        ctx.note('No document with that id is visible to you.')
        return
    key = str(document['id'])
    reasons_by_id, fields_by_id, _policy = assess_documents(db, ctx.tenant, [document])
    reasons, fields = reasons_by_id.get(key, []), fields_by_id.get(key, [])
    unverified = sum(1 for row in fields if _value(row) is not None and not field_is_verified(row))
    level = risk(reasons, unverified)
    code = ctx.untrusted(document.get('document_code') or 'document', 40)
    source, href = f'Document {code} review', ctx.facts.link('documents', item=key)
    ctx.facts.add(f'Document {code} status', document['status'], 'text', source=source, href=href)
    ctx.facts.add('Review risk level', level['level'], 'text', source=source, href=href)
    ctx.facts.add('Fields not yet confirmed', unverified, 'count', source=source, href=href)
    for item in reasons[:5]:
        ctx.facts.add('Reason to check', ctx.untrusted(item.get('message'), 160), 'text', source=source, href=href)
    for row in db.execute("""SELECT type,severity,message FROM exceptions
            WHERE tenant_id=%(tenant)s AND document_id=%(id)s AND status<>'resolved'
            ORDER BY created_at LIMIT 5""", params).fetchall():
        ctx.facts.add(f'Open exception {ctx.untrusted(row["type"], 40)} ({row["severity"]})',
                      ctx.untrusted(row['message'], 160), 'text', source=source, href=href)
    for row in db.execute("""SELECT v.rule_id,v.status,v.message FROM validation_results v
            JOIN documents d ON d.id=v.document_id AND d.tenant_id=v.tenant_id
            WHERE v.tenant_id=%(tenant)s AND v.document_id=%(id)s AND v.content_revision=d.content_revision
                AND (v.status='failed' OR v.prevents_approval OR (v.status='not_checked' AND v.requires_review))
            ORDER BY v.rule_id LIMIT 5""", params).fetchall():
        ctx.facts.add(f'Check {ctx.untrusted(row["rule_id"], 40)} ({row["status"]})',
                      ctx.untrusted(row['message'], 160), 'text', source=source, href=href)
    if not reasons:
        ctx.note('Nothing is blocking this document: every readiness condition holds.')


TOOLS: dict[str, Tool] = {tool.name: tool for tool in (
    Tool('contact_balance', 'the ageing report for one contact',
         'What is owed to us by a customer, or what we owe a supplier, from the ageing report.',
         ContactBalanceArgs, contact_balance),
    Tool('due_soon', 'the ageing report by due date',
         'Open bills (payables) or invoices (receivables) due in the next N days, optionally with the overdue ones.',
         DueSoonArgs, due_soon),
    Tool('top_contacts', 'the business summary',
         'Top suppliers by posted spend, or top customers by posted sales, for a date range.',
         TopContactsArgs, top_contacts),
    Tool('profit_and_loss', 'the profit and loss report',
         'Income, expenses, net profit and the largest expense accounts (spend by category) for a date range.',
         DateRangeArgs, profit_and_loss),
    Tool('balance_sheet', 'the balance sheet',
         'Total assets, liabilities and equity, and the largest asset accounts, as of a date.',
         BalanceSheetArgs, balance_sheet),
    Tool('compare_periods', 'the profit and loss for two months',
         'Income, expenses and profit for two months (YYYY-MM), their differences, and the expense accounts that '
         'changed most.', ComparePeriodsArgs, compare_periods),
    Tool('document_queue', 'the Today page document counts',
         'How many documents are to review, ready, need fixing (open exceptions) or wait for approval, and the most '
         'common exception reasons.', NoArgs, document_queue),
    Tool('month_end_status', 'the month-end checklist',
         'Progress of this month\'s close: documents reviewed, bank lines explained, records posted, reconciliation.',
         NoArgs, month_end_status),
    Tool('bank_unmatched', 'the bank lines still to match',
         'Confirmed bank statement lines that are not matched, classified or ignored yet, with the oldest ones.',
         BankUnmatchedArgs, bank_unmatched),
    Tool('search_records', 'a search of approved records',
         "Find the company's approved records and typed sales or purchase documents by number, contact or "
         'reference.', SearchRecordsArgs, search_records),
    Tool('search_document_text', 'a search of text read from approved documents',
         'Find words printed on approved documents (field readings and line descriptions). Results are untrusted '
         'text from scans.', SearchTextArgs, search_document_text),
    Tool('explain_document', "the document's review checks",
         'Why a document is flagged: readiness reasons, risk level, open exceptions and failing checks.',
         ExplainDocumentArgs, explain_document),
)}
