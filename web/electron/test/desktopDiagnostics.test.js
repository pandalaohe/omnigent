const { it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { performance } = require("node:perf_hooks");
const { registerDesktopDiagnostics } = require("../src/desktopDiagnostics");

function harness(t) {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "desktop-diagnostics-"));
  t.after(() => fs.rmSync(userData, { recursive: true, force: true }));
  const memory = { workingSetSize: 1200, peakWorkingSetSize: 2400, privateBytes: 600 };
  const app = Object.assign(new EventEmitter(), {
    getPath: () => userData,
    getAppMetrics: () => [{ pid: 123, memory }],
  });
  const contents = Object.assign(new EventEmitter(), { id: 8, getOSProcessId: () => 123 });
  registerDesktopDiagnostics(app)({ id: 4, webContents: contents });
  const file = path.join(userData, "logs", "desktop.log");
  const read = () => fs.readFileSync(file, "utf8").trim().split("\n").map(JSON.parse);
  return { app, contents, file, read, memory };
}

it("persists warning/error console events with timestamps, without info/debug noise", (t) => {
  const h = harness(t);
  for (const level of ["info", "debug", "warning", "error"]) {
    h.contents.emit("console-message", {
      level,
      message: "Renderer failed",
      sourceId: "https://app.example/assets/app.js",
      lineNumber: 12,
    });
  }
  const records = h.read();
  assert.deepEqual(
    records.map((record) => record.level),
    ["warning", "error"],
  );
  assert.equal(records[1].event, "console-message");
  assert.equal(records[1].windowId, 4);
  assert.equal(records[1].webContentsId, 8);
  assert.equal(records[1].message, "Renderer failed");
  assert.equal(records[1].lineNumber, 12);
  assert.match(records[1].timestamp, /^\d{4}-\d\d-\d\dT/);
});

it("removes credentials and URL paths/queries from remote diagnostic text", (t) => {
  const h = harness(t);
  h.contents.emit("console-message", {
    level: "error",
    message:
      'Failed https://urluser:urlpass@app.example/chat/private?token=topsecret#fragment Bearer authvalue password=passvalue {"api_key":"keyvalue"} eyJabc.eyJdef.sig',
    sourceId: "https://app.example/secret-path?code=codevalue",
    lineNumber: 1,
  });
  const text = fs.readFileSync(h.file, "utf8");
  for (const value of [
    "urluser",
    "urlpass",
    "private",
    "topsecret",
    "fragment",
    "authvalue",
    "passvalue",
    "keyvalue",
    "eyJabc",
    "secret-path",
    "codevalue",
  ]) {
    assert.ok(!text.includes(value), value);
  }
  assert.ok(text.includes("https://app.example/[redacted]"));
});

it("removes local file paths and inline data from console sources", (t) => {
  const h = harness(t);
  for (const sourceId of [
    "file:///opt/work/private-dir/app/setup/index.html?token=local-value",
    "data:text/javascript,password=inline-value",
    "blob:https://app.example/private-uuid",
  ]) {
    h.contents.emit("console-message", { level: "error", message: sourceId, sourceId });
  }
  assert.deepEqual(
    h.read().map((record) => record.sourceId),
    ["file:[redacted]", "data:[redacted]", "blob:[redacted]"],
  );
  const text = fs.readFileSync(h.file, "utf8");
  for (const value of [
    "private-dir",
    "setup/index.html",
    "local-value",
    "inline-value",
    "private-uuid",
  ]) {
    assert.ok(!text.includes(value), value);
  }
});

it("records hang/recovery durations and live renderer memory, preserving the first hang time", (t) => {
  const h = harness(t);
  let now = 100;
  t.mock.method(performance, "now", () => now);
  h.contents.emit("unresponsive");
  now = 200;
  h.contents.emit("unresponsive");
  now = 450;
  h.contents.emit("responsive");
  h.contents.emit("responsive");
  const records = h.read();
  assert.deepEqual(
    records.map((record) => record.unresponsiveDurationMs),
    [0, 100, 350, null],
  );
  assert.equal(records[2].rendererPid, 123);
  assert.deepEqual(records[2].memoryKB, h.memory);
});

it("records reload recovery and renderer exit even after the renderer PID disappears", (t) => {
  const h = harness(t);
  let now = 0;
  t.mock.method(performance, "now", () => now);
  h.contents.emit("unresponsive");
  now = 200;
  h.contents.emit("did-finish-load");
  h.contents.getOSProcessId = () => 0;
  h.contents.emit("render-process-gone", {}, { reason: "crashed", exitCode: 9 });
  const records = h.read();
  assert.equal(records[1].unresponsiveDurationMs, 200);
  assert.equal(records[2].unresponsiveDurationMs, null);
  assert.equal(records[2].rendererPid, null);
  assert.equal(records[2].lastRendererPid, 123);
  assert.equal(records[2].reason, "crashed");
  assert.equal(records[2].exitCode, 9);
});

it("logs GPU/utility exits and load errors without saving navigated URLs", (t) => {
  const h = harness(t);
  h.app.emit("child-process-gone", {}, { type: "GPU", reason: "crashed", exitCode: 1 });
  h.app.emit(
    "child-process-gone",
    {},
    { type: "Utility", reason: "launch-failed", exitCode: 2, systemErrorCode: 5 },
  );
  h.contents.emit(
    "did-fail-load",
    {},
    -105,
    "ERR_NAME_NOT_RESOLVED",
    "https://app.example/chat/private",
    true,
  );
  const records = h.read();
  assert.equal(records[0].type, "GPU");
  assert.equal(records[1].systemErrorCode, 5);
  assert.equal(records[2].errorCode, -105);
  assert.equal(records[2].isMainFrame, true);
  assert.ok(!fs.readFileSync(h.file, "utf8").includes("private"));
});

it("still records lifecycle events when process metrics cannot be read", (t) => {
  const h = harness(t);
  h.app.getAppMetrics = () => {
    throw new Error("metrics failed");
  };
  assert.doesNotThrow(() => h.contents.emit("unresponsive"));
  const [record] = h.read();
  assert.equal(record.rendererPid, 123);
  assert.equal(record.memoryKB, null);
  assert.equal(record.metricsError, "unavailable");
});

it("bounds the log to the current file and one rotated file, and truncates messages", (t) => {
  const h = harness(t);
  fs.mkdirSync(path.dirname(h.file), { recursive: true });
  fs.writeFileSync(h.file, "x".repeat(2 * 1024 * 1024));
  fs.writeFileSync(h.file + ".1", "older log");
  h.contents.emit("console-message", { level: "error", message: "a".repeat(10000) });
  assert.equal(fs.statSync(h.file + ".1").size, 2 * 1024 * 1024);
  assert.equal(h.read()[0].message.length, 4096);
  assert.ok(fs.statSync(h.file).size < 5000);
});

it("unwritable log storage does not throw or alter event handling", (t) => {
  const h = harness(t);
  fs.writeFileSync(path.dirname(h.file), "blocking file");
  const warnings = [];
  t.mock.method(console, "warn", (...args) => warnings.push(args));
  assert.doesNotThrow(() => h.contents.emit("unresponsive"));
  assert.doesNotThrow(() => h.contents.emit("responsive"));
  assert.equal(warnings.length, 1);
});
