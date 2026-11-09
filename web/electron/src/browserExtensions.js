"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");

const BITWARDEN_VERSION = "2026.10.2";
const BITWARDEN_SHA256 = "2ebc61246a2ea82a65c589b8484e26a5678253fa6474c26dc5b6a7a7fcacbda4";
const BITWARDEN_URL = `https://github.com/bitwarden/clients/releases/download/browser-v${BITWARDEN_VERSION}/dist-chrome-${BITWARDEN_VERSION}.zip`;

async function installBitwarden({ directory, fetch, extract }) {
  await fs.mkdir(path.dirname(directory), { recursive: true });
  const scratch = await fs.mkdtemp(path.join(path.dirname(directory), ".download-"));
  try {
    const response = await fetch(BITWARDEN_URL, { signal: AbortSignal.timeout(120000) });
    if (!response.ok) throw new Error("Bitwarden download failed.");
    const bytes = Buffer.from(await response.arrayBuffer());
    if (crypto.createHash("sha256").update(bytes).digest("hex") !== BITWARDEN_SHA256) {
      throw new Error("Bitwarden download failed verification.");
    }
    const archive = path.join(scratch, "extension.zip");
    await fs.writeFile(archive, bytes);
    const unpacked = path.join(scratch, "unpacked");
    await extract(archive, { dir: unpacked });
    const manifest = JSON.parse(await fs.readFile(path.join(unpacked, "manifest.json"), "utf8"));
    if (manifest.version !== BITWARDEN_VERSION || manifest.manifest_version !== 3) {
      throw new Error("Unexpected Bitwarden extension version.");
    }
    await fs.rename(unpacked, directory);
  } finally {
    await fs.rm(scratch, { recursive: true, force: true });
  }
}

function createBrowserExtensions({ session, directory, BrowserWindow, ElectronChromeExtensions }) {
  const workers = new Set();
  const views = new Map();
  const popups = new Set();
  let extension;
  let ready;
  let generation = 0;
  let loadingGeneration;
  let pendingLoad = Promise.resolve();
  const origin = () => extension && `chrome-extension://${extension.id}`;
  const isExtensionFrame = (event) => {
    try {
      return event.sender.session === session && event.senderFrame.url.startsWith(`${origin()}/`);
    } catch {
      return false;
    }
  };
  const broadcast = (keys, area) => {
    if (
      !["local", "session"].includes(area) ||
      !Array.isArray(keys) ||
      keys.some((key) => typeof key !== "string")
    )
      return;
    for (const worker of workers) {
      try {
        worker.send("omnigent:extension-storage", keys, area);
      } catch {
        workers.delete(worker);
      }
    }
  };
  session.extensions.on("extension-loaded", (_event, loaded) => {
    if (loadingGeneration === generation && path.resolve(loaded.path) === path.resolve(directory))
      extension = loaded;
  });
  session.serviceWorkers.on("running-status-changed", (details) => {
    if (details.runningStatus !== "starting") return;
    const worker = session.serviceWorkers.getWorkerFromVersionID(details.versionId);
    if (!worker || !origin() || !worker.scope.startsWith(`${origin()}/`)) return;
    worker.ipc.on("omnigent:extension-storage", (_event, keys, area) => broadcast(keys, area));
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
    session,
    modulePath: path.resolve(path.dirname(require.resolve("electron-chrome-extensions")), "../.."),
    createTab: async () => {
      throw new Error("Open the site in the browser pane first.");
    },
  });
  function load() {
    if (!ready) {
      const requestedGeneration = generation;
      const operation = pendingLoad
        .catch(() => {})
        .then(() => {
          if (requestedGeneration !== generation) throw new Error("Bitwarden was unloaded.");
          loadingGeneration = requestedGeneration;
          return session.extensions.loadExtension(directory, { allowFileAccess: false });
        })
        .then((loaded) => {
          if (requestedGeneration !== generation) {
            session.extensions.removeExtension(loaded.id);
            throw new Error("Bitwarden was unloaded.");
          }
          extension = loaded;
          return loaded;
        });
      pendingLoad = operation;
      ready = operation.catch((error) => {
        if (requestedGeneration === generation) ready = null;
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
    if (extension) session.extensions.removeExtension(extension.id);
    extension = null;
    ready = null;
    workers.clear();
    return pendingLoad.catch(() => {});
  }
  return {
    host,
    load,
    storageChanged(event, keys, area) {
      if (isExtensionFrame(event)) broadcast(keys, area);
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
    async openPopup(window) {
      const requestedGeneration = generation;
      await load();
      if (requestedGeneration !== generation) throw new Error("Bitwarden was unloaded.");
      const worker = await session.serviceWorkers.startWorkerForScope(`${origin()}/`);
      let task = worker.startTask();
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
          title: "Bitwarden",
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
          if (!url.startsWith(`${origin()}/`)) event.preventDefault();
        });
        await popup.loadURL(`${origin()}/popup/index.html`);
        if (requestedGeneration !== generation || popup.isDestroyed())
          throw new Error("Bitwarden was unloaded.");
        return popup;
      } catch (error) {
        if (popup && !popup.isDestroyed()) popup.destroy();
        releaseWorker();
        throw error;
      }
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
        try {
          worker.send("omnigent:extension-request", name, request);
        } catch {
          workers.delete(worker);
        }
      }
    },
    async remove() {
      await unload();
      await fs.rm(directory, { recursive: true, force: true });
    },
  };
}

module.exports = { BITWARDEN_VERSION, BITWARDEN_SHA256, installBitwarden, createBrowserExtensions };
