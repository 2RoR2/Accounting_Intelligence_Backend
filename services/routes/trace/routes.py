"""Trace (plan P1): `GET /api/trace/{type}/{id}` returns the chain around one record.

Read-only. The chain is found by walking the links the system already keeps, one level at a time, from the record that
was asked for:

* typed documents: transfers (quotation -> invoice -> payment / credit note), `erp_document_allocations`, the posted
  journal and the settlement on the header, and the bank line that evidences the document;
* scanned documents: upload -> standardised record -> `settlement_allocations` -> settlement, confirmed bank matches
  (`reconciliation_candidates`) and the journals whose source is the uploaded document;
* settlements: their bank line (`source_type='bank_import'`) and their journals;
* journals: the journal they reverse, the one that reverses them and the record or settlement they came from.

Every query is tenant-scoped and parameterised. The walk stops at `MAX_NODES` nodes and says so (`truncated`).
Accountants see only the uploads they own or are assigned (the documents rule); everything else is company-wide, as
the ledger, bank and records endpoints are.
"""
from __future__ import annotations

from main import HTTPException, Request, connect

from services.routes.erp.common import DOCUMENT_TYPES, erp_user, money, parse_uuid
from services.routes.home.lanes import ACCOUNTANT_DOCUMENT_VISIBILITY

MAX_NODES = 60
MAX_DEPTH = 8

# URL segment -> node kind for everything that is not a typed ERP document type.
OTHER_KINDS = {"record": "record", "upload": "upload", "bank-transaction": "bank", "settlement": "settlement",
               "journal": "journal"}

# Where a node sits in the vertical chain: what leads to a sale or purchase first, journals last.
ERP_STAGE = {
    "quotation": 1, "sales-order": 1, "delivery-order": 1, "purchase-order": 1, "goods-received": 1,
    "invoice": 2, "bill": 2,
    "credit-note": 3, "debit-note": 3, "purchase-credit-note": 3, "purchase-debit-note": 3,
}
PAYMENT_STAGE = 4  # every other typed document: payments, refunds, money in/out, transfers
STAGE = {"upload": 0, "record": 2, "settlement": 5, "bank": 6, "journal": 7}

# How an edge reads on its later endpoint: (when that endpoint is the edge's source, when it is the target).
EDGE_TEXT = {
    "transfer": ("Converted to", "Created from"),
    "allocation": ("Applied to", "Settled by"),
    "journal": ("Posted as", "Posted from"),
    "reversal": ("Reversed by", "Reverses"),
    "settlement": ("Settlement", "Payment"),
    "bank": ("Matched to", "Matched to"),
    "match": ("Matched to", "Matched to"),
    "upload": ("Read as", "Scanned from"),
}
ARR = "%s::uuid[]"


def _edge(src, dst, rel, amount=None, status=None):
    """(src_kind, src_id, dst_kind, dst_id, rel, amount, status)."""
    return (src[0], str(src[1]), dst[0], str(dst[1]), rel, amount, status)


def _ids(values):
    return [str(v) for v in values]


def _visible_uploads(db, user, ids):
    """Upload ids the user may see (the documents rule: an accountant sees what they own or are assigned)."""
    if not ids:
        return set()
    sql = f"SELECT d.id FROM documents d WHERE d.tenant_id=%s AND d.excluded_at IS NULL AND d.id=ANY({ARR})"
    params = [user["tenant_id"], _ids(ids)]
    if user["role"] == "accountant":
        sql += " AND " + ACCOUNTANT_DOCUMENT_VISIBILITY
        params += [user["id"], user["id"]]
    return {str(r["id"]) for r in db.execute(sql, params).fetchall()}


def _undone(table_alias="a"):
    return (f"EXISTS (SELECT 1 FROM settlement_allocations x WHERE x.tenant_id={table_alias}.tenant_id "
            f"AND x.reverses_allocation_id={table_alias}.id)")


def _neighbours(db, user, kind, ids):
    """Every link that touches one of `ids` (all of one kind), as edges. One batch query per link table."""
    tenant, edges, ids = user["tenant_id"], [], _ids(ids)
    if kind == "erp":
        for r in db.execute(f"""SELECT source_document_id s,target_document_id t FROM erp_document_links
                WHERE tenant_id=%s AND link_type='transfer' AND (source_document_id=ANY({ARR}) OR target_document_id=ANY({ARR}))""",
                            (tenant, ids, ids)).fetchall():
            edges.append(_edge(("erp", r["s"]), ("erp", r["t"]), "transfer"))
        for r in db.execute(f"""SELECT source_document_id s,target_document_id t,amount,status FROM erp_document_allocations
                WHERE tenant_id=%s AND (source_document_id=ANY({ARR}) OR target_document_id=ANY({ARR}))""",
                            (tenant, ids, ids)).fetchall():
            edges.append(_edge(("erp", r["s"]), ("erp", r["t"]), "allocation", r["amount"], r["status"]))
        for r in db.execute(f"""SELECT id,journal_id,settlement_id FROM erp_document_headers
                WHERE tenant_id=%s AND id=ANY({ARR})""", (tenant, ids)).fetchall():
            if r["journal_id"]:
                edges.append(_edge(("erp", r["id"]), ("journal", r["journal_id"]), "journal"))
            if r["settlement_id"]:
                edges.append(_edge(("erp", r["id"]), ("settlement", r["settlement_id"]), "settlement"))
        for r in db.execute(f"""SELECT id,recorded_document_id FROM bank_transactions
                WHERE tenant_id=%s AND recorded_document_id=ANY({ARR})""", (tenant, ids)).fetchall():
            edges.append(_edge(("bank", r["id"]), ("erp", r["recorded_document_id"]), "bank"))
    elif kind == "upload":
        for r in db.execute(f"""SELECT id,document_id FROM standardised_records
                WHERE tenant_id=%s AND is_current AND document_id=ANY({ARR})""", (tenant, ids)).fetchall():
            edges.append(_edge(("upload", r["document_id"]), ("record", r["id"]), "upload"))
    elif kind == "record":
        records = db.execute(f"SELECT id,document_id FROM standardised_records WHERE tenant_id=%s AND id=ANY({ARR})",
                             (tenant, ids)).fetchall()
        for r in records:
            edges.append(_edge(("upload", r["document_id"]), ("record", r["id"]), "upload"))
        for r in db.execute(f"""SELECT a.settlement_id,a.record_id,a.amount,{_undone()} AS undone
                FROM settlement_allocations a WHERE a.tenant_id=%s AND a.status='active' AND a.record_id=ANY({ARR})""",
                            (tenant, ids)).fetchall():
            edges.append(_edge(("settlement", r["settlement_id"]), ("record", r["record_id"]), "allocation",
                               r["amount"], "reversed" if r["undone"] else "active"))
        for r in db.execute(f"""SELECT bank_transaction_id,record_id FROM reconciliation_candidates
                WHERE tenant_id=%s AND status='confirmed' AND record_id=ANY({ARR})""", (tenant, ids)).fetchall():
            edges.append(_edge(("bank", r["bank_transaction_id"]), ("record", r["record_id"]), "match"))
        by_document = {}
        for r in records:
            by_document.setdefault(str(r["document_id"]), []).append(r["id"])
        if by_document:
            for j in db.execute(f"""SELECT id,source_id FROM journal_headers WHERE tenant_id=%s
                    AND source_type IN ('document','adjustment') AND source_id=ANY({ARR})""",
                                (tenant, list(by_document))).fetchall():
                for record_id in by_document.get(str(j["source_id"]), []):
                    edges.append(_edge(("record", record_id), ("journal", j["id"]), "journal"))
    elif kind == "bank":
        for r in db.execute(f"""SELECT id,recorded_document_id FROM bank_transactions
                WHERE tenant_id=%s AND recorded_document_id IS NOT NULL AND id=ANY({ARR})""", (tenant, ids)).fetchall():
            edges.append(_edge(("bank", r["id"]), ("erp", r["recorded_document_id"]), "bank"))
        for r in db.execute(f"""SELECT bank_transaction_id,record_id FROM reconciliation_candidates
                WHERE tenant_id=%s AND status='confirmed' AND bank_transaction_id=ANY({ARR})""", (tenant, ids)).fetchall():
            edges.append(_edge(("bank", r["bank_transaction_id"]), ("record", r["record_id"]), "match"))
        for r in db.execute(f"""SELECT id,source_id FROM settlements WHERE tenant_id=%s AND source_type='bank_import'
                AND source_id=ANY({ARR})""", (tenant, ids)).fetchall():
            edges.append(_edge(("bank", r["source_id"]), ("settlement", r["id"]), "bank"))
    elif kind == "settlement":
        for r in db.execute(f"""SELECT a.settlement_id,a.record_id,a.amount,{_undone()} AS undone
                FROM settlement_allocations a WHERE a.tenant_id=%s AND a.status='active' AND a.settlement_id=ANY({ARR})""",
                            (tenant, ids)).fetchall():
            edges.append(_edge(("settlement", r["settlement_id"]), ("record", r["record_id"]), "allocation",
                               r["amount"], "reversed" if r["undone"] else "active"))
        for r in db.execute(f"""SELECT id,settlement_id FROM erp_document_headers
                WHERE tenant_id=%s AND settlement_id=ANY({ARR})""", (tenant, ids)).fetchall():
            edges.append(_edge(("erp", r["id"]), ("settlement", r["settlement_id"]), "settlement"))
        for r in db.execute(f"""SELECT id,source_id FROM settlements WHERE tenant_id=%s AND source_type='bank_import'
                AND source_id IS NOT NULL AND id=ANY({ARR})""", (tenant, ids)).fetchall():
            edges.append(_edge(("bank", r["source_id"]), ("settlement", r["id"]), "bank"))
        for r in db.execute(f"""SELECT id,source_id FROM journal_headers WHERE tenant_id=%s AND source_type='settlement'
                AND source_id=ANY({ARR})""", (tenant, ids)).fetchall():
            edges.append(_edge(("settlement", r["source_id"]), ("journal", r["id"]), "journal"))
    elif kind == "journal":
        for r in db.execute(f"""SELECT id,reversed_journal_id,source_type,source_id FROM journal_headers WHERE tenant_id=%s
                AND (id=ANY({ARR}) OR reversed_journal_id=ANY({ARR}))""", (tenant, ids, ids)).fetchall():
            if r["reversed_journal_id"]:
                edges.append(_edge(("journal", r["reversed_journal_id"]), ("journal", r["id"]), "reversal"))
            elif r["source_type"] == "settlement" and r["source_id"]:
                edges.append(_edge(("settlement", r["source_id"]), ("journal", r["id"]), "journal"))
            elif r["source_type"] in ("document", "adjustment") and r["source_id"]:
                for rec in db.execute("SELECT id FROM standardised_records WHERE tenant_id=%s AND document_id=%s AND is_current",
                                      (tenant, r["source_id"])).fetchall():
                    edges.append(_edge(("record", rec["id"]), ("journal", r["id"]), "journal"))
        for r in db.execute(f"""SELECT id,journal_id FROM erp_document_headers WHERE tenant_id=%s
                AND journal_id=ANY({ARR})""", (tenant, ids)).fetchall():
            edges.append(_edge(("erp", r["id"]), ("journal", r["journal_id"]), "journal"))
    return edges


def _load_nodes(db, user, wanted):
    """Display fields for every node, tenant-scoped. A node that cannot be loaded (another company, hidden) is absent."""
    tenant, out = user["tenant_id"], {}
    ids = {k: _ids(v) for k, v in wanted.items() if v}
    if "erp" in ids:
        for r in db.execute(f"""SELECT id,document_type,document_number,status,contact_name,document_date,total_amount,currency
                FROM erp_document_headers WHERE tenant_id=%s AND id=ANY({ARR})""", (tenant, ids["erp"])).fetchall():
            out[("erp", str(r["id"]))] = {
                "type": r["document_type"], "number": r["document_number"], "status": r["status"], "party": r["contact_name"],
                "date": r["document_date"], "amount": r["total_amount"], "currency": r["currency"],
                "stage": ERP_STAGE.get(r["document_type"], PAYMENT_STAGE)}
    if "record" in ids:
        for r in db.execute(f"""SELECT r.id,r.record_code,r.invoice_number,r.supplier_name,r.buyer_name,r.category,r.invoice_date,
                    r.total_amount,r.currency,r.is_current,r.revision_number,d.kind
                FROM standardised_records r JOIN documents d ON d.id=r.document_id AND d.tenant_id=r.tenant_id
                WHERE r.tenant_id=%s AND r.id=ANY({ARR})""", (tenant, ids["record"])).fetchall():
            out[("record", str(r["id"]))] = {
                "type": r["kind"], "number": r["invoice_number"] or r["record_code"],
                "status": "approved" if r["is_current"] else "superseded",
                "party": r["buyer_name"] if r["category"] == "accounts_receivable" else r["supplier_name"],
                "date": r["invoice_date"], "amount": r["total_amount"], "currency": r["currency"], "stage": STAGE["record"],
                "detail": f"Revision {r['revision_number']}"}
    if "upload" in ids:
        visible = list(_visible_uploads(db, user, ids["upload"]))
        for r in (db.execute(f"""SELECT id,document_code,original_filename,kind,status,uploaded_at FROM documents
                WHERE tenant_id=%s AND id=ANY({ARR})""", (tenant, visible)).fetchall() if visible else []):
            out[("upload", str(r["id"]))] = {
                "type": r["kind"], "number": r["document_code"], "status": r["status"], "party": None,
                "date": r["uploaded_at"].date() if r["uploaded_at"] else None, "amount": None, "currency": None,
                "stage": STAGE["upload"], "detail": r["original_filename"]}
    if "bank" in ids:
        for r in db.execute(f"""SELECT id,posted_date,description,reference,signed_amount_decimal,currency,status
                FROM bank_transactions WHERE tenant_id=%s AND id=ANY({ARR})""", (tenant, ids["bank"])).fetchall():
            out[("bank", str(r["id"]))] = {
                "type": "bank-transaction", "number": r["reference"] or (r["description"] or "")[:40] or "Bank line",
                "status": r["status"], "party": None, "date": r["posted_date"], "amount": r["signed_amount_decimal"],
                "currency": r["currency"], "stage": STAGE["bank"], "detail": r["description"]}
    if "settlement" in ids:
        for r in db.execute(f"""SELECT id,settlement_ref,settlement_date,direction,amount,currency,status FROM settlements
                WHERE tenant_id=%s AND id=ANY({ARR})""", (tenant, ids["settlement"])).fetchall():
            out[("settlement", str(r["id"]))] = {
                "type": "settlement", "number": r["settlement_ref"], "status": r["status"], "party": None,
                "date": r["settlement_date"], "amount": r["amount"], "currency": r["currency"], "stage": STAGE["settlement"],
                "detail": "Money in" if r["direction"] == "money_in" else "Money out"}
    if "journal" in ids:
        for r in db.execute(f"""SELECT j.id,j.description,j.journal_date,j.status,j.source_type,
                    COALESCE((SELECT sum(l.debit) FROM journal_lines l WHERE l.journal_id=j.id AND l.tenant_id=j.tenant_id),0) AS total
                FROM journal_headers j WHERE j.tenant_id=%s AND j.id=ANY({ARR})""", (tenant, ids["journal"])).fetchall():
            out[("journal", str(r["id"]))] = {
                "type": "reversal" if r["source_type"] == "reversal" else "journal", "number": (r["description"] or "")[:60],
                "status": r["status"], "party": None, "date": r["journal_date"], "amount": r["total"], "currency": None,
                "stage": STAGE["journal"], "detail": r["source_type"]}
    return out


def _subject(db, user, segment, subject_id):
    uid = str(parse_uuid(subject_id, "Record id"))
    if segment in DOCUMENT_TYPES:
        found = db.execute("SELECT 1 FROM erp_document_headers WHERE id=%s AND tenant_id=%s AND document_type=%s",
                           (uid, user["tenant_id"], segment)).fetchone()
        kind = "erp"
    elif segment in OTHER_KINDS:
        kind = OTHER_KINDS[segment]
        found = _load_nodes(db, user, {kind: {uid}}).get((kind, uid))
    else:
        raise HTTPException(422, "Unknown record type for trace.")
    if not found:
        raise HTTPException(404, "Record not found.")
    return kind, uid


def _walk(db, user, start):
    nodes, edges, frontier, depth, truncated = {start}, {}, {start}, 0, False
    while frontier and depth < MAX_DEPTH:
        depth += 1
        found = []
        for kind in sorted({k for k, _ in frontier}):
            found += _neighbours(db, user, kind, [i for k, i in frontier if k == kind])
        new = {end for e in found for end in ((e[0], e[1]), (e[2], e[3]))} - nodes
        room = MAX_NODES - len(nodes)
        if len(new) > room:  # keep the walk bounded: nodes beyond the cap are dropped with their edges
            truncated = True
            new = set(sorted(new)[:max(room, 0)])
        nodes |= new
        for e in found:
            if (e[0], e[1]) in nodes and (e[2], e[3]) in nodes:
                edges[e[:5]] = e
        frontier = new
    return nodes, list(edges.values()), truncated


def trace_record(record_type: str, record_id: str, request: Request):
    user = erp_user(request)
    with connect() as db:
        subject = _subject(db, user, record_type, record_id)
        keys, edges, truncated = _walk(db, user, subject)
        grouped = {}
        for kind, node_id in keys:
            grouped.setdefault(kind, set()).add(node_id)
        details = _load_nodes(db, user, grouped)
    edges = [e for e in edges if (e[0], e[1]) in details and (e[2], e[3]) in details]

    def order(key):  # a reversal journal comes after the journal it undoes
        d = details[key]
        return d["stage"], str(d["date"] or ""), d["type"] == "reversal", d["number"]

    ordered = sorted(details, key=order)
    rank = {key: n for n, key in enumerate(ordered)}
    nodes = []
    for key in ordered:
        kind, node_id = key
        d = details[key]
        via = []
        for e in edges:
            src, dst = (e[0], e[1]), (e[2], e[3])
            if key not in (src, dst):
                continue
            other = dst if key == src else src
            if rank[other] >= rank[key]:
                continue
            od = details[other]
            via.append({"text": EDGE_TEXT[e[4]][0 if key == src else 1], "key": f"{other[0]}:{other[1]}",
                        "number": od["number"], "type": od["type"],
                        "amount": money(e[5]) if e[5] is not None else None, "status": e[6]})
        nodes.append({
            "key": f"{kind}:{node_id}", "kind": kind, "id": node_id, "type": d["type"], "number": d["number"],
            "status": d["status"], "party": d["party"], "date": str(d["date"]) if d["date"] else None,
            "amount": money(d["amount"]) if d["amount"] is not None else None, "currency": d["currency"],
            "detail": d.get("detail"), "isSubject": key == subject, "via": via})
    return {"subject": f"{subject[0]}:{subject[1]}", "nodes": nodes, "truncated": truncated}


def register(router, handlers):
    router.add_api_route("/api/trace/{record_type}/{record_id}", trace_record, methods=["GET"], tags=["Trace"])
