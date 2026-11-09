// oxlint-disable no-underscore-dangle -- Page-world watcher cleanup before JSDOM teardown.
const { it } = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");
const { EventEmitter } = require("node:events");
const {
  WATCH_JS,
  SHOWING_JS,
  passwordIsShowing,
  captureCredentialSafePage,
  attachCredentialWatch,
} = require("../src/browserCredentialWatch");
const {
  buildBrowserActionScript,
  buildBrowserActionScriptForExecution,
} = require("../src/browserActionScript.mjs");

it("redacts credentials including revealed passwords; the old snapshot is a positive leak control", async () => {
  const dom = new JSDOM(
    '<input type="password"><input autocomplete="one-time-code"><input autocomplete="cc-number"><textarea name="password"></textarea><input name="ordinary">',
    { runScripts: "outside-only" },
  );
  try {
    const w = dom.window;
    for (const el of w.document.querySelectorAll("*"))
      el.getBoundingClientRect = () => ({ width: 100, height: 30 });
    for (const el of w.document.querySelectorAll("input,textarea")) el.value = "FAKE-S1-CREDENTIAL";
    w.document.querySelector("[name=ordinary]").value = "ordinary text";
    w.eval(WATCH_JS);
    const password = w.document.querySelector("input");
    password.type = "text";
    assert.equal(w.eval(SHOWING_JS), "yes");
    const snapshot = w.eval(buildBrowserActionScriptForExecution("snapshot"));
    assert.ok(!snapshot.includes("FAKE-S1-CREDENTIAL"));
    assert.ok(snapshot.includes("ordinary text"));
    assert.ok(w.eval(buildBrowserActionScript("snapshot")).includes("FAKE-S1-CREDENTIAL"));
    const dynamic = w.document.createElement("input");
    w.document.body.append(dynamic);
    dynamic.type = "password";
    dynamic.type = "text";
    dynamic.value = "FAKE-DYNAMIC";
    await new Promise((resolve) => {
      w.queueMicrotask(resolve);
    });
    assert.equal(dynamic.hasAttribute("data-omnigent-credential"), true);
    assert.ok(!w.eval(buildBrowserActionScriptForExecution("snapshot")).includes("FAKE-DYNAMIC"));
    w.eval(WATCH_JS); // installer is idempotent
  } finally {
    dom.window.__omni_credential_watch__?.disconnect();
    dom.window.close();
  }
});

it("allows masked/empty fields and refuses an absent watcher", () => {
  const dom = new JSDOM('<input type="password" value="FAKE">', { runScripts: "outside-only" });
  try {
    const w = dom.window;
    const input = w.document.querySelector("input");
    input.getBoundingClientRect = () => ({ width: 100, height: 30 });
    assert.equal(w.eval(SHOWING_JS), "unknown");
    w.eval(WATCH_JS);
    assert.equal(w.eval(SHOWING_JS), "no");
    input.type = "text";
    assert.equal(w.eval(SHOWING_JS), "yes");
    input.value = "";
    assert.equal(w.eval(SHOWING_JS), "no");
  } finally {
    dom.window.__omni_credential_watch__?.disconnect();
    dom.window.close();
  }
});

it("checks every frame and discards an image when reveal occurs during capture", async () => {
  const main = { executeJavaScript: async () => "no" };
  const sub = { executeJavaScript: async () => "yes" };
  main.framesInSubtree = [main, sub];
  let captures = 0;
  const wc = {
    mainFrame: main,
    capturePage: async () => {
      captures++;
      return "FAKE-PASSWORD-PIXELS";
    },
  };
  assert.equal(await passwordIsShowing(wc), "yes");
  await assert.rejects(captureCredentialSafePage(wc), /Cannot capture/);
  assert.equal(captures, 0);
  sub.executeJavaScript = async () => {
    throw new Error("navigated");
  };
  assert.equal(await passwordIsShowing(wc), "no");
  wc.capturePage = async () => {
    captures++;
    main.executeJavaScript = async () => "yes";
    return "FAKE-PASSWORD-PIXELS";
  };
  await assert.rejects(captureCredentialSafePage(wc), /Cannot capture/);
  assert.equal(captures, 1);
  main.executeJavaScript = async () => {
    throw new Error("gone");
  };
  assert.equal(await passwordIsShowing(wc), "unknown");
  await assert.rejects(captureCredentialSafePage(wc), /Cannot capture/);
});

it("installs the watcher at both frame lifecycle events", async () => {
  const wc = new EventEmitter();
  const scripts = [];
  const frame = { executeJavaScript: async (script) => scripts.push(script) };
  wc.mainFrame = { framesInSubtree: [frame] };
  attachCredentialWatch({ view: { webContents: wc } });
  wc.emit("dom-ready");
  wc.emit("did-frame-finish-load");
  assert.deepEqual(scripts, [WATCH_JS, WATCH_JS]);
});
