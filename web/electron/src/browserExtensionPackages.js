/* eslint-disable no-await-in-loop -- Bound memory while hashing packages and serialize registry writes. */
"use strict";
const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");

function validateManifest(manifest) {
  if (manifest.manifest_version !== 3)
    throw new Error("Only Manifest V3 extensions are supported.");
  if (typeof manifest.name !== "string" || typeof manifest.version !== "string")
    throw new Error("The extension needs a name and version.");
  for (const field of [
    "permissions",
    "optional_permissions",
    "host_permissions",
    "optional_host_permissions",
  ]) {
    if (
      manifest[field] !== undefined &&
      (!Array.isArray(manifest[field]) || manifest[field].some((p) => typeof p !== "string"))
    )
      throw new Error("Invalid extension permissions.");
    for (const permission of manifest[field] || [])
      if (field === "permissions" && ["nativeMessaging", "debugger"].includes(permission))
        throw new Error(`Permission ${permission} is not supported.`);
  }
  if (
    !/^(0|[1-9]\d{0,4})(\.(0|[1-9]\d{0,4})){0,3}$/.test(manifest.version) ||
    manifest.version.split(".").some((part) => Number(part) > 65535) ||
    manifest.version.split(".").every((part) => Number(part) === 0)
  )
    throw new Error("Invalid extension version.");
  if (manifest.key !== undefined && typeof manifest.key !== "string")
    throw new Error("Invalid extension key.");
  return manifest;
}
function validateZipEntry(entry) {
  const name = entry.fileName;
  if (
    name.includes("\\") ||
    name.includes("\0") ||
    name.startsWith("/") ||
    /^[a-z]:/i.test(name) ||
    name.split("/").includes("..") ||
    ((entry.externalFileAttributes >>> 16) & 0o170000) === 0o120000
  )
    throw new Error("Unsafe extension archive path.");
}
async function contentDigest(directory) {
  const hash = crypto.createHash("sha256");
  let size = 0,
    count = 0;
  async function walk(relative) {
    for (const name of (await fsp.readdir(path.join(directory, relative))).sort()) {
      const file = path.join(relative, name);
      const stat = await fsp.lstat(path.join(directory, file));
      if (stat.isSymbolicLink() || (!stat.isFile() && !stat.isDirectory()))
        throw new Error("Extension links and special files are not supported.");
      if (stat.isDirectory()) await walk(file);
      else {
        size += stat.size;
        if (++count > 20000 || size > 512 * 1024 * 1024)
          throw new Error("Extension package is too large.");
        hash.update(JSON.stringify(file.split(path.sep).join("/")) + "\0");
        hash.update(
          crypto
            .createHash("sha256")
            .update(await fsp.readFile(path.join(directory, file)))
            .digest(),
        );
      }
    }
  }
  await walk("");
  return hash.digest("hex");
}
async function packageDetails(directory) {
  const manifest = validateManifest(
    JSON.parse(await fsp.readFile(path.join(directory, "manifest.json"), "utf8")),
  );
  let name = manifest.name;
  const message = /^__MSG_(.+)__$/.exec(name);
  if (message && /^[a-zA-Z_-]+$/.test(manifest.default_locale || "")) {
    const messages = JSON.parse(
      await fsp.readFile(
        path.join(directory, "_locales", manifest.default_locale, "messages.json"),
        "utf8",
      ),
    );
    name = messages[message[1]]?.message || name;
  }
  return { manifest, name, digest: await contentDigest(directory) };
}
function requestExtensionPermissions(extension, request) {
  const declared = [
    ...(extension.manifest?.host_permissions || []),
    ...(extension.manifest?.optional_host_permissions || []),
  ];
  return Promise.resolve(
    !(request.permissions || []).some((permission) =>
      ["nativeMessaging", "debugger"].includes(permission),
    ) && (request.origins || []).every((origin) => declared.includes(origin)),
  );
}
function storageKey(directory, manifest, platform = process.platform) {
  let normalized = (platform === "win32" ? path.win32 : path).resolve(directory);
  if (platform === "win32") normalized = normalized[0].toUpperCase() + normalized.slice(1);
  return manifest.key
    ? Buffer.from(
        manifest.key.replace(/-----BEGIN [^-]+-----|-----END [^-]+-----|\s/g, ""),
        "base64",
      ).toString("base64")
    : Buffer.from(normalized, platform === "win32" ? "utf16le" : "utf8").toString("base64");
}
function runtimeId(directory, manifest, platform = process.platform) {
  const input = Buffer.from(storageKey(directory, manifest, platform), "base64");
  return crypto
    .createHash("sha256")
    .update(input)
    .digest("hex")
    .slice(0, 32)
    .replace(/[0-9a-f]/g, (digit) => String.fromCharCode(97 + parseInt(digit, 16)));
}
function createExtensionPackages({ directory, dialog, changed = async () => {} }) {
  const registry = path.join(directory, "extensions.json");
  let entries = [];
  if (fs.existsSync(registry)) {
    entries = JSON.parse(fs.readFileSync(registry, "utf8"));
    if (
      !Array.isArray(entries) ||
      entries.some(
        (entry) =>
          !/^[0-9a-f-]{36}$/.test(entry.id) ||
          typeof entry.enabled !== "boolean" ||
          typeof entry.name !== "string" ||
          typeof entry.version !== "string",
      ) ||
      new Set(entries.map((entry) => entry.id)).size !== entries.length
    )
      throw new Error("Invalid extension registry.");
  }
  let busy = false;
  const save = async () => {
    await fsp.mkdir(directory, { recursive: true });
    await fsp.writeFile(`${registry}.tmp`, JSON.stringify(entries));
    await fsp.rename(`${registry}.tmp`, registry);
  };
  const location = (id) => {
    if (!/^[0-9a-f-]{36}$/.test(id)) throw new Error("Unknown extension.");
    return path.join(directory, id);
  };
  const confirm = async (window, details) => {
    const { manifest, name } = details;
    const permissions = manifest.permissions || [];
    const optional = (manifest.optional_permissions || []).map((permission) =>
      ["nativeMessaging", "debugger"].includes(permission)
        ? `${permission} (declared, not granted)`
        : `${permission} (optional)`,
    );
    const hosts = [
      ...(manifest.host_permissions || []),
      ...(manifest.optional_host_permissions || []),
      ...(manifest.content_scripts || []).flatMap((script) => script.matches || []),
    ];
    const bitwarden = /\bbitwarden\b/i.test(name);
    const answer = await dialog.showMessageBox(window, {
      type: "warning",
      message: `Install ${name} ${manifest.version}?`,
      detail: `Permissions: ${permissions.join(", ") || "None"}\nOptional permissions: ${optional.join(", ") || "None"}\nHost access: ${hosts.join(", ") || "None"}\nThis extension can read pages open in the in-app browser, including pages an agent is using.${bitwarden ? "\nThe name does not prove the publisher. Only allow agent fill if you trust this package as Bitwarden." : ""}`,
      buttons: ["Cancel", "Install extension"],
      defaultId: 0,
      cancelId: 0,
      ...(bitwarden
        ? {
            checkboxLabel: "Allow agents to fill saved logins through this extension",
            checkboxChecked: false,
          }
        : {}),
    });
    if (answer.response !== 1) throw new Error("Installation cancelled.");
    return bitwarden && answer.checkboxChecked === true ? details.digest : null;
  };
  const records = async () => {
    const result = [],
      ids = new Set();
    for (const entry of entries.filter((item) => item.enabled)) {
      const installed = location(entry.id);
      const details = await packageDetails(installed);
      const id = runtimeId(installed, details.manifest);
      if (ids.has(id))
        throw new Error(
          "Two extensions have the same runtime ID. Remove one before enabling them.",
        );
      ids.add(id);
      if (entry.agentFillHash && entry.agentFillHash !== details.digest) {
        entry.agentFillHash = null;
        await save();
      }
      // Permission changes require a new native install confirmation.
      if (JSON.stringify(details.manifest) !== entry.confirmedManifest) continue;
      result.push({
        ...entry,
        directory: installed,
        manifest: details.manifest,
        runtimeId: id,
        agentFillAllowed:
          entry.agentFillHash === details.digest && /\bbitwarden\b/i.test(details.name),
      });
    }
    return result;
  };
  const mutate = async (action) => {
    if (busy) return { ok: false, error: "Wait for the extension operation to finish." };
    busy = true;
    try {
      await action();
      return { ok: true };
    } catch (error) {
      return { ok: false, error: error.message };
    } finally {
      busy = false;
    }
  };
  const review = async (window, entry) => {
    const details = await packageDetails(location(entry.id));
    entry.agentFillHash = await confirm(window, details);
    entry.confirmedManifest = JSON.stringify(details.manifest);
    entry.name = details.name;
    entry.version = details.manifest.version;
    await save();
    await changed();
  };
  return {
    records,
    list: () =>
      entries.map(({ id, name, version, enabled, runtimeId: extensionId }) => ({
        id,
        name,
        version,
        enabled,
        runtimeId: extensionId,
      })),
    add: (window) =>
      mutate(async () => {
        // Windows/Linux cannot select files and folders together in a native dialog.
        const kind = await dialog.showMessageBox(window, {
          message: "Choose extension package type",
          buttons: ["Cancel", "Zip package", "Unpacked folder"],
          defaultId: 0,
          cancelId: 0,
        });
        if (![1, 2].includes(kind.response)) throw new Error("Installation cancelled.");
        const selection = await dialog.showOpenDialog(window, {
          title: "Add browser extension",
          properties: [kind.response === 2 ? "openDirectory" : "openFile"],
          filters: [{ name: "Extension package", extensions: ["zip"] }],
        });
        if (selection.canceled || !selection.filePaths?.[0])
          throw new Error("Installation cancelled.");
        await fsp.mkdir(directory, { recursive: true });
        const scratch = await fsp.mkdtemp(path.join(directory, ".import-"));
        try {
          const source = selection.filePaths[0],
            staged = path.join(scratch, "extension");
          if ((await fsp.lstat(source)).isDirectory()) {
            await contentDigest(source);
            await fsp.cp(source, staged, { recursive: true, dereference: false });
          } else {
            if (path.extname(source).toLowerCase() !== ".zip")
              throw new Error("Choose an unpacked folder or .zip package.");
            let size = 0,
              count = 0;
            await require("extract-zip")(source, {
              dir: staged,
              onEntry: (entry) => {
                validateZipEntry(entry);
                size += entry.uncompressedSize;
                if (++count > 20000 || size > 512 * 1024 * 1024)
                  throw new Error("Extension package is too large.");
              },
            });
          }
          const details = await packageDetails(staged),
            id = crypto.randomUUID();
          const target = location(id),
            predictedId = runtimeId(target, details.manifest);
          for (const entry of entries) {
            if (
              runtimeId(location(entry.id), (await packageDetails(location(entry.id))).manifest) ===
              predictedId
            )
              throw new Error("An extension with this runtime ID is already installed.");
          }
          const agentFillHash = await confirm(window, details);
          await fsp.rename(staged, target);
          entries.push({
            id,
            name: details.name,
            version: details.manifest.version,
            enabled: true,
            runtimeId: predictedId,
            storageKey: storageKey(target, details.manifest),
            agentFillHash,
            confirmedManifest: JSON.stringify(details.manifest),
          });
          await save();
          try {
            await changed();
          } catch (error) {
            entries.find((item) => item.id === id).enabled = false;
            await save();
            await changed();
            throw error;
          }
        } finally {
          await fsp.rm(scratch, { recursive: true, force: true });
        }
      }),
    review: (window, id) =>
      mutate(async () => {
        const entry = entries.find((item) => item.id === id);
        if (!entry) throw new Error("Unknown extension.");
        await review(window, entry);
      }),
    setEnabled: (window, id, enabled) =>
      mutate(async () => {
        const entry = entries.find((item) => item.id === id);
        if (!entry || typeof enabled !== "boolean")
          throw new Error("Unknown extension or invalid state.");
        if (enabled) {
          const details = await packageDetails(location(id));
          if (JSON.stringify(details.manifest) !== entry.confirmedManifest)
            await review(window, entry);
        }
        const previous = entry.enabled;
        entry.enabled = enabled;
        await save();
        try {
          await changed();
        } catch (error) {
          entry.enabled = previous;
          await save();
          await changed();
          throw error;
        }
      }),
    remove: (window, id) =>
      mutate(async () => {
        const entry = entries.find((item) => item.id === id);
        if (!entry) throw new Error("Unknown extension.");
        const { response } = await dialog.showMessageBox(window, {
          type: "warning",
          message: `Remove ${entry.name}?`,
          detail:
            "Removes the extension and its local data on this desktop. This cannot be undone.",
          buttons: ["Cancel", "Remove extension"],
          defaultId: 0,
          cancelId: 0,
        });
        if (response !== 1) throw new Error("Removal cancelled.");
        entry.enabled = false;
        entry.agentFillHash = null;
        await save();
        await changed({
          remove: {
            runtimeId: entry.runtimeId,
            storageKey: entry.storageKey,
          },
        });
        await fsp.rm(location(id), { recursive: true, force: true });
        entries = entries.filter((item) => item.id !== id);
        await save();
      }),
    isBusy: () => busy,
  };
}
module.exports = {
  requestExtensionPermissions,
  validateManifest,
  validateZipEntry,
  contentDigest,
  packageDetails,
  storageKey,
  runtimeId,
  createExtensionPackages,
};
