import { ApiError } from "./client";

export function retryErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : "Retry failed";
  const details = error.details;
  if (details?.jobStatus || details?.dispatchAttempted === false) {
    const status = details.jobStatus ? ` Status: ${details.jobStatus}.` : "";
    const dispatch = details.dispatchAttempted === false ? " No additional retry was dispatched." : "";
    return `${error.message}.${status}${dispatch}`;
  }
  return error.message;
}
