"use client";
import { Badge } from "@/components/common/ui";
import { decimalDisplay, documentReference, fieldLabel, sourceKind } from "@/lib/api/document-contract";
import type { Document, Extraction } from "@/types";
export function DocumentDetailPreview({ doc, extraction }: { doc: Document; extraction: Extraction }) {
  const kind = sourceKind(extraction, doc.type);
  const title = kind === "UNKNOWN" ? "Unknown / Needs Review" : kind[0] + kind.slice(1).toLowerCase() + " details";
  const value = (key: string) => key === "reference" ? documentReference(extraction.fields) : extraction.fields[key]?.value;
  const keys = kind === "BILL" ? ["supplier", "reference", "account_number", "billing_period", "date", "due", "currency"]
    : kind === "RECEIPT" ? ["supplier", "reference", "date", "transaction_time", "payment_method", "currency"]
    : ["supplier", "customer", "invoice", "date", "due", "currency"];
  // Receipt rows kept apart: what is printed before tax, with tax, after rounding, and the cash that changed hands. Cash is never sales.
  const receiptKeys = kind === "RECEIPT" ? ["total_excluding_tax", "total_including_tax", "tendered_amount", "change_given"].filter(key => extraction.fields[key]?.value != null) : [];
  const amountKeys = ["subtotal", "tax", "discount", "total", "amount_due", "amount_paid", "allowances", "charges", "prepaid", "rounding"].filter(key => key in extraction.fields || kind === "INVOICE").concat(receiptKeys);
  const sources = [...(extraction.classification?.candidates ?? []), ...(extraction.classification?.unsupported_headings ?? [])];
  return <section className="invoice-review-page" aria-label={title}>
    <div className="invoice-review-heading"><div><span className="invoice-status">{doc.status}</span><h2>{title}</h2>
      <div className="invoice-classification" aria-label="Document classification">
        <div><span>Source classification</span><strong>{extraction.classification?.value ?? "Not provided"}</strong><small>{extraction.classification?.status ?? "Not provided"}</small></div>
        <div><span>Classification reason</span><strong>{extraction.classification?.reason ?? "Not provided"}</strong></div>
        <div><span>Stored kind</span><strong>{doc.kind ?? doc.type}</strong></div>
        <div><span>Intake identifier</span><strong>{doc.code ?? doc.id}</strong></div>
        <div><span>Accounting category</span><strong>{doc.category ?? "Not provided"}</strong></div>
      </div>
    </div><Badge>Human review required</Badge></div>
    <details className="panel-body"><summary>Classification evidence</summary>
      {!sources.length && <p>No supported source heading evidence available. Inspect the original document.</p>}
      {sources.map((item, index) => <div key={index}><p>{item.evidence?.text ?? item.matched_title ?? "Not provided"}</p><small>{item.evidence?.source_ref}</small>
        {item.evidence?.prov?.map((prov, i) => <p key={i}><a href={"/api/documents/" + encodeURIComponent(doc.id) + "/file" + (prov.page_no ? "#page=" + prov.page_no : "")} target="_blank" rel="noreferrer">Source page {prov.page_no ?? "not provided"}</a>{prov.bbox && <small> | Original Docling box: {JSON.stringify(prov.bbox)}</small>}</p>)}
      </div>)}
    </details>
    {kind === "UNKNOWN" ? <div className="panel-body"><p>No accounting fields were mapped. Inspect the original source and classification explanation. Standardisation is unavailable.</p></div> :
      <div className="invoice-review-grid">
        <div className="invoice-detail-card"><h3>Extracted accounting information</h3><div className="invoice-detail-columns">
          {keys.map(key => <div key={key}><strong>{fieldLabel(kind, key, extraction.fields[key]?.label)}</strong><span>{value(key) ?? "Not provided"}</span></div>)}
        </div></div>
        <div className="invoice-detail-card"><h3>Source amounts</h3><div className="invoice-totals">
          {amountKeys.map(key => <span key={key}>{fieldLabel(kind, key, extraction.fields[key]?.label)}<b>{decimalDisplay(value(key), value("currency"))}</b></span>)}
        </div><p>Validation and approval do not indicate payment or settlement.</p></div>
      </div>}
  </section>;
}
