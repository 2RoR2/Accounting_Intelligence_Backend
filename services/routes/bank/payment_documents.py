"""Bank line <-> the payment voucher or official receipt that already recorded the payment.

Real-world order: the accountant pays a bill by payment voucher (or records a customer's official receipt) first, and
the bank line arrives later. That voucher's journal already moved the bank ledger, so matching the line to it posts
nothing; the line is linked (bank_transactions.recorded_document_id) and marked matched.

These helpers take an open connection; the routes in routes.py own the transactions and locks.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from fastapi import HTTPException

from services.routes.erp.common import money

# A payment voucher pays money out; an official receipt (customer payment) brings money in.
PAYMENT_DOCUMENT_DIRECTIONS = {"purchase-payment": "money_out", "payment": "money_in"}
PAYMENT_DOCUMENT_LABELS = {"purchase-payment": "Payment voucher", "payment": "Official receipt"}
PAYMENT_DOCUMENT_TYPES = tuple(PAYMENT_DOCUMENT_DIRECTIONS)
# Suggest payments recorded within this many days of the bank line, or any date when its reference is on the line.
DATE_WINDOW_DAYS = 60
MAX_PAYMENT_SUGGESTIONS = 5
# Words too common in Malaysian company names to say "payee found in the description".
_COMMON_NAME_WORDS = {"sdn", "bhd", "berhad", "enterprise", "enterprises", "trading", "services", "company",
                      "holdings", "resources", "industries", "plt", "the", "and"}

# Posted, ready vouchers/receipts no bank line is matched to yet.
PAYMENT_DOCUMENT_POOL = """SELECT h.id,h.document_type,h.document_number,h.reference_no,h.contact_name,h.document_date,
        h.total_amount,h.currency,h.bank_account_id,h.journal_id
    FROM erp_document_headers h
    JOIN journal_headers j ON j.id=h.journal_id AND j.tenant_id=h.tenant_id AND j.status='posted'
    WHERE h.tenant_id=%s AND h.status='ready' AND h.document_type IN ('payment','purchase-payment')
      AND NOT EXISTS (SELECT 1 FROM bank_transactions b
          WHERE b.tenant_id=h.tenant_id AND b.recorded_document_id=h.id)"""


def _norm(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _bank_text(bank) -> str:
    return _norm(f"{bank.get('description') or ''} {bank.get('reference') or ''}")


def _reference_found(bank, *tokens):
    """The first token (document number or reference) that appears on the bank line, or None."""
    text = _bank_text(bank)
    for token in tokens:
        if token and len(_norm(token)) >= 3 and _norm(token) in text:
            return str(token)
    return None


def _party_found(bank, name) -> bool:
    text = _norm(bank.get("description"))
    words = [_norm(word) for word in re.split(r"\s+", str(name or ""))]
    return any(len(word) >= 4 and word not in _COMMON_NAME_WORDS and word in text for word in words)


def _days_apart(bank_day, other_day):
    if not bank_day or not other_day:
        return None
    if isinstance(other_day, str):
        other_day = date.fromisoformat(other_day[:10])
    return abs((bank_day - other_day).days)


def _date_reason(days):
    if days is None:
        return None
    return "Same day as the bank line" if days == 0 else f"{days} day{'s' if days != 1 else ''} apart"


def _score(*, amount_exact, reference, party, days, settled=False):
    score = (60 if amount_exact else 20) + (25 if reference else 0) + (10 if party else 0)
    score += max(0, 15 - days) if days is not None else 0
    return score - (50 if settled else 0)


def statement_ledger_account(db, bank, cache):
    """The ledger account behind the statement's bank account, or None when the statement label is ambiguous."""
    key = ("statement-ledger", bank["tenant_id"], bank.get("source_bank_account_ref"))
    if key not in cache:
        rows = db.execute("""SELECT ledger_account_id FROM erp_catalog_entries
            WHERE tenant_id=%s AND kind='bank-account' AND is_active
              AND lower(%s) IN (lower(btrim(data->>'accountNumber')), lower(btrim(code)), lower(btrim(name)))
            LIMIT 2""", (bank["tenant_id"], (bank.get("source_bank_account_ref") or "").strip())).fetchall()
        cache[key] = str(rows[0]["ledger_account_id"]) if len(rows) == 1 and rows[0]["ledger_account_id"] else None
    return cache[key]


def payment_ledger_account(db, tenant_id, document, cache):
    """The bank ledger account the voucher/receipt journal used (its bank account, else the default bank)."""
    from services.routes.erp import posting

    key = ("payment-ledger", tenant_id, document["bank_account_id"])
    if key not in cache:
        try:
            cache[key] = str(posting.bank_ledger_account(db, tenant_id, document["bank_account_id"])["id"])
        except HTTPException:
            cache[key] = None
    return cache[key]


def explain_payment_document(db, bank, document, cache):
    """Why this voucher/receipt is suggested for the bank line, or None when it cannot be the match."""
    amount = abs(bank["signed_amount_decimal"] or Decimal(0))
    if (PAYMENT_DOCUMENT_DIRECTIONS.get(document["document_type"]) != bank["direction"]
            or document["currency"] != bank["currency"] or Decimal(document["total_amount"]) != amount):
        return None
    reference = _reference_found(bank, document["document_number"], document["reference_no"])
    days = _days_apart(bank["posted_date"], document["document_date"])
    if not reference and (days is None or days > DATE_WINDOW_DAYS):
        return None
    statement_ledger = statement_ledger_account(db, bank, cache)
    payment_ledger = payment_ledger_account(db, bank["tenant_id"], document, cache)
    if statement_ledger and payment_ledger and statement_ledger != payment_ledger:
        return None  # paid from another bank account: matching it would leave this account's book balance wrong
    party = _party_found(bank, document["contact_name"])
    reasons = [f"Same amount ({bank['currency']} {money(amount)})"]
    if reference:
        reasons.append(f"Reference {reference} found on the bank line")
    if party:
        reasons.append(f"{'Payee' if bank['direction'] == 'money_out' else 'Payer'} "
                       f"{document['contact_name']} found in the description")
    if (date_reason := _date_reason(days)):
        reasons.append(date_reason)
    if statement_ledger and payment_ledger:
        reasons.append("Recorded against this statement's bank account")
    else:
        reasons.append("Check the bank account: this statement is not linked to one")
    reasons.append("Already posted; confirming the match posts nothing")
    return {"kind": "payment_document", "documentId": str(document["id"]),
            "documentType": document["document_type"], "documentLabel": PAYMENT_DOCUMENT_LABELS[document["document_type"]],
            "documentNumber": document["document_number"], "contactName": document["contact_name"],
            "reference": document["reference_no"], "documentDate": str(document["document_date"]),
            "amount": money(document["total_amount"]), "currency": document["currency"], "daysApart": days,
            "score": _score(amount_exact=True, reference=reference, party=party, days=days), "explanation": reasons}


def payment_document_candidates(db, bank, cache):
    """Ranked voucher/receipt suggestions for one reviewable bank line. ``cache`` is reused across a list request."""
    if not all(bank.get(k) for k in ("direction", "currency", "posted_date", "signed_amount_decimal")):
        return []
    key = ("payment-documents", bank["tenant_id"])
    if key not in cache:
        pool = {}
        for row in db.execute(PAYMENT_DOCUMENT_POOL, (bank["tenant_id"],)).fetchall():
            pool.setdefault((row["currency"], Decimal(row["total_amount"])), []).append(row)
        cache[key] = pool
    documents = cache[key].get((bank["currency"], abs(bank["signed_amount_decimal"])), [])
    found = [item for item in (explain_payment_document(db, bank, document, cache) for document in documents) if item]
    found.sort(key=lambda item: (-item["score"], item["daysApart"] if item["daysApart"] is not None else 10**6,
                                 item["documentNumber"]))
    return found[:MAX_PAYMENT_SUGGESTIONS]


def voucher_settling_record(db, tenant_id, record_id):
    """The latest ready voucher/receipt whose settlement allocates this scanned bill/invoice, with what is left."""
    return db.execute("""SELECT h.id,h.document_type,h.document_number,h.total_amount,
            (SELECT COALESCE(r.amount_due,r.total_amount,0) FROM standardised_records r
                WHERE r.id=%s AND r.tenant_id=%s) - COALESCE((SELECT SUM(x.amount) FROM settlement_allocations x
                JOIN settlements xs ON xs.id=x.settlement_id AND xs.tenant_id=x.tenant_id AND xs.status='recorded'
                WHERE x.tenant_id=%s AND x.record_id=%s AND x.status='active'
                  AND NOT EXISTS (SELECT 1 FROM settlement_allocations u
                    WHERE u.tenant_id=x.tenant_id AND u.reverses_allocation_id=x.id)),0) AS remaining,
            EXISTS (SELECT 1 FROM bank_transactions b WHERE b.tenant_id=h.tenant_id AND b.recorded_document_id=h.id)
                AS bank_matched
        FROM settlement_allocations a
        JOIN settlements s ON s.id=a.settlement_id AND s.tenant_id=a.tenant_id AND s.status='recorded'
        JOIN erp_document_headers h ON h.settlement_id=s.id AND h.tenant_id=s.tenant_id AND h.status='ready'
            AND h.document_type IN ('payment','purchase-payment')
        WHERE a.tenant_id=%s AND a.record_id=%s AND a.status='active'
          AND NOT EXISTS (SELECT 1 FROM settlement_allocations u
                WHERE u.tenant_id=a.tenant_id AND u.reverses_allocation_id=a.id)
        ORDER BY h.document_date DESC,h.created_at DESC LIMIT 1""",
                      (record_id, tenant_id, tenant_id, record_id, tenant_id, record_id)).fetchone()


def settled_by_summary(voucher):
    if not voucher:
        return None
    return {"documentId": str(voucher["id"]), "documentType": voucher["document_type"],
            "documentNumber": voucher["document_number"], "documentLabel": PAYMENT_DOCUMENT_LABELS[voucher["document_type"]],
            "bankMatched": voucher["bank_matched"]}


def explain_record_candidate(db, bank, candidate):
    """Score and plain-language reasons for a suggested bill/invoice; flags one a voucher already paid."""
    bank_amount = abs(bank["signed_amount_decimal"] or Decimal(0))
    # Compare the record with the bank line itself: a candidate added by hand has no stored amount delta.
    delta = (Decimal(candidate["amount"]) - bank_amount if candidate["amount"] is not None
             else Decimal(candidate["amountDelta"] or "0"))
    currency = candidate["currency"] or bank["currency"] or ""
    reference = candidate["reference"] if "REFERENCE_MATCH" in candidate["reasons"] else None
    reference = reference or _reference_found(bank, candidate["reference"])
    party = _party_found(bank, candidate["party"])
    days = _days_apart(bank["posted_date"], candidate.get("recordDate"))
    settled = None
    if candidate["status"] == "proposed":
        voucher = voucher_settling_record(db, bank["tenant_id"], candidate["recordId"])
        if voucher and Decimal(voucher["remaining"]) < bank_amount:
            settled = settled_by_summary(voucher)
    reasons = [f"Same amount ({currency} {candidate['amount']})" if delta == 0
               else f"Record total {currency} {candidate['amount']} differs from the bank line by {currency} "
                    f"{money(abs(delta))}"]
    if reference:
        reasons.append(f"Reference {reference} found on the bank line")
    if party:
        reasons.append(f"{'Supplier' if bank['direction'] == 'money_out' else 'Customer'} "
                       f"{candidate['party']} found in the description")
    if (date_reason := _date_reason(days)):
        reasons.append(date_reason)
    if settled:
        reasons.append(f"Already paid by {settled['documentLabel'].lower()} {settled['documentNumber']}: "
                       f"match the bank line to {settled['documentNumber']} instead")
    return {"score": _score(amount_exact=delta == 0, reference=reference, party=party, days=days,
                            settled=bool(settled)),
            "explanation": reasons, "settledBy": settled}


def bill_already_paid_message(db, tenant_id, record_id):
    """The 409 sentence when a bank match would over-allocate a bill a voucher already settled."""
    voucher = voucher_settling_record(db, tenant_id, record_id)
    if not voucher:
        return ("This bill is already settled by an earlier payment, so this bank line cannot be matched to it again. "
                "Reject the match, or match the line to another record.")
    label = PAYMENT_DOCUMENT_LABELS[voucher["document_type"]].lower()
    if voucher["bank_matched"]:
        return (f"This bill is already settled by {label} {voucher['document_number']}, which is already matched to "
                f"another bank line. Reject this match, or match the line to another record.")
    return (f"This bill is already settled by {label} {voucher['document_number']}, so this bank line cannot be "
            f"matched to the bill again. Match the bank line to {voucher['document_number']} instead: it already "
            f"recorded this payment, so nothing is posted twice.")
