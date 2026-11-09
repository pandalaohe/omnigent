"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const vm = require("node:vm");
const { clearExtensionData } = require("../src/browserExtensionData");
const { runtimeId, storageKey } = require("../src/browserExtensionPackages");

test("removal loads only an app-owned storage helper, clearing all areas without reading original package", async () => {
  const original = "/opt/work/removed-extension";
  const record = { runtimeId: runtimeId(original, {}), storageKey: storageKey(original, {}) };
  const cleared = [],
    removed = [];
  let directory;
  const session = {
    extensions: {
      loadExtension: async (location) => {
        directory = location;
        assert.notEqual(location, original);
        const manifest = JSON.parse(
          await fs.readFile(path.join(location, "manifest.json"), "utf8"),
        );
        assert.deepEqual(manifest.permissions, ["storage"]);
        assert.equal(manifest.background, undefined);
        return { id: runtimeId(location, manifest) };
      },
      removeExtension: (id) => removed.push(id),
    },
    clearStorageData: async (options) => cleared.push(options.origin),
  };
  class Window {
    constructor() {
      this.webContents = {
        executeJavaScript: async (code) =>
          vm.runInNewContext(code, {
            chrome: {
              storage: Object.fromEntries(
                ["local", "sync", "session"].map((area) => [
                  area,
                  { clear: async () => cleared.push(area) },
                ]),
              ),
            },
          }),
      };
    }
    async loadURL(url) {
      assert.equal(url, `chrome-extension://${record.runtimeId}/clear.html`);
    }
    destroy() {
      this.destroyed = true;
    }
    isDestroyed() {
      return !!this.destroyed;
    }
  }
  await clearExtensionData({ session, BrowserWindow: Window, record });
  assert.deepEqual(cleared, ["local", "sync", "session", `chrome-extension://${record.runtimeId}`]);
  assert.deepEqual(removed, [record.runtimeId]);
  await assert.rejects(fs.access(directory));
});
