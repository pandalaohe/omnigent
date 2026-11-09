/* oxlint-disable no-await-in-loop -- Serial saves and injected DNS matrices keep each case isolated. */
const { it } = require("node:test");
const assert = require("node:assert/strict");
const { checkAgentNavigation, isAgentConnectionAllowed } = require("../src/browserUrlPolicy");

const resolveHost = async () => ["198.18.0.10"];
it("accepts fake-IP names, but keeps literal benchmark addresses internal", async () => {
  assert.equal((await checkAgentNavigation("https://example.com", { resolveHost })).ok, true);
  assert.equal(isAgentConnectionAllowed("https://example.com", "198.18.0.10").ok, true);
  assert.equal((await checkAgentNavigation("http://198.18.0.10")).ok, false);
});
it("applies levels and ordered lists to names and resolved addresses", async () => {
  const url = "https://app.example.com";
  for (const level of ["none", "agent", "all"]) {
    for (const userNavigation of [true, false]) {
      const options = { access: { level }, userNavigation, resolveHost: async () => ["127.0.0.2"] };
      assert.equal(
        (await checkAgentNavigation(url, options)).ok,
        level === "none" || (level === "agent" && userNavigation),
      );
      assert.equal(
        (
          await checkAgentNavigation(url, {
            ...options,
            access: { level, allowed: ["127.0.0.0/8"] },
          })
        ).ok,
        true,
      );
      const ask = await checkAgentNavigation(url, {
        ...options,
        access: { level, allowed: ["*.example.com"], ask: ["app.example.com"] },
      });
      assert.equal(ask.ok, false);
      assert.ok(ask.grant);
      const blocked = await checkAgentNavigation(url, {
        ...options,
        access: {
          level,
          blocked: ["127.0.0.0/8"],
          ask: ["app.example.com"],
          allowed: ["*.example.com"],
        },
      });
      assert.equal(blocked.ok, false);
      assert.equal(blocked.grant, undefined);
    }
  }
});
it("never grants metadata or unspecified/link-local destinations at any level", async () => {
  for (const level of ["none", "agent", "all"]) {
    for (const address of ["169.254.169.254", "0.0.0.0", "::", "fe80::1"]) {
      const access = { level, allowed: ["*.example.com", "0.0.0.0/0", "[::]", "[fe80::1]"] };
      assert.equal(
        (
          await checkAgentNavigation("https://app.example.com", {
            access,
            resolveHost: async () => [address],
          })
        ).ok,
        false,
      );
      assert.equal(
        isAgentConnectionAllowed("https://app.example.com", address, { access }).ok,
        false,
      );
    }
  }
});

const { EventEmitter } = require("node:events");
const {
  trackBrowserNavigationInput,
  consumeBrowserNavigationInput,
  registerBrowserAccessSettings,
} = require("../src/browserAccessSettings");

it("consumes only a fresh native Enter once, not renderer claims", () => {
  const contents = new EventEmitter();
  let now = 100;
  trackBrowserNavigationInput(contents, () => now);
  assert.equal(
    consumeBrowserNavigationInput(contents, () => now),
    false,
  );
  contents.emit("before-input-event", {}, { type: "keyDown", key: "Enter" });
  assert.equal(
    consumeBrowserNavigationInput(contents, () => now),
    true,
  );
  assert.equal(
    consumeBrowserNavigationInput(contents, () => now),
    false,
  );
  contents.emit("before-input-event", {}, { type: "keyDown", key: "Enter" });
  now += 1501;
  assert.equal(
    consumeBrowserNavigationInput(contents, () => now),
    false,
  );
});
it("requires native consent for every widening and leaves cancelled settings unchanged", async () => {
  let settings = {
    browser_access: { level: "all", blocked: ["example.com"], ask: ["other.example"], allowed: [] },
  };
  const handlers = new Map();
  let confirmations = 0;
  registerBrowserAccessSettings({
    ipcMain: { handle: (name, fn) => handlers.set(name, fn) },
    isTrusted: () => true,
    loadSettings: () => structuredClone(settings),
    saveSettings: (next) => {
      settings = next;
    },
    confirmWidening: async () => {
      confirmations++;
      return false;
    },
  });
  const before = structuredClone(settings);
  for (const patch of [
    { level: "none" },
    { allowed: ["example.com"] },
    { blocked: [] },
    { ask: [] },
  ]) {
    const result = await handlers.get("omnigent:browser-access:set")(
      {},
      { ...settings.browser_access, ...patch },
    );
    assert.equal(result.ok, false);
    assert.deepEqual(settings, before);
  }
  assert.equal(confirmations, 4);
});

it("matches canonical IPv6 addresses and ports in every list", async () => {
  const url = "https://app.example.com:8443/";
  for (const [ip, entry] of [
    ["2606:4700:4700:0:0:0:0:1111", "[2606:4700:4700::1111]"],
    ["fd12:0:0:0:0:0:0:1", "[fd12::1]"],
  ]) {
    for (const list of ["blocked", "ask", "allowed"]) {
      const access = { level: "all", [list]: [`${entry}:8443`] };
      const options = { access, resolveHost: async () => [ip] };
      const dns = await checkAgentNavigation(url, options);
      const connected = isAgentConnectionAllowed(url, ip, options);
      for (const verdict of [dns, connected]) {
        assert.equal(verdict.ok, list === "allowed");
        assert.equal(Boolean(verdict.grant), list === "ask");
      }
      const publicOptions = {
        access: { level: "none", [list]: [`${entry}:443`] },
        resolveHost: async () => [ip],
      };
      assert.equal(
        (await checkAgentNavigation(url, publicOptions)).ok,
        true,
        "ports must still match",
      );
    }
  }
});
it("deferred URL preflight still refuses unmatched private and metadata DNS answers", async () => {
  const access = { level: "all", allowed: ["127.0.0.0/8"] };
  for (const ip of ["fd12::2", "169.254.169.254"]) {
    assert.equal(
      (
        await checkAgentNavigation("http://localhost:8080/", {
          access,
          resolveHost: async () => [ip],
        })
      ).ok,
      false,
    );
    assert.equal(isAgentConnectionAllowed("http://localhost:8080/", ip, { access }).ok, false);
  }
});
