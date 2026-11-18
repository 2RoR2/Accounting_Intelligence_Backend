"use client";
import { ErrorState } from "@/components/common/ui";

function readableError(error: unknown) {
  if (error instanceof globalThis.Error && error.message && error.message !== "[object Event]") return error.message;
  if (error && typeof error === "object" && "message" in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === "string" && message && message !== "[object Event]") return message;
  }
  return "The page encountered an unexpected browser event. Refresh the page and try again. If it continues, check that the backend is running on port 8000.";
}

export default function Error({
  error,
  reset,
}: {
  error: Error;
  reset: () => void;
}) {
  return (
    <main className="content">
      <ErrorState message={readableError(error)} retry={reset} />
    </main>
  );
}
