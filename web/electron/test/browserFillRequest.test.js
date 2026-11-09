"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createFillRequest, createFillApprovals } = require("../src/browserFillRequest");

function fixture(t, options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "fill-test-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const file = path.join(directory, "approvals.json");
  const approvals = createFillApprovals(file);
  const traces = [];
  let listener;
  const world = vm.createContext({
    document: {
      addEventListener: (_name, fn) => {
        listener = fn;
      },
      removeEventListener: () => {
        listener = null;
      },
    },
  });
  const frame = { origin: "https://example.test" };
  const contents = {
    mainFrame: frame,
    executeJavaScriptInIsolatedWorld: async (_id, scripts) => {
      traces.push(scripts);
      return vm.runInContext(scripts[0].code, world);
    },
  };
  const entry = {
    partition: "persist:fake",
    navGeneration: 0,
    view: { webContents: contents },
  };
  const registry = {
    get: () => entry,
    activeConversationId: () => "tab",
    isSuppressed: () => false,
    isFillOriginAllowed: () => true,
  };
  const item = { id: `autofill_${"a".repeat(36)}`, label: "Demo login" };
  let emitted = 0;
  let dialogs = 0;
  const extensions = {
    listFillItems: () => [item],
    emitFill: () => {
      emitted++;
      if (!options.noEvent)
        listener?.({ composedPath: () => [{ tagName: "INPUT", type: "password" }] });
    },
  };
  const dialog = {
    showMessageBox: async (_window, request) => {
      dialogs++;
      traces.push(request);
      return options.answer
        ? options.answer(entry, request)
        : { response: 1, checkboxChecked: true };
    },
  };
  const controller = createFillRequest({
    extensions,
    registry,
    approvals,
    dialog,
    window: { isVisible: () => true, isDestroyed: () => false },
    fillTimeout: 1,
    ...options,
  });
  return {
    controller,
    approvals,
    registry,
    entry,
    extensions,
    file,
    traces,
    item,
    emitted: () => emitted,
    dialogs: () => dialogs,
  };
}

test("first approval persists, reuse skips dialog; revocation asks again; metadata partitions stay isolated", async (t) => {
  const f = fixture(t);
  assert.deepEqual(await f.controller.fill("tab"), { ok: true, filled: true });
  assert.equal(f.dialogs(), 1);
  await f.controller.fill("tab");
  assert.equal(f.dialogs(), 1);
  assert.equal(createFillApprovals(f.file).list("persist:fake").length, 1);
  assert.equal(f.approvals.list("other-partition").length, 0);
  f.approvals.revoke(f.approvals.list("persist:fake")[0]);
  await f.controller.fill("tab");
  assert.equal(f.dialogs(), 2);
});

for (const failure of [
  "denied",
  "navigation",
  "hidden",
  "no login",
  "insecure",
  "revoked",
  "removed item",
  "timeout",
  "exception",
]) {
  test(`fails closed: ${failure}`, async (t) => {
    let f;
    f = fixture(t, {
      dialogTimeout: failure === "timeout" ? 1 : 40000,
      answer: async (entry) => {
        if (failure === "navigation") entry.navGeneration += 2;
        if (failure === "revoked") f.approvals.revoke({});
        if (failure === "removed item") f.extensions.listFillItems = () => [];
        if (failure === "timeout")
          await new Promise((resolve) => {
            setTimeout(resolve, 5);
          });
        if (failure === "exception") throw Error("private exception must not escape");
        return { response: failure === "denied" ? 0 : 1, checkboxChecked: true };
      },
    });
    if (failure === "hidden") f.registry.isSuppressed = () => true;
    if (failure === "no login") f.extensions.listFillItems = () => [];
    if (failure === "insecure") f.registry.isFillOriginAllowed = () => false;
    const result = await f.controller.fill("tab");
    assert.equal(result.ok, false);
    assert.equal(f.emitted(), 0);
    assert.equal(f.approvals.list("persist:fake").length, 0);
    assert.equal(JSON.stringify(result).includes("private exception"), false);
  });
}

test("a preexisting field value is insufficient without a new password input event", async (t) => {
  const f = fixture(t, { noEvent: true });
  assert.equal((await f.controller.fill("tab")).ok, false);
  assert.equal(f.emitted(), 1);
});

test("fake credential stays outside request scripts, dialogs, results and approval metadata; leak assertion has a negative control", async (t) => {
  const f = fixture(t);
  const fakeCredential = require("node:crypto").randomBytes(24).toString("hex");
  // Only the fake extension/page boundary owns the value; no application callback receives it.
  let pageValue = "";
  const emit = f.extensions.emitFill;
  f.extensions.emitFill = () => {
    pageValue = fakeCredential;
    emit();
  };
  const result = await f.controller.fill("tab");
  assert.equal(pageValue === fakeCredential, true);
  const boundary = JSON.stringify({
    result,
    requests: f.traces,
    disk: fs.readFileSync(f.file, "utf8"),
  });
  const noLeak = (text) =>
    assert.equal(text.includes(fakeCredential), false, "credential escaped boundary");
  noLeak(boundary);
  assert.throws(() => noLeak(boundary + fakeCredential), /credential escaped boundary/);
});

test("fill policy uses the window's configured internal sites after user navigation", async () => {
  const { createBrowserViewRegistry } = require("../src/browserViewRegistry");
  const { EventEmitter } = require("node:events");
  const wc = Object.assign(new EventEmitter(), {
    loadURL: async () => {},
    setWindowOpenHandler() {},
  });
  const registry = createBrowserViewRegistry({
    WebContentsViewCtor: () => ({ webContents: wc }),
    createBoundsController: () => ({}),
    attachToHost() {},
    detachFromHost() {},
    sendToRenderer() {},
    getAgentAllowlist: (context) => (context?.serverOrigin ? ["internal.example.test:8080"] : []),
    getFillAllowlist: () => ["internal.example.test:8080"],
  });
  await registry.openOrNavigate("tab", "http://internal.example.test:8080");
  assert.equal(registry.isFillOriginAllowed("tab", "http://internal.example.test:8080"), true);
  assert.equal(registry.isFillOriginAllowed("tab", "http://internal.example.test:9090"), false);
  assert.equal(registry.isFillOriginAllowed("tab", "http://other.example.test"), false);
  const entry = registry.get("tab");
  const before = entry.navGeneration;
  wc.emit("did-start-navigation", {}, "http://internal.example.test:8080/#a", true, true);
  wc.emit("did-navigate-in-page", {}, "http://internal.example.test:8080/#a", true);
  assert.ok(entry.navGeneration > before);
});

test("several accounts require a native choice before approval", async (t) => {
  const f = fixture(t);
  const chosen = { id: `autofill_${"b".repeat(36)}`, label: "Second demo login" };
  f.extensions.listFillItems = () => [f.item, chosen];
  let calls = 0;
  const prompts = [];
  let emitted;
  const originalEmit = f.extensions.emitFill;
  f.extensions.emitFill = (wc, id) => {
    emitted = id;
    originalEmit();
  };
  const controller = createFillRequest({
    extensions: f.extensions,
    registry: f.registry,
    approvals: f.approvals,
    window: { isVisible: () => true, isDestroyed: () => false },
    dialog: {
      showMessageBox: async (_win, options) => {
        prompts.push(options);
        return { response: ++calls === 1 ? 2 : 1, checkboxChecked: false };
      },
    },
  });
  assert.equal((await controller.fill("tab")).ok, true);
  assert.equal(emitted, chosen.id);
  assert.deepEqual(prompts[0].buttons, ["Cancel", "Demo login", "Second demo login"]);
  assert.match(prompts[1].detail, /Second demo login/);
  assert.equal(f.approvals.list("persist:fake").length, 0);
});

test("rejected concurrent requests cannot release the active fill's guard", async (t) => {
  let answer;
  const f = fixture(t, {
    answer: () =>
      new Promise((resolve) => {
        answer = resolve;
      }),
  });
  const active = f.controller.fill("tab");
  assert.equal((await f.controller.fill("tab")).ok, false);
  assert.equal((await f.controller.fill("tab")).ok, false);
  assert.equal(f.dialogs(), 1);
  answer({ response: 1 });
  assert.equal((await active).ok, true);
  assert.equal(f.emitted(), 1);
});
