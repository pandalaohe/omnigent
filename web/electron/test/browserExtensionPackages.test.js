/* eslint-disable no-await-in-loop -- Each lifecycle assertion depends on its preceding operation. */
"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const {
  requestExtensionPermissions,
  validateManifest,
  validateZipEntry,
  createExtensionPackages,
} = require("../src/browserExtensionPackages");

const manifest = {
  manifest_version: 3,
  name: "Synthetic extension",
  version: "1.0",
  permissions: ["storage"],
};
test("main refuses MV2 and both dangerous permissions without rejecting optional declarations", () => {
  assert.throws(() => validateManifest({ ...manifest, manifest_version: 2 }), /Manifest V3/);
  for (const permission of ["nativeMessaging", "debugger"]) {
    for (const field of ["permissions"]) {
      assert.throws(
        () => validateManifest({ ...manifest, [field]: [permission] }),
        new RegExp(permission),
      );
    }
  }
});
test("optional dangerous permissions resolve false, while a supported permission can succeed", async () => {
  for (const permission of ["nativeMessaging", "debugger"]) {
    assert.doesNotThrow(() =>
      validateManifest({ ...manifest, optional_permissions: [permission] }),
    );
    assert.equal(await requestExtensionPermissions({}, { permissions: [permission] }), false);
  }
  assert.equal(await requestExtensionPermissions({}, { permissions: ["storage"] }), true);
});
test("archive rejects traversal, absolute paths, backslashes and symlinks", () => {
  for (const fileName of ["../escape", "/escape", "C:/escape", "a/../../escape", "a\\escape"]) {
    assert.throws(() => validateZipEntry({ fileName }), /Unsafe/);
  }
  assert.throws(
    () => validateZipEntry({ fileName: "link", externalFileAttributes: 0o120777 << 16 }),
    /Unsafe/,
  );
  assert.doesNotThrow(() => validateZipEntry({ fileName: "scripts/main.js" }));
});
test("native selection ignores renderer paths; cancellation never installs; source is copied", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "extensions-test-"));
  try {
    const source = path.join(root, "source");
    await fs.mkdir(source);
    await fs.writeFile(path.join(source, "manifest.json"), JSON.stringify(manifest));
    let response = 0,
      changes = 0;
    const packages = createExtensionPackages({
      directory: path.join(root, "installed"),
      dialog: {
        showOpenDialog: async () => ({ filePaths: [source] }),
        showMessageBox: async () => ({ response }),
      },
      changed: async () => {
        changes++;
      },
    });
    assert.equal((await packages.add({}, "/renderer/evil")).ok, false);
    assert.deepEqual(packages.list(), []);
    assert.equal(changes, 0);
    response = 1;
    assert.equal((await packages.add({}, "/renderer/evil")).ok, true);
    const [entry] = packages.list();
    assert.equal(entry.name, manifest.name);
    assert.equal((await packages.records())[0].agentFillAllowed, false);
    await fs.rm(source, { recursive: true });
    assert.equal(
      (await packages.records())[0].directory.startsWith(path.join(root, "installed")),
      true,
    );
    assert.equal((await packages.setEnabled({}, entry.id, false)).ok, true);
    assert.deepEqual(await packages.records(), []);
    assert.equal((await packages.remove({}, entry.id)).ok, true);
    assert.deepEqual(packages.list(), []);
    assert.equal(changes, 3);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("Bitwarden agent-fill consent is separate, default off, content-bound and cleared irreversibly on mismatch", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "consent-test-"));
  try {
    const source = path.join(root, "source");
    await fs.mkdir(source);
    await fs.writeFile(
      path.join(source, "manifest.json"),
      JSON.stringify({ ...manifest, name: "Bitwarden" }),
    );
    await fs.writeFile(path.join(source, "code.js"), "// original");
    let checked = false;
    const prompts = [];
    const packages = createExtensionPackages({
      directory: path.join(root, "installed"),
      dialog: {
        showOpenDialog: async () => ({ filePaths: [source] }),
        showMessageBox: async (_window, options) => {
          prompts.push(options);
          return { response: 1, checkboxChecked: checked };
        },
      },
    });
    assert.equal((await packages.add({})).ok, true);
    const [entry] = packages.list();
    assert.equal((await packages.records())[0].agentFillAllowed, false);
    const prompt = prompts.find((item) => item.checkboxLabel);
    assert.equal(prompt.checkboxChecked, false);
    checked = true;
    assert.equal((await packages.review({}, entry.id)).ok, true);
    assert.equal((await packages.records())[0].agentFillAllowed, true);
    const installed = path.join(root, "installed", entry.id, "code.js");
    await fs.writeFile(installed, "// changed");
    assert.equal((await packages.records())[0].agentFillAllowed, false);
    await fs.writeFile(installed, "// original");
    assert.equal((await packages.records())[0].agentFillAllowed, false);
    assert.equal((await packages.review({}, entry.id)).ok, true);
    assert.equal((await packages.records())[0].agentFillAllowed, true);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});
test("copied manifest keys cannot replace another extension's runtime ID", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "duplicate-test-"));
  try {
    const source = path.join(root, "source");
    await fs.mkdir(source);
    await fs.writeFile(
      path.join(source, "manifest.json"),
      JSON.stringify({ ...manifest, key: Buffer.from("synthetic-public-key").toString("base64") }),
    );
    const packages = createExtensionPackages({
      directory: path.join(root, "installed"),
      dialog: {
        showOpenDialog: async () => ({ filePaths: [source] }),
        showMessageBox: async () => ({ response: 1 }),
      },
    });
    assert.equal((await packages.add({})).ok, true);
    const duplicate = await packages.add({});
    assert.equal(duplicate.ok, false);
    assert.match(duplicate.error, /runtime ID/);
    assert.equal(packages.list().length, 1);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("a runtime load failure disables only the new package and restores the working set", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "load-failure-test-"));
  try {
    const source = path.join(root, "source");
    await fs.mkdir(source);
    await fs.writeFile(path.join(source, "manifest.json"), JSON.stringify(manifest));
    let packages,
      failed = false;
    packages = createExtensionPackages({
      directory: path.join(root, "installed"),
      dialog: {
        showMessageBox: async () => ({ response: 1 }),
        showOpenDialog: async () => ({ filePaths: [source] }),
      },
      changed: async () => {
        if (packages.list().length === 2 && packages.list()[1].enabled && !failed) {
          failed = true;
          throw Error("Synthetic load failure");
        }
      },
    });
    assert.equal((await packages.add({})).ok, true);
    assert.equal((await packages.add({})).ok, false);
    assert.deepEqual(
      packages.list().map((entry) => entry.enabled),
      [true, false],
    );
    const second = packages.list()[1];
    // Removal uses persisted identity even if the disabled source package is corrupt.
    await fs.writeFile(path.join(root, "installed", second.id, "manifest.json"), "corrupt");
    assert.equal((await packages.remove({}, second.id)).ok, true);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("a traversal zip is refused by the actual importer before native install confirmation", async () => {
  const { crc32 } = require("node:zlib");
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "zip-test-"));
  try {
    const filename = Buffer.from("../escape.txt"),
      data = Buffer.from("synthetic"),
      crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50);
    local.writeUInt16LE(20, 4);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(filename.length, 26);
    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(filename.length, 28);
    const footer = Buffer.alloc(22);
    footer.writeUInt32LE(0x06054b50);
    footer.writeUInt16LE(1, 8);
    footer.writeUInt16LE(1, 10);
    footer.writeUInt32LE(central.length + filename.length, 12);
    footer.writeUInt32LE(local.length + filename.length + data.length, 16);
    const archive = path.join(root, "synthetic.zip");
    await fs.writeFile(archive, Buffer.concat([local, filename, data, central, filename, footer]));
    let confirms = 0;
    const packages = createExtensionPackages({
      directory: path.join(root, "installed"),
      dialog: {
        showOpenDialog: async () => ({ filePaths: [archive] }),
        showMessageBox: async (_window, options) => {
          if (options.message.startsWith("Install ")) confirms++;
          return { response: 1 };
        },
      },
    });
    assert.equal((await packages.add({})).ok, false);
    assert.equal(confirms, 0);
    assert.deepEqual(packages.list(), []);
    await assert.rejects(fs.access(path.join(root, "escape.txt")));
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("Windows path-derived identity matches Chromium's UTF-16LE bytes, not UTF-8", () => {
  const { runtimeId } = require("../src/browserExtensionPackages");
  assert.equal(runtimeId("c:\\work\\extension", {}, "win32"), "fnjgbophkoigajajmpplkkaaojdiiiop");
});
