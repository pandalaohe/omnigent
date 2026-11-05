import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KeyboardShortcutEditor } from "./KeyboardShortcutEditor";
import {
  isShortcutRecordingActive,
  readKeyboardShortcutPreferences,
  readMacWindowsKeyPositions,
  resolveShortcutBindings,
  writeShortcutPreference,
} from "@/lib/keyboardShortcutPreferences";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "@/lib/composerSendShortcutPreferences";

function actionRow(label: string): HTMLElement {
  const row = screen.getByText(label).closest('[data-testid^="shortcut-editor-row-"]');
  expect(row).toBeTruthy();
  return row as HTMLElement;
}

function renderExpandedEditor() {
  const view = render(<KeyboardShortcutEditor />);
  view.container.querySelectorAll("summary").forEach((summary) => fireEvent.click(summary));
  return view;
}

describe("KeyboardShortcutEditor", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("starts with six folded groups and opens search matches", () => {
    const { container } = render(<KeyboardShortcutEditor />);
    expect(container.querySelectorAll("details")).toHaveLength(6);
    expect(container.querySelectorAll("details[open]")).toHaveLength(0);
    fireEvent.change(screen.getByRole("textbox", { name: "Search shortcuts" }), {
      target: { value: "poll sessions" },
    });
    expect(
      screen.getByRole("button", { name: "Record common shortcut for Poll sessions" }),
    ).toBeVisible();
    expect(
      screen
        .getByLabelText("Polling window")
        .closest('[data-testid="shortcut-editor-row-pollSessions"]'),
    ).not.toBeNull();
    expect(screen.queryByTestId("shortcut-editor-row-newSession")).toBeNull();
    expect(
      screen.queryByRole("switch", { name: "Scroll to the bottom when opening a session" }),
    ).toBeNull();
  });

  it("finds a platform key, filters changed rows and resets the entire row", () => {
    writeShortcutPreference("newSession", {
      common: [{ code: "F9", modifiers: [] }],
      enabled: false,
      platformOverrides: { macos: [{ code: "F10", modifiers: [] }] },
    });
    writeShortcutPreference("commandPalette", { common: [{ code: "F11", modifiers: [] }] });
    render(<KeyboardShortcutEditor />);
    const search = screen.getByRole("textbox", { name: "Search shortcuts" });
    fireEvent.change(search, { target: { value: "F10" } });
    expect(screen.getByTestId("shortcut-editor-row-newSession")).toBeVisible();
    expect(screen.queryByTestId("shortcut-editor-row-commandPalette")).toBeNull();
    fireEvent.change(search, { target: { value: "" } });
    fireEvent.click(screen.getByRole("switch", { name: "Changed only" }));
    expect(screen.getByTestId("shortcut-editor-row-commandPalette")).toBeVisible();
    fireEvent.click(
      screen.getByRole("button", { name: "Restore default shortcut for Start a new session" }),
    );
    expect(readKeyboardShortcutPreferences().actions.newSession).toBeUndefined();
    expect(screen.queryByTestId("shortcut-editor-row-newSession")).toBeNull();
    expect(readKeyboardShortcutPreferences().actions.commandPalette?.common).toEqual([
      { code: "F11", modifiers: [] },
    ]);
  });

  it("cancels recording when its action is filtered away", () => {
    renderExpandedEditor();
    fireEvent.click(
      screen.getByRole("button", { name: "Record common shortcut for Start a new session" }),
    );
    expect(isShortcutRecordingActive()).toBe(true);
    fireEvent.change(screen.getByRole("textbox", { name: "Search shortcuts" }), {
      target: { value: "poll" },
    });
    fireEvent.keyDown(window, { key: "F9", code: "F9" });
    expect(isShortcutRecordingActive()).toBe(false);
    expect(readKeyboardShortcutPreferences().actions.newSession).toBeUndefined();
  });

  it("lists the question-card group and records its actions", () => {
    renderExpandedEditor();

    expect(actionRow("Focus question card")).toBeTruthy();
    expect(within(actionRow("Previous option")).getByText("↑")).toBeTruthy();
    expect(within(actionRow("Cancel question")).getByText("Not set")).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "Record common shortcut for Select option" }),
    );
    fireEvent.keyDown(window, { key: "x", code: "KeyX" });

    expect(resolveShortcutBindings("questionCardSelectOption", "windows")).toEqual([
      { code: "KeyX", modifiers: [] },
    ]);
  });

  it("rejects a card chord already used by another card action", () => {
    renderExpandedEditor();

    fireEvent.click(screen.getByRole("button", { name: "Record common shortcut for Next option" }));
    fireEvent.keyDown(window, { key: "ArrowUp", code: "ArrowUp" });

    expect(screen.getByText(/already used by Previous option/i)).toBeTruthy();
  });

  it("shows the effective default in the common row", () => {
    renderExpandedEditor();

    const row = actionRow("Start a new session");
    expect(within(row).getByText("Ctrl")).toBeTruthy();
    expect(within(row).getByText("O")).toBeTruthy();
    expect(within(row).getByText("⇧")).toBeTruthy();
  });

  it("records and persists a common shortcut inline", () => {
    renderExpandedEditor();

    fireEvent.click(
      screen.getByRole("button", { name: "Record common shortcut for Start a new session" }),
    );
    fireEvent.keyDown(window, { key: "p", code: "KeyP", altKey: true });

    expect(resolveShortcutBindings("newSession", "windows")).toEqual([
      { code: "KeyP", modifiers: ["alt"] },
    ]);
    expect(within(actionRow("Start a new session")).getByText("P")).toBeTruthy();
  });

  it("adds, records, and deletes a platform-specific override", () => {
    renderExpandedEditor();

    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Add system override for Start a new session" }),
      { button: 0, ctrlKey: false },
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "macOS" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Windows" }));
    fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });

    const platformRow = screen.getByTestId("shortcut-platform-newSession-macos");
    expect(screen.getByTestId("shortcut-platform-newSession-windows")).toBeTruthy();
    fireEvent.click(
      within(platformRow).getByRole("button", {
        name: "Record macOS shortcut for Start a new session",
      }),
    );
    fireEvent.keyDown(window, { key: "n", code: "KeyN", metaKey: true });

    expect(resolveShortcutBindings("newSession", "macos")).toEqual([
      { code: "KeyN", modifiers: ["meta"] },
    ]);

    fireEvent.click(
      within(platformRow).getByRole("button", {
        name: "Delete macOS override for Start a new session",
      }),
    );
    expect(screen.queryByTestId("shortcut-platform-newSession-macos")).toBeNull();
    expect(
      readKeyboardShortcutPreferences().actions.newSession?.platformOverrides?.macos,
    ).toBeUndefined();
  });

  it("rejects a shortcut already used by another action on the target platform", () => {
    renderExpandedEditor();

    fireEvent.click(
      screen.getByRole("button", { name: "Record common shortcut for Open command palette" }),
    );
    fireEvent.keyDown(window, { key: "O", code: "KeyO", ctrlKey: true, shiftKey: true });

    expect(screen.getByText(/already used by Start a new session/i)).toBeTruthy();
    expect(resolveShortcutBindings("commandPalette", "windows")).toEqual([
      { code: "KeyK", modifiers: ["primary"] },
    ]);
  });

  it("shows the legacy composer preference as the effective reset target", () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    renderExpandedEditor();

    const row = actionRow("Send message");
    expect(within(row).getByText("Ctrl")).toBeTruthy();
    expect(within(row).getByText("↵")).toBeTruthy();
  });

  it("rejects global chords that would collide with composer actions", () => {
    renderExpandedEditor();

    fireEvent.click(
      screen.getByRole("button", { name: "Record common shortcut for Archive current session" }),
    );
    fireEvent.keyDown(window, { key: "Enter", code: "Enter" });

    expect(screen.getByText(/already used by Send message/i)).toBeTruthy();
  });

  it("rejects a non-number pinned-session recording and clears recording mode", () => {
    renderExpandedEditor();

    fireEvent.click(
      screen.getByRole("button", {
        name: "Record common shortcut for Jump to pinned session (1–10)",
      }),
    );
    expect(isShortcutRecordingActive()).toBe(true);
    fireEvent.keyDown(window, { key: "F9", code: "F9" });

    expect(screen.getByText(/must use a number key/i)).toBeTruthy();
    expect(isShortcutRecordingActive()).toBe(false);
  });

  it("renders Not set and the note for an action with no default binding", () => {
    renderExpandedEditor();

    const row = actionRow("Select workspace tab 1");
    expect(within(row).getByText("Not set")).toBeTruthy();
    expect(within(row).getByText(/Focusing the workspace tabs/)).toBeTruthy();
  });

  it("renders the secondary description on the Poll sessions row", () => {
    renderExpandedEditor();

    const row = actionRow("Poll sessions");
    expect(
      within(row).getByText("Jumps to the next session waiting for your answer."),
    ).toBeTruthy();
  });

  it("refuses a Shift chord for the recent-sessions switcher", () => {
    renderExpandedEditor();

    fireEvent.click(
      screen.getByRole("button", { name: "Record common shortcut for Switch recent sessions" }),
    );
    fireEvent.keyDown(window, { key: "Tab", code: "Tab", ctrlKey: true, shiftKey: true });

    expect(screen.getByText(/uses Shift to go back/)).toBeTruthy();
    expect(isShortcutRecordingActive()).toBe(false);
    expect(resolveShortcutBindings("recentSessions", "windows")).toEqual([
      { code: "Tab", modifiers: ["control"] },
    ]);
  });

  it("refuses a modifier-less chord for the recent-sessions switcher", () => {
    renderExpandedEditor();

    fireEvent.click(
      screen.getByRole("button", { name: "Record common shortcut for Switch recent sessions" }),
    );
    fireEvent.keyDown(window, { key: "Tab", code: "Tab" });

    expect(screen.getByText(/needs a modifier to hold/)).toBeTruthy();
    expect(isShortcutRecordingActive()).toBe(false);
    expect(readKeyboardShortcutPreferences().actions.recentSessions).toBeUndefined();
  });

  it("offers Windows key positions on macOS and previews the mapped defaults", () => {
    vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
    renderExpandedEditor();

    const toggle = screen.getByRole("switch", { name: "Use Windows key positions" });
    fireEvent.click(toggle);

    expect(readMacWindowsKeyPositions()).toBe(true);
    const pollRow = actionRow("Poll sessions");
    expect(within(pollRow).getByText("⌘")).toBeTruthy();
    expect(within(pollRow).getByText("~")).toBeTruthy();
    expect(screen.getByText(/macOS uses ⌘` to move focus/)).toBeTruthy();
    expect(screen.getByText("Keeps ⌥W: ⌘W closes the window or tab.")).toBeTruthy();
  });

  it("does not offer Windows key positions off macOS", () => {
    renderExpandedEditor();

    expect(screen.queryByRole("switch", { name: "Use Windows key positions" })).toBeNull();
  });
});
