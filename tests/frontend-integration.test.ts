import { test } from "node:test";
import assert from "node:assert/strict";
import { loadReviewResource } from "../src/lib/api/review-resource";
import { standardiseDocument } from "../src/lib/api/record.service";
import { ApiError } from "../src/lib/api/client";
import type { User } from "../src/types";
const user = { id: "synthetic-reviewer", role: "LOCAL_ADMIN" } as User;

test("empty review resources remain genuinely empty", async () => {
  assert.deepEqual(await loadReviewResource(Promise.resolve([]), "Records", []), { data: [], error: null });
});
test("unavailable review resources are distinguishable from empty responses", async () => {
  for (const status of [401, 403, 500]) {
    const result = await loadReviewResource(Promise.reject(new ApiError("Denied or unavailable", status)), "Audit history", []);
    assert.deepEqual(result.data, []);
    assert.match(result.error!, /Audit history: Denied or unavailable/);
  }
});
test("review resource timeouts expose retry information without sample data", async () => {
  const result = await loadReviewResource(new Promise<never>(() => {}), "Comments", [], 5);
  assert.deepEqual(result.data, []);
  assert.match(result.error!, /Comments: Request timed out/);
});
test("standardisation waits for the actual POST response and record identifier", async () => {
  const previous = globalThis.fetch;
  let reply!: (response: Response) => void;
  let sent: RequestInit | undefined;
  let url = "";
  globalThis.fetch = async (input, init) => { url = String(input); sent = init; return new Promise(resolve => { reply = resolve; }); };
  try {
    let completed = false;
    const pending = standardiseDocument(user, "synthetic-document").then(id => { completed = true; return id; });
    await Promise.resolve();
    assert.equal(completed, false);
    assert.match(url, /\/api\/records\/from-document\/synthetic-document$/);
    assert.equal(sent?.method, "POST");
    reply(Response.json({ id: "synthetic-server-record" }));
    assert.equal(await pending, "synthetic-server-record");
  } finally { globalThis.fetch = previous; }
});
test("standardisation rejects denied, stale, unavailable or unconfirmed outcomes", async () => {
  const previous = globalThis.fetch;
  try {
    for (const status of [403, 409, 500]) {
      globalThis.fetch = async () => Response.json({ detail: "Server rejected standardisation" }, { status });
      await assert.rejects(standardiseDocument(user, "d"), error => error instanceof ApiError && error.status === status);
    }
    for (const body of [null, {}, { id: "" }]) {
      globalThis.fetch = async () => Response.json(body);
      await assert.rejects(standardiseDocument(user, "d"), /server did not confirm/);
    }
  } finally { globalThis.fetch = previous; }
});
