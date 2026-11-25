// Desktop auto-update orchestration for the Omnigent Electron shell.
//
// Everything the shell needs to check for, download, and install its own
// updates via `electron-updater` lives here, behind a small factory API. The
// main process constructs one updater with its main-process dependencies
// injected (`app`, `BrowserWindow`, `ipcMain`, `dialog`, `nativeImage`, the
// `electron-updater` `autoUpdater`, settings load/save, and the trusted-sender
// checks), then wires four thin seams to it: startup `init()`, the "Updates"
// menu, the `omnigent:*update*` IPC surface (`registerIpc()`), and the
// before-quit install handoff (`quitAndInstallIfPending()`).
//
// Design notes preserved from the original inline implementation:
//   - Updates are gated on a usable feed: a packaged build, or a dev build with
//     `forceDevUpdateConfig` set (main.js derives it from !app.isPackaged, so
//     dev always uses dev-app-update.yml and a packaged build can never be
//     redirected to it). Manual actions in an unusable feed reject with a
//     friendly dev message.
//   - `autoDownload` is always off; downloads and installs are explicit and
//     each privileged IPC action re-confirms with a native dialog (a cached
//     hosting grant must not silently authorize an update action).
//   - Status is broadcast to every window AND cached (`getStatus()`), so a
//     freshly-loaded renderer can replay the latest state without a round trip.
//   - v1 is stable-only and rides electron-updater's default "latest" channel.

"use strict";

/** Update modes persisted in settings.json under `update_mode`. */
const UPDATE_MODES = new Set(["none", "manual", "start", "default"]);
const UPDATES_UNAVAILABLE_IN_DEV = "Desktop updates are unavailable in development builds.";
const EXTERNAL_CHECK_FAILED = "Couldn't check for updates.";
/** Background re-check cadence for the "default" mode (every 6 hours). */
const PERIODIC_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;
const EXTERNAL_FEED_TIMEOUT_MS = 10_000;
const EXTERNAL_REPO_SEGMENT_RE = /^[A-Za-z0-9._-]+$/;
const MAX_VERSION_FIELD_DIGITS = 64;
const DISTRIBUTION_VERSION_RE = /^v?(\d+)\.(\d+)\.(\d+)-wukong\.(\d+)$/;
const ATOM_NAMESPACE = "http://www.w3.org/2005/Atom";
const XML_NAME_RE = /^[A-Za-z_:][A-Za-z0-9._:-]*$/;
const MAX_FEED_REDIRECTS = 3;

/**
 * Validate and normalize an `owner/repo` string, rejecting dot segments and
 * malformed characters so URL construction cannot be traversed or spoofed.
 *
 * @param {unknown} value
 * @returns {string | null}
 */
function normalizeExternalReleaseRepo(value) {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  const parts = trimmed.split("/");
  if (parts.length !== 2) return null;
  const [owner, repo] = parts;
  if (
    !EXTERNAL_REPO_SEGMENT_RE.test(owner) ||
    !EXTERNAL_REPO_SEGMENT_RE.test(repo) ||
    owner === "." ||
    owner === ".." ||
    repo === "." ||
    repo === ".."
  ) {
    return null;
  }
  return `${owner}/${repo}`;
}

/**
 * Parse a stamped distribution version (`[v]X.Y.Z-wukong.N`) into an exact
 * BigInt tuple, or null when the string does not match or exceeds the field
 * digit bound.
 *
 * @param {unknown} value
 * @returns {[bigint, bigint, bigint, bigint] | null}
 */
function parseDistributionVersion(value) {
  if (typeof value !== "string") return null;
  const match = DISTRIBUTION_VERSION_RE.exec(value.trim());
  if (!match) return null;
  for (let i = 1; i <= 4; i += 1) {
    if (match[i].length > MAX_VERSION_FIELD_DIGITS) return null;
  }
  return [BigInt(match[1]), BigInt(match[2]), BigInt(match[3]), BigInt(match[4])];
}

/**
 * Strict numeric ordering for stamped distribution versions. Non-matching tags
 * return false rather than falling back to string inequality.
 *
 * @param {string} latest
 * @param {string} current
 * @returns {boolean}
 */
function isNewerDistributionVersion(latest, current) {
  const a = parseDistributionVersion(latest);
  const b = parseDistributionVersion(current);
  if (!a || !b) return false;
  for (let i = 0; i < 4; i += 1) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return false;
}

/**
 * Validate that a URL exactly matches `https://github.com/<owner>/<repo>/releases.atom`
 * with no credentials, explicit port, query, fragment, dot segments, or encoded
 * path separators.
 *
 * @param {unknown} rawUrl
 * @param {string} repo
 * @returns {string | null}
 */
function validateExternalFeedUrl(rawUrl, repo) {
  const normalizedRepo = normalizeExternalReleaseRepo(repo);
  if (!normalizedRepo || typeof rawUrl !== "string") return null;
  if (rawUrl.includes("\\") || /%(?:2f|5c)/i.test(rawUrl)) return null;
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }
  const canonical = `https://github.com/${normalizedRepo}/releases.atom`;
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname !== "github.com" ||
    parsed.port !== "" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.search !== "" ||
    parsed.hash !== "" ||
    parsed.href !== canonical ||
    rawUrl !== canonical
  ) {
    return null;
  }
  return canonical;
}

/**
 * Validate a release tag URL against the expected GitHub repository and strict
 * Wukong tag format. Returns the parsed version, tag, and canonical URL, or
 * null if anything (origin, repo, credentials, port, query, fragment, dot
 * segments, encoded separators, or tag format) does not match.
 *
 * @param {unknown} rawUrl
 * @param {string} repo
 * @returns {{ version: string, tag: string, releaseUrl: string } | null}
 */
function parseExpectedReleaseTagUrl(rawUrl, repo) {
  const normalizedRepo = normalizeExternalReleaseRepo(repo);
  if (!normalizedRepo || typeof rawUrl !== "string") return null;
  if (rawUrl.includes("\\") || /%(?:2f|5c)/i.test(rawUrl)) return null;
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return null;
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname !== "github.com" ||
    parsed.port !== "" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.search !== "" ||
    parsed.hash !== ""
  ) {
    return null;
  }
  const [expectedOwner, expectedRepo] = normalizedRepo.split("/");
  const segments = parsed.pathname.split("/");
  if (
    segments.length !== 6 ||
    segments[0] !== "" ||
    segments[1] !== expectedOwner ||
    segments[2] !== expectedRepo ||
    segments[3] !== "releases" ||
    segments[4] !== "tag"
  ) {
    return null;
  }
  const rawTag = segments[5];
  if (!parseDistributionVersion(rawTag)) return null;
  const canonical = `https://github.com/${expectedOwner}/${expectedRepo}/releases/tag/${rawTag}`;
  if (parsed.href !== canonical || rawUrl !== canonical) return null;
  const version = rawTag.startsWith("v") ? rawTag.slice(1) : rawTag;
  return { version, tag: rawTag, releaseUrl: canonical };
}

function decodeXmlEntities(text) {
  return text.replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[A-Za-z]+);/g, (_match, entity) => {
    if (entity === "amp") return "&";
    if (entity === "lt") return "<";
    if (entity === "gt") return ">";
    if (entity === "quot") return '"';
    if (entity === "apos") return "'";
    if (entity.startsWith("#x") || entity.startsWith("#X")) {
      const code = Number.parseInt(entity.slice(2), 16);
      if (!Number.isFinite(code) || code <= 0 || code > 0x10ffff) {
        throw new Error("Invalid XML character reference");
      }
      return String.fromCodePoint(code);
    }
    if (entity.startsWith("#")) {
      const code = Number.parseInt(entity.slice(1), 10);
      if (!Number.isFinite(code) || code <= 0 || code > 0x10ffff) {
        throw new Error("Invalid XML character reference");
      }
      return String.fromCodePoint(code);
    }
    throw new Error(`Unsupported XML entity &${entity};`);
  });
}

function assertValidXmlEntities(text) {
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === "&") {
      const semi = text.indexOf(";", i + 1);
      if (semi === -1) throw new Error("Unterminated XML entity");
      decodeXmlEntities(text.slice(i, semi + 1));
      i = semi;
    }
  }
}

/**
 * Minimal strict XML 1.0 well-formedness parser for Atom documents. Rejects
 * truncated documents, mismatched tags, unclosed attributes/comments/CDATA,
 * DOCTYPE declarations, duplicate attributes, and trailing garbage.
 *
 * @param {string} xmlText
 * @returns {{ name: string, attrs: Record<string, string>, children: Array<any> }}
 */
function parseStrictXmlDocument(xmlText) {
  if (typeof xmlText !== "string") throw new Error("XML document must be a string");
  const src = xmlText.startsWith("\uFEFF") ? xmlText.slice(1) : xmlText;
  const len = src.length;
  let pos = 0;

  function skipWs() {
    while (
      pos < len &&
      (src[pos] === " " || src[pos] === "\t" || src[pos] === "\r" || src[pos] === "\n")
    ) {
      pos += 1;
    }
  }

  function skipMisc() {
    while (pos < len) {
      skipWs();
      if (src.startsWith("<!--", pos)) {
        const end = src.indexOf("-->", pos + 4);
        if (end === -1 || src.slice(pos + 4, end).includes("--")) {
          throw new Error("Malformed XML comment");
        }
        pos = end + 3;
      } else if (src.startsWith("<?", pos)) {
        const end = src.indexOf("?>", pos + 2);
        if (end === -1) throw new Error("Unterminated XML processing instruction");
        pos = end + 2;
      } else {
        break;
      }
    }
  }

  function readName() {
    const start = pos;
    while (
      pos < len &&
      src[pos] !== " " &&
      src[pos] !== "\t" &&
      src[pos] !== "\r" &&
      src[pos] !== "\n" &&
      src[pos] !== ">" &&
      src[pos] !== "/" &&
      src[pos] !== "="
    ) {
      pos += 1;
    }
    const name = src.slice(start, pos);
    if (!XML_NAME_RE.test(name)) throw new Error(`Invalid XML name: ${name}`);
    return name;
  }

  skipWs();
  if (src.startsWith("<?xml", pos)) {
    const end = src.indexOf("?>", pos + 5);
    if (end === -1) throw new Error("Unterminated XML declaration");
    pos = end + 2;
  }
  skipMisc();
  if (pos >= len || src[pos] !== "<") throw new Error("Missing XML root element");
  if (src.startsWith("<!DOCTYPE", pos) || src.startsWith("<!doctype", pos)) {
    throw new Error("DOCTYPE declarations are not allowed");
  }

  let root = null;
  const stack = [];

  while (pos < len) {
    if (src[pos] === "<") {
      if (src.startsWith("<!--", pos)) {
        const end = src.indexOf("-->", pos + 4);
        if (end === -1 || src.slice(pos + 4, end).includes("--")) {
          throw new Error("Malformed XML comment");
        }
        pos = end + 3;
        continue;
      }
      if (src.startsWith("<![CDATA[", pos)) {
        if (stack.length === 0) throw new Error("CDATA outside root element");
        const end = src.indexOf("]]>", pos + 9);
        if (end === -1) throw new Error("Unterminated CDATA section");
        pos = end + 3;
        continue;
      }
      if (src.startsWith("<?", pos)) {
        const end = src.indexOf("?>", pos + 2);
        if (end === -1) throw new Error("Unterminated XML processing instruction");
        pos = end + 2;
        continue;
      }
      if (src.startsWith("<!", pos)) {
        throw new Error("Unsupported XML markup declaration");
      }
      if (src.startsWith("</", pos)) {
        pos += 2;
        const closeName = readName();
        skipWs();
        if (src[pos] !== ">") throw new Error("Malformed XML closing tag");
        pos += 1;
        const open = stack.pop();
        if (!open || open.name !== closeName) {
          throw new Error(`Mismatched XML closing tag </${closeName}>`);
        }
        if (stack.length === 0) {
          skipMisc();
          if (pos !== len) throw new Error("Trailing content after XML root element");
          break;
        }
        continue;
      }
      if (root && stack.length === 0) {
        throw new Error("Multiple XML root elements");
      }
      pos += 1;
      const name = readName();
      const attrs = Object.create(null);
      let selfClosing = false;
      while (pos < len) {
        const beforeWs = pos;
        skipWs();
        if (src[pos] === ">") {
          pos += 1;
          break;
        }
        if (src.startsWith("/>", pos)) {
          selfClosing = true;
          pos += 2;
          break;
        }
        if (pos === beforeWs) throw new Error("Missing whitespace before XML attribute");
        const attrName = readName();
        skipWs();
        if (src[pos] !== "=") throw new Error("Missing '=' in XML attribute");
        pos += 1;
        skipWs();
        const quote = src[pos];
        if (quote !== '"' && quote !== "'") throw new Error("Unquoted XML attribute value");
        pos += 1;
        const valStart = pos;
        const valEnd = src.indexOf(quote, valStart);
        if (valEnd === -1) throw new Error("Unterminated XML attribute value");
        const rawVal = src.slice(valStart, valEnd);
        if (rawVal.includes("<")) throw new Error("Unescaped '<' in XML attribute value");
        if (Object.hasOwn(attrs, attrName)) {
          throw new Error(`Duplicate XML attribute: ${attrName}`);
        }
        attrs[attrName] = decodeXmlEntities(rawVal);
        pos = valEnd + 1;
      }
      if (pos > len || (!selfClosing && src[pos - 1] !== ">")) {
        throw new Error("Unterminated XML start tag");
      }
      const node = { name, attrs, children: [] };
      if (stack.length > 0) {
        stack[stack.length - 1].children.push(node);
      } else {
        root = node;
      }
      if (!selfClosing) {
        stack.push(node);
      } else if (stack.length === 0) {
        skipMisc();
        if (pos !== len) throw new Error("Trailing content after XML root element");
        break;
      }
    } else {
      const nextTag = src.indexOf("<", pos);
      const textEnd = nextTag === -1 ? len : nextTag;
      const chunk = src.slice(pos, textEnd);
      if (stack.length === 0) {
        if (chunk.trim() !== "") throw new Error("Unexpected text outside XML root element");
      } else {
        if (chunk.includes("]]>")) throw new Error("Illegal ']]>' in XML text");
        assertValidXmlEntities(chunk);
      }
      pos = textEnd;
    }
  }

  if (!root || stack.length > 0) {
    throw new Error("Truncated XML document");
  }
  return root;
}

function xmlLocalName(name) {
  const colon = name.indexOf(":");
  return colon === -1 ? name : name.slice(colon + 1);
}

function resolveElementNamespace(node, inheritedNamespaces) {
  const namespaces = { ...inheritedNamespaces };
  for (const [key, value] of Object.entries(node.attrs)) {
    if (key === "xmlns") namespaces[""] = value;
    else if (key.startsWith("xmlns:")) namespaces[key.slice(6)] = value;
  }
  const colon = node.name.indexOf(":");
  const prefix = colon === -1 ? "" : node.name.slice(0, colon);
  return { ns: namespaces[prefix] ?? "", local: xmlLocalName(node.name), namespaces };
}

/**
 * Extract the numerically highest `-wukong.N` release from a GitHub
 * `releases.atom` XML document, strictly bound to `repo`. Throws on malformed
 * XML or non-Atom documents; skips individual entries whose alternate link or
 * tag does not match.
 *
 * @param {string} xmlText
 * @param {string} repo `owner/repo`
 * @returns {{ version: string, tag: string, releaseUrl: string } | null}
 */
function parseReleasesAtom(xmlText, repo) {
  const normalizedRepo = normalizeExternalReleaseRepo(repo);
  if (!normalizedRepo) {
    throw new Error("Invalid external release repository");
  }
  const root = parseStrictXmlDocument(xmlText);
  const rootInfo = resolveElementNamespace(root, Object.create(null));
  if (rootInfo.local !== "feed" || (rootInfo.ns !== "" && rootInfo.ns !== ATOM_NAMESPACE)) {
    throw new Error("Atom feed root element must be <feed>");
  }

  let best = null;
  for (const entry of root.children) {
    const entryInfo = resolveElementNamespace(entry, rootInfo.namespaces);
    if (entryInfo.local !== "entry" || (entryInfo.ns !== "" && entryInfo.ns !== ATOM_NAMESPACE)) {
      continue;
    }
    let matched = null;
    for (const child of entry.children) {
      const childInfo = resolveElementNamespace(child, entryInfo.namespaces);
      if (childInfo.local !== "link" || (childInfo.ns !== "" && childInfo.ns !== ATOM_NAMESPACE)) {
        continue;
      }
      const rel = child.attrs.rel;
      if (rel !== undefined && rel !== "alternate") continue;
      const candidate = parseExpectedReleaseTagUrl(child.attrs.href, normalizedRepo);
      if (candidate) {
        matched = candidate;
        break;
      }
    }
    if (matched && (!best || isNewerDistributionVersion(matched.version, best.version))) {
      best = matched;
    }
  }
  return best;
}

/**
 * Default HTTP getter for `releases.atom` using Node/Electron's built-in fetch.
 * Refuses redirects to any other origin or repository.
 *
 * @param {string} url
 * @param {string} [expectedRepo]
 * @param {number} [hop]
 * @returns {Promise<{ status: number, text: string, url?: string }>}
 */
async function defaultFetchFeed(url, expectedRepo, hop = 0) {
  const resp = await fetch(url, {
    headers: { Accept: "application/atom+xml, application/xml, text/xml" },
    redirect: "manual",
    signal: AbortSignal.timeout(EXTERNAL_FEED_TIMEOUT_MS),
  });
  if (resp.status >= 300 && resp.status < 400) {
    const location = resp.headers?.get?.("location");
    if (!location || hop >= MAX_FEED_REDIRECTS) {
      throw new Error(`Unexpected redirect (${resp.status}) while checking release feed`);
    }
    let nextUrl;
    try {
      nextUrl = new URL(location, url).href;
    } catch {
      throw new Error("Malformed redirect URL while checking release feed");
    }
    if (!expectedRepo || !validateExternalFeedUrl(nextUrl, expectedRepo)) {
      throw new Error("Refused redirect to unexpected origin or repository");
    }
    return defaultFetchFeed(nextUrl, expectedRepo, hop + 1);
  }
  return {
    status: resp.status,
    text: await resp.text(),
    url: typeof resp.url === "string" && resp.url ? resp.url : url,
  };
}

/**
 * Normalize the raw settings object into the update config the renderer sees.
 * Unknown/missing values fall back to the safe defaults (auto-checking on,
 * auto-install on, nothing skipped).
 *
 * @param {Record<string, unknown>} settings
 * @returns {{ mode: string, autoInstall: boolean, skippedVersion: string | null }}
 */
function normalizeUpdateConfig(settings) {
  const mode = UPDATE_MODES.has(settings.update_mode) ? settings.update_mode : "default";
  return {
    mode,
    autoInstall:
      typeof settings.update_auto_install === "boolean" ? settings.update_auto_install : true,
    skippedVersion:
      typeof settings.update_skipped_version === "string" ? settings.update_skipped_version : null,
  };
}

/**
 * True when an updater error message looks like a code-signing / integrity
 * failure (as opposed to a transient network/404). Those are surfaced to the
 * user as `error-security` even outside a manual check.
 *
 * @param {string} message
 * @returns {boolean}
 */
function isUpdateSecurityError(message) {
  return /signature|sha512|checksum|not signed|code sign/i.test(message);
}

/**
 * Build the desktop updater. All host dependencies are injected so the module
 * carries no Electron/`main.js` globals and is directly unit-testable with
 * fakes.
 *
 * @param {object} deps
 * @param {import("electron").App} deps.app
 * @param {typeof import("electron").BrowserWindow} deps.BrowserWindow
 * @param {import("electron").IpcMain} deps.ipcMain
 * @param {import("electron").Dialog} deps.dialog
 * @param {import("electron").NativeImage} deps.nativeImage The `nativeImage` module.
 * @param {import("electron-updater").AppUpdater} deps.autoUpdater
 * @param {() => Record<string, unknown>} deps.loadSettings
 * @param {(settings: Record<string, unknown>) => void} deps.saveSettings
 * @param {(event: Electron.IpcMainInvokeEvent) => boolean} deps.isPinnedOriginSender
 *   Whether an IPC call came from a connected server's own pinned page.
 * @param {(win: Electron.BrowserWindow | null | undefined) => string | null} deps.pinnedOrigin
 *   The origin a window is pinned to (used for the consent dialog copy).
 * @param {string} deps.iconPath Absolute path to the app icon PNG.
 * @param {boolean} [deps.forceDevUpdateConfig] Enable the development update
 *   config in an unpackaged build (main.js sets this from !app.isPackaged).
 * @param {boolean} [deps.updatesEnabled] Disable the production feed for dev packages.
 * @param {() => string} [deps.getCurrentVersion] Version shown in update UI.
 *   Defaults to Electron's real app version.
 * @param {(installReady: boolean) => void} [deps.onInstallReadyChange]
 *   Called when a downloaded update becomes ready or stops being ready.
 * @returns {{
 *   getConfig: () => { mode: string, autoInstall: boolean, skippedVersion: string | null },
 *   setConfig: (patch?: object) => { mode: string, autoInstall: boolean, skippedVersion: string | null },
 *   getStatus: () => object,
 *   init: () => void,
 *   checkForUpdates: (opts?: { manual?: boolean }) => Promise<void>,
 *   installUpdateNow: () => boolean,
 *   registerIpc: () => void,
 *   quitAndInstallIfPending: () => boolean,
 *   readonly installPending: boolean,
 * }}
 */
function createDesktopUpdater({
  app,
  BrowserWindow,
  ipcMain,
  dialog,
  nativeImage,
  autoUpdater,
  loadSettings,
  saveSettings,
  isPinnedOriginSender,
  pinnedOrigin,
  iconPath,
  forceDevUpdateConfig = false,
  updatesEnabled = true,
  getCurrentVersion = () => app.getVersion(),
  onInstallReadyChange = () => {},
  externalReleaseRepo = null,
  fetchFeed = defaultFetchFeed,
  openExternal = null,
}) {
  const validExternalRepo = normalizeExternalReleaseRepo(externalReleaseRepo);
  const isExternalMode = validExternalRepo !== null;
  let updateCheckTimer = null;
  let currentUpdateStatus = { state: "idle" };
  let installPending = false;
  // A user-initiated ("Check for Updates…") check in flight — lets the error
  // handler surface a failure to the user only when they explicitly asked.
  let manualCheckInFlight = false;
  let statusBeforeCheck = null;
  // Single in-flight external-feed check shared by startup/periodic and manual callers.
  let externalCheckInFlight = null;

  function getConfig() {
    const cfg = normalizeUpdateConfig(loadSettings());
    return isExternalMode ? { ...cfg, deliveryMode: "manual-download" } : cfg;
  }

  function setConfig(patch = {}) {
    const settings = loadSettings();
    const next = { ...settings };
    if (Object.hasOwn(patch, "mode") && UPDATE_MODES.has(patch.mode)) {
      next.update_mode = patch.mode;
    }
    if (Object.hasOwn(patch, "autoInstall") && typeof patch.autoInstall === "boolean") {
      next.update_auto_install = patch.autoInstall;
    }
    if (Object.hasOwn(patch, "skippedVersion")) {
      next.update_skipped_version =
        typeof patch.skippedVersion === "string" ? patch.skippedVersion : null;
    }
    saveSettings(next);
    return getConfig();
  }

  function getStatus() {
    return currentUpdateStatus;
  }

  function broadcast(status) {
    const wasInstallReady = currentUpdateStatus.state === "downloaded";
    currentUpdateStatus = status;
    const installReady = status.state === "downloaded";
    if (installReady !== wasInstallReady) onInstallReadyChange(installReady);
    for (const win of BrowserWindow.getAllWindows()) {
      if (win.isDestroyed()) continue;
      try {
        win.webContents.send("omnigent:update-status", status);
      } catch {
        // Window torn down between enumeration and send; ignore.
      }
    }
  }

  function canUseFeed() {
    if (isExternalMode) {
      return updatesEnabled && (app.isPackaged || forceDevUpdateConfig === true);
    }
    return updatesEnabled && (app.isPackaged || autoUpdater.forceDevUpdateConfig === true);
  }

  function reportUnavailableInDev() {
    broadcast({ state: "idle", lastError: UPDATES_UNAVAILABLE_IN_DEV });
  }

  function unavailableInDevError() {
    return new Error(UPDATES_UNAVAILABLE_IN_DEV);
  }

  function applyConfig(cfg) {
    if (updateCheckTimer) {
      clearInterval(updateCheckTimer);
      updateCheckTimer = null;
    }
    if (!isExternalMode) {
      autoUpdater.autoDownload = false;
      autoUpdater.autoInstallOnAppQuit = cfg.autoInstall;
    }
    // v1 is stable-only and rides electron-updater's default "latest" channel.
    if (!canUseFeed() || cfg.mode === "none") return;
    if (cfg.mode === "manual") return;
    checkForUpdates().catch(() => {});
    if (cfg.mode === "default") {
      updateCheckTimer = setInterval(
        () => checkForUpdates().catch(() => {}),
        PERIODIC_CHECK_INTERVAL_MS,
      );
      // Don't let the 6-hourly re-check keep the Node event loop alive at quit
      // (a ref'd interval can leave the main process lingering after the
      // windows close on some platforms).
      if (typeof updateCheckTimer.unref === "function") updateCheckTimer.unref();
    }
  }

  function init() {
    const cfg = getConfig();
    if (isExternalMode) {
      applyConfig(cfg);
      return;
    }
    if (forceDevUpdateConfig) {
      autoUpdater.forceDevUpdateConfig = true;
    }
    autoUpdater.on("checking-for-update", () => {
      if (
        currentUpdateStatus.state !== "downloading" &&
        currentUpdateStatus.state !== "downloaded"
      ) {
        broadcast({ state: "checking" });
      }
    });
    autoUpdater.on("update-available", (info) => {
      manualCheckInFlight = false;
      if (
        currentUpdateStatus.state !== "downloading" &&
        currentUpdateStatus.state !== "downloaded"
      ) {
        broadcast({ state: "available", currentVersion: getCurrentVersion(), info });
      }
    });
    autoUpdater.on("update-not-available", () => {
      manualCheckInFlight = false;
      if (
        currentUpdateStatus.state !== "downloading" &&
        currentUpdateStatus.state !== "downloaded"
      ) {
        broadcast({ state: "none" });
      }
    });
    autoUpdater.on("download-progress", (progress) =>
      broadcast({ state: "downloading", progress }),
    );
    autoUpdater.on("update-downloaded", (info) =>
      broadcast({ state: "downloaded", currentVersion: getCurrentVersion(), info }),
    );
    autoUpdater.on("error", (err) => {
      const msg = String(err?.message ?? err);
      const isSecurity = isUpdateSecurityError(msg);
      const wasManualCheck = manualCheckInFlight;
      manualCheckInFlight = false;
      console[isSecurity ? "error" : "warn"]("[omnigent] update error:", msg);
      if (isSecurity || wasManualCheck) {
        broadcast({ state: isSecurity ? "error-security" : "idle", lastError: msg });
      } else if (currentUpdateStatus.state === "checking") {
        broadcast(statusBeforeCheck ?? { state: "idle" });
      }
    });
    applyConfig(cfg);
  }

  function checkExternalRelease({ manual = false } = {}) {
    if (externalCheckInFlight) {
      if (manual && !externalCheckInFlight.manual) {
        externalCheckInFlight.manual = true;
        broadcast({ state: "checking" });
      }
      return externalCheckInFlight.promise;
    }
    const run = { manual: Boolean(manual), promise: null };
    if (run.manual) broadcast({ state: "checking" });
    const feedUrl = `https://github.com/${validExternalRepo}/releases.atom`;
    run.promise = (async () => {
      try {
        const resp = await fetchFeed(feedUrl, validExternalRepo);
        if (resp && resp.status >= 300 && resp.status < 400) {
          throw new Error(`Unexpected redirect (${resp.status}) while checking release feed`);
        }
        if (
          resp &&
          typeof resp.url === "string" &&
          resp.url !== "" &&
          !validateExternalFeedUrl(resp.url, validExternalRepo)
        ) {
          throw new Error("Feed response resolved to an unexpected origin or repository");
        }
        if (resp && resp.status === 404) {
          if (run.manual) {
            broadcast({ state: "none", manualOnly: true });
          }
          return;
        }
        if (!resp || resp.status !== 200) {
          throw new Error(`Unexpected status ${resp?.status ?? "unknown"}`);
        }
        const latest = parseReleasesAtom(resp.text, validExternalRepo);
        const current = getCurrentVersion();
        if (latest && isNewerDistributionVersion(latest.version, current)) {
          broadcast({
            state: "available",
            currentVersion: current,
            info: {
              version: latest.version,
              downloadUrl: latest.releaseUrl,
            },
          });
        } else if (run.manual) {
          broadcast({ state: "none", manualOnly: true });
        }
      } catch (err) {
        const msg = String(err?.message ?? err);
        console.warn("[omnigent] update error:", msg);
        if (run.manual) {
          broadcast({ state: "idle", manualOnly: true, lastError: EXTERNAL_CHECK_FAILED });
        }
      } finally {
        if (externalCheckInFlight === run) {
          externalCheckInFlight = null;
        }
      }
    })();
    externalCheckInFlight = run;
    return run.promise;
  }

  function checkForUpdates({ manual = false } = {}) {
    if (currentUpdateStatus.state === "downloading" || currentUpdateStatus.state === "downloaded") {
      return Promise.resolve();
    }
    if (!canUseFeed()) {
      if (manual) reportUnavailableInDev();
      return Promise.reject(unavailableInDevError());
    }
    if (isExternalMode) {
      return checkExternalRelease({ manual });
    }
    statusBeforeCheck = currentUpdateStatus;
    if (manual) manualCheckInFlight = true;
    return autoUpdater
      .checkForUpdates()
      .then(() => {
        if (manual) manualCheckInFlight = false;
        statusBeforeCheck = null;
        return undefined;
      })
      .catch((err) => {
        const msg = String(err?.message ?? err);
        if (manualCheckInFlight) {
          manualCheckInFlight = false;
          broadcast({
            state: isUpdateSecurityError(msg) ? "error-security" : "idle",
            lastError: msg,
          });
        } else if (!manual && currentUpdateStatus.state === "checking") {
          broadcast(statusBeforeCheck ?? { state: "idle" });
        }
        statusBeforeCheck = null;
        throw err;
      });
  }

  function installUpdateNow() {
    if (isExternalMode) return false;
    if (!canUseFeed()) {
      reportUnavailableInDev();
      return false;
    }
    if (currentUpdateStatus.state !== "downloaded") return false;
    installPending = true;
    app.quit();
    return true;
  }

  /**
   * Start downloading the available update. Feed-gated like the other actions.
   * Used by the trusted shell update overlay (the server-page IPC calls
   * `autoUpdater.downloadUpdate()` inline behind its own consent dialog).
   *
   * @returns {Promise<void>}
   */
  function downloadUpdate() {
    if (!canUseFeed()) {
      reportUnavailableInDev();
      return Promise.reject(unavailableInDevError());
    }
    if (isExternalMode) {
      const releaseUrl = currentUpdateStatus.info?.downloadUrl;
      const checked = parseExpectedReleaseTagUrl(releaseUrl, validExternalRepo);
      if (
        currentUpdateStatus.state !== "available" ||
        !checked ||
        typeof openExternal !== "function"
      ) {
        return Promise.reject(new Error("No release download link is available."));
      }
      return Promise.resolve(openExternal(checked.releaseUrl)).then(() => undefined);
    }
    // Enter the progress state immediately instead of waiting for the first
    // provider event. Fast downloads and slow-loading About windows then still
    // get a truthful starting state from the cached updater snapshot.
    broadcast({ state: "downloading", progress: { percent: 0 } });
    return autoUpdater
      .downloadUpdate()
      .then(() => undefined)
      .catch((err) => {
        const message = String(err?.message ?? err);
        broadcast({
          state: isUpdateSecurityError(message) ? "error-security" : "idle",
          lastError: message,
        });
        throw err;
      });
  }

  /**
   * Native, per-action consent for a privileged update control. The IPC gate
   * only proves the call came FROM the pinned server's page — not that the USER
   * asked for it — so download/install/config changes re-confirm here. Returns
   * true only when the user picks "Allow Once".
   */
  async function confirmControl(win, action) {
    if (!win) return false;
    const pinned = pinnedOrigin(win);
    if (!pinned) return false;

    let host = pinned;
    try {
      host = new URL(pinned).host;
    } catch {
      // Keep the full origin string if it somehow doesn't parse.
    }

    const copy = {
      download: {
        message: "Download an Omnigent update?",
        detail:
          `${host} wants to download a desktop update for this Omnigent app.\n\n` +
          `Only allow servers you trust.`,
      },
      install: {
        message: "Restart Omnigent to install an update?",
        detail:
          `${host} wants to restart Omnigent and install the downloaded desktop update.\n\n` +
          `Only allow servers you trust.`,
      },
      config: {
        message: "Change Omnigent update settings?",
        detail:
          `${host} wants to change how this Omnigent app checks for and installs updates.\n\n` +
          `Only allow servers you trust.`,
      },
    }[action];
    if (!copy) return false;

    const icon = nativeImage.createFromPath(iconPath);
    const { response } = await dialog.showMessageBox(win, {
      type: "warning",
      icon: icon.isEmpty() ? undefined : icon,
      title: "Omnigent",
      message: copy.message,
      detail: copy.detail,
      buttons: ["Don't Allow", "Allow Once"],
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    });
    return response === 1;
  }

  function registerIpc() {
    ipcMain.handle("omnigent:get-update-config", (event) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("get-update-config is only available to a connected server page");
      }
      return getConfig();
    });

    ipcMain.handle("omnigent:get-update-status", (event) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("get-update-status is only available to a connected server page");
      }
      return currentUpdateStatus;
    });

    ipcMain.handle("omnigent:update-check", async (event) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("update-check is only available to a connected server page");
      }
      await checkForUpdates({ manual: true });
    });

    ipcMain.handle("omnigent:update-download", async (event) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("update-download is only available to a connected server page");
      }
      if (!canUseFeed()) {
        reportUnavailableInDev();
        throw unavailableInDevError();
      }
      const win = BrowserWindow.fromWebContents(event.sender);
      if (!(await confirmControl(win, "download"))) {
        throw new Error("Update download wasn't approved for this server.");
      }
      await downloadUpdate();
    });

    ipcMain.handle("omnigent:update-install", async (event) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("update-install is only available to a connected server page");
      }
      if (!canUseFeed()) {
        reportUnavailableInDev();
        throw unavailableInDevError();
      }
      const win = BrowserWindow.fromWebContents(event.sender);
      if (!(await confirmControl(win, "install"))) {
        throw new Error("Update install wasn't approved for this server.");
      }
      if (!installUpdateNow()) {
        throw new Error("No downloaded update is ready to install.");
      }
    });

    ipcMain.handle("omnigent:set-update-config", async (event, patch) => {
      if (!isPinnedOriginSender(event)) {
        throw new Error("set-update-config is only available to a connected server page");
      }
      if (!canUseFeed()) {
        reportUnavailableInDev();
        throw unavailableInDevError();
      }
      const win = BrowserWindow.fromWebContents(event.sender);
      if (!(await confirmControl(win, "config"))) {
        throw new Error("Update settings change wasn't approved for this server.");
      }
      const cfg = setConfig(patch);
      applyConfig(cfg);
      return cfg;
    });
  }

  /**
   * Complete a deferred install once the app's own before-quit cleanup has
   * finished. No-op unless a user-approved install is pending.
   *
   * @returns {boolean} Whether a pending install was handed off.
   */
  function quitAndInstallIfPending() {
    if (isExternalMode || !installPending) return false;
    autoUpdater.quitAndInstall(false, true);
    return true;
  }

  return {
    getConfig,
    setConfig,
    getStatus,
    init,
    checkForUpdates,
    downloadUpdate,
    installUpdateNow,
    registerIpc,
    quitAndInstallIfPending,
    get installPending() {
      return installPending;
    },
  };
}

module.exports = {
  createDesktopUpdater,
  normalizeUpdateConfig,
  isUpdateSecurityError,
  normalizeExternalReleaseRepo,
  parseDistributionVersion,
  isNewerDistributionVersion,
  parseExpectedReleaseTagUrl,
  validateExternalFeedUrl,
  parseReleasesAtom,
  UPDATE_MODES,
  UPDATES_UNAVAILABLE_IN_DEV,
};
