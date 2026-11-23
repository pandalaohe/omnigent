import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  collectLocalUserPreferences,
  initializeUserPreferencesSync,
  queueUserPreferencePatch,
  queueAcknowledgedUserPreferencePatch,
  deactivateUserPreferencesSync,
  refreshUserPreferencesFromServer,
  resetUserPreferencesSyncForTests,
  USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT,
  USER_PREFERENCES_PATCH_REJECTED_EVENT,
} from "./userPreferencesSync";
import {
  readApprovalTimeoutPreferences,
  writeApprovalTimeoutPreferences,
} from "./approvalTimeoutPreferences";
import { patchHostColor, readHostColorPreferences } from "./hostColorPreferences";
import {
  KEEP_WARM_DEFAULTS,
  KEEP_WARM_STORAGE_KEY,
  writeKeepWarmPreferences,
} from "./keepWarmPreferences";
import { readAlwaysUseWorktree, writeAlwaysUseWorktree } from "./worktreeDefaultPreferences";
import { defaultTerminalBar, legacyFingerprint, projectLegacy } from "./terminalKeyBar";
import { resolveTerminalKeys, writeTerminalKeyPreferences } from "./terminalKeys";
import {
  readTerminalKeyboardPreferences,
  writeTerminalKeyboardChange,
  writeTerminalKeyboardPreferences,
  type TerminalKeyboardPreferences,
} from "./terminalKeyboardPreferences";
import {
  readKeyboardShortcutPreferences,
  resetShortcutPreference,
} from "./keyboardShortcutPreferences";
import {
  readMobileAssistantPreferences,
  writeMobileAssistantPreferences,
} from "./mobileAssistantPreferences";
import {
  MOBILE_MENU_PINS_STORAGE_KEY,
  readMobileMenuPinsPreferences,
  toggleMobileMenuPin,
} from "./mobileMenuPinPreferences";

beforeEach(() => {
  localStorage.clear();
  resetUserPreferencesSyncForTests();
  vi.useRealTimers();
});

describe("terminal bar preference synchronization", () => {
  function mobileValue() {
    const terminalBar = defaultTerminalBar();
    const terminalButtons = projectLegacy(terminalBar);
    return {
      version: 2 as const,
      enabled: true,
      buttons: [],
      chatActionsRevision: 1 as const,
      terminalBar,
      terminalBarPreset: structuredClone(terminalBar),
      terminalButtons,
      terminalSecondRow: true,
      terminalLegacyBasis: legacyFingerprint(terminalButtons, true),
    };
  }

  it("sends and retries only the edited terminal keyboard fields", async () => {
    vi.useFakeTimers();
    const originalKeyboard = {
      v: 1,
      pasteBoxMode: { windows: "whenBlocked" },
      bindings: {
        newline: { platformOverrides: { macos: [{ code: "F8", modifiers: [] }] } },
      },
    };
    const mobile = { ...mobileValue(), terminalKeyboard: originalKeyboard };
    const envelope = { version: 1, settings: { mobile_assistant: mobile } };
    let acknowledge!: (response: Response) => void;
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    fetcher.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          acknowledge = resolve;
        }),
    );
    await initializeUserPreferencesSync(envelope, fetcher, "alice");

    const pasteEdit = { platform: "macos", field: "pasteBoxMode", value: "never" } as const;
    const bindingReset = { platform: "macos", field: "newline", value: null } as const;
    const mapEdit = {
      platform: "linux",
      field: "modifierMap",
      value: { ctrl: "ctrl", alt: "cmd", meta: "alt" },
    } as const;
    expect(writeTerminalKeyboardChange(pasteEdit)).toBe("saved");
    await vi.advanceTimersByTimeAsync(251);
    const first = JSON.parse(fetcher.mock.calls[0][1].body);
    expect(first.value).toEqual({});
    expect(first.terminal_keyboard.changes).toEqual([pasteEdit]);

    expect(writeTerminalKeyboardChange(bindingReset)).toBe("saved");
    expect(writeTerminalKeyboardChange(mapEdit)).toBe("saved");
    // A canonical response can include a newer choice from the other device.
    acknowledge(
      Response.json({
        ...envelope,
        settings: {
          mobile_assistant: {
            ...mobile,
            terminalKeyboard: {
              ...originalKeyboard,
              pasteBoxMode: { windows: "always", macos: "never" },
            },
          },
        },
      }),
    );
    await vi.advanceTimersByTimeAsync(300);
    const second = JSON.parse(fetcher.mock.calls[1][1].body);
    expect(second.value).toEqual({});
    expect(second.terminal_keyboard.changes).toEqual([bindingReset, mapEdit]);
    expect(readTerminalKeyboardPreferences().pasteBoxMode).toEqual({
      windows: "always",
      macos: "never",
    });
    expect(
      readTerminalKeyboardPreferences().bindings?.newline?.platformOverrides?.macos,
    ).toBeUndefined();

    const keys = resolveTerminalKeys({ ...readMobileAssistantPreferences() }).keys;
    expect(writeTerminalKeyPreferences({ terminalKeys: keys })).toBe("saved");
    await vi.advanceTimersByTimeAsync(251);
    const unrelated = JSON.parse(fetcher.mock.calls[2][1].body);
    expect(unrelated.value).toHaveProperty("terminalKeys");
    expect(unrelated.value).not.toHaveProperty("terminalKeyboard");
    expect(unrelated).not.toHaveProperty("terminal_keyboard");
  });

  it.each(["legacy", "scoped"])(
    "retains a %s offline keyboard edit across reload and a toolbar save",
    async (kind) => {
      vi.useFakeTimers();
      const envelope = { version: 1, settings: { mobile_assistant: mobileValue() } };
      const offline = vi.fn().mockRejectedValue(new Error("offline"));
      await initializeUserPreferencesSync(envelope, offline, "alice");
      const keyboard = { v: 1, pasteBoxMode: { macos: "never" } } as const;
      const change = { platform: "macos", field: "pasteBoxMode", value: "never" } as const;
      const saved =
        kind === "legacy"
          ? writeTerminalKeyboardPreferences(keyboard)
          : writeTerminalKeyboardChange(change);
      expect(saved).toBe("saved");
      await vi.advanceTimersByTimeAsync(251);
      expect(offline).toHaveBeenCalledOnce();

      resetUserPreferencesSyncForTests();
      const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
      await initializeUserPreferencesSync(envelope, fetcher, "alice");
      expect(readTerminalKeyboardPreferences().pasteBoxMode).toEqual(keyboard.pasteBoxMode);
      const { keys } = resolveTerminalKeys({ ...readMobileAssistantPreferences() });
      expect(
        writeTerminalKeyPreferences({
          terminalKeys: { ...keys, shell: { ...keys.shell, enabled: false } },
        }),
      ).toBe("saved");
      await vi.advanceTimersByTimeAsync(251);
      const [request] = fetcher.mock.calls;
      const payload = JSON.parse(request[1].body);
      expect(payload.value.terminalKeysEnabled).toBe(false);
      if (kind === "legacy") expect(payload.value.terminalKeyboard).toEqual(keyboard);
      else {
        expect(payload.value).not.toHaveProperty("terminalKeyboard");
        expect(payload.terminal_keyboard.changes).toEqual([change]);
      }
    },
  );

  it.each([
    ["refresh", "scoped"],
    ["patch", "scoped"],
    ["patch", "legacy"],
  ])("keeps the latest keyboard when %s responds first to a %s save", async (first, kind) => {
    vi.useFakeTimers();
    const withKeyboard = (windows: "always" | "never", macos?: "never") => ({
      version: 1,
      settings: {
        mobile_assistant: {
          ...mobileValue(),
          terminalKeyboard: { v: 1, pasteBoxMode: { windows, macos } },
        },
      },
    });
    const me = (preferences: ReturnType<typeof withKeyboard>) =>
      Response.json({ user_id: "alice", preferences });
    let finishPatch!: (response: Response) => void;
    let finishRefresh!: (response: Response) => void;
    const fetcher = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            finishPatch = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            finishRefresh = resolve;
          }),
      )
      .mockImplementationOnce(() => Promise.resolve(me(withKeyboard("never", "never"))));
    await initializeUserPreferencesSync(withKeyboard("always"), fetcher, "alice");
    const result =
      kind === "legacy"
        ? writeTerminalKeyboardPreferences({
            v: 1,
            pasteBoxMode: { windows: "never", macos: "never" },
          })
        : writeTerminalKeyboardChange({
            platform: "macos",
            field: "pasteBoxMode",
            value: "never",
          });
    expect(result).toBe("saved");
    await vi.advanceTimersByTimeAsync(251);
    const refresh = refreshUserPreferencesFromServer();
    if (first === "refresh") {
      finishRefresh(me(withKeyboard("never", "never")));
      await refresh;
      finishPatch(Response.json(withKeyboard("always", "never")));
    } else {
      finishPatch(Response.json(withKeyboard("never", "never")));
      await vi.advanceTimersByTimeAsync(0);
      finishRefresh(me(withKeyboard("always")));
      await refresh;
    }
    await vi.advanceTimersByTimeAsync(300);
    expect(readTerminalKeyboardPreferences().pasteBoxMode).toEqual({
      windows: "never",
      macos: "never",
    });
    expect(fetcher.mock.calls.map((call) => call[0])).toEqual([
      "/v1/me/preferences/mobile_assistant",
      "/v1/me",
      "/v1/me",
    ]);
  });

  it("does not replay a committed legacy keyboard snapshot with a newer toolbar edit", async () => {
    vi.useFakeTimers();
    const mobile = mobileValue();
    const envelope = { version: 1, settings: { mobile_assistant: mobile } };
    let acknowledge!: (response: Response) => void;
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    fetcher.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          acknowledge = resolve;
        }),
    );
    await initializeUserPreferencesSync(envelope, fetcher, "alice");
    const keyboard = { v: 1, pasteBoxMode: { macos: "always" } } as const;
    expect(writeTerminalKeyboardPreferences(keyboard)).toBe("saved");
    const change = { platform: "macos", field: "pasteBoxMode", value: "never" } as const;
    expect(writeTerminalKeyboardChange(change)).toBe("saved");
    await vi.advanceTimersByTimeAsync(251);
    const combined = JSON.parse(fetcher.mock.calls[0][1].body);
    expect(combined.value.terminalKeyboard).toEqual(keyboard);
    expect(combined.terminal_keyboard.changes).toEqual([change]);

    const { keys } = resolveTerminalKeys({ ...readMobileAssistantPreferences() });
    expect(
      writeTerminalKeyPreferences({
        terminalKeys: { ...keys, shell: { ...keys.shell, enabled: false } },
      }),
    ).toBe("saved");
    const savedKeyboard = { v: 1, pasteBoxMode: { macos: "never" } };
    acknowledge(
      Response.json({
        ...envelope,
        settings: { mobile_assistant: { ...combined.value, terminalKeyboard: savedKeyboard } },
      }),
    );
    await vi.advanceTimersByTimeAsync(251);
    const toolbar = JSON.parse(fetcher.mock.calls[1][1].body);
    expect(toolbar.value.terminalKeysEnabled).toBe(false);
    expect(toolbar.value).not.toHaveProperty("terminalKeyboard");
    expect(toolbar).not.toHaveProperty("terminal_keyboard");
    expect(readTerminalKeyboardPreferences()).toEqual(savedKeyboard);
  });

  it("preserves terminal maps and bindings when an older editor deletes the app shortcut namespace", async () => {
    vi.useFakeTimers();
    const terminalKeyboard: TerminalKeyboardPreferences = {
      v: 1,
      pasteBoxMode: { macos: "never", windows: "always" },
      modifierMap: { macos: { ctrl: "ctrl", alt: "cmd", meta: "alt" } },
      bindings: {
        lineStart: {
          platformOverrides: { windows: [{ code: "KeyA", modifiers: ["control", "shift"] }] },
        },
      },
    };
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: {
          mobile_assistant: mobileValue(),
          keyboard_shortcuts: {
            version: 1,
            actions: { newSession: { common: [{ code: "F9", modifiers: [] }] } },
          },
        },
      },
      fetcher,
      "alice",
    );
    expect(writeTerminalKeyboardPreferences(terminalKeyboard)).toBe("saved");
    await vi.advanceTimersByTimeAsync(251);
    const mobile = JSON.parse(fetcher.mock.calls[0][1].body).value;
    expect(mobile.terminalKeyboard).toEqual(terminalKeyboard);
    resetShortcutPreference("newSession");
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher.mock.calls[1][0]).toBe("/v1/me/preferences/keyboard_shortcuts");
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ value: null });
    fetcher.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          user_id: "alice",
          preferences: { version: 1, settings: { mobile_assistant: mobile } },
        }),
      ),
    );
    await refreshUserPreferencesFromServer();
    expect(readKeyboardShortcutPreferences().actions).toEqual({});
    expect(readTerminalKeyboardPreferences()).toEqual(terminalKeyboard);
  });

  it("round-trips both surfaces and the unmodified circle envelope through a legacy client's edit", async () => {
    vi.useFakeTimers();
    const { chatActionsRevision: _unused, ...legacy } = mobileValue();
    const value = {
      ...legacy,
      buttons: [{ label: "Old", binding: { kind: "text", text: "^C" }, id: "old" }],
    };
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: { mobile_assistant: value } },
      fetcher,
      "alice",
    );
    const { keys } = resolveTerminalKeys({ ...readMobileAssistantPreferences() });
    expect(
      writeTerminalKeyPreferences({
        terminalKeys: { ...keys, shell: { ...keys.shell, enabled: false } },
        terminalKeysPreset: { native: keys.native },
        terminalKeyboard: { v: 1 },
      }),
    ).toBe("saved");
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher).toHaveBeenCalledOnce();
    const sent = JSON.parse(fetcher.mock.calls[0][1].body).value;
    expect(sent).not.toHaveProperty("chatActionsRevision");
    expect(JSON.stringify(sent.buttons)).toBe(JSON.stringify(value.buttons));
    expect(sent.terminalKeysEnabled).toBe(false);

    const updatedCircle = [{ label: "Old client edit", id: "new-button" }];
    fetcher.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          user_id: "alice",
          preferences: {
            version: 1,
            settings: {
              mobile_assistant: { ...sent, buttons: updatedCircle, terminalKeysEnabled: true },
            },
          },
        }),
      ),
    );
    await refreshUserPreferencesFromServer();
    const reconciled = resolveTerminalKeys({ ...readMobileAssistantPreferences() });
    expect(reconciled.keys.shell.enabled).toBe(true);
    expect(reconciled.keys.native).toEqual(keys.native);
    expect(reconciled.preset.native).toEqual(keys.native);
    expect(writeTerminalKeyPreferences({ terminalKeyboard: { v: 1, bindings: {} } })).toBe("saved");
    await vi.advanceTimersByTimeAsync(251);
    const next = JSON.parse(fetcher.mock.calls[2][1].body).value;
    expect(JSON.stringify(next.buttons)).toBe(JSON.stringify(updatedCircle));
    expect(next).not.toHaveProperty("chatActionsRevision");
  });

  it("hydrates the layout and preset without a write-back, then syncs a width-only edit", async () => {
    vi.useFakeTimers();
    const value = mobileValue();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: { mobile_assistant: value } },
      fetcher,
      "alice",
    );
    await vi.advanceTimersByTimeAsync(2000);
    expect(fetcher).not.toHaveBeenCalled();
    const preferences = readMobileAssistantPreferences();
    expect(preferences.terminalBar).toEqual(value.terminalBar);
    expect(preferences.terminalBarPreset).toEqual(value.terminalBarPreset);
    preferences.terminalBar!.top.keys[0].width = 1;
    writeMobileAssistantPreferences(preferences);
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher).toHaveBeenCalledOnce();
    const sent = JSON.parse(fetcher.mock.calls[0][1].body).value;
    expect(sent.terminalBar.top.keys[0].width).toBe(1);
    expect(sent.terminalBarPreset).toEqual(value.terminalBarPreset);
  });

  it("retains an offline layout and sends its newer edit after a stale ACK", async () => {
    vi.useFakeTimers();
    const value = mobileValue();
    let acknowledge!: (response: Response) => void;
    const fetcher = vi
      .fn()
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          acknowledge = resolve;
        }),
      )
      .mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: { mobile_assistant: value } },
      vi.fn().mockRejectedValue(new TypeError("offline")),
      "alice",
    );
    const offline = readMobileAssistantPreferences();
    offline.terminalBar!.top.keys[0].width = 1;
    writeMobileAssistantPreferences(offline);
    expect(readMobileAssistantPreferences().terminalBar!.top.keys[0].width).toBe(1);
    await vi.advanceTimersByTimeAsync(251);
    deactivateUserPreferencesSync();
    await initializeUserPreferencesSync(
      { version: 1, settings: { mobile_assistant: value } },
      fetcher,
      "alice",
    );
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher).toHaveBeenCalledOnce();
    const edited = readMobileAssistantPreferences();
    edited.terminalBar!.top.keys[1].width = 1;
    writeMobileAssistantPreferences(edited);
    await vi.advanceTimersByTimeAsync(251);
    acknowledge(new Response(null, { status: 200 }));
    await vi.advanceTimersByTimeAsync(1);
    expect(fetcher).toHaveBeenCalledTimes(2);
    const sent = JSON.parse(fetcher.mock.calls[1][1].body).value;
    expect(
      sent.terminalBar.top.keys.slice(0, 2).map((key: { width: number }) => key.width),
    ).toEqual([1, 1]);
    expect(readMobileAssistantPreferences().terminalBar).toEqual(sent.terminalBar);
  });

  it("sends a preset-only edit and restores the server layout with visible rejection detail", async () => {
    vi.useFakeTimers();
    const value = mobileValue();
    const fetcher = vi
      .fn()
      .mockResolvedValue(
        Response.json({ detail: "preferences exceed the 64 KiB limit" }, { status: 422 }),
      );
    await initializeUserPreferencesSync(
      { version: 1, settings: { mobile_assistant: value } },
      fetcher,
      "alice",
    );
    const rejected = vi.fn();
    window.addEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, rejected);
    const preferences = readMobileAssistantPreferences();
    preferences.terminalBarPreset!.top.keys[0].width = 1;
    writeMobileAssistantPreferences(preferences);
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher).toHaveBeenCalledOnce();
    expect(
      JSON.parse(fetcher.mock.calls[0][1].body).value.terminalBarPreset.top.keys[0].width,
    ).toBe(1);
    expect(readMobileAssistantPreferences().terminalBarPreset).toEqual(value.terminalBarPreset);
    expect(rejected.mock.calls[0][0].detail).toMatchObject({
      namespace: "mobile_assistant",
      detail: "preferences exceed the 64 KiB limit",
    });
    window.removeEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, rejected);
  });
});

describe("acknowledged preference saves", () => {
  it.each([
    { transition: "account", owner: "bob", server: "server-a" },
    { transition: "server", owner: "alice", server: "server-b" },
  ])("ignores a delayed 422 body after a $transition switch", async ({ owner, server }) => {
    vi.useFakeTimers();
    let releaseBody!: (body: { detail: string }) => void;
    const delayedBody = new Promise<{ detail: string }>((resolve) => {
      releaseBody = resolve;
    });
    const oldResponse = new Response(null, { status: 422 });
    const readBody = vi.spyOn(oldResponse, "json").mockReturnValue(delayedBody);
    const oldFetcher = vi.fn().mockResolvedValue(oldResponse);
    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      oldFetcher,
      "alice",
      "server-a",
    );
    queueUserPreferencePatch("keep_warm", { cells: {} });
    await vi.advanceTimersByTimeAsync(251);
    expect(readBody).toHaveBeenCalledOnce();

    let acknowledge!: (response: Response) => void;
    const currentFetcher = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        acknowledge = resolve;
      }),
    );
    await initializeUserPreferencesSync(
      { version: 1, settings: { keep_warm: { cells: { h2: { codex: { main: true } } } } } },
      currentFetcher,
      owner,
      server,
    );
    localStorage.setItem(KEEP_WARM_STORAGE_KEY, JSON.stringify({ cells: {} }));
    const currentSave = queueAcknowledgedUserPreferencePatch("keep_warm", { cells: {} });
    const settled: string[] = [];
    void currentSave.then(
      () => settled.push("saved"),
      (error: Error) => settled.push(error.message),
    );
    await vi.advanceTimersByTimeAsync(251);
    expect(currentFetcher).toHaveBeenCalledOnce();

    releaseBody({ detail: "Alice's settings were rejected" });
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toEqual([]);
    expect(JSON.parse(localStorage.getItem(KEEP_WARM_STORAGE_KEY)!)).toEqual({ cells: {} });
    const dirty = JSON.parse(localStorage.getItem("omnigent:user-preferences-dirty-values")!);
    expect(dirty[`${server}:${owner}`]).toEqual({ keep_warm: { cells: {} } });

    acknowledge(new Response(null, { status: 200 }));
    await currentSave;
    expect(settled).toEqual(["saved"]);
  });

  it("waits for the exact value when an older patch is in flight", async () => {
    vi.useFakeTimers();
    let resolveOld!: (response: Response) => void;
    let resolveNew!: (response: Response) => void;
    const fetcher = vi
      .fn()
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveOld = resolve;
        }),
      )
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveNew = resolve;
        }),
      );
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    queueUserPreferencePatch("keep_warm", { cells: {} });
    await vi.advanceTimersByTimeAsync(251);
    const settled = vi.fn();
    const next = queueAcknowledgedUserPreferencePatch("keep_warm", {
      cells: { h1: { codex: { main: true } } },
    });
    void next.then(settled);
    await vi.advanceTimersByTimeAsync(251);
    resolveOld(new Response(null, { status: 200 }));
    await vi.advanceTimersByTimeAsync(1);
    expect(settled).not.toHaveBeenCalled();
    resolveNew(new Response(null, { status: 200 }));
    await next;
    expect(settled).toHaveBeenCalledOnce();
  });

  it("returns the 422 detail and does not retry a rejected save", async () => {
    vi.useFakeTimers();
    const fetcher = vi
      .fn()
      .mockImplementation(() =>
        Promise.resolve(Response.json({ detail: "64 KiB: 20 hosts, 30 columns" }, { status: 422 })),
      );
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    const save = queueAcknowledgedUserPreferencePatch("keep_warm", { cells: {} });
    const rejected = expect(save).rejects.toThrow("64 KiB: 20 hosts, 30 columns");
    await vi.advanceTimersByTimeAsync(251);
    await rejected;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it("rejects superseded, aborted, unavailable and disposed observers", async () => {
    vi.useFakeTimers();
    await expect(queueAcknowledgedUserPreferencePatch("keep_warm", {})).rejects.toThrow(
      "not ready",
    );
    await initializeUserPreferencesSync({ version: 1, settings: {} }, vi.fn(), "alice");
    const previous = queueAcknowledgedUserPreferencePatch("keep_warm", { cells: {} });
    const replaced = expect(previous).rejects.toThrow("newer edit");
    queueUserPreferencePatch("keep_warm", { cells: { h1: {} } });
    await replaced;
    const controller = new AbortController();
    const aborted = queueAcknowledgedUserPreferencePatch(
      "keep_warm",
      { cells: {} },
      controller.signal,
    );
    const cancelled = expect(aborted).rejects.toThrow("cancelled");
    controller.abort();
    await cancelled;
    const pending = queueAcknowledgedUserPreferencePatch("keep_warm", { cells: {} });
    const disposed = expect(pending).rejects.toThrow("account or server changed");
    deactivateUserPreferencesSync();
    await disposed;
  });

  it("settles permanent transport failures and a hung request instead of hanging", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockRejectedValue(new Error("offline"));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    const failed = queueAcknowledgedUserPreferencePatch("keep_warm", { cells: {} });
    const failure = expect(failed).rejects.toThrow("Couldn't save");
    await vi.advanceTimersByTimeAsync(10_000);
    await failure;
    fetcher.mockReturnValue(new Promise(() => {}));
    const hung = queueAcknowledgedUserPreferencePatch("keep_warm", { cells: { h1: {} } });
    const timedOut = expect(hung).rejects.toThrow("wasn't confirmed");
    await vi.advanceTimersByTimeAsync(30_001);
    await timedOut;
  });
});

describe("user preference synchronization", () => {
  it("syncs idle reminders independently and clears them on an owner switch", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: { child_idle: { enabled: true } } },
      fetcher,
      "alice",
    );
    expect(JSON.parse(localStorage.getItem("omnigent:child-idle") ?? "null")).toEqual({
      enabled: true,
    });
    expect(localStorage.getItem("omnigent:keep-warm")).toBeNull();
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "bob");
    expect(localStorage.getItem("omnigent:child-idle")).toBeNull();
  });
  it("retains a legacy worktree opt-in on an older server until migration", async () => {
    localStorage.setItem("omnigent:always-use-worktree", "true");
    await initializeUserPreferencesSync(undefined, vi.fn(), "alice");
    expect(readAlwaysUseWorktree()).toBe(true);
    expect(localStorage.getItem("omnigent:always-use-worktree")).toBe("true");
  });

  it("clears the prior owner's legacy worktree opt-in on an older-server switch", async () => {
    localStorage.setItem("omnigent:always-use-worktree", "true");
    localStorage.setItem("omnigent:user-preferences-owner", "server-a:alice");
    await initializeUserPreferencesSync(undefined, vi.fn(), "bob", "server-a");
    expect(readAlwaysUseWorktree()).toBe(false);
    expect(localStorage.getItem("omnigent:always-use-worktree")).toBeNull();
  });
  it("retries migration after a first initialization failure", async () => {
    vi.useFakeTimers();
    localStorage.setItem("omnigent:always-use-worktree", "true");
    await initializeUserPreferencesSync(
      null,
      vi.fn().mockResolvedValue(new Response(null, { status: 503 })),
      "alice",
    );
    expect(localStorage.getItem("omnigent:always-use-worktree")).toBe("true");
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    await vi.advanceTimersByTimeAsync(1000);
    expect(readAlwaysUseWorktree()).toBe(true);
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences/worktree_defaults",
      expect.objectContaining({ body: JSON.stringify({ value: { alwaysUseWorktree: true } }) }),
    );
  });
  it("migrates a legacy worktree opt-in once even for an existing server envelope", async () => {
    vi.useFakeTimers();
    localStorage.setItem("omnigent:always-use-worktree", "true");
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    expect(readAlwaysUseWorktree()).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences/worktree_defaults",
      expect.objectContaining({ body: JSON.stringify({ value: { alwaysUseWorktree: true } }) }),
    );
    expect(localStorage.getItem("omnigent:always-use-worktree")).toBeNull();
    fetcher.mockClear();
    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: {
          worktree_defaults: { alwaysUseWorktree: false },
        },
      },
      fetcher,
      "alice",
    );
    await vi.advanceTimersByTimeAsync(1000);
    expect(readAlwaysUseWorktree()).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("uses the server worktree choice and syncs an explicit opt-out", async () => {
    vi.useFakeTimers();
    localStorage.setItem("omnigent:always-use-worktree", "true");
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: {
          worktree_defaults: { alwaysUseWorktree: true },
        },
      },
      fetcher,
      "alice",
    );
    expect(readAlwaysUseWorktree()).toBe(true);
    writeAlwaysUseWorktree(false);
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences/worktree_defaults",
      expect.objectContaining({ body: JSON.stringify({ value: { alwaysUseWorktree: false } }) }),
    );
  });

  it("does not migrate a previous account's device-only worktree opt-in", async () => {
    vi.useFakeTimers();
    localStorage.setItem("omnigent:user-preferences-owner", "__default__:alice");
    localStorage.setItem("omnigent:always-use-worktree", "true");
    const fetcher = vi.fn();
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "bob");
    await vi.advanceTimersByTimeAsync(1000);
    expect(readAlwaysUseWorktree()).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("does nothing when an older Server omits the preferences field", async () => {
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    const fetcher = vi.fn();
    await initializeUserPreferencesSync(undefined, fetcher);
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("__default__:__local__");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not seed a new Server from an older Server's local preferences", async () => {
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    await initializeUserPreferencesSync(undefined, vi.fn(), "alice", "server-a");
    const serverB = vi.fn().mockResolvedValue(Response.json({ version: 1, settings: {} }));

    await initializeUserPreferencesSync(null, serverB, "alice", "server-b");

    expect(serverB).toHaveBeenCalledWith(
      "/v1/me/preferences",
      expect.objectContaining({ body: JSON.stringify({ version: 1, settings: {} }) }),
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("server-b:alice");
  });

  it("downgrades an unrecognized preferences envelope to device-only behavior", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn();
    await initializeUserPreferencesSync({ version: 2, settings: {} }, fetcher, "alice", "server-a");

    queueUserPreferencePatch("context_indicator", "compact");
    await vi.advanceTimersByTimeAsync(500);

    expect(fetcher).not.toHaveBeenCalled();
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("server-a:alice");
  });

  it("collects and hydrates the Agent badge namespace", async () => {
    const badgePreferences = {
      version: 1,
      enabled: false,
      entries: {
        "agent-a": { label: "A", borderColor: "#123456", textColor: "#abcdef" },
      },
    };
    localStorage.setItem("omnigent:agent-badge-preferences", JSON.stringify(badgePreferences));
    expect(collectLocalUserPreferences().settings.agent_badges).toEqual(badgePreferences);

    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: { agent_badges: { ...badgePreferences, enabled: true } },
      },
      vi.fn(),
    );

    expect(JSON.parse(localStorage.getItem("omnigent:agent-badge-preferences") ?? "null")).toEqual({
      ...badgePreferences,
      enabled: true,
    });
  });

  it("collects, patches, and hydrates the Approval timeout namespace", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    const preferences = { timeoutMinutes: 10, stopTurn: false, asyncApprovals: false };
    writeApprovalTimeoutPreferences(preferences);
    await vi.advanceTimersByTimeAsync(251);
    expect(collectLocalUserPreferences().settings.approval_timeout).toEqual(preferences);
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences/approval_timeout",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ value: preferences }),
      }),
    );

    writeApprovalTimeoutPreferences({ timeoutMinutes: 50, stopTurn: true, asyncApprovals: true });
    await vi.advanceTimersByTimeAsync(500);
    expect(localStorage.getItem("omnigent:approval-timeout")).toBeNull();
    expect(fetcher).toHaveBeenLastCalledWith(
      "/v1/me/preferences/approval_timeout",
      expect.objectContaining({ body: JSON.stringify({ value: null }) }),
    );

    resetUserPreferencesSyncForTests();
    await initializeUserPreferencesSync(
      { version: 1, settings: { approval_timeout: { timeoutMinutes: 5, stopTurn: false } } },
      vi.fn(),
      "alice",
    );
    expect(readApprovalTimeoutPreferences()).toEqual({
      timeoutMinutes: 5,
      stopTurn: false,
      asyncApprovals: true,
    });
  });

  it("collects and hydrates the Agent pins namespace", async () => {
    const pins = { ids: ["ag_polly", "ca_reviewer"] };
    localStorage.setItem("omnigent:agent-pins", JSON.stringify(pins));
    expect(collectLocalUserPreferences().settings.agent_pins).toEqual(pins);

    await initializeUserPreferencesSync(
      { version: 1, settings: { agent_pins: { ids: ["ca_reviewer"] } } },
      vi.fn(),
    );

    expect(JSON.parse(localStorage.getItem("omnigent:agent-pins") ?? "null")).toEqual({
      ids: ["ca_reviewer"],
    });
  });

  it("collects and hydrates the calling-defaults namespaces", async () => {
    const callingDefaults = {
      "host-1": { codex: { model: "gpt-6-sol", effort: "high" } },
    };
    const callingLast = {
      enabled: true,
      "p:abc": { "host-1": { last_agent_id: "codex-sdk", agents: {} } },
    };
    localStorage.setItem("omnigent:calling-defaults", JSON.stringify(callingDefaults));
    localStorage.setItem("omnigent:calling-last", JSON.stringify(callingLast));
    expect(collectLocalUserPreferences().settings.calling_defaults).toEqual(callingDefaults);
    expect(collectLocalUserPreferences().settings.calling_last).toEqual(callingLast);

    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: { calling_defaults: { "host-2": {} }, calling_last: { enabled: false } },
      },
      vi.fn(),
    );

    expect(JSON.parse(localStorage.getItem("omnigent:calling-defaults") ?? "null")).toEqual({
      "host-2": {},
    });
    expect(JSON.parse(localStorage.getItem("omnigent:calling-last") ?? "null")).toEqual({
      enabled: false,
    });
  });

  it("patches and hydrates the keep-warm namespace", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    const preferences = {
      default: KEEP_WARM_DEFAULTS.default,
      agents: {
        "claude-native-ui": { main: true, child: true, intervalSeconds: 3300, maxSeconds: 14400 },
      },
      hostOfflineArchiveSeconds: 7200,
    };
    writeKeepWarmPreferences(preferences, [{ id: "claude-native-ui", harness: "claude-native" }]);
    await vi.advanceTimersByTimeAsync(251);

    expect(collectLocalUserPreferences().settings.keep_warm).toEqual(preferences);
    expect(fetcher.mock.calls.filter(([url]) => url === "/v1/me/preferences/keep_warm")).toEqual([
      [
        "/v1/me/preferences/keep_warm",
        expect.objectContaining({ method: "PATCH", body: JSON.stringify({ value: preferences }) }),
      ],
    ]);

    resetUserPreferencesSyncForTests();
    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: { keep_warm: { ...preferences, hostOfflineArchiveSeconds: 3600 } },
      },
      vi.fn(),
      "alice",
    );

    expect(JSON.parse(localStorage.getItem(KEEP_WARM_STORAGE_KEY) ?? "null")).toEqual({
      ...preferences,
      hostOfflineArchiveSeconds: 3600,
    });
    expect(JSON.parse(localStorage.getItem(KEEP_WARM_STORAGE_KEY) ?? "null").default).toEqual(
      KEEP_WARM_DEFAULTS.default,
    );
  });

  it("announces an acknowledged patch with its namespace", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    const listener = vi.fn();
    window.addEventListener(USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT, listener);

    writeKeepWarmPreferences(
      {
        default: KEEP_WARM_DEFAULTS.default,
        agents: {
          "claude-native-ui": {
            main: true,
            child: false,
            intervalSeconds: 3300,
            maxSeconds: 14400,
            coldAfterSeconds: 0,
          },
        },
        hostOfflineArchiveSeconds: 14400,
      },
      [{ id: "claude-native-ui", harness: "claude-native" }],
    );
    await vi.advanceTimersByTimeAsync(500);

    expect(listener).toHaveBeenCalledWith(
      expect.objectContaining({ detail: { namespace: "keep_warm" } }),
    );
    window.removeEventListener(USER_PREFERENCES_PATCH_ACKNOWLEDGED_EVENT, listener);
  });

  it("cancels queued sync when switching to an older Server", async () => {
    vi.useFakeTimers();
    const currentServer = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, currentServer, "alice");
    queueUserPreferencePatch("context_indicator", "compact");

    const oldServer = vi.fn();
    await initializeUserPreferencesSync(undefined, oldServer, "alice");
    await vi.advanceTimersByTimeAsync(500);
    queueUserPreferencePatch("context_indicator", "compact");
    await vi.advanceTimersByTimeAsync(500);

    expect(currentServer).not.toHaveBeenCalled();
    expect(oldServer).not.toHaveBeenCalled();
  });

  it("uploads existing local preferences exactly when the user has no server value", async () => {
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    const fetcher = vi
      .fn()
      .mockResolvedValue(Response.json({ version: 1, settings: { context_indicator: "compact" } }));
    await initializeUserPreferencesSync(null, fetcher, "alice");
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify(collectLocalUserPreferences()),
      }),
    );
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("__default__:alice");
  });

  it("does not seed a new account from another user's local cache", async () => {
    localStorage.setItem("omnigent:user-preferences-owner", "__default__:alice");
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    const fetcher = vi.fn().mockResolvedValue(Response.json({ version: 1, settings: {} }));
    await initializeUserPreferencesSync(null, fetcher, "bob");
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences",
      expect.objectContaining({
        body: JSON.stringify({ version: 1, settings: {} }),
      }),
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("__default__:bob");
  });

  it("clears another scope's local values even when initialization fails", async () => {
    localStorage.setItem("omnigent:user-preferences-owner", "server-a:alice");
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));

    await initializeUserPreferencesSync(null, fetcher, "alice", "server-b");

    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences",
      expect.objectContaining({
        body: JSON.stringify({ version: 1, settings: {} }),
      }),
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
    expect(localStorage.getItem("omnigent:user-preferences-owner")).toBe("server-b:alice");
  });

  it("hydrates the persisted winner after a racing first-device initialization", async () => {
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    const fetcher = vi
      .fn()
      .mockResolvedValue(Response.json({ version: 1, settings: { context_indicator: null } }));
    await initializeUserPreferencesSync(null, fetcher, "alice");
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
  });

  it("lets the server win while retaining device-only assistant position", async () => {
    localStorage.setItem(
      "omnigent:mobile-assistant-preferences",
      JSON.stringify({ version: 2, enabled: true, buttons: [], position: { x: 0.2, y: 0.8 } }),
    );
    const fetcher = vi.fn();
    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: {
          context_indicator: "compact",
          mobile_assistant: { version: 2, enabled: false, buttons: [{ id: "one" }] },
        },
      },
      fetcher,
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-preferences")!)).toEqual({
      version: 2,
      enabled: false,
      buttons: [{ id: "one" }],
      position: { x: 0.2, y: 0.8 },
    });
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-device-state")!)).toEqual({
      position: { x: 0.2, y: 0.8 },
    });
  });

  it("syncs both mobile key sets and row switches while keeping placement local", async () => {
    const value = {
      version: 2,
      chatActionsRevision: 1,
      enabled: true,
      buttons: [],
      terminalButtons: [{ id: "ctrl", label: "Ctrl", kind: "modifier", modifier: "ctrl" }],
      terminalKeysEnabled: false,
      terminalSecondRow: false,
    };
    localStorage.setItem(
      "omnigent:mobile-assistant-preferences",
      JSON.stringify({ ...value, position: { x: 0.2, y: 0.8 } }),
    );
    expect(collectLocalUserPreferences().settings.mobile_assistant).toEqual(value);
    await initializeUserPreferencesSync(
      { version: 1, settings: { mobile_assistant: value } },
      vi.fn(),
    );
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-preferences")!)).toEqual({
      ...value,
      position: { x: 0.2, y: 0.8 },
    });
  });

  it("retains device-only assistant placement when the user has no assistant namespace", async () => {
    localStorage.setItem(
      "omnigent:mobile-assistant-preferences",
      JSON.stringify({
        version: 2,
        enabled: true,
        buttons: [],
        dock: { edge: "right", offset: 0.4 },
      }),
    );
    await initializeUserPreferencesSync({ version: 1, settings: {} }, vi.fn(), "alice");
    expect(localStorage.getItem("omnigent:mobile-assistant-preferences")).toBeNull();
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-device-state")!)).toEqual({
      dock: { edge: "right", offset: 0.4 },
    });
  });

  it("ignores device-only assistant placement received from the Server", async () => {
    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: {
          mobile_assistant: {
            version: 2,
            enabled: true,
            buttons: [],
            dock: { edge: "left", offset: 0.25 },
            position: { x: 0.1, y: 0.2 },
          },
        },
      },
      vi.fn(),
    );

    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-preferences")!)).toEqual({
      version: 2,
      enabled: true,
      buttons: [],
    });
    expect(localStorage.getItem("omnigent:mobile-assistant-device-state")).toBeNull();
  });

  it("keeps the latest local assistant placement across later hydration", async () => {
    localStorage.setItem(
      "omnigent:mobile-assistant-preferences",
      JSON.stringify({ version: 2, enabled: true, buttons: [], position: { x: 0.2, y: 0.8 } }),
    );
    const serverEnvelope = {
      version: 1 as const,
      settings: { mobile_assistant: { version: 2, enabled: false, buttons: [] } },
    };
    await initializeUserPreferencesSync(serverEnvelope, vi.fn(), "alice");
    localStorage.setItem(
      "omnigent:mobile-assistant-preferences",
      JSON.stringify({ version: 2, enabled: false, buttons: [], position: { x: 0.6, y: 0.4 } }),
    );

    await initializeUserPreferencesSync(serverEnvelope, vi.fn(), "alice");

    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-preferences")!)).toEqual({
      version: 2,
      enabled: false,
      buttons: [],
      position: { x: 0.6, y: 0.4 },
    });
    expect(JSON.parse(localStorage.getItem("omnigent:mobile-assistant-device-state")!)).toEqual({
      position: { x: 0.6, y: 0.4 },
    });
  });

  it("debounces independent namespace patches", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher);
    queueUserPreferencePatch("usage_context", { version: 1, showCodexRateLimits: false });
    queueUserPreferencePatch("usage_context", { version: 1, showCodexRateLimits: true });
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith(
      "/v1/me/preferences/usage_context",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ value: { version: 1, showCodexRateLimits: true } }),
      }),
    );
  });

  it("coalesces host colour picks into one full-map patch with an auto reset", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    patchHostColor("h1", "blue");
    patchHostColor("h2", "green");
    await vi.advanceTimersByTimeAsync(251);

    const hostColorPatches = fetcher.mock.calls.filter(
      ([url]) => url === "/v1/me/preferences/host_colors",
    );
    expect(hostColorPatches).toHaveLength(1);
    expect(fetcher).toHaveBeenLastCalledWith(
      "/v1/me/preferences/host_colors",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ value: { h1: "blue", h2: "green" } }),
      }),
    );

    // A pick then a reset of the same host keeps every key in the map, with
    // the reset as the "auto" tombstone (a shallow merge cannot delete).
    patchHostColor("h3", "red");
    patchHostColor("h3", null);
    await vi.advanceTimersByTimeAsync(251);

    expect(fetcher.mock.calls.at(-1)?.[1]).toEqual(
      expect.objectContaining({
        body: JSON.stringify({ value: { h1: "blue", h2: "green", h3: "auto" } }),
      }),
    );
  });

  it("never replays an acknowledged host colour after hydration or an account switch", async () => {
    vi.useFakeTimers();
    let server: Record<string, string> = {};
    const fetcher = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        server = { ...server, ...(JSON.parse(String(init.body)).value as Record<string, string>) };
        return new Response(null, { status: 200 });
      }
      return Response.json({
        user_id: "alice",
        preferences: { version: 1, settings: { host_colors: { ...server } } },
      });
    });
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    patchHostColor("h1", "blue");
    patchHostColor("h2", "green");
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher.mock.calls.at(-1)?.[1]).toEqual(
      expect.objectContaining({
        body: JSON.stringify({ value: { h1: "blue", h2: "green" } }),
      }),
    );

    // Another client changes h1; the next refresh hydrates the new value.
    server = { ...server, h1: "red" };
    await refreshUserPreferencesFromServer();
    expect(readHostColorPreferences()).toEqual({ h1: "red", h2: "green" });

    patchHostColor("h2", "purple");
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher.mock.calls.at(-1)?.[1]).toEqual(
      expect.objectContaining({
        body: JSON.stringify({ value: { h1: "red", h2: "purple" } }),
      }),
    );

    // A different account's scope has no colours; the previous account's
    // keys must not ride along with the next edit.
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "bob");
    patchHostColor("h3", "orange");
    await vi.advanceTimersByTimeAsync(251);
    expect(fetcher.mock.calls.at(-1)?.[1]).toEqual(
      expect.objectContaining({
        body: JSON.stringify({ value: { h3: "orange" } }),
      }),
    );
  });

  it("serializes overlapping patches so the latest value is persisted last", async () => {
    vi.useFakeTimers();
    let resolveFirst!: (response: Response) => void;
    let resolveSecond!: (response: Response) => void;
    const fetcher = vi
      .fn()
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveSecond = resolve;
        }),
      );
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    queueUserPreferencePatch("context_indicator", "compact");
    await vi.advanceTimersByTimeAsync(251);

    localStorage.removeItem("omnigent:context-indicator-mode");
    queueUserPreferencePatch("context_indicator", null);
    await vi.advanceTimersByTimeAsync(251);

    expect(fetcher).toHaveBeenCalledTimes(1);
    resolveFirst(new Response(null, { status: 200 }));
    await vi.runOnlyPendingTimersAsync();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls.map((call) => (call[1] as RequestInit).body)).toEqual([
      JSON.stringify({ value: "compact" }),
      JSON.stringify({ value: null }),
    ]);
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).not.toBeNull();

    resolveSecond(new Response(null, { status: 200 }));
    await Promise.resolve();
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).toBeNull();
  });

  it("retries an unacknowledged patch and clears its durable dirty marker", async () => {
    vi.useFakeTimers();
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({ version: 1, showCodexRateLimits: true }),
    );
    queueUserPreferencePatch("usage_context", {
      version: 1,
      showCodexRateLimits: true,
    });
    await vi.advanceTimersByTimeAsync(251);
    await vi.runOnlyPendingTimersAsync();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).toBeNull();
  });

  it("still retries a 500 patch as before", async () => {
    vi.useFakeTimers();
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 500 }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    queueUserPreferencePatch("usage_context", { version: 1, showCodexRateLimits: true });
    await vi.advanceTimersByTimeAsync(251);
    await vi.runOnlyPendingTimersAsync();
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).toBeNull();
  });

  it("treats a 422 namespace patch as permanent and restores the Server value", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 422 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: { usage_context: { version: 1, showCodexRateLimits: true } } },
      fetcher,
      "alice",
    );
    const rejected = vi.fn();
    window.addEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, rejected);

    localStorage.setItem(
      "omnigent:usage-context-preferences",
      JSON.stringify({ version: 1, showCodexRateLimits: false }),
    );
    queueUserPreferencePatch("usage_context", { version: 1, showCodexRateLimits: false });
    await vi.advanceTimersByTimeAsync(251);
    await vi.advanceTimersByTimeAsync(60_000);

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(rejected).toHaveBeenCalledWith(
      expect.objectContaining({ detail: { namespace: "usage_context", status: 422 } }),
    );
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).toBeNull();
    expect(JSON.parse(localStorage.getItem("omnigent:usage-context-preferences")!)).toEqual({
      version: 1,
      showCodexRateLimits: true,
    });
    window.removeEventListener(USER_PREFERENCES_PATCH_REJECTED_EVENT, rejected);
  });

  it("restores the last acknowledged value when a later patch is rejected", async () => {
    vi.useFakeTimers();
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(new Response(null, { status: 422 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    const saved = { version: 1, showCodexRateLimits: false };
    localStorage.setItem("omnigent:usage-context-preferences", JSON.stringify(saved));
    queueUserPreferencePatch("usage_context", saved);
    await vi.advanceTimersByTimeAsync(251);

    const rejected = { version: 1, showCodexRateLimits: true };
    localStorage.setItem("omnigent:usage-context-preferences", JSON.stringify(rejected));
    queueUserPreferencePatch("usage_context", rejected);
    await vi.advanceTimersByTimeAsync(251);

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(JSON.parse(localStorage.getItem("omnigent:usage-context-preferences")!)).toEqual(saved);
  });

  it("keeps a newer queued edit when the in-flight patch is rejected", async () => {
    vi.useFakeTimers();
    let resolveFirst!: (response: Response) => void;
    const fetcher = vi
      .fn()
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    const first = { version: 1, showCodexRateLimits: false };
    localStorage.setItem("omnigent:usage-context-preferences", JSON.stringify(first));
    queueUserPreferencePatch("usage_context", first);
    await vi.advanceTimersByTimeAsync(251);

    const second = { version: 1, showCodexRateLimits: true };
    localStorage.setItem("omnigent:usage-context-preferences", JSON.stringify(second));
    queueUserPreferencePatch("usage_context", second);
    await vi.advanceTimersByTimeAsync(251);

    expect(fetcher).toHaveBeenCalledTimes(1);
    resolveFirst(new Response(null, { status: 422 }));
    await vi.runOnlyPendingTimersAsync();

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls.map((call) => (call[1] as RequestInit).body)).toEqual([
      JSON.stringify({ value: first }),
      JSON.stringify({ value: second }),
    ]);
    expect(JSON.parse(localStorage.getItem("omnigent:usage-context-preferences")!)).toEqual(second);

    await vi.advanceTimersByTimeAsync(0);
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).toBeNull();
  });

  it("sends the newest edit when a rejected patch had both a queued and a pending edit behind it", async () => {
    vi.useFakeTimers();
    let resolveFirst!: (response: Response) => void;
    const fetcher = vi
      .fn()
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    const key = "omnigent:usage-context-preferences";
    const edit = (value: object) => {
      localStorage.setItem(key, JSON.stringify(value));
      queueUserPreferencePatch("usage_context", value);
    };

    const first = { version: 1, showCodexRateLimits: false };
    edit(first);
    await vi.advanceTimersByTimeAsync(251);
    edit({ version: 1, showCodexRateLimits: true });
    await vi.advanceTimersByTimeAsync(251);
    const third = { version: 1, showCodexRateLimits: true, showClaudeRateLimits: false };
    edit(third);

    expect(fetcher).toHaveBeenCalledTimes(1);
    resolveFirst(new Response(null, { status: 422 }));
    await vi.runOnlyPendingTimersAsync();
    await vi.runOnlyPendingTimersAsync();

    expect(fetcher.mock.calls.map((call) => (call[1] as RequestInit).body)).toEqual([
      JSON.stringify({ value: first }),
      JSON.stringify({ value: third }),
    ]);
    expect(JSON.parse(localStorage.getItem(key)!)).toEqual(third);
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).toBeNull();
  });

  it("cancels a pending account patch when the resolved user changes", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");
    queueUserPreferencePatch("context_indicator", "compact");
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "bob");
    await vi.advanceTimersByTimeAsync(500);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("cancels pending writes when the same owner switches servers", async () => {
    vi.useFakeTimers();
    const serverA = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    const serverB = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, serverA, "alice", "server-a");
    queueUserPreferencePatch("context_indicator", "compact");

    await initializeUserPreferencesSync({ version: 1, settings: {} }, serverB, "alice", "server-b");
    await vi.advanceTimersByTimeAsync(500);

    expect(serverA).not.toHaveBeenCalled();
    expect(serverB).not.toHaveBeenCalled();
  });

  it("retains an offline edit across A to B to A using the stable Server id", async () => {
    vi.useFakeTimers();
    const serverAFirst = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));
    const serverB = vi.fn();
    const serverAReturn = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      serverAFirst,
      "alice",
      "server-a",
      "server-a:connection-1",
    );
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    queueUserPreferencePatch("context_indicator", "compact");
    await vi.advanceTimersByTimeAsync(251);
    expect(serverAFirst).toHaveBeenCalledTimes(1);

    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      serverB,
      "alice",
      "server-b",
      "server-b:connection-1",
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      serverAReturn,
      "alice",
      "server-a",
      "server-a:connection-2",
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");
    await vi.advanceTimersByTimeAsync(251);

    expect(serverB).not.toHaveBeenCalled();
    expect(serverAReturn).toHaveBeenCalledWith(
      "/v1/me/preferences/context_indicator",
      expect.objectContaining({ body: JSON.stringify({ value: "compact" }) }),
    );
    expect(localStorage.getItem("omnigent:user-preferences-dirty")).toBeNull();
  });

  it("recovers an offline edit after a reload on the same stable Server", async () => {
    vi.useFakeTimers();
    const beforeReload = vi.fn().mockResolvedValue(new Response(null, { status: 503 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      beforeReload,
      "alice",
      "server-a",
      "server-a:connection-1",
    );
    localStorage.setItem("omnigent:context-indicator-mode", "compact");
    queueUserPreferencePatch("context_indicator", "compact");
    await vi.advanceTimersByTimeAsync(251);

    resetUserPreferencesSyncForTests();
    const afterReload = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      { version: 1, settings: {} },
      afterReload,
      "alice",
      "server-a",
      "server-a:connection-2",
    );
    await vi.advanceTimersByTimeAsync(251);

    expect(afterReload).toHaveBeenCalledWith(
      "/v1/me/preferences/context_indicator",
      expect.objectContaining({ body: JSON.stringify({ value: "compact" }) }),
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");
    expect(localStorage.getItem("omnigent:user-preferences-dirty-values")).toBeNull();
  });

  it("ignores a delayed foreground refresh from the previous server", async () => {
    let resolveA!: (response: Response) => void;
    const serverA = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveA = resolve;
      }),
    );
    const serverB = vi.fn();
    await initializeUserPreferencesSync(
      { version: 1, settings: { context_indicator: "compact" } },
      serverA,
      "alice",
      "server-a",
    );
    const refresh = refreshUserPreferencesFromServer();
    await initializeUserPreferencesSync(
      { version: 1, settings: { context_indicator: "compact" } },
      serverB,
      "alice",
      "server-b",
    );

    resolveA(
      Response.json({
        user_id: "alice",
        preferences: { version: 1, settings: { context_indicator: null } },
      }),
    );
    await refresh;

    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");
  });

  it("refreshes server settings when a long-lived client returns to the foreground", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      Response.json({
        user_id: "alice",
        preferences: { version: 1, settings: {} },
      }),
    );
    await initializeUserPreferencesSync(
      { version: 1, settings: { context_indicator: "compact" } },
      fetcher,
      "alice",
    );
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBe("compact");

    await refreshUserPreferencesFromServer();

    expect(fetcher).toHaveBeenCalledWith("/v1/me", { cache: "no-store" });
    expect(localStorage.getItem("omnigent:context-indicator-mode")).toBeNull();
  });
});

it("hydrates the voice namespace and refreshes it from another device", async () => {
  const changed = vi.fn();
  window.addEventListener("omnigent:voice-input-changed", changed);
  const fetcher = vi.fn().mockResolvedValue(
    new Response(
      JSON.stringify({
        user_id: "alice",
        preferences: {
          version: 1,
          settings: { voice_input: { words: "updated words", mode: "manual" } },
        },
      }),
      { status: 200 },
    ),
  );
  await initializeUserPreferencesSync(
    { version: 1, settings: { voice_input: { words: "initial words" } } },
    fetcher,
    "alice",
  );
  expect(JSON.parse(localStorage.getItem("omnigent:voice-input")!)).toEqual({
    words: "initial words",
  });
  await refreshUserPreferencesFromServer();
  expect(JSON.parse(localStorage.getItem("omnigent:voice-input")!)).toEqual({
    words: "updated words",
    mode: "manual",
  });
  expect(changed).toHaveBeenCalled();
  window.removeEventListener("omnigent:voice-input-changed", changed);
});

describe("phone session menu pins", () => {
  /** A terminal-bearing mobile_assistant value, as the account holds it. */
  function mobileValue() {
    const terminalBar = defaultTerminalBar();
    const terminalButtons = projectLegacy(terminalBar);
    return {
      version: 2 as const,
      enabled: true,
      buttons: [],
      chatActionsRevision: 1 as const,
      terminalBar,
      terminalButtons,
      terminalSecondRow: true,
      terminalLegacyBasis: legacyFingerprint(terminalButtons, true),
    };
  }

  function requestPath(call: unknown[]): string {
    return String(call[0]);
  }

  /** The `value` a PATCH carried; a GET has no body. */
  function requestValue(call: unknown[]): unknown {
    const body = (call[1] as { body?: unknown } | undefined)?.body;
    return typeof body === "string" ? JSON.parse(body).value : undefined;
  }

  it("carries the pinned rows in a namespace of their own", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn();
    fetcher.mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    toggleMobileMenuPin("files");
    await vi.advanceTimersByTimeAsync(251);

    expect(requestPath(fetcher.mock.calls[0])).toBe("/v1/me/preferences/mobile_menu_pins");
    expect(requestValue(fetcher.mock.calls[0])).toEqual({
      version: 1,
      pinnedMenuItems: ["shells", "files"],
    });
    expect(readMobileMenuPinsPreferences().pinnedMenuItems).toEqual(["shells", "files"]);
  });

  it("keeps the terminal prefs and sends no null when a pin save is pending at refresh", async () => {
    vi.useFakeTimers();
    const terminal = mobileValue();
    const fetcher = vi.fn();
    fetcher.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input) === "/v1/me") {
        return Response.json(
          {
            user_id: "alice",
            preferences: { version: 1, settings: { mobile_assistant: terminal } },
          },
          { status: 200 },
        );
      }
      return new Response(null, { status: 200 });
    });
    await initializeUserPreferencesSync(
      { version: 1, settings: { mobile_assistant: terminal } },
      fetcher,
      "alice",
    );

    // The pin save is still inside its debounce window when the refetch lands.
    toggleMobileMenuPin("files");
    await refreshUserPreferencesFromServer();
    await vi.advanceTimersByTimeAsync(400);

    const bodies = fetcher.mock.calls
      .filter((call) => typeof (call[1] as { body?: unknown } | undefined)?.body === "string")
      .map((call) => JSON.parse((call[1] as { body: string }).body));
    // The pins are restored as their own object, never as a namespace-wide
    // value that would delete mobile_assistant.
    expect(bodies.some((body) => body.value === null)).toBe(false);
    for (const call of fetcher.mock.calls) {
      const value = requestValue(call);
      if (value === undefined || !JSON.stringify(value).includes("pinnedMenuItems")) continue;
      expect(requestPath(call)).toBe("/v1/me/preferences/mobile_menu_pins");
    }
    expect(readMobileMenuPinsPreferences().pinnedMenuItems).toEqual(["shells", "files"]);
    // The terminal layout this device already had is untouched.
    expect(readMobileAssistantPreferences().terminalBar).toEqual(terminal.terminalBar);
    expect(readMobileAssistantPreferences().terminalButtons).toEqual(terminal.terminalButtons);
    const pinCall = fetcher.mock.calls.find((call) =>
      requestPath(call).endsWith("/mobile_menu_pins"),
    );
    expect(pinCall).toBeDefined();
    expect(requestValue(pinCall!)).toEqual({
      version: 1,
      pinnedMenuItems: ["shells", "files"],
    });
  });

  it("reads the first pin of an empty account back on the next fetch", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn();
    fetcher.mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync({ version: 1, settings: {} }, fetcher, "alice");

    toggleMobileMenuPin("files");
    await vi.advanceTimersByTimeAsync(251);
    const sent = requestValue(fetcher.mock.calls[0]);
    expect(sent).toEqual({ version: 1, pinnedMenuItems: ["shells", "files"] });

    // The Server keeps exactly that value; another device — or this one after a
    // reload — must read it as the pins, not fall back to the stock choice.
    localStorage.removeItem(MOBILE_MENU_PINS_STORAGE_KEY);
    fetcher.mockResolvedValueOnce(
      Response.json(
        {
          user_id: "alice",
          preferences: { version: 1, settings: { mobile_menu_pins: sent } },
        },
        { status: 200 },
      ),
    );
    await refreshUserPreferencesFromServer();
    expect(readMobileMenuPinsPreferences().pinnedMenuItems).toEqual(["shells", "files"]);
  });

  it("lands a terminal save queued while a pin save is in flight", async () => {
    vi.useFakeTimers();
    let releasePin!: (response: Response) => void;
    const fetcher = vi.fn();
    fetcher.mockImplementation((input: RequestInfo | URL) => {
      if (String(input).endsWith("/mobile_menu_pins")) {
        return new Promise<Response>((resolve) => {
          releasePin = resolve;
        });
      }
      return Promise.resolve(new Response(null, { status: 200 }));
    });
    await initializeUserPreferencesSync(
      { version: 1, settings: { mobile_assistant: mobileValue() } },
      fetcher,
      "alice",
    );

    toggleMobileMenuPin("files");
    await vi.advanceTimersByTimeAsync(251);
    expect(
      writeTerminalKeyPreferences({ terminalKeyboard: { v: 1, bindings: { next: true } } }),
    ).toBe("saved");
    await vi.advanceTimersByTimeAsync(251);

    // The older pin response arrives after the terminal save was queued.
    releasePin(new Response(null, { status: 200 }));
    await vi.advanceTimersByTimeAsync(400);

    const paths = fetcher.mock.calls.map(requestPath);
    expect(paths).toContain("/v1/me/preferences/mobile_menu_pins");
    expect(paths).toContain("/v1/me/preferences/mobile_assistant");
    const terminalCall = fetcher.mock.calls.find((call) =>
      requestPath(call).endsWith("/mobile_assistant"),
    );
    expect((requestValue(terminalCall!) as { terminalKeyboard: unknown }).terminalKeyboard).toEqual(
      { v: 1, bindings: { next: true } },
    );
  });

  it("does not let a terminal save from a stale device rewrite the pins", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn();
    fetcher.mockResolvedValue(new Response(null, { status: 200 }));
    await initializeUserPreferencesSync(
      {
        version: 1,
        settings: {
          mobile_assistant: mobileValue(),
          mobile_menu_pins: { version: 1, pinnedMenuItems: ["files"] },
        },
      },
      fetcher,
      "alice",
    );

    // This device still holds an older pin list than the account.
    localStorage.setItem(
      MOBILE_MENU_PINS_STORAGE_KEY,
      JSON.stringify({ version: 1, pinnedMenuItems: ["shells"] }),
    );
    expect(writeTerminalKeyPreferences({ terminalKeyboard: { v: 1 } })).toBe("saved");
    await vi.advanceTimersByTimeAsync(251);

    expect(requestValue(fetcher.mock.calls[0])).not.toHaveProperty("pinnedMenuItems");
    expect(fetcher.mock.calls.map(requestPath)).not.toContain(
      "/v1/me/preferences/mobile_menu_pins",
    );
    expect(readMobileMenuPinsPreferences().pinnedMenuItems).toEqual(["shells"]);
  });
});
