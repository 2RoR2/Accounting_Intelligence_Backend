# Fraud Shield (F2) and AI Inbox (F3): API contract

Status: **fixed** for FE-2 on 27 Nov 2026 (BE-B, branch `claude/ai-fraud-inbox-backend-f99d78`). Additive changes only
from here on. Design: `docs/AI_FEATURES_PLAN.md` §5 (PR #68).

## Rules that hold for every endpoint

- **Behind `AI_ASSIST`** (default `off`). When it is off, every endpoint below returns
  `404 {"detail": {"code": "AI_ASSIST_OFF", "message": "..."}}`; the frontend hides the Inbox link and shield chips.
  Values that turn it on: `on`, `true`, `1`, `yes`.
- **Roles:** `local_admin` and `accountant` of a company workspace. Anyone else gets 403. An accountant sees only
  documents they own or are assigned (the same rule as `GET /api/documents`).
- **Read-only.** `GET` endpoints write nothing. AccordAI never posts, pays, approves, emails or exports. Every accept
  button calls an **existing** endpoint, as the signed-in person, with that endpoint's own permission, idempotency and
  audit rules.
- **Numbers come from the database.** Every amount, date and count in a title, message, evidence row or draft is filled
  by the server from stored values. A model may write only the wording of the supplier email and group summaries.
  Its digits are rejected, and the deterministic template is used instead.
- **Tax is never calculated.** Amounts are the totals stored on the record or read from the document.
- **Bank account numbers:** only the last 4 digits (`…4417`) leave the server. Full numbers are compared locally and
  never sent to a model.

### Shared shapes

`Evidence`:

```json
{"label": "Printed on this invoice", "value": "…4417", "href": null}
```

- `value` is a display string (amounts as `MYR 4,620.00`, dates as `2026-11-18`).
- `href` is a **frontend page segment** without a leading slash, in the same format as notification resources, for
  example `documents/<uuid>`, `records/<uuid>`, `reconciliation?status=unmatched` or `contacts?kind=supplier`.
  It is `null` when there is nothing to open.

`Severity`: `"high" | "medium" | "low"`.

---

## F2 Fraud Shield

Rule-based and explainable: no model is used to detect anything. Flags are **advisory**. They never block an action,
except where an existing rule already blocks it. Approving a flagged document needs an "approve anyway" reason
(see `acknowledge`).

### Detector codes

| Code | Severity | Fires when |
|---|---|---|
| `FS-BANK-CHANGE` | high | The supplier bank account printed on this document differs from the account on that supplier's approved documents. |
| `FS-NEAR-DUP` | high (same amount + same number shape), medium otherwise | Another document from the same supplier, of the same kind: (a) same total within 14 days with a different number, or (b) the same number once punctuation, case and O/0 and I/1 look-alikes are ignored, or (c) the same file hash. |
| `FS-NEW-LARGE` | medium | A supplier first seen in the last 30 days (no earlier document), whose total is at or above the company's 90th percentile of approved totals in the same category. It needs 10 or more approved records to set the percentile. |
| `FS-ROUND` | low | The total is a whole multiple of RM 1,000 (at least RM 1,000), and none of the supplier's 3 or more approved totals were round. |
| `FS-SOD` | high | The same person submitted and approved the document, or a value was changed after the approval. |

### `GET /api/insights/fraud?documentId=<uuid>`

The shields on one document. Returns 404 if the document is not visible to the user, and 422 if the id is not a UUID.

```json
{
  "documentId": "6f0c…",
  "documentCode": "BILL_102",
  "checkedAt": "2026-11-27T15:20:11+00:00",
  "flags": [
    {
      "code": "FS-BANK-CHANGE",
      "severity": "high",
      "title": "Bank account changed",
      "message": "The bank account printed on this document (…4417) differs from the one on Seladah Utilities' last 5 approved documents (…2093).",
      "evidence": [
        {"label": "Printed on this document", "value": "…4417", "href": null},
        {"label": "On approved documents", "value": "…2093 (5 documents, latest 2026-10-18)", "href": "documents/1a2b…"},
        {"label": "Read from", "value": "Extracted field", "href": null}
      ],
      "action": "Call the supplier on a phone number you already have (not one printed on this document) and confirm the new account before paying."
    }
  ],
  "acknowledgedCodes": ["FS-ROUND"],
  "acknowledgements": [
    {"codes": ["FS-ROUND"], "reason": "Agreed fixed fee in the contract", "by": "Aina (local admin)", "at": "2026-11-27T15:22:03+00:00"}
  ]
}
```

- `flags` is ordered by severity (high first), then code. If there are no flags, it is `[]`.
- `acknowledgedCodes` lists the codes on this document that someone has acknowledged with a reason, at any time.

### `POST /api/insights/fraud/{documentId}/acknowledge`

This is the "approve anyway" reason. Call it **before** the normal approval call, which is unchanged
(`POST /api/documents/{id}/review/decision`). It writes one `audit_logs` row with the action
`FRAUD_SHIELD_ACKNOWLEDGED` and changes nothing else.

Request:

```json
{"codes": ["FS-BANK-CHANGE"], "reason": "Confirmed by phone with Seladah's finance manager on 03-1234 5678"}
```

- `codes`: 1–5 codes. Each must be a flag currently shown on the document; otherwise the response is 422.
- `reason`: 5–1000 characters after trimming. It is required.

Response 201:

```json
{"documentId": "6f0c…", "codes": ["FS-BANK-CHANGE"], "reason": "…", "by": "Aina (local admin)", "at": "2026-11-27T15:22:03+00:00"}
```

### `GET /api/insights/fraud/summary`

Counts for the dashboard and the inbox badge, over the user's visible documents that are not set aside.

```json
{
  "checkedDocuments": 214,
  "flaggedDocuments": 3,
  "flags": 4,
  "byCode": {"FS-BANK-CHANGE": 1, "FS-NEAR-DUP": 2, "FS-NEW-LARGE": 0, "FS-ROUND": 1, "FS-SOD": 0},
  "bySeverity": {"high": 3, "medium": 0, "low": 1},
  "top": [
    {"documentId": "6f0c…", "documentCode": "BILL_102", "code": "FS-BANK-CHANGE", "severity": "high",
     "title": "Bank account changed", "href": "documents/6f0c…"}
  ]
}
```

`top` holds at most 10 flags, ordered by severity and then the newest document. All five codes are always present in
`byCode`.

---

## F3 AI Inbox

This is a list of proposals. A person accepts or dismisses each one. Nothing runs on the server by itself.

### `GET /api/inbox`

```json
{"generatedAt": "2026-11-27T15:20:11+00:00", "count": 7, "items": [ /* InboxItem */ ]}
```

`InboxItem`:

```json
{
  "id": "a3f9c2e1b07d4c5e8f10",
  "kind": "EXCEPTION_GROUP",
  "severity": "medium",
  "title": "12 documents: supplier \"Seladah Utilities\" is not on your list",
  "why": "The supplier check (EX-008) could not find \"Seladah Utilities\" in your supplier list. Add it once and check the 12 documents again.",
  "evidence": [{"label": "BILL_102", "value": "Seladah Utilities · 2026-11-18", "href": "documents/6f0c…"}],
  "actions": [ /* Action */ ],
  "basis": null,
  "draft": null
}
```

- `id` is a stable 20-character hex fingerprint of the proposal: the kind plus what it is about. It does not change
  between loads. Dismissal uses it.
- Items are ordered by severity, then in this kind order: `FRAUD_SHIELD`, `SUPPLIER_QUERY_DRAFT`,
  `EXCEPTION_GROUP`, `BANK_MATCH`, `CLOSE_BLOCKER`. There are at most 50 items.
- `basis` (on `BANK_MATCH` only) lists why the candidate was chosen, strongest first: any of `"Rule"`, `"Match"`,
  `"Memory"` and `"Similar"`.
- `draft` (on `SUPPLIER_QUERY_DRAFT` only) has the shape
  `{"to": "ap@seladah.example" | null, "subject": "…", "body": "…", "wording": "template" | "model"}`.

`Action`:

```json
{"label": "Add supplier", "method": "POST", "path": "/api/erp/contacts",
 "body": {"kind": "supplier", "legalName": "Seladah Utilities"},
 "then": [{"method": "POST", "path": "/api/documents/6f0c…/revalidate", "body": null}],
 "confirm": "Add \"Seladah Utilities\" to your suppliers and check 12 documents again?"}
```

| `method` | What the frontend does |
|---|---|
| `POST` `PATCH` `PUT` | Calls `path` (a backend path starting with `/api/`) with `body` as JSON, with the user's own session, through the normal API client. If the call fails, show the error the endpoint returns. A 403 means the user lacks that permission. |
| `LINK` | Navigates to `path`, which is a frontend page segment like an evidence `href`. `body` is `null`. |
| `COPY` | Copies `draft.subject` + a blank line + `draft.body` to the clipboard. `path` and `body` are `null`. |
| `MAILTO` | Opens `mailto:` built on the client from `draft.to`, `draft.subject` and `draft.body`. `path` and `body` are `null`. |

- `then` (optional): API calls to run in order **after** the main call succeeds. Stop at the first failure.
- `confirm` (optional): text to show in a confirm dialog before an API call. It is always set on actions that change
  data.
- After a successful accept, reload `GET /api/inbox`. A proposal that is done disappears because the state it was
  computed from has changed.

### Kinds

| `kind` | Source | Actions |
|---|---|---|
| `FRAUD_SHIELD` | Every high-severity F2 flag on visible, not-yet-approved documents | `LINK` "Open document" (`documents/<id>`). There is no accept-to-approve. |
| `SUPPLIER_QUERY_DRAFT` | Each `FS-BANK-CHANGE` flag | `COPY` "Copy email", `MAILTO` "Open in mail app", `LINK` "Open document". **AccordAI sends nothing.** |
| `EXCEPTION_GROUP` | Open exceptions grouped by root cause. EX-008 (supplier or customer not on the list) is grouped per normalised name; other codes are grouped per code. | EX-008 with no close match: `POST /api/erp/contacts` `{kind, legalName}`, then `POST /api/documents/{id}/revalidate` for each document. If the name is already on the list: only the revalidate calls. If there is a close but not exact match, or for any other code: `LINK` to the first document and the Exceptions page. |
| `BANK_MATCH` | Unmatched bank lines on confirmed statements, with their best approved open record of exactly the same amount, currency and direction | If a rule candidate exists: `POST /api/bank/reconciliation/decisions` `{candidateId, decision: "confirmed", expectedRevision, postPayment: false}`. Otherwise: `POST /api/bank/transactions/{id}/match-many` `{expectedAllocated, allocations: [{recordId, amount}]}`. Both only match. **Posting stays on the existing confirm-and-post path.** There is also a `LINK` "Review on Reconcile page". |
| `CLOSE_BLOCKER` | The current period's month-end steps that are not complete (the same figures as the Today page), plus suppliers that billed in each of the last 3 months but not this one | `LINK` per step. |

`basis` labels for `BANK_MATCH`:

- **Rule:** the existing matching rules already proposed this record (a `reconciliation_candidates` row).
- **Match:** a reference token of at least 3 digits from the record's number appears in the bank line's reference or
  description.
- **Memory:** an earlier bank line with the same counterparty words was matched to this supplier.
- **Similar:** the supplier or customer name is similar to the bank description, or the dates are within 7 days.

### `POST /api/inbox/{id}/dismiss`

This hides the proposal **for this user** for 30 days. It writes one `audit_logs` row with the action
`AI_INBOX_DISMISSED` and the resource `id`. The body is optional: `{"reason": "…"}` (≤500 characters). The response is
200 `{"id": "…", "dismissedUntil": "2026-12-27T15:20:11+00:00"}`. An `id` that is not 20 hex characters gets 422.
Dismissing an id that is not currently shown is allowed (the response is the same). Another user's inbox is not
affected.

---

## Not in this contract (later)

- A perceptual image hash (dHash) for `FS-NEAR-DUP`. Only the stored SHA-256 file hash is compared today.
- An "Explain" button that sends a shield to Ask AccordAI (F1). BE-A owns that.
