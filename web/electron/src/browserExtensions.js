/* eslint-disable no-await-in-loop -- Extension loading shares session state and must remain ordered. */
"use strict";

const path = require("node:path");
const { clearExtensionData } = require("./browserExtensionData");
const { guardExtensionCookies, extensionAccess } = require("./browserExtensionPermissions");
const { requestExtensionPermissions } = require("./browserExtensionPackages");

function createBrowserExtensions({ session, records, BrowserWindow, ElectronChromeExtensions }) {
  const workers = new Set();
  const views = new Map();
  const popups = new Set();
  let extension;
  const loadedExtensions = new Map();
  const allowedDirectories = new Map();
  let ready;
  let generation = 0;
  let loadingGeneration;
  let pendingLoad = Promise.resolve();
  const frameId = (url) => {
    try {
      const parsed = new URL(url);
      return parsed.protocol === "chrome-extension:" && loadedExtensions.has(parsed.hostname)
        ? parsed.hostname
        : null;
    } catch {
      return null;
    }
  };
  const isExtensionFrame = (event) => {
    try {
      return event.sender.session === session && frameId(event.senderFrame.url);
    } catch {
      return false;
    }
  };
  const broadcast = (keys, area, id) => {
    if (
      !["local", "session"].includes(area) ||
      !Array.isArray(keys) ||
      keys.some((key) => typeof key !== "string")
    )
      return;
    for (const worker of workers) {
      if (frameId(worker.scope) !== id) continue;
      try {
        worker.send("omnigent:extension-storage", keys, area);
      } catch {
        workers.delete(worker);
      }
    }
  };
  session.extensions.on("extension-loaded", (_event, loaded) => {
    if (extension?.id === loaded.id) extension = null;
    if (loadingGeneration === generation && allowedDirectories.has(path.resolve(loaded.path))) {
      loadedExtensions.set(loaded.id, loaded);
      if (allowedDirectories.get(path.resolve(loaded.path)).agentFillAllowed) extension = loaded;
    }
  });
  session.serviceWorkers.on("running-status-changed", (details) => {
    if (details.runningStatus !== "starting") return;
    const worker = session.serviceWorkers.getWorkerFromVersionID(details.versionId);
    if (!worker || !frameId(worker.scope)) return;
    worker.ipc.on("omnigent:extension-storage", (_event, keys, area) =>
      broadcast(keys, area, frameId(worker.scope)),
    );
    worker.ipc.on("omnigent:extension-storage-subscribe", () => workers.add(worker));
  });
  for (const type of ["frame", "service-worker"]) {
    session.registerPreloadScript({
      type,
      filePath: path.join(__dirname, "browser_extension_storage_preload.js"),
    });
  }
  const host = new ElectronChromeExtensions({
    license: "MIT",
    requestPermissions: requestExtensionPermissions,
    session,
    modulePath: path.resolve(path.dirname(require.resolve("electron-chrome-extensions")), "../.."),
    createTab: async () => {
      throw new Error("Open the site in the browser pane first.");
    },
  });
  guardExtensionCookies(host, () => [...loadedExtensions.values()]);
  function load() {
    if (!ready) {
      const requestedGeneration = generation;
      const operation = pendingLoad
        .catch(() => {})
        .then(async () => {
          if (requestedGeneration !== generation) throw new Error("Bitwarden was unloaded.");
          loadingGeneration = requestedGeneration;
          const entries = await records();
          const ids = entries.map((entry) => entry.runtimeId).filter(Boolean);
          if (new Set(ids).size !== ids.length) throw new Error("Duplicate extension runtime ID.");
          return (async () => {
            for (const entry of entries) {
              if (requestedGeneration !== generation) throw new Error("Extensions were unloaded.");
              allowedDirectories.set(path.resolve(entry.directory), entry);
              const loaded = await session.extensions.loadExtension(entry.directory, {
                allowFileAccess: false,
              });
              if (entry.runtimeId && loaded.id !== entry.runtimeId) {
                session.extensions.removeExtension(loaded.id);
                throw new Error("Unexpected extension runtime ID.");
              }
              if (requestedGeneration !== generation) {
                session.extensions.removeExtension(loaded.id);
                throw new Error("Extensions were unloaded.");
              }
              loadedExtensions.set(loaded.id, loaded);
              if (entry.agentFillAllowed) extension = loaded;
            }
            loadingGeneration = undefined;
            return extension;
          })();
        });
      pendingLoad = operation;
      ready = operation.catch((error) => {
        if (requestedGeneration === generation) {
          extension = null;
          closeExtensionWindows();
          for (const id of loadedExtensions.keys()) session.extensions.removeExtension(id);
          loadedExtensions.clear();
          allowedDirectories.clear();
          workers.clear();
          ready = null;
        }
        throw error;
      });
    }
    return ready;
  }
  function refreshTabDetails() {
    // 4.10.0 marks cached tabs inactive across windows; rebuild from its per-window store.
    for (const wc of views.keys()) host.api.tabs.createTabDetails(wc);
  }
  function closeExtensionWindows() {
    for (const popup of popups) if (!popup.isDestroyed()) popup.destroy();
    popups.clear();
  }
  function unload() {
    generation++;
    closeExtensionWindows();
    for (const id of loadedExtensions.keys()) session.extensions.removeExtension(id);
    loadedExtensions.clear();
    allowedDirectories.clear();
    extension = null;
    ready = null;
    workers.clear();
    return pendingLoad.catch(() => {});
  }
  return {
    host,
    load,
    loaded: () => [...loadedExtensions.values()],
    reloadViews() {
      for (const contents of views.keys()) if (!contents.isDestroyed?.()) contents.reload?.();
    },
    storageChanged(event, keys, area) {
      if (isExtensionFrame(event)) broadcast(keys, area, frameId(event.senderFrame.url));
    },
    addView(wc, window) {
      if (views.has(wc)) return;
      views.set(wc, window);
      host.addTab(wc, window);
      refreshTabDetails();
      wc.once("destroyed", () => {
        views.delete(wc);
      });
    },
    selectView(wc) {
      if (views.has(wc)) {
        host.selectTab(wc);
        refreshTabDetails();
      }
    },
    async verifyFillIdentity() {
      const requestedGeneration = generation;
      const entries = await records();
      const trusted = entries.filter((entry) => entry.agentFillAllowed);
      extension =
        trusted.length === 1 && requestedGeneration === generation
          ? [...loadedExtensions.values()].find(
              (loaded) =>
                loaded.id === trusted[0].runtimeId &&
                path.resolve(loaded.path) === path.resolve(trusted[0].directory),
            )
          : null;
      return !!extension;
    },
    listFillItems() {
      const menus = host.api.contextMenus.menus.get(extension?.id);
      return [...(menus?.values() ?? [])]
        .filter((item) => /^autofill_[0-9a-f-]{36}$/i.test(item.id))
        .map((item) => ({ id: item.id, label: item.title }));
    },
    emitFill(wc, itemId) {
      if (!extension || !views.has(wc)) throw new Error("Bitwarden is not ready.");
      host.ctx.router.sendEvent(
        extension.id,
        "contextMenus.onClicked",
        { menuItemId: itemId, parentMenuItemId: "autofill", editable: true, pageUrl: wc.getURL() },
        host.api.tabs.getTabDetails(wc),
      );
    },
    async openPopup(window, id) {
      const requestedGeneration = generation;
      await load();
      if (requestedGeneration !== generation) throw new Error("Bitwarden was unloaded.");
      const selected = id
        ? loadedExtensions.get(id)
        : [...loadedExtensions.values()].find((item) => /\bbitwarden\b/i.test(item.name));
      if (!selected) throw new Error("Extension is not ready.");
      const popupOrigin = `chrome-extension://${selected.id}`;
      const popupPath = selected.manifest?.action?.default_popup;
      if (!popupPath) throw new Error("This extension has no popup.");
      let worker, task;
      if (selected.manifest?.background?.service_worker) {
        worker = await session.serviceWorkers.startWorkerForScope(`${popupOrigin}/`);
        task = worker.startTask();
      }
      const releaseWorker = () => {
        if (!task) return;
        const current = task;
        task = null;
        try {
          if (!worker.isDestroyed()) current.end();
        } catch {
          // Closing a popup can race worker teardown.
        }
      };
      let popup;
      try {
        if (requestedGeneration !== generation) throw new Error("Bitwarden was unloaded.");
        if (window.isDestroyed?.()) throw new Error("The browser window closed.");
        closeExtensionWindows();
        popup = new BrowserWindow({
          width: 420,
          height: 660,
          title: selected.name || "Extension",
          autoHideMenuBar: true,
          webPreferences: {
            session,
            sandbox: true,
            contextIsolation: true,
            nodeIntegration: false,
          },
        });
        const closeWithOwner = () => {
          if (!popup.isDestroyed()) popup.destroy();
        };
        window.once?.("closed", closeWithOwner);
        popups.add(popup);
        popup.once("closed", () => {
          window.removeListener?.("closed", closeWithOwner);
          popups.delete(popup);
          releaseWorker();
        });
        popup.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
        popup.webContents.on("will-navigate", (event, url) => {
          if (!url.startsWith(`${popupOrigin}/`)) event.preventDefault();
        });
        await popup.loadURL(`${popupOrigin}/${popupPath}`);
        if (requestedGeneration !== generation || popup.isDestroyed())
          throw new Error("Bitwarden was unloaded.");
        return popup;
      } catch (error) {
        if (popup && !popup.isDestroyed()) popup.destroy();
        releaseWorker();
        throw error;
      }
    },
    async clearData(record) {
      await unload();
      await clearExtensionData({ session, BrowserWindow, record });
    },
    closeExtensionWindows,
    unload,
    relayWebRequest(name, details) {
      if (![...views.keys()].some((wc) => wc.id === details.webContentsId)) return;
      const types = {
        mainFrame: "main_frame",
        subFrame: "sub_frame",
        xhr: "xmlhttprequest",
        fetch: "xmlhttprequest",
        other: "other",
      };
      const type = types[details.resourceType];
      if (!type) return;
      const request = {
        requestId: String(details.id),
        url: details.url,
        method: details.method,
        tabId: details.webContentsId,
        frameId:
          details.frame && details.frame !== details.frame.top ? details.frame.frameTreeNodeId : 0,
        parentFrameId: details.frame?.parent
          ? details.frame.parent === details.frame.top
            ? 0
            : details.frame.parent.frameTreeNodeId
          : -1,
        type,
        timeStamp: details.timestamp,
        statusCode: details.statusCode,
      };
      for (const worker of workers) {
        if (
          !extensionAccess(
            host,
            loadedExtensions.get(frameId(worker.scope)),
            "webRequest",
            request.url,
          )
        )
          continue;
        try {
          worker.send("omnigent:extension-request", name, request);
        } catch {
          workers.delete(worker);
        }
      }
    },
  };
}

module.exports = { createBrowserExtensions };
