import { afterEach, expect, it, vi } from "vitest";
import { resolveUiLanguage, uiLanguageStorageKey } from "./uiLanguage";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.removeItem(uiLanguageStorageKey);
});

it.each([
  [null, undefined, "en"],
  [null, "zh-CN", "zh-CN"],
  ["en", "zh-CN", "en"],
  ["zh-CN", "en", "zh-CN"],
  ["invalid", "zh-CN", "zh-CN"],
  ["invalid", "invalid", "en"],
])("resolves stored %s and build default %s to %s", (stored, buildDefault, expected) => {
  if (stored !== null) localStorage.setItem(uiLanguageStorageKey, stored);
  expect(resolveUiLanguage(buildDefault)).toBe(expected);
});

it("uses the build default when storage is inaccessible", () => {
  vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
    throw new Error("Storage unavailable");
  });
  expect(resolveUiLanguage("zh-CN")).toBe("zh-CN");
});
