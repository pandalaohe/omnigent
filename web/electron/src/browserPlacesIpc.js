"use strict";

const path = require("node:path");
const { pathToFileURL } = require("node:url");

/** Native child window paints above the page, while typing stays in the address bar. */
function registerBrowserPlacesIpc({
  ipcMain,
  BrowserWindow,
  places,
  isPinnedOriginSender,
  getRegistry,
}) {
  const popups = new Map();
  const page = path.join(__dirname, "browser_suggestions.html");
  function dismiss(parent) {
    const state = popups.get(parent);
    if (!state) return;
    popups.delete(parent);
    for (const event of ["move", "resize", "hide", "minimize", "closed"])
      parent.removeListener(event, state.close);
    parent.webContents.removeListener("did-start-navigation", state.close);
    if (!state.popup.isDestroyed()) state.popup.destroy();
  }
  ipcMain.handle("omnigent:browser-places", async (event, args = {}) => {
    if (!isPinnedOriginSender(event)) return { ok: false, error: "Untrusted sender." };
    const parent = BrowserWindow.fromWebContents(event.sender);
    try {
      if (args.action === "list")
        return { ok: true, bookmarks: places.bookmarks(), history: places.history() };
      if (args.action === "update")
        return {
          ok: true,
          bookmarks: places.update({
            ...args,
            title: args.title ?? getRegistry(event)?.get(args.viewId)?.view.webContents.getTitle(),
          }),
        };
      if (args.action === "clear-history") {
        places.clearHistory();
        return { ok: true };
      }
      if (args.action === "dismiss") {
        dismiss(parent);
        return { ok: true };
      }
      if (args.action === "key") {
        const state = popups.get(parent);
        if (!state) return { ok: true };
        if (args.key === "Escape") dismiss(parent);
        if (args.key === "ArrowDown" || args.key === "ArrowUp") {
          state.index =
            ((state.index < 0 && args.key === "ArrowUp" ? 0 : state.index) +
              (args.key === "ArrowDown" ? 1 : -1) +
              state.rows.length) %
            state.rows.length;
          state.popup.webContents.send("browser-suggestions", {
            rows: state.rows,
            index: state.index,
          });
        }
        if (args.key === "Enter" && state.index >= 0) {
          choose(state, state.index);
          return { ok: true, empty: true };
        }
        return { ok: true };
      }
      if (args.action !== "suggest") throw new Error("Unknown places action.");
      dismiss(parent);
      const registry = getRegistry(event);
      if (registry?.isSuppressed()) return { ok: true };
      const rows = places.suggestions(args.query, args.bookmarksOnly === true);
      if (!rows.length) return { ok: true, empty: true };
      const anchor = args.bounds;
      if (!anchor || ![anchor.x, anchor.y, anchor.width, anchor.height].every(Number.isFinite))
        throw new Error("Invalid address bar bounds.");
      const zoom = parent.webContents.getZoomFactor();
      const content = parent.getContentBounds();
      const width = Math.min(Math.max(240, Math.round(anchor.width * zoom)), content.width);
      const height = Math.min(rows.length * 52 + 12, content.height);
      const popup = new BrowserWindow({
        parent,
        frame: false,
        show: false,
        resizable: false,
        skipTaskbar: true,
        x: content.x + Math.max(0, Math.min(Math.round(anchor.x * zoom), content.width - width)),
        y:
          content.y +
          Math.max(
            0,
            Math.min(Math.round((anchor.y + anchor.height) * zoom), content.height - height),
          ),
        width,
        height,
        webPreferences: {
          preload: path.join(__dirname, "browser_suggestions_preload.js"),
          nodeIntegration: false,
          contextIsolation: true,
          sandbox: true,
          devTools: false,
        },
      });
      const state = {
        popup,
        parent,
        rows,
        index: -1,
        viewId: args.viewId,
        close: () => dismiss(parent),
      };
      popups.set(parent, state);
      for (const name of ["move", "resize", "hide", "minimize", "closed"])
        parent.on(name, state.close);
      parent.webContents.on("did-start-navigation", state.close);
      popup.on("closed", state.close);
      popup.on("blur", state.close);
      popup.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      popup.webContents.on("will-navigate", (e) => e.preventDefault());
      popup.once("ready-to-show", () => {
        if (!popup.isDestroyed()) popup.showInactive();
      });
      await popup.loadFile(page);
      return { ok: true };
    } catch (error) {
      return { ok: false, error: error.message };
    }
  });
  function choose(state, index) {
    const row = state.rows[index];
    if (!row) return;
    state.parent.webContents.send("browser-place-chosen", { viewId: state.viewId, url: row.url });
    dismiss(state.parent);
  }
  function senderState(event) {
    return [...popups.values()].find(
      (state) =>
        event.sender === state.popup.webContents &&
        event.senderFrame === event.sender.mainFrame &&
        event.senderFrame.url === pathToFileURL(page).href,
    );
  }
  ipcMain.handle("omnigent:browser-suggestions-data", (event) => {
    const state = senderState(event);
    return state ? { rows: state.rows, index: state.index } : null;
  });
  ipcMain.on("omnigent:browser-suggestions-choose", (event, index) => {
    const state = senderState(event);
    if (state && Number.isInteger(index)) choose(state, index);
  });
  return { dismiss };
}
module.exports = { registerBrowserPlacesIpc };
