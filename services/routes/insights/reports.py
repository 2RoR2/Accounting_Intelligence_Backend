"""Insights reports (F4): chart data and month-end commentary, read-only and scoped to the signed-in workspace.

``GET /api/insights/overview`` is deterministic and works with ``AI_ASSIST`` off. ``GET /api/insights/commentary``
needs ``AI_ASSIST=on`` for AI wording; otherwise (or when the provider fails) it answers with a labelled template.
The contract is in docs/api/insights-reports.md.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import Request
from fastapi.encoders import jsonable_encoder

from main import connect, require_workspace_user
from services.insights import commentary, overview
from services.routes.erp.common import money
from services.routes.home.lanes import company_today
from services.routes.reporting import business_summary

ROLES = {"local_admin", "accountant"}


def _encode(body):
    return jsonable_encoder(body, custom_encoder={Decimal: money})


def _read_period(db, tenant, period):
    """The month's first day and as-of date from one snapshot; the current month runs to today."""
    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    today = company_today(db, tenant)
    return business_summary._month_bounds(period or None, today)


def insights_overview(request: Request, period: str | None = None):
    tenant = require_workspace_user(request, ROLES)["tenant_id"]
    with connect() as db:
        start, end = _read_period(db, tenant, period)
        body = overview.build_overview(db, tenant, start, end)
    return _encode(body)


def insights_commentary(request: Request, period: str | None = None):
    user = require_workspace_user(request, ROLES)
    tenant = user["tenant_id"]
    with connect() as db:
        start, end = _read_period(db, tenant, period)
        table = commentary.build_facts(db, tenant, start, end)
    # The snapshot is closed before any model call, so a slow provider never holds a database connection.
    body = commentary.resolve_commentary(table, tenant=tenant, period=f"{start}:{end}", user=user)
    meta = {"period": f"{start:%Y-%m}", "fromDate": start, "toDate": end, "asOf": end}
    return _encode({**meta, **body})


def register(router):
    router.add_api_route("/api/insights/overview", insights_overview, methods=["GET"], tags=["Insights"])
    router.add_api_route("/api/insights/commentary", insights_commentary, methods=["GET"], tags=["Insights"])
