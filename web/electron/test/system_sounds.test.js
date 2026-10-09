"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  FALLBACK_SYSTEM_SOUNDS,
  legacyNotificationSound,
  listSystemSounds,
  resolveSystemSound,
} = require("../src/system_sounds");

const STOCK_MACOS_SOUNDS = [
  "Basso",
  "Blow",
  "Bottle",
  "Frog",
  "Funk",
  "Glass",
  "Hero",
  "Morse",
  "Ping",
  "Pop",
  "Purr",
  "Sosumi",
  "Submarine",
  "Tink",
];

function darwinDeps(files) {
  return {
    platform: "darwin",
    readdirSync: (dir) => {
      assert.equal(dir, "/System/Library/Sounds");
      if (files instanceof Error) throw files;
      return files;
    },
    env: {},
  };
}

describe("listSystemSounds", () => {
  it("lists macOS .aiff names without extension, sorted, ignoring other files", () => {
    const names = listSystemSounds(
      darwinDeps(["Glass.aiff", "Basso.aiff", "readme.txt", "Ping.aiff", "subdir"]),
    );

    assert.deepEqual(names, ["Basso", "Glass", "Ping"]);
  });

  it("falls back to the stock macOS set when the sounds dir can't be read", () => {
    const names = listSystemSounds(darwinDeps(new Error("ENOENT")));

    assert.deepEqual(names, STOCK_MACOS_SOUNDS);
    assert.deepEqual(FALLBACK_SYSTEM_SOUNDS, STOCK_MACOS_SOUNDS);
  });

  it("lists Windows .wav names from WINDIR\\Media", () => {
    const seen = [];
    const names = listSystemSounds({
      platform: "win32",
      readdirSync: (dir) => {
        seen.push(dir);
        return ["chimes.wav", "ding.wav", "notasound.txt"];
      },
      env: { WINDIR: "C:\\Windows" },
    });

    assert.deepEqual(seen, ["C:\\Windows\\Media"]);
    assert.deepEqual(names, ["chimes", "ding"]);
  });

  it("falls back to SystemRoot then C:\\Windows for the Windows sounds dir", () => {
    const seen = [];
    const deps = (env) => ({
      platform: "win32",
      readdirSync: (dir) => {
        seen.push(dir);
        return [];
      },
      env,
    });

    listSystemSounds(deps({ SystemRoot: "D:\\Win" }));
    listSystemSounds(deps({}));

    assert.deepEqual(seen, ["D:\\Win\\Media", "C:\\Windows\\Media"]);
  });

  it("returns an empty list on unsupported platforms", () => {
    assert.deepEqual(
      listSystemSounds({
        platform: "linux",
        readdirSync: () => {
          throw new Error("should not read");
        },
        env: {},
      }),
      [],
    );
  });
});

describe("resolveSystemSound", () => {
  it("resolves a listed macOS name to its file path", () => {
    const deps = darwinDeps(["Glass.aiff", "Ping.aiff"]);

    assert.equal(resolveSystemSound("Glass", deps), "/System/Library/Sounds/Glass.aiff");
  });

  it("resolves a listed Windows name to its file path", () => {
    const deps = {
      platform: "win32",
      readdirSync: () => ["ding.wav"],
      env: { WINDIR: "C:\\Windows" },
    };

    assert.equal(resolveSystemSound("ding", deps), "C:\\Windows\\Media\\ding.wav");
  });

  it("rejects traversal, extensions, empty and path-separated names", () => {
    const deps = darwinDeps(["Glass.aiff"]);

    assert.equal(resolveSystemSound("../Glass", deps), null);
    assert.equal(resolveSystemSound("Glass.aiff", deps), null);
    assert.equal(resolveSystemSound("", deps), null);
    assert.equal(resolveSystemSound("Sub/Dir", deps), null);
    assert.equal(resolveSystemSound("Nope", deps), null);
    assert.equal(resolveSystemSound(7, deps), null);
  });
});

describe("legacyNotificationSound", () => {
  it("returns nulls when the settings are missing", () => {
    assert.deepEqual(legacyNotificationSound(undefined), { enabled: null, name: null });
    assert.deepEqual(legacyNotificationSound({}), { enabled: null, name: null });
  });

  it("reads an enabled sound with a name", () => {
    assert.deepEqual(
      legacyNotificationSound({
        notification_sound_enabled: true,
        notification_sound_name: "Glass",
      }),
      { enabled: true, name: "Glass" },
    );
  });

  it("reads a disabled switch", () => {
    assert.deepEqual(legacyNotificationSound({ notification_sound_enabled: false }), {
      enabled: false,
      name: null,
    });
  });

  it("rejects a non-string or empty name", () => {
    assert.deepEqual(legacyNotificationSound({ notification_sound_name: 3 }), {
      enabled: null,
      name: null,
    });
    assert.deepEqual(legacyNotificationSound({ notification_sound_name: "" }), {
      enabled: null,
      name: null,
    });
  });
});
