"use client";

import { useEffect, useState, type ReactNode } from "react";
import { AppLoader } from "./app-loader";

// Keep the authored opening scene visible long enough to be perceived on fast
// local and production loads. Content/skeleton loading remains separate.
const OPENING_DURATION_MS = 1450;

export function OpeningLoader({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setReady(true), OPENING_DURATION_MS);
    return () => window.clearTimeout(timer);
  }, []);

  if (!ready) return <AppLoader />;
  return <>{children}</>;
}
