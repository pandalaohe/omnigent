const { it } = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");
const { installCredentialWatch } = require("../src/browserCredentialPreload");
const {
  STATE_JS,
  passwordIsShowing,
  captureCredentialSafePage,
} = require("../src/browserCredentialWatch");
const {
  buildBrowserActionScript,
  buildBrowserActionScriptForExecution,
} = require("../src/browserActionScript.mjs");

it("tracks early reveal and open shadow fields without exposing the observer", async () => {
  const dom = new JSDOM(
    "<input type=password value=FAKE-S1-CREDENTIAL><div id=host></div><input id=ordinary value=ordinary>",
    { runScripts: "outside-only", pretendToBeVisual: true },
  );
  const observers = [];
  const NativeObserver = dom.window.MutationObserver;
  dom.window.MutationObserver = class extends NativeObserver {
    constructor(callback) {
      super(callback);
      observers.push(this);
    }
  };
  try {
    const w = dom.window;
    for (const el of w.document.querySelectorAll("*"))
      el.getBoundingClientRect = () => ({ width: 100, height: 30 });
    w.eval(`(${installCredentialWatch.toString()})()`);
    const password = w.document.querySelector("input");
    password.type = "text";
    const shadow = w.document.querySelector("#host").attachShadow({ mode: "open" });
    shadow.innerHTML = "<input value=FAKE-SHADOW>";
    shadow.querySelector("input").getBoundingClientRect = () => ({ width: 100, height: 30 });
    await new Promise((resolve) => {
      w.queueMicrotask(resolve);
    });
    assert.equal(password.hasAttribute("data-omnigent-credential"), true);
    assert.equal(w.eval(STATE_JS).showing, true);
    const snapshot = w.eval(buildBrowserActionScriptForExecution("snapshot"));
    assert.ok(!snapshot.includes("FAKE-S1-CREDENTIAL"));
    assert.ok(!snapshot.includes("FAKE-SHADOW"));
    assert.ok(snapshot.includes("ordinary"));
    assert.ok(w.eval(buildBrowserActionScript("snapshot")).includes("FAKE-S1-CREDENTIAL"));
    const before = w.eval(STATE_JS).counter;
    password.type = "password";
    password.type = "text";
    password.type = "password";
    await new Promise((resolve) => {
      w.queueMicrotask(resolve);
    });
    assert.ok(Number(w.eval(STATE_JS).counter) > Number(before));
    assert.equal(w.eval("typeof window.__omni_credential_watch__"), "undefined");
  } finally {
    for (const observer of observers) observer.disconnect();
    dom.window.close();
  }
});

it("refuses missing protection and permits masked or empty fields", () => {
  const dom = new JSDOM("<input type=password value=FAKE>", { runScripts: "outside-only" });
  try {
    const w = dom.window;
    const input = w.document.querySelector("input");
    input.getBoundingClientRect = () => ({ width: 100, height: 30 });
    assert.equal(w.eval(STATE_JS), null);
    assert.match(
      w.eval(buildBrowserActionScriptForExecution("snapshot")),
      /protection is unavailable/,
    );
    w.document.documentElement.setAttribute("data-omnigent-credential-watch", "doc");
    w.document.documentElement.setAttribute("data-omnigent-credential-reveals", "0");
    assert.equal(w.eval(STATE_JS).showing, false);
    input.setAttribute("data-omnigent-credential", "");
    input.type = "text";
    assert.equal(w.eval(STATE_JS).showing, true);
    input.value = "";
    assert.equal(w.eval(STATE_JS).showing, false);
  } finally {
    dom.window.close();
  }
});

it("refuses capture after any observed reveal, document or frame-set change", async () => {
  let state = { identity: "doc", counter: "0", showing: false };
  const main = { executeJavaScript: async () => ({ ...state }) };
  main.framesInSubtree = [main];
  const image = { fake: true };
  const wc = { mainFrame: main, capturePage: async () => image };
  assert.equal(await captureCredentialSafePage(wc), image);
  for (const change of [
    () => {
      state.counter = "2";
    },
    () => {
      state.identity = "new-document";
    },
    () => {
      state.showing = true;
    },
    () => {
      main.framesInSubtree = [main, { executeJavaScript: async () => state }];
    },
  ]) {
    state = { identity: "doc", counter: "0", showing: false };
    main.framesInSubtree = [main];
    wc.capturePage = async () => {
      change();
      return image;
    };
    // oxlint-disable-next-line no-await-in-loop -- Each case mutates the same capture boundary serially.
    await assert.rejects(captureCredentialSafePage(wc), /Cannot capture/);
  }
  state = { identity: "doc", counter: "0", showing: false };
  main.framesInSubtree = [main, { executeJavaScript: async () => null }];
  assert.equal(await passwordIsShowing(wc), "unknown");
  await assert.rejects(captureCredentialSafePage(wc), /Cannot capture/);
});

it("discards capture when the first credential is inserted and reveal/remasked in one batch", async () => {
  const dom = new JSDOM("<main></main>", { runScripts: "outside-only", pretendToBeVisual: true });
  const observers = [];
  const NativeObserver = dom.window.MutationObserver;
  dom.window.MutationObserver = class extends NativeObserver {
    constructor(callback) {
      super(callback);
      observers.push(this);
    }
  };
  try {
    const w = dom.window;
    w.eval(`(${installCredentialWatch.toString()})()`);
    const main = { executeJavaScript: async () => w.eval(STATE_JS) };
    main.framesInSubtree = [main];
    const before = w.eval(STATE_JS).counter;
    const wc = {
      mainFrame: main,
      capturePage: async () => {
        const input = w.document.createElement("input");
        input.type = "password";
        input.value = "FAKE-NEW-CREDENTIAL";
        w.document.body.append(input);
        input.type = "text";
        input.type = "password";
        await new Promise((resolve) => {
          w.queueMicrotask(resolve);
        });
        return { fake: true };
      },
    };
    await assert.rejects(captureCredentialSafePage(wc), /Cannot capture/);
    assert.ok(Number(w.eval(STATE_JS).counter) > Number(before));
  } finally {
    for (const observer of observers) observer.disconnect();
    dom.window.close();
  }
});
