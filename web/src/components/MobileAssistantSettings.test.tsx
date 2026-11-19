import { act, fireEvent, render, screen } from "@testing-library/react";
import { toast } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  readMobileAssistantPreferences,
  writeMobileAssistantPreferences,
} from "@/lib/mobileAssistantPreferences";
import { MobileAssistantSettings } from "./MobileAssistantSettings";
import {
  defaultTerminalBar,
  preferencesEnvelopeBytes,
  legacyFingerprint,
  projectLegacy,
} from "@/lib/terminalKeyBar";
import { buildTerminalKeyPreferences, fromBar322 } from "@/lib/terminalKeys";
import { collectLocalUserPreferences } from "@/lib/userPreferencesSync";

vi.mock("sonner", () => ({ toast: vi.fn() }));

function pickCombobox(label: string, query: string, option: string | RegExp) {
  const input = screen.getByRole("combobox", { name: label });
  fireEvent.change(input, { target: { value: query } });
  fireEvent.click(screen.getByRole("option", { name: option }));
}

describe("MobileAssistantSettings", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("edits the terminal bar, adds a chord by tap, moves it across rows, and persists both switches", () => {
    localStorage.clear();
    render(<MobileAssistantSettings />);
    const chat = readMobileAssistantPreferences().buttons;
    expect(screen.getByText(/Top row · 1 page/)).toBeTruthy();
    expect(screen.getByText(/Bottom row · 2 pages/)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Add key" }).at(-1)!);
    pickCombobox("More keys", "R", /^R\s*Letters/);
    fireEvent.click(screen.getByRole("button", { name: "Ctrl" }));
    fireEvent.click(screen.getByRole("button", { name: "Shift" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const added = readMobileAssistantPreferences().terminalBar!.bottom.keys.at(-1)!;
    expect(added).toMatchObject({
      kind: "key",
      width: 2,
      chord: { code: "KeyR", modifiers: ["control", "shift"] },
      label: "Ctrl+Shift+R",
    });
    fireEvent.click(screen.getByRole("button", { name: /^Edit Ctrl\+Shift\+R, 2 cells/ }));
    fireEvent.click(screen.getByRole("button", { name: "Move to other row" }));
    expect(
      readMobileAssistantPreferences().terminalBar!.top.keys.some((key) => key.id === added.id),
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("switch", { name: "Enable terminal key rows" }));
    fireEvent.click(screen.getByRole("switch", { name: "Show bottom terminal key row" }));
    expect(readMobileAssistantPreferences()).toMatchObject({
      buttons: chat,
      terminalKeysEnabled: false,
      terminalSecondRow: false,
    });
    expect(readMobileAssistantPreferences().buttons).toEqual(chat);
    fireEvent.click(screen.getByRole("button", { name: /^Edit Ctrl\+Shift\+R, 2 cells/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete key" }));
    expect(
      readMobileAssistantPreferences().terminalBar!.top.keys.some((key) => key.id === added.id),
    ).toBe(false);
  });

  it("pins leading keys with the stepper and refuses an unsupported terminal chord", () => {
    localStorage.clear();
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("button", { name: "Pin one more bottom row key" }));
    expect(readMobileAssistantPreferences().terminalBar!.bottom.pinned).toBe(1);
    const tab = readMobileAssistantPreferences().terminalBar!.bottom.keys[0];
    expect(tab.id).toBe("tab");
    expect(screen.getByRole("button", { name: /^Edit Tab, 2 cells, pinned/ })).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("button", { name: "Add key" }).at(-1)!);
    fireEvent.click(screen.getByRole("button", { name: "Tab" }));
    fireEvent.click(screen.getByRole("button", { name: "Ctrl" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByText(/Not supported in terminals/)).toBeVisible();
    pickCombobox("More keys", "R", /^R\s*Letters/);
    fireEvent.click(screen.getByRole("button", { name: "Shift" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    expect(screen.queryByText(/Not supported in terminals/)).toBeNull();
  });

  it("saves modified arrows in the terminal bar", () => {
    localStorage.clear();
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getAllByRole("button", { name: "Add key" }).at(-1)!);
    fireEvent.click(screen.getByRole("button", { name: "↑" }));
    fireEvent.click(screen.getByRole("button", { name: "Ctrl" }));
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(readMobileAssistantPreferences().terminalBar!.bottom.keys.at(-1)).toMatchObject({
      kind: "key",
      chord: { code: "ArrowUp", modifiers: ["control"] },
    });
  });

  it("uses the shared help control for terminal rows and closes it on a second tap", () => {
    render(<MobileAssistantSettings />);
    const help = screen.getByRole("button", { name: "About Terminal key rows" });
    fireEvent.click(help);
    expect(screen.getByText(/Show keys above the phone keyboard/)).toBeVisible();
    fireEvent.click(help);
    expect(screen.queryByText(/Show keys above the phone keyboard/)).not.toBeInTheDocument();
  });

  it("round-trips a preset and undoes defaults without reverting an unrelated switch", () => {
    localStorage.clear();
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("button", { name: /^Edit Esc,/ }));
    fireEvent.click(screen.getByRole("radio", { name: "1 cell" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    fireEvent.click(screen.getByRole("button", { name: "Save as my preset" }));
    fireEvent.click(screen.getByRole("button", { name: "Restore defaults" }));
    expect(readMobileAssistantPreferences().terminalBar!.top.keys[0].width).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "Load my preset" }));
    expect(readMobileAssistantPreferences().terminalBar!.top.keys[0].width).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Restore defaults" }));
    const undo = vi.mocked(toast).mock.calls.at(-1)?.[1]?.action;
    expect(undo && typeof undo === "object" && "onClick" in undo).toBe(true);
    fireEvent.click(screen.getByRole("switch", { name: "Enable terminal key rows" }));
    act(() => {
      if (undo && typeof undo === "object" && "onClick" in undo) undo.onClick({} as never);
    });
    expect(readMobileAssistantPreferences().terminalBar!.top.keys[0].width).toBe(1);
    expect(readMobileAssistantPreferences().terminalKeysEnabled).toBe(false);
  });

  it("refuses an oversized saved preset before touching local storage", () => {
    const bar = defaultTerminalBar();
    for (const row of ["top", "bottom"] as const) {
      bar[row] = {
        enabled: true,
        pinned: 0,
        keys: Array.from({ length: 60 }, (_, index) => ({
          id: `${row}-${index}`,
          label: "快捷".repeat(6),
          kind: "text",
          display: "text",
          width: 1,
          text: "输入".repeat(32),
        })),
      };
    }
    const legacy = projectLegacy(bar);
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [],
      terminalBar: bar,
      terminalButtons: legacy,
      terminalLegacyBasis: legacyFingerprint(legacy, true),
      chatActionsRevision: 1,
    });
    render(<MobileAssistantSettings />);
    const before = localStorage.getItem("omnigent:mobile-assistant-preferences");
    fireEvent.click(screen.getByRole("button", { name: "Save as my preset" }));
    expect(screen.getByText("Too large to sync: remove keys or shorten texts")).toBeVisible();
    expect(localStorage.getItem("omnigent:mobile-assistant-preferences")).toBe(before);
  });

  it("names the default fallback when an invalid stored bar had no older key list", () => {
    const raw = {
      version: 2,
      enabled: true,
      buttons: [],
      chatActionsRevision: 1,
      terminalBar: { invalid: true },
    };
    localStorage.setItem("omnigent:mobile-assistant-preferences", JSON.stringify(raw));
    expect(readMobileAssistantPreferences().terminalButtons).toBeUndefined();
    render(<MobileAssistantSettings />);
    expect(screen.getByText(/showing the default keys/)).toBeVisible();
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-preferences")!)).toEqual(raw);
    fireEvent.click(screen.getByRole("switch", { name: "Enable native terminal key rows" }));
    expect(readMobileAssistantPreferences().terminalKeys?.shell.enabled).toBe(true);
    expect(readMobileAssistantPreferences().terminalKeys?.native.enabled).toBe(false);
    expect(screen.queryByText(/showing the default keys/)).toBeNull();
  });

  it.each([65_536, 65_537])("enforces the exact normalized editor budget at %i bytes", (bytes) => {
    localStorage.clear();
    const bar = defaultTerminalBar();
    const terminalButtons = projectLegacy(bar);
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [],
      chatActionsRevision: 1,
      terminalBar: bar,
      terminalButtons,
      terminalLegacyBasis: legacyFingerprint(terminalButtons, true),
    });
    render(<MobileAssistantSettings />);
    const envelope = collectLocalUserPreferences();
    const mobile = buildTerminalKeyPreferences(
      { terminalKeysPreset: { shell: fromBar322(bar) } },
      { ...readMobileAssistantPreferences() },
      envelope.settings.mobile_assistant as Record<string, unknown>,
    );
    envelope.settings.mobile_assistant = mobile;
    envelope.settings.usage_context = "";
    const padding = "x".repeat(bytes - preferencesEnvelopeBytes(envelope));
    localStorage.setItem("omnigent:usage-context-preferences", JSON.stringify(padding));
    const actual = collectLocalUserPreferences();
    actual.settings.mobile_assistant = mobile;
    expect(preferencesEnvelopeBytes(actual)).toBe(bytes);
    fireEvent.click(screen.getByRole("button", { name: "Save as my preset" }));
    if (bytes === 65_536) {
      expect(readMobileAssistantPreferences().terminalBarPreset).toEqual(bar);
    } else {
      expect(readMobileAssistantPreferences().terminalBarPreset).toBeUndefined();
      expect(screen.getByText("Too large to sync: remove keys or shorten texts")).toBeVisible();
    }
  });
});
