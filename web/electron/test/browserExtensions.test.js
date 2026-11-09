"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs/promises");
const path = require("node:path");
const vm = require("node:vm");
const { createBrowserExtensions } = require("../src/browserExtensions");

function fixture(records) {
  const extension = {
    id: "a".repeat(32),
    path: "/opt/work/bitwarden",
    name: "Bitwarden",
    manifest: {
      permissions: ["webRequest"],
      host_permissions: ["<all_urls>"],
      action: { default_popup: "popup/index.html" },
      background: { service_worker: "background.js" },
    },
  };
  const session = {
    extensions: new EventEmitter(),
    serviceWorkers: new EventEmitter(),
    registerPreloadScript() {},
  };
  const worker = {
    scope: `chrome-extension://${extension.id}/`,
    ipc: new EventEmitter(),
    sent: [],
    tasks: 0,
    isDestroyed: () => false,
    startTask() {
      this.tasks++;
      return {
        end: () => {
          this.tasks--;
        },
      };
    },
    send(...args) {
      this.sent.push(args);
    },
  };
  session.serviceWorkers.getWorkerFromVersionID = () => worker;
  session.serviceWorkers.startWorkerForScope = async () => worker;
  session.extensions.loadExtension = async () => {
    session.extensions.emit("extension-loaded", {}, extension);
    return extension;
  };
  let removed;
  session.extensions.removeExtension = (id) => {
    removed = id;
  };
  class Library {
    constructor() {
      this.tabs = new Set();
      const parents = new Map();
      const active = new Map();
      const cache = new Map();
      this.api = {
        contextMenus: { menus: new Map() },
        tabs: {
          getTabDetails: (wc) => cache.get(wc.id),
          createTabDetails: (wc) => {
            const details = {
              id: wc.id,
              windowId: parents.get(wc).id,
              active: active.get(parents.get(wc)) === wc,
            };
            cache.set(wc.id, details);
            return details;
          },
        },
      };
      this.ctx = {
        store: { parents, active, cache },
        router: {
          sendEvent: (...args) => {
            this.fill = args;
          },
        },
      };
    }
    addTab(wc, window) {
      this.tabs.add(wc);
      wc.once("destroyed", () => this.tabs.delete(wc));
      this.ctx.store.parents.set(wc, window);
      this.selectTab(wc);
    }
    removeTab(wc) {
      if (wc.isDestroyed?.()) throw new TypeError("Object has been destroyed");
      this.tabs.delete(wc);
    }
    selectTab(wc) {
      this.selected = wc;
      const { active, cache, parents } = this.ctx.store;
      if (active.get(parents.get(wc)) === wc) return;
      active.set(parents.get(wc), wc);
      for (const [id, details] of cache) details.active = id === wc.id;
    }
  }
  const windows = [];
  class Window extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      windows.push(this);
      this.webContents = new EventEmitter();
      this.webContents.setWindowOpenHandler = () => {};
    }
    async loadURL(url) {
      this.url = url;
    }
    isDestroyed() {
      return !!this.destroyed;
    }
    destroy() {
      this.destroyed = true;
      this.emit("closed");
    }
  }
  const host = createBrowserExtensions({
    session,
    records:
      records ||
      (async () => [
        { directory: extension.path, agentFillAllowed: true, runtimeId: extension.id },
      ]),
    BrowserWindow: Window,
    ElectronChromeExtensions: Library,
  });
  return { host, session, extension, worker, windows, removed: () => removed };
}

test("storage notifications stay in their extension session and carry only keys", async () => {
  const { host, session, worker, extension } = fixture();
  await host.load();
  session.serviceWorkers.emit("running-status-changed", {
    runningStatus: "starting",
    versionId: 1,
  });
  worker.ipc.emit("omnigent:extension-storage-subscribe");
  const event = {
    sender: { session },
    senderFrame: { url: `chrome-extension://${extension.id}/popup/index.html` },
  };
  host.storageChanged({ ...event, sender: { session: {} } }, ["cipher"], "local");
  host.storageChanged(
    { ...event, senderFrame: { url: "https://example.test/" } },
    ["cipher"],
    "local",
  );
  host.storageChanged(event, { cipher: { newValue: "fake" } }, "local");
  assert.deepEqual(worker.sent, []);
  host.storageChanged(event, ["cipher"], "local");
  assert.deepEqual(worker.sent, [["omnigent:extension-storage", ["cipher"], "local"]]);
});

test("popup stays outside tabs and unload closes it before clearing login", async () => {
  const { host, session, worker, extension, removed } = fixture();
  const wc = Object.assign(new EventEmitter(), { id: 7, getURL: () => "https://example.test/" });
  host.addView(wc, {});
  host.selectView(wc);
  const popup = await host.openPopup({});
  assert.equal(host.host.tabs.size, 1);
  assert.equal(host.host.selected, wc);
  assert.equal(popup.url, `chrome-extension://${extension.id}/popup/index.html`);
  session.serviceWorkers.emit("running-status-changed", {
    runningStatus: "starting",
    versionId: 1,
  });
  worker.ipc.emit("omnigent:extension-storage-subscribe");
  host.relayWebRequest("onBeforeRequest", {
    id: 1,
    webContentsId: 7,
    resourceType: "mainFrame",
    url: wc.getURL(),
    method: "POST",
    uploadData: [{ bytes: Buffer.from("fake") }],
    frame: (() => {
      const main = { routingId: 31, frameTreeNodeId: 42 };
      main.top = main;
      return main;
    })(),
  });
  assert.equal(JSON.stringify(worker.sent).includes("uploadData"), false);
  assert.equal(worker.sent[0][2].tabId, 7);
  assert.equal(worker.sent[0][2].frameId, 0);
  assert.equal(worker.sent[0][2].parentFrameId, -1);
  const top = {};
  top.top = top;
  host.relayWebRequest("onCompleted", {
    id: 2,
    webContentsId: 7,
    resourceType: "subFrame",
    frame: { top, parent: top, routingId: 32, frameTreeNodeId: 43 },
  });
  assert.equal(worker.sent[1][2].frameId, 43);
  assert.equal(worker.sent[1][2].parentFrameId, 0);
  await host.unload();
  assert.equal(popup.isDestroyed(), true);
  assert.equal(removed(), extension.id);
  wc.emit("destroyed");
  assert.equal(host.host.tabs.size, 0);
});

test("selecting one window preserves each other window's active cached tab", () => {
  const { host } = fixture();
  const a = { id: 1 },
    b = { id: 2 };
  const tabs = [11, 12, 21].map((id) => Object.assign(new EventEmitter(), { id }));
  host.addView(tabs[0], a);
  host.addView(tabs[1], a);
  host.addView(tabs[2], b);
  host.selectView(tabs[0]);
  host.selectView(tabs[2]);
  assert.deepEqual(
    tabs.map((wc) => host.host.api.tabs.getTabDetails(wc).active),
    [true, false, true],
  );
});

test("closing a tab lets the library clean it up without reusing destroyed web contents", () => {
  const { host } = fixture();
  let destroyed = false;
  const wc = Object.assign(new EventEmitter(), { id: 13, isDestroyed: () => destroyed });
  host.addView(wc, { id: 1 });
  destroyed = true;
  assert.doesNotThrow(() => wc.emit("destroyed"));
  assert.equal(host.host.tabs.size, 0);
});

test("the popup keeps its worker alive only while it is open", async () => {
  const { host, worker } = fixture();
  const popup = await host.openPopup({});
  assert.equal(worker.tasks, 1, "the popup's port must survive worker idle shutdown");
  popup.destroy();
  assert.equal(worker.tasks, 0, "closing the popup must release its worker task");
  const next = await host.openPopup({});
  assert.equal(worker.tasks, 1);
  await host.unload();
  assert.equal(next.isDestroyed(), true);
  assert.equal(worker.tasks, 0);
});

test("the popup minimizes independently and closes with its owner", async () => {
  const { host, worker } = fixture();
  const owner = new EventEmitter();
  const popup = await host.openPopup(owner);
  assert.equal(popup.options.parent, undefined, "native child windows share macOS minimization");
  owner.emit("closed");
  assert.equal(popup.isDestroyed(), true);
  assert.equal(worker.tasks, 0);
});

test("an owner closing while its worker starts cannot leave a popup or task behind", async () => {
  const { host, session, worker, windows } = fixture();
  const owner = new EventEmitter();
  let destroyed = false;
  owner.isDestroyed = () => destroyed;
  let finish;
  let started;
  const starting = new Promise((resolve) => {
    started = resolve;
  });
  session.serviceWorkers.startWorkerForScope = () => {
    started();
    return new Promise((resolve) => {
      finish = resolve;
    });
  };
  const rejected = assert.rejects(host.openPopup(owner), /closed/);
  await starting;
  destroyed = true;
  owner.emit("closed");
  finish(worker);
  await rejected;
  assert.equal(windows.length, 0);
  assert.equal(worker.tasks, 0);
});

test("unloading during extension load rejects a waiting popup and removes the late load", async () => {
  const { host, session, extension, windows, removed } = fixture();
  let finish;
  let started;
  const loading = new Promise((resolve) => {
    started = resolve;
  });
  const loadExtension = session.extensions.loadExtension;
  session.extensions.loadExtension = () => {
    started();
    return new Promise((resolve) => {
      finish = resolve;
    });
  };
  const popup = host.openPopup({});
  const rejected = assert.rejects(popup, /unloaded/);
  await loading;
  let unloaded = false;
  const unloading = host.unload().then(() => {
    unloaded = true;
  });
  await Promise.resolve();
  assert.equal(unloaded, false);
  session.extensions.emit("extension-loaded", {}, extension);
  finish(extension);
  await rejected;
  await unloading;
  assert.equal(windows.length, 0);
  assert.equal(removed(), extension.id);
  session.extensions.loadExtension = loadExtension;
  await host.load();
});

test("request filters use Chrome hostname and port semantics before notifying the worker", async () => {
  const ipc = new EventEmitter();
  ipc.send = () => {};
  const world = vm.createContext({
    URL,
    chrome: { runtime: { id: "a".repeat(32) }, storage: {}, webRequest: {} },
  });
  world.require = () => ({
    ipcRenderer: ipc,
    contextBridge: {
      executeInMainWorld: ({ func }) => func(),
      exposeInMainWorld: (name, bridge) => {
        world[name] = bridge;
      },
    },
  });
  vm.runInContext(
    await fs.readFile(path.join(__dirname, "../src/browser_extension_storage_preload.js"), "utf8"),
    world,
  );
  const seen = [];
  world.chrome.webRequest.onBeforeRequest.addListener((details) => seen.push(details.url), {
    urls: ["*://*.example.test/*"],
    types: ["main_frame"],
  });
  for (const url of [
    "https://example.test:18444/after",
    "https://signin.example.test:18444/after",
    "https://badexample.test/after",
    "https://example.test.evil/after",
  ]) {
    ipc.emit("omnigent:extension-request", {}, "onBeforeRequest", { url, type: "main_frame" });
  }
  assert.deepEqual(seen, [
    "https://example.test:18444/after",
    "https://signin.example.test:18444/after",
  ]);
});

test("two extension workers receive only their own storage keys; unapproved menus cannot fill", async () => {
  const entries = [
    { directory: "/opt/work/extension-a", runtimeId: "a".repeat(32), agentFillAllowed: true },
    { directory: "/opt/work/extension-b", runtimeId: "b".repeat(32), agentFillAllowed: false },
  ];
  const { host, session } = fixture(async () => entries);
  session.extensions.loadExtension = async (directory) => {
    const entry = entries.find((item) => item.directory === directory);
    const loaded = { id: entry.runtimeId, path: directory, name: "Synthetic" };
    session.extensions.emit("extension-loaded", {}, loaded);
    return loaded;
  };
  await host.load();
  assert.equal(host.loaded().length, 2);
  const workers = entries.map((entry) => ({
    scope: `chrome-extension://${entry.runtimeId}/`,
    ipc: new EventEmitter(),
    sent: [],
    send(...args) {
      this.sent.push(args);
    },
  }));
  session.serviceWorkers.getWorkerFromVersionID = (id) => workers[id];
  workers.forEach((worker, id) => {
    session.serviceWorkers.emit("running-status-changed", {
      runningStatus: "starting",
      versionId: id,
    });
    worker.ipc.emit("omnigent:extension-storage-subscribe");
  });
  host.storageChanged(
    {
      sender: { session },
      senderFrame: { url: `chrome-extension://${entries[0].runtimeId}/popup.html` },
    },
    ["key-a"],
    "local",
  );
  assert.equal(workers[0].sent.length, 1);
  assert.equal(workers[1].sent.length, 0);
  workers[1].ipc.emit("omnigent:extension-storage", {}, ["key-b"], "session");
  assert.equal(workers[0].sent.length, 1);
  assert.equal(workers[1].sent.length, 1);
  const id = "autofill_aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
  for (const entry of entries)
    host.host.api.contextMenus.menus.set(
      entry.runtimeId,
      new Map([[id, { id, title: "Synthetic login" }]]),
    );
  assert.equal(host.listFillItems().length, 1);
  entries[0].agentFillAllowed = false;
  assert.equal(await host.verifyFillIdentity(), false);
  assert.deepEqual(host.listFillItems(), []);
  await host.unload();
  assert.equal(host.loaded().length, 0);
});

test("legitimate reload recovers fill only after current content consent is revalidated", async () => {
  const entries = [
    { directory: "/opt/work/bitwarden", runtimeId: "a".repeat(32), agentFillAllowed: true },
  ];
  const { host, session, extension } = fixture(async () => entries);
  await host.load();
  session.extensions.emit("extension-loaded", {}, extension);
  assert.equal(await host.verifyFillIdentity(), true);
  entries[0].agentFillAllowed = false;
  assert.equal(await host.verifyFillIdentity(), false);
  assert.deepEqual(host.listFillItems(), []);
});
