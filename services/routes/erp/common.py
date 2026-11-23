"""Shared authentication, formatting and audit helpers for ERP routes."""

import json
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from main import HTTPException, Request, _uuid_or_422, require_workspace_user

DOCUMENT_TYPES = frozenset({
    "quotation", "sales-order", "delivery-order", "invoice", "credit-note", "debit-note", "payment",
    "refund", "purchase-order", "goods-received", "bill", "purchase-credit-note", "purchase-debit-note",
    "purchase-payment", "purchase-refund", "money-in", "money-out", "bank-transfer",
    "stock-adjustment", "stock-transfer", "journal-entry", "contra", "deemed-payment",
    "store-order", "minipos-order",
})

ERP_ROLES = frozenset({"local_admin", "accountant"})


def erp_user(request: Request, write_roles=None):
    user = require_workspace_user(request)
    if not user.get("tenant_id"):
        raise HTTPException(403, "This operation requires a company workspace.")
    roles = ERP_ROLES if write_roles is None else write_roles
    if user["role"] not in roles:
        raise HTTPException(403, "You do not have permission for this workspace.")
    return user


def parse_uuid(value, label: str) -> UUID:
    return value if isinstance(value, UUID) else _uuid_or_422(value, label)


def money(value: Decimal | int | float | str | None) -> str:
    amount = Decimal(str(value)) if value is not None else Decimal(0)
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def audit(db, user, action: str, resource, details: dict):
    db.execute(
        """INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (user["tenant_id"], user["id"], user["name"], user["role"],
         action, str(resource), json.dumps(details)),
    )
