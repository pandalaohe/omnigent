// Direct unit tests for the desktop auto-updater module.
//
// Unlike update-main.test.js (which boots the whole main.js in a vm to prove
// the wiring is hooked up), this file requires the module normally and drives
// its API with in-memory fakes for every injected dependency. That keeps the
// updater's own behavior — config normalization, feed gating, consent, event
// broadcasts, manual-error surfacing, and the install handoff — tested at the
// unit boundary the refactor introduced.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const yaml = require("js-yaml");

const {
  createDesktopUpdater,
  normalizeUpdateConfig,
  isUpdateSecurityError,
  UPDATE_MODES,
  UPDATES_UNAVAILABLE_IN_DEV,
} = require("../src/desktop_updater");

const PINNED_ORIGIN = "https://server.example";

/**
 * Build a fully-faked updater plus handles for asserting on the injected deps.
 *
 * @param {object} [opts]
 * @param {Record<string, unknown>} [opts.settings] Initial persisted settings.
 * @param {boolean} [opts.isPackaged] Simulate a packaged build.
 * @param {boolean} [opts.forceDevUpdateConfig] Enable the development update config.
 * @param {boolean} [opts.updatesEnabled] Whether this build may use the update feed.
 * @param {boolean} [opts.pinnedSender] Whether IPC calls count as trusted.
 * @param {Array<{response: number}>} [opts.dialogResponses] Queued dialog answers.
 * @param {() => string} [opts.getCurrentVersion] Display-version override.
 */
function makeUpdater({
  settings = {},
  isPackaged = false,
  forceDevUpdateConfig = false,
  updatesEnabled = true,
  pinnedSender = true,
  dialogResponses = [{ response: 1 }],
  getCurrentVersion,
  externalReleaseRepo = null,
  fetchFeed,
  openExternal,
} = {}) {
  let store = { ...settings };
  const calls = {
    sent: [],
    showMessageBox: [],
    appQuit: 0,
    checkForUpdates: 0,
    downloadUpdate: 0,
    quitAndInstall: [],
    intervals: [],
    clearedIntervals: 0,
    openedExternal: [],
    fetchedUrls: [],
  };

  const win = {
    isDestroyed: () => false,
    webContents: {
      send: (channel, payload) => calls.sent.push({ channel, payload }),
    },
  };

  const autoUpdater = new EventEmitter();
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.forceDevUpdateConfig = forceDevUpdateConfig;
  autoUpdater.checkForUpdates = () => {
    calls.checkForUpdates = (calls.checkForUpdates ?? 0) + 1;
    return Promise.resolve();
  };
  autoUpdater.downloadUpdate = () => {
    calls.downloadUpdate += 1;
    return Promise.resolve();
  };
  autoUpdater.quitAndInstall = (...args) => calls.quitAndInstall.push(args);

  const ipcHandlers = new Map();

  const deps = {
    app: {
      isPackaged,
      getVersion: () => "0.3.0",
      quit: () => {
        calls.appQuit += 1;
      },
    },
    BrowserWindow: {
      getAllWindows: () => [win],
      fromWebContents: () => win,
    },
    ipcMain: {
      handle: (channel, handler) => ipcHandlers.set(channel, handler),
    },
    dialog: {
      showMessageBox: (dialogWin, options) => {
        calls.showMessageBox.push({ win: dialogWin, options });
        return Promise.resolve(dialogResponses.shift() ?? { response: 1 });
      },
    },
    nativeImage: {
      createFromPath: () => ({ isEmpty: () => true }),
    },
    autoUpdater,
    loadSettings: () => ({ ...store }),
    saveSettings: (next) => {
      store = { ...next };
    },
    isPinnedOriginSender: () => pinnedSender,
    pinnedOrigin: () => PINNED_ORIGIN,
    iconPath: "/icons/icon.png",
    forceDevUpdateConfig,
    updatesEnabled,
    getCurrentVersion,
    externalReleaseRepo,
    fetchFeed: fetchFeed
      ? (url) => {
          calls.fetchedUrls.push(url);
          return fetchFeed(url);
        }
      : undefined,
    openExternal:
      openExternal ??
      ((url) => {
        calls.openedExternal.push(url);
        return Promise.resolve();
      }),
  };

  const updater = createDesktopUpdater(deps);
  return {
    updater,
    autoUpdater,
    calls,
    ipcHandlers,
    win,
    event: { sender: {} },
    readSettings: () => ({ ...store }),
  };
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

describe("desktop_updater — pure helpers", () => {
  it("normalizes unknown/missing config to safe defaults", () => {
    assert.deepEqual(normalizeUpdateConfig({}), {
      mode: "default",
      autoInstall: true,
      skippedVersion: null,
    });
    assert.deepEqual(normalizeUpdateConfig({ update_mode: "bogus" }).mode, "default");
    assert.deepEqual(
      normalizeUpdateConfig({
        update_mode: "manual",
        update_auto_install: false,
        update_skipped_version: "0.4.0",
      }),
      { mode: "manual", autoInstall: false, skippedVersion: "0.4.0" },
    );
  });

  it("exposes the update-mode set and dev-unavailable message", () => {
    assert.deepEqual([...UPDATE_MODES].sort(), ["default", "manual", "none", "start"]);
    assert.match(UPDATES_UNAVAILABLE_IN_DEV, /unavailable in development/);
  });

  it("uses the production update endpoint in development", () => {
    const devConfig = yaml.load(
      fs.readFileSync(path.join(__dirname, "../dev-app-update.yml"), "utf8"),
    );
    const packageConfig = require("../package.json");

    assert.equal(devConfig.url, packageConfig.build.publish[0].url);
    assert.match(devConfig.url, /^https:\/\//);
  });

  it("classifies signing/integrity errors as security errors", () => {
    assert.equal(isUpdateSecurityError("sha512 mismatch"), true);
    assert.equal(isUpdateSecurityError("app is not signed"), true);
    assert.equal(isUpdateSecurityError("Cannot find latest.yml: 404"), false);
  });
});

describe("desktop_updater — config persistence", () => {
  it("preserves unrelated settings keys when writing update config", () => {
    const h = makeUpdater({
      settings: {
        server_url: "https://server.example/app",
        window_bounds: { width: 1200, height: 800 },
        update_mode: "start",
      },
    });

    assert.deepEqual(
      plain(
        h.updater.setConfig({
          mode: "manual",
          autoInstall: false,
          skippedVersion: "0.4.0",
          ignored: "value",
        }),
      ),
      { mode: "manual", autoInstall: false, skippedVersion: "0.4.0" },
    );

    const saved = h.readSettings();
    assert.equal(saved.server_url, "https://server.example/app");
    assert.deepEqual(saved.window_bounds, { width: 1200, height: 800 });
    assert.equal(saved.update_mode, "manual");
    assert.equal(saved.update_auto_install, false);
    assert.equal(saved.update_skipped_version, "0.4.0");
    assert.equal(saved.mode, undefined);

    h.updater.setConfig({ mode: "bogus" });
    assert.equal(h.readSettings().update_mode, "manual");
  });
});

describe("desktop_updater — event wiring + broadcast", () => {
  it("broadcasts updater events and replays the latest status", () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();

    h.autoUpdater.emit("update-available", { version: "0.4.0" });

    assert.equal(h.autoUpdater.forceDevUpdateConfig, true);
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "available",
      currentVersion: "0.3.0",
      info: { version: "0.4.0" },
    });
    assert.deepEqual(plain(h.calls.sent), [
      {
        channel: "omnigent:update-status",
        payload: {
          state: "available",
          currentVersion: "0.3.0",
          info: { version: "0.4.0" },
        },
      },
    ]);

    h.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "downloaded",
      currentVersion: "0.3.0",
      info: { version: "0.4.0" },
    });
  });

  it("uses an injected display version for update prompts", () => {
    const h = makeUpdater({
      forceDevUpdateConfig: true,
      settings: { update_mode: "manual" },
      getCurrentVersion: () => "0.2.0",
    });
    h.updater.init();

    h.autoUpdater.emit("update-available", { version: "0.4.0" });
    assert.equal(h.updater.getStatus().currentVersion, "0.2.0");

    h.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
    assert.equal(h.updater.getStatus().currentVersion, "0.2.0");
  });

  it("start mode kicks off a check with no lingering periodic timer", () => {
    // "start" runs one check at launch (unlike "default", which also schedules
    // a 6-hour re-check); asserting on it avoids leaving a live interval that
    // would keep the test process alive.
    const h = makeUpdater({ isPackaged: true, settings: { update_mode: "start" } });
    h.updater.init();
    assert.equal(h.calls.checkForUpdates, 1);
  });
});

describe("desktop_updater — check errors", () => {
  it("restores the prior state after a background check fails", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "start" } });
    h.autoUpdater.checkForUpdates = () => {
      const err = new Error("background feed unavailable");
      h.autoUpdater.emit("checking-for-update");
      h.autoUpdater.emit("error", err);
      return Promise.reject(err);
    };

    h.updater.init();
    await Promise.resolve();

    assert.deepEqual(plain(h.updater.getStatus()), { state: "idle" });
  });

  it("surfaces manual check failures without a false available/none flip", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.autoUpdater.checkForUpdates = () => {
      const err = new Error("Cannot find latest.yml: 404");
      h.autoUpdater.emit("error", err);
      return Promise.reject(err);
    };

    await assert.rejects(h.updater.checkForUpdates({ manual: true }), /latest\.yml/);
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "idle",
      lastError: "Cannot find latest.yml: 404",
    });
  });

  it("marks signing errors as error-security", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.autoUpdater.checkForUpdates = () => {
      const err = new Error("sha512 checksum mismatch");
      h.autoUpdater.emit("error", err);
      return Promise.reject(err);
    };

    await assert.rejects(h.updater.checkForUpdates({ manual: true }), /sha512/);
    assert.equal(h.updater.getStatus().state, "error-security");
  });
});

describe("desktop_updater — downloads", () => {
  it("preserves a downloaded update instead of re-checking", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.autoUpdater.emit("update-downloaded", { version: "0.4.0" });

    await h.updater.checkForUpdates({ manual: true });
    h.autoUpdater.emit("checking-for-update");
    h.autoUpdater.emit("update-not-available");

    assert.equal(h.calls.checkForUpdates ?? 0, 0);
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "downloaded",
      currentVersion: "0.3.0",
      info: { version: "0.4.0" },
    });
  });

  it("broadcasts a cached 0% state as soon as downloading starts", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    let finishDownload;
    h.autoUpdater.downloadUpdate = () =>
      new Promise((resolve) => {
        finishDownload = resolve;
      });

    const download = h.updater.downloadUpdate();
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "downloading",
      progress: { percent: 0 },
    });

    finishDownload();
    await download;
  });

  it("leaves downloading state and exposes a retryable error", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.autoUpdater.emit("download-progress", { percent: 42 });
    h.autoUpdater.downloadUpdate = () => Promise.reject(new Error("download interrupted"));

    await assert.rejects(h.updater.downloadUpdate(), /download interrupted/);
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "idle",
      lastError: "download interrupted",
    });
  });
});

describe("desktop_updater — development update config gating", () => {
  it("does not offer production updates to a packaged dev build", async () => {
    const h = makeUpdater({ isPackaged: true, updatesEnabled: false });
    h.updater.init();
    assert.equal(h.calls.checkForUpdates ?? 0, 0);
    await assert.rejects(h.updater.checkForUpdates({ manual: true }), /unavailable in development/);
  });

  it("blocks manual paths when the feed is unavailable in development", async () => {
    const h = makeUpdater({ settings: { update_mode: "manual" } });
    h.updater.registerIpc();

    await Promise.all(
      [
        "omnigent:update-check",
        "omnigent:update-download",
        "omnigent:update-install",
        "omnigent:set-update-config",
      ].map((channel) =>
        assert.rejects(
          h.ipcHandlers.get(channel)(h.event, { mode: "manual" }),
          /unavailable in development/,
        ),
      ),
    );
    assert.equal(h.calls.showMessageBox.length, 0);
    assert.equal(h.calls.checkForUpdates ?? 0, 0);
    assert.equal(h.calls.downloadUpdate, 0);
    assert.equal(h.updater.installPending, false);
  });
});

describe("desktop_updater — IPC trust + consent", () => {
  it("rejects every privileged handler from a non-pinned sender", async () => {
    const h = makeUpdater({ pinnedSender: false });
    h.updater.registerIpc();

    const cases = [
      ["omnigent:get-update-config", []],
      ["omnigent:get-update-status", []],
      ["omnigent:update-check", []],
      ["omnigent:update-download", []],
      ["omnigent:update-install", []],
      ["omnigent:set-update-config", [{ mode: "manual" }]],
    ];
    await Promise.all(
      cases.map(([channel, args]) =>
        assert.rejects(
          Promise.resolve().then(() => h.ipcHandlers.get(channel)(h.event, ...args)),
          /connected server page/,
        ),
      ),
    );
  });

  it("prompts and runs each privileged action when approved", async () => {
    // download
    let h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.updater.registerIpc();
    await h.ipcHandlers.get("omnigent:update-download")(h.event);
    assert.equal(h.calls.showMessageBox.length, 1);
    assert.equal(h.calls.showMessageBox[0].options.message, "Download an Omnigent update?");
    assert.equal(h.calls.downloadUpdate, 1);

    // set-config
    h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "start" } });
    h.updater.init();
    h.updater.registerIpc();
    await h.ipcHandlers.get("omnigent:set-update-config")(h.event, { mode: "manual" });
    assert.equal(h.calls.showMessageBox[0].options.message, "Change Omnigent update settings?");
    assert.equal(h.readSettings().update_mode, "manual");
  });

  it("does not let a denied consent run the action", async () => {
    const h = makeUpdater({
      forceDevUpdateConfig: true,
      settings: { update_mode: "start" },
      dialogResponses: [{ response: 0 }],
    });
    h.updater.init();
    h.updater.registerIpc();

    await assert.rejects(h.ipcHandlers.get("omnigent:update-download")(h.event), /approved/);
    assert.equal(h.calls.downloadUpdate, 0);
  });
});

describe("desktop_updater — install handoff", () => {
  it("routes an approved install through before-quit to quitAndInstall", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.autoUpdater.emit("update-downloaded", { version: "0.4.0" });
    h.updater.registerIpc();

    await h.ipcHandlers.get("omnigent:update-install")(h.event);
    assert.equal(h.calls.showMessageBox.length, 1);
    assert.equal(h.updater.installPending, true);
    assert.equal(h.calls.appQuit, 1);

    assert.equal(h.updater.quitAndInstallIfPending(), true);
    assert.deepEqual(h.calls.quitAndInstall, [[false, true]]);
  });

  it("install fails cleanly when no update is downloaded", async () => {
    const h = makeUpdater({ forceDevUpdateConfig: true, settings: { update_mode: "manual" } });
    h.updater.init();
    h.updater.registerIpc();

    await assert.rejects(
      h.ipcHandlers.get("omnigent:update-install")(h.event),
      /No downloaded update/,
    );
    assert.equal(h.updater.installPending, false);
    assert.equal(h.calls.appQuit, 0);
    // Nothing pending → the before-quit handoff is a no-op.
    assert.equal(h.updater.quitAndInstallIfPending(), false);
    assert.deepEqual(h.calls.quitAndInstall, []);
  });
});

function atomFeed(tags, repo = "example-org/omnigent-dist") {
  const entries = tags
    .map(
      (tag) =>
        `<entry><id>tag:github.com,2008:Repository/1/${tag}</id>` +
        `<link rel="alternate" type="text/html" href="https://github.com/${repo}/releases/tag/${tag}"/>` +
        `<title>omnigent-dist ${tag}</title></entry>`,
    )
    .join("");
  return `<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">${entries}</feed>`;
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("desktop_updater — external release feed mode (unsigned mac build)", () => {
  it("parses and compares -wukong.N versions exactly (including BigInt neighbours) and ignores non-wukong suffixes", () => {
    const {
      isNewerDistributionVersion,
      parseDistributionVersion,
      parseReleasesAtom,
      normalizeExternalReleaseRepo,
      parseExpectedReleaseTagUrl,
    } = require("../src/desktop_updater");

    assert.equal(isNewerDistributionVersion("0.21.0-wukong.10", "0.21.0-wukong.9"), true);
    assert.equal(isNewerDistributionVersion("0.22.0-wukong.13", "0.21.0-wukong.12"), true);
    assert.equal(isNewerDistributionVersion("0.21.0-wukong.12", "0.21.0-wukong.12"), false);
    assert.equal(isNewerDistributionVersion("0.21.0-wukong.11", "0.21.0-wukong.12"), false);
    // Exact BigInt comparison beyond Number.MAX_SAFE_INTEGER (9007199254740991).
    assert.equal(
      isNewerDistributionVersion(
        "0.21.0-wukong.9007199254740993",
        "0.21.0-wukong.9007199254740992",
      ),
      true,
    );
    assert.equal(
      isNewerDistributionVersion(
        "0.21.0-wukong.9007199254740992",
        "0.21.0-wukong.9007199254740993",
      ),
      false,
    );
    assert.equal(
      isNewerDistributionVersion(
        "0.21.0-wukong.9007199254740992",
        "0.21.0-wukong.9007199254740992",
      ),
      false,
    );
    // Oversized (>64 digits) and other pre-release suffixes are rejected.
    assert.equal(parseDistributionVersion(`0.21.0-wukong.${"9".repeat(65)}`), null);
    for (const bad of [
      "0.99.0",
      "0.21.0-rc.1",
      "0.21.0-beta.2",
      "0.21.0-alpha.1",
      "0.21.0-wukong.12-rc.1",
      "0.21.0-wukong.12+build.1",
      "0.21.0.dev0",
    ]) {
      assert.equal(isNewerDistributionVersion(bad, "0.21.0-wukong.12"), false);
    }

    // Repo validation rejects dot segments and malformed values.
    assert.equal(
      normalizeExternalReleaseRepo("example-org/omnigent-dist"),
      "example-org/omnigent-dist",
    );
    for (const badRepo of ["../evil", "./repo", "owner/..", "owner/.", "a/b/c", "noslash", ""]) {
      assert.equal(normalizeExternalReleaseRepo(badRepo), null);
      assert.throws(() => parseReleasesAtom(atomFeed([]), badRepo));
    }

    // Strict release URL validation rejects foreign repos, credentials, ports, query, fragment, dot segments, encoded separators.
    for (const badUrl of [
      "https://github.com/other-org/other-repo/releases/tag/v0.22.0-wukong.99",
      "https://evil.example/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.99",
      "http://github.com/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.99",
      "https://user:pass@github.com/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.99",
      "https://github.com:443/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.99",
      "https://github.com:8443/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.99",
      "https://github.com/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.99?foo=1",
      "https://github.com/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.99#frag",
      "https://github.com/other/repo/../../example-org/omnigent-dist/releases/tag/v0.22.0-wukong.99",
      "https://github.com/example-org/omnigent-dist/releases/tag/./v0.22.0-wukong.99",
      "https://github.com/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.99%2fextra",
      "https://github.com/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.99%5cextra",
    ]) {
      assert.equal(parseExpectedReleaseTagUrl(badUrl, "example-org/omnigent-dist"), null, badUrl);
    }

    // Foreign-repo same-format entry, ID-only entry, non-alternate link, and oversized entry are skipped one by one.
    const mixedFeed =
      '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">' +
      "<entry><id>tag:github.com,2008:Repository/1/v9.9.9-wukong.99</id>" +
      '<link rel="alternate" href="https://github.com/foreign-org/foreign-repo/releases/tag/v9.9.9-wukong.99"/></entry>' +
      "<entry><id>tag:github.com,2008:Repository/1/v8.8.8-wukong.88</id></entry>" +
      '<entry><link rel="related" href="https://github.com/example-org/omnigent-dist/releases/tag/v7.7.7-wukong.77"/></entry>' +
      `<entry><link rel="alternate" href="https://github.com/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.${"9".repeat(70)}"/></entry>` +
      '<entry><link rel="alternate" href="https://github.com/example-org/omnigent-dist/releases/tag/v0.21.0-wukong.9"/></entry>' +
      '<entry><link rel="alternate" href="https://github.com/example-org/omnigent-dist/releases/tag/v0.99.0"/></entry>' +
      '<entry><link rel="alternate" href="https://github.com/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.13"/></entry>' +
      "</feed>";
    const parsed = parseReleasesAtom(mixedFeed, "example-org/omnigent-dist");
    assert.deepEqual(parsed, {
      version: "0.22.0-wukong.13",
      tag: "v0.22.0-wukong.13",
      releaseUrl: "https://github.com/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.13",
    });

    // Corrupt XML or non-Atom documents throw so callers treat them as errors.
    for (const corrupt of [
      "",
      "<html><body>Not an Atom feed</body></html>",
      '<?xml version="1.0"?><feed><entry><link href="https://github.com/example-org/omnigent-dist/releases/tag/v0.22.0-wukong.13"/>',
      '<?xml version="1.0"?><feed><entry></feed>',
      '<?xml version="1.0"?><!DOCTYPE feed><feed/>',
      '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="a" href="b"/></entry></feed>',
    ]) {
      assert.throws(() => parseReleasesAtom(corrupt, "example-org/omnigent-dist"), corrupt);
    }
  });

  it("exposes stable deliveryMode='manual-download' in getConfig only for external mode", () => {
    const ext = makeUpdater({
      isPackaged: true,
      externalReleaseRepo: "example-org/omnigent-dist",
    });
    assert.equal(ext.updater.getConfig().deliveryMode, "manual-download");

    const legacy = makeUpdater({ isPackaged: true });
    assert.equal(legacy.updater.getConfig().deliveryMode, undefined);
  });

  it("announces a newer release with a download URL and opens it without calling autoUpdater", async () => {
    const h = makeUpdater({
      isPackaged: true,
      settings: { update_mode: "manual" },
      getCurrentVersion: () => "0.21.0-wukong.12",
      externalReleaseRepo: "example-org/omnigent-dist",
      fetchFeed: async () => ({
        status: 200,
        text: atomFeed(["v0.21.0-wukong.12", "v0.21.0-wukong.13"]),
      }),
    });
    h.updater.init();
    await h.updater.checkForUpdates({ manual: true });

    assert.deepEqual(h.calls.fetchedUrls, [
      "https://github.com/example-org/omnigent-dist/releases.atom",
    ]);
    assert.equal(h.calls.checkForUpdates, 0);
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "available",
      currentVersion: "0.21.0-wukong.12",
      info: {
        version: "0.21.0-wukong.13",
        downloadUrl: "https://github.com/example-org/omnigent-dist/releases/tag/v0.21.0-wukong.13",
      },
    });

    await h.updater.downloadUpdate();
    assert.equal(h.calls.downloadUpdate, 0);
    assert.deepEqual(h.calls.openedExternal, [
      "https://github.com/example-org/omnigent-dist/releases/tag/v0.21.0-wukong.13",
    ]);
    assert.equal(h.updater.installUpdateNow(), false);
    assert.equal(h.updater.quitAndInstallIfPending(), false);
    assert.deepEqual(h.calls.quitAndInstall, []);
  });

  it("re-checks the normalised release URL right before openExternal", async () => {
    const h = makeUpdater({
      isPackaged: true,
      settings: { update_mode: "manual" },
      getCurrentVersion: () => "0.21.0-wukong.12",
      externalReleaseRepo: "example-org/omnigent-dist",
      fetchFeed: async () => ({
        status: 200,
        text: atomFeed(["v0.21.0-wukong.13"]),
      }),
    });
    h.updater.init();
    await h.updater.checkForUpdates({ manual: true });
    // Tamper the cached downloadUrl with query/dot-segments before downloadUpdate().
    h.updater.getStatus().info.downloadUrl =
      "https://github.com/example-org/omnigent-dist/releases/tag/v0.21.0-wukong.13?evil=1";
    await assert.rejects(h.updater.downloadUpdate(), /No release download link/);
    assert.deepEqual(h.calls.openedExternal, []);
  });

  it("refuses redirects to another origin or repo and treats corrupt XML as an error", async () => {
    const responses = [
      { status: 302, text: "" },
      {
        status: 200,
        url: "https://github.com/other-org/other-repo/releases.atom",
        text: atomFeed(["v0.21.0-wukong.99"]),
      },
      { status: 200, text: "<html>broken</html>" },
      { status: 200, text: '<?xml version="1.0"?><feed><entry>' },
    ];
    await Promise.all(
      responses.map(async (response) => {
        const h = makeUpdater({
          isPackaged: true,
          settings: { update_mode: "manual" },
          getCurrentVersion: () => "0.21.0-wukong.12",
          externalReleaseRepo: "example-org/omnigent-dist",
          fetchFeed: async () => response,
        });
        h.updater.init();

        await h.updater.checkForUpdates({ manual: false });
        assert.deepEqual(plain(h.updater.getStatus()), { state: "idle" });

        await h.updater.checkForUpdates({ manual: true });
        assert.deepEqual(plain(h.updater.getStatus()), {
          state: "idle",
          manualOnly: true,
          lastError: "Couldn't check for updates.",
        });
      }),
    );
  });

  it("defaultFetchFeed refuses HTTP 302 redirects to another origin or repo", async (t) => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => ({
      status: 302,
      headers: {
        get: (k) =>
          k.toLowerCase() === "location" ? "https://github.com/evil/repo/releases.atom" : null,
      },
      text: async () => "",
    });
    t.after(() => {
      globalThis.fetch = originalFetch;
    });

    const h = makeUpdater({
      isPackaged: true,
      settings: { update_mode: "manual" },
      getCurrentVersion: () => "0.21.0-wukong.12",
      externalReleaseRepo: "example-org/omnigent-dist",
      fetchFeed: undefined,
    });
    h.updater.init();
    await h.updater.checkForUpdates({ manual: true });
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "idle",
      manualOnly: true,
      lastError: "Couldn't check for updates.",
    });
  });

  it("stays silent on background up-to-date/empty/404 and reports up-to-date on manual check", async () => {
    const cases = [
      { status: 404, text: "Not Found" },
      { status: 200, text: atomFeed([]) },
      { status: 200, text: atomFeed(["v0.21.0-wukong.12"]) },
    ];
    await Promise.all(
      cases.map(async (response) => {
        const h = makeUpdater({
          isPackaged: true,
          settings: { update_mode: "manual" },
          getCurrentVersion: () => "0.21.0-wukong.12",
          externalReleaseRepo: "example-org/omnigent-dist",
          fetchFeed: async () => response,
        });
        h.updater.init();

        await h.updater.checkForUpdates({ manual: false });
        assert.deepEqual(plain(h.updater.getStatus()), { state: "idle" });

        await h.updater.checkForUpdates({ manual: true });
        assert.deepEqual(plain(h.updater.getStatus()), { state: "none", manualOnly: true });
        assert.equal(h.calls.showMessageBox.length, 0);
      }),
    );
  });

  it("shares one in-flight external check across startup and manual calls (both join orders) and protects manual feedback", async () => {
    // Order 1: startup background check in flight -> manual joins -> resolves with 404 -> manual gets up-to-date.
    const d1 = deferred();
    let fetchCount1 = 0;
    const h1 = makeUpdater({
      isPackaged: true,
      settings: { update_mode: "manual" },
      getCurrentVersion: () => "0.21.0-wukong.12",
      externalReleaseRepo: "example-org/omnigent-dist",
      fetchFeed: () => {
        fetchCount1 += 1;
        return d1.promise;
      },
    });
    h1.updater.init();
    const bg1 = h1.updater.checkForUpdates({ manual: false });
    assert.equal(h1.updater.getStatus().state, "idle");
    const man1 = h1.updater.checkForUpdates({ manual: true });
    assert.equal(h1.updater.getStatus().state, "checking");
    assert.equal(fetchCount1, 1);
    d1.resolve({ status: 404, text: "Not Found" });
    await Promise.all([bg1, man1]);
    assert.deepEqual(plain(h1.updater.getStatus()), { state: "none", manualOnly: true });

    // Order 2: startup background check in flight -> manual joins -> rejects -> manual gets inline error.
    const d2 = deferred();
    let fetchCount2 = 0;
    const h2 = makeUpdater({
      isPackaged: true,
      settings: { update_mode: "manual" },
      getCurrentVersion: () => "0.21.0-wukong.12",
      externalReleaseRepo: "example-org/omnigent-dist",
      fetchFeed: () => {
        fetchCount2 += 1;
        return d2.promise;
      },
    });
    h2.updater.init();
    const bg2 = h2.updater.checkForUpdates({ manual: false });
    const man2 = h2.updater.checkForUpdates({ manual: true });
    assert.equal(fetchCount2, 1);
    d2.reject(new Error("ENOTFOUND github.com"));
    await Promise.all([bg2, man2]);
    assert.deepEqual(plain(h2.updater.getStatus()), {
      state: "idle",
      manualOnly: true,
      lastError: "Couldn't check for updates.",
    });

    // Order 3: manual check starts first -> background joins -> resolves with empty -> manual still gets manual result.
    const d3 = deferred();
    let fetchCount3 = 0;
    const h3 = makeUpdater({
      isPackaged: true,
      settings: { update_mode: "manual" },
      getCurrentVersion: () => "0.21.0-wukong.12",
      externalReleaseRepo: "example-org/omnigent-dist",
      fetchFeed: () => {
        fetchCount3 += 1;
        return d3.promise;
      },
    });
    h3.updater.init();
    const man3 = h3.updater.checkForUpdates({ manual: true });
    const bg3 = h3.updater.checkForUpdates({ manual: false });
    const man3Dup = h3.updater.checkForUpdates({ manual: true });
    assert.equal(fetchCount3, 1);
    d3.resolve({ status: 200, text: atomFeed(["v0.21.0-wukong.12"]) });
    await Promise.all([man3, bg3, man3Dup]);
    assert.deepEqual(plain(h3.updater.getStatus()), { state: "none", manualOnly: true });

    // Order 4: manual completes first (available / up-to-date / error), then later background 404/error NEVER overwrites it.
    let nextResp = { status: 200, text: atomFeed(["v0.21.0-wukong.13"]) };
    let nextErrorMessage = null;
    const h4 = makeUpdater({
      isPackaged: true,
      settings: { update_mode: "manual" },
      getCurrentVersion: () => "0.21.0-wukong.12",
      externalReleaseRepo: "example-org/omnigent-dist",
      fetchFeed: async () => {
        if (nextErrorMessage) throw new Error(nextErrorMessage);
        return nextResp;
      },
    });
    h4.updater.init();
    await h4.updater.checkForUpdates({ manual: true });
    const availableSnap = plain(h4.updater.getStatus());
    assert.equal(availableSnap.state, "available");

    nextResp = { status: 404, text: "Not Found" };
    await h4.updater.checkForUpdates({ manual: false });
    assert.deepEqual(plain(h4.updater.getStatus()), availableSnap);

    nextErrorMessage = "transient network error";
    await h4.updater.checkForUpdates({ manual: false });
    assert.deepEqual(plain(h4.updater.getStatus()), availableSnap);

    // Manual up-to-date is also preserved across later background 404/error.
    nextErrorMessage = null;
    nextResp = { status: 200, text: atomFeed(["v0.21.0-wukong.12"]) };
    await h4.updater.checkForUpdates({ manual: true });
    assert.deepEqual(plain(h4.updater.getStatus()), { state: "none", manualOnly: true });
    nextResp = { status: 404, text: "Not Found" };
    await h4.updater.checkForUpdates({ manual: false });
    assert.deepEqual(plain(h4.updater.getStatus()), { state: "none", manualOnly: true });
    nextErrorMessage = "offline";
    await h4.updater.checkForUpdates({ manual: false });
    assert.deepEqual(plain(h4.updater.getStatus()), { state: "none", manualOnly: true });
  });

  it("stays silent on background network error and reports a plain message on manual check", async () => {
    const h = makeUpdater({
      isPackaged: true,
      settings: { update_mode: "manual" },
      getCurrentVersion: () => "0.21.0-wukong.12",
      externalReleaseRepo: "example-org/omnigent-dist",
      fetchFeed: async () => {
        throw new Error("ENOTFOUND github.com");
      },
    });
    h.updater.init();

    await h.updater.checkForUpdates({ manual: false });
    assert.deepEqual(plain(h.updater.getStatus()), { state: "idle" });
    assert.equal(h.calls.showMessageBox.length, 0);

    await h.updater.checkForUpdates({ manual: true });
    assert.deepEqual(plain(h.updater.getStatus()), {
      state: "idle",
      manualOnly: true,
      lastError: "Couldn't check for updates.",
    });
    assert.equal(h.calls.showMessageBox.length, 0);
  });
});
