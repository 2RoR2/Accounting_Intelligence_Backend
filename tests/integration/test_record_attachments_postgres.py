"""Plan P1: proof attached to typed records, on the real schema (skipped without TEST_DATABASE_URL).

A disposable database next to TEST_DATABASE_URL (loopback only) gets the consolidated schema; requests go through
the real routes with only the session lookup replaced and the document storage pointed at a temporary folder.
Checks size/type/link validation, the safe serving headers, tenant isolation (another company gets 404),
soft delete and the audit rows.
"""
import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation
from psycopg.rows import dict_row
from starlette.datastructures import UploadFile as StarletteUploadFile

import main as api
from database.schema_compatibility import apply_migrations
from services.routes.erp import attachments

pytestmark = pytest.mark.runtime_defaults
ROOT = Path(__file__).resolve().parents[2]
HEADERS = {'X-Requested-With': 'AccountingIntelligence', 'Origin': 'http://localhost:3000'}
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 64
PDF = b'%PDF-1.7\n' + b'\x00' * 64
WEBP = b'RIFF\x24\x00\x00\x00WEBPVP8 ' + b'\x00' * 64
ATTACHABLE = ('payment', 'purchase-payment', 'refund', 'purchase-refund', 'money-in', 'money-out', 'bank-transfer', 'journal-entry')


class Session:
    user = None


@pytest.fixture(scope='module')
def database(tmp_path_factory):
    url = os.getenv('TEST_DATABASE_URL')
    if not url:
        pytest.skip('Requires the disposable local TEST_DATABASE_URL')
    parts = urlsplit(url)
    assert parts.hostname in {'127.0.0.1', 'localhost', '::1'}
    name = 'record_attachments_' + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE {name}')
    target = parts._replace(path='/' + name).geturl()
    try:
        with psycopg.connect(target, row_factory=dict_row) as db:
            apply_migrations(db, ROOT / 'database/PostgreSQL Database schema.sql', tmp_path_factory.mktemp('no-migrations'))
        yield target
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS {name} WITH (FORCE)')


@pytest.fixture
def company(database, tmp_path, monkeypatch):
    """Two companies, a user each, and one record of every attachable type (plus an invoice) in the first."""
    monkeypatch.setenv('DOCUMENT_STORAGE_PATH', str(tmp_path / 'storage'))
    monkeypatch.delenv('S3_BUCKET', raising=False)

    @contextmanager
    def connect():
        with psycopg.connect(database, row_factory=dict_row) as db:
            yield db

    with connect() as db:
        def tenant(label):
            key = uuid4().hex
            return db.execute("INSERT INTO tenants(name,registration_number,admin_email) VALUES (%s,%s,'a@x.test') RETURNING id",
                              (label + ' ' + key[:8], key[:12])).fetchone()['id']

        def person(tenant_id, role):
            return dict(db.execute("""INSERT INTO users(tenant_id,name,email,password_hash,role,status)
                VALUES (%s,%s,%s,'x',%s,'active') RETURNING id,tenant_id,name,role""",
                                   (tenant_id, role.title(), uuid4().hex + '@x.test', role)).fetchone())

        own, other = tenant('Own'), tenant('Other')
        ids = {'tenant': own, 'other_tenant': other, 'accountant': person(own, 'accountant'), 'admin': person(own, 'local_admin'),
               'outsider': person(other, 'local_admin')}
        ids['records'] = {kind: str(db.execute(
            "INSERT INTO erp_document_headers(tenant_id,document_type,document_number) VALUES (%s,%s,%s) RETURNING id",
            (own, kind, kind.upper() + '-1')).fetchone()['id']) for kind in (*ATTACHABLE, 'invoice')}
        ids['foreign'] = str(db.execute(
            "INSERT INTO erp_document_headers(tenant_id,document_type,document_number) VALUES (%s,'payment','PM-OTHER') RETURNING id",
            (other,)).fetchone()['id'])

    patches = [patch.object(module, 'connect', side_effect=connect) for module in (api, attachments)]
    patches.append(patch.object(api, 'session_user', side_effect=lambda _db, _request: dict(Session.user)))
    for item in patches:
        item.start()
    try:
        yield connect, ids
    finally:
        for item in reversed(patches):
            item.stop()


def call(method, path, user, **kwargs):
    Session.user = user
    return TestClient(api.app).request(method, path, headers=HEADERS, **kwargs)


def upload(user, kind, record_id, data, filename='slip.png', content_type='image/png'):
    return call('POST', f'/api/erp/documents/{kind}/{record_id}/attachments', user, files={'file': (filename, data, content_type)})


def link(user, kind, record_id, url, name=None):
    return call('POST', f'/api/erp/documents/{kind}/{record_id}/attachments/link', user, json={'url': url, 'name': name})


def listing(user, kind, record_id):
    response = call('GET', f'/api/erp/documents/{kind}/{record_id}/attachments', user)
    assert response.status_code == 200, response.text
    return response.json()['items']


def audit_rows(connect, ids, action):
    with connect() as db:
        return db.execute("SELECT user_id,resource,details FROM audit_logs WHERE tenant_id=%s AND action=%s ORDER BY created_at",
                          (ids['tenant'], action)).fetchall()


def test_every_proof_record_type_accepts_a_file_and_a_link(company):
    _, ids = company
    accountant = ids['accountant']
    for kind in ATTACHABLE:
        record = ids['records'][kind]
        made = upload(accountant, kind, record, PNG, 'slip.png')
        assert made.status_code == 201, made.text
        assert link(accountant, kind, record, 'https://bank.example.com/slip/1').status_code == 201
        items = listing(accountant, kind, record)
        assert [i['kind'] for i in items] == ['file', 'link']
        assert items[0]['mediaType'] == 'image/png' and items[0]['sizeBytes'] == len(PNG) and items[0]['uploadedBy'] == 'Accountant'
        assert items[0]['fileUrl'] == f"/api/erp/attachments/{items[0]['id']}/file" and items[0]['url'] is None
        assert items[1]['url'] == 'https://bank.example.com/slip/1' and items[1]['name'] == 'bank.example.com' and items[1]['fileUrl'] is None
    # Invoices, bills and the like keep their own source file: not attachable here.
    invoice = ids['records']['invoice']
    assert upload(accountant, 'invoice', invoice, PNG).status_code == 404
    assert link(accountant, 'invoice', invoice, 'https://example.com/a').status_code == 404
    assert call('GET', f'/api/erp/documents/invoice/{invoice}/attachments', accountant).status_code == 404
    # The id must match the type in the address.
    assert upload(accountant, 'money-out', ids['records']['money-in'], PNG).status_code == 404


def test_type_is_decided_from_the_bytes_not_the_name_or_header(company):
    _, ids = company
    accountant, record = ids['accountant'], ids['records']['payment']
    # A real PNG under a misleading name and content type is stored as a PNG; a real PDF/JPEG/WebP likewise.
    for data in (PNG, JPEG, PDF, WEBP):
        response = upload(accountant, 'payment', record, data, 'notes.txt', 'text/plain')
        assert response.status_code == 201, response.text
    assert [i['mediaType'] for i in listing(accountant, 'payment', record)] == ['image/png', 'image/jpeg', 'application/pdf', 'image/webp']
    other = ids['records']['refund']
    for bad, name, declared in ((b'<html><script>alert(1)</script></html>', 'slip.jpg', 'image/jpeg'),
                                (b'GIF89a' + b'\x00' * 32, 'slip.gif', 'image/gif'),
                                (b'MZ\x90\x00' + b'\x00' * 32, 'slip.pdf', 'application/pdf'),
                                (b'II*\x00' + b'\x00' * 32, 'scan.tif', 'image/tiff'),
                                (b'plain text receipt', 'slip.png', 'image/png')):
        response = upload(accountant, 'refund', other, bad, name, declared)
        assert response.status_code == 422, (name, response.text)
    assert upload(accountant, 'refund', other, b'', 'empty.png').status_code == 422
    assert listing(accountant, 'refund', other) == []


def test_size_limit_reads_at_most_one_byte_past_it(company, monkeypatch):
    _, ids = company
    accountant, record = ids['accountant'], ids['records']['money-in']
    assert attachments.MAX_ATTACHMENT_BYTES == 10 * 1024 * 1024
    exact = PNG + b'\x00' * (attachments.MAX_ATTACHMENT_BYTES - len(PNG))
    assert upload(accountant, 'money-in', record, exact).status_code == 201
    over = exact + b'\x00'
    response = upload(accountant, 'money-in', record, over)
    assert response.status_code == 413 and '10 MB' in response.json()['detail']
    assert len(listing(accountant, 'money-in', record)) == 1

    # The route asks the upload for no more than limit + 1 bytes.
    asked = []
    original = StarletteUploadFile.read

    async def spy(self, size=-1):
        asked.append(size)
        return await original(self, size)

    monkeypatch.setattr(StarletteUploadFile, "read", spy)
    upload(accountant, 'money-in', record, PNG)
    assert asked == [attachments.MAX_ATTACHMENT_BYTES + 1]


def test_links_must_be_plain_https(company):
    connect, ids = company
    accountant, record = ids['accountant'], ids['records']['bank-transfer']
    for bad in ('http://example.com/a', 'javascript:alert(1)', 'data:text/html,x', 'ftp://example.com/a', 'example.com/a',
                'https://user:pw@example.com/a', 'https://example.com/a b', 'https://', ''):
        response = link(accountant, 'bank-transfer', record, bad)
        assert response.status_code == 422, (bad, response.text)
    made = link(accountant, 'bank-transfer', record, 'https://drive.example.com/file/abc?x=1#top', 'Maybank slip 12 Aug')
    assert made.status_code == 201
    assert [(i['name'], i['url']) for i in listing(accountant, 'bank-transfer', record)] == [
        ('Maybank slip 12 Aug', 'https://drive.example.com/file/abc?x=1#top')]
    # The database refuses what the API refuses.
    with connect() as db:
        for url in ('http://example.com/a', 'javascript:alert(1)'):
            with pytest.raises(CheckViolation):
                with db.transaction():
                    db.execute("""INSERT INTO record_attachments(tenant_id,target_type,target_id,kind,url,original_name)
                        VALUES (%s,'erp_document',%s,'link',%s,'x')""", (ids['tenant'], record, url))
        for media in ('text/html', 'image/svg+xml', None):
            with pytest.raises(CheckViolation):
                with db.transaction():
                    db.execute("""INSERT INTO record_attachments(tenant_id,target_type,target_id,kind,storage_path,original_name,media_type,size_bytes)
                        VALUES (%s,'erp_document',%s,'file','/x','x',%s,10)""", (ids['tenant'], record, media))
        with pytest.raises(CheckViolation):
            with db.transaction():
                db.execute("""INSERT INTO record_attachments(tenant_id,target_type,target_id,kind,storage_path,original_name,media_type,size_bytes)
                    VALUES (%s,'erp_document',%s,'file','/x','x','image/png',10485761)""", (ids['tenant'], record))


def test_a_link_with_an_uppercase_scheme_is_stored_lowercase_not_a_server_error(company):
    connect, ids = company
    accountant, record = ids['accountant'], ids['records']['money-in']
    made = link(accountant, 'money-in', record, 'HTTPS://Bank.Example.com/Slip/9')
    assert made.status_code == 201, made.text
    assert link(accountant, 'money-in', record, 'Https://example.com/b', 'Mixed case').status_code == 201
    assert [i['url'] for i in listing(accountant, 'money-in', record)] == ['https://Bank.Example.com/Slip/9', 'https://example.com/b']
    with connect() as db:
        assert db.execute("SELECT count(*) AS n FROM record_attachments WHERE target_id=%s AND url !~ '^https://'", (record,)).fetchone()['n'] == 0


def test_files_are_served_with_the_safe_preview_headers(company):
    _, ids = company
    accountant, record = ids['accountant'], ids['records']['payment']
    upload(accountant, 'payment', record, PNG, 'slip.png')
    upload(accountant, 'payment', record, PDF, 'statement.pdf', 'application/pdf')
    png, pdf = listing(accountant, 'payment', record)
    served = call('GET', png['fileUrl'], ids['admin'])  # any user of the company can read it
    assert served.status_code == 200 and served.content == PNG
    assert served.headers['content-type'] == 'image/png'
    assert served.headers['x-content-type-options'] == 'nosniff'
    assert served.headers['content-security-policy'] == 'sandbox'
    assert 'inline' in served.headers['content-disposition'] and 'slip.png' in served.headers['content-disposition']
    served_pdf = call('GET', pdf['fileUrl'], accountant)
    assert served_pdf.headers['content-type'] == 'application/pdf'
    assert served_pdf.headers['x-content-type-options'] == 'nosniff'
    assert 'content-security-policy' not in served_pdf.headers
    # A link has no file to serve.
    made = link(accountant, 'payment', record, 'https://example.com/a')
    assert call('GET', f"/api/erp/attachments/{made.json()['id']}/file", accountant).status_code == 404


def test_served_type_follows_the_bytes_on_disk(company):
    connect, ids = company
    accountant, record = ids['accountant'], ids['records']['payment']
    upload(accountant, 'payment', record, PNG)
    item = listing(accountant, 'payment', record)[0]
    with connect() as db:
        path = Path(db.execute("SELECT storage_path FROM record_attachments WHERE id=%s", (item['id'],)).fetchone()['storage_path'])
    path.write_bytes(b'<html><script>alert(1)</script></html>')  # the stored object was swapped
    served = call('GET', item['fileUrl'], accountant)
    assert served.headers['content-type'] == 'application/octet-stream'
    assert served.headers['content-disposition'].startswith('attachment')
    assert served.headers['x-content-type-options'] == 'nosniff' and served.headers['content-security-policy'] == 'sandbox'


def test_another_company_gets_404_everywhere(company):
    connect, ids = company
    accountant, outsider = ids['accountant'], ids['outsider']
    record = ids['records']['payment']
    upload(accountant, 'payment', record, PNG)
    link(accountant, 'payment', record, 'https://example.com/a')
    file_item, link_item = listing(accountant, 'payment', record)
    assert call('GET', f'/api/erp/documents/payment/{record}/attachments', outsider).status_code == 404
    assert upload(outsider, 'payment', record, PNG).status_code == 404
    assert link(outsider, 'payment', record, 'https://example.com/b').status_code == 404
    assert call('GET', file_item['fileUrl'], outsider).status_code == 404
    assert call('DELETE', f"/api/erp/attachments/{file_item['id']}", outsider).status_code == 404
    assert call('DELETE', f"/api/erp/attachments/{link_item['id']}", outsider).status_code == 404
    # A made-up or malformed id is 404/422, never another company's data; the outsider's own record stays separate.
    assert call('GET', f"/api/erp/attachments/{uuid4()}/file", accountant).status_code == 404
    assert call('GET', '/api/erp/attachments/not-a-uuid/file', accountant).status_code == 422
    assert listing(outsider, 'payment', ids['foreign']) == []
    assert len(listing(accountant, 'payment', record)) == 2
    with connect() as db:
        assert db.execute("SELECT count(*) AS n FROM record_attachments WHERE tenant_id=%s", (ids['other_tenant'],)).fetchone()['n'] == 0
        assert db.execute("SELECT count(*) AS n FROM record_attachments WHERE deleted_at IS NOT NULL").fetchone()['n'] == 0
    # The database itself refuses a record from another company.
    with connect() as db:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            db.execute("""INSERT INTO record_attachments(tenant_id,target_type,target_id,kind,url,original_name)
                VALUES (%s,'erp_document',%s,'link','https://example.com/x','x')""", (ids['other_tenant'], record))


def test_remove_is_a_soft_delete_and_every_change_is_audited(company):
    connect, ids = company
    accountant, record = ids['accountant'], ids['records']['money-out']
    upload(accountant, 'money-out', record, JPEG, 'receipt.jpg', 'image/jpeg')
    link(accountant, 'money-out', record, 'https://example.com/evidence')
    file_item, link_item = listing(accountant, 'money-out', record)
    with connect() as db:
        path = Path(db.execute("SELECT storage_path FROM record_attachments WHERE id=%s", (file_item['id'],)).fetchone()['storage_path'])
    assert path.is_file()

    removed = call('DELETE', f"/api/erp/attachments/{file_item['id']}", ids['admin'])
    assert removed.status_code == 200, removed.text
    assert [i['id'] for i in removed.json()['items']] == [link_item['id']]
    assert [i['id'] for i in listing(accountant, 'money-out', record)] == [link_item['id']]
    assert call('GET', file_item['fileUrl'], accountant).status_code == 404         # gone from the app ...
    assert call('DELETE', f"/api/erp/attachments/{file_item['id']}", accountant).status_code == 404  # ... and not removable twice
    with connect() as db:                                                           # ... but kept for audit
        row = db.execute("SELECT deleted_at, deleted_by, storage_path, original_name FROM record_attachments WHERE id=%s", (file_item['id'],)).fetchone()
    assert row['deleted_at'] is not None and row['deleted_by'] == ids['admin']['id'] and row['original_name'] == 'receipt.jpg'
    assert path.is_file()

    added = audit_rows(connect, ids, 'erp.attachment_added')
    assert [(json.loads(a['details'])['kind'], a['user_id']) for a in added] == [('file', ids['accountant']['id']), ('link', ids['accountant']['id'])]
    assert all(a['resource'] == record for a in added)
    detail = json.loads(added[0]['details'])
    assert detail['name'] == 'receipt.jpg' and detail['mediaType'] == 'image/jpeg' and detail['sizeBytes'] == len(JPEG)
    assert detail['type'] == 'money-out' and detail['number'] == 'MONEY-OUT-1' and detail['attachmentId'] == file_item['id']
    gone = audit_rows(connect, ids, 'erp.attachment_removed')
    assert len(gone) == 1 and gone[0]['user_id'] == ids['admin']['id'] and gone[0]['resource'] == record
    assert json.loads(gone[0]['details'])['attachmentId'] == file_item['id']


def test_a_record_holds_a_limited_number_and_a_refused_upload_leaves_no_file(company, monkeypatch, tmp_path):
    _, ids = company
    accountant, record = ids['accountant'], ids['records']['journal-entry']
    monkeypatch.setattr(attachments, 'MAX_ATTACHMENTS_PER_RECORD', 2)
    assert upload(accountant, 'journal-entry', record, PNG).status_code == 201
    assert link(accountant, 'journal-entry', record, 'https://example.com/a').status_code == 201
    stored = sorted((tmp_path / 'storage').rglob('*.png'))
    assert link(accountant, 'journal-entry', record, 'https://example.com/b').status_code == 422
    assert upload(accountant, 'journal-entry', record, PNG, 'again.png').status_code == 422
    assert sorted((tmp_path / 'storage').rglob('*.png')) == stored  # the refused upload's file was cleaned up
    # Removing one frees a place.
    first = listing(accountant, 'journal-entry', record)[0]
    assert call('DELETE', f"/api/erp/attachments/{first['id']}", accountant).status_code == 200
    assert link(accountant, 'journal-entry', record, 'https://example.com/b').status_code == 201


def test_production_without_object_storage_refuses_uploads(company, monkeypatch):
    _, ids = company
    accountant, record = ids['accountant'], ids['records']['refund']
    monkeypatch.setattr(attachments, 'is_production', lambda: True)
    response = upload(accountant, 'refund', record, PNG)
    assert response.status_code == 503 and 'object storage' in response.json()['detail']
    assert link(accountant, 'refund', record, 'https://example.com/a').status_code == 201  # links need no storage


def test_names_cannot_escape_the_private_storage(company, tmp_path):
    connect, ids = company
    accountant, record = ids['accountant'], ids['records']['purchase-refund']
    response = upload(accountant, 'purchase-refund', record, PNG, '../../../escape.png')
    assert response.status_code == 201, response.text
    assert listing(accountant, 'purchase-refund', record)[0]['name'] == 'escape.png'
    with connect() as db:
        path = Path(db.execute("SELECT storage_path FROM record_attachments WHERE target_id=%s", (record,)).fetchone()['storage_path'])
    assert path.is_relative_to((tmp_path / 'storage').resolve()) and path.is_file()
    assert not (tmp_path / 'escape.png').exists()
