"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { performance } = require("node:perf_hooks");

const MAX_LOG_BYTES = 2 * 1024 * 1024;

// Remote pages may include authentication URLs or credentials in errors.
function diagnosticText(value) {
  return String(value ?? "")
    .replace(/\b(?:https?|file|data|blob):[^\s<>"']+/gi, (url) => {
      try {
        const parsed = new URL(url);
        return parsed.protocol === "http:" || parsed.protocol === "https:"
          ? parsed.origin + "/[redacted]"
          : parsed.protocol + "[redacted]";
      } catch {
        return "[redacted URL]";
      }
    })
    .replace(/\b(Bearer|Basic)\s+\S+/gi, "$1 [redacted]")
    .replace(
      /((?:["']?)(?:access[_-]?token|refresh[_-]?token|token|password|secret|authorization|cookie|api[_-]?key)(?:["']?)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,
      "$1[redacted]",
    )
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, "[redacted]")
    .slice(0, 4096);
}

function registerDesktopDiagnostics(app) {
  let writeFailureReported = false;
  function log(event, details) {
    try {
      const file = path.join(app.getPath("userData"), "logs", "desktop.log");
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const line =
        JSON.stringify({ timestamp: new Date().toISOString(), event, ...details }) + "\n";
      if (fs.existsSync(file) && fs.statSync(file).size + Buffer.byteLength(line) > MAX_LOG_BYTES) {
        fs.rmSync(file + ".1", { force: true });
        fs.renameSync(file, file + ".1");
      }
      fs.appendFileSync(file, line, { mode: 0o600 });
    } catch (error) {
      if (!writeFailureReported) {
        writeFailureReported = true;
        console.warn("[omnigent] desktop diagnostics unavailable", error.code ?? "write-failed");
      }
    }
  }

  app.on("child-process-gone", (_event, details) => {
    log("child-process-gone", {
      type: details.type,
      reason: details.reason,
      exitCode: details.exitCode,
      systemErrorCode: details.systemErrorCode,
    });
  });

  return function registerWindow(win) {
    const contents = win.webContents;
    let unresponsiveSince = null;
    let lastRendererPid = null;
    function snapshot() {
      let rendererPid = null;
      let memoryKB = null;
      let metricsError = null;
      try {
        rendererPid = contents.getOSProcessId() || null;
        if (rendererPid) {
          lastRendererPid = rendererPid;
          const memory = app.getAppMetrics().find((metric) => metric.pid === rendererPid)?.memory;
          if (memory) {
            memoryKB = {
              workingSetSize: memory.workingSetSize,
              peakWorkingSetSize: memory.peakWorkingSetSize,
              privateBytes: memory.privateBytes,
            };
          }
        }
      } catch (error) {
        metricsError = error.code ?? "unavailable";
      }
      return { rendererPid, lastRendererPid, memoryKB, metricsError };
    }
    function record(event, details = {}) {
      log(event, { windowId: win.id, webContentsId: contents.id, ...details });
    }
    function duration() {
      return unresponsiveSince === null ? null : Math.round(performance.now() - unresponsiveSince);
    }

    contents.on("console-message", (details) => {
      if (details.level !== "warning" && details.level !== "error") return;
      record("console-message", {
        level: details.level,
        message: diagnosticText(details.message),
        sourceId: diagnosticText(details.sourceId),
        lineNumber: details.lineNumber,
      });
    });
    contents.on("did-fail-load", (_event, errorCode, errorDescription, _url, isMainFrame) => {
      record("did-fail-load", {
        errorCode,
        errorDescription: diagnosticText(errorDescription),
        isMainFrame,
      });
    });
    contents.on("did-finish-load", () => {
      record("did-finish-load", { ...snapshot(), unresponsiveDurationMs: duration() });
      unresponsiveSince = null;
    });
    contents.on("unresponsive", () => {
      unresponsiveSince ??= performance.now();
      record("unresponsive", { ...snapshot(), unresponsiveDurationMs: duration() });
    });
    contents.on("responsive", () => {
      record("responsive", { ...snapshot(), unresponsiveDurationMs: duration() });
      unresponsiveSince = null;
    });
    contents.on("render-process-gone", (_event, details) => {
      record("render-process-gone", {
        ...snapshot(),
        reason: details.reason,
        exitCode: details.exitCode,
        unresponsiveDurationMs: duration(),
      });
      unresponsiveSince = null;
    });
  };
}

module.exports = { registerDesktopDiagnostics };
