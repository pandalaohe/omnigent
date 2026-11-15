"use strict";
const { permissionOrigin } = require("./browserPermissions");

function registerBrowserSettingsIpc({
  ipcMain,
  BrowserWindow,
  isTrusted,
  loadSettings,
  browserPermissionStore,
  confirmLocalNetwork,
}) {
  const object = (value) => value && typeof value === "object" && !Array.isArray(value);
  const sites = () =>
    Object.entries(
      object(loadSettings().browser_local_network_permissions)
        ? loadSettings().browser_local_network_permissions
        : {},
    )
      .filter(
        ([origin, allowed]) => permissionOrigin(origin) === origin && typeof allowed === "boolean",
      )
      .map(([origin, allowed]) => ({ origin, allowed }));
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
