import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { writeShortcutPreference } from "@/lib/keyboardShortcutPreferences";

import {
  SELECT_WORKSPACE_TAB_ACTION_EVENT,
  dispatchSelectWorkspaceTab,
  useWorkspaceTabHotkeys,
} from "./useWorkspaceTabHotkeys";

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

function press(init: KeyboardEventInit): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
  window.dispatchEvent(event);
  return event;
}

describe("useWorkspaceTabHotkeys", () => {
  it("fires the bound chord with its tab number and claims the event", () => {
    const onSelect = vi.fn();
    renderHook(() => useWorkspaceTabHotkeys(onSelect, true));
    writeShortcutPreference("selectWorkspaceTab2", {
      common: [{ code: "KeyJ", modifiers: ["primary", "shift"] }],
    });

    const event = press({ key: "j", code: "KeyJ", ctrlKey: true, shiftKey: true });

    expect(onSelect).toHaveBeenCalledWith(2);
    expect(event.defaultPrevented).toBe(true);
  });

  it("ignores the unbound defaults", () => {
    const onSelect = vi.fn();
    renderHook(() => useWorkspaceTabHotkeys(onSelect, true));

    press({ key: "2", code: "Digit2" });

    expect(onSelect).not.toHaveBeenCalled();
  });

  it("does not re-fire on auto-repeat", () => {
    const onSelect = vi.fn();
    renderHook(() => useWorkspaceTabHotkeys(onSelect, true));
    writeShortcutPreference("selectWorkspaceTab1", {
      common: [{ code: "KeyJ", modifiers: ["primary", "shift"] }],
    });

    press({ key: "j", code: "KeyJ", ctrlKey: true, shiftKey: true, repeat: true });

    expect(onSelect).not.toHaveBeenCalled();
  });

  it("responds to the dispatched action event with its tab number", () => {
    const onSelect = vi.fn();
    renderHook(() => useWorkspaceTabHotkeys(onSelect, true));

    dispatchSelectWorkspaceTab(2);

    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it("stays inert while disabled", () => {
    const onSelect = vi.fn();
    renderHook(() => useWorkspaceTabHotkeys(onSelect, false));
    writeShortcutPreference("selectWorkspaceTab3", {
      common: [{ code: "KeyJ", modifiers: ["primary", "shift"] }],
    });

    press({ key: "j", code: "KeyJ", ctrlKey: true, shiftKey: true });
    window.dispatchEvent(
      new CustomEvent(SELECT_WORKSPACE_TAB_ACTION_EVENT, { detail: { tabNumber: 3 } }),
    );

    expect(onSelect).not.toHaveBeenCalled();
  });
});
