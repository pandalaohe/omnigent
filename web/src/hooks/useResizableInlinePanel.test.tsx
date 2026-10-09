import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readPanelSizePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";
import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { writeWidenWorkspaceForContent } from "@/lib/workspacePanelPreferences";
import { resetWidthStoreForTesting, useResizableInlinePanel } from "./useResizableInlinePanel";

// useResizableInlinePanel keeps its device-wide width in a module-level store
// shared across all callers. resetWidthStoreForTesting reloads it from storage
// between tests so cases are fully independent. A 2000px viewport gives a
// 1512px clamp ceiling (2000 - 480 chat minimum - 8 gap); the default width
// there is 600 (0.36 * 2000 = 720, clamped to the [420, 600] band).

const SESSION = "conv_test";
const originalInnerWidth = window.innerWidth;

function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

// Simulate a manual resize via the public keyboard handle (ArrowLeft widens by
// 20px). Returns the resulting panelWidth.
function nudgeWiderOnce(result: { current: ReturnType<typeof useResizableInlinePanel> }): number {
  act(() =>
    result.current.handleProps.onKeyDown({
      key: "ArrowLeft",
      preventDefault: () => {},
    } as React.KeyboardEvent),
  );
  return result.current.panelWidth;
}

beforeEach(() => {
  setInnerWidth(2000);
});

afterEach(() => {
  localStorage.clear();
  resetWidthStoreForTesting();
  setInnerWidth(originalInnerWidth);
});

describe("useResizableInlinePanel persistence", () => {
  it("persists explicit keyboard resize for this device and restores it after store reset", () => {
    const { result, unmount } = renderHook(() => useResizableInlinePanel(SESSION));

    // Default 600 + one ArrowLeft step (20px) = 620, persisted under the
    // device-local panel preference.
    const afterNudge = nudgeWiderOnce(result);
    expect(afterNudge).toBe(620);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBe(620);

    unmount();
    resetWidthStoreForTesting();
    const restored = renderHook(() => useResizableInlinePanel(SESSION));

    // The saved manual width wins over the viewport-derived default of 600.
    expect(restored.result.current.panelWidth).toBe(620);
    restored.unmount();
  });

  it("carries the last adjusted width into a different session tree", () => {
    const rootTreeKey = "conv_root";
    const first = renderHook(() => useResizableInlinePanel(rootTreeKey));
    expect(nudgeWiderOnce(first.result)).toBe(620);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBe(620);
    first.unmount();

    // The next session on this device inherits the last adjusted width.
    const second = renderHook(() => useResizableInlinePanel("conv_other_root"));
    expect(second.result.current.panelWidth).toBe(620);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBe(620);
    second.unmount();
  });

  it("migrates an existing per-session width into the device preference once", () => {
    writeSessionWorkspaceState(SESSION, { widthPx: 680 });
    resetWidthStoreForTesting();

    const first = renderHook(() => useResizableInlinePanel(SESSION));
    expect(first.result.current.panelWidth).toBe(680);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBe(680);
    first.unmount();

    const second = renderHook(() => useResizableInlinePanel("conv_other_root"));
    expect(second.result.current.panelWidth).toBe(680);
    second.unmount();
  });

  it("re-derives from the preference on resize: clamps down on shrink, springs back on widen", () => {
    const { result } = renderHook(() => useResizableInlinePanel(SESSION));

    // Establish a persisted preference of 620 (default 600 + one ArrowLeft step).
    expect(nudgeWiderOnce(result)).toBe(620);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBe(620);

    // Shrinking the viewport clamps the live width to the chat-preserving
    // ceiling (700 - 480 chat - 8 gap = 212). The chat's 480 floor wins over
    // the panel's own 240 comfort minimum, so the panel yields below 240 rather
    // than squeeze the chat. The saved 620 preference is untouched.
    setInnerWidth(700);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.panelWidth).toBe(212);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBe(620);

    // Widening again re-derives from the preference, restoring 620 in-session.
    setInnerWidth(2000);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.panelWidth).toBe(620);
  });
});

describe("useResizableInlinePanel reserved width (sidebar)", () => {
  // `reservedPx` is the open sidebar's width. It must tighten the ceiling
  // (keeping the chat at its 480px minimum) without overwriting the user's
  // preferred width, so collapsing the sidebar gives the width straight back.
  it("caps at the sidebar-aware ceiling and restores the preference when it collapses", () => {
    setInnerWidth(1400);
    // Drag the panel out to its sidebar-collapsed ceiling: 1400 - 480 - 8 = 912.
    const collapsed = renderHook(() =>
      useResizableInlinePanel(SESSION, undefined, /* reservedPx */ 0),
    );
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 0 })));
    act(() =>
      collapsed.result.current.handleProps.onMouseDown({
        preventDefault: () => {},
      } as React.MouseEvent),
    );
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 100 })));
    act(() => window.dispatchEvent(new MouseEvent("mouseup")));
    expect(collapsed.result.current.panelWidth).toBe(912);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBe(912);
    collapsed.unmount();

    // Sidebar open (320px): the ceiling drops to 1400 - 320 - 480 - 8 = 592, so
    // the rendered width is squeezed but the saved preference is untouched.
    const open = renderHook(() => useResizableInlinePanel(SESSION, undefined, 320));
    expect(open.result.current.panelWidth).toBe(592);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBe(912);
    open.unmount();

    // Collapsing restores the full preferred width.
    const reopened = renderHook(() => useResizableInlinePanel(SESSION, undefined, 0));
    expect(reopened.result.current.panelWidth).toBe(912);
    reopened.unmount();
  });

  it("leaves the chat its 480px minimum with the sidebar open", () => {
    setInnerWidth(1400);
    // A preference far wider than the sidebar-open ceiling allows.
    const { result, unmount } = renderHook(() => useResizableInlinePanel(SESSION, undefined, 320));
    act(() => {
      for (let i = 0; i < 60; i++) {
        result.current.handleProps.onKeyDown({
          key: "ArrowLeft",
          preventDefault: () => {},
        } as React.KeyboardEvent);
      }
    });
    // 1400 - 320 sidebar - 8 gap - panel >= 480 for the chat.
    expect(1400 - 320 - result.current.panelWidth - 8).toBeGreaterThanOrEqual(480);
    unmount();
  });

  it("keeps the chat >= 480px when the viewport shrinks with both sidebars open", () => {
    // The reported bug: with the left sidebar open (reserved) AND the rail wide,
    // shrinking the window let the chat fall under 480 — the panel's own 240
    // comfort minimum was overriding the chat-preserving ceiling, and a resize
    // that didn't move the stored width never re-rendered. The chat floor must
    // win and the recompute must fire on every resize.
    setInnerWidth(1400);
    const reservedPx = 320; // open left sidebar
    const { result, rerender } = renderHook(
      ({ reserved }) => useResizableInlinePanel(SESSION, undefined, reserved),
      { initialProps: { reserved: reservedPx } },
    );
    // Drag the rail out to its widest at this viewport.
    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 0 })));
    act(() => window.dispatchEvent(new MouseEvent("mouseup")));

    // Now shrink the viewport hard. Even though the stored (no-reserve) width may
    // still fit its own ceiling, the render-time reserve clamp must re-run.
    setInnerWidth(1000);
    act(() => window.dispatchEvent(new Event("resize")));
    rerender({ reserved: reservedPx });
    // chat = viewport - sidebar - gap - panel.
    expect(1000 - reservedPx - 8 - result.current.panelWidth).toBeGreaterThanOrEqual(480);
  });
});

describe("useResizableInlinePanel browser/file width", () => {
  // The rail keeps a second, independent width while it shows a browser tab or
  // an opened file. `reservedPx` is 320 (the open left sidebar), so the wide
  // default is max(normal, round((2000 - 320) / 2)) = max(normal, 840).
  it("uses the wide default while wide content shows and restores the normal width after", () => {
    const { result, rerender } = renderHook(
      ({ wide }) => useResizableInlinePanel(SESSION, undefined, 320, true, wide),
      { initialProps: { wide: false } },
    );

    expect(result.current.panelWidth).toBe(600);

    rerender({ wide: true });
    expect(result.current.panelWidth).toBe(840);

    rerender({ wide: false });
    expect(result.current.panelWidth).toBe(600);
  });

  it("keeps the two widths independent across keyboard steps", () => {
    const { result, rerender } = renderHook(
      ({ wide }) => useResizableInlinePanel(SESSION, undefined, 320, true, wide),
      { initialProps: { wide: false } },
    );

    rerender({ wide: true });
    expect(result.current.panelWidth).toBe(840);

    act(() =>
      result.current.handleProps.onKeyDown({
        key: "ArrowLeft",
        preventDefault: () => {},
      } as React.KeyboardEvent),
    );
    expect(result.current.panelWidth).toBe(860);
    expect(readPanelSizePreference("inlinePanelWideWidthPx")).toBe(860);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();

    rerender({ wide: false });
    expect(result.current.panelWidth).toBe(600);

    // A step in normal mode writes only the normal width.
    act(() =>
      result.current.handleProps.onKeyDown({
        key: "ArrowLeft",
        preventDefault: () => {},
      } as React.KeyboardEvent),
    );
    expect(result.current.panelWidth).toBe(620);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBe(620);
    expect(readPanelSizePreference("inlinePanelWideWidthPx")).toBe(860);

    // Returning to wide content restores the remembered wide width.
    rerender({ wide: true });
    expect(result.current.panelWidth).toBe(860);
  });

  it("never starts the wide mode narrower than the normal width", () => {
    writePanelSizePreference("inlinePanelWidthPx", 1000);
    resetWidthStoreForTesting();

    const { result } = renderHook(
      ({ wide }) => useResizableInlinePanel(SESSION, undefined, 320, true, wide),
      { initialProps: { wide: true } },
    );

    expect(result.current.panelWidth).toBe(1000);
  });

  it("caps the wide width at the chat floor without overwriting the preference", () => {
    setInnerWidth(1512);
    writePanelSizePreference("inlinePanelWideWidthPx", 900);
    resetWidthStoreForTesting();

    const { result } = renderHook(
      ({ wide }) => useResizableInlinePanel(SESSION, undefined, 320, true, wide),
      { initialProps: { wide: true } },
    );

    // 1512 - 320 sidebar - 480 chat - 8 gap = 704 ceiling; the saved 900 is
    // squeezed at render time but left intact on disk.
    expect(result.current.panelWidth).toBe(704);
    expect(readPanelSizePreference("inlinePanelWideWidthPx")).toBe(900);
  });

  it("falls back to the normal width when the setting is off and reacts when it turns on", () => {
    writeWidenWorkspaceForContent(false);

    const { result } = renderHook(
      ({ wide }) => useResizableInlinePanel(SESSION, undefined, 320, true, wide),
      { initialProps: { wide: true } },
    );

    expect(result.current.panelWidth).toBe(600);

    act(() => writeWidenWorkspaceForContent(true));
    expect(result.current.panelWidth).toBe(840);
  });

  it("persists only the wide width when dragged in wide mode", () => {
    const { result } = renderHook(
      ({ wide }) => useResizableInlinePanel(SESSION, undefined, 320, true, wide),
      { initialProps: { wide: true } },
    );

    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 1000 })));
    act(() => window.dispatchEvent(new MouseEvent("mouseup")));

    // 2000 - 1000 = 1000, within the 1192 sidebar-aware ceiling.
    expect(result.current.panelWidth).toBe(1000);
    expect(readPanelSizePreference("inlinePanelWideWidthPx")).toBe(1000);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();
  });

  it("keeps a drag in wide mode when the rail switches to normal content mid-drag", async () => {
    const { result, rerender } = renderHook(
      ({ wide }) => useResizableInlinePanel(SESSION, undefined, 320, true, wide),
      { initialProps: { wide: true } },
    );

    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 1000 })));
    // Let the coalescing rAF flush run before the mode flips.
    await act(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => resolve());
        }),
    );
    rerender({ wide: false });
    act(() => window.dispatchEvent(new MouseEvent("mouseup")));

    // The width is written to the wide store, never the normal one.
    expect(readPanelSizePreference("inlinePanelWideWidthPx")).toBe(1000);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();
  });

  it("still writes the wide width when mouseup arrives before the queued frame", () => {
    const { result, rerender } = renderHook(
      ({ wide }) => useResizableInlinePanel(SESSION, undefined, 320, true, wide),
      { initialProps: { wide: true } },
    );

    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 1000 })));
    // Release with the move still queued: stop()'s flush must still target wide.
    rerender({ wide: false });
    act(() => window.dispatchEvent(new MouseEvent("mouseup")));

    expect(readPanelSizePreference("inlinePanelWideWidthPx")).toBe(1000);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();
  });

  it("keeps the comments floor in wide mode without overwriting the saved wide width", () => {
    writePanelSizePreference("inlinePanelWideWidthPx", 500);
    resetWidthStoreForTesting();

    const { result } = renderHook(
      ({ wide }) => useResizableInlinePanel(SESSION, 720, 320, true, wide),
      { initialProps: { wide: true } },
    );

    // The 720 comments floor wins over the saved 500, which stays on disk.
    expect(result.current.panelWidth).toBe(720);
    expect(readPanelSizePreference("inlinePanelWideWidthPx")).toBe(500);
  });
});

describe("useResizableInlinePanel drag overlay", () => {
  const overlaySelector = () =>
    [...document.body.children].find(
      (c): c is HTMLElement =>
        c instanceof HTMLElement && c.style.position === "fixed" && c.style.zIndex === "2147483647",
    ) ?? null;

  it("ignores drag input while persistence is disabled for a tentative key", () => {
    const sessionId = "conv_tentative";
    const { result, unmount } = renderHook(() =>
      useResizableInlinePanel(sessionId, undefined, 0, false),
    );
    const initialWidth = result.current.panelWidth;
    expect(result.current.handleProps["aria-disabled"]).toBe(true);
    expect(result.current.handleProps.tabIndex).toBe(-1);

    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    expect(overlaySelector()).toBeNull();

    act(() => {
      window.dispatchEvent(new MouseEvent("mousemove", { clientX: 100 }));
      window.dispatchEvent(new MouseEvent("mouseup"));
    });
    expect(result.current.panelWidth).toBe(initialWidth);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();
    unmount();
  });

  it("freezes and does not persist a drag when its key becomes tentative", async () => {
    const sessionId = "conv_mid_drag";
    const { result, rerender, unmount } = renderHook(
      ({ persistEnabled }) => useResizableInlinePanel(sessionId, undefined, 0, persistEnabled),
      { initialProps: { persistEnabled: true } },
    );

    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    act(() => window.dispatchEvent(new MouseEvent("mousemove", { clientX: 1200 })));
    await waitFor(() => expect(result.current.panelWidth).toBe(800));
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();

    rerender({ persistEnabled: false });
    act(() => {
      window.dispatchEvent(new MouseEvent("mousemove", { clientX: 1000 }));
      window.dispatchEvent(new MouseEvent("mouseup"));
    });
    expect(result.current.panelWidth).toBe(800);
    expect(readPanelSizePreference("inlinePanelWidthPx")).toBeNull();
    unmount();
  });

  it("mounts a full-window overlay during a drag so mouseup isn't lost to an iframe", () => {
    // The panel sits beside the sandboxed HTML-preview iframe. Without an
    // overlay, dragging over the frame routes mousemove/mouseup into it and the
    // parent never sees the release, so the drag sticks to the cursor.
    const { result, unmount } = renderHook(() => useResizableInlinePanel(SESSION));
    expect(overlaySelector()).toBeNull();

    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    expect(result.current.isDragging).toBe(true);
    const overlay = overlaySelector();
    expect(overlay).not.toBeNull();
    expect(overlay?.style.cursor).toBe("col-resize");

    act(() => window.dispatchEvent(new MouseEvent("mouseup")));
    expect(result.current.isDragging).toBe(false);
    expect(overlaySelector()).toBeNull();
    unmount();
  });

  it("removes the overlay if unmounted mid-drag", () => {
    const { result, unmount } = renderHook(() => useResizableInlinePanel(SESSION));
    act(() =>
      result.current.handleProps.onMouseDown({ preventDefault: () => {} } as React.MouseEvent),
    );
    expect(overlaySelector()).not.toBeNull();

    // Panel closes (e.g. tab switch) while still dragging — cleanup must not
    // leave the transparent overlay swallowing every click on the page.
    unmount();
    expect(overlaySelector()).toBeNull();
  });
});
