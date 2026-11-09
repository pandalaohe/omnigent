"use strict";

const { parseAllowlistEntry } = require("./browserUrlPolicy");

const levels = ["none", "agent", "all"];
const nativeEnters = new WeakMap();

function trackBrowserNavigationInput(contents, now = Date.now) {
  contents.on("before-input-event", (_event, input) => {
    if (input.type === "keyDown" && input.key === "Enter" && !input.isAutoRepeat)
      nativeEnters.set(contents, now());
  });
  contents.on("did-start-navigation", () => nativeEnters.delete(contents));
  contents.on("blur", () => nativeEnters.delete(contents));
}
function consumeBrowserNavigationInput(contents, now = Date.now) {
  const timestamp = nativeEnters.get(contents);
  nativeEnters.delete(contents);
  return timestamp !== undefined && now() - timestamp >= 0 && now() - timestamp <= 1500;
}
function normalizeBrowserAccess(raw = {}) {
  return {
    level: levels.includes(raw?.level) ? raw.level : "agent",
    ...Object.fromEntries(
      ["blocked", "ask", "allowed"].map((key) => [
        key,
        Array.isArray(raw?.[key])
          ? raw[key].filter((v) => typeof v === "string" && parseAllowlistEntry(v))
          : [],
      ]),
    ),
  };
}
function registerBrowserAccessSettings({
  ipcMain,
  isTrusted,
  loadSettings,
  saveSettings,
  confirmWidening,
}) {
  ipcMain.handle("omnigent:browser-access:get", (event) => {
    if (!isTrusted(event)) return { ok: false, error: "untrusted sender" };
    return { ok: true, access: normalizeBrowserAccess(loadSettings().browser_access) };
  });
  // Serialize confirmation and save, so a later narrowing cannot be overwritten by a stale dialog.
  let pending = false;
  ipcMain.handle("omnigent:browser-access:set", async (event, raw) => {
    if (!isTrusted(event)) return { ok: false, error: "untrusted sender" };
    if (pending) return { ok: false, error: "Browser access save already in progress" };
    if (
      !raw ||
      !levels.includes(raw.level) ||
      ["blocked", "ask", "allowed"].some(
        (key) => !Array.isArray(raw[key]) || raw[key].some((v) => !parseAllowlistEntry(v)),
      )
    )
      return { ok: false, error: "Invalid browser access settings" };
    pending = true;
    try {
      const before = normalizeBrowserAccess(loadSettings().browser_access);
      const next = normalizeBrowserAccess(raw);
      const widening =
        levels.indexOf(next.level) < levels.indexOf(before.level) ||
        next.allowed.some((v) => !before.allowed.includes(v)) ||
        ["blocked", "ask"].some((key) => before[key].some((v) => !next[key].includes(v)));
      if (widening && !(await confirmWidening(event, next)))
        return { ok: false, error: "Browser access change cancelled" };
      if (!isTrusted(event)) return { ok: false, error: "untrusted sender" };
      const settings = loadSettings();
      // Hand edits while the native dialog is open require a fresh save.
      if (
        JSON.stringify(normalizeBrowserAccess(settings.browser_access)) !== JSON.stringify(before)
      )
        return { ok: false, error: "Browser access settings changed; reload and try again" };
      settings.browser_access = next;
      saveSettings(settings);
      return { ok: true, access: next };
    } finally {
      pending = false;
    }
  });
}
module.exports = {
  normalizeBrowserAccess,
  trackBrowserNavigationInput,
  consumeBrowserNavigationInput,
  registerBrowserAccessSettings,
};
