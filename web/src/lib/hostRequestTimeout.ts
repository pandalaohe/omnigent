/** Bound host lookups, including browsers without the static AbortSignal APIs. */
export async function withHostRequestTimeout<T>(
  request: (signal: AbortSignal) => Promise<T>,
  signal?: AbortSignal,
): Promise<T> {
  if (typeof AbortSignal.timeout === "function" && typeof AbortSignal.any === "function") {
    const timeout = AbortSignal.timeout(8_000);
    return request(signal ? AbortSignal.any([signal, timeout]) : timeout);
  }

  const controller = new AbortController();
  const followCaller = () => controller.abort(signal?.reason);
  if (signal?.aborted) followCaller();
  else signal?.addEventListener("abort", followCaller, { once: true });
  const timeoutError = new DOMException("Host request timed out", "TimeoutError");
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort(timeoutError);
  }, 8_000);
  try {
    return await request(controller.signal);
  } catch (error) {
    // Older browsers ignore abort(reason), so retain the timeout classification.
    if (timedOut) throw timeoutError;
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", followCaller);
  }
}
