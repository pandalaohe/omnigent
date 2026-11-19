import { act, fireEvent, render, screen } from "@testing-library/react";
import { toast } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  MOBILE_ASSISTANT_STORAGE_KEY,
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
import { buildTerminalKeyPreferences, fromBar322, resolveTerminalKeys } from "@/lib/terminalKeys";
import * as preferencesSync from "@/lib/userPreferencesSync";

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

  it.each([
    ["preset", 65_536],
    ["preset", 65_537],
    ["native switch", 65_537],
  ])("enforces the account limit when the editor saves a %s at %i bytes", (edit, bytes) => {
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
    const envelope = preferencesSync.collectLocalUserPreferences();
    const current = { ...readMobileAssistantPreferences() };
    const { keys } = resolveTerminalKeys(current);
    const mobile = buildTerminalKeyPreferences(
      edit === "preset"
        ? { terminalKeysPreset: { shell: fromBar322(bar) } }
        : { terminalKeys: { ...keys, native: { ...keys.native, enabled: false } } },
      current,
      envelope.settings.mobile_assistant as Record<string, unknown>,
    );
    envelope.settings.mobile_assistant = mobile;
    envelope.settings.usage_context = "";
    const padding = "x".repeat(bytes - preferencesEnvelopeBytes(envelope));
    localStorage.setItem("omnigent:usage-context-preferences", JSON.stringify(padding));
    const before = preferencesSync.collectLocalUserPreferences();
    const rawBefore = localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY);
    expect(preferencesEnvelopeBytes(before)).toBeLessThanOrEqual(65_536);
    expect(
      preferencesEnvelopeBytes({
        ...before,
        settings: { ...before.settings, mobile_assistant: mobile },
      }),
    ).toBe(bytes);
    const writeLocal = vi.spyOn(Storage.prototype, "setItem");
    const queuePatch = vi.spyOn(preferencesSync, "queueUserPreferencePatch");
    const control =
      edit === "preset"
        ? screen.getByRole("button", { name: "Save as my preset" })
        : screen.getByRole("switch", { name: "Enable native terminal key rows" });
    fireEvent.click(control);
    if (bytes === 65_536) {
      expect(readMobileAssistantPreferences().terminalBarPreset).toEqual(bar);
      expect(queuePatch).toHaveBeenCalledWith("mobile_assistant", mobile);
    } else {
      const message = screen.getByText(
        "Too large to save: preferences are limited to 64 KiB. Shorten or remove some keys.",
      );
      expect(message).toBeVisible();
      expect(message).toHaveAttribute("role", "status");
      expect(writeLocal).not.toHaveBeenCalled();
      expect(queuePatch).not.toHaveBeenCalled();
      expect(localStorage.getItem(MOBILE_ASSISTANT_STORAGE_KEY)).toBe(rawBefore);
      expect(preferencesSync.collectLocalUserPreferences()).toEqual(before);
      if (edit === "native switch") expect(control).toBeChecked();
    }
  });
});
