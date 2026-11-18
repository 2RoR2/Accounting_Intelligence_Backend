// Keep the browser and API on the same host name so SameSite=strict auth
// cookies are sent after login. Configure NEXT_PUBLIC_API_URL when the API
// is deployed separately. For a same-origin deployment, use relative URLs;
// local development continues to use the backend on port 8000.
const configuredApiBase = process.env.NEXT_PUBLIC_API_URL?.trim();
const API_BASE = (configuredApiBase ?? "http://127.0.0.1:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(message: string, public readonly status?: number, public readonly details?: Readonly<Record<string, unknown>>) {
    super(message);
    this.name = "ApiError";
  }
}

function apiUrl(path: string) {
  if (typeof window !== "undefined") {
    // Always use the same-origin Next.js proxy in the browser. This keeps the
    // MFA/session cookies on the exact host the user is visiting and avoids
    // localhost/127.0.0.1/CORS mismatches.
    return path;
  }
  return `${API_BASE}${path}`;
}

// These endpoints establish or end a session, or respond to one that is not
// yet active; a 401 from them must surface instead of triggering a refresh.
const NO_REFRESH = /^\/api\/auth\/(refresh|login|verify-mfa|resend-mfa|logout|forgot-password|verify-reset-code|reset-status|reset-password|activate|recover-authenticator)\b/;
let refreshInFlight: Promise<boolean> | null = null;

/** Refresh tokens rotate on use, so concurrent 401s must share one refresh request. */
function refreshSession(): Promise<boolean> {
  refreshInFlight ??= fetch(apiUrl("/api/auth/refresh"), {
    method: "POST",
    credentials: "include",
    headers: { Accept: "application/json", "X-Requested-With": "AccountingIntelligence" },
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
  }).then(response => response.ok).catch(() => false).finally(() => { refreshInFlight = null; });
  return refreshInFlight;
}

export async function api<T>(path: string, init: RequestInit = {}, allowRefresh = true): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  headers.set("X-Requested-With", "AccountingIntelligence");
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  let response: Response;
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(() => controller.abort(), 15000);
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      headers,
      credentials: "include",
      cache: "no-store",
      signal: controller.signal,
    });

    // Keep the timeout active until the body is fully consumed. A server can
    // send 200 headers while the database-backed JSON response is still
    // blocked, which previously left the UI in a permanent loading state.
    const body = await response.json().catch(() => null);
    if (response.status === 401 && allowRefresh && !NO_REFRESH.test(path) && await refreshSession()) {
      return await api<T>(path, init, false);
    }
    if (!response.ok) {
      const detail = body && typeof body.detail === "string"
        ? body.detail
        : body?.detail?.message
          ? `${body.detail.message}${body.detail.existingDocumentCode ? ` Existing document: ${body.detail.existingDocumentCode}.` : ""}`
          : typeof body?.message === "string"
            ? body.message
            : typeof body?.error === "string"
              ? body.error
              : response.status >= 500
                ? `The backend could not complete this request (${response.status}). Check the API and database service logs.`
                : `Request failed (${response.status})`;
      const details = body?.detail && typeof body.detail === "object" && !Array.isArray(body.detail) ? body.detail as Record<string, unknown> : undefined;
      throw new ApiError(detail, response.status, details);
    }
    return body as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("The request timed out. Check that the backend and database are running, then try again.");
    }
    if (error instanceof Error && (error.message.startsWith("Your session expired") || error.message.startsWith("Request failed") || error.message.startsWith("Unable to reach") || error.message.startsWith("The request timed out") || error.message.includes("not allowed") || error.message.includes("required") || error.message.includes("invalid"))) {
      throw error;
    }
    // Preserve API/application errors. Only browser transport failures should
    // be rewritten as an unreachable-backend message.
    if (error instanceof Error && error.name !== "TypeError") throw error;
    throw new Error("Unable to reach the backend API. Check that the backend is running on port 8000.");
  } finally {
    globalThis.clearTimeout(timeout);
  }
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
