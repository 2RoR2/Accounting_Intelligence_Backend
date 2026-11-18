"use client";
import { useCallback, useEffect, useRef, useState } from "react";
export function useResource<T>(loader: () => Promise<T>, key: string) {
  const ref = useRef(loader);
  ref.current = loader;
  const [data, setData] = useState<T>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const displayedKey = useRef(key);
  // A failed background refresh keeps what is already on screen instead of replacing the
  // page with an error; only a first load (or a new key) with nothing to show reports it.
  const loaded = useRef(false);
  const scope = useRef({ key, request: 0 });
  if (scope.current.key !== key) scope.current = { key, request: 0 };
  const refresh = useCallback(async () => {
    const currentScope = scope.current;
    const request = ++currentScope.request;
    const isCurrent = () => scope.current === currentScope && request === currentScope.request;
    const load = ref.current;
    let timeoutId: number | undefined;
    try {
      setError("");
      const timeout = new Promise<never>((_, reject) => {
        timeoutId = window.setTimeout(
          () => reject(new Error("This page took too long to load. Check the backend and database, then try again.")),
          15000,
        );
      });
      const result = await Promise.race([load(), timeout]);
      if (isCurrent()) {
        setData(result);
        loaded.current = true;
      }
    } catch (e) {
      if (isCurrent() && !loaded.current) setError(e instanceof Error ? e.message : "Unable to load data.");
    } finally {
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
      if (isCurrent()) setLoading(false);
    }
  }, []);
  useEffect(() => {
    displayedKey.current = key;
    setLoading(true);
    void refresh();
    const handler = () => void refresh();
    window.addEventListener("ai:changed", handler);
    window.addEventListener("storage", handler);
    return () => {
      scope.current.request++;
      window.removeEventListener("ai:changed", handler);
      window.removeEventListener("storage", handler);
    };
  }, [key, refresh]);
  // Keep the last successful result visible while a filter/search key is being
  // refreshed. Clearing it here caused every keystroke to replace the page with
  // the full loading skeleton, which made search feel like navigation.
  return { data, error, loading: loading && !data, refresh, setData };
}
