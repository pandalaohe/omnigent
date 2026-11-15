"use strict";
const { permissionOrigin } = require("./browserPermissions");

function registerBrowserSettingsIpc({
  ipcMain,
  BrowserWindow,
  isTrusted,
  loadSettings,
  saveSettings,
  getOrigin = () => null,
  browserPermissionStore,
  confirmLocalNetwork,
}) {
  const object = (value) =>
    value &&
    typeof value === "object" &&
    (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
  const sites = () => {
    const saved = loadSettings().browser_local_network_permissions;
    return Object.entries(object(saved) ? saved : {})
      .filter(
        ([origin, allowed]) => permissionOrigin(origin) === origin && typeof allowed === "boolean",
      )
      .map(([origin, allowed]) => ({ origin, allowed }));
  };
  ipcMain.handle("omnigent:browser-remembered:list", (event) => {
    if (!isTrusted(event)) return { ok: false, error: "untrusted sender" };
    try {
      const saved = loadSettings().agent_browser_allowlist,
        current = getOrigin(event);
      const servers = Object.entries(object(saved) ? saved : {})
        .filter(
          ([, entries]) =>
            Array.isArray(entries) && entries.every((entry) => typeof entry === "string"),
        )
        .map(([origin, entries]) => ({ origin, entries, current: origin === current }))
        .sort((a, b) => Number(b.current) - Number(a.current) || a.origin.localeCompare(b.origin));
      return { ok: true, servers };
    } catch {
      return { ok: false, error: "Could not update remembered addresses." };
    }
  });
  ipcMain.handle("omnigent:browser-remembered:forget", (event, payload) => {
    if (!isTrusted(event)) return { ok: false, error: "untrusted sender" };
    if (
      !object(payload) ||
      typeof payload.origin !== "string" ||
      typeof payload.entry !== "string" ||
      payload.entry.length > 256
    )
      return { ok: false, error: "Invalid request." };
    try {
      const settings = loadSettings(),
        saved = settings.agent_browser_allowlist;
      if (
        !object(saved) ||
        !Object.hasOwn(saved, payload.origin) ||
        !Array.isArray(saved[payload.origin]) ||
        !saved[payload.origin].every((entry) => typeof entry === "string")
      )
        return { ok: false, error: "Invalid request." };
      const entries = saved[payload.origin].filter((entry) => entry !== payload.entry);
      const next = Object.fromEntries(
        Object.entries(saved).filter(([origin]) => origin !== payload.origin),
      );
      if (entries.length) next[payload.origin] = entries;
      settings.agent_browser_allowlist = next;
      saveSettings(settings);
      return { ok: true };
    } catch {
      return { ok: false, error: "Could not update remembered addresses." };
    }
  });
  const failure = { ok: false, error: "Could not update local network choices." };
  ipcMain.handle("omnigent:browser-local-network:list", (event) => {
    if (!isTrusted(event)) return { ok: false, error: "untrusted sender" };
    try {
      return { ok: true, sites: sites() };
    } catch {
      return failure;
    }
  });
  let pending = false;
  ipcMain.handle("omnigent:browser-local-network:set", async (event, payload) => {
    if (!isTrusted(event)) return { ok: false, error: "untrusted sender" };
    if (
      !object(payload) ||
      typeof payload.origin !== "string" ||
      payload.origin !== permissionOrigin(payload.origin) ||
      ![true, false, null].includes(payload.allowed)
    )
      return { ok: false, error: "Invalid request." };
    const { origin, allowed } = payload;
    try {
      if (allowed === true) {
        if (pending)
          return {
            ok: false,
            error: "A local network change is already waiting for confirmation.",
          };
        const win = BrowserWindow.fromWebContents(event.sender),
          revision = browserPermissionStore.revision(origin);
        if (!win || win.isDestroyed()) return { ok: false, error: "untrusted sender" };
        pending = true;
        try {
          let accepted = false;
          try {
            accepted = (await confirmLocalNetwork(win, origin)) === true;
          } catch {
            /* Native prompt dismissal fails closed. */
          }
          if (!accepted) return { ok: false, cancelled: true };
          if (!isTrusted(event)) return { ok: false, error: "untrusted sender" };
          if (
            win.isDestroyed() ||
            BrowserWindow.fromWebContents(event.sender) !== win ||
            browserPermissionStore.revision(origin) !== revision
          )
            return { ok: false, error: "Local network choices changed; reload and try again." };
          browserPermissionStore.set(origin, true);
        } finally {
          pending = false;
        }
      } else browserPermissionStore.set(origin, allowed === null ? undefined : false);
      return { ok: true, sites: sites() };
    } catch {
      return failure;
    }
  });
}
module.exports = { registerBrowserSettingsIpc };
