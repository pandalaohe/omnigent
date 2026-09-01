import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { KeyboardShortcutEditor } from "./KeyboardShortcutEditor";
import {
  isShortcutRecordingActive,
  readKeyboardShortcutPreferences,
  resolveShortcutBindings,
} from "@/lib/keyboardShortcutPreferences";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "@/lib/composerSendShortcutPreferences";

function actionRow(label: string): HTMLElement {
  const row = screen.getByText(label).closest('[data-testid^="shortcut-editor-row-"]');
  expect(row).toBeTruthy();
  return row as HTMLElement;
}

describe("KeyboardShortcutEditor", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("shows the effective default in the common row", () => {
    render(<KeyboardShortcutEditor />);

    const row = actionRow("Start a new session");
    expect(within(row).getByText("Ctrl")).toBeTruthy();
    expect(within(row).getByText("N")).toBeTruthy();
  });

  it("records and persists a common shortcut inline", () => {
    render(<KeyboardShortcutEditor />);

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
    render(<KeyboardShortcutEditor />);

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
    render(<KeyboardShortcutEditor />);

    fireEvent.click(
      screen.getByRole("button", { name: "Record common shortcut for Open command palette" }),
    );
    fireEvent.keyDown(window, { key: "n", code: "KeyN", ctrlKey: true });

    expect(screen.getByText(/already used by Start a new session/i)).toBeTruthy();
    expect(resolveShortcutBindings("commandPalette", "windows")).toEqual([
      { code: "KeyK", modifiers: ["primary"] },
    ]);
  });

  it("shows the legacy composer preference as the effective reset target", () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    render(<KeyboardShortcutEditor />);

    const row = actionRow("Send message");
    expect(within(row).getByText("Ctrl")).toBeTruthy();
    expect(within(row).getByText("↵")).toBeTruthy();
  });

  it("rejects global chords that would collide with composer actions", () => {
    render(<KeyboardShortcutEditor />);

    fireEvent.click(
      screen.getByRole("button", { name: "Record common shortcut for Archive current session" }),
    );
    fireEvent.keyDown(window, { key: "Enter", code: "Enter" });

    expect(screen.getByText(/already used by Send message/i)).toBeTruthy();
  });

  it("rejects a non-number pinned-session recording and clears recording mode", () => {
    render(<KeyboardShortcutEditor />);

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
});
