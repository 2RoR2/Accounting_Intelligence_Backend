import { test } from "node:test";
import assert from "node:assert/strict";
import type { Document, User } from "../src/types";
import { getCurrentUser, AUTH_IDENTITY_CONTRACT } from "../src/lib/api/auth.service";
import { mapDocument } from "../src/lib/api/document-contract";
import { canEditDocument, canDecideReview, hasCurrentApproval } from "../src/lib/permissions/document-review";
import { correctLineItem, decideDocumentReview, submitDocumentReview, type DocumentReviews } from "../src/lib/api/workflow.service";
import { retryDocumentProcessing } from "../src/lib/api/document.service";
import { ApiError } from "../src/lib/api/client";
import { retryErrorMessage } from "../src/lib/api/retry-error";
const tenant = "11111111-1111-4111-8111-111111111111";
const otherTenant = "22222222-2222-4222-8222-222222222222";
const user = { id: "33333333-3333-4333-8333-333333333333", role: "ACCOUNTANT", tenantId: tenant, tenantCode: "TENANT_001" } as User;
const doc = { id: "44444444-4444-4444-8444-444444444444", tenantId: tenant, ownerId: user.id, assignedTo: null, contentRevision: 2, status: "EXTRACTED" } as Document;
const reviews: DocumentReviews = { submissions: [{ id: "s", submittedBy: user.id, decision: "SUBMITTED", note: "", createdAt: "2026-10-20T00:00:00Z" }], decisions: [] };

test("fresh auth/me preserves canonical UUID separately from display code and bypasses response cache", async () => {
  const previous = globalThis.fetch; let calls = 0;
  globalThis.fetch = async (_input, init) => { calls++; assert.equal(init?.cache, "no-store"); return Response.json({ ...user, name: "Synthetic", email: "synthetic@example.test", status: "ACTIVE", mustChangePassword: false, tenantCode: calls === 1 ? "TENANT_001" : "TENANT_002" }); };
  try {
    const first = await getCurrentUser(); const second = await getCurrentUser();
    assert.equal(first?.tenantId, tenant); assert.equal(first?.tenantCode, "TENANT_001");
    assert.equal(second?.tenantId, tenant); assert.equal(second?.tenantCode, "TENANT_002");
    assert.match(AUTH_IDENTITY_CONTRACT, /canonical-tenant-uuid/); assert.equal(calls, 2);
  } finally { globalThis.fetch = previous; }
});
test("document assignment stays null and current revision is retained", () => {
  const mapped = mapDocument({ ...doc, kind: "INVOICE" });
  assert.equal(mapped.assignedTo, null); assert.equal(mapped.contentRevision, 2); assert.equal(mapped.type, "INVOICE");
});
test("accountants need ownership or assignment within the canonical tenant", () => {
  assert.equal(canEditDocument(user, doc), true);
  assert.equal(canEditDocument(user, { ...doc, ownerId: "other", assignedTo: user.id }), true);
  assert.equal(canEditDocument(user, { ...doc, ownerId: "other", assignedTo: null }), false);
  assert.equal(canEditDocument(user, { ...doc, tenantId: otherTenant }), false);
  assert.equal(canEditDocument({ ...user, tenantId: "TENANT_001" }, doc), false, "display codes cannot grant access");
});
test("local administrators have same-tenant authority; platform admin is not a tenant editor", () => {
  assert.equal(canEditDocument({ ...user, role: "LOCAL_ADMIN" }, { ...doc, ownerId: "other", assignedTo: null }), true);
  assert.equal(canEditDocument({ ...user, role: "LOCAL_ADMIN", tenantId: otherTenant }, doc), false);
  assert.equal(canEditDocument({ ...user, role: "SUPER_ADMIN" }, doc), false);
  assert.equal(canEditDocument(user, { ...doc, status: "COMPLETED" }), false);
  assert.equal(canEditDocument(user, doc, true), false, "record existence protects stale status");
});
test("only a different local administrator can decide a visible in-review submission", () => {
  const pending = { ...doc, status: "IN_REVIEW" as const };
  assert.equal(canDecideReview(user, pending, reviews), false);
  assert.equal(canDecideReview({ ...user, role: "LOCAL_ADMIN" }, pending, reviews), false);
  assert.equal(canDecideReview({ ...user, id: "another-admin", role: "LOCAL_ADMIN" }, pending, reviews), true);
  assert.equal(canDecideReview({ ...user, id: "another-admin", role: "LOCAL_ADMIN" }, doc, reviews), false);
  assert.equal(canDecideReview({ ...user, role: "LOCAL_ADMIN" }, pending, null), false);
});
test("approval must reference the exact current revision", () => {
  const approved = { ...reviews, decisions: [{ id: "d", reviewedBy: "admin", decision: "APPROVED", note: "", approvedRevision: 2, createdAt: "" }] };
  assert.equal(hasCurrentApproval(approved, 2), true); assert.equal(hasCurrentApproval(approved, 3), false); assert.equal(hasCurrentApproval(null, 2), false);
});
test("line save sends displayed revision and consumes exact server value and new revision", async () => {
  const previous = globalThis.fetch; let sent: RequestInit | undefined;
  globalThis.fetch = async (_input, init) => { sent = init; return Response.json({ lineItemId: "line", fieldKey: "amount", value: "50.00", correctedBy: user.id, contentRevision: 3 }); };
  try { const result = await correctLineItem(doc.id, "line", "amount", "50.000", 2); assert.deepEqual(JSON.parse(sent!.body as string), { value: "50.000", expectedContentRevision: 2 }); assert.equal(result.value, "50.00"); assert.equal(result.contentRevision, 3); }
  finally { globalThis.fetch = previous; }
});
test("line save rejects invalid revisions, stale 409 and unconfirmed 200 without replay", async () => {
  const previous = globalThis.fetch; let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json({ detail: "Stale line revision" }, { status: 409 }); };
  try {
    for (const revision of [0, -1, 1.5, NaN]) await assert.rejects(correctLineItem(doc.id, "line", "amount", "1.00", revision), /current revision/);
    assert.equal(calls, 0);
    await assert.rejects(correctLineItem(doc.id, "line", "amount", "1.00", 2), e => e instanceof ApiError && e.status === 409);
    assert.equal(calls, 1);
    globalThis.fetch = async () => Response.json({ lineItemId: "line", fieldKey: "amount", value: "1.00", correctedBy: user.id, contentRevision: 2 });
    await assert.rejects(correctLineItem(doc.id, "line", "amount", "1.00", 2), /did not confirm/);
  } finally { globalThis.fetch = previous; }
});
test("review submission and decisions require explicit revision and server confirmation", async () => {
  const previous = globalThis.fetch; let body: Record<string, unknown> | undefined;
  globalThis.fetch = async (_input, init) => { body = JSON.parse(init!.body as string); return Response.json({ documentId: doc.id, decision: "submitted", status: "IN_REVIEW" }); };
  try {
    await submitDocumentReview(doc.id, "submitted", "Synthetic", 2); assert.equal(body?.expectedContentRevision, 2);
    await assert.rejects(decideDocumentReview(doc.id, "approved", "Synthetic", 2), /did not confirm/);
    await assert.rejects(submitDocumentReview(doc.id, "submitted", "Synthetic", 0), /current revision/);
  } finally { globalThis.fetch = previous; }
});
test("retry deliberate refusals retain structured codes/status and never become success", async () => {
  const previous = globalThis.fetch;
  try {
    for (const code of ["JOB_ALREADY_OPEN", "DOCUMENT_NOT_RETRYABLE", "RETRY_PROTOCOL_REQUIRED", "AMENDMENT_REQUIRED"]) {
      globalThis.fetch = async () => Response.json({ detail: { code, message: "Retry deliberately refused", jobStatus: "PENDING", dispatchAttempted: false } }, { status: 409 });
      await assert.rejects(retryDocumentProcessing(user, doc.id), error => {
        assert.ok(error instanceof ApiError); assert.equal(error.details?.code, code);
        assert.match(retryErrorMessage(error), /PENDING/); assert.match(retryErrorMessage(error), /No additional retry was dispatched/); return true;
      });
    }
    for (const status of [403, 500, 503]) {
      globalThis.fetch = async () => Response.json({ detail: "Retry unavailable" }, { status });
      await assert.rejects(retryDocumentProcessing(user, doc.id), e => e instanceof ApiError && e.status === status);
    }
    globalThis.fetch = async () => Response.json({ status: "QUEUED", queueJobId: null });
    await assert.rejects(retryDocumentProcessing(user, doc.id), /not confirmed by a supported protocol/);
  } finally { globalThis.fetch = previous; }
});
