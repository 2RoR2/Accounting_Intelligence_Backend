"""Document checks: advisory findings on a typed sales document or supplier bill before it is saved, sent or issued.

POST /api/erp/documents/{type}/checks takes the draft as the form holds it (the shape of preview/create, read leniently
so a half-typed line never fails the request) plus an optional saved document id, and returns findings:
    {code, severity: block|warn|info, field, lineIndex?, message, howToFix, suggestion?}

The system flags; the user decides. Nothing here saves, sends or changes a document, and a finding never blocks by
itself: "block" marks only what the save/issue endpoints (or the form) already refuse. Tax is never calculated: the
typed tax amounts are added up as they are, exactly as the totals are (posting.line_net + typed tax).

The rules are pure functions of the draft and a context (contact, company profile, catalog prices, source document,
duplicates) so they are tested without a database; check_document() gathers that context read-only.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from main import *  # noqa: F401,F403  (Request, HTTPException, connect)
from pydantic import BaseModel, Field

from . import posting
from .common import erp_user, money, parse_uuid

# Typed documents the engine checks: sales documents the company issues, and the supplier documents it types in.
SALES_CHECKED = {"quotation", "sales-order", "delivery-order", "invoice", "credit-note", "debit-note"}
PURCHASE_CHECKED = {"purchase-order", "bill", "purchase-credit-note", "purchase-debit-note"}
CHECKED_TYPES = SALES_CHECKED | PURCHASE_CHECKED
# The form already refuses to save these without a contact (REQUIRES_CONTACT in document-form.tsx).
REQUIRES_CONTACT = {"quotation", "invoice", "credit-note", "debit-note", "bill", "purchase-credit-note", "purchase-debit-note"}
# Open items carry payment terms and a due date.
OUTSTANDING = {"invoice", "bill", "debit-note", "purchase-debit-note"}
# What the company prints and sends: its own details and the customer's print on these.
PRINTED = {"quotation", "sales-order", "delivery-order", "invoice", "credit-note", "debit-note"}
# Where the other party's tax numbers matter (e-invoice buyer/supplier details).
TAX_ID_TYPES = {"invoice", "credit-note", "debit-note", "bill", "purchase-credit-note", "purchase-debit-note"}

PRICE_TOLERANCE = Decimal("0.10")  # a line more than 10% away from the catalog price is flagged
GENERIC_DESCRIPTIONS = {"item", "items", "misc", "misc.", "miscellaneous", "goods", "product", "products", "service",
                        "services", "sales", "sale", "stuff", "others", "other", "na", "n/a", "tbc", "tba", "-", "--",
                        ".", "...", "x", "test"}
MIN_DESCRIPTION = 4
CODE_ONLY = re.compile(r"^(?=.*\d)[A-Za-z0-9][A-Za-z0-9\-_/.#]*$")  # one token with a digit, e.g. "SKU-1042"


class CheckLine(BaseModel):
    """A draft line, read leniently: empty or half-typed numbers are allowed (the form is mid-edit)."""
    itemId: str | None = None
    description: str | None = Field(default=None, max_length=2000)
    quantity: Any = None
    unitPrice: Any = None
    discount: Any = None
    tax: Any = None
    taxCode: str | None = Field(default=None, max_length=80)
    accountId: str | None = None
    # The line amount as printed on the other party's copy (optional; a typed line's amount is computed).
    amount: Any = None


class CheckInput(BaseModel):
    number: str | None = Field(default=None, max_length=120)
    contactId: str | None = None
    contactName: str | None = Field(default=None, max_length=250)
    documentDate: str | None = None
    dueDate: str | None = None
    currency: str | None = Field(default="MYR", max_length=10)
    referenceNo: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    amount: Any = None
    bankAccountId: str | None = None
    lines: list[CheckLine] = Field(default_factory=list, max_length=500)
    allocations: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    fields: dict[str, Any] = Field(default_factory=dict)
    relatedDocumentId: str | None = None
    documentId: str | None = None


# ------------------------------------------------------------------ helpers

def _dec(value) -> Decimal | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _day(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def line_amount(line: dict) -> Decimal | None:
    """quantity x unit price - discount, rounded as the totals round it (posting.line_net). None if not typed yet."""
    quantity, price = _dec(line.get("quantity")), _dec(line.get("unitPrice"))
    if quantity is None or price is None:
        return None
    return posting.line_net({"quantity": quantity, "unitPrice": price, "discount": _dec(line.get("discount")) or 0})


def typed_tax(lines: list[dict]) -> Decimal:
    """The tax as typed on each line, added up. Never computed from a rate or a tax code."""
    return sum((posting.cents(_dec(l.get("tax")) or 0) for l in lines), Decimal("0"))


def _finding(code, severity, field, message, how_to_fix, *, line_index=None, suggestion=None) -> dict:
    result = {"code": code, "severity": severity, "field": field, "message": message, "howToFix": how_to_fix}
    if line_index is not None:
        result["lineIndex"] = line_index
    if suggestion is not None:
        result["suggestion"] = suggestion
    return result


def _suggest(field, value, label, *, line_index=None) -> dict:
    result = {"field": field, "value": value, "label": label}
    if line_index is not None:
        result["lineIndex"] = line_index
    return result


def _noun(document_type: str) -> str:
    return document_type.replace("-", " ")


def _party(document_type: str) -> str:
    return "customer" if document_type in SALES_CHECKED else "supplier"


def _blank(line: dict) -> bool:
    """An untouched line (the form drops it on save)."""
    return not (_text(line.get("itemId")) or _text(line.get("description")) or _dec(line.get("unitPrice"))
                or _dec(line.get("tax")))


def _price_text(value: Decimal) -> str:
    text = format(value.quantize(Decimal("0.0001")).normalize(), "f")
    whole, _, cents = text.partition(".")
    return f"{whole}.{(cents or '').ljust(2, '0')}"


def _quantity_text(value: Decimal) -> str:
    """A quantity as typed: 250, 2.5 (never padded to cents)."""
    return format(value.normalize(), "f")


def _same_name(a, b) -> bool:
    def norm(s):
        return re.sub(r"[^a-z0-9]", "", re.sub(r"sdn\.?\s*bhd\.?", "", (s or "").lower()))
    return bool(norm(a)) and norm(a) == norm(b)


# ------------------------------------------------------------------ rules (pure)

def missing_details(document_type: str, draft: dict, contact: dict | None, company: dict | None, today: date) -> list[dict]:
    found = []
    party = _party(document_type)
    fields = draft.get("fields") or {}
    if not draft.get("contactId"):
        required = document_type in REQUIRES_CONTACT
        found.append(_finding("CONTACT_MISSING", "block" if required else "warn", "contactId",
                              f"No {party} is chosen.",
                              f"Choose the {party} in the {party.capitalize()} field" + (" before saving." if required else ".")))
    elif contact:
        who = contact.get("name") or f"This {party}"
        if document_type in PRINTED and not _text(contact.get("address")):
            found.append(_finding("CONTACT_ADDRESS_MISSING", "warn", "contactId",
                                  f"{who} has no address; the {_noun(document_type)} prints without one.",
                                  f"Add the address on the {party}'s contact page, then run the checks again."))
        if document_type in TAX_ID_TYPES:
            if not _text(contact.get("tin")):
                found.append(_finding("CONTACT_TIN_MISSING", "warn", "contactId", f"{who} has no TIN.",
                                      f"Add the {party}'s TIN on their contact page (needed for e-invoicing)."))
            if not _text(contact.get("registrationNumber")):
                found.append(_finding("CONTACT_SSM_MISSING", "info", "contactId",
                                      f"{who} has no SSM / registration number.",
                                      f"Add it on the {party}'s contact page if they are a registered business."))
        if company and document_type in SALES_CHECKED and _same_name(contact.get("name"), company.get("name")):
            found.append(_finding("CONTACT_IS_COMPANY", "warn", "contactId", "The customer is your own company.",
                                  "Choose the customer this document is for."))
    if document_type in OUTSTANDING:
        if not _text(fields.get("paymentTerms")):
            found.append(_finding("PAYMENT_TERMS_MISSING", "warn", "paymentTerms", "No payment terms.",
                                  "Choose the payment terms; they set the due date and print on the document."))
        if not _day(draft.get("dueDate")):
            found.append(_finding("DUE_DATE_MISSING", "warn", "dueDate", "No due date.",
                                  "Choose payment terms or type the due date."))
    if document_type == "quotation":
        valid_until = _day(draft.get("dueDate"))
        document_date = _day(draft.get("documentDate"))
        if not valid_until:
            found.append(_finding("VALID_UNTIL_MISSING", "warn", "dueDate",
                                  "No valid-until date: the customer will not see how long the prices hold.",
                                  "Type the valid-until date (usually 30 days after the document date)."))
        elif valid_until < today:
            suggested = (max(today, document_date or today) + timedelta(days=30)).isoformat()
            found.append(_finding("VALID_UNTIL_PAST", "warn", "dueDate",
                                  f"The valid-until date {valid_until.isoformat()} has already passed.",
                                  "Move the valid-until date forward before sending.",
                                  suggestion=_suggest("dueDate", suggested, f"Valid until {suggested} (30 days)")))
        elif document_date and valid_until < document_date:
            found.append(_finding("VALID_UNTIL_BEFORE_DATE", "warn", "dueDate",
                                  "The valid-until date is before the document date.", "Correct the valid-until date."))
    if company is not None and document_type in PRINTED:
        missing = [label for key, label in (("name", "name"), ("registrationNumber", "SSM number"), ("tin", "TIN"),
                                            ("address", "address")) if not _text(company.get(key))]
        if missing:
            found.append(_finding("COMPANY_PROFILE_INCOMPLETE", "warn", "company",
                                  f"Your company profile has no {', '.join(missing)}; the {_noun(document_type)} prints without it.",
                                  "Fill them in under Settings › Company profile (an admin can)."))
    return found


def unclear_descriptions(lines: list[dict], products: dict[str, dict]) -> list[dict]:
    found = []
    for index, line in enumerate(lines):
        if _blank(line):
            continue
        text = _text(line.get("description"))
        product = products.get(_text(line.get("itemId")))
        name = _text(product.get("name")) if product else ""
        suggestion = _suggest("description", name, f"Use the product name “{name}”", line_index=index) \
            if name and name != text and len(name) >= MIN_DESCRIPTION else None
        if not text:
            code, message = "DESCRIPTION_EMPTY", f"Line {index + 1} has no description."
        elif text.lower() in GENERIC_DESCRIPTIONS:
            code, message = "DESCRIPTION_GENERIC", f"Line {index + 1} says only “{text}”, which does not say what was sold or bought."
        elif len(text) < MIN_DESCRIPTION:
            code, message = "DESCRIPTION_SHORT", f"Line {index + 1}'s description “{text}” is too short to be clear."
        elif CODE_ONLY.match(text) or (product and text.lower() == _text(product.get("sku")).lower()):
            code, message = "DESCRIPTION_CODE_ONLY", f"Line {index + 1} shows only a code (“{text}”)."
        else:
            continue
        found.append(_finding(code, "warn", "description", message,
                              "Describe the item as the other party will recognise it (what it is, size or model).",
                              line_index=index, suggestion=suggestion))
    return found


def price_list(document_type: str, lines: list[dict], products: dict[str, dict], currency: str | None,
               tolerance: Decimal = PRICE_TOLERANCE) -> list[dict]:
    """A line more than the tolerance away from the catalog price (sale price for sales, purchase price for purchases)."""
    if (currency or "MYR").upper() != "MYR":
        return []  # catalog prices are in MYR
    key = "salePrice" if document_type in SALES_CHECKED else "purchasePrice"
    label = "sale price" if key == "salePrice" else "purchase price"
    found = []
    for index, line in enumerate(lines):
        product = products.get(_text(line.get("itemId")))
        price = _dec(line.get("unitPrice"))
        catalog = _dec(product.get(key)) if product else None
        if price is None or not catalog or catalog <= 0:
            continue
        difference = (price - catalog) / catalog
        if abs(difference) > tolerance:
            direction = "above" if difference > 0 else "below"
            catalog_text = _price_text(catalog)
            found.append(_finding("PRICE_LIST_DIFFERS", "warn", "unitPrice",
                                  f"Line {index + 1}: {_price_text(price)} is {abs(difference) * 100:.0f}% {direction} the catalog "
                                  f"{label} of {catalog_text} for {product.get('name') or 'this product'}.",
                                  "Use the catalog price, or keep this price if it was agreed (for example a special discount).",
                                  line_index=index, suggestion=_suggest("unitPrice", catalog_text, f"Use {catalog_text}", line_index=index)))
    return found


def arithmetic(document_type: str, lines: list[dict], printed_total) -> list[dict]:
    """quantity x unit price - discount = line amount; lines + typed tax = total. The tax is added as typed, never computed."""
    found = []
    for index, line in enumerate(lines):
        if _blank(line):
            continue
        quantity, price = _dec(line.get("quantity")), _dec(line.get("unitPrice"))
        if quantity is None or quantity <= 0:
            found.append(_finding("QUANTITY_MISSING", "block", "quantity", f"Line {index + 1} has no quantity.",
                                  "Type a quantity greater than 0.", line_index=index))
            continue
        if price is None:
            found.append(_finding("PRICE_MISSING", "block", "unitPrice", f"Line {index + 1} has no unit price.",
                                  "Type the unit price (0 for a free item).", line_index=index))
            continue
        gross = posting.cents(quantity * price)
        discount = posting.cents(_dec(line.get("discount")) or 0)
        net = gross - discount
        if net < 0:
            found.append(_finding("DISCOUNT_TOO_LARGE", "block", "discount",
                                  f"Line {index + 1}: the discount {money(discount)} is more than the line amount {money(gross)}.",
                                  "Lower the discount; it cannot be more than quantity × unit price.", line_index=index,
                                  suggestion=_suggest("discount", money(gross), f"Use {money(gross)}", line_index=index)))
        typed = _dec(line.get("amount"))
        if typed is not None and net >= 0 and posting.cents(typed) != net:
            found.append(_finding("LINE_AMOUNT_MISMATCH", "warn", "amount",
                                  f"Line {index + 1}: {_price_text(quantity)} × {_price_text(price)} − {money(discount)} = {money(net)}, "
                                  f"but the amount shown is {money(typed)}.",
                                  "Check the quantity, unit price and discount against the document.", line_index=index,
                                  suggestion=_suggest("amount", money(net), f"Line amount {money(net)}", line_index=index)))
        if _text(line.get("taxCode")) and not _dec(line.get("tax")):
            found.append(_finding("TAX_CODE_WITHOUT_AMOUNT", "warn", "tax",
                                  f"Line {index + 1} has tax code {line['taxCode']} but no tax amount.",
                                  "Type the tax amount as printed; nothing is calculated.", line_index=index))
    total = _dec(printed_total)
    clean = [l for l in lines if not _blank(l)]
    if total is not None and clean:
        subtotal = sum((line_amount(l) or Decimal("0") for l in clean), Decimal("0"))
        tax = typed_tax(clean)
        if posting.cents(total) != subtotal + tax:
            found.append(_finding("TOTAL_MISMATCH", "warn", "printedTotal",
                                  f"Lines {money(subtotal)} + tax as typed {money(tax)} = {money(subtotal + tax)}, "
                                  f"but the total on the {_noun(document_type)} is {money(total)} "
                                  f"(a difference of {money(posting.cents(total) - subtotal - tax)}).",
                                  "Check each line and its tax amount against the document; the tax is never calculated."))
    return found


def duplicates(document_type: str, matches: list[dict], scanned_problem: str | None) -> list[dict]:
    found = [_finding("POSSIBLE_DUPLICATE", "warn", "referenceNo",
                      f"Looks like {m['number']} ({m['reason']}, {str(m['status']).replace('_', ' ')}, {m['documentDate']}).",
                      f"Open {m['number']} and check this is not the same {_noun(document_type)}; void one if it is.")
             for m in matches]
    if scanned_problem:
        found.append(_finding("DUPLICATE_OF_SCANNED", "block", "referenceNo", scanned_problem,
                              "Do not issue it again here: the scanned copy already posts. Delete this draft or change the number."))
    return found


def _line_key(line: dict) -> str:
    return _text(line.get("itemId")) or _text(line.get("description")).lower()


def _reworded(source: dict, line: dict) -> bool:
    if _text(source.get("itemId")) != _text(line.get("itemId")):
        return False
    was, now = _text(source.get("description")).lower(), _text(line.get("description")).lower()
    return bool(was and now) and (was in now or now in was)


def compare_with_source(lines: list[dict], source_lines: list[dict], source_label: str) -> list[dict]:
    """Lines whose quantity, price or description changed from the quotation / order, and lines missing or added.
    Lines are paired by product (or the same description), then by a reworded description."""
    found = []
    current = [(i, l) for i, l in enumerate(lines) if not _blank(l)]
    remaining = [(i, l) for i, l in enumerate(source_lines) if not _blank(l)]
    pairs: dict[int, tuple[int, dict]] = {}
    for index, line in current:  # first pass: same product or same description
        match = next((p for p in remaining if _line_key(line) and _line_key(p[1]) == _line_key(line)), None)
        if match:
            pairs[index] = match
            remaining.remove(match)
    for index, line in current:  # second pass: reworded lines (one description contains the other), same product or none
        if index in pairs:
            continue
        match = next((p for p in remaining if _reworded(p[1], line)), None)
        if match:
            pairs[index] = match
            remaining.remove(match)
    for index, line in current:
        if index not in pairs:
            found.append(_finding("SOURCE_LINE_ADDED", "info", "description",
                                  f"Line {index + 1} ({_text(line.get('description')) or 'no description'}) is not on {source_label}.",
                                  f"Check the line was agreed after {source_label}.", line_index=index))
            continue
        source = pairs[index][1]
        for key, label, show in (("quantity", "quantity", _quantity_text), ("unitPrice", "unit price", _price_text)):
            was, now = _dec(source.get(key)), _dec(line.get(key))
            if was is not None and now is not None and was != now:
                found.append(_finding("SOURCE_LINE_CHANGED", "warn", key,
                                      f"Line {index + 1}: the {label} was {show(was)} on {source_label}; it is now {show(now)}.",
                                      f"Keep it if the change was agreed, or restore {show(was)}.", line_index=index,
                                      suggestion=_suggest(key, show(was), f"Restore {show(was)}", line_index=index)))
        was_text, now_text = _text(source.get("description")), _text(line.get("description"))
        if was_text and was_text != now_text:
            found.append(_finding("SOURCE_LINE_CHANGED", "info", "description",
                                  f"Line {index + 1}: the description was “{was_text}” on {source_label}; it is now “{now_text}”.",
                                  "Keep it if the change was agreed, or restore the original wording.", line_index=index,
                                  suggestion=_suggest("description", was_text, "Restore the original description", line_index=index)))
    for source_index, source in remaining:
        found.append(_finding("SOURCE_LINE_MISSING", "warn", "lines",
                              f"{source_label[0].upper() + source_label[1:]} has line {source_index + 1} "
                              f"({_text(source.get('description')) or 'no description'}), which is not on this document.",
                              "Add it back if it should be charged, or leave it out if it was dropped on purpose (for example, delivered later)."))
    return found


SEVERITY_ORDER = {"block": 0, "warn": 1, "info": 2}


def run_rules(document_type: str, draft: dict, *, contact=None, company=None, products=None, today: date | None = None,
              duplicate_matches=(), scanned_problem=None, source=None) -> list[dict]:
    """All findings for a draft, most severe first. `source` is {"label": "quotation QT-..", "lines": [...]}."""
    today = today or date.today()
    lines = draft.get("lines") or []
    products = products or {}
    fields = draft.get("fields") or {}
    found = []
    found += missing_details(document_type, draft, contact, company, today)
    found += unclear_descriptions(lines, products)
    found += price_list(document_type, lines, products, draft.get("currency"))
    found += arithmetic(document_type, lines, fields.get("printedTotal"))
    found += duplicates(document_type, list(duplicate_matches), scanned_problem)
    if source and source.get("lines") is not None:
        found += compare_with_source(lines, source["lines"], source["label"])
    # A debit note can be an amount-only extra charge copied without lines; everything else needs one.
    if not [l for l in lines if not _blank(l)] and document_type not in {"debit-note", "purchase-debit-note"}:
        found.append(_finding("NO_LINES", "warn", "lines", "There are no lines.", "Add at least one line."))
    return sorted(found, key=lambda f: (SEVERITY_ORDER[f["severity"]], f.get("lineIndex", -1)))


# ------------------------------------------------------------------ context (read-only database lookups)

def _contact_record(db, tenant_id, document_type, contact_id) -> dict | None:
    try:
        contact_uuid = parse_uuid(contact_id, "contact id")
    except HTTPException:
        return None
    table, name = ("customers", "customer_name") if document_type in SALES_CHECKED else ("vendors", "vendor_name")
    row = db.execute(f"""SELECT id,{name} AS name,registration_number,address,city,state,postcode,fields FROM {table}
        WHERE id=%s AND tenant_id=%s""", (contact_uuid, tenant_id)).fetchone()
    if not row:
        return None
    address = " ".join(x for x in [row.get("address"), row.get("postcode"), row.get("city"), row.get("state")] if x)
    return {"id": row["id"], "name": row["name"], "registrationNumber": row.get("registration_number"),
            "tin": (row.get("fields") or {}).get("tin"), "address": address}


def _company(db, tenant_id) -> dict:
    row = db.execute("SELECT name,registration_number,profile FROM tenants WHERE id=%s", (tenant_id,)).fetchone()
    profile = (row["profile"] if row else None) or {}
    return {"name": row["name"] if row else "", "registrationNumber": row["registration_number"] if row else "",
            "tin": profile.get("tin"), "address": profile.get("address")}


def _products(db, tenant_id, lines) -> dict[str, dict]:
    ids = []
    for line in lines:
        try:
            if _text(line.get("itemId")):
                ids.append(UUID(_text(line.get("itemId"))))
        except ValueError:
            continue
    if not ids:
        return {}
    rows = db.execute("SELECT id,name,sku,sale_price,purchase_price FROM erp_products WHERE tenant_id=%s AND id = ANY(%s)",
                      (tenant_id, ids)).fetchall()
    return {str(r["id"]): {"name": r["name"], "sku": r["sku"], "salePrice": r["sale_price"], "purchasePrice": r["purchase_price"]}
            for r in rows}


def _source(db, tenant_id, document_id) -> dict | None:
    """The quotation or order this document was transferred from (erp_document_links, link_type 'transfer')."""
    row = db.execute("""SELECT s.document_type,s.document_number,s.payload FROM erp_document_links l
        JOIN erp_document_headers s ON s.id=l.source_document_id AND s.tenant_id=l.tenant_id
        WHERE l.tenant_id=%s AND l.target_document_id=%s AND l.link_type='transfer'
          AND s.document_type IN ('quotation','sales-order','purchase-order')
        ORDER BY l.created_at DESC LIMIT 1""", (tenant_id, document_id)).fetchone()
    if not row:
        return None
    return {"label": f"{_noun(row['document_type'])} {row['document_number']}", "lines": (row["payload"] or {}).get("lines", [])}


def _duplicate_context(db, tenant_id, document_type, draft, contact, document_id):
    """The save/issue duplicate rules from documents.py, reused as they are on an unsaved row."""
    from . import documents
    if not contact:
        return [], None
    lines = [l for l in draft.get("lines") or [] if not _blank(l)]
    total = sum((line_amount(l) or Decimal("0") for l in lines), Decimal("0")) + typed_tax(lines)
    sales = document_type in SALES_CHECKED
    row = {"id": document_id or UUID(int=0), "tenant_id": tenant_id, "document_type": document_type,
           "customer_id": contact["id"] if sales else None, "vendor_id": None if sales else contact["id"],
           "reference_no": _text(draft.get("referenceNo")) or None, "total_amount": total,
           "document_date": _day(draft.get("documentDate")) or date.today(), "document_number": _text(draft.get("number")),
           "contact_name": contact.get("name")}
    matches = documents._possible_duplicates(db, row)
    problem = None
    try:
        documents._check_duplicate_of_scanned(db, tenant_id, row)
    except HTTPException as error:
        problem = str(error.detail)
    return matches, problem


def check_document(document_type: str, data: CheckInput, request: Request):
    from . import documents
    if document_type not in CHECKED_TYPES:
        raise HTTPException(422, "Checks are available for quotations, orders, delivery orders, invoices, notes and bills.")
    user = erp_user(request, documents.WRITE_ROLES)
    draft = data.model_dump(mode="json")
    with connect() as db:
        db.execute("SET TRANSACTION READ ONLY")
        tenant_id = user["tenant_id"]
        document_id = None
        if data.documentId:
            saved = documents._load(db, user, document_type, data.documentId)
            document_id = saved["id"]
            draft["number"] = draft.get("number") or saved["document_number"]
        contact = _contact_record(db, tenant_id, document_type, data.contactId) if data.contactId else None
        matches, problem = _duplicate_context(db, tenant_id, document_type, draft, contact, document_id)
        findings = run_rules(document_type, draft, contact=contact,
                             company=_company(db, tenant_id) if document_type in PRINTED else None,
                             products=_products(db, tenant_id, draft["lines"]), duplicate_matches=matches,
                             scanned_problem=problem, source=_source(db, tenant_id, document_id) if document_id else None)
    counts = {level: sum(1 for f in findings if f["severity"] == level) for level in ("block", "warn", "info")}
    return {"findings": findings, "counts": counts}


def register(router):
    router.add_api_route("/api/erp/documents/{document_type}/checks", check_document, methods=["POST"], tags=["ERP Document checks"])
