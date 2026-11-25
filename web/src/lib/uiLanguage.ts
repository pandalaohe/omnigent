export type UiLanguage = "en" | "zh-CN";

export const uiLanguageStorageKey = "omnigent:ui-language";

function isUiLanguage(value: unknown): value is UiLanguage {
  return value === "en" || value === "zh-CN";
}

export function resolveUiLanguage(buildDefault: unknown): UiLanguage {
  if (typeof window !== "undefined") {
    try {
      const stored = window.localStorage.getItem(uiLanguageStorageKey);
      if (isUiLanguage(stored)) return stored;
    } catch {
      // Sandboxed contexts can deny access to local storage.
    }
  }
  return isUiLanguage(buildDefault) ? buildDefault : "en";
}

export const uiLanguage = resolveUiLanguage(import.meta.env.VITE_OMNIGENT_UI_LANGUAGE);
