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
import type { ProjectConfig } from "./projectsApi";
import { normalizeAgyPermissionTier } from "./nativeHarnessModes";
import {
  sessionDefaultModeOptions,
  sessionModeHandler,
  type HarnessCapabilityMap,
} from "./sessionDefaultModes";

import { queueUserPreferencePatch } from "./userPreferencesSync";

const CALLING_DEFAULTS_STORAGE_KEY = "omnigent:calling-defaults";
const CALLING_DEFAULTS_CHANGED_EVENT = "omnigent:calling-defaults-changed";
const CALLING_LAST_STORAGE_KEY = "omnigent:calling-last";
const CALLING_LAST_CHANGED_EVENT = "omnigent:calling-last-changed";

/** Compatibility for catalogs served before `calling_defaults` was declared. */
const LEGACY_CALLING_DEFAULT_HARNESSES = [
  "codex-native",
  "codex",
  "claude-native",
  "claude-sdk",
  "pi-native",
  "devin-native",
  "antigravity-native",
] as const;

const LEGACY_SDK_NATIVE_PARENT: Record<string, string> = {
  codex: "codex-native",
  "claude-sdk": "claude-native",
};

/** Declared row membership; a missing declaration uses the older catalog's set. */
export function callingDefaultHarnesses(capabilities?: HarnessCapabilityMap): string[] {
  const rows = Object.entries(capabilities ?? {});
  if (rows.some(([, capability]) => "calling_defaults" in capability))
    return rows
      .filter(([, capability]) => capability.calling_defaults != null)
      .map(([harness]) => harness);
  return [...LEGACY_CALLING_DEFAULT_HARNESSES];
}

export function callingDefaultParent(
  harness: string,
  capabilities?: HarnessCapabilityMap,
): string | null {
  const capability =
    capabilities?.[harness] ?? (harness.startsWith("acp:") ? capabilities?.acp : undefined);
  if (capability && "calling_defaults" in capability)
    return capability.calling_defaults?.inherits_from ?? null;
  return LEGACY_SDK_NATIVE_PARENT[harness] ?? null;
}

export function callingDefaultHasModels(
  harness: string,
  capabilities?: HarnessCapabilityMap,
): boolean {
  const capability =
    capabilities?.[harness] ?? (harness.startsWith("acp:") ? capabilities?.acp : undefined);
  if (capability && "calling_defaults" in capability)
    return capability.calling_defaults?.model_options ?? false;
  return (
    LEGACY_CALLING_DEFAULT_HARNESSES.some((entry) => entry === harness) ||
    harness.startsWith("acp:")
  );
}

const HARNESS_LABELS: Record<string, string> = {
  "codex-native": "Codex",
  codex: "Codex SDK",
  "claude-native": "Claude Code",
  "claude-sdk": "Claude SDK",
  "pi-native": "Pi",
  "devin-native": "Devin",
  "antigravity-native": "Antigravity",
};

/** Display label for a canonical harness id; unknown ids render as-is. */
export function callingHarnessLabel(harness: string, capabilities?: HarnessCapabilityMap): string {
  if (harness.startsWith("acp:")) return `ACP · ${harness.slice(4)}`;
  return capabilities?.[harness]?.label ?? HARNESS_LABELS[harness] ?? harness;
}

// EFFORT_CLEAR_VALUES server-side, plus the pickers' no-override sentinels.
// The bare effort "none" (Pi's lowest rung) is a real value, not a clear.
const CLEAR_VALUES = new Set(["default", "off", "reset", "__default__", "__none__"]);

function isClearValue(value: string): boolean {
  return CLEAR_VALUES.has(value.trim().toLowerCase());
}

function isClearEffort(value: string, harness: string): boolean {
  return harness === "acp" || harness.startsWith("acp:")
    ? ["__none__", "__default__"].includes(value)
    : isClearValue(value);
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

export type CallingDefaultField = keyof CallingDefaultEntry;
export type ProjectDefaultSource =
  "project_legacy" | "master" | "project_host_native" | "master_native" | "none";

/** The create-time fallback chain after the project's own harness field. */
export function resolveProjectInheritedField(
  config: ProjectConfig,
  master: CallingDefaultsTable,
  hostId: string,
  harness: string,
  field: CallingDefaultField,
  agentId: string | null,
  capabilities?: HarnessCapabilityMap,
): { value: string | null; source: ProjectDefaultSource } {
  const projectHost = config.calling_defaults?.[hostId]?.harnesses;
  const masterHost = master[hostId];
  const parent = callingDefaultParent(harness, capabilities);
  const candidates: [string | undefined, ProjectDefaultSource][] = [];
  if (field === "model" && agentId && agentId === config.agent_id) {
    candidates.push([config.model, "project_legacy"]);
  }
  candidates.push([masterHost?.[harness]?.[field], "master"]);
  if (parent) {
    candidates.push([projectHost?.[parent]?.[field], "project_host_native"]);
    candidates.push([masterHost?.[parent]?.[field], "master_native"]);
  }
  for (const [candidate, source] of candidates) {
    const value =
      sessionModeHandler(harness, "permission", capabilities) === "agy-tier" &&
      field === "permission"
        ? normalizeAgyPermissionTier(candidate)
        : candidate;
    if (!value) continue;
    if (
      field === "permission" &&
      !sessionDefaultModeOptions(harness, "permission", [], null, "default", capabilities).some(
        (option) => option.value === value,
      )
    )
      continue;
    if (
      field === "speed" &&
      !(capabilities?.[harness]?.session_modes?.speed?.options === "per-model" ||
      (capabilities?.[harness]?.session_modes === undefined &&
        sessionModeHandler(harness, "speed", capabilities) === "codex-service-tier")
        ? /^[a-z][a-z0-9_-]{0,63}$/.test(value)
        : sessionDefaultModeOptions(harness, "speed", [], null, "default", capabilities).some(
            (option) => option.value === value,
          ))
    )
      continue;
    return { value, source };
  }
  return { value: null, source: "none" };
}

function sanitizeEntry(value: unknown, harness: string): CallingDefaultEntry | null {
  if (!isRecord(value)) return null;
  const entry: CallingDefaultEntry = {};
  const { model, effort } = value;
  if (typeof model === "string" && model && !isClearValue(model)) entry.model = model;
  if (typeof effort === "string" && effort && !isClearEffort(effort, harness))
    entry.effort = effort;
  for (const field of ["speed", "permission"] as const) {
    const setting =
      harness === "antigravity-native" && field === "permission"
        ? normalizeAgyPermissionTier(typeof value[field] === "string" ? value[field] : null)
        : value[field];
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
      const entry = sanitizeEntry(entryValue, harness);
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
  if (
    value === null ||
    value === "" ||
    (field === "effort" ? isClearEffort(value, harness) : isClearValue(value))
  ) {
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
  speed?: string;
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
  const { harness, model, effort, speed, at } = value;
  if (typeof harness !== "string" || !harness) return null;
  const entry: CallingLastAgentEntry = {
    harness,
    at: typeof at === "number" && Number.isFinite(at) ? at : 0,
  };
  if (typeof model === "string" && model && !isClearValue(model)) entry.model = model;
  if (typeof effort === "string" && effort && !isClearEffort(effort, harness))
    entry.effort = effort;
  if (typeof speed === "string" && speed && !isClearValue(speed)) entry.speed = speed;
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
  value: { harness: string; model?: string | null; effort?: string | null; speed?: string | null },
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
  if (typeof value.effort === "string" && value.effort && !isClearEffort(value.effort, harness)) {
    agentEntry.effort = value.effort;
  }
  if (typeof value.speed === "string" && value.speed && !isClearValue(value.speed)) {
    agentEntry.speed = value.speed;
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
