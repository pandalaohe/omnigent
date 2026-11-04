import { act, cleanup, render, screen } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useCommandPaletteHotkey } from "@/hooks/useCommandPaletteHotkey";
import { useNewSessionHotkey } from "@/hooks/useNewSessionHotkey";
import { useSettingsHotkey } from "@/hooks/useSettingsHotkey";
import { useModelPickerHotkey } from "@/hooks/useModelPickerHotkey";
import { useComposerConfigCycleHotkeys } from "@/hooks/useComposerConfigCycleHotkeys";
import { useVoiceDictationHotkey } from "@/hooks/useVoiceDictationHotkey";
import { useViewModeToggleHotkey } from "@/hooks/useViewModeToggleHotkey";
import { useSidebarToggleHotkeys } from "@/hooks/useSidebarToggleHotkeys";
import { currentShortcutPlatform } from "@/lib/keyboardShortcutPreferences";
import { dispatchMobileAssistantButton } from "@/lib/mobileAssistantPreferences";

function Consumers() {
  const [surface, setSurface] = useState("Chat");
  const [view, setView] = useState<"chat" | "terminal">("chat");
  const [sidebar, setSidebar] = useState(false);
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
  useVoiceDictationHotkey(() => setSurface("Dictation started"), true, isMac);
  useViewModeToggleHotkey({ enabled: true, view, setView }, isMac);
  useSidebarToggleHotkeys({ onToggleLeft: () => setSidebar((open) => !open) }, isMac);
  return (
    <output data-testid="surface">
      {surface} | {view} | {sidebar ? "Sidebar open" : "Sidebar closed"} | {location.pathname}
    </output>
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
    ["openModelPicker", "Model picker"],
    ["cycleModel", "Next model"],
    ["cycleEffort", "Next effort"],
    ["voiceDictation", "Dictation started"],
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
});
