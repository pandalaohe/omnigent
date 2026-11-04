import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isPhoneViewport, useIsPhone } from "./useIsPhone";

const nativeBridge = vi.hoisted(() => ({
  ios: false,
  android: false,
}));

vi.mock("@/lib/nativeBridge", () => ({
  isIOSShell: () => nativeBridge.ios,
  isAndroidShell: () => nativeBridge.android,
}));

function stubViewport(width: number, height: number, coarse = true) {
  vi.stubGlobal("innerWidth", width);
  vi.stubGlobal("innerHeight", height);
  vi.stubGlobal("screen", { width, height });
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches:
        query === "(max-width: 767.98px)"
          ? width < 768
          : query === "(pointer: coarse)"
            ? coarse
            : false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

describe("useIsPhone", () => {
  beforeEach(() => {
    nativeBridge.ios = false;
    nativeBridge.android = false;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each([
    [320, 568, true, true],
    [568, 320, true, true],
    [375, 667, true, true],
    [667, 375, true, true],
    [390, 844, true, true],
    [844, 390, true, true],
    [412, 915, true, true],
    [915, 412, true, true],
    [768, 1024, true, false],
    [1024, 768, true, false],
    [844, 390, false, false],
    [1440, 900, false, false],
  ])("classifies %ix%i (coarse=%s) as phone=%s", (width, height, coarse, expected) => {
    stubViewport(width, height, coarse);
    expect(isPhoneViewport()).toBe(expected);
  });

  it("treats iOS and Android native shells as phone", () => {
    stubViewport(844, 390, false);
    expect(isPhoneViewport()).toBe(false);
    nativeBridge.ios = true;
    expect(isPhoneViewport()).toBe(true);
    nativeBridge.ios = false;
    nativeBridge.android = true;
    expect(isPhoneViewport()).toBe(true);
  });

  it("updates when the viewport rotates between portrait, landscape phone, and desktop", () => {
    stubViewport(390, 844, true);
    const { result } = renderHook(() => useIsPhone());
    expect(result.current).toBe(true);

    act(() => {
      stubViewport(844, 390, true);
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current).toBe(true);

    act(() => {
      stubViewport(1024, 768, false);
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current).toBe(false);
  });
});
