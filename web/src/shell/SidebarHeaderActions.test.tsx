import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { TooltipProvider } from "@/components/ui/tooltip";
import { writeShortcutPreference } from "@/lib/keyboardShortcutPreferences";

import { SidebarHeaderActions } from "./SidebarHeaderActions";

beforeEach(() => {
  localStorage.clear();
});

afterEach(cleanup);

describe("SidebarHeaderActions shortcut hints", () => {
  it.each([
    {
      control: "Search",
      tooltip: "Search",
      keys: [MOD_KEY, "K"],
      aria: MOD_KEY === "⌘" ? "Meta+K" : "Control+K",
    },
    {
      control: "Settings",
      tooltip: "Settings",
      keys: [MOD_KEY, ALT_KEY, ","],
      aria: MOD_KEY === "⌘" ? "Meta+Alt+," : "Control+Alt+,",
    },
    {
      control: "Close sidebar",
      tooltip: "Collapse sidebar",
      keys: [MOD_KEY, ALT_KEY, "["],
      aria: MOD_KEY === "⌘" ? "Meta+Alt+[" : "Control+Alt+[",
    },
  ])("shows the $control shortcut in its tooltip", async ({ control, tooltip, keys, aria }) => {
    render(
      <MemoryRouter>
        <TooltipProvider delayDuration={0}>
          <SidebarHeaderActions expanded onToggle={vi.fn()} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    const trigger = screen.getByLabelText(control);
    expect(trigger).toHaveAttribute("aria-keyshortcuts", aria);
    fireEvent.focus(trigger);
    const content = await screen.findByRole("tooltip");

    expect(content).toHaveTextContent(tooltip);
    expect(
      Array.from(content.querySelectorAll('[data-slot="kbd"]'), (key) => key.textContent),
    ).toEqual(keys);
  });

  it("rebinds the sidebar-toggle and search hints after a preference write", async () => {
    render(
      <MemoryRouter>
        <TooltipProvider delayDuration={0}>
          <SidebarHeaderActions expanded onToggle={vi.fn()} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    const toggle = screen.getByLabelText("Close sidebar");
    const search = screen.getByLabelText("Search");
    act(() => {
      writeShortcutPreference("toggleConversationsSidebar", {
        common: [{ code: "KeyP", modifiers: ["primary", "shift"] }],
      });
      writeShortcutPreference("commandPalette", {
        common: [{ code: "KeyJ", modifiers: ["primary", "shift"] }],
      });
    });

    expect(toggle).toHaveAttribute("aria-keyshortcuts", "Control+Shift+P");
    expect(search).toHaveAttribute("aria-keyshortcuts", "Control+Shift+J");
    fireEvent.focus(search);
    const content = await screen.findByRole("tooltip");
    expect(
      Array.from(content.querySelectorAll('[data-slot="kbd"]'), (key) => key.textContent),
    ).toEqual(["Ctrl", "⇧", "J"]);
  });

  it("rebinds the Settings shortcut hint after a preference write", async () => {
    render(
      <MemoryRouter>
        <TooltipProvider delayDuration={0}>
          <SidebarHeaderActions expanded onToggle={vi.fn()} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    const settings = screen.getByLabelText("Settings");
    expect(settings).toHaveAttribute("aria-keyshortcuts", "Control+Alt+,");

    act(() => {
      writeShortcutPreference("openSettings", {
        common: [{ code: "KeyP", modifiers: ["primary", "shift"] }],
      });
    });

    expect(settings).toHaveAttribute("aria-keyshortcuts", "Control+Shift+P");
    fireEvent.focus(settings);
    const content = await screen.findByRole("tooltip");
    expect(
      Array.from(content.querySelectorAll('[data-slot="kbd"]'), (key) => key.textContent),
    ).toEqual(["Ctrl", "⇧", "P"]);
  });
});
