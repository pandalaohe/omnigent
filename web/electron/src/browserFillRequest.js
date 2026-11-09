/* eslint-disable no-await-in-loop -- Observe fill events and page identity in order until the deadline. */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

// This file stores permission metadata only. Bitwarden owns all credential values.
function createFillApprovals(file) {
  let rows = [];
  let revision = 0;
  try {
    const saved = JSON.parse(fs.readFileSync(file, "utf8"));
    if (Array.isArray(saved))
      rows = saved.filter((row) =>
        ["partition", "origin", "id", "label"].every((key) => typeof row?.[key] === "string"),
      );
  } catch {
    /* Missing or invalid permissions deny by default. */
  }
  const matches = (a, b) => a.partition === b.partition && a.origin === b.origin && a.id === b.id;
  function save(next) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const temporary = `${file}.${crypto.randomUUID()}.tmp`;
    try {
      fs.writeFileSync(temporary, JSON.stringify(next), { mode: 0o600 });
      fs.renameSync(temporary, file);
      rows = next;
      revision++;
    } finally {
      fs.rmSync(temporary, { force: true });
    }
  }
  return {
    revision: () => revision,
    has: (key) => rows.some((row) => matches(row, key)),
    allow: (key) => save([...rows.filter((row) => !matches(row, key)), key]),
    list: (partition) =>
      rows.filter((row) => row.partition === partition).map((row) => ({ ...row })),
    revoke: (key) => save(rows.filter((row) => !matches(row, key))),
  };
}

const ON_SCREEN = "This tab is not on screen. Ask the user to show it, then try again.";
const PAGE_CHANGED = "The page changed. Try again on the page you want to sign in to.";
const NO_LOGIN =
  "No saved login is available. Unlock Bitwarden and check the saved login for this site.";

function createFillRequest({
  extensions,
  registry,
  approvals,
  dialog,
  window,
  isAvailable = () => true,
  dialogTimeout = 40000,
  fillTimeout = 8000,
  busyPartitions = new Set(),
}) {
  async function fill(viewId) {
    let partition;
    let acquired = false;
    let frame;
    let contents;
    const key = `__omnigentFill_${crypto.randomUUID().replaceAll("-", "")}`;
    const run = async (code) => contents.executeJavaScriptInIsolatedWorld(999, [{ code }]);
    try {
      const entry = registry.get(viewId);
      if (
        !entry ||
        registry.activeConversationId() !== viewId ||
        registry.isSuppressed() ||
        window.isDestroyed() ||
        !window.isVisible()
      )
        return { ok: false, error: ON_SCREEN };
      partition = entry.partition;
      if (!isAvailable(partition) || busyPartitions.has(partition))
        return { ok: false, error: NO_LOGIN };
      busyPartitions.add(partition);
      acquired = true;
      contents = entry.view.webContents;
      frame = contents.mainFrame;
      const origin = frame.origin;
      const generation = entry.navGeneration;
      if (!registry.isFillOriginAllowed(viewId, origin))
        return { ok: false, error: "Saved logins require HTTPS or an approved internal site." };
      const current = () =>
        registry.get(viewId) === entry &&
        registry.activeConversationId() === viewId &&
        !registry.isSuppressed() &&
        !window.isDestroyed() &&
        window.isVisible() &&
        isAvailable(partition) &&
        entry.navGeneration === generation &&
        entry.view.webContents.mainFrame === frame &&
        frame.origin === origin;
      if (extensions.verifyFillIdentity && !(await extensions.verifyFillIdentity()))
        return { ok: false, error: NO_LOGIN };
      const items = extensions.listFillItems();
      if (!items.length) return { ok: false, error: NO_LOGIN };
      const signal = AbortSignal.timeout(dialogTimeout);
      let item = items[0];
      if (items.length > 1) {
        const choice = await dialog.showMessageBox(window, {
          message: `Choose a saved login for ${origin}`,
          buttons: ["Cancel", ...items.map((row) => row.label)],
          defaultId: 0,
          cancelId: 0,
          signal,
        });
        if (signal.aborted || choice.response < 1 || choice.response > items.length)
          return { ok: false, error: "Saved login fill was not allowed." };
        item = items[choice.response - 1];
      }
      const permission = { partition, origin, id: item.id, label: item.label };
      const revision = approvals.revision();
      let remember = false;
      if (!approvals.has(permission)) {
        const answer = await dialog.showMessageBox(window, {
          type: "question",
          message: `Allow this session to fill a saved login on ${origin}?`,
          detail: `Saved login: ${item.label}\nThe session can then click Log in. Your password stays in Bitwarden and the page.`,
          buttons: ["Don't Allow", "Allow"],
          defaultId: 0,
          cancelId: 0,
          checkboxLabel: "Always allow for this site",
          checkboxChecked: false,
          signal,
        });
        if (signal.aborted || answer.response !== 1)
          return { ok: false, error: "Saved login fill was not allowed." };
        remember = answer.checkboxChecked === true;
      }
      const valid = () =>
        current() &&
        approvals.revision() === revision &&
        extensions.listFillItems().some((row) => row.id === item.id);
      if (!valid()) return { ok: false, error: PAGE_CHANGED };
      await run(`(() => {
        const state = { observed: false };
        state.listener = event => {
          if (event.composedPath().some(node => node?.tagName === 'INPUT' && node.type === 'password'))
            state.observed = true;
        };
        globalThis[${JSON.stringify(key)}] = state;
        document.addEventListener('input', state.listener, true);
      })()`);
      if (!valid()) return { ok: false, error: PAGE_CHANGED };
      if (extensions.verifyFillIdentity && !(await extensions.verifyFillIdentity()))
        return { ok: false, error: NO_LOGIN };
      if (!valid()) return { ok: false, error: PAGE_CHANGED };
      if (remember) approvals.allow(permission);
      extensions.emitFill(entry.view.webContents, item.id);
      const deadline = Date.now() + fillTimeout;
      do {
        if (!current()) return { ok: false, error: PAGE_CHANGED };
        if (await run(`globalThis[${JSON.stringify(key)}]?.observed === true`)) {
          if (!current()) return { ok: false, error: PAGE_CHANGED };
          return { ok: true, filled: true };
        }
        await new Promise((resolve) => {
          setTimeout(resolve, 100);
        });
      } while (Date.now() < deadline);
      return { ok: false, error: "Fill was not confirmed. Check the page before trying again." };
    } catch {
      return { ok: false, error: "Could not fill the saved login. Check Bitwarden and try again." };
    } finally {
      if (frame)
        await run(`(() => {
        const state = globalThis[${JSON.stringify(key)}];
        if (state) document.removeEventListener('input', state.listener, true);
        delete globalThis[${JSON.stringify(key)}];
      })()`).catch(() => {});
      if (acquired) busyPartitions.delete(partition);
    }
  }
  return { fill };
}

module.exports = { createFillRequest, createFillApprovals };
