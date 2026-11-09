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

/** Desktop-local places. All writes replace a complete JSON file atomically. */
function createBrowserPlaces({ directory, settings = () => ({}), now = Date.now }) {
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
  function history() {
    const config = settings();
    const limit = (value, fallback, max) =>
      Number.isFinite(value) ? Math.max(0, Math.min(max, Math.floor(value))) : fallback;
    const entries = limit(config.browser_history_max_entries, 5000, 100000);
    const days = limit(config.browser_history_max_days, 90, 3650);
    const rows = read("history");
    const retained = rows
      .filter((row) => row.visitedAt >= now() - days * 86400000)
      .sort((a, b) => b.visitedAt - a.visitedAt)
      .slice(0, entries);
    if (retained.length !== rows.length) write("history", retained);
    return retained;
  }
  function visit(value, title) {
    const url = placeUrl(value);
    if (!url) return;
    write("history", [
      { url, title: String(title || url).slice(0, 512), visitedAt: now() },
      ...history().filter((row) => row.url !== url),
    ]);
    history();
  }
  function bookmarks() {
    return read("bookmarks");
  }
  function update({ url: value, title, remove = false }) {
    const url = placeUrl(value);
    if (!url) throw new Error("Only web pages can be bookmarked.");
    const rows = bookmarks().filter((row) => row.url !== url);
    if (!remove)
      rows.unshift({
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
    clearHistory: () => write("history", []),
  };
}
module.exports = { createBrowserPlaces, placeUrl };
