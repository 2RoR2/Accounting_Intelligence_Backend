-- Accounting Intelligence development seed data
--
-- Populates the major schema areas with fictional development-only records.
-- Run PostgreSQL Database schema.sql first, then run this file once.
-- Demo password for all seeded users: DemoPass!2026
-- This seed uses fixed UUIDs and is intentionally not idempotent. Rebuild the
-- development database with the schema file before running it again.
-- No real documents, credentials, or customer data are included.

BEGIN;

-- ---------------------------------------------------------------------------
-- Core tenant, users, validation, vendors, customers, and documents
-- ---------------------------------------------------------------------------

INSERT INTO tenants (id, tenant_code, name, registration_number, admin_email, seat_limit, status) VALUES

('00000000-0000-4000-8000-000100000001', 'TENANT_DEMO_001', 'Demo Company — Sarawak Office', 'DEMO-REG-001', 'reviewer.demo@saic.example', 25, 'active');

INSERT INTO users (id, tenant_id, name, email, password_hash, role, status, must_change_password) VALUES

('00000000-0000-4000-8000-001000000001', '00000000-0000-4000-8000-000100000001', 'Demo Accountant', 'accountant.demo@saic.example', 'scrypt$mock-salt-2026$d0da1f0a7dbdf504be03a70dd2cdeb37807890588917a8f342ed5fe0a82c6c377d4f9a580c91a387feed98c56ab1314a185e1c83fb43f77f7ca77a9b86d989a8', 'accountant', 'active', FALSE),
('00000000-0000-4000-8000-001000000002', '00000000-0000-4000-8000-000100000001', 'Demo Local Admin', 'reviewer.demo@saic.example', 'scrypt$mock-salt-2026$d0da1f0a7dbdf504be03a70dd2cdeb37807890588917a8f342ed5fe0a82c6c377d4f9a580c91a387feed98c56ab1314a185e1c83fb43f77f7ca77a9b86d989a8', 'local_admin', 'active', FALSE),
('00000000-0000-4000-8000-001000000003', NULL, 'Demo Platform Admin', 'platform.demo@saic.example', 'scrypt$mock-salt-2026$d0da1f0a7dbdf504be03a70dd2cdeb37807890588917a8f342ed5fe0a82c6c377d4f9a580c91a387feed98c56ab1314a185e1c83fb43f77f7ca77a9b86d989a8', 'super_admin', 'active', FALSE),
('00000000-0000-4000-8000-001000000005', '00000000-0000-4000-8000-000100000001', 'AI Admin Portal', 'ai.adminportal@gmail.com', 'scrypt$mock-salt-2026$d0da1f0a7dbdf504be03a70dd2cdeb37807890588917a8f342ed5fe0a82c6c377d4f9a580c91a387feed98c56ab1314a185e1c83fb43f77f7ca77a9b86d989a8', 'accountant', 'active', FALSE);

-- Resend OTP test account. Password: DemoPass!2026
INSERT INTO tenants (id, tenant_code, name, registration_number, admin_email, seat_limit, status) VALUES
('00000000-0000-4000-8000-000100000002', 'TENANT_SWIN_001', 'ABC Sdn Bhd', 'SWIN-ABC-001', '102784694@students.swinburne.edu.my', 10, 'active');

INSERT INTO users (id, tenant_id, name, email, password_hash, role, status, must_change_password) VALUES
('00000000-0000-4000-8000-001000000004', '00000000-0000-4000-8000-000100000002', 'Swinburne Accountant', '102784694@students.swinburne.edu.my', '$argon2id$v=19$m=65536,t=3,p=2$saG536sug6m5NQuw2P9few$yrjPaMrmODe6AhGeqNqyRVYZCM2Y1Mbli3p0SwI0GcE', 'accountant', 'active', FALSE);

INSERT INTO validation_rules (tenant_id) VALUES ('00000000-0000-4000-8000-000100000001');

INSERT INTO vendors (id, tenant_id, vendor_name, registration_number, address) VALUES

('00000000-0000-4000-8000-003000000001', '00000000-0000-4000-8000-000100000001', 'Borneo Office Supplies Sdn Bhd', 'DEMO-SUP-001', 'Kuching, Sarawak'),
('00000000-0000-4000-8000-003000000002', '00000000-0000-4000-8000-000100000001', 'Sarawak Tech Solutions Sdn Bhd', 'DEMO-SUP-002', 'Kuching, Sarawak'),
('00000000-0000-4000-8000-003000000003', '00000000-0000-4000-8000-000100000001', 'Kuching Stationery Centre', 'DEMO-SUP-003', 'Kuching, Sarawak'),
('00000000-0000-4000-8000-003000000004', '00000000-0000-4000-8000-000100000001', 'East Malaysia Logistics Sdn Bhd', 'DEMO-SUP-004', 'Kuching, Sarawak');

INSERT INTO customers (id, tenant_id, customer_name, registration_number, address) VALUES

('00000000-0000-4000-8000-004000000001', '00000000-0000-4000-8000-000100000001', 'Riverfront Trading Sdn Bhd', 'DEMO-CUS-001', 'Sarawak, Malaysia'),
('00000000-0000-4000-8000-004000000002', '00000000-0000-4000-8000-000100000001', 'Santubong Creative Studio', 'DEMO-CUS-002', 'Sarawak, Malaysia'),
('00000000-0000-4000-8000-004000000003', '00000000-0000-4000-8000-000100000001', 'Metro Retail Sdn Bhd', 'DEMO-CUS-003', 'Sarawak, Malaysia');

INSERT INTO documents (id, document_code, tenant_id, owner_id, original_filename, kind, category, storage_path, mime_type, file_size_bytes, file_hash_sha256, status, content_revision) VALUES

('00000000-0000-4000-8000-002000000001', 'INVOICE_100001', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'invoice_001.pdf', 'invoice', 'accounts_receivable', 'mock/invoice_001.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0001', 'completed', 1),
('00000000-0000-4000-8000-002000000002', 'BILL_100002', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'bill_002.pdf', 'bill', 'accounts_payable', 'mock/bill_002.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0002', 'exception', 1),
('00000000-0000-4000-8000-002000000003', 'RECEIPT_100003', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'receipt_003.jpg', 'receipt', 'accounts_payable', 'mock/receipt_003.jpg', 'image/jpeg', 102400, 'sha256-DEMO-DOC-0003', 'completed', 1),
('00000000-0000-4000-8000-002000000004', 'INVOICE_100004', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'invoice_004.pdf', 'invoice', 'accounts_receivable', 'mock/invoice_004.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0004', 'in_review', 1),
('00000000-0000-4000-8000-002000000005', 'BILL_100005', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'bill_005.pdf', 'bill', 'accounts_payable', 'mock/bill_005.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0005', 'exception', 1),
('00000000-0000-4000-8000-002000000006', 'BILL_100006', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'bill_006.pdf', 'bill', 'accounts_payable', 'mock/bill_006.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0006', 'completed', 1),
('00000000-0000-4000-8000-002000000007', 'RECEIPT_100007', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'receipt_007.jpg', 'receipt', 'accounts_payable', 'mock/receipt_007.jpg', 'image/jpeg', 102400, 'sha256-DEMO-DOC-0007', 'exception', 1),
('00000000-0000-4000-8000-002000000008', 'INVOICE_100008', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'invoice_008.pdf', 'invoice', 'accounts_receivable', 'mock/invoice_008.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0008', 'exception', 1),
('00000000-0000-4000-8000-002000000009', 'RECEIPT_100009', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'receipt_009.jpg', 'receipt', 'accounts_payable', 'mock/receipt_009.jpg', 'image/jpeg', 102400, 'sha256-DEMO-DOC-0009', 'validated', 1),
('00000000-0000-4000-8000-002000000010', 'BILL_100010', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'bill_010.pdf', 'bill', 'accounts_payable', 'mock/bill_010.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0010', 'failed', 1),
('00000000-0000-4000-8000-002000000011', 'BILL_100011', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'bill_011.pdf', 'bill', 'accounts_payable', 'mock/bill_011.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0011', 'exception', 1),
('00000000-0000-4000-8000-002000000012', 'INVOICE_100012', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'invoice_012.pdf', 'invoice', 'accounts_receivable', 'mock/invoice_012.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0012', 'in_review', 1),
('00000000-0000-4000-8000-002000000013', 'CREDIT_NOTE_100013', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'credit_note_013.pdf', 'credit_note', 'accounts_receivable', 'mock/credit_note_013.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0013', 'in_review', 1),
('00000000-0000-4000-8000-002000000014', 'DEBIT_NOTE_100014', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'debit_note_014.pdf', 'debit_note', 'accounts_payable', 'mock/debit_note_014.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0014', 'in_review', 1),
('00000000-0000-4000-8000-002000000015', 'RECEIPT_100015', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'receipt_015.jpg', 'receipt', 'accounts_payable', 'mock/receipt_015.jpg', 'image/jpeg', 102400, 'sha256-DEMO-DOC-0015', 'exception', 1),
('00000000-0000-4000-8000-002000000016', 'BILL_100016', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'bill_016.pdf', 'bill', 'accounts_payable', 'mock/bill_016.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0016', 'processing', 1),
('00000000-0000-4000-8000-002000000017', 'INVOICE_100017', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'invoice_017.pdf', 'invoice', 'accounts_receivable', 'mock/invoice_017.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0017', 'exception', 1),
('00000000-0000-4000-8000-002000000018', 'RECEIPT_100018', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'receipt_018.jpg', 'receipt', 'accounts_payable', 'mock/receipt_018.jpg', 'image/jpeg', 102400, 'sha256-DEMO-DOC-0018', 'exception', 1),
('00000000-0000-4000-8000-002000000019', 'BILL_100019', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'bill_019.pdf', 'bill', 'accounts_payable', 'mock/bill_019.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0019', 'in_review', 1),
('00000000-0000-4000-8000-002000000020', 'INVOICE_100020', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'invoice_020.pdf', 'invoice', 'accounts_receivable', 'mock/invoice_020.pdf', 'application/pdf', 102400, 'sha256-DEMO-DOC-0020', 'completed', 1);

INSERT INTO raw_extractions (id, document_id, run_number, is_current, routing_decision, raw_json) VALUES

('00000000-0000-4000-8000-005000000001', '00000000-0000-4000-8000-002000000001', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0001", "document_number": "INV-2026-1001", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000002', '00000000-0000-4000-8000-002000000002', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0002", "document_number": "BOS-8421", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000003', '00000000-0000-4000-8000-002000000003', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0003", "document_number": "R-2039", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000004', '00000000-0000-4000-8000-002000000004', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0004", "document_number": "INV-2026-1002", "confidence": 0.62}'::jsonb),
('00000000-0000-4000-8000-005000000005', '00000000-0000-4000-8000-002000000005', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0005", "document_number": "STS-7781", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000006', '00000000-0000-4000-8000-002000000006', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0006", "document_number": "STS-7781", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000007', '00000000-0000-4000-8000-002000000007', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0007", "document_number": null, "confidence": null}'::jsonb),
('00000000-0000-4000-8000-005000000008', '00000000-0000-4000-8000-002000000008', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0008", "document_number": null, "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000009', '00000000-0000-4000-8000-002000000009', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0009", "document_number": null, "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000010', '00000000-0000-4000-8000-002000000010', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0010", "document_number": "EML-1112", "confidence": null}'::jsonb),
('00000000-0000-4000-8000-005000000011', '00000000-0000-4000-8000-002000000011', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0011", "document_number": "BOS-8422", "confidence": 0.62}'::jsonb),
('00000000-0000-4000-8000-005000000012', '00000000-0000-4000-8000-002000000012', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0012", "document_number": "INV-2026-1003", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000013', '00000000-0000-4000-8000-002000000013', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0013", "document_number": "CN-2026-001", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000014', '00000000-0000-4000-8000-002000000014', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0014", "document_number": "DN-2026-001", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000015', '00000000-0000-4000-8000-002000000015', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0015", "document_number": "R-2040", "confidence": 0.62}'::jsonb),
('00000000-0000-4000-8000-005000000016', '00000000-0000-4000-8000-002000000016', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0016", "document_number": "BOS-8423", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000017', '00000000-0000-4000-8000-002000000017', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0017", "document_number": "INV-2026-1004", "confidence": 0.62}'::jsonb),
('00000000-0000-4000-8000-005000000018', '00000000-0000-4000-8000-002000000018', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0018", "document_number": "R-2041", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000019', '00000000-0000-4000-8000-002000000019', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0019", "document_number": "EML-1113", "confidence": 0.96}'::jsonb),
('00000000-0000-4000-8000-005000000020', '00000000-0000-4000-8000-002000000020', 1, TRUE, 'parser_only', '{"fixture_id": "DEMO-DOC-0020", "document_number": "INV-2026-1005", "confidence": 0.96}'::jsonb);

-- ---------------------------------------------------------------------------
-- Extraction runs, field predictions, and line items
-- ---------------------------------------------------------------------------
-- Rich extracted invoice fixture used by the accountant review screen.
INSERT INTO field_predictions
    (extraction_id, document_id, field_key, field_label, extraction_state,
     resolution, raw_value, normalized_value, current_value, confidence,
     extraction_method)
VALUES
('00000000-0000-4000-8000-005000000001','00000000-0000-4000-8000-002000000001','supplier','Supplier','resolved','parser_preserved','Riverfront Trading Sdn Bhd','Riverfront Trading Sdn Bhd','Riverfront Trading Sdn Bhd',0.960,'PARSER'),
('00000000-0000-4000-8000-005000000001','00000000-0000-4000-8000-002000000001','customer','Customer','resolved','parser_preserved','Demo Customer Sdn Bhd','Demo Customer Sdn Bhd','Demo Customer Sdn Bhd',0.960,'PARSER'),
('00000000-0000-4000-8000-005000000001','00000000-0000-4000-8000-002000000001','invoice','Document number','resolved','parser_preserved','INV-2026-1001','INV-2026-1001','INV-2026-1001',0.960,'PARSER'),
('00000000-0000-4000-8000-005000000001','00000000-0000-4000-8000-002000000001','date','Issue date','resolved','parser_preserved','2026-10-13','2026-10-13','2026-10-13',0.960,'PARSER'),
('00000000-0000-4000-8000-005000000001','00000000-0000-4000-8000-002000000001','currency','Currency','resolved','parser_preserved','MYR','MYR','MYR',0.990,'PARSER'),
('00000000-0000-4000-8000-005000000001','00000000-0000-4000-8000-002000000001','type','Document type','resolved','parser_preserved','Invoice','Invoice','Invoice',0.990,'PARSER'),
('00000000-0000-4000-8000-005000000001','00000000-0000-4000-8000-002000000001','total','Total payable amount','resolved','parser_preserved','1250.00','1250.00','1250.00',0.960,'PARSER');

INSERT INTO line_items
    (extraction_id, document_id, line_number, description, quantity, unit_price, amount)
VALUES
('00000000-0000-4000-8000-005000000001','00000000-0000-4000-8000-002000000001',1,'Professional accounting services',1,1250.00,1250.00);

-- Reviewable low-confidence invoice fixture.
INSERT INTO field_predictions
    (extraction_id, document_id, field_key, field_label, extraction_state,
     resolution, raw_value, normalized_value, current_value, confidence,
     extraction_method)
VALUES
('00000000-0000-4000-8000-005000000004','00000000-0000-4000-8000-002000000004','supplier','Supplier','resolved','parser_preserved','Santubong Creative Studio','Santubong Creative Studio','Santubong Creative Studio',0.620,'PARSER'),
('00000000-0000-4000-8000-005000000004','00000000-0000-4000-8000-002000000004','customer','Customer','resolved','parser_preserved','Santubong Creative Studio','Santubong Creative Studio','Santubong Creative Studio',0.620,'PARSER'),
('00000000-0000-4000-8000-005000000004','00000000-0000-4000-8000-002000000004','invoice','Document number','resolved','parser_preserved','INV-2026-1002','INV-2026-1002','INV-2026-1002',0.620,'PARSER'),
('00000000-0000-4000-8000-005000000004','00000000-0000-4000-8000-002000000004','date','Issue date','resolved','parser_preserved','2026-10-04','2026-10-04','2026-10-04',0.620,'PARSER'),
('00000000-0000-4000-8000-005000000004','00000000-0000-4000-8000-002000000004','currency','Currency','resolved','parser_preserved','MYR','MYR','MYR',0.990,'PARSER'),
('00000000-0000-4000-8000-005000000004','00000000-0000-4000-8000-002000000004','total','Total payable amount','resolved','parser_preserved','2300.00','2300.00','2300.00',0.620,'PARSER');

INSERT INTO line_items
    (extraction_id, document_id, line_number, description, quantity, unit_price, amount)
VALUES
('00000000-0000-4000-8000-005000000004','00000000-0000-4000-8000-002000000004',1,'Creative design services',1,2300.00,2300.00);

-- ---------------------------------------------------------------------------
-- Processing jobs and worker leases
-- ---------------------------------------------------------------------------
-- Processing-job fixtures from the frontend data. The failed job is terminal,
-- so finished_at is populated as required by pending_jobs.
INSERT INTO pending_jobs
    (id, tenant_id, document_id, status, attempt_count, max_attempts,
     dispatch_count, next_attempt_at, last_error, finished_at)
VALUES
    ('00000000-0000-4000-8000-009000000001', '00000000-0000-4000-8000-000100000001',
     '00000000-0000-4000-8000-002000000010', 'failed', 3, 3, 3, now(),
     'AI_TIMEOUT', now()),
    ('00000000-0000-4000-8000-009000000002', '00000000-0000-4000-8000-000100000001',
     '00000000-0000-4000-8000-002000000003', 'succeeded', 1, 3, 1, now(),
     NULL, now());

-- Active extraction fixture shown on the accountant dashboard. The worker can
-- process this document through queue.jobs when a real demo
-- file is placed under DOCUMENT_STORAGE_PATH.
INSERT INTO pending_jobs
    (id, tenant_id, document_id, status, attempt_count, max_attempts,
     dispatch_count, next_attempt_at, claim_token, claimed_at,
     heartbeat_at, processing_expires_at)
VALUES
    ('00000000-0000-4000-8000-009000000003', '00000000-0000-4000-8000-000100000001',
     '00000000-0000-4000-8000-002000000016', 'processing', 1, 3, 1,
     now(), gen_random_uuid(), now() - interval '18 seconds',
     now() - interval '3 seconds', now() + interval '15 minutes');

INSERT INTO workspace_settings (
    tenant_id, timezone, notifications, session_timeout_minutes,
    default_document_view, email_assignment_notifications,
    processing_status_notifications, upload_max_size_mb, allow_camera_uploads
)
VALUES (
    '00000000-0000-4000-8000-000100000001', 'Asia/Kuala_Lumpur', 'enabled', 30,
    'all', TRUE, TRUE, 20, TRUE
);

-- ---------------------------------------------------------------------------
-- Persisted role-aware navigation used by the frontend application shell
-- ---------------------------------------------------------------------------
INSERT INTO workspace_modules(module_key, role, section, label, route, icon_key, sort_order)
VALUES
('local.dashboard','local_admin','Dashboard','Dashboard','dashboard','dashboard',10),
('local.documents','local_admin','Documents','All Documents','documents','documents',20),
('local.upload','local_admin','Digital Shoebox','Upload Documents','upload','upload',30),
('local.batch','local_admin','Digital Shoebox','Batch Uploads','upload','upload',31),
('local.queue','local_admin','Digital Shoebox','Processing Queue','documents','processing',32),
('local.failed','local_admin','Digital Shoebox','Failed Processing','documents','failed',33),
('local.review','local_admin','Review & Approval','Review Queue','documents','review',40),
('local.approvals','local_admin','Review & Approval','Pending Approvals','documents','approval',41),
('local.exceptions','local_admin','Exceptions','All Exceptions','exceptions','exceptions',50),
('local.contacts','local_admin','Contacts','Customers and Suppliers','records','contacts',60),
('local.records','local_admin','Standardised Records','All Records','records','records',70),
('local.reports','local_admin','Reports & Exports','Document Register','reports','reports',80),
('local.audit','local_admin','Audit Trail','All Activities','audit','audit',90),
('local.users','local_admin','Team Management','All Users','users','users',100),
('local.settings','local_admin','Company Settings','Workspace Settings','settings','settings',110),
('local.profile','local_admin','Account','My Profile','profile','profile',120),
('platform.dashboard','super_admin','Dashboard','Dashboard','dashboard','dashboard',10),
('platform.companies','super_admin','Company Management','All Companies','tenants','companies',20),
('platform.requests','super_admin','Company Management','Pending Company Requests','approvals','approval',21),
('platform.users','super_admin','Account & Access Management','Platform Users','users','users',30),
('platform.processing','super_admin','AI Processing','Processing Overview','processing','processing',40),
('platform.performance','super_admin','Extraction Performance','Processing Performance','performance','performance',50),
('platform.records','super_admin','Platform Records','Record Overview','records','records',60),
('platform.analytics','super_admin','Reports & Analytics','Platform Usage','performance','reports',70),
('platform.audit','super_admin','Security & Audit','Platform Audit Logs','audit','audit',80),
('platform.operations','super_admin','System Operations','Service Health','settings','health',90),
('platform.settings','super_admin','System Settings','Platform Configuration','settings','settings',100),
('platform.profile','super_admin','Account','My Profile','profile','profile',110),
('local.account_limits','local_admin','Team Management','Account Limit Requests','users','users',105),
('platform.mfa_recovery','super_admin','Account & Access Management','MFA Recovery Requests','approvals','security',35);

-- Additional persisted navigation modules for the current Local Admin and
-- Super Admin menus. These are views over supported parent routes.
INSERT INTO workspace_modules(module_key, role, section, label, route, icon_key, sort_order)
VALUES
('local.sales','local_admin','Documents','Sales Invoices','documents?kind=invoice&category=accounts_receivable','documents',21),
('local.purchases','local_admin','Documents','Purchase Bills','documents?kind=bill&category=accounts_payable','documents',22),
('local.receipts','local_admin','Documents','Receipts','documents?kind=receipt','documents',23),
('local.credit_notes','local_admin','Documents','Credit Notes','documents?kind=credit_note','documents',24),
('local.debit_notes','local_admin','Documents','Debit Notes','documents?kind=debit_note','documents',25),
('local.returned','local_admin','Review & Approval','Returned for Correction','documents?view=returned','review',42),
('local.history','local_admin','Review & Approval','Approval History','documents?view=approval-history','review',43),
('local.blocking','local_admin','Exceptions','Blocking Errors','exceptions?severity=high','exceptions',51),
('local.warnings','local_admin','Exceptions','Warnings','exceptions?severity=medium','exceptions',52),
('local.resolved','local_admin','Exceptions','Resolved Exceptions','exceptions?status=resolved','exceptions',53),
('local.customers','local_admin','Contacts','Customers','contacts?type=customer','contacts',61),
('local.suppliers','local_admin','Contacts','Suppliers','contacts?type=supplier','contacts',62),
('local.matching','local_admin','Contacts','Contact Matching','contacts?view=matching','contacts',63),
('local.sales_records','local_admin','Standardised Records','Sales Records','records?category=accounts_receivable','records',71),
('local.purchase_records','local_admin','Standardised Records','Purchase Records','records?category=accounts_payable','records',72),
('local.amendments','local_admin','Standardised Records','Record Amendments','records?view=amendments','records',73),
('local.processing_reports','local_admin','Reports & Exports','Processing Reports','reports?tab=processing','reports',81),
('local.validation_reports','local_admin','Reports & Exports','Validation Reports','reports?tab=validation','reports',82),
('local.review_reports','local_admin','Reports & Exports','Review Reports','reports?tab=review','reports',83),
('local.export_history','local_admin','Reports & Exports','Export History','reports?tab=exports','reports',84),
('local.changes','local_admin','Audit Trail','Document Changes','audit?tab=documents','audit',91),
('local.approval_actions','local_admin','Audit Trail','Approval Actions','audit?tab=approvals','audit',92),
('local.user_activities','local_admin','Audit Trail','User Activities','audit?tab=users','audit',93),
('local.invitations','local_admin','Team Management','Invitations','users?view=invitations','users',101),
('local.permissions','local_admin','Team Management','Roles & Permissions','users?view=permissions','users',102),
('local.seats','local_admin','Team Management','Seat Usage','users?view=seats','users',103),
('local.workload','local_admin','Team Management','Workload Assignment','users?view=workload','users',104),
('local.company_profile','local_admin','Company Settings','Company Profile','settings?tab=company','settings',111),
('local.validation','local_admin','Company Settings','Validation Rules','validation-rules','settings',112),
('local.approval_workflow','local_admin','Company Settings','Approval Workflow','settings?tab=approval','settings',113),
('local.notifications','local_admin','Company Settings','Notifications','settings?tab=notifications','settings',114),
('local.security','local_admin','Account','Security Settings','profile?tab=security','security',121),
('platform.pending_requests','super_admin','Company Management','Approved / Rejected Requests','approvals?view=history','approvals',22),
('platform.suspended','super_admin','Company Management','Suspended Companies','tenants?status=suspended','tenants',23),
('platform.company_details','super_admin','Company Management','Company Details','tenants?view=details','tenants',24),
('platform.company_admins','super_admin','Account & Access Management','Company Administrators','users?role=local_admin','users',31),
('platform.limit_requests','super_admin','Account & Access Management','Account Limit Requests','users?view=limits','users',32),
('platform.seats','super_admin','Account & Access Management','Seat Allocation','users?view=seats','users',33),
('platform.access_reviews','super_admin','Account & Access Management','Access Reviews','users?view=access-reviews','security',34),
('platform.jobs','super_admin','AI Processing','Job Queue','processing?view=queue','processing',41),
('platform.failed_jobs','super_admin','AI Processing','Failed Jobs','processing?view=failed','processing',42),
('platform.ai_health','super_admin','AI Processing','AI Service Health','processing?view=health','health',43),
('platform.metadata','super_admin','Platform Records','Record Metadata','records?view=metadata','records',61),
('platform.integrity','super_admin','Platform Records','Record Integrity','records?view=integrity','records',62),
('platform.support','super_admin','Platform Records','Support Access Requests','records?view=support','security',63),
('platform.processing_reports','super_admin','Reports & Analytics','Processing Performance','reports?tab=processing','reports',71),
('platform.quality','super_admin','Reports & Analytics','Extraction Quality','reports?tab=quality','reports',72),
('platform.failures','super_admin','Reports & Analytics','Failure Trends','reports?tab=failures','reports',73),
('platform.exports','super_admin','Reports & Analytics','Operational Exports','reports?tab=exports','reports',74),
('platform.auth_events','super_admin','Security & Audit','Authentication Events','audit?tab=authentication','security',81),
('platform.permission_changes','super_admin','Security & Audit','Permission Changes','audit?tab=permissions','security',82),
('platform.alerts','super_admin','Security & Audit','Security Alerts','audit?tab=alerts','security',83),
('platform.support_history','super_admin','Security & Audit','Support Access History','audit?tab=support','security',84),
('platform.storage','super_admin','System Operations','Database & Storage','settings?tab=storage','settings',92),
('platform.workers','super_admin','System Operations','Background Workers','settings?tab=workers','settings',93),
('platform.backups','super_admin','System Operations','Backup Status','settings?tab=backups','settings',94),
('platform.system_notifications','super_admin','System Operations','System Notifications','settings?tab=notifications','settings',95),
('platform.upload_policies','super_admin','System Settings','Upload Policies','settings?tab=upload','settings',101),
('platform.ai_settings','super_admin','System Settings','AI Provider Settings','settings?tab=ai','settings',102),
('platform.email','super_admin','System Settings','Email Configuration','settings?tab=email','settings',103),
('platform.security_defaults','super_admin','System Settings','Default Security Policies','settings?tab=security','security',104),
('platform.security','super_admin','Account','Security Settings','profile?tab=security','security',111)
ON CONFLICT (module_key) DO UPDATE SET
  section=EXCLUDED.section, label=EXCLUDED.label, route=EXCLUDED.route,
  icon_key=EXCLUDED.icon_key, sort_order=EXCLUDED.sort_order, enabled=TRUE;

INSERT INTO notifications (tenant_id, user_id, type, title, message, resource)
VALUES
('00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'review_request', 'Review request', 'Documents are waiting for your review.', 'INVOICE_100012'),
('00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'processing_failure', 'Processing failure', 'BILL_100010 failed extraction and is ready for re-detection.', 'BILL_100010'),
('00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'assignment', 'Assignment', 'A document has been assigned to you for validation.', 'BILL_100011');

-- Every fixture document has an initial status-history entry. This is useful
-- for the inbox timeline and remains compatible with the automatic trigger.
INSERT INTO document_status_history
    (document_id, tenant_id, old_status, new_status, changed_by, reason)
SELECT id, tenant_id, NULL, status,
       '00000000-0000-4000-8000-001000000001',
       'Initial status imported from frontend fixture'
FROM documents;

INSERT INTO review_submissions (id, document_id, tenant_id, submitted_by, decision, note) VALUES

('00000000-0000-4000-8000-006000000001', '00000000-0000-4000-8000-002000000001', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'submitted', 'Fixture review submission');

INSERT INTO review_decisions (id, submission_id, document_id, tenant_id, reviewed_by, decision, note, approved_content_revision) VALUES

('00000000-0000-4000-8000-006100000001', '00000000-0000-4000-8000-006000000001', '00000000-0000-4000-8000-002000000001', '00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000002', 'approved', 'Fixture approval', 1);

INSERT INTO exceptions (id, document_id, tenant_id, type, severity, status, message, note) VALUES

('00000000-0000-4000-8000-007000000001', '00000000-0000-4000-8000-002000000002', '00000000-0000-4000-8000-000100000001', 'TOTAL_MISMATCH', 'high', 'open', 'Document total differs from the sum of extracted line items.', 'Compare the original document and correct the total or line items.'),
('00000000-0000-4000-8000-007000000002', '00000000-0000-4000-8000-002000000004', '00000000-0000-4000-8000-000100000001', 'LOW_CONFIDENCE', 'medium', 'open', 'AI could not confidently read one or more fields.', 'Review highlighted source evidence and confirm the values.'),
('00000000-0000-4000-8000-007000000003', '00000000-0000-4000-8000-002000000005', '00000000-0000-4000-8000-000100000001', 'DUPLICATE_DOCUMENT', 'high', 'open', 'A document with the same business and reference already exists.', 'Open the existing document and confirm whether this is a duplicate.'),
('00000000-0000-4000-8000-007000000004', '00000000-0000-4000-8000-002000000007', '00000000-0000-4000-8000-000100000001', 'UNREADABLE_DOCUMENT', 'high', 'open', 'Receipt image is too blurred or faded for reliable extraction.', 'Upload a clearer image or enter the visible fields manually.'),
('00000000-0000-4000-8000-007000000005', '00000000-0000-4000-8000-002000000008', '00000000-0000-4000-8000-000100000001', 'MISSING_INVOICE_NUMBER', 'high', 'open', 'Sales invoice number is missing.', 'Check the original invoice and enter its number.'),
('00000000-0000-4000-8000-007000000006', '00000000-0000-4000-8000-002000000010', '00000000-0000-4000-8000-000100000001', 'AI_TIMEOUT', 'high', 'open', 'Extraction service did not respond before the timeout.', 'Retry processing; the original file remains stored.'),
('00000000-0000-4000-8000-007000000007', '00000000-0000-4000-8000-002000000011', '00000000-0000-4000-8000-000100000001', 'SUPPLIER_MATCH_UNCERTAIN', 'medium', 'open', 'Supplier could not be matched confidently.', 'Choose an existing supplier or create a verified supplier record.'),
('00000000-0000-4000-8000-007000000008', '00000000-0000-4000-8000-002000000012', '00000000-0000-4000-8000-000100000001', 'APPROVAL_REQUIRED', 'medium', 'open', 'This document has not been approved for the current content revision.', 'Submit for authorised review; do not finalise until approved.'),
('00000000-0000-4000-8000-007000000009', '00000000-0000-4000-8000-002000000015', '00000000-0000-4000-8000-000100000001', 'DATE_UNREADABLE', 'medium', 'open', 'Transaction date could not be read confidently.', 'Verify the date from the receipt.'),
('00000000-0000-4000-8000-007000000010', '00000000-0000-4000-8000-002000000017', '00000000-0000-4000-8000-000100000001', 'CUSTOMER_MATCH_UNCERTAIN', 'medium', 'open', 'Customer record matching requires confirmation.', 'Select the correct customer from the directory.'),
('00000000-0000-4000-8000-007000000011', '00000000-0000-4000-8000-002000000018', '00000000-0000-4000-8000-000100000001', 'TOTAL_MISMATCH', 'high', 'open', 'Document total differs from the sum of extracted line items.', 'Compare the original document and correct the total or line items.'),
('00000000-0000-4000-8000-007000000012', '00000000-0000-4000-8000-002000000019', '00000000-0000-4000-8000-000100000001', 'MISSING_DUE_DATE', 'medium', 'open', 'No due date was detected on the bill.', 'Verify whether the source contains a due date; leave blank if not provided.');

INSERT INTO standardised_records (id, document_id, revision_number, tenant_id, category, invoice_number, currency, total_amount, validation_status, validated_at, snapshot, created_by, is_current) VALUES

('00000000-0000-4000-8000-008000000001', '00000000-0000-4000-8000-002000000001', 1, '00000000-0000-4000-8000-000100000001', 'accounts_receivable', 'INV-2026-1001', 'MYR', 1250, 'passed', now(), '{"fixture_id":"DEMO-DOC-0001"}'::jsonb, '00000000-0000-4000-8000-001000000002', TRUE);

INSERT INTO audit_logs (tenant_id, user_id, user_name, role, action, resource, details) VALUES

('00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000002', 'Demo Local Admin', 'local_admin', 'MOCK_FIXTURES_LOADED', 'TENANT_DEMO_001', 'Loaded supplied frontend fixtures');

INSERT INTO audit_logs (tenant_id, user_id, user_name, role, action, resource, details) VALUES

('00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'Demo Accountant', 'accountant', 'DASHBOARD_OPENED', 'TENANT_DEMO_001', 'Accountant dashboard demo activity'),
('00000000-0000-4000-8000-000100000001', '00000000-0000-4000-8000-001000000001', 'Demo Accountant', 'accountant', 'DOCUMENT_PROCESSING_STARTED', 'BILL_100016', 'Demo extraction job is processing');

-- ---------------------------------------------------------------------------
-- Company onboarding and administration workflows
-- ---------------------------------------------------------------------------
INSERT INTO company_requests
    (id, company_name, registration_number, contact_name, contact_email, phone,
     requested_accounts, employee_emails, notes, status, tenant_id, reviewed_by, reviewed_at)
VALUES
('00000000-0000-4000-8000-010000000001','Pending Demo Holdings','DEMO-REG-002','Nadia Demo','nadia@pending.example','+60123456789',5,
 ' ["nadia@pending.example","staff@pending.example"]'::jsonb,'Pending access request for QA','pending',NULL,NULL,NULL),
('00000000-0000-4000-8000-010000000002','Approved Demo Branch','DEMO-REG-003','Demo Local Admin','reviewer.demo@saic.example',NULL,3,
 '["reviewer.demo@saic.example"]'::jsonb,'Approved fixture request','approved','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-001000000003',now());

INSERT INTO account_limit_requests
    (id, tenant_id, current_limit, requested_limit, reason, status, review_reason, reviewed_by, reviewed_at)
VALUES
('00000000-0000-4000-8000-011000000001','00000000-0000-4000-8000-000100000001',25,40,'Seasonal document volume','pending',NULL,NULL,NULL);

INSERT INTO auth_tokens (id, user_id, purpose, token_hash, expires_at)
VALUES
('00000000-0000-4000-8000-012000000001','00000000-0000-4000-8000-001000000001','activation','demo-activation-hash-001',now()+interval '7 days'),
('00000000-0000-4000-8000-012000000002','00000000-0000-4000-8000-001000000002','password_reset','demo-reset-hash-001',now()+interval '1 hour');

-- ---------------------------------------------------------------------------
-- Correction history, entity matching, and record amendment examples
-- ---------------------------------------------------------------------------
INSERT INTO field_corrections
    (id, field_prediction_id, old_value, new_value, corrected_by)
SELECT '00000000-0000-4000-8000-013000000001', id, '1250.00', '1250.00',
       '00000000-0000-4000-8000-001000000001'
FROM field_predictions
WHERE extraction_id='00000000-0000-4000-8000-005000000001' AND field_key='total';

INSERT INTO line_item_corrections
    (id, line_item_id, document_id, field_key, old_value, new_value, corrected_by)
SELECT '00000000-0000-4000-8000-014000000001', id, document_id, 'amount', '1250.00', '1250.00',
       '00000000-0000-4000-8000-001000000001'
FROM line_items
WHERE extraction_id='00000000-0000-4000-8000-005000000001' AND line_number=1;

INSERT INTO customer_matches
    (id, tenant_id, document_id, customer_id, candidate_name, confidence, source, decision, decided_by, decided_at)
VALUES
('00000000-0000-4000-8000-015000000001','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-002000000017',
 '00000000-0000-4000-8000-004000000003','Metro Retail Sdn Bhd',0.620,'ai','pending',NULL,NULL);

INSERT INTO record_amendments
    (id, original_record_id, document_id, tenant_id, requested_by, reason, status)
VALUES
('00000000-0000-4000-8000-016000000001','00000000-0000-4000-8000-008000000001','00000000-0000-4000-8000-002000000001',
 '00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-001000000001','Demo amendment request for corrected reference','requested');

INSERT INTO custom_fields (id, tenant_id, field_key, label, field_type, options, required)
VALUES
('00000000-0000-4000-8000-017000000001','00000000-0000-4000-8000-000100000001','cost_centre','Cost Centre','text','[]'::jsonb,FALSE),
('00000000-0000-4000-8000-017000000002','00000000-0000-4000-8000-000100000001','payment_status','Payment Status','select','["unpaid","paid","disputed"]'::jsonb,FALSE);

INSERT INTO document_custom_values (document_id, field_id, value, updated_by)
VALUES
('00000000-0000-4000-8000-002000000001','00000000-0000-4000-8000-017000000001','"CC-100"'::jsonb,'00000000-0000-4000-8000-001000000001'),
('00000000-0000-4000-8000-002000000001','00000000-0000-4000-8000-017000000002','"unpaid"'::jsonb,'00000000-0000-4000-8000-001000000001');

-- ---------------------------------------------------------------------------
-- Reconciliation, comments, contacts, tags, filters, reminders, and versions
-- ---------------------------------------------------------------------------
INSERT INTO document_reconciliations
    (id, tenant_id, source_document_id, related_document_id, relation_type, amount, note, created_by)
VALUES
('00000000-0000-4000-8000-018000000001','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-002000000013','00000000-0000-4000-8000-002000000001','credit_for',200,'Demo partial credit note','00000000-0000-4000-8000-001000000001'),
('00000000-0000-4000-8000-018000000002','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-002000000014','00000000-0000-4000-8000-002000000010','debit_for',100,'Demo extra supplier charge','00000000-0000-4000-8000-001000000001');

INSERT INTO supplier_contacts (id, tenant_id, vendor_id, contact_name, email, phone, role, notes)
VALUES
('00000000-0000-4000-8000-019000000001','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-003000000001','Aina Supplies','aina@borneo.example','+6082555001','Accounts','Demo supplier contact'),
('00000000-0000-4000-8000-019000000002','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-003000000002','Daniel Tech','daniel@sarawaktech.example','+6082555002','Finance','Demo supplier contact');

INSERT INTO document_tags (id, tenant_id, name, color)
VALUES
('00000000-0000-4000-8000-020000000001','00000000-0000-4000-8000-000100000001','Urgent','#dc2626'),
('00000000-0000-4000-8000-020000000002','00000000-0000-4000-8000-000100000001','Month End','#2563eb');

INSERT INTO document_tag_links (document_id, tag_id, created_by)
VALUES
('00000000-0000-4000-8000-002000000002','00000000-0000-4000-8000-020000000001','00000000-0000-4000-8000-001000000001'),
('00000000-0000-4000-8000-002000000001','00000000-0000-4000-8000-020000000002','00000000-0000-4000-8000-001000000001');

INSERT INTO saved_filters (id, tenant_id, created_by, name, resource, definition, is_shared)
VALUES
('00000000-0000-4000-8000-021000000001','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-001000000001','Open high-priority exceptions','exceptions','{"status":"open","severity":"high"}'::jsonb,TRUE),
('00000000-0000-4000-8000-021000000002','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-001000000002','October invoices','documents','{"kind":"invoice","month":"2026-10"}'::jsonb,FALSE);

INSERT INTO document_comments (id, document_id, tenant_id, author_id, comment, page_number, field_key)
VALUES
('00000000-0000-4000-8000-022000000001','00000000-0000-4000-8000-002000000002','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-001000000002','Please compare the total with the line items.',1,'total'),
('00000000-0000-4000-8000-022000000002','00000000-0000-4000-8000-002000000004','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-001000000001','Confirm the low-confidence invoice amount.',1,'total');

INSERT INTO review_reminders (id, tenant_id, document_id, assigned_to, remind_at, created_by)
VALUES
('00000000-0000-4000-8000-023000000001','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-002000000004','00000000-0000-4000-8000-001000000002',now()+interval '1 day','00000000-0000-4000-8000-001000000001');

INSERT INTO export_history (id, tenant_id, requested_by, resource, filters, format, row_count, status, completed_at)
VALUES
('00000000-0000-4000-8000-024000000001','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-001000000001','records','{"category":"accounts_receivable"}'::jsonb,'csv',1,'completed',now()),
('00000000-0000-4000-8000-024000000002','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-001000000002','exceptions','{}'::jsonb,'xlsx',12,'queued',NULL);

INSERT INTO document_versions (id, document_id, tenant_id, version_number, storage_path, file_hash_sha256, file_size_bytes, created_by)
VALUES
('00000000-0000-4000-8000-025000000001','00000000-0000-4000-8000-002000000001','00000000-0000-4000-8000-000100000001',1,'mock/versions/invoice_001_v1.pdf','sha256-DEMO-DOC-0001-V1',102400,'00000000-0000-4000-8000-001000000001'),
('00000000-0000-4000-8000-025000000002','00000000-0000-4000-8000-002000000001','00000000-0000-4000-8000-000100000001',2,'mock/versions/invoice_001_v2.pdf','sha256-DEMO-DOC-0001-V2',103000,'00000000-0000-4000-8000-001000000001');

-- ---------------------------------------------------------------------------
-- Batch processing and team workload examples
-- ---------------------------------------------------------------------------
INSERT INTO document_batches (id, tenant_id, created_by, name, status, total_documents, processed_documents, failed_documents, started_at)
VALUES
('00000000-0000-4000-8000-026000000001','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-001000000002','October month-end batch','review',3,2,1,now()-interval '2 hours');

INSERT INTO batch_documents (batch_id, document_id, tenant_id, position, status, error)
VALUES
('00000000-0000-4000-8000-026000000001','00000000-0000-4000-8000-002000000001','00000000-0000-4000-8000-000100000001',1,'completed',NULL),
('00000000-0000-4000-8000-026000000001','00000000-0000-4000-8000-002000000002','00000000-0000-4000-8000-000100000001',2,'review',NULL),
('00000000-0000-4000-8000-026000000001','00000000-0000-4000-8000-002000000010','00000000-0000-4000-8000-000100000001',3,'failed','AI_TIMEOUT');

INSERT INTO workload_assignments (id, tenant_id, document_id, assigned_to, assigned_by, priority, due_at)
VALUES
('00000000-0000-4000-8000-027000000001','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-002000000002','00000000-0000-4000-8000-001000000002','00000000-0000-4000-8000-001000000002','urgent',now()+interval '1 day'),
('00000000-0000-4000-8000-027000000002','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-002000000004','00000000-0000-4000-8000-001000000002','00000000-0000-4000-8000-001000000002','high',now()+interval '2 days');

-- ---------------------------------------------------------------------------
-- Authentication challenge and recovery examples
-- Values are hashes/placeholders only.
-- ---------------------------------------------------------------------------
INSERT INTO web_sessions (token_hash, user_id, expires_at)
VALUES ('demo-session-token-hash','00000000-0000-4000-8000-001000000001',now()+interval '8 hours');
INSERT INTO password_challenges (email, user_id, code_hash, expires_at)
VALUES ('accountant.demo@saic.example','00000000-0000-4000-8000-001000000001','demo-code-hash',now()+interval '10 minutes');
INSERT INTO auth_rate_limits (key, window_start, hits) VALUES ('demo:accountant.login',now(),2);
INSERT INTO auth_challenges (challenge_hash, user_id, email_code_hash, method, expires_at, attempts)
VALUES ('demo-auth-challenge','00000000-0000-4000-8000-001000000001','demo-email-code-hash','email',now()+interval '10 minutes',0);
INSERT INTO mfa_recovery_requests (id, requester_id, target_user_id, status, reason)
VALUES ('00000000-0000-4000-8000-028000000001','00000000-0000-4000-8000-001000000002','00000000-0000-4000-8000-001000000001','pending','Demo recovery request for testing approval workflow');

-- ---------------------------------------------------------------------------
-- Permission overrides and platform operations
-- ---------------------------------------------------------------------------
INSERT INTO user_permissions (user_id, permission_key, granted, granted_by)
VALUES
('00000000-0000-4000-8000-001000000001','documents.comment',TRUE,'00000000-0000-4000-8000-001000000002'),
('00000000-0000-4000-8000-001000000001','documents.approve',FALSE,'00000000-0000-4000-8000-001000000002');

INSERT INTO platform_notifications (id, notification_type, severity, title, message, created_by)
VALUES
('00000000-0000-4000-8000-029000000001','service_health','warning','AI queue latency elevated','Demo processing queue contains a delayed job.','00000000-0000-4000-8000-001000000003');

INSERT INTO backup_runs (id, requested_by, backup_type, status, storage_ref, checksum, started_at, completed_at)
VALUES
('00000000-0000-4000-8000-030000000001','00000000-0000-4000-8000-001000000003','database','completed','demo/backups/database-2026-10-18.sql','demo-sha256-checksum',now()-interval '1 day',now()-interval '1 day'+interval '3 minutes');

INSERT INTO supplier_matches (id, tenant_id, document_id, vendor_id, candidate_name, confidence, source, decision)
VALUES
('00000000-0000-4000-8000-031000000001','00000000-0000-4000-8000-000100000001','00000000-0000-4000-8000-002000000011','00000000-0000-4000-8000-003000000001','Borneo Office Supplies Sdn Bhd',0.620,'ai','pending');

COMMIT;
