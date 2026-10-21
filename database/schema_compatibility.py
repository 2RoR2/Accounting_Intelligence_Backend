"""Read-only runtime prerequisites and explicit administrative upgrades.

Startup never invokes apply_migrations. Catalog checks accept a structurally
compatible consolidated bootstrap without requiring a synthetic ledger entry.
"""
from pathlib import Path

REQUIRED_COLUMNS = {
    'documents': {'id': 'uuid', 'tenant_id': 'uuid', 'file_hash_sha256': 'text', 'content_revision': 'int4'},
    'review_submissions': {'document_id': 'uuid', 'tenant_id': 'uuid', 'content_revision': 'int4'},
    'review_decisions': {'approved_content_revision': 'int4'},
    'pending_jobs': {'document_id': 'uuid', 'tenant_id': 'uuid', 'dispatch_token': 'uuid',
                     'claim_token': 'uuid', 'dispatch_expires_at': 'timestamptz',
                     'processing_expires_at': 'timestamptz', 'attempt_count': 'int4'},
    'raw_extractions': {'document_id': 'uuid', 'pending_job_id': 'uuid', 'is_current': 'bool'},
    'field_predictions': {'extraction_id': 'uuid', 'raw_value': 'text', 'current_value': 'text'},
    'line_item_corrections': {'document_id': 'uuid'},
    'standardised_records': {'document_id': 'uuid', 'is_current': 'bool'},
}
REQUIRED_INDEXES = {
    'uq_pending_jobs_open_document': 'pending_jobs',
    'uq_raw_extractions_current': 'raw_extractions',
    'uq_standardised_records_current': 'standardised_records',
}


class SchemaCompatibilityError(RuntimeError):
    """Safe operator-facing error; never includes a URL, password or row data."""


def check_runtime_schema(db):
    columns=db.execute("""SELECT c.relname AS table_name,a.attname AS column_name,
        t.typname AS type_name,a.attnotnull AS not_null,
        pg_get_expr(d.adbin,d.adrelid) AS default_expr
        FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_type t ON t.oid=a.atttypid
        LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE n.nspname='public' AND c.relkind IN ('r','p')
        AND c.relname=ANY(%s) AND a.attnum>0 AND NOT a.attisdropped""",
        (list(REQUIRED_COLUMNS),)).fetchall()
    actual={(r['table_name'],r['column_name']):r for r in columns}
    problems=[]
    for table,fields in REQUIRED_COLUMNS.items():
        for field,expected in fields.items():
            row=actual.get((table,field))
            if not row or row['type_name']!=expected:
                problems.append(table+'.'+field)
    for table in ('documents','review_submissions'):
        row=actual.get((table,'content_revision'))
        if row and (not row['not_null'] or row['default_expr'] not in ('1','1::integer')):
            problems.append(table+'.content_revision requires NOT NULL DEFAULT 1')
    indexes=db.execute("""SELECT idx.relname AS name,tbl.relname AS table_name,
        i.indisunique,i.indisvalid,i.indisready,pg_get_indexdef(i.indexrelid) AS definition,
        pg_get_expr(i.indpred,i.indrelid) AS predicate
        FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid
        JOIN pg_class tbl ON tbl.oid=i.indrelid JOIN pg_namespace n ON n.oid=tbl.relnamespace
        WHERE n.nspname='public' AND idx.relname=ANY(%s)""",
        (list(REQUIRED_INDEXES),)).fetchall()
    actual_indexes={r['name']:r for r in indexes}
    for name,table in REQUIRED_INDEXES.items():
        row=actual_indexes.get(name)
        if (not row or row['table_name']!=table or not all(row[k] for k in ('indisunique','indisvalid','indisready'))
                or '(document_id)' not in row['definition'] or not row['predicate']):
            problems.append(name)
        elif table=='pending_jobs':
            # All open states must be covered; a pending-only legacy index is insufficient.
            import re
            if set(re.findall(r"'([^']+)'",row['predicate']))!={'pending','dispatched','processing'}:
                problems.append(name+' requires all open states')
        elif row['predicate'].strip()!='is_current':
            problems.append(name+' requires is_current')
    functions=db.execute("""SELECT
        pg_get_function_result(to_regprocedure('public.switch_current_extraction(uuid)')) AS switch_result,
        has_function_privilege(current_user,'pg_catalog.pg_advisory_xact_lock(bigint)','EXECUTE') AS upload_lock""").fetchone()
    if not functions or functions['switch_result']!='integer' or not functions['upload_lock']:
        problems.append('extraction switch / upload transaction lock')
    if problems:
        raise SchemaCompatibilityError(
            'DATABASE_SCHEMA_INCOMPATIBLE: '+', '.join(problems)+
            '. An authorized owner must review and apply manage.py migrate before startup. '
            '004_review_submission_revision.sql covers the review revision column only; '
            'other missing integration prerequisites require a reviewed baseline upgrade.')


def apply_migrations(db,schema_path: Path,migrations_path: Path):
    """Privileged manage.py only; caller owns the transaction and target approval."""
    applied=[]
    exists=db.execute("SELECT to_regclass('public.tenants') AS table_name").fetchone()['table_name']
    if exists is None:
        occupied=db.execute("""SELECT EXISTS (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
            UNION ALL SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
        ) AS occupied""").fetchone()['occupied']
        if occupied:
            raise SchemaCompatibilityError('Refusing consolidated bootstrap on a nonempty database.')
        db.execute(schema_path.read_text(encoding='utf-8-sig'))
        applied.append('consolidated schema')
    db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    for migration in sorted(migrations_path.glob('*.sql')):
        if not db.execute('SELECT 1 FROM schema_migrations WHERE name=%s',(migration.name,)).fetchone():
            db.execute(migration.read_text(encoding='utf-8-sig'))
            db.execute('INSERT INTO schema_migrations(name) VALUES (%s)',(migration.name,))
            applied.append(migration.name)
    # Failure rolls back SQL and ledger together in manage.py's transaction.
    check_runtime_schema(db)
    return applied
