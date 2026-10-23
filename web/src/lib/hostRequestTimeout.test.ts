import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { withHostRequestTimeout } from "./hostRequestTimeout";

const originalTimeout = AbortSignal.timeout;
const originalAny = AbortSignal.any;

beforeEach(() => {
  Object.defineProperty(AbortSignal, "timeout", { configurable: true, value: undefined });
  Object.defineProperty(AbortSignal, "any", { configurable: true, value: undefined });
  vi.useFakeTimers();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  Object.defineProperty(AbortSignal, "timeout", { configurable: true, value: originalTimeout });
  Object.defineProperty(AbortSignal, "any", { configurable: true, value: originalAny });
});

it.each([false, true])(
  "bounds a stalled operation (abort reason unsupported: %s)",
  async (legacy) => {
    if (legacy) {
      const abort = AbortController.prototype.abort;
      vi.spyOn(AbortController.prototype, "abort").mockImplementation(function (
        this: AbortController,
      ) {
        abort.call(this);
      });
    }
    const pending = withHostRequestTimeout(
      (signal) =>
        new Promise((_, reject) => {
          signal.addEventListener("abort", () => reject(signal.reason), { once: true });
        }),
    );
    const rejected = expect(pending).rejects.toMatchObject({ name: "TimeoutError" });
    await vi.advanceTimersByTimeAsync(8_000);
    await rejected;
    expect(vi.getTimerCount()).toBe(0);
  },
);

it("cleans up after an operation failure and follows an already-aborted caller", async () => {
  const error = new Error("lookup failed");
  await expect(
    withHostRequestTimeout(async () => {
      throw error;
    }),
  ).rejects.toBe(error);
  expect(vi.getTimerCount()).toBe(0);
  const caller = new AbortController();
  caller.abort();
  await withHostRequestTimeout(async (signal) => {
    expect(signal.aborted).toBe(true);
  }, caller.signal);
  expect(vi.getTimerCount()).toBe(0);
});
