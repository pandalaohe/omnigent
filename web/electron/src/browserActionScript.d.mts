export function buildBrowserActionScript(
  action: "snapshot" | "click" | "type",
  args?: Record<string, unknown>,
): string;

export function buildBrowserActionScriptForExecution(
  action: "snapshot" | "click" | "type",
  args?: Record<string, unknown>,
): string;
