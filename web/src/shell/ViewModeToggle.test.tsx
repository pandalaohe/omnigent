import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ALT_KEY, ARIA_MOD_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { DropdownMenu, DropdownMenuContent } from "@/components/ui/dropdown-menu";
import { TooltipProvider } from "@/components/ui/tooltip";
import { writeShortcutPreference } from "@/lib/keyboardShortcutPreferences";
import { ViewModeMenuItems, ViewModeToggle } from "./ViewModeToggle";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "./TerminalFirstContext";

const { isMobileMock, trackClickMock } = vi.hoisted(() => ({
  isMobileMock: vi.fn(() => false),
  trackClickMock: vi.fn(),
}));
vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => isMobileMock(),
}));
vi.mock("@/lib/analytics", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useOmnigentAnalytics: () => ({ trackClick: trackClickMock }),
}));

function makeCtx(overrides: Partial<TerminalFirstContextValue> = {}): TerminalFirstContextValue {
  return {
    isClaudeNative: true,
    isNativeWrapper: true,
    isTerminalFirst: true,
    isShellView: false,
    view: "chat",
    terminalViewKey: null,
    setView: vi.fn(),
    terminalsAvailable: true,
    terminalStartingUp: false,
    ...overrides,
  };
}

function renderToggle(ctx: TerminalFirstContextValue | null) {
  return render(
    <TooltipProvider>
      {ctx ? (
        <TerminalFirstContextProvider value={ctx}>
          <ViewModeToggle />
        </TerminalFirstContextProvider>
      ) : (
        <ViewModeToggle />
      )}
    </TooltipProvider>,
  );
}

/** The Chat segment — icon-only, so it's addressed by its accessible name. */
function chatSegment() {
  return screen.getByRole("radio", { name: /^chat view$/i });
}

/** The Terminal segment. Its name doubles as the starting-up explanation. */
function terminalSegment() {
  return screen.getByRole("radio", { name: /^terminal (view|is starting up…)$/i });
}

/** Renders the menu-items variant inside an open dropdown so the items mount. */
function renderMenuItems(ctx: TerminalFirstContextValue | null) {
  return render(
    <TooltipProvider>
      <DropdownMenu open>
        <DropdownMenuContent>
          {ctx ? (
            <TerminalFirstContextProvider value={ctx}>
              <ViewModeMenuItems />
            </TerminalFirstContextProvider>
          ) : (
            <ViewModeMenuItems />
          )}
        </DropdownMenuContent>
      </DropdownMenu>
    </TooltipProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  isMobileMock.mockReturnValue(false);
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.clearAllMocks();
});

describe("ViewModeToggle", () => {
  it("renders both segments for terminal-first sessions", () => {
    renderToggle(makeCtx());
    expect(
      screen.getByRole("radiogroup", { name: /switch between chat and terminal/i }),
    ).toBeVisible();
    expect(chatSegment()).toBeVisible();
    expect(terminalSegment()).toBeVisible();
  });

  it("renders nothing for a non-terminal-first session", () => {
    const { container } = renderToggle(makeCtx({ isTerminalFirst: false }));
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing on mobile — the switch folds into the header kebab", () => {
    isMobileMock.mockReturnValue(true);
    const { container } = renderToggle(makeCtx());
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing outside a provider", () => {
    const { container } = renderToggle(null);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing while a shell owns the main view (isShellView)", () => {
    const { container } = renderToggle(makeCtx({ isShellView: true, view: "terminal" }));
    expect(container).toBeEmptyDOMElement();
  });

  it("renders in the iOS shell — the header is the switcher's one placement", () => {
    // The native bottom pill below the composer is retired; iOS gets the same
    // header toggle as the web UI.
    (window as unknown as Record<string, unknown>).omnigentNative = { kind: "ios" };
    try {
      renderToggle(makeCtx());
      expect(screen.getByTestId("view-mode-toggle")).toBeVisible();
    } finally {
      delete (window as unknown as Record<string, unknown>).omnigentNative;
    }
  });

  it("checks only the active segment and gives it the one tab stop in chat view", () => {
    renderToggle(makeCtx({ view: "chat" }));
    expect(chatSegment()).toHaveAttribute("aria-checked", "true");
    expect(chatSegment()).toHaveAttribute("tabindex", "0");
    expect(terminalSegment()).toHaveAttribute("aria-checked", "false");
    expect(terminalSegment()).toHaveAttribute("tabindex", "-1");
  });

  it("checks only the active segment in the terminal view", () => {
    renderToggle(makeCtx({ view: "terminal" }));
    expect(terminalSegment()).toHaveAttribute("aria-checked", "true");
    expect(chatSegment()).toHaveAttribute("aria-checked", "false");
  });

  it("switches to the terminal view in one click — no menu to open", () => {
    const setView = vi.fn();
    renderToggle(makeCtx({ setView, view: "chat" }));
    fireEvent.click(terminalSegment());
    expect(setView).toHaveBeenCalledWith("terminal");
  });

  it("switches back to the chat view in one click", () => {
    const setView = vi.fn();
    renderToggle(makeCtx({ setView, view: "terminal" }));
    fireEvent.click(chatSegment());
    expect(setView).toHaveBeenCalledWith("chat");
  });

  it("keeps the existing component analytics when a segment is chosen", () => {
    renderToggle(makeCtx({ view: "chat" }));
    fireEvent.click(terminalSegment());
    expect(trackClickMock).toHaveBeenCalledWith("chat.header.view_terminal", "button");
  });

  it("uses arrow keys to change the view and keep focus on the selected radio", () => {
    const setViewMock = vi.fn();
    function StatefulToggle() {
      const [view, setView] = useState<TerminalFirstContextValue["view"]>("chat");
      return (
        <TerminalFirstContextProvider
          value={makeCtx({
            view,
            setView: (next) => {
              setViewMock(next);
              setView(next);
            },
          })}
        >
          <ViewModeToggle />
        </TerminalFirstContextProvider>
      );
    }
    render(
      <TooltipProvider>
        <StatefulToggle />
      </TooltipProvider>,
    );
    const chat = screen.getByTestId("view-mode-chat");
    const terminal = screen.getByTestId("view-mode-terminal");

    fireEvent.keyDown(chat, { key: "ArrowRight" });
    expect(setViewMock).toHaveBeenCalledWith("terminal");
    expect(terminal).toHaveFocus();
    expect(terminal).toHaveAttribute("aria-checked", "true");
    expect(terminal).toHaveAttribute("tabindex", "0");
    expect(chat).toHaveAttribute("tabindex", "-1");
  });

  it("names the focused radio with its label and shortcut keys", async () => {
    renderToggle(makeCtx({ view: "chat" }));
    fireEvent.focus(chatSegment());
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("Chat view");
    const keys = Array.from(tooltip.querySelectorAll<HTMLElement>('[data-slot="kbd"]'));
    expect(keys.map((key) => key.textContent)).toEqual([MOD_KEY, ALT_KEY, "\\"]);
  });

  it("does not reopen a tooltip after the toggle is hidden and shown", async () => {
    const element = () => (
      <TooltipProvider>
        <TerminalFirstContextProvider value={makeCtx()}>
          <ViewModeToggle />
        </TerminalFirstContextProvider>
      </TooltipProvider>
    );
    const { rerender } = render(element());
    fireEvent.focus(chatSegment());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Chat view");

    isMobileMock.mockReturnValue(true);
    rerender(element());
    expect(screen.queryByTestId("view-mode-chat")).toBeNull();

    isMobileMock.mockReturnValue(false);
    rerender(element());
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("syncs shortcut ARIA when a mobile-mounted toggle becomes visible on desktop", () => {
    isMobileMock.mockReturnValue(true);
    const element = () => (
      <TooltipProvider>
        <TerminalFirstContextProvider value={makeCtx()}>
          <ViewModeToggle />
        </TerminalFirstContextProvider>
      </TooltipProvider>
    );
    const { rerender } = render(element());
    expect(screen.queryByTestId("view-mode-chat")).toBeNull();

    isMobileMock.mockReturnValue(false);
    rerender(element());
    expect(chatSegment()).toHaveAttribute("aria-keyshortcuts", `${ARIA_MOD_KEY}+Alt+\\`);
  });

  it("syncs shortcut ARIA when the session becomes terminal-first", () => {
    const { rerender } = render(
      <TooltipProvider>
        <TerminalFirstContextProvider value={makeCtx({ isTerminalFirst: false })}>
          <ViewModeToggle />
        </TerminalFirstContextProvider>
      </TooltipProvider>,
    );
    expect(screen.queryByTestId("view-mode-chat")).toBeNull();

    rerender(
      <TooltipProvider>
        <TerminalFirstContextProvider value={makeCtx({ isTerminalFirst: true })}>
          <ViewModeToggle />
        </TerminalFirstContextProvider>
      </TooltipProvider>,
    );
    expect(chatSegment()).toHaveAttribute("aria-keyshortcuts", `${ARIA_MOD_KEY}+Alt+\\`);
  });

  it("names each segment on hover so the icon-only control is legible", async () => {
    renderToggle(makeCtx({ view: "chat" }));
    expect(chatSegment()).toHaveAttribute("aria-keyshortcuts", `${ARIA_MOD_KEY}+Alt+\\`);
    // The styled tooltip stays on the icon content inside the shared radio.
    fireEvent.pointerMove(chatSegment().querySelector('[data-slot="tooltip-trigger"]')!, {
      pointerType: "mouse",
    });
    const tooltip = await screen.findByRole("tooltip");
    expect(tooltip).toHaveTextContent("Chat view");
    expect(tooltip).toHaveClass("menu-surface", "bg-popover", "text-popover-foreground");
    expect(tooltip).not.toHaveClass("bg-neutral-900", "text-white");
    const keys = Array.from(tooltip.querySelectorAll<HTMLElement>('[data-slot="kbd"]'));
    expect(keys.map((key) => key.textContent)).toEqual([MOD_KEY, ALT_KEY, "\\"]);
    for (const key of keys) {
      expect(key).toHaveClass("bg-muted", "text-muted-foreground");
    }
  });

  it("follows a rebound view-mode chord in the segment hints", async () => {
    writeShortcutPreference("toggleViewMode", {
      common: [{ code: "KeyJ", modifiers: ["primary", "shift"] }],
    });
    renderToggle(makeCtx({ view: "chat" }));

    const chat = chatSegment();
    expect(chat).toHaveAttribute("aria-keyshortcuts", "Control+Shift+J");
    fireEvent.pointerMove(chat.querySelector('[data-slot="tooltip-trigger"]')!, {
      pointerType: "mouse",
    });
    const tooltip = await screen.findByRole("tooltip");
    expect(
      Array.from(tooltip.querySelectorAll('[data-slot="kbd"]'), (key) => key.textContent),
    ).toEqual(["Ctrl", "⇧", "J"]);
  });

  it("keeps the Terminal segment usable and shows a spinner while the terminal is coming up", () => {
    const setView = vi.fn();
    renderToggle(makeCtx({ setView, terminalsAvailable: false, terminalStartingUp: true }));
    const terminal = terminalSegment();
    expect(terminal).toBeEnabled();
    expect(terminal).toHaveAccessibleName(/starting up/i);
    expect(terminal.querySelector(".animate-spin")).not.toBeNull();
    fireEvent.click(terminal);
    expect(setView).toHaveBeenCalledWith("terminal");
  });

  it("keeps the Terminal segment usable when the runner is offline", () => {
    const setView = vi.fn();
    renderToggle(makeCtx({ setView, terminalsAvailable: false, terminalStartingUp: false }));
    const terminal = terminalSegment();
    expect(terminal).toBeEnabled();
    expect(terminal.querySelector(".animate-spin")).toBeNull();
    fireEvent.click(terminal);
    expect(setView).toHaveBeenCalledWith("terminal");
  });

  it("leaves the Chat segment usable while the terminal is unavailable", () => {
    const setView = vi.fn();
    renderToggle(makeCtx({ setView, terminalsAvailable: false, view: "terminal" }));
    fireEvent.click(chatSegment());
    expect(setView).toHaveBeenCalledWith("chat");
  });
});

describe("ViewModeMenuItems", () => {
  it("renders Chat and Terminal entries for terminal-first sessions", () => {
    renderMenuItems(makeCtx());
    expect(screen.getByTestId("view-mode-menu-chat")).toBeVisible();
    expect(screen.getByTestId("view-mode-menu-terminal")).toBeVisible();
  });

  it("renders nothing for a non-terminal-first session", () => {
    renderMenuItems(makeCtx({ isTerminalFirst: false }));
    expect(screen.queryByTestId("view-mode-menu-chat")).toBeNull();
  });

  it("keeps Chat and the agent Terminal reachable while a shell owns the main view", () => {
    const setView = vi.fn();
    renderMenuItems(makeCtx({ isShellView: true, view: "terminal", setView }));
    fireEvent.click(screen.getByTestId("view-mode-menu-chat"));
    expect(setView).toHaveBeenLastCalledWith("chat");
    fireEvent.click(screen.getByTestId("view-mode-menu-terminal"));
    expect(setView).toHaveBeenLastCalledWith("terminal");
  });

  it("switches to the terminal view when its entry is chosen", () => {
    const setView = vi.fn();
    renderMenuItems(makeCtx({ setView, view: "chat" }));
    fireEvent.click(screen.getByTestId("view-mode-menu-terminal"));
    expect(setView).toHaveBeenCalledWith("terminal");
  });

  it("switches back to the chat view when its entry is chosen", () => {
    const setView = vi.fn();
    renderMenuItems(makeCtx({ setView, view: "terminal" }));
    fireEvent.click(screen.getByTestId("view-mode-menu-chat"));
    expect(setView).toHaveBeenCalledWith("chat");
  });

  it("shows a spinner on the Terminal entry while the terminal is coming up", () => {
    renderMenuItems(makeCtx({ terminalsAvailable: false, terminalStartingUp: true }));
    const terminal = screen.getByTestId("view-mode-menu-terminal");
    expect(terminal).toHaveTextContent(/starting up/i);
    expect(terminal.querySelector(".animate-spin")).not.toBeNull();
  });
});
