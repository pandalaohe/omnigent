import { act, fireEvent, render, screen, within } from "@testing-library/react";
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

vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }));

function storedKeys() {
  return resolveTerminalKeys({ ...readMobileAssistantPreferences() }).keys;
}

function openNative() {
  fireEvent.mouseDown(screen.getByRole("tab", { name: "Native terminal" }), { button: 0 });
}

function addKey() {
  fireEvent.click(screen.getAllByRole("button", { name: "Add key" }).at(-1)!);
}

function chooseFunction(value: string) {
  fireEvent.change(screen.getByLabelText("Function"), { target: { value } });
}

describe("MobileAssistantSettings", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it("records Ctrl+B, pages through icons, and moves and deletes the saved key", () => {
    render(<MobileAssistantSettings />);
    const chat = readMobileAssistantPreferences().buttons;
    expect(screen.getByText(/Top row · 1 page/)).toBeTruthy();
    expect(screen.getByText(/Bottom row · 2 pages/)).toBeTruthy();
    addKey();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    fireEvent.keyDown(window, { key: "b", code: "KeyB", ctrlKey: true });
    expect(screen.getByLabelText("Send sequence")).toHaveValue("^B");
    expect(screen.getByLabelText("Label")).toHaveValue("ctrl+b");
    fireEvent.click(screen.getByRole("radio", { name: "Icon" }));
    const icons = within(screen.getByRole("group", { name: "Terminal key icons" }));
    for (const page of [1, 2, 3]) {
      expect(icons.getByText(`Page ${page} of 3`, { exact: false })).toBeVisible();
      expect(
        icons.getAllByRole("button", { pressed: false }).length +
          icons.queryAllByRole("button", { pressed: true }).length,
      ).toBe(24);
      if (page < 3) fireEvent.click(icons.getByRole("button", { name: "Next icon page" }));
    }
    fireEvent.click(icons.getByRole("button", { name: "Previous icon page" }));
    fireEvent.click(icons.getByRole("button", { name: "codex" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const added = storedKeys().shell.bottom.keys.at(-1)!;
    expect(added).toMatchObject({
      width: 2,
      label: "ctrl+b",
      icon: "agent-codex",
      display: "icon",
      action: { type: "send", send: "^B" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Edit ctrl\+b, 2 cells/ }));
    fireEvent.click(screen.getByRole("button", { name: "Move to other row" }));
    expect(storedKeys().shell.top.keys.some((key) => key.id === added.id)).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("switch", { name: "Show key bar on this page" }));
    fireEvent.click(screen.getByRole("switch", { name: "Show bottom terminal key row" }));
    expect(readMobileAssistantPreferences()).toMatchObject({
      buttons: chat,
      terminalKeysEnabled: false,
      terminalSecondRow: false,
    });
    fireEvent.click(screen.getByRole("button", { name: /^Edit ctrl\+b, 2 cells/ }));
    fireEvent.click(screen.getByRole("button", { name: "Delete key" }));
    expect(storedKeys().shell.top.keys.some((key) => key.id === added.id)).toBe(false);
  });

  it("stops recording without changing the field and shows refusals while listening", () => {
    render(<MobileAssistantSettings />);
    addKey();
    fireEvent.change(screen.getByLabelText("Send sequence"), { target: { value: "text" } });
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    fireEvent.keyDown(window, { key: "b", code: "KeyB", repeat: true });
    fireEvent.keyDown(window, { key: "Shift", code: "ShiftLeft" });
    fireEvent.keyDown(window, { key: "MediaPlayPause", code: "MediaPlayPause" });
    expect(screen.getByText("This key has no terminal sequence.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(screen.getByLabelText("Send sequence")).toHaveValue("text");
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    fireEvent.blur(window);
    expect(screen.getByRole("button", { name: "Record" })).toBeVisible();
  });

  it("keeps Send Enter after when re-recording a Send key and drops it for a Cmd chord", () => {
    render(<MobileAssistantSettings />);
    addKey();
    fireEvent.change(screen.getByLabelText("Send sequence"), { target: { value: "old text" } });
    const enterSwitch = screen.getByRole("switch", { name: "Send Enter after" });
    fireEvent.click(enterSwitch);
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    fireEvent.keyDown(window, { key: "b", code: "KeyB" });
    expect(enterSwitch).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(storedKeys().shell.bottom.keys.at(-1)?.action).toEqual({
      type: "send",
      send: "b",
      autoEnter: true,
    });

    fireEvent.click(screen.getByRole("button", { name: /^Edit b,/ }));
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    fireEvent.keyDown(window, { key: "v", code: "Period", metaKey: true });
    expect(screen.getByText("⌘V → Paste")).toBeVisible();
    expect(screen.queryByRole("switch", { name: "Send Enter after" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(storedKeys().shell.bottom.keys.at(-1)?.action).toEqual({
      type: "combo",
      chord: { code: "v", modifiers: ["meta"] },
    });
  });

  it("offers only the horizontal moves available at the key's current row position", () => {
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("button", { name: /^Edit Esc,/ }));
    expect(screen.queryByRole("button", { name: "Move left" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Move right" }));
    expect(screen.getByRole("button", { name: "Move left" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    const last = storedKeys().shell.top.keys.at(-1)!;
    fireEvent.click(
      screen.getByRole("button", { name: `Edit ${last.label}, ${last.width} cells` }),
    );
    expect(screen.queryByRole("button", { name: "Move right" })).toBeNull();
    expect(screen.getByRole("button", { name: "Move left" })).toBeVisible();
  });

  it("shows a recovery action without exposing the server's rejection details", () => {
    render(<MobileAssistantSettings />);
    fireEvent(
      window,
      new CustomEvent(preferencesSync.USER_PREFERENCES_PATCH_REJECTED_EVENT, {
        detail: {
          namespace: "mobile_assistant",
          detail: "Terminal preferences failed the schema check",
        },
      }),
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Couldn't sync the key layout");
    expect(alert).toHaveTextContent("save your changes again");
    expect(alert).not.toHaveTextContent("schema check");
  });

  it("captures a Cmd token and saves paste and special-key options", () => {
    render(<MobileAssistantSettings />);
    openNative();
    addKey();
    fireEvent.click(screen.getByRole("button", { name: "Record" }));
    fireEvent.keyDown(window, { key: "ArrowLeft", code: "ArrowLeft", metaKey: true });
    expect(screen.getByText("⌘← → Move to start of line")).toBeVisible();
    expect(screen.queryByRole("switch", { name: "Send Enter after" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(storedKeys().native.bottom.keys.at(-1)?.action).toEqual({
      type: "combo",
      chord: { code: "ArrowLeft", modifiers: ["meta"] },
    });
    addKey();
    chooseFunction("clipboard");
    fireEvent.change(screen.getByLabelText("Clipboard action"), { target: { value: "pasteHost" } });
    expect(screen.getByRole("switch", { name: "Press Enter after one line" })).toBeChecked();
    expect(screen.queryByRole("switch", { name: "Repeat on hold" })).toBeNull();
    fireEvent.click(screen.getByRole("switch", { name: "Press Enter after one line" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(storedKeys().native.bottom.keys.at(-1)?.action).toEqual({
      type: "clipboard",
      op: "pasteHost",
      autoEnter: false,
    });
    addKey();
    chooseFunction("special");
    fireEvent.change(screen.getByLabelText("Special key"), { target: { value: "cmd" } });
    fireEvent.click(screen.getByRole("switch", { name: "Keep held until tapped again" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(storedKeys().native.bottom.keys.at(-1)?.action).toEqual({
      type: "special",
      special: "cmd",
      keepHeld: true,
    });
  });

  it("pins leading keys and keeps legacy text literal until the function is changed", () => {
    const bar = defaultTerminalBar();
    bar.top.keys[0] = {
      id: "legacy",
      label: "Legacy",
      display: "text",
      width: 2,
      kind: "text",
      text: "^C",
    };
    const terminalButtons = projectLegacy(bar);
    writeMobileAssistantPreferences({
      version: 2,
      enabled: true,
      buttons: [],
      terminalBar: bar,
      terminalButtons,
      terminalLegacyBasis: legacyFingerprint(terminalButtons, true),
    });
    render(<MobileAssistantSettings />);
    fireEvent.click(screen.getByRole("button", { name: "Pin one more bottom row key" }));
    expect(storedKeys().shell.bottom.pinned).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: /^Edit Legacy,/ }));
    expect(screen.getByText("Kept from the previous editor: ^C")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("radio", { name: "1 cell" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(storedKeys().shell.top.keys[0]).toMatchObject({
      label: "Renamed",
      width: 1,
      action: { type: "legacy", key: { kind: "text", text: "^C" } },
    });
    fireEvent.click(screen.getByRole("button", { name: /^Edit Renamed,/ }));
    chooseFunction("send");
    expect(screen.getByLabelText("Send sequence")).toHaveValue("\\x5eC");
    expect(screen.getByText("^C", { selector: "code" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(storedKeys().shell.top.keys[0].action).toEqual({ type: "send", send: "\\x5eC" });
  });

  it("copies only rows and undoes the copy without touching either page’s switches or presets", () => {
    render(<MobileAssistantSettings />);
    const original = storedKeys().native;
    fireEvent.click(screen.getByRole("button", { name: "Pin one more bottom row key" }));
    openNative();
    fireEvent.click(screen.getByRole("switch", { name: "Show key bar on this page" }));
    fireEvent.click(screen.getByRole("switch", { name: "Only while the phone keyboard is up" }));
    fireEvent.click(screen.getByRole("button", { name: "Save as my preset" }));
    fireEvent.click(screen.getByRole("button", { name: "Copy from Shell" }));
    const { shell, native } = storedKeys();
    expect(native).toMatchObject({ enabled: false, showRows: "keyboard", bottom: shell.bottom });
    const undo = vi.mocked(toast).mock.calls.at(-1)?.[1]?.action;
    act(() => {
      if (undo && typeof undo === "object" && "onClick" in undo) undo.onClick({} as never);
    });
    expect(storedKeys().native).toMatchObject({
      enabled: false,
      showRows: "keyboard",
      bottom: original.bottom,
    });
    expect(storedKeys().shell).toEqual(shell);
    expect(readMobileAssistantPreferences().terminalKeysPreset).toMatchObject({
      native: { bottom: original.bottom },
    });
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
    fireEvent.click(screen.getByRole("switch", { name: "Show key bar on this page" }));
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
    expect(screen.getByText(/Saved terminal key layouts couldn’t be read/)).toBeVisible();
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-preferences")!)).toEqual(raw);
    openNative();
    fireEvent.click(screen.getByRole("switch", { name: "Show key bar on this page" }));
    expect(readMobileAssistantPreferences().terminalKeys?.shell.enabled).toBe(true);
    expect(readMobileAssistantPreferences().terminalKeys?.native.enabled).toBe(false);
    expect(screen.queryByText(/Saved terminal key layouts couldn’t be read/)).toBeNull();
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
    if (edit === "native switch") openNative();
    const control =
      edit === "preset"
        ? screen.getByRole("button", { name: "Save as my preset" })
        : screen.getByRole("switch", { name: "Show key bar on this page" });
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
