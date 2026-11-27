"""Fraud Shield (F2): deterministic, explainable checks on documents. No model is used.

Every flag is advisory. Nothing here writes to the database, blocks an action, or sends data to an AI provider.
Bank account numbers are compared in full locally, and only the last four digits leave this module.

The module has two halves:
- `load_documents` reads one company's document facts in a few indexed queries (read-only);
- `detect_*` are pure functions over those facts, unit-tested without a database.

Contract: docs/api/insights-fraud-inbox.md.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from validation.matching.vendor_matching import normalize_vendor_name

CODES = ('FS-BANK-CHANGE', 'FS-NEAR-DUP', 'FS-NEW-LARGE', 'FS-ROUND', 'FS-SOD')
SEVERITY_ORDER = {'high': 0, 'medium': 1, 'low': 2}
APPROVED_STATUSES = frozenset({'validated', 'standardised', 'completed'})

NEAR_DUP_DAYS = 14
NEW_SUPPLIER_DAYS = 30
NEW_LARGE_MIN_HISTORY = 10
NEW_LARGE_PERCENTILE = 90
ROUND_UNIT = Decimal(1000)
ROUND_MIN_HISTORY = 3
MAX_DOCUMENTS = 3000
MAX_OCR_ROWS = 1000

# Extracted field keys, in preference order: the frontend keys first, then names other extractors and seeds use.
COUNTERPARTY_KEYS = {'accounts_payable': ('supplier', 'vendor_name', 'supplier_name'),
                     'accounts_receivable': ('customer', 'buyer_name', 'customer_name')}
NUMBER_KEYS = ('invoice', 'bill_number', 'reference', 'receipt_number', 'document_number')
TOTAL_KEYS = ('total', 'total_amount', 'amount_due')
DATE_KEYS = ('date', 'invoice_date', 'document_date')
# The printed payee bank account (the evaluation field dictionary's name). Never `account_number`, which is the
# customer account on a utility bill.
PAYMENT_ACCOUNT_KEYS = ('payment_account_number', 'bank_account_number', 'payee_account_number')
FIELD_KEYS = sorted({*COUNTERPARTY_KEYS['accounts_payable'], *COUNTERPARTY_KEYS['accounts_receivable'],
                     *NUMBER_KEYS, *TOTAL_KEYS, *DATE_KEYS, *PAYMENT_ACCOUNT_KEYS})

ROOT = Path(__file__).resolve().parents[2]
RUN_ID = re.compile(r'^lite-[0-9a-f]{32}$')
ACKNOWLEDGED = 'FRAUD_SHIELD_ACKNOWLEDGED'


@dataclass
class Doc:
    """What the checks need to know about one document. Values come from the current saved record when there is
    one, otherwise from the current extraction (a person's correction first)."""
    id: str
    code: str
    kind: str
    category: str
    status: str
    uploaded_at: datetime
    file_hash: str | None = None
    counterparty: str | None = None
    number: str | None = None
    total: Decimal | None = None
    currency: str = 'MYR'
    doc_date: date | None = None
    record_id: str | None = None
    payment_account: str | None = None
    payment_account_source: str | None = None  # 'field', 'ocr', or 'none' once looked for and not found
    self_approved_by: str | None = None
    approved_at: datetime | None = None
    edited_after_approval: list[dict] = field(default_factory=list)
    visible: bool = True

    @property
    def approved(self) -> bool:
        return self.status in APPROVED_STATUSES or self.record_id is not None

    @property
    def party_key(self) -> str:
        return normalize_vendor_name(self.counterparty or '')

    @property
    def when(self) -> date:
        return self.doc_date or self.uploaded_at.date()


@dataclass
class Context:
    """Company-wide facts shared by every document's checks."""
    docs: list[Doc]
    vendor_created: dict[str, datetime] = field(default_factory=dict)  # normalised list name -> when it was added
    today: date | None = None
    account_reader: Callable[[Doc], str | None] | None = None

    def __post_init__(self):
        self.by_party: dict[tuple[str, str], list[Doc]] = {}
        for doc in self.docs:
            if doc.party_key:
                self.by_party.setdefault((doc.category, doc.party_key), []).append(doc)
        self._p90: dict[str, Decimal | None] = {}

    def peers(self, doc: Doc) -> list[Doc]:
        """Other documents from the same counterparty, any status."""
        return [other for other in self.by_party.get((doc.category, doc.party_key), []) if other.id != doc.id]

    def account(self, doc: Doc) -> str | None:
        """The printed payee account: an extracted field, else (once, lazily) the narrow OCR read."""
        if doc.payment_account is None and doc.payment_account_source is None:
            doc.payment_account_source = 'none'
            found = self.account_reader(doc) if self.account_reader else None
            if found:
                doc.payment_account, doc.payment_account_source = found, 'ocr'
        return doc.payment_account

    def p90(self, category: str) -> Decimal | None:
        if category not in self._p90:
            totals = sorted(d.total for d in self.docs if d.category == category and d.approved and d.total is not None)
            self._p90[category] = (percentile(totals, NEW_LARGE_PERCENTILE)
                                   if len(totals) >= NEW_LARGE_MIN_HISTORY else None)
        return self._p90[category]


# ---------------------------------------------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------------------------------------------

def money(amount: Decimal | None, currency: str = 'MYR') -> str:
    return '-' if amount is None else f'{currency} {amount:,.2f}'


def digits_only(value: str | None) -> str:
    return re.sub(r'\D', '', value or '')


def last4(account: str | None) -> str:
    digits = digits_only(account)
    return '…' + digits[-4:] if digits else '-'


def to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, Decimal):
        return value
    text = re.sub(r'[^\d.\-]', '', str(value).replace(',', ''))
    try:
        return Decimal(text) if text not in {'', '.', '-'} else None
    except InvalidOperation:
        return None


def to_date(value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


LOOKALIKES = str.maketrans({'O': '0', 'I': '1', 'L': '1'})


def number_shape(number: str | None) -> str:
    """A document number with punctuation, case, spacing, leading zeros and O/0 I/1 look-alikes ignored.
    `INV-1001`, `inv 1001` and `INV1OO1` share a shape; `INV-1002` does not."""
    text = re.sub(r'[^0-9A-Z]', '', (number or '').upper()).translate(LOOKALIKES)
    return re.sub(r'(?<!\d)0+(?=\d)', '', text)


def percentile(values: list[Decimal], pct: int) -> Decimal | None:
    """Nearest-rank percentile of a sorted list."""
    if not values:
        return None
    rank = max(1, -(-pct * len(values) // 100))
    return values[rank - 1]


def is_round(amount: Decimal | None) -> bool:
    return amount is not None and amount >= ROUND_UNIT and amount % ROUND_UNIT == 0


def flag(code: str, severity: str, title: str, message: str, evidence: list[dict], action: str) -> dict:
    return {'code': code, 'severity': severity, 'title': title, 'message': message, 'evidence': evidence,
            'action': action}


def ev(label: str, value: str, href: str | None = None) -> dict:
    return {'label': label, 'value': value, 'href': href}


def doc_href(doc: Doc) -> str:
    return f'documents/{doc.id}'


def plural(count: int, word: str) -> str:
    return f'{count} {word}{"" if count == 1 else "s"}'


# ---------------------------------------------------------------------------------------------------------------
# The narrow OCR read for the printed payee account, used only when no extracted field holds it
# ---------------------------------------------------------------------------------------------------------------

ACCOUNT_LABEL = re.compile(r'(?<![a-z])(?:a/c|acct?|account|akaun)(?![a-z])\.?\s*(?:no\.?|number|num|#)?\s*[:.]?', re.IGNORECASE)
BANK_WORD = re.compile(r'\bbank\b|maybank|\bcimb\b|\brhb\b|hong\s*leong|ambank|affin|alliance|ocbc|hsbc|\buob\b|'
                       r'standard\s*chartered|\bbsn\b|agrobank|muamalat|beneficiary|payee|pay\s*to|transfer\s*to|'
                       r'payment\s*details', re.IGNORECASE)
NOT_PAYEE = re.compile(r'customer|your\s+account|contract|pelanggan|akaun\s+anda|member|policy|meter', re.IGNORECASE)
ACCOUNT_DIGITS = re.compile(r'(?<!\d)(\d[\d -]{6,22}\d)(?!\d)')


def find_payment_account(rows: Iterable[str]) -> str | None:
    """The payee bank account printed after an "Account / A/C / Acct / Akaun" label, on the same row or the next one,
    with a bank word within two rows. Returns 8 to 17 digits, or None. Rows that name a customer, contract, member,
    policy or meter account are skipped, so a utility bill's own "Account No." is not taken for the payee's."""
    texts = [str(row or '') for row in list(rows)[:MAX_OCR_ROWS]]
    for index, text in enumerate(texts):
        label = ACCOUNT_LABEL.search(text)
        if not label or NOT_PAYEE.search(text):
            continue
        if not BANK_WORD.search(' '.join(texts[max(0, index - 2):index + 3])):
            continue
        following = texts[index + 1] if index + 1 < len(texts) else ''
        for candidate in (text[label.end():], '' if NOT_PAYEE.search(following) else following):
            match = ACCOUNT_DIGITS.search(candidate)
            digits = digits_only(match.group(1)) if match else ''
            if 8 <= len(digits) <= 17:
                return digits
    return None


def ocr_rows(service_run_id: str | None, root: Path = ROOT) -> list[str]:
    """Row texts from a lite extraction's private OCR file on this server, or [] when it is not here."""
    if not service_run_id or not RUN_ID.match(service_run_id):
        return []
    base = (root / 'runs').resolve()
    path = (base / service_run_id / 'ocr.private.json').resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    rows = payload.get('rows') if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [str(row.get('text') or '') for row in rows[:MAX_OCR_ROWS] if isinstance(row, dict)]


# ---------------------------------------------------------------------------------------------------------------
# Detectors (pure)
# ---------------------------------------------------------------------------------------------------------------

def detect_bank_change(doc: Doc, ctx: Context) -> list[dict]:
    if doc.category != 'accounts_payable' or not doc.party_key:
        return []
    printed = digits_only(ctx.account(doc))
    if not printed:
        return []
    history = [peer for peer in ctx.peers(doc) if peer.approved and digits_only(ctx.account(peer))]
    accounts = Counter(digits_only(peer.payment_account) for peer in history)
    if not accounts or printed in accounts:
        return []
    usual, count = accounts.most_common(1)[0]
    latest = max((peer for peer in history if digits_only(peer.payment_account) == usual), key=lambda p: p.when)
    supplier = doc.counterparty or 'this supplier'
    source = 'Document text next to the account label' if doc.payment_account_source == 'ocr' else 'Extracted field'
    return [flag('FS-BANK-CHANGE', 'high', 'Bank account changed',
                 f"The bank account printed on this document ({last4(printed)}) differs from the one on {supplier}'s "
                 f"last {plural(count, 'approved document')} ({last4(usual)}).",
                 [ev('Printed on this document', last4(printed)),
                  ev('On approved documents', f'{last4(usual)} ({plural(count, "document")}, '
                                             f'latest {latest.when.isoformat()})', doc_href(latest)),
                  ev('Read from', source)],
                 'Call the supplier on a phone number you already have (not one printed on this document) and '
                 'confirm the new account before paying.')]


def detect_near_duplicate(doc: Doc, ctx: Context) -> list[dict]:
    shape = number_shape(doc.number)
    best: tuple[int, Doc, str] | None = None
    for peer in ctx.peers(doc):
        if peer.kind != doc.kind:
            continue
        same_total = doc.total is not None and peer.total == doc.total
        peer_shape = number_shape(peer.number)
        gap = abs((peer.when - doc.when).days)
        if doc.file_hash and peer.file_hash == doc.file_hash:
            rank, reason = 0, 'the same file'
        elif shape and peer_shape == shape and (doc.number or '') != (peer.number or ''):
            rank = 1 if same_total else 2
            reason = f'number {peer.number}, the same as {doc.number} apart from punctuation or look-alike characters'
        elif same_total and gap <= NEAR_DUP_DAYS and shape != peer_shape:
            rank = 3
            reason = (f'the same total ({money(doc.total, doc.currency)}) {plural(gap, "day")} apart with a different '
                      f'number ({peer.number or "none printed"})')
        else:
            continue
        if best is None or rank < best[0]:
            best = (rank, peer, reason)
    if best is None:
        return []
    rank, peer, reason = best
    supplier = doc.counterparty or 'the same supplier'
    return [flag('FS-NEAR-DUP', 'high' if rank <= 1 else 'medium', 'Possible duplicate',
                 f'{peer.code} from {supplier} has {reason}.',
                 [ev('This document', f'{doc.number or "no number"} · {money(doc.total, doc.currency)} · '
                                      f'{doc.when.isoformat()}'),
                  ev(peer.code, f'{peer.number or "no number"} · {money(peer.total, peer.currency)} · '
                                f'{peer.when.isoformat()}', doc_href(peer)),
                  ev('Status of the other document', peer.status.replace('_', ' '))],
                 f'Open {peer.code} and compare the two. If both are real, approve with a reason; if not, set this '
                 'one aside.')]


def detect_new_large(doc: Doc, ctx: Context) -> list[dict]:
    if doc.total is None or not doc.party_key:
        return []
    threshold = ctx.p90(doc.category)
    if threshold is None or doc.total < threshold:
        return []
    listed = ctx.vendor_created.get(doc.party_key)
    if listed is not None and listed < doc.uploaded_at:
        return []  # On the supplier list before this document arrived: not a first-time supplier.
    if any((peer.when, peer.uploaded_at) < (doc.when, doc.uploaded_at) for peer in ctx.peers(doc)):
        return []
    today = ctx.today or date.today()
    if (today - doc.uploaded_at.date()).days > NEW_SUPPLIER_DAYS:
        return []
    party = doc.counterparty or 'This supplier'
    return [flag('FS-NEW-LARGE', 'medium', 'New supplier, large amount',
                 f'{party} has no earlier documents and was not on your list, and this total '
                 f'({money(doc.total, doc.currency)}) is in the top 10% of your approved totals '
                 f'(90th percentile {money(threshold, doc.currency)}).',
                 [ev('This total', money(doc.total, doc.currency)),
                  ev('90th percentile of approved totals', money(threshold, doc.currency)),
                  ev('Earlier documents from this party', '0')],
                 'Check that the supplier is real (registration number, address, who ordered it) before approving.')]


def detect_round(doc: Doc, ctx: Context) -> list[dict]:
    if not is_round(doc.total):
        return []
    history = [peer for peer in ctx.peers(doc) if peer.approved and peer.total is not None]
    if len(history) < ROUND_MIN_HISTORY or any(is_round(peer.total) for peer in history):
        return []
    party = doc.counterparty or 'this supplier'
    return [flag('FS-ROUND', 'low', 'Round amount',
                 f'The total {money(doc.total, doc.currency)} is a round amount, and none of the '
                 f'{plural(len(history), "approved document")} from {party} were.',
                 [ev('This total', money(doc.total, doc.currency)),
                  ev('Approved documents from this party', str(len(history))),
                  ev('Of those, with a round total', '0')],
                 'Ask for the itemised invoice or quotation behind this amount.')]


def detect_sod(doc: Doc, _ctx: Context) -> list[dict]:
    found = []
    if doc.self_approved_by:
        found.append(flag('FS-SOD', 'high', 'Same person submitted and approved',
                          f'{doc.self_approved_by} both submitted and approved this document.',
                          [ev('Submitted and approved by', doc.self_approved_by)],
                          'Ask a second local admin to review it again.'))
    if doc.edited_after_approval and doc.approved_at:
        edits = doc.edited_after_approval
        found.append(flag('FS-SOD', 'high', 'Changed after approval',
                          f'{plural(len(edits), "value")} changed after the approval on '
                          f'{doc.approved_at.date().isoformat()}.',
                          [ev('Approved on', doc.approved_at.date().isoformat()),
                           *[ev(f'Changed: {edit["field"]}', f'{edit["at"]} by {edit["by"]}') for edit in edits[:5]]],
                          f'Open the change history and confirm the {edits[0]["field"]} change with the person who '
                          'made it.'))
    return found


DETECTORS = (detect_bank_change, detect_near_duplicate, detect_new_large, detect_round, detect_sod)


def check(doc: Doc, ctx: Context) -> list[dict]:
    flags = [found for detector in DETECTORS for found in detector(doc, ctx)]
    return sorted(flags, key=lambda f: (SEVERITY_ORDER[f['severity']], f['code']))


# ---------------------------------------------------------------------------------------------------------------
# Loading (read-only SQL)
# ---------------------------------------------------------------------------------------------------------------

def load_documents(db, user, *, root: Path = ROOT) -> Context:
    """The user's company's documents that are not set aside (newest first, capped), each marked visible or not.
    Every document takes part in comparisons; only visible ones are reported to an accountant."""
    tenant = user['tenant_id']
    visible_sql, visible_params = 'TRUE', []
    if user['role'] == 'accountant':
        visible_sql, visible_params = '(d.owner_id=%s OR d.assigned_to=%s)', [user['id'], user['id']]
    rows = db.execute(f"""SELECT d.id, d.document_code, d.kind, d.category, d.status, d.uploaded_at,
            d.file_hash_sha256, ({visible_sql}) AS visible,
            r.id AS record_id, r.supplier_name, r.buyer_name,
            COALESCE(r.invoice_number, r.reference_number) AS record_number, r.invoice_date, r.total_amount,
            r.currency, x.run_id
        FROM documents d
        LEFT JOIN LATERAL (SELECT s.* FROM standardised_records s WHERE s.document_id=d.id AND s.tenant_id=d.tenant_id
            AND s.is_current ORDER BY s.revision_number DESC LIMIT 1) r ON TRUE
        LEFT JOIN LATERAL (SELECT COALESCE(e.service_run_id, e.raw_json->'provenance'->>'service_run_id') AS run_id
            FROM raw_extractions e WHERE e.document_id=d.id AND e.is_current LIMIT 1) x ON TRUE
        WHERE d.tenant_id=%s AND d.excluded_at IS NULL
        ORDER BY d.uploaded_at DESC, d.id LIMIT %s""", [*visible_params, tenant, MAX_DOCUMENTS]).fetchall()
    ids = [row['id'] for row in rows]
    fields: dict[str, dict[str, str]] = {}
    for item in db.execute("""SELECT p.document_id, p.field_key, COALESCE(p.current_value, p.normalized_value) AS value
            FROM field_predictions p JOIN raw_extractions e ON e.id=p.extraction_id AND e.is_current
            WHERE p.document_id=ANY(%s) AND p.field_key=ANY(%s)""", (ids, FIELD_KEYS)).fetchall():
        if item['value'] not in (None, ''):
            fields.setdefault(str(item['document_id']), {})[item['field_key']] = item['value']
    docs, run_ids = [], {}
    for row in rows:
        values = fields.get(str(row['id']), {})
        category = row['category']

        def first(keys, values=values):
            return next((values[key] for key in keys if values.get(key)), None)
        account = digits_only(first(PAYMENT_ACCOUNT_KEYS)) or None
        record_party = row['supplier_name'] if category == 'accounts_payable' else row['buyer_name']
        doc = Doc(id=str(row['id']), code=row['document_code'], kind=row['kind'], category=category,
                  status=row['status'], uploaded_at=row['uploaded_at'], file_hash=row['file_hash_sha256'],
                  counterparty=record_party or first(COUNTERPARTY_KEYS[category]),
                  number=row['record_number'] or first(NUMBER_KEYS),
                  total=row['total_amount'] if row['total_amount'] is not None else to_decimal(first(TOTAL_KEYS)),
                  currency=(row['currency'] or 'MYR').strip(),
                  doc_date=row['invoice_date'] or to_date(first(DATE_KEYS)),
                  record_id=str(row['record_id']) if row['record_id'] else None,
                  payment_account=account, payment_account_source='field' if account else None,
                  visible=bool(row['visible']))
        run_ids[doc.id] = row['run_id']
        docs.append(doc)
    _load_reviews(db, tenant, {doc.id: doc for doc in docs})
    vendors: dict[str, datetime] = {}
    for vendor in db.execute('SELECT vendor_name, created_at FROM vendors WHERE tenant_id=%s', (tenant,)).fetchall():
        key = normalize_vendor_name(vendor['vendor_name'])
        if key and (key not in vendors or vendor['created_at'] < vendors[key]):
            vendors[key] = vendor['created_at']
    today = db.execute("""SELECT (now() AT TIME ZONE COALESCE((SELECT timezone FROM workspace_settings
        WHERE tenant_id=%s LIMIT 1), 'Asia/Kuala_Lumpur'))::date AS today""", (tenant,)).fetchone()['today']
    return Context(docs=docs, vendor_created=vendors, today=today,
                   account_reader=lambda doc: find_payment_account(ocr_rows(run_ids.get(doc.id), root)))


def _load_reviews(db, tenant, by_id: dict[str, Doc]) -> None:
    for row in db.execute("""SELECT r.document_id, r.reviewed_at, r.reviewed_by, s.submitted_by, u.name AS reviewer
            FROM review_decisions r JOIN review_submissions s ON s.id=r.submission_id AND s.tenant_id=r.tenant_id
            LEFT JOIN users u ON u.id=r.reviewed_by
            WHERE r.tenant_id=%s AND r.decision='approved' ORDER BY r.reviewed_at""", (tenant,)).fetchall():
        doc = by_id.get(str(row['document_id']))
        if doc is None:
            continue
        doc.approved_at = row['reviewed_at']  # Ordered, so the latest approval wins.
        if row['submitted_by'] == row['reviewed_by']:
            doc.self_approved_by = row['reviewer'] or 'One user'
    approved = [doc.id for doc in by_id.values() if doc.approved_at]
    if not approved:
        return
    for row in db.execute("""SELECT p.document_id, p.field_label, c.corrected_at, u.name AS editor
            FROM field_corrections c JOIN field_predictions p ON p.id=c.field_prediction_id
            JOIN raw_extractions e ON e.id=p.extraction_id AND e.is_current
            JOIN documents d ON d.id=p.document_id AND d.tenant_id=%s
            LEFT JOIN users u ON u.id=c.corrected_by
            WHERE p.document_id=ANY(%s::uuid[]) ORDER BY c.corrected_at""", (tenant, approved)).fetchall():
        doc = by_id[str(row['document_id'])]
        if doc.approved_at and row['corrected_at'] > doc.approved_at:
            doc.edited_after_approval.append({'field': row['field_label'],
                                              'at': row['corrected_at'].date().isoformat(),
                                              'by': row['editor'] or 'a user'})


# ---------------------------------------------------------------------------------------------------------------
# Results for the routes
# ---------------------------------------------------------------------------------------------------------------

def acknowledgements(db, tenant, document_id: str) -> list[dict]:
    result = []
    for row in db.execute("""SELECT user_name, role, details, created_at FROM audit_logs
            WHERE tenant_id=%s AND action=%s AND resource=%s ORDER BY created_at""",
                          (tenant, ACKNOWLEDGED, document_id)).fetchall():
        try:
            details = json.loads(row['details'] or '{}')
        except ValueError:
            details = {}
        result.append({'codes': [str(code) for code in details.get('codes') or []],
                       'reason': str(details.get('reason') or ''),
                       'by': f"{row['user_name']} ({row['role'].replace('_', ' ')})",
                       'at': row['created_at'].isoformat()})
    return result


def document_flags(db, user, document_id: str, ctx: Context | None = None) -> dict | None:
    """One visible document's flags, or None when the user cannot see it."""
    ctx = ctx or load_documents(db, user)
    doc = next((d for d in ctx.docs if d.id == document_id and d.visible), None)
    if doc is None:
        return None
    acks = acknowledgements(db, user['tenant_id'], document_id)
    return {'documentId': doc.id, 'documentCode': doc.code, 'checkedAt': datetime.now().astimezone().isoformat(),
            'flags': check(doc, ctx),
            'acknowledgedCodes': sorted({code for ack in acks for code in ack['codes']}),
            'acknowledgements': acks}


def visible_flags(ctx: Context) -> list[tuple[Doc, dict]]:
    return [(doc, found) for doc in ctx.docs if doc.visible for found in check(doc, ctx)]


def summary(db, user, ctx: Context | None = None) -> dict:
    ctx = ctx or load_documents(db, user)
    found = visible_flags(ctx)
    by_code = {code: 0 for code in CODES}
    by_severity = {'high': 0, 'medium': 0, 'low': 0}
    for _doc, item in found:
        by_code[item['code']] += 1
        by_severity[item['severity']] += 1
    ordered = sorted(found, key=lambda pair: (SEVERITY_ORDER[pair[1]['severity']], -pair[0].uploaded_at.timestamp()))
    return {'checkedDocuments': sum(1 for doc in ctx.docs if doc.visible),
            'flaggedDocuments': len({doc.id for doc, _ in found}), 'flags': len(found),
            'byCode': by_code, 'bySeverity': by_severity,
            'top': [{'documentId': doc.id, 'documentCode': doc.code, 'code': item['code'],
                     'severity': item['severity'], 'title': item['title'], 'href': doc_href(doc)}
                    for doc, item in ordered[:10]]}
