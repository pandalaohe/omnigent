import { beforeEach, describe, expect, it } from "vitest";

import {
  CONTEXT_INDICATOR_DEFAULT,
  CONTEXT_INDICATOR_STORAGE_KEY,
  readContextIndicatorMode,
  writeContextIndicatorMode,
} from "./contextIndicatorPreferences";

beforeEach(() => localStorage.clear());

describe("context indicator preference", () => {
  it("keeps the existing context-window display by default", () => {
    expect(readContextIndicatorMode()).toBe(CONTEXT_INDICATOR_DEFAULT);
  });

  it("persists the opt-in compact-progress display", () => {
    writeContextIndicatorMode("compact");
    expect(localStorage.getItem(CONTEXT_INDICATOR_STORAGE_KEY)).toBe("compact");
    expect(readContextIndicatorMode()).toBe("compact");
  });

  it("falls back safely when storage is malformed", () => {
    localStorage.setItem(CONTEXT_INDICATOR_STORAGE_KEY, "future-mode");
    expect(readContextIndicatorMode()).toBe(CONTEXT_INDICATOR_DEFAULT);
  });
});
