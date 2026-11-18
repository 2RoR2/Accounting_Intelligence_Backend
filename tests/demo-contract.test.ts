import { test } from "node:test";
import assert from "node:assert/strict";
import { api, ApiError } from "../src/lib/api/client";
import { readFileSync } from "node:fs";
import { currencyTotals, documentReference, escapeMarkup, isPlainNumber, mapValidationReport, normalizeValidationResult, validationState, validationSummary, type ValidationReport } from "../src/lib/api/document-contract";
import type { Extraction, ValidationResult } from "../src/types";

test("report exports escape markup and only mark true numbers as numeric", () => {
  assert.equal(escapeMarkup(`A&B <x> "y"`), "A&amp;B &lt;x&gt; &quot;y&quot;");
  assert.equal(isPlainNumber("10.60"), true);
  assert.equal(isPlainNumber("-3"), true);
  assert.equal(isPlainNumber("MYR 10.60"), false);
  assert.equal(isPlainNumber("25%"), false);
});

test("reports page keeps document-level rate, per-currency totals and true standardised counts", () => {
  const source = readFileSync("src/features/common/reports/accounting-reports.tsx", "utf8");
  assert.match(source, /\["COMPLETED", "STANDARDISED"\]\.includes\(d\.status\)/);
  assert.doesNotMatch(source, /\["COMPLETED", "VALIDATED", "STANDARDISED"\]/);
  assert.match(source, /documentsWithExceptions \/ total/);
  assert.doesNotMatch(source, /open \/ total/);
  assert.match(source, /currencyTotals\(/);
  assert.doesNotMatch(source, /acc \+ asNumber/);
});

const row = (result: string, extra: Record<string, unknown> = {}) => ({ rule: "R", message: "m", result, ...extra }) as ValidationResult;

test("stored validation statuses display as the UI's result values", () => {
  assert.equal(normalizeValidationResult(row("PASSED", { checkStatus: "PASSED" })).result, "PASS");
  assert.equal(normalizeValidationResult(row("FAILED", { checkStatus: "FAILED" })).result, "FAIL");
  assert.equal(normalizeValidationResult(row("PASS", { checkStatus: "CHECKED" })).result, "PASS");
  assert.equal(normalizeValidationResult(row("FAIL")).result, "FAIL");
  const notChecked = normalizeValidationResult(row("NOT_CHECKED", { checkStatus: "NOT_CHECKED" }));
  assert.equal(notChecked.result, "WARNING");
  assert.equal(validationState(notChecked), "NOT_CHECKED");
  assert.equal(normalizeValidationResult(row("WARNING", { checkStatus: "WARNING" })).result, "WARNING");
});
test("an unknown or missing status is never displayed as a pass", () => {
  assert.equal(normalizeValidationResult(row("SOMETHING_NEW")).result, "WARNING");
  assert.equal(normalizeValidationResult({ rule: "R", message: "m" }).result, "WARNING");
});
test("a NOT_CHECKED rule that prevents approval counts as blocking, not zero", () => {
  const report = mapValidationReport({ results: [
    row("PASSED", { checkStatus: "PASSED", preventsApproval: false }),
    row("NOT_CHECKED", { checkStatus: "NOT_CHECKED", preventsApproval: true }),
    row("NOT_CHECKED", { checkStatus: "NOT_CHECKED", preventsApproval: false }),
    row("FAILED", { checkStatus: "FAILED", preventsApproval: true }),
    row("WARNING", { checkStatus: "WARNING", preventsApproval: false }),
  ] } as unknown as ValidationReport);
  assert.deepEqual(validationSummary(report.results), { passed: 1, failed: 1, notChecked: 2, notApplicable: 0, warnings: 1, blocking: 2 });
});
test("header reference uses the key the extractor stores for each kind", () => {
  const fields = (entries: Record<string, string | null>) => Object.fromEntries(Object.entries(entries).map(([key, value]) => [key, { value }])) as unknown as Extraction["fields"];
  assert.equal(documentReference(fields({ bill_reference: "B-1" })), "B-1");
  assert.equal(documentReference(fields({ receipt_number: "R-1" })), "R-1");
  assert.equal(documentReference(fields({ reference: "X", bill_reference: "B-1" })), "X");
  assert.equal(documentReference(fields({ bill_reference: null })), null);
});
test("report totals are exact and never mix currencies", () => {
  assert.deepEqual(currencyTotals([
    { currency: "MYR", total: "0.10" }, { currency: "MYR", total: "0.20" }, { currency: "EUR", total: "184.50" },
    { currency: null, total: "1.00" }, { currency: "MYR", total: null }, { currency: "MYR", total: "bad" },
  ]), [
    { currency: null, total: "1.00", records: 1 },
    { currency: "EUR", total: "184.50", records: 1 },
    { currency: "MYR", total: "0.30", records: 2 },
  ]);
  assert.deepEqual(currencyTotals([{ currency: "MYR", total: "-5.5" }, { currency: "MYR", total: "1" }]), [{ currency: "MYR", total: "-4.50", records: 2 }]);
  assert.deepEqual(currencyTotals([{ currency: "BHD", total: "1.125" }, { currency: "BHD", total: "1.1" }]), [{ currency: "BHD", total: "2.225", records: 2 }]);
});

function json(status: number, body: unknown) { return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }); }
async function withFetch(handler: (path: string, init: RequestInit) => Response | Promise<Response>, check: (calls: string[]) => Promise<void>) {
  const original = globalThis.fetch; const calls: string[] = [];
  globalThis.fetch = (async (url: string, init: RequestInit) => { const path = String(url).replace(/^https?:\/\/[^/]+/, ""); calls.push(path); return handler(path, init); }) as typeof fetch;
  try { await check(calls); } finally { globalThis.fetch = original; }
}

test("an expired access token refreshes once and replays the request", async () => {
  let refreshed = false;
  await withFetch(path => path === "/api/auth/refresh" ? (refreshed = true, json(200, { success: true })) : refreshed ? json(200, { ok: 1 }) : json(401, { detail: "Sign in" }), async calls => {
    assert.deepEqual(await api("/api/documents"), { ok: 1 });
    assert.deepEqual(calls, ["/api/documents", "/api/auth/refresh", "/api/documents"]);
  });
});
test("concurrent 401s share one refresh because refresh tokens rotate", async () => {
  let refreshed = false; let refreshes = 0;
  await withFetch(async path => {
    if (path === "/api/auth/refresh") { refreshes += 1; await new Promise(resolve => setTimeout(resolve, 20)); refreshed = true; return json(200, {}); }
    return refreshed ? json(200, { path }) : json(401, {});
  }, async () => {
    const results = await Promise.all(["/api/a", "/api/b", "/api/c"].map(path => api(path)));
    assert.equal(refreshes, 1);
    assert.deepEqual(results, [{ path: "/api/a" }, { path: "/api/b" }, { path: "/api/c" }]);
  });
});
test("a failed refresh surfaces the original 401 without looping", async () => {
  await withFetch(() => json(401, { detail: "Sign in with an active company account." }), async calls => {
    await assert.rejects(api("/api/documents"), (error: ApiError) => error.status === 401);
    assert.deepEqual(calls, ["/api/documents", "/api/auth/refresh"]);
  });
});
test("a request still rejected after refresh does not refresh again", async () => {
  await withFetch(path => path === "/api/auth/refresh" ? json(200, {}) : json(401, {}), async calls => {
    await assert.rejects(api("/api/documents"), (error: ApiError) => error.status === 401);
    assert.deepEqual(calls, ["/api/documents", "/api/auth/refresh", "/api/documents"]);
  });
});
test("wrong credentials and MFA codes never trigger a refresh", async () => {
  for (const path of ["/api/auth/login", "/api/auth/verify-mfa", "/api/auth/logout", "/api/auth/refresh"]) {
    await withFetch(() => json(401, { detail: "Invalid" }), async calls => {
      await assert.rejects(api(path, { method: "POST" }), (error: ApiError) => error.status === 401);
      assert.deepEqual(calls, [path]);
    });
  }
});
