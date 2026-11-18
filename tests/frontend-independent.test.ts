import { test } from "node:test";
import assert from "node:assert/strict";
import { ApiError, get } from "../src/lib/api/client";
import { getCurrentUser, login } from "../src/lib/api/auth.service";
import { validateBankMatchingInputs } from "../src/lib/api/bank-contract";
import { runBankReconciliation } from "../src/lib/api/workflow.service";
import { mapSupportView } from "../src/lib/api/document-contract";
import { getStandardisedRecord, openSupportView } from "../src/lib/api/record.service";
import { revalidateDocument } from "../src/lib/api/document.service";
import type { User } from "../src/types";
const admin = { id: "synthetic-reviewer", role: "LOCAL_ADMIN" } as User;

test("invalid login and protected-resource 401 retain their distinct server messages", async () => {
  const previous = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async input => { calls++; return Response.json({ detail: String(input).endsWith("/auth/login") ? "Invalid email or password, or account is not active." : "Sign in with an active company account." }, { status: 401 }); };
  try {
    await assert.rejects(login("synthetic@example.test", "synthetic"), e => e instanceof ApiError && e.status === 401 && e.message.startsWith("Invalid email"));
    await assert.rejects(get("/api/documents"), e => e instanceof ApiError && e.status === 401 && e.message.startsWith("Sign in with"));
    // Login never refreshes. A protected 401 tries one refresh, and a failed refresh replays nothing.
    assert.equal(calls, 3, "login, protected request, one refresh; no replay");
  } finally { globalThis.fetch = previous; }
});
test("only auth/me 401 becomes signed out; service, permission and transport failures propagate", async () => {
  const previous = globalThis.fetch;
  try {
    globalThis.fetch = async () => Response.json({ detail: "Sign in" }, { status: 401 });
    assert.equal(await getCurrentUser(), null);
    for (const status of [403, 404, 500, 503]) {
      globalThis.fetch = async () => Response.json({ detail: "Account service unavailable" }, { status });
      await assert.rejects(getCurrentUser(), e => e instanceof ApiError && e.status === status);
    }
    globalThis.fetch = async () => { throw new TypeError("Synthetic network failure"); };
    await assert.rejects(getCurrentUser(), /Unable to reach/);
  } finally { globalThis.fetch = previous; }
});
test("bank inputs preserve exact signed decimal strings and nullable/omitted references", () => {
  const result = validateBankMatchingInputs([{ record_id: "r", total_amount: "999999999999999999.9900", currency: "USD", reference_number: null }], [{ transaction_id: "t", amount: "-25.5000", currency: "USD" }]);
  assert.equal(result.accounting_records[0].total_amount, "999999999999999999.9900");
  assert.equal(result.accounting_records[0].reference_number, null);
  assert.equal(result.bank_transactions[0].amount, "-25.5000");
  assert.equal(result.bank_transactions[0].reference, undefined);
  assert.deepEqual(validateBankMatchingInputs([], []), { accounting_records: [], bank_transactions: [] });
});
test("bank service rejects malformed money and shapes before issuing requests", async () => {
  const previous = globalThis.fetch; let calls = 0;
  globalThis.fetch = async () => { calls++; return Response.json({}); };
  try {
    for (const value of [100, null, "NaN", "Infinity", "1e3", "1,000.00", "USD 25.00", "", " 25.00"]) {
      await assert.rejects(runBankReconciliation([{ record_id: "r", total_amount: value, currency: "USD" }], []), /decimal string/);
      await assert.rejects(runBankReconciliation([], [{ transaction_id: "t", amount: value, currency: "USD" }]), /decimal string/);
    }
    await assert.rejects(runBankReconciliation({}, []), /JSON array/);
    await assert.rejects(runBankReconciliation([null], []), /object/);
    await assert.rejects(runBankReconciliation([{ record_id: "r", total_amount: "1.00", currency: "USD", reference_number: 1 }], []), /null, or omitted/);
    assert.equal(calls, 0);
  } finally { globalThis.fetch = previous; }
});
test("bank request uses documented string-money envelope without fabricating optional references", async () => {
  const previous = globalThis.fetch; let sent: RequestInit | undefined;
  globalThis.fetch = async (_input, init) => { sent = init; return Response.json({ results: [], used_bank_transaction_ids: [] }); };
  try {
    assert.deepEqual(await runBankReconciliation([{ record_id: "r", total_amount: "-1.00", currency: "USD" }], [{ transaction_id: "t", amount: "-1.00", currency: "USD", reference: null }]), { results: [], used_bank_transaction_ids: [] });
    assert.deepEqual(JSON.parse(sent!.body as string), { accounting_records: [{ record_id: "r", total_amount: "-1.00", currency: "USD" }], bank_transactions: [{ transaction_id: "t", amount: "-1.00", currency: "USD", reference: null }] });
  } finally { globalThis.fetch = previous; }
});
test("support mapping preserves snapshot nulls/decimals without inventing absent record metadata", async () => {
  const dto = { id: "r", documentId: "d", tenantId: "tenant", status: "COMPLETED" as const, snapshot: { contentRevision: 2, fieldPredictions: [{ key: "total", value: "-12.3400", confidence: null }], lineItems: [] } };
  const result = mapSupportView(dto);
  assert.equal(result.extraction.fields.total.value, "-12.3400");
  assert.equal(result.extraction.fields.total.confidence, null);
  for (const key of ["category", "validation", "createdAt"]) assert.equal(key in result, false);
  const previous = globalThis.fetch;
  globalThis.fetch = async () => Response.json(dto);
  try { assert.deepEqual(await openSupportView({ role: "SUPER_ADMIN" } as User, "r", "Synthetic support reason"), result); }
  finally { globalThis.fetch = previous; }
});
test("revalidation models errors/checks without pretending to return the GET validation summary", async () => {
  const previous = globalThis.fetch;
  const response = { documentId: "d", contentRevision: 3, status: "EXTRACTED", validationStatus: "REVIEW_REQUIRED", reviewRequired: true, accountingApprovalPerformed: false, coverageComplete: false, errors: [], checks: [{ rule: "BILL_RECONCILIATION", result: "WARNING", checkStatus: "NOT_CHECKED", message: "Not available" }] };
  globalThis.fetch = async () => Response.json(response);
  try { const result = await revalidateDocument(admin, "d"); assert.deepEqual(result, response); assert.equal("results" in result, false); }
  finally { globalThis.fetch = previous; }
});
test("record-list detail lookup keeps missing records absent", async () => {
  const previous = globalThis.fetch; globalThis.fetch = async () => Response.json([]);
  try { assert.equal(await getStandardisedRecord(admin, "missing"), undefined); }
  finally { globalThis.fetch = previous; }
});
