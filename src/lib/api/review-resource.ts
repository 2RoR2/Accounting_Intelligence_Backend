export async function loadReviewResource<T>(promise: Promise<T>, label: string, fallback: T, timeoutMs = 15000): Promise<{ data: T; error: string | null }> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error("Request timed out")), timeoutMs);
    });
    return { data: await Promise.race([promise, timeout]), error: null };
  } catch (error) {
    const message = error instanceof Error ? error.message : "Unable to load resource";
    return { data: fallback, error: `${label}: ${message}` };
  } finally {
    if (timer) clearTimeout(timer);
  }
}
