const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { createBrowserPlaces, placeUrl } = require("../src/browserPlaces");

test("desktop places survive restart, rank bookmarks first and support rename/removal", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "browser-places-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const options = { directory: () => directory };
  const places = createBrowserPlaces(options);
  places.visit("https://example.test/recent", "Recent example");
  places.update({ url: "https://example.test/saved", title: "Saved example" });
  const restarted = createBrowserPlaces(options);
  assert.deepEqual(
    restarted.suggestions("example").map((r) => r.url),
    ["https://example.test/saved", "https://example.test/recent"],
  );
  restarted.update({ url: "https://example.test/saved", title: "Renamed" });
  assert.equal(restarted.bookmarks()[0].title, "Renamed");
  restarted.update({ url: "https://example.test/saved", remove: true });
  assert.equal(restarted.bookmarks().length, 0);
  restarted.clearHistory();
  assert.equal(restarted.suggestions("").length, 0);
});

test("history enforces age/count, deduplicates URLs and excludes credentials and non-web pages", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "browser-places-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let time = 1000000000;
  const places = createBrowserPlaces({
    directory: () => directory,
    now: () => time,
    settings: () => ({ browser_history_max_entries: 2, browser_history_max_days: 1 }),
  });
  places.visit("https://user:fake@example.test/one", "One");
  places.visit("file:///tmp/page", "Ignored");
  places.visit("https://example.test/two", "Two");
  time++;
  places.visit("https://example.test/three", "Three");
  assert.equal(places.history().length, 2);
  places.visit("https://example.test/three", "Again");
  assert.equal(places.history().length, 2);
  assert.equal(places.history()[0].title, "Again");
  time += 86400001;
  assert.equal(places.history().length, 0);
  assert.equal(placeUrl("https://user:fake@example.test/one"), "https://example.test/one");
  assert.equal(placeUrl("javascript:alert(1)"), null);
});

test("a malformed file is reported without overwriting it", (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "browser-places-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const file = path.join(directory, "browser-bookmarks.json");
  fs.writeFileSync(file, "broken");
  const places = createBrowserPlaces({ directory: () => directory });
  assert.throws(() => places.update({ url: "https://example.test" }), /Could not read/);
  assert.equal(fs.readFileSync(file, "utf8"), "broken");
});
