"""CSV imports for contacts, chart of accounts and opening balances (plan v2, task A2).

Same steps as the bank statement import: upload a file and read the preview (a result for
every row), then confirm with the same file and the preview token. Previews are not stored,
so a preview nobody confirms needs no discard step. Only a local administrator can import,
as only a local administrator can create suppliers, customers, accounts and journals today.
"""
from __future__ import annotations
from main import *
from main import _uuid_or_422
import hashlib
import json
from pathlib import Path
from fastapi.concurrency import run_in_threadpool
from services.imports import chart, contacts, opening
from services.imports.common import ImportFileError, MAX_IMPORT_BYTES, preview_token, template_csv
from services.routes.accounting.routes import JournalInput, record_journal

IMPORTERS = {"contacts": contacts, "chart_of_accounts": chart, "opening_balances": opening}
LABELS = {"contacts": "contacts", "chart_of_accounts": "chart of accounts", "opening_balances": "opening balances"}
IMPORT_ROLES = {"local_admin"}


async def _upload(request: Request):
    user = await run_in_threadpool(require_workspace_user, request, IMPORT_ROLES)
    if not user.get("tenant_id"):
        raise HTTPException(403, "A company workspace is required.")
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise HTTPException(422, "Upload the CSV file as a form with a 'file' field.")
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "Choose a CSV file.")
    if Path(str(getattr(upload, "filename", "") or "")).suffix.lower() != ".csv":
        raise HTTPException(422, "Imports take CSV files. In Excel use Save As, CSV UTF-8.")
    content = await upload.read(MAX_IMPORT_BYTES + 1)
    return user, content, str(form.get("previewToken") or "").strip()


def _preview(kind, user, content):
    try:
        with connect() as db:
            return IMPORTERS[kind].preview(db, user["tenant_id"], content)
    except ImportFileError as error:
        raise HTTPException(422, str(error))


def _audit(db, user, action, resource, details):
    db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
        VALUES (%s,%s,%s,%s,%s,%s,%s)""", (user["tenant_id"], user["id"], user.get("name", "Workspace user"),
        user["role"], action, str(resource), json.dumps(details, default=str)))


def _refuse(body):
    if body["fileErrors"] or body["errorCount"]:
        raise HTTPException(422, {"message": "Fix the rows marked as errors, then preview the file again.", "preview": body})
    raise HTTPException(409, {"message": "Nothing new to import: every row is already in the company.", "preview": body})


def _confirm(kind, user, content, token):
    if not token or token != preview_token(kind, user["tenant_id"], content):
        raise HTTPException(409, "The file is not the one that was previewed. Preview it again before confirming.")
    try:
        with connect() as db:
            # One import of a kind at a time per company, so two confirms cannot both add the same rows.
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"accordai.import:{kind}:{user['tenant_id']}",))
            if kind == "opening_balances":
                body, journal = opening.preview_plan(db, user["tenant_id"], content)
                if not body["canConfirm"] or not journal:
                    _refuse(body)
                posted = record_journal(db, user, JournalInput(**journal))
                result = {"journalId": posted["id"], "journalStatus": posted["status"], "lineCount": len(journal["lines"]),
                          "totalDebit": body["journal"]["totalDebit"], "totalCredit": body["journal"]["totalCredit"]}
                resource = posted["id"]
            else:
                body = IMPORTERS[kind].preview(db, user["tenant_id"], content)
                if not body["canConfirm"]:
                    _refuse(body)
                result = IMPORTERS[kind].apply(db, user, body["rows"])
                resource = kind
            details = {"rows": body["rowCount"], "imported": body["newCount"], "skippedDuplicates": body["duplicateCount"],
                       "sourceHash": hashlib.sha256(content).hexdigest(), **result}
            _audit(db, user, f"{kind}.import_confirmed", resource, details)
    except ImportFileError as error:
        raise HTTPException(422, str(error))
    skipped = [{"sourceRow": row["sourceRow"], "duplicateOf": row["duplicateOf"], "warnings": row["warnings"]}
               for row in body["rows"] if row["status"] == "duplicate"]
    return {"kind": kind, "status": "confirmed", "rowCount": body["rowCount"], "imported": body["newCount"],
            "skippedDuplicates": skipped, **result}


def _template(kind):
    def download(request: Request):
        require_workspace_user(request, {"super_admin", "local_admin", "accountant"})
        filename = kind.replace("_", "-") + "-template.csv"
        return Response(template_csv(IMPORTERS[kind].COLUMNS), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    download.__name__ = f"download_{kind}_template"
    return download


def _preview_route(kind):
    async def preview_import(request: Request):
        user, content, _ = await _upload(request)
        return await run_in_threadpool(_preview, kind, user, content)
    preview_import.__name__ = f"preview_{kind}_import"
    return preview_import


def _confirm_route(kind):
    async def confirm_import(request: Request):
        user, content, token = await _upload(request)
        return await run_in_threadpool(_confirm, kind, user, content, token)
    confirm_import.__name__ = f"confirm_{kind}_import"
    return confirm_import


PATHS = {"contacts": "/api/contacts/import", "chart_of_accounts": "/api/chart-of-accounts/import",
         "opening_balances": "/api/opening-balances/import"}


def register(router, handlers):
    for kind, path in PATHS.items():
        router.add_api_route(path + "/template", _template(kind), methods=["GET"])
        router.add_api_route(path, _preview_route(kind), methods=["POST"])
        router.add_api_route(path + "/confirm", _confirm_route(kind), methods=["POST"], status_code=201)
