const { test } = require("node:test");
const assert = require("node:assert/strict");
const { registerBrowserSettingsIpc } = require("../src/browserSettingsIpc");
const { createBrowserPermissionStore } = require("../src/browserPermissions");

function setup(confirm = async () => true) {
  const handlers = new Map();
  let settings = { other: "kept" },
    trusted = true;
  const win = { isDestroyed: () => false };
  let current = win;
  const loadSettings = () => structuredClone(settings),
    saveSettings = (value) => {
      settings = value;
    };
  registerBrowserSettingsIpc({
    ipcMain: { handle: (key, fn) => handlers.set(key, fn) },
    BrowserWindow: { fromWebContents: () => current },
    isTrusted: () => trusted,
    loadSettings,
    browserPermissionStore: createBrowserPermissionStore({ loadSettings, saveSettings }),
    confirmLocalNetwork: confirm,
  });
  return {
    call: (action, payload) =>
      handlers.get(`omnigent:browser-local-network:${action}`)({ sender: {} }, payload),
    settings: () => settings,
    untrust: () => {
      trusted = false;
    },
    replaceWindow: () => {
      current = { isDestroyed: () => false };
    },
  };
}
test("local choices validate, preserve unrelated keys and translate null Forget", async () => {
  const env = setup();
  await Promise.all(
    [
      null,
      [],
      { origin: "https://example.test/path", allowed: true },
      { origin: "http://example.test", allowed: false },
      { origin: "https://example.test", allowed: 1 },
    ].map(async (payload) =>
      assert.equal((await env.call("set", payload)).error, "Invalid request."),
    ),
  );
  assert.equal(
    (await env.call("set", { origin: "https://example.test", allowed: false })).ok,
    true,
  );
  assert.deepEqual((await env.call("list")).sites, [
    { origin: "https://example.test", allowed: false },
  ]);
  await env.call("set", { origin: "https://example.test", allowed: null });
  assert.equal((await env.call("list")).sites.length, 0);
  assert.equal(env.settings().other, "kept");
});
test("Allow is native-confirmed and cancellation/errors fail closed", async () => {
  await Promise.all(
    [
      async () => false,
      async () => {
        throw Error("/opt/private/settings.json");
      },
    ].map(async (confirm) => {
      const env = setup(confirm);
      assert.equal(
        (await env.call("set", { origin: "https://example.test", allowed: true })).cancelled,
        true,
      );
      assert.equal((await env.call("list")).sites.length, 0);
    }),
  );
  const env = setup();
  assert.equal((await env.call("set", { origin: "https://example.test", allowed: true })).ok, true);
  assert.equal((await env.call("list")).sites[0].allowed, true);
});
test("stale Allow cannot overwrite Block/Forget, or commit after sender navigation/window change", async () => {
  await Promise.all(
    ["block", "forget", "navigation", "window"].map(async (change) => {
      let resolve;
      const env = setup(
        () =>
          new Promise((done) => {
            resolve = done;
          }),
      );
      const pending = env.call("set", { origin: "https://example.test", allowed: true });
      assert.equal(
        (await env.call("set", { origin: "https://other.example", allowed: true })).ok,
        false,
      );
      if (change === "navigation") env.untrust();
      else if (change === "window") env.replaceWindow();
      else
        await env.call("set", {
          origin: "https://example.test",
          allowed: change === "block" ? false : null,
        });
      resolve(true);
      assert.equal((await pending).ok, false);
      assert.notEqual(
        env.settings().browser_local_network_permissions?.["https://example.test"],
        true,
      );
    }),
  );
});
