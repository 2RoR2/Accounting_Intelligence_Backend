"""Proposed shared READ policy only. Not installed in either application router."""


def shared_read_allowed(user, document, demo_registration, demo_tenant_enabled):
    return bool(user.get('status') == 'active'
        and user.get('role') in {'accountant', 'local_admin'}
        and user.get('tenant_id') is not None
        and str(user['tenant_id']) == str(document.get('tenant_id'))
        and demo_registration is not None
        and str(demo_registration.get('tenant_id')) == str(user['tenant_id'])
        and str(demo_registration.get('document_id')) == str(document.get('id'))
        and demo_tenant_enabled is True)


def shared_read_predicate(user):
    """Append as an OR inside existing owner/assigned clause, within tenant AND.

    Caller must use authenticated session user and bind returned parameters.
    Never use on correction, approval, assignment, retry or standardisation writes.
    """
    if user.get('status') != 'active' or user.get('role') not in {'accountant', 'local_admin'} or not user.get('tenant_id'):
        return 'FALSE', ()
    return '''EXISTS (SELECT 1 FROM benchmark_demo_documents b
        JOIN benchmark_demo_tenants t ON t.tenant_id=b.tenant_id AND t.enabled=true
        WHERE b.tenant_id=%s AND b.tenant_id=d.tenant_id AND b.document_id=d.id)''', (str(user['tenant_id']),)
