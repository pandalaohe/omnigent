import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import { writeShortcutPreference } from "@/lib/keyboardShortcutPreferences";

import { RecentSessionsSwitcher } from "./RecentSessionsSwitcher";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));

afterEach(() => {
  cleanup();
  localStorage.clear();
  navigate.mockReset();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

function conversation(
  id: string,
  updatedAt: number,
  options: { archived?: boolean; provisional?: boolean } = {},
): Conversation {
  return {
    id,
    object: "conversation",
    title: `Session ${id}`,
    created_at: updatedAt,
    updated_at: updatedAt,
    labels: {},
    archived: options.archived ?? false,
    provisional: options.provisional,
    agent_name: "Claude",
  } as Conversation;
}

function pressTab(options: { shift?: boolean } = {}) {
  fireEvent.keyDown(window, {
    key: "Tab",
    code: "Tab",
    ctrlKey: true,
    shiftKey: options.shift ?? false,
  });
}

describe("RecentSessionsSwitcher", () => {
  it("shows only the five most recently active real sessions", () => {
    render(
      <RecentSessionsSwitcher
        conversations={[
          conversation("one", 1),
          conversation("seven", 7, { archived: true }),
          conversation("three", 3),
          conversation("six", 6, { provisional: true }),
          conversation("five", 5),
          conversation("two", 2),
          conversation("four", 4),
        ]}
        activeSessionId={null}
        enabled
      />,
    );

    pressTab();

    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Session fiveClaude",
      "Session fourClaude",
      "Session threeClaude",
      "Session twoClaude",
      "Session oneClaude",
    ]);
  });

  it("cycles from the active session, reverses with Shift, and switches on Control release", () => {
    render(
      <RecentSessionsSwitcher
        conversations={[
          conversation("three", 3),
          conversation("one", 1),
          conversation("four", 4),
          conversation("two", 2),
        ]}
        activeSessionId="four"
        enabled
      />,
    );

    pressTab();
    expect(screen.getByText("Session three").closest('[role="option"]')).toHaveAttribute(
      "data-selected",
      "true",
    );

    pressTab();
    expect(screen.getByText("Session two").closest('[role="option"]')).toHaveAttribute(
      "data-selected",
      "true",
    );

    pressTab({ shift: true });
    expect(screen.getByText("Session three").closest('[role="option"]')).toHaveAttribute(
      "data-selected",
      "true",
    );

    fireEvent.keyUp(window, { key: "Control", code: "ControlLeft" });
    expect(navigate).toHaveBeenCalledWith("/c/three");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancels with Escape without switching on the later Control release", () => {
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );

    pressTab();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape", code: "Escape", ctrlKey: true });
    fireEvent.keyUp(window, { key: "Control", code: "ControlLeft" });

    expect(navigate).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("cancels without switching when the window loses focus", () => {
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );

    pressTab();
    fireEvent.blur(window);
    fireEvent.keyUp(window, { key: "Control", code: "ControlLeft" });

    expect(navigate).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("switches from keyboard input forwarded by an embedded Browser page", () => {
    let forwardInput: ((input: Record<string, unknown>) => void) | undefined;
    const unsubscribe = vi.fn();
    const setSupported = vi.fn().mockResolvedValue({ ok: true });
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      onBrowserRecentSessionInput: (callback: (input: Record<string, unknown>) => void) => {
        forwardInput = callback;
        return unsubscribe;
      },
      browserSetRecentSessionSwitchSupported: setSupported,
    };
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );
    expect(setSupported).toHaveBeenCalledWith(true);

    act(() => {
      forwardInput?.({
        type: "keydown",
        key: "Tab",
        code: "Tab",
        ctrlKey: true,
        shiftKey: false,
        altKey: false,
        metaKey: false,
        repeat: false,
      });
    });
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    act(() => {
      forwardInput?.({
        type: "keyup",
        key: "Control",
        code: "ControlLeft",
        ctrlKey: false,
        shiftKey: false,
        altKey: false,
        metaKey: false,
        repeat: false,
      });
    });

    expect(navigate).toHaveBeenCalledWith("/c/one");
    cleanup();
    expect(unsubscribe).toHaveBeenCalledOnce();
    expect(setSupported).toHaveBeenLastCalledWith(false);
  });

  it("stops advertising embedded-browser support once the switcher is rebound", () => {
    const setSupported = vi.fn().mockResolvedValue({ ok: true });
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      onBrowserRecentSessionInput: () => () => {},
      browserSetRecentSessionSwitchSupported: setSupported,
    };
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );
    expect(setSupported).toHaveBeenLastCalledWith(true);

    act(() => {
      writeShortcutPreference("recentSessions", {
        common: [{ code: "KeyJ", modifiers: ["primary"] }],
      });
    });

    expect(setSupported).toHaveBeenLastCalledWith(false);
  });

  it("keeps embedded-browser support when the override targets another platform", () => {
    const setSupported = vi.fn().mockResolvedValue({ ok: true });
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      onBrowserRecentSessionInput: () => () => {},
      browserSetRecentSessionSwitchSupported: setSupported,
    };
    // The test platform is not macOS, so a macOS-only recording leaves the
    // effective chord at the default Ctrl+Tab.
    writeShortcutPreference("recentSessions", {
      platformOverrides: { macos: [{ code: "KeyJ", modifiers: ["primary"] }] },
    });
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );

    expect(setSupported).toHaveBeenLastCalledWith(true);
  });

  it("keeps embedded-browser support for an explicit Ctrl+Tab recording", () => {
    const setSupported = vi.fn().mockResolvedValue({ ok: true });
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      onBrowserRecentSessionInput: () => () => {},
      browserSetRecentSessionSwitchSupported: setSupported,
    };
    writeShortcutPreference("recentSessions", {
      common: [{ code: "Tab", modifiers: ["control"] }],
    });
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );

    expect(setSupported).toHaveBeenLastCalledWith(true);
  });

  it("releases native interception when a forwarded gesture has no sessions", () => {
    let forwardInput: ((input: Record<string, unknown>) => void) | undefined;
    const cancelRecentSessionSwitch = vi.fn().mockResolvedValue({ ok: true });
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      onBrowserRecentSessionInput: (callback: (input: Record<string, unknown>) => void) => {
        forwardInput = callback;
        return vi.fn();
      },
      browserCancelRecentSessionSwitch: cancelRecentSessionSwitch,
    };
    render(<RecentSessionsSwitcher conversations={[]} activeSessionId={null} enabled />);

    act(() => {
      forwardInput?.({
        type: "keydown",
        key: "Tab",
        code: "Tab",
        ctrlKey: true,
        shiftKey: false,
        altKey: false,
        metaKey: false,
        repeat: false,
      });
    });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(cancelRecentSessionSwitch).toHaveBeenCalledOnce();
  });

  it("opens, cycles, and commits with a recorded chord and labels the footer from it", () => {
    writeShortcutPreference("recentSessions", {
      common: [{ code: "KeyJ", modifiers: ["primary"] }],
    });
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("three", 3), conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );

    // The default Ctrl+Tab no longer opens the switcher.
    pressTab();
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.keyDown(window, { key: "j", code: "KeyJ", ctrlKey: true });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(
      screen.getByText("Press J to cycle · Release Ctrl to switch · Esc to cancel"),
    ).toBeInTheDocument();
    expect(screen.getByText("Session one").closest('[role="option"]')).toHaveAttribute(
      "data-selected",
      "true",
    );

    // A second press of the cycle chord moves the selection on.
    fireEvent.keyDown(window, { key: "j", code: "KeyJ", ctrlKey: true });
    expect(screen.getByText("Session three").closest('[role="option"]')).toHaveAttribute(
      "data-selected",
      "true",
    );

    // Releasing the held modifier commits the selection.
    fireEvent.keyUp(window, { key: "Control", code: "ControlLeft" });

    expect(navigate).toHaveBeenCalledWith("/c/three");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("commits on release of the modifier from the binding that opened the switcher", () => {
    writeShortcutPreference("recentSessions", {
      common: [
        { code: "KeyJ", modifiers: ["primary"] },
        { code: "KeyK", modifiers: ["alt"] },
      ],
    });
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );

    fireEvent.keyDown(window, { key: "k", code: "KeyK", altKey: true });

    expect(
      screen.getByText("Press K to cycle · Release Alt to switch · Esc to cancel"),
    ).toBeInTheDocument();
    expect(screen.getByText("Session one").closest('[role="option"]')).toHaveAttribute(
      "data-selected",
      "true",
    );

    fireEvent.keyUp(window, { key: "Alt", code: "AltLeft" });

    expect(navigate).toHaveBeenCalledWith("/c/one");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("ignores an AltGr event that matches a custom chord", () => {
    writeShortcutPreference("recentSessions", {
      common: [{ code: "KeyJ", modifiers: ["control", "alt"] }],
    });
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );

    const event = new KeyboardEvent("keydown", {
      key: "j",
      code: "KeyJ",
      ctrlKey: true,
      altKey: true,
      bubbles: true,
      cancelable: true,
    });
    Object.defineProperty(event, "getModifierState", {
      value: (key: string) => key === "AltGraph",
    });
    act(() => {
      window.dispatchEvent(event);
    });

    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("never opens while the action is disabled", () => {
    writeShortcutPreference("recentSessions", { enabled: false });
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("two", 2), conversation("one", 1)]}
        activeSessionId="two"
        enabled
      />,
    );

    fireEvent.keyDown(window, { key: "Tab", code: "Tab", ctrlKey: true });

    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("leaves Ctrl+Tab untouched outside Electron", () => {
    render(
      <RecentSessionsSwitcher
        conversations={[conversation("one", 1)]}
        activeSessionId={null}
        enabled={false}
      />,
    );
    const keyboardEvent = new KeyboardEvent("keydown", {
      key: "Tab",
      code: "Tab",
      ctrlKey: true,
      bubbles: true,
      cancelable: true,
    });

    window.dispatchEvent(keyboardEvent);

    expect(keyboardEvent.defaultPrevented).toBe(false);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
