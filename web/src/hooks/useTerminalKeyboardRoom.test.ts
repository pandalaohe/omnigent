import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTerminalKeyboardRoom } from "./useTerminalKeyboardRoom";

const nativeBridge = vi.hoisted(() => ({
  ios: false,
  nativeHeight: null as number | null,
  listeners: new Set<() => void>(),
}));

vi.mock("@/lib/nativeBridge", () => ({
  isIOSShell: () => nativeBridge.ios,
  getIOSKeyboardViewportHeight: () => nativeBridge.nativeHeight,
  onNativeKeyboardViewportChanged: (cb: () => void) => {
    nativeBridge.listeners.add(cb);
    return () => {
      nativeBridge.listeners.delete(cb);
    };
  },
}));

interface StubVisualViewport {
  width: number;
  height: number;
  offsetTop: number;
  addEventListener: (type: string, listener: EventListener) => void;
  removeEventListener: (type: string, listener: EventListener) => void;
  emit: (type: string) => void;
}

function createStubVisualViewport(
  width: number,
  height: number,
  offsetTop = 0,
): StubVisualViewport {
  const listeners = new Map<string, Set<EventListener>>();
  return {
    width,
    height,
    offsetTop,
    addEventListener(type, listener) {
      let set = listeners.get(type);
      if (!set) {
        set = new Set();
        listeners.set(type, set);
      }
      set.add(listener);
    },
    removeEventListener(type, listener) {
      listeners.get(type)?.delete(listener);
    },
    emit(type) {
      for (const listener of listeners.get(type) ?? []) {
        listener(new Event(type));
      }
    },
  };
}

function makeRoot(
  rect: { top: number; bottom: number; width: number; height: number },
  insetBottom = "0px",
) {
  const el = document.createElement("div");
  el.style.setProperty("--omnigent-inset-bottom", insetBottom);
  document.body.appendChild(el);
  vi.spyOn(el, "getBoundingClientRect").mockImplementation(
    () =>
      ({
        top: rect.top,
        bottom: rect.bottom,
        left: 0,
        right: rect.width,
        width: rect.width,
        height: rect.height,
        x: 0,
        y: rect.top,
        toJSON: () => ({}),
      }) as DOMRect,
  );
  return { current: el };
}

describe("useTerminalKeyboardRoom", () => {
  beforeEach(() => {
    nativeBridge.ios = false;
    nativeBridge.nativeHeight = null;
    nativeBridge.listeners.clear();
    vi.stubGlobal("scrollY", 0);
    vi.stubGlobal("scrollTo", vi.fn());
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb: FrameRequestCallback) => {
      cb(0);
      return 1;
    });
  });

  afterEach(() => {
    document.body.innerHTML = "";
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("returns neutral state when disabled", () => {
    const rootRef = makeRoot({ top: 56, bottom: 844, width: 390, height: 788 });
    const { result } = renderHook(() => useTerminalKeyboardRoom(rootRef, false, true));
    expect(result.current).toEqual({
      overlap: 0,
      keyboardUp: false,
      roomPx: Number.POSITIVE_INFINITY,
      paddingBottomPx: 0,
      keyRowWidth: 0,
    });
  });

  it("computes visualViewport overlap, keyboardUp (>80px), and roomPx on iOS Safari", () => {
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);
    const vv = createStubVisualViewport(390, 544, 0);
    vi.stubGlobal("visualViewport", vv);

    const rootRef = makeRoot({ top: 56, bottom: 844, width: 390, height: 788 }, "34px");
    const { result } = renderHook(() => useTerminalKeyboardRoom(rootRef, true, true));

    // vvBottom = 544; rootRect.bottom = 844 -> overlap = 300
    expect(result.current).toEqual({
      overlap: 300,
      keyboardUp: true,
      roomPx: 788 - 300,
      paddingBottomPx: 300,
      keyRowWidth: 374,
    });

    // Small browser bar shift (50px <= 80px): overlap is 50, but keyboardUp stays false
    act(() => {
      vv.height = 794;
      vv.emit("resize");
    });
    expect(result.current).toEqual({
      overlap: 50,
      keyboardUp: false,
      roomPx: 788 - 50,
      paddingBottomPx: 50,
      keyRowWidth: 374,
    });
  });

  it("prefers getIOSKeyboardViewportHeight in the iOS native shell and skips scrollTo", () => {
    nativeBridge.ios = true;
    nativeBridge.nativeHeight = 508; // 844 - 336 = 508 visibleBottom
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);
    const vv = createStubVisualViewport(390, 844, 24);
    vi.stubGlobal("visualViewport", vv);

    const rootRef = makeRoot({ top: 56, bottom: 844, width: 390, height: 788 }, "34px");
    const { result } = renderHook(() => useTerminalKeyboardRoom(rootRef, true, true));

    expect(result.current).toEqual({
      overlap: 336,
      keyboardUp: true,
      roomPx: 788 - 336,
      paddingBottomPx: 336,
      keyRowWidth: 374,
    });
    expect(window.scrollTo).not.toHaveBeenCalled();
  });

  it("computes roomPx when the layout viewport shrinks with the keyboard", () => {
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);
    const vv = createStubVisualViewport(390, 844, 0);
    vi.stubGlobal("visualViewport", vv);

    let rect = { top: 56, bottom: 844, width: 390, height: 788 };
    const el = document.createElement("div");
    el.style.setProperty("--omnigent-inset-bottom", "34px");
    document.body.appendChild(el);
    vi.spyOn(el, "getBoundingClientRect").mockImplementation(() => rect as DOMRect);
    const rootRef = { current: el };

    const { result } = renderHook(() => useTerminalKeyboardRoom(rootRef, true, true));
    expect(result.current.keyboardUp).toBe(false);
    expect(result.current.roomPx).toBe(788 - 34);

    // Layout-resize keyboard opens: both innerHeight and visualViewport.height drop to 544
    act(() => {
      vi.stubGlobal("innerHeight", 544);
      vv.height = 544;
      rect = { top: 56, bottom: 544, width: 390, height: 488 };
      vv.emit("resize");
    });

    expect(result.current).toEqual({
      overlap: 0,
      keyboardUp: false,
      roomPx: 488 - 34,
      paddingBottomPx: 34,
      keyRowWidth: 374,
    });
  });

  it("releases iOS Safari visualViewport pan via window.scrollTo(0, 0) when keyboardUp and panned", () => {
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);
    const vv = createStubVisualViewport(390, 544, 18);
    vi.stubGlobal("visualViewport", vv);

    const rootRef = makeRoot({ top: 38, bottom: 826, width: 390, height: 788 });
    renderHook(() => useTerminalKeyboardRoom(rootRef, true, true));

    expect(window.scrollTo).toHaveBeenCalledWith(0, 0);
  });

  it("resolves a calc() safe-area inset to computed pixels", () => {
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);
    vi.stubGlobal("visualViewport", createStubVisualViewport(390, 844, 0));

    const rootRef = makeRoot(
      { top: 56, bottom: 844, width: 390, height: 788 },
      "calc(34px + 0px * 0)",
    );
    const computedStyle = window.getComputedStyle.bind(window);
    vi.spyOn(window, "getComputedStyle").mockImplementation((element, pseudo) => {
      const style = computedStyle(element, pseudo);
      if (element !== rootRef.current) {
        Object.defineProperty(style, "height", { value: "34px", configurable: true });
      }
      return style;
    });

    const { result } = renderHook(() => useTerminalKeyboardRoom(rootRef, true, true));

    expect(result.current.paddingBottomPx).toBe(34);
    expect(result.current.roomPx).toBe(788 - 34);
  });

  it("lets --term-safe-b: 0px override the inherited inset where an ancestor reserves it", () => {
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);
    vi.stubGlobal("visualViewport", createStubVisualViewport(390, 844, 0));

    const rootRef = makeRoot({ top: 56, bottom: 844, width: 390, height: 788 }, "34px");
    rootRef.current.style.setProperty("--term-safe-b", "0px");
    const { result } = renderHook(() => useTerminalKeyboardRoom(rootRef, true, true));

    expect(result.current.paddingBottomPx).toBe(0);
    expect(result.current.roomPx).toBe(788);
  });

  it("ignores the safe bottom when the surface already reserves it", () => {
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);
    vi.stubGlobal("visualViewport", createStubVisualViewport(390, 844, 0));

    const rootRef = makeRoot({ top: 56, bottom: 844, width: 390, height: 788 }, "34px");
    const { result } = renderHook(() => useTerminalKeyboardRoom(rootRef, true, false));

    expect(result.current.paddingBottomPx).toBe(0);
  });
});
