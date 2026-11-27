"""Seeded demo cases for Fraud Shield and the AI Inbox, added to the KNT demo company.

Run it with `scripts/seed_ai_demo_cases.py`: a dry run by default, `--apply` to write. Approvals, contacts and the bank
statement go through the real API in-process, as the KNT accountant and local admin, so every existing rule and audit
row applies. Only the scanned documents are inserted directly, the same way the KNT seed captures its purchases.

The cases (all fictional; file names start with AIDEMO-, which is how a second run is refused):
1. Bank change: Seladah Utilities has 3 approved bills paid to an account ending 2093; a new bill prints one ending 4417.
2. Near-duplicate: Hup Seng Stationery's approved HS-1001, and a new "HS1001" for the same total 3 days later.
3. Supplier not on the list: 2 bills from Pakar Aircond Services, which is not a supplier yet (an EX-008 group).
4. Unmatched bank lines: a confirmed statement with payments of 2 approved bills and a deposit that matches nothing.

`reset-knt` removes all of it with the rest of the KNT books.
"""
from __future__ import annotations

import hashlib
import io
import os
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import uuid4

from ai.synthetic.knt_pack import COMPANY

MARKER = 'AIDEMO-'
OLD_ACCOUNT = ('CIMB Bank Berhad', '5140 9900 2093')
NEW_ACCOUNT = ('CIMB Bank Berhad', '8604 1233 4417')


@dataclass(frozen=True)
class Bill:
    key: str
    supplier: str
    number: str
    issued: date
    total: Decimal
    description: str
    approve: bool = False
    account: tuple[str, str] | None = None


SELADAH = 'Seladah Utilities Sdn Bhd'
HUP_SENG = 'Hup Seng Stationery Sdn Bhd'
PAKAR = 'Pakar Aircond Services'
BILLS = (
    Bill('sel-aug', SELADAH, 'BL-0071', date(2026, 8, 18), Decimal('455.80'), 'Water and sewerage, July', True, OLD_ACCOUNT),
    Bill('sel-sep', SELADAH, 'BL-0079', date(2026, 9, 18), Decimal('471.10'), 'Water and sewerage, August', True, OLD_ACCOUNT),
    Bill('sel-oct', SELADAH, 'BL-0090', date(2026, 10, 18), Decimal('462.35'), 'Water and sewerage, September', True, OLD_ACCOUNT),
    Bill('sel-nov', SELADAH, 'BL-0098', date(2026, 11, 18), Decimal('462.00'), 'Water and sewerage, October', False, NEW_ACCOUNT),
    Bill('hs-1', HUP_SENG, 'HS-1001', date(2026, 11, 9), Decimal('1284.60'), 'Printer paper and toner', True),
    Bill('hs-2', HUP_SENG, 'HS1001', date(2026, 11, 12), Decimal('1284.60'), 'Printer paper and toner', False),
    Bill('pk-1', PAKAR, 'PAS-2611-07', date(2026, 11, 6), Decimal('380.00'), 'Aircond servicing, office', False),
    Bill('pk-2', PAKAR, 'PAS-2611-15', date(2026, 11, 14), Decimal('245.50'), 'Aircond gas top-up', False),
)
LISTED = (SELADAH, HUP_SENG)  # Pakar stays off the supplier list on purpose.
BANK_ROWS = (
    {'postedDate': '2026-11-20', 'description': 'IBG SELADAH UTILITIES SDN BHD BL-0090', 'reference': 'BL-0090',
     'debit': '462.35'},
    {'postedDate': '2026-11-21', 'description': 'DUITNOW HUP SENG STATIONERY', 'reference': 'DN26112101',
     'debit': '1284.60'},
    {'postedDate': '2026-11-24', 'description': 'CASH DEPOSIT COUNTER', 'reference': 'CD-2411', 'credit': '250.00'},
)


class DemoError(RuntimeError):
    pass


def plan() -> list[str]:
    lines = [f'Company: {COMPANY["name"]}', f'Suppliers added to the list if missing: {", ".join(LISTED)}']
    for bill in BILLS:
        state = 'approved and saved as a record' if bill.approve else 'left to review'
        account = f', pays into account ending {bill.account[1][-4:]}' if bill.account else ''
        lines.append(f'  {bill.supplier} {bill.number} {bill.issued} MYR {bill.total} ({state}{account})')
    for row in BANK_ROWS:
        amount = row.get('debit') or row.get('credit')
        lines.append(f'  Bank line {row["postedDate"]} {row["description"]} MYR {amount}')
    return lines


def render_bill(bill: Bill) -> bytes:
    """A plain fictional supplier bill as a JPEG, so the review page has something to show."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new('RGB', (1000, 1300), 'white')
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=28)
        small = ImageFont.load_default(size=22)
    except TypeError:  # Pillow without sized default fonts
        font = small = ImageFont.load_default()
    rows = [(bill.supplier, font), ('(Fictional supplier for the AccordAI demo)', small), ('', small),
            (f'TAX INVOICE / BILL  {bill.number}', font), (f'Date: {bill.issued.isoformat()}', small),
            (f'Bill to: {COMPANY["name"]}', small), ('', small), (bill.description, small),
            (f'Amount: MYR {bill.total:,.2f}', small), (f'TOTAL MYR {bill.total:,.2f}', font), ('', small)]
    if bill.account:
        rows += [('Payment details', small), (f'Bank: {bill.account[0]}', small), (f'Account No: {bill.account[1]}', small)]
    y = 80
    for text, face in rows:
        draw.text((80, y), text, fill='black', font=face)
        y += 52
    buffer = io.BytesIO()
    image.save(buffer, 'JPEG', quality=85)
    return buffer.getvalue()


def _store(tenant, owner, document_id, filename, data: bytes) -> str:
    from database.object_storage import object_store

    key = object_store.document_key(tenant, owner['id'], owner['name'], document_id, filename)
    if object_store.enabled():
        return object_store.put_bytes(key, data, 'image/jpeg')
    root = object_store.local_storage_root()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        raise DemoError('The demo file path is outside local storage.')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def capture(connect, tenant, owner, bill: Bill) -> str:
    """Insert one scanned bill with its read values, then run the normal validation (as the KNT seed does)."""
    from psycopg.types.json import Jsonb

    from validation.documents.document_validation import validate_document

    document_id = uuid4()
    filename = f'{MARKER}{bill.key}-{bill.number}.jpg'
    data = render_bill(bill)
    storage = _store(tenant, owner, document_id, filename, data)
    values = {'supplier': bill.supplier, 'customer': COMPANY['name'], 'date': bill.issued.isoformat(),
              'currency': 'MYR', 'subtotal': f'{bill.total:.2f}', 'tax': '0.00', 'total': f'{bill.total:.2f}',
              'invoice': bill.number}
    if bill.account:
        values.update(payment_bank_name=bill.account[0], payment_account_number=bill.account[1])
    with connect() as db:
        db.execute("""INSERT INTO documents(id,tenant_id,owner_id,original_filename,kind,category,storage_path,
                mime_type,file_size_bytes,file_hash_sha256,status,page_count)
            VALUES (%s,%s,%s,%s,'invoice','accounts_payable',%s,'image/jpeg',%s,%s,'extracted',1)""",
                   (document_id, tenant, owner['id'], filename, storage, len(data), hashlib.sha256(data).hexdigest()))
        extraction = db.execute("""INSERT INTO raw_extractions(document_id,run_number,is_current,routing_decision,raw_json)
            VALUES (%s,1,true,'human_review',%s) RETURNING id""",
                                (document_id, Jsonb({'mode': 'ai_demo_fixture', 'source': filename}))).fetchone()['id']
        for key, value in values.items():
            db.execute("""INSERT INTO field_predictions(extraction_id,document_id,field_key,field_label,
                    extraction_state,raw_value,normalized_value,current_value)
                VALUES (%s,%s,%s,%s,'resolved',%s,%s,%s)""",
                       (extraction, document_id, key, key.replace('_', ' ').title(), value, value, value))
        db.execute("""INSERT INTO line_items(extraction_id,document_id,line_number,description,quantity,unit_price,amount)
            VALUES (%s,%s,1,%s,1,%s,%s)""", (extraction, document_id, bill.description, bill.total, bill.total))
        validate_document(db, str(document_id), owner)
    return str(document_id)


def _company(db):
    from services.admin.knt_seed import EMAILS, find_company

    tenant = find_company(db)
    if not tenant:
        raise DemoError('The KNT demo company is not in this database. Run seed-knt first.')
    users = {}
    for role, email in EMAILS.items():
        user = db.execute("SELECT * FROM users WHERE lower(email)=%s AND tenant_id=%s AND status='active'",
                          (email, tenant['id'])).fetchone()
        if not user:
            raise DemoError(f'The KNT {role.replace("_", " ")} account is missing.')
        users[role] = user
    if db.execute('SELECT 1 FROM documents WHERE tenant_id=%s AND original_filename LIKE %s LIMIT 1',
                  (tenant['id'], MARKER + '%')).fetchone():
        raise DemoError('The AI demo cases are already in this company. Run reset-knt to start again.')
    return tenant, users


def seed(connect, *, apply: bool = False, out=print) -> dict:
    """Print the plan; with apply, create the cases. Returns the created ids by bill key."""
    for line in plan():
        out(line)
    with connect() as db:
        tenant, users = _company(db)
    if not apply:
        out('Dry run: nothing written. Pass --apply to create these.')
        return {}
    from fastapi.testclient import TestClient

    import main
    from services.admin.demo_company import Api, issue_tokens

    tokens, sessions = issue_tokens(users)
    ids: dict[str, str] = {}
    try:
        with closing(TestClient(main.app, base_url='http://127.0.0.1')) as client:
            api = Api(client, tokens)
            for name in LISTED:
                api.call('local_admin', 'POST', '/api/erp/contacts', expected=(201, 409),
                         json={'kind': 'supplier', 'legalName': name})
            for bill in BILLS:
                ids[bill.key] = capture(connect, tenant['id'], users['accountant'], bill)
                if not bill.approve:
                    continue
                path = f'/api/documents/{ids[bill.key]}/review'
                review = {'expectedContentRevision': 1, 'note': 'AI demo case: checked against the fictional source'}
                api.call('accountant', 'POST', path, json=review)
                api.call('local_admin', 'POST', path + '/decision', json={**review, 'decision': 'approved'})
                api.call('local_admin', 'POST', f'/api/records/from-document/{ids[bill.key]}', json={})
            rows = [{**row, 'currency': 'MYR'} for row in BANK_ROWS]
            api.call('local_admin', 'POST', '/api/bank/imports', json={
                'sourceBankAccountRef': COMPANY['bank_account_number'], 'mappingVersion': 'ai-demo-v1', 'rows': rows})
    finally:
        with connect() as db:
            db.execute('DELETE FROM web_sessions WHERE session_id=ANY(%s)', (sessions,))
    out(f'Created {len(ids)} documents and {len(BANK_ROWS)} bank lines in {COMPANY["name"]}.')
    return ids


def require_target(allow_remote: bool, environ=None):
    """The database in DATABASE_URL; a remote one (UAT) only with --allow-remote and AI_DEMO_REMOTE_HOST set to its
    host name, so a stray URL cannot be written to by accident. Nothing is printed about the URL."""
    from services.admin.knt_extend import require_target as knt_require_target
    env = dict(os.environ if environ is None else environ)
    env['KNT_EXTEND_REMOTE_HOST'] = env.get('AI_DEMO_REMOTE_HOST', '')
    try:
        return knt_require_target(allow_remote, env)
    except Exception as error:  # noqa: BLE001 - the KNT SeedError, re-worded for this script
        raise DemoError(str(error).replace('KNT_EXTEND_REMOTE_HOST', 'AI_DEMO_REMOTE_HOST')
                        .replace('to extend it', 'to seed it')) from None
