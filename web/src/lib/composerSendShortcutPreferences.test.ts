import { afterEach, describe, expect, it, vi } from "vitest";
import {
  COMPOSER_SEND_SHORTCUT_STORAGE_KEY,
  DEFAULT_SUBMIT_WITH_MOD_ENTER,
  composerNewLineDisposition,
  isComposerAltNewlineKey,
  isComposerSendKey,
  isComposerSteerAllKey,
  parseSubmitWithModEnter,
  readSubmitWithModEnter,
  writeSubmitWithModEnter,
} from "./composerSendShortcutPreferences";
import { writeShortcutPreference } from "./keyboardShortcutPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("composerSendShortcutPreferences", () => {
  it("enables the alternate behavior only for the exact persisted value", () => {
    expect(parseSubmitWithModEnter("true")).toBe(true);
    expect(parseSubmitWithModEnter("false")).toBe(false);
    expect(parseSubmitWithModEnter("1")).toBe(false);
    expect(parseSubmitWithModEnter(null)).toBe(DEFAULT_SUBMIT_WITH_MOD_ENTER);
  });

  it("round-trips the opt-in and removes the default override", () => {
    writeSubmitWithModEnter(true);
    expect(readSubmitWithModEnter()).toBe(true);

    writeSubmitWithModEnter(false);
    expect(readSubmitWithModEnter()).toBe(false);
    expect(localStorage.getItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY)).toBeNull();
  });

  it("falls back safely when storage cannot be read or written", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });

    expect(() => writeSubmitWithModEnter(true)).not.toThrow();
    expect(readSubmitWithModEnter()).toBe(DEFAULT_SUBMIT_WITH_MOD_ENTER);
  });
});

describe("isComposerSendKey", () => {
  it("keeps Enter and the legacy modifier chord in default mode", () => {
    expect(isComposerSendKey({ key: "Enter" }, false, false)).toBe(true);
    expect(isComposerSendKey({ key: "Enter", shiftKey: true }, false, false)).toBe(false);
    expect(isComposerSendKey({ key: "Enter", metaKey: true }, false, false)).toBe(true);
    expect(isComposerSendKey({ key: "Enter", ctrlKey: true }, false, false)).toBe(true);
  });

  it("uses Command/Ctrl+Enter only for the alternate shortcut", () => {
    expect(isComposerSendKey({ key: "Enter" }, true, false)).toBe(false);
    expect(isComposerSendKey({ key: "Enter", metaKey: true }, true, false)).toBe(true);
    expect(isComposerSendKey({ key: "Enter", ctrlKey: true }, true, false)).toBe(true);
  });

  it("never submits from composition, modified chords, or mobile Enter", () => {
    expect(isComposerSendKey({ key: "Enter", metaKey: true, isComposing: true }, true, false)).toBe(
      false,
    );
    expect(isComposerSendKey({ key: "Enter", metaKey: true, shiftKey: true }, true, false)).toBe(
      false,
    );
    expect(isComposerSendKey({ key: "Enter", metaKey: true }, true, true)).toBe(false);
  });
});

describe("composerNewLineDisposition", () => {
  it("resolves both legacy newline modes", () => {
    expect(composerNewLineDisposition({ key: "Enter", shiftKey: true }, false, false)).toBe(
      "insert",
    );
    expect(composerNewLineDisposition({ key: "Enter" }, true, false)).toBe("insert");
    expect(composerNewLineDisposition({ key: "Enter" }, false, false)).toBe("none");
  });

  it("blocks the browser-owned default when newline is disabled", () => {
    writeShortcutPreference("newLine", { enabled: false });

    expect(composerNewLineDisposition({ key: "Enter", shiftKey: true }, false, false)).toBe(
      "block",
    );
  });

  it("matches a custom newline chord while preserving the disabled state", () => {
    writeShortcutPreference("newLine", {
      common: [{ code: "KeyL", modifiers: ["alt"] }],
      enabled: false,
    });

    expect(composerNewLineDisposition({ key: "l", code: "KeyL", altKey: true }, false, false)).toBe(
      "block",
    );
  });

  it("matches the default Alt+Enter newline in both modes and on touch", () => {
    for (const submitWithModEnter of [false, true]) {
      expect(
        composerNewLineDisposition({ key: "Enter", altKey: true }, submitWithModEnter, false),
      ).toBe("insert");
      expect(
        composerNewLineDisposition({ key: "Enter", altKey: true }, submitWithModEnter, true),
      ).toBe("insert");
    }
    expect(composerNewLineDisposition({ key: "Enter", shiftKey: true }, false, true)).toBe("none");
    expect(composerNewLineDisposition({ key: "Enter" }, false, true)).toBe("none");
  });

  it("replaces the default newline chords with a recorded binding", () => {
    writeShortcutPreference("newLine", { common: [{ code: "KeyJ", modifiers: ["control"] }] });

    expect(
      composerNewLineDisposition({ key: "j", code: "KeyJ", ctrlKey: true }, false, false),
    ).toBe("insert");
    expect(composerNewLineDisposition({ key: "Enter", altKey: true }, false, false)).toBe("none");
  });

  it("blocks the default Alt+Enter newline when the action is disabled", () => {
    writeShortcutPreference("newLine", { enabled: false });

    expect(composerNewLineDisposition({ key: "Enter", altKey: true }, false, false)).toBe("block");
  });

  it("keeps touch Enter and Shift+Enter native even when the newline is disabled", () => {
    writeShortcutPreference("newLine", { enabled: false });

    expect(composerNewLineDisposition({ key: "Enter", shiftKey: true }, false, true)).toBe("none");
    expect(composerNewLineDisposition({ key: "Enter" }, true, true)).toBe("none");
  });
});

describe("isComposerAltNewlineKey", () => {
  it("is Alt/Option+Enter, with or without Shift", () => {
    expect(isComposerAltNewlineKey({ key: "Enter", altKey: true })).toBe(true);
    expect(isComposerAltNewlineKey({ key: "Enter", altKey: true, shiftKey: true })).toBe(true);
    expect(isComposerAltNewlineKey({ key: "Enter" })).toBe(false);
    expect(isComposerAltNewlineKey({ key: "Enter", shiftKey: true })).toBe(false);
    expect(isComposerAltNewlineKey({ key: "a", altKey: true })).toBe(false);
  });

  it("leaves Ctrl/Cmd chords (including AltGr) and composition alone", () => {
    expect(isComposerAltNewlineKey({ key: "Enter", altKey: true, ctrlKey: true })).toBe(false);
    expect(isComposerAltNewlineKey({ key: "Enter", altKey: true, metaKey: true })).toBe(false);
    expect(isComposerAltNewlineKey({ key: "Enter", altKey: true, isComposing: true })).toBe(false);
  });
});

describe("isComposerSteerAllKey", () => {
  it("is Command/Ctrl+Enter in default mode", () => {
    expect(isComposerSteerAllKey({ key: "Enter" }, false, false)).toBe(false);
    expect(isComposerSteerAllKey({ key: "Enter", metaKey: true }, false, false)).toBe(true);
    expect(isComposerSteerAllKey({ key: "Enter", ctrlKey: true }, false, false)).toBe(true);
    expect(
      isComposerSteerAllKey({ key: "Enter", ctrlKey: true, shiftKey: true }, false, false),
    ).toBe(false);
  });

  it("moves to Command/Ctrl+Shift+Enter when Mod+Enter already sends", () => {
    expect(isComposerSteerAllKey({ key: "Enter", metaKey: true }, true, false)).toBe(false);
    expect(
      isComposerSteerAllKey({ key: "Enter", metaKey: true, shiftKey: true }, true, false),
    ).toBe(true);
  });

  it("never fires from composition, Alt chords, or mobile", () => {
    expect(
      isComposerSteerAllKey({ key: "Enter", metaKey: true, isComposing: true }, false, false),
    ).toBe(false);
    expect(isComposerSteerAllKey({ key: "Enter", metaKey: true, altKey: true }, false, false)).toBe(
      false,
    );
    expect(isComposerSteerAllKey({ key: "Enter", metaKey: true }, false, true)).toBe(false);
  });
});
