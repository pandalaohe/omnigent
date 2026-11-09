"use strict";
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");

// A storage-only helper preserves the installed ID without executing the removed package.
async function clearExtensionData({ session, BrowserWindow, record }) {
  if (!/^[a-p]{32}$/.test(record.runtimeId) || typeof record.storageKey !== "string")
    throw new Error("Invalid extension storage identity.");
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "omnigent-extension-clear-"));
  let extension, window;
  try {
    await fs.writeFile(
      path.join(directory, "manifest.json"),
      JSON.stringify({
        manifest_version: 3,
        name: "Extension data cleanup",
        version: "1",
        key: record.storageKey,
        permissions: ["storage"],
      }),
    );
    await fs.writeFile(
      path.join(directory, "clear.html"),
      "<!doctype html><title>Clear extension data</title>",
    );
    extension = await session.extensions.loadExtension(directory, { allowFileAccess: false });
    if (extension.id !== record.runtimeId)
      throw new Error("Extension storage identity did not match.");
    window = new BrowserWindow({
      show: false,
      webPreferences: { session, sandbox: true, contextIsolation: true, nodeIntegration: false },
    });
    await window.loadURL(`chrome-extension://${extension.id}/clear.html`);
    await window.webContents.executeJavaScript(`(async () => {
      for (const area of ['local','sync','session']) if (chrome.storage?.[area]) await chrome.storage[area].clear();
      return true;
    })()`);
    window.destroy();
    window = null;
    session.extensions.removeExtension(extension.id);
    extension = null;
    await session.clearStorageData({ origin: `chrome-extension://${record.runtimeId}` });
  } finally {
    if (window && !window.isDestroyed()) window.destroy();
    if (extension) session.extensions.removeExtension(extension.id);
    await fs.rm(directory, { recursive: true, force: true });
  }
}
module.exports = { clearExtensionData };
