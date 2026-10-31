from __future__ import annotations
from main import *
from main import _lock_upload_identity, _processing_error, _lock_workspace_document, _require_current_extraction, _authorised_document, _bank_row_values, _bank_transaction_dto, _uuid_or_422, _account_dto, _cost_centre_dto

def list_chart_of_accounts(request: Request, includeInactive: bool = False):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("""SELECT * FROM coa_accounts
            WHERE tenant_id=%s AND (%s OR is_active) ORDER BY code""",
            (user["tenant_id"], includeInactive)).fetchall()
    return [_account_dto(row) for row in rows]

def create_chart_account(data: AccountInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    parent_id = _uuid_or_422(data.parentId, "parent account id") if data.parentId else None
    with connect() as db:
        row = db.execute("""INSERT INTO coa_accounts(tenant_id,code,name,account_class,parent_id,created_by)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
            (user["tenant_id"], data.code.strip(), data.name.strip(), data.accountClass, parent_id, user["id"])).fetchone()
    return _account_dto(row)

def update_chart_account(account_id: str, data: AccountInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    account_id = _uuid_or_422(account_id, "account id")
    parent_id = _uuid_or_422(data.parentId, "parent account id") if data.parentId else None
    with connect() as db:
        row = db.execute("""UPDATE coa_accounts SET code=%s,name=%s,account_class=%s,parent_id=%s
            WHERE id=%s AND tenant_id=%s RETURNING *""",
            (data.code.strip(), data.name.strip(), data.accountClass, parent_id, account_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Account not found.")
    return _account_dto(row)

def deactivate_chart_account(account_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    account_id = _uuid_or_422(account_id, "account id")
    with connect() as db:
        row = db.execute("UPDATE coa_accounts SET is_active=FALSE WHERE id=%s AND tenant_id=%s RETURNING id", (account_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Account not found.")
    return {"id": str(row["id"]), "isActive": False}

def list_cost_centres(request: Request, includeInactive: bool = False):
    user = require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
    with connect() as db:
        rows = db.execute("SELECT * FROM cost_centres WHERE tenant_id=%s AND (%s OR is_active) ORDER BY code", (user["tenant_id"], includeInactive)).fetchall()
    return [_cost_centre_dto(row) for row in rows]

def create_cost_centre(data: CostCentreInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    with connect() as db:
        row = db.execute("""INSERT INTO cost_centres(tenant_id,code,name,created_by)
            VALUES (%s,%s,%s,%s) RETURNING *""", (user["tenant_id"], data.code.strip(), data.name.strip(), user["id"])).fetchone()
    return _cost_centre_dto(row)

def update_cost_centre(cost_centre_id: str, data: CostCentreInput, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    cost_centre_id = _uuid_or_422(cost_centre_id, "cost centre id")
    with connect() as db:
        row = db.execute("UPDATE cost_centres SET code=%s,name=%s WHERE id=%s AND tenant_id=%s RETURNING *", (data.code.strip(), data.name.strip(), cost_centre_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Cost centre not found.")
    return _cost_centre_dto(row)

def deactivate_cost_centre(cost_centre_id: str, request: Request):
    user = require_workspace_user(request, {"local_admin"})
    cost_centre_id = _uuid_or_422(cost_centre_id, "cost centre id")
    with connect() as db:
        row = db.execute("UPDATE cost_centres SET is_active=FALSE WHERE id=%s AND tenant_id=%s RETURNING id", (cost_centre_id, user["tenant_id"])).fetchone()
    if not row:
        raise HTTPException(404, "Cost centre not found.")
    return {"id": str(row["id"]), "isActive": False}
"""Accounting API route registrations."""

def register(router, handlers):
    router.add_api_route('/api/chart-of-accounts', list_chart_of_accounts, methods=['GET'])
    router.add_api_route('/api/chart-of-accounts', create_chart_account, methods=['POST'], status_code=201)
    router.add_api_route('/api/chart-of-accounts/{account_id}', update_chart_account, methods=['PATCH'])
    router.add_api_route('/api/chart-of-accounts/{account_id}', deactivate_chart_account, methods=['DELETE'])
    router.add_api_route('/api/cost-centres', list_cost_centres, methods=['GET'])
    router.add_api_route('/api/cost-centres', create_cost_centre, methods=['POST'], status_code=201)
    router.add_api_route('/api/cost-centres/{cost_centre_id}', update_cost_centre, methods=['PATCH'])
    router.add_api_route('/api/cost-centres/{cost_centre_id}', deactivate_cost_centre, methods=['DELETE'])
