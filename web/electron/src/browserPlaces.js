"use strict";

const fs = require("node:fs");
const path = require("node:path");

function placeUrl(value) {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    url.username = "";
    url.password = "";
    return url.href;
  } catch {
    return null;
  }
}

class PublicPlacesError extends Error {}
const limit = (value, fallback, max) =>
  Number.isFinite(value) ? Math.max(0, Math.min(max, Math.floor(value))) : fallback;
function normalizeRetention({ days, entries }) {
  return { days: limit(days, 90, 3650), entries: limit(entries, 5000, 100000) };
}
function applyRetention(rows, limits, now) {
  const { days, entries } = normalizeRetention(limits);
  const retained =
    days === 0 || entries === 0
      ? []
      : rows
          .filter((row) => row.visitedAt >= now - days * 86400000)
          .sort((a, b) => b.visitedAt - a.visitedAt || a.url.localeCompare(b.url))
          .slice(0, entries);
  return { retained, removed: rows.length - retained.length };
}
/** Desktop-local places. All writes replace a complete JSON file atomically. */
function createBrowserPlaces({
  directory,
  settings = () => ({}),
  saveSettings = () => {},
  now = Date.now,
}) {
  const file = (kind) => path.join(directory(), `browser-${kind}.json`);
  function read(kind) {
    try {
      const rows = JSON.parse(fs.readFileSync(file(kind), "utf8"));
      if (!Array.isArray(rows)) throw new Error("Invalid places file");
      return rows.filter((row) => row && placeUrl(row.url) === row.url);
    } catch (error) {
      if (error.code === "ENOENT") return [];
      throw new Error(`Could not read browser ${kind}.`, { cause: error });
    }
  }
  function write(kind, rows) {
    const target = file(kind);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(`${target}.tmp`, JSON.stringify(rows), { mode: 0o600 });
    fs.renameSync(`${target}.tmp`, target);
  }
  function retention() {
    const config = settings();
    return {
      ...normalizeRetention({
        days: config.browser_history_max_days,
        entries: config.browser_history_max_entries,
      }),
      defaults: { days: 90, entries: 5000 },
    };
  }
  function history() {
    const rows = read("history"),
      { retained, removed } = applyRetention(rows, retention(), now());
    if (removed) write("history", retained);
    return retained;
  }
  function visit(value, title) {
    const url = placeUrl(value);
    if (!url) return;
    write(
      "history",
      applyRetention(
        [
          { url, title: String(title || url).slice(0, 512), visitedAt: now() },
          ...read("history").filter((row) => row.url !== url),
        ],
        retention(),
        now(),
      ).retained,
    );
  }
  function removeHistory(urls) {
    const rows = read("history"),
      keys = new Set(urls),
      removed = rows.filter((row) => keys.has(row.url));
    write(
      "history",
      rows.filter((row) => !keys.has(row.url)),
    );
    return removed;
  }
  function restoreHistory(rows) {
    const merged = new Map();
    for (const row of [...read("history"), ...rows])
      if (!merged.has(row.url) || (merged.get(row.url).visitedAt ?? 0) < row.visitedAt)
        merged.set(row.url, row);
    write("history", applyRetention([...merged.values()], retention(), now()).retained);
  }
  function clearHistory(since = 0) {
    const rows = read("history"),
      kept = rows.filter((row) => row.visitedAt < since);
    write("history", kept);
    return rows.length - kept.length;
  }
  function setRetention({ days, entries, preview, expectedRemoves }) {
    const limits = normalizeRetention({ days, entries }),
      result = applyRetention(read("history"), limits, now());
    if (preview) return { ok: true, removes: result.removed };
    if (expectedRemoves !== result.removed)
      return { ok: false, stale: true, removes: result.removed };
    const config = settings();
    config.browser_history_max_days = limits.days;
    config.browser_history_max_entries = limits.entries;
    saveSettings(config);
    write("history", result.retained);
    return { ok: true, removed: result.removed, ...limits };
  }
  function bookmarks() {
    return read("bookmarks");
  }
  function update({ url: value, title, remove = false, previousUrl, index }) {
    const url = placeUrl(value);
    if (!url) throw new PublicPlacesError("Invalid request.");
    const rows = bookmarks(),
      previous = previousUrl ?? url,
      found = rows.findIndex((row) => row.url === previous);
    if (previousUrl && previousUrl !== url && rows.some((row) => row.url === url))
      throw new PublicPlacesError("Already bookmarked.");
    if (found >= 0) rows.splice(found, 1);
    if (!remove)
      rows.splice(Math.min(index ?? (found < 0 ? 0 : found), rows.length), 0, {
        url,
        title: String(title || url)
          .trim()
          .slice(0, 512),
      });
    write("bookmarks", rows);
    return rows;
  }
  function suggestions(query, onlyBookmarks = false) {
    const saved = bookmarks();
    const urls = new Set(saved.map((row) => row.url));
    const rows = [
      ...saved.map((row) => ({ ...row, bookmarked: true })),
      ...(onlyBookmarks ? [] : history().filter((row) => !urls.has(row.url))),
    ];
    const text = String(query || "")
      .trim()
      .toLowerCase();
    return rows
      .filter((row) => `${row.url} ${row.title}`.toLowerCase().includes(text))
      .sort((a, b) => Number(b.bookmarked === true) - Number(a.bookmarked === true))
      .slice(0, 8);
  }
  return {
    visit,
    bookmarks,
    history,
    update,
    suggestions,
    clearHistory,
    removeHistory,
    restoreHistory,
    retention,
    setRetention,
  };
}
module.exports = {
  createBrowserPlaces,
  placeUrl,
  applyRetention,
  normalizeRetention,
  PublicPlacesError,
};
