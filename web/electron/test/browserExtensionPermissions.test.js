"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { guardExtensionCookies, extensionAccess } = require("../src/browserExtensionPermissions");

test("cookies and cookie events require the caller's permission and host, with a permitted control", async () => {
  const extension = {
    id: "a".repeat(32),
    manifest: { permissions: ["storage"], host_permissions: ["https://allowed.test/*"] },
  };
  const sent = [],
    handlers = new Map();
  let reads = 0;
  handlers.set("cookies.getAll", {
    callback: async () => {
      reads++;
      return [
        { domain: "allowed.test", secure: true, path: "/" },
        { domain: "other.test", secure: true, path: "/" },
      ];
    },
  });
  const host = { api: {}, ctx: { router: { handlers, sendEvent: (...args) => sent.push(args) } } };
  guardExtensionCookies(host, () => [extension]);
  const event = {
    extension,
    sender: { getURL: () => `chrome-extension://${extension.id}/popup.html` },
  };
  await assert.rejects(handlers.get("cookies.getAll").callback(event, {}), /not granted/);
  assert.equal(reads, 0);
  extension.manifest.permissions.push("cookies");
  await assert.rejects(
    handlers.get("cookies.getAll").callback(event, { url: "https://other.test/" }),
    /not granted/,
  );
  const allowed = await handlers.get("cookies.getAll").callback(event, {});
  assert.deepEqual(
    allowed.map((cookie) => cookie.domain),
    ["allowed.test"],
  );
  await assert.rejects(
    handlers
      .get("cookies.getAll")
      .callback({ ...event, sender: { getURL: () => "https://allowed.test/" } }, {}),
    /not granted/,
  );
  host.ctx.router.sendEvent(undefined, "cookies.onChanged", {
    cookie: { domain: "other.test", secure: true, path: "/" },
  });
  assert.equal(sent.length, 0);
  host.ctx.router.sendEvent(undefined, "cookies.onChanged", {
    cookie: { domain: "allowed.test", secure: true, path: "/" },
  });
  assert.equal(sent.length, 1);
});
test("request relay permission is enforced in main for each extension and URL", () => {
  const host = { api: {} };
  const extension = {
    manifest: { permissions: ["webRequest"], host_permissions: ["https://*.example.test/*"] },
  };
  assert.equal(extensionAccess(host, extension, "webRequest", "https://example.test:8443/"), true);
  assert.equal(extensionAccess(host, extension, "webRequest", "https://other.test/"), false);
  extension.manifest.permissions = [];
  assert.equal(extensionAccess(host, extension, "webRequest", "https://example.test/"), false);
});

test("single-cookie operations reject an omitted URL before querying the underlying store", async () => {
  let reads = 0;
  const handlers = new Map(
    ["get", "set", "remove"].map((name) => [
      `cookies.${name}`,
      {
        callback: async () => {
          reads++;
          return { domain: "other.test" };
        },
      },
    ]),
  );
  const extension = {
    id: "a".repeat(32),
    manifest: { permissions: ["cookies"], host_permissions: [] },
  };
  const host = { api: {}, ctx: { router: { handlers, sendEvent() {} } } };
  guardExtensionCookies(host, () => [extension]);
  const event = {
    extension,
    sender: { getURL: () => `chrome-extension://${extension.id}/popup.html` },
  };
  await Promise.all(
    ["get", "set", "remove"].map((name) =>
      assert.rejects(
        handlers.get(`cookies.${name}`).callback(event, { name: "session" }),
        /URL is required/,
      ),
    ),
  );
  assert.equal(reads, 0);
});
