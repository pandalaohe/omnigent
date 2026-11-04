import { act, cleanup, render, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useCommandPaletteHotkey } from "@/hooks/useCommandPaletteHotkey";
import { useNewSessionHotkey } from "@/hooks/useNewSessionHotkey";
import { useSettingsHotkey } from "@/hooks/useSettingsHotkey";
import { useRenameSessionHotkey } from "@/hooks/useRenameSessionHotkey";
import { useModelPickerHotkey } from "@/hooks/useModelPickerHotkey";
import { useComposerConfigCycleHotkeys } from "@/hooks/useComposerConfigCycleHotkeys";
import { useFocusComposerHotkey } from "@/hooks/useFocusComposerHotkey";
import { useVoiceDictationHotkey } from "@/hooks/useVoiceDictationHotkey";
import { useViewModeToggleHotkey } from "@/hooks/useViewModeToggleHotkey";
import { useSidebarToggleHotkeys } from "@/hooks/useSidebarToggleHotkeys";
import { useNewShellHotkey } from "@/hooks/useNewShellHotkey";
import { currentShortcutPlatform } from "@/lib/keyboardShortcutPreferences";
import {
  dispatchMobileAssistantButton,
  type MobileAssistantActionId,
} from "@/lib/mobileAssistantPreferences";

function Consumers() {
  const [surface, setSurface] = useState("Chat");
  const [view, setView] = useState<"chat" | "terminal">("chat");
  const [sidebar, setSidebar] = useState(false);
  const composer = useRef<HTMLTextAreaElement>(null);
  const location = useLocation();
  const isMac = currentShortcutPlatform() === "macos";
  useNewSessionHotkey(true, isMac);
  useSettingsHotkey(true, isMac);
  useCommandPaletteHotkey(
    () => setSurface("Command palette"),
    true,
    isMac,
    () => setSurface("Session search"),
  );
  useRenameSessionHotkey(() => setSurface("Rename session"), true, isMac);
  useModelPickerHotkey(() => setSurface("Model picker"));
  useComposerConfigCycleHotkeys(
    [
      {
        key: "model",
        label: "Next model",
        checked: false,
        onSelect: () => setSurface("Next model"),
      },
    ],
    [
      {
        key: "effort",
        label: "Next effort",
        checked: false,
        onSelect: () => setSurface("Next effort"),
      },
    ],
  );
  useFocusComposerHotkey(() => composer.current?.focus());
  useVoiceDictationHotkey(() => setSurface("Dictation started"), true, isMac);
  useViewModeToggleHotkey({ enabled: true, view, setView }, isMac);
  useSidebarToggleHotkeys({ onToggleLeft: () => setSidebar((open) => !open) }, isMac);
  useNewShellHotkey(() => setSurface("Shell created"), true, isMac);
  return (
    <>
      <output data-testid="surface">
        {surface} | {view} | {sidebar ? "Sidebar open" : "Sidebar closed"} | {location.pathname}
      </output>
      <textarea ref={composer} aria-label="Composer" />
    </>
  );
}

beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe("mobile offered actions reach their existing consumer hooks", () => {
  it.each([
    ["newSession", "| /"],
    ["openSettings", "| /settings"],
    ["findSession", "Session search"],
    ["commandPalette", "Command palette"],
    ["renameSession", "Rename session"],
    ["openModelPicker", "Model picker"],
    ["cycleModel", "Next model"],
    ["cycleEffort", "Next effort"],
    ["voiceDictation", "Dictation started"],
    ["newShell", "Shell created"],
    ["toggleViewMode", "| terminal |"],
    ["toggleConversationsSidebar", "Sidebar open"],
  ] as const)("%s produces %s", (actionId, expected) => {
    render(
      <MemoryRouter initialEntries={["/c/demo"]}>
        <Consumers />
      </MemoryRouter>,
    );
    act(() =>
      dispatchMobileAssistantButton({
        id: actionId,
        label: actionId,
        binding: { kind: "shortcut", actionId },
      }),
    );
    expect(screen.getByTestId("surface")).toHaveTextContent(expected);
    if (actionId === "newSession")
      expect(screen.getByTestId("surface")).not.toHaveTextContent("/c/demo");
  });
  it("focusComposer focuses the actual input", () => {
    render(
      <MemoryRouter>
        <Consumers />
      </MemoryRouter>,
    );
    const actionId: MobileAssistantActionId = "focusComposer";
    act(() =>
      dispatchMobileAssistantButton({
        id: actionId,
        label: actionId,
        binding: { kind: "shortcut", actionId },
      }),
    );
    expect(screen.getByRole("textbox", { name: "Composer" })).toHaveFocus();
  });
});
