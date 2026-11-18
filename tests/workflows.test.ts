import { test } from "node:test";
import assert from "node:assert/strict";

const baseUrl = process.env.API_BASE_URL ?? "http://127.0.0.1:8002";
const headers = { Accept: "application/json", "X-Requested-With": "AccountingIntelligence" };

async function backendIsRunning() {
  try {
    return (await fetch(`${baseUrl}/api/health`, { headers })).ok;
  } catch {
    return false;
  }
}

async function requireBackend(t: { skip: (message?: string) => void }) {
  if (!(await backendIsRunning())) {
    t.skip(`Backend is not running at ${baseUrl}`);
    return false;
  }
  return true;
}

test("backend health endpoint is available", async (t) => {
  if (!(await requireBackend(t))) return;
  const response = await fetch(`${baseUrl}/api/health`, { headers });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).status, "ok");
});

test("protected document API rejects unauthenticated access", async (t) => {
  if (!(await requireBackend(t))) return;
  const response = await fetch(`${baseUrl}/api/documents`, { headers });
  assert.ok([401, 403].includes(response.status));
});

test("protected processing API rejects unauthenticated access", async (t) => {
  if (!(await requireBackend(t))) return;
  const response = await fetch(`${baseUrl}/api/processing/jobs`, { headers });
  assert.ok([401, 403].includes(response.status));
});
