// Calling-defaults preference namespaces.
//
// `calling_defaults` is the per-host master table consulted when a project
// overrides nothing: `{<host_id>: {<harness>: {model?, effort?}}}`. Writes
// replace exactly one host key so two devices editing different hosts don't
// clobber each other (the server merges namespace top-level keys shallowly).
//
// `calling_last` is the web-only carry-over memory:
// `{enabled: bool, "p:<project_id>": {<host_id>: {last_agent_id,
// agents: {<agent_id>: {harness, model?, effort?, at}}}}}`. localStorage is
// the synchronous read path; userPreferencesSync hydrates and patches the
// server (like agentPins.ts).

import { useEffect, useState } from "react";

import { queueUserPreferencePatch } from "./userPreferencesSync";

const CALLING_DEFAULTS_STORAGE_KEY = "omnigent:calling-defaults";
const CALLING_DEFAULTS_CHANGED_EVENT = "omnigent:calling-defaults-changed";
const CALLING_LAST_STORAGE_KEY = "omnigent:calling-last";
const CALLING_LAST_CHANGED_EVENT = "omnigent:calling-last-changed";

/** Harnesses a host can answer model-options requests for, mirroring the server. */
export const CALLING_DEFAULT_HARNESSES = [
  "codex-native",
  "codex",
  "claude-native",
  "claude-sdk",
  "pi-native",
  "devin-native",
] as const;

/** SDK harnesses whose entries fall back to a native sibling's master row. */
export const SDK_NATIVE_PARENT: Record<string, string> = {
  codex: "codex-native",
  "claude-sdk": "claude-native",
};

const HARNESS_LABELS: Record<string, string> = {
  "codex-native": "Codex",
  codex: "Codex SDK",
  "claude-native": "Claude Code",
  "claude-sdk": "Claude SDK",
  "pi-native": "Pi",
  "devin-native": "Devin",
};

/** Display label for a canonical harness id; unknown ids render as-is. */
export function callingHarnessLabel(harness: string): string {
  return HARNESS_LABELS[harness] ?? harness;
}

// EFFORT_CLEAR_VALUES server-side, plus the pickers' no-override sentinels.
// The bare effort "none" (Pi's lowest rung) is a real value, not a clear.
const CLEAR_VALUES = new Set(["default", "off", "reset", "__default__", "__none__"]);

function isClearValue(value: string): boolean {
  return CLEAR_VALUES.has(value.trim().toLowerCase());
}

function readStoredObject(storageKey: string): Record<string, unknown> {
  if (typeof window === "undefined") return {};
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(storageKey);
  } catch {
    return {};
  }
  if (raw === null) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return parsed as Record<string, unknown>;
  } catch {
    return {};
  }
}

function storeObject(storageKey: string, eventName: string, value: unknown): void {
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(value));
  } catch {
    // Storage quota / access errors must not break the setting.
  }
  window.dispatchEvent(new Event(eventName));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** One harness entry in the master table; absent keys mean "Default". */
export interface CallingDefaultEntry {
  model?: string;
  effort?: string;
  speed?: string;
  permission?: string;
}

export type CallingDefaultsTable = Record<string, Record<string, CallingDefaultEntry>>;

function sanitizeEntry(value: unknown): CallingDefaultEntry | null {
  if (!isRecord(value)) return null;
  const entry: CallingDefaultEntry = {};
  const { model, effort } = value;
  if (typeof model === "string" && model && !isClearValue(model)) entry.model = model;
  if (typeof effort === "string" && effort && !isClearValue(effort)) entry.effort = effort;
  for (const field of ["speed", "permission"] as const) {
    const setting = value[field];
    if (typeof setting === "string" && setting && !isClearValue(setting)) entry[field] = setting;
  }
  return entry;
}

/** Read the master table; malformed entries are dropped. */
export function readCallingDefaults(): CallingDefaultsTable {
  const stored = readStoredObject(CALLING_DEFAULTS_STORAGE_KEY);
  const table: CallingDefaultsTable = {};
  for (const [hostId, hostValue] of Object.entries(stored)) {
    if (!isRecord(hostValue)) continue;
    const hostTable: Record<string, CallingDefaultEntry> = {};
    for (const [harness, entryValue] of Object.entries(hostValue)) {
      const entry = sanitizeEntry(entryValue);
      if (entry !== null) hostTable[harness] = entry;
    }
    table[hostId] = hostTable;
  }
  return table;
}

/** Store one host's table and patch that host key, never the others. */
function writeCallingDefaultsHost(
  hostId: string,
  hostTable: Record<string, CallingDefaultEntry>,
): void {
  storeObject(CALLING_DEFAULTS_STORAGE_KEY, CALLING_DEFAULTS_CHANGED_EVENT, {
    ...readCallingDefaults(),
    [hostId]: hostTable,
  });
  queueUserPreferencePatch("calling_defaults", { [hostId]: hostTable });
}

/** Add an explicit row for a harness; a no-op when the row already exists. */
export function addCallingDefaultEntry(hostId: string, harness: string): void {
  const table = readCallingDefaults();
  const hostTable = { ...(table[hostId] ?? {}) };
  if (harness in hostTable) return;
  hostTable[harness] = {};
  writeCallingDefaultsHost(hostId, hostTable);
}

/** Delete one harness row, leaving the other hosts and rows untouched. */
export function removeCallingDefaultEntry(hostId: string, harness: string): void {
  const table = readCallingDefaults();
  const hostTable = { ...(table[hostId] ?? {}) };
  if (!(harness in hostTable)) return;
  Reflect.deleteProperty(hostTable, harness);
  writeCallingDefaultsHost(hostId, hostTable);
}

/** Set one field; `null` (or a clear word) removes it. */
export function setCallingDefaultField(
  hostId: string,
  harness: string,
  field: "model" | "effort" | "speed" | "permission",
  value: string | null,
): void {
  const table = readCallingDefaults();
  const hostTable = { ...(table[hostId] ?? {}) };
  const entry: CallingDefaultEntry = { ...(hostTable[harness] ?? {}) };
  if (value === null || value === "" || isClearValue(value)) {
    Reflect.deleteProperty(entry, field);
  } else {
    entry[field] = value;
  }
  hostTable[harness] = entry;
  writeCallingDefaultsHost(hostId, hostTable);
}

/** One agent's last-used settings under a project × host. */
export interface CallingLastAgentEntry {
  harness: string;
  model?: string;
  effort?: string;
  /** Unix epoch seconds when the entry was recorded. */
  at: number;
}

/** The last agent and its per-agent settings under a project × host. */
export interface CallingLastHostEntry {
  last_agent_id: string;
  agents: Record<string, CallingLastAgentEntry>;
}

export interface CallingLast {
  enabled: boolean;
  /** Raw top-level keys, `p:<project_id>`, exactly as stored. */
  projects: Record<string, Record<string, CallingLastHostEntry>>;
}

function sanitizeAgentEntry(value: unknown): CallingLastAgentEntry | null {
  if (!isRecord(value)) return null;
  const { harness, model, effort, at } = value;
  if (typeof harness !== "string" || !harness) return null;
  const entry: CallingLastAgentEntry = {
    harness,
    at: typeof at === "number" && Number.isFinite(at) ? at : 0,
  };
  if (typeof model === "string" && model && !isClearValue(model)) entry.model = model;
  if (typeof effort === "string" && effort && !isClearValue(effort)) entry.effort = effort;
  return entry;
}

/** Read the carry-over memory; malformed entries are dropped. */
export function readCallingLast(): CallingLast {
  const stored = readStoredObject(CALLING_LAST_STORAGE_KEY);
  const projects: CallingLast["projects"] = {};
  for (const [projectKey, projectValue] of Object.entries(stored)) {
    if (!projectKey.startsWith("p:") || !isRecord(projectValue)) continue;
    const hosts: Record<string, CallingLastHostEntry> = {};
    for (const [hostId, hostValue] of Object.entries(projectValue)) {
      if (!isRecord(hostValue)) continue;
      const { last_agent_id: lastAgentId, agents } = hostValue;
      if (typeof lastAgentId !== "string" || !lastAgentId) continue;
      const cleanedAgents: Record<string, CallingLastAgentEntry> = {};
      if (isRecord(agents)) {
        for (const [agentId, agentValue] of Object.entries(agents)) {
          const entry = sanitizeAgentEntry(agentValue);
          if (entry !== null) cleanedAgents[agentId] = entry;
        }
      }
      hosts[hostId] = { last_agent_id: lastAgentId, agents: cleanedAgents };
    }
    projects[projectKey] = hosts;
  }
  return { enabled: stored.enabled === true, projects };
}

export function readCallingLastEnabled(): boolean {
  return readCallingLast().enabled;
}

function writeCallingLast(next: CallingLast, patch: Record<string, unknown>): void {
  storeObject(CALLING_LAST_STORAGE_KEY, CALLING_LAST_CHANGED_EVENT, {
    enabled: next.enabled,
    ...next.projects,
  });
  queueUserPreferencePatch("calling_last", patch);
}

/** Toggle carry-over; patches only the `enabled` key, keeping project memory. */
export function writeCallingLastEnabled(enabled: boolean): void {
  const current = readCallingLast();
  if (current.enabled === enabled) return;
  writeCallingLast({ ...current, enabled }, { enabled });
}

/**
 * Record a successful create / accepted in-session change for one agent.
 *
 * Patches only the `p:<project_id>` key, with every host and agent read from
 * the local cache, so sibling projects survive the shallow server merge.
 */
export function recordCallingLast(
  projectId: string,
  hostId: string,
  agentId: string,
  value: { harness: string; model?: string | null; effort?: string | null },
): void {
  const harness = typeof value.harness === "string" ? value.harness.trim() : "";
  if (!projectId || !hostId || !agentId || !harness) return;
  const projectKey = `p:${projectId}`;
  const current = readCallingLast();
  const project = current.projects[projectKey] ?? {};
  const existing = project[hostId];
  const agentEntry: CallingLastAgentEntry = {
    harness,
    at: Math.floor(Date.now() / 1000),
  };
  if (typeof value.model === "string" && value.model && !isClearValue(value.model)) {
    agentEntry.model = value.model;
  }
  if (typeof value.effort === "string" && value.effort && !isClearValue(value.effort)) {
    agentEntry.effort = value.effort;
  }
  const nextHost: CallingLastHostEntry = {
    last_agent_id: agentId,
    agents: { ...(existing?.agents ?? {}), [agentId]: agentEntry },
  };
  const nextProject = { ...project, [hostId]: nextHost };
  writeCallingLast(
    { ...current, projects: { ...current.projects, [projectKey]: nextProject } },
    { [projectKey]: nextProject },
  );
}

function useStorageValue<T>(read: () => T, eventName: string): T {
  const [value, setValue] = useState(read);
  useEffect(() => {
    const refresh = () => setValue(read());
    window.addEventListener(eventName, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(eventName, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, [read, eventName]);
  return value;
}

/** Subscribe to the master table (category + storage events). */
export function useCallingDefaults(): CallingDefaultsTable {
  return useStorageValue(readCallingDefaults, CALLING_DEFAULTS_CHANGED_EVENT);
}

/** Subscribe to the carry-over toggle (default OFF when never set). */
export function useCallingLastEnabled(): boolean {
  return useStorageValue(readCallingLastEnabled, CALLING_LAST_CHANGED_EVENT);
}
