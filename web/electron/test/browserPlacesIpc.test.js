const { test } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { pathToFileURL } = require("node:url");
const path = require("node:path");
const { registerBrowserPlacesIpc } = require("../src/browserPlacesIpc");

test("places IPC gates senders, selects only bundled rows and cleans up on parent changes", async () => {
  const handlers = new Map();
  const instances = [];
  const sent = [];
  class Window extends EventEmitter {
    constructor() {
      super();
      this.dead = false;
      this.webContents = Object.assign(new EventEmitter(), {
        mainFrame: {},
        getZoomFactor: () => 1,
        send: (channel, payload) => sent.push({ channel, payload }),
        setWindowOpenHandler() {},
      });
      instances.push(this);
    }
    static fromWebContents() {
      return parent;
    }
    getContentBounds() {
      return { x: 0, y: 0, width: 800, height: 600 };
    }
    isDestroyed() {
      return this.dead;
    }
    destroy() {
      this.dead = true;
      this.emit("closed");
    }
    showInactive() {}
    async loadFile(file) {
      this.webContents.mainFrame.url = pathToFileURL(file).href;
    }
  }
  const parent = new Window();
  const places = {
    suggestions: () => [{ url: "https://example.test/saved", title: "Saved", bookmarked: true }],
    bookmarks: () => [],
    history: () => [],
    update: () => {
      throw new Error("disk full");
    },
  };
  const ui = registerBrowserPlacesIpc({
    ipcMain: { handle: (key, fn) => handlers.set(key, fn), on: (key, fn) => handlers.set(key, fn) },
    BrowserWindow: Window,
    places,
    isPinnedOriginSender: (event) => event.sender === parent.webContents,
    getRegistry: () => ({ isSuppressed: () => false }),
  });
  const call = (args, event = { sender: parent.webContents }) =>
    handlers.get("omnigent:browser-places")(event, args);
  assert.equal((await call({ action: "list" }, { sender: {} })).ok, false);
  assert.equal((await call({ action: "update", title: "New" })).error, "disk full");
  const request = {
    action: "suggest",
    viewId: "tab",
    bounds: { x: 1, y: 1, width: 500, height: 24 },
  };
  assert.equal((await call(request)).ok, true);
  const popup = instances.at(-1);
  const choose = handlers.get("omnigent:browser-suggestions-choose");
  choose({ sender: parent.webContents, senderFrame: parent.webContents.mainFrame }, 0);
  assert.equal(sent.length, 0, "server renderer cannot forge a bundled popup click");
  await call({ action: "key", key: "ArrowDown" });
  assert.equal(sent.at(-1).payload.index, 0);
  await call({ action: "key", key: "Enter" });
  assert.equal(sent.at(-1).payload.url, "https://example.test/saved");
  assert.equal(popup.dead, true);
  assert.equal(parent.listenerCount("move"), 0);
  await call(request);
  const second = instances.at(-1);
  const senderEvent = { sender: second.webContents, senderFrame: second.webContents.mainFrame };
  assert.equal(handlers.get("omnigent:browser-suggestions-data")(senderEvent).rows.length, 1);
  second.webContents.mainFrame.url = pathToFileURL(path.join(__dirname, "foreign.html")).href;
  choose(senderEvent, 0);
  assert.equal(second.dead, false);
  parent.emit("resize");
  assert.equal(second.dead, true);
  ui.dismiss(parent);
});
