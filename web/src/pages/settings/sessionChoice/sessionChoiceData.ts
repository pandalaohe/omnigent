import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { UsageAccountView } from "@/pages/usageBoard/usageBoardData";

export type ChoiceFallbackMode = "drain" | "return";
export type ChoicePermissionTier = "ask" | "plan" | "accept-edits" | "auto";

export interface ChoiceLink {
  agent: string;
  model?: string | null;
  effort?: string | null;
  meter?: string | null;
  maxRunning?: number | null;
  fallbackMode?: ChoiceFallbackMode | null;
  permissionTier?: ChoicePermissionTier | null;
}
export interface ChoiceRow {
  preset?: string | null;
  name?: string | null;
  links: ChoiceLink[];
  weight?: number | null;
  adjusted?: boolean;
}
export type ChoicePreset = SessionChoiceDocument["presets"][number];
export interface ChoiceWorkType {
  id: string;
  label: string;
  definition?: string;
  aliases?: string[];
  ifFull?: "queue" | "ask";
}
export interface ChoiceLimit {
  reserve?: {
    mode: "daily" | "total";
    window: string;
    pct?: number | null;
    stopPct?: number | null;
  } | null;
  reserveExempt?: string[];
  maxRunning?: number | null;
}
export interface SessionChoiceDocument {
  [key: string]: unknown;
  schemaVersion?: number;
  limitRules?: ChoiceLimitRule[];
  codexRoutePools?: Record<string, string>;
  enabled: boolean;
  strategy: "drain" | "balance";
  defaultWorkType?: string | null;
  workTypes: ChoiceWorkType[];
  presets: { id: string; name: string; links: ChoiceLink[] }[];
  limits: Record<string, ChoiceLimit>;
  hosts: Record<string, Record<string, ChoiceRow[][]>>;
  abilityOrder: string[][];
}
export interface ChoiceEnvelope {
  settings: SessionChoiceDocument | null;
  revision: string;
  runtime?: { settingsRevision: string; drainLocks: ChoiceDrainLock[] };
  corrected?: string[];
  capabilities?: { documentVersions: number[]; limitRules: boolean; effectiveLimits: boolean };
  configurationError?: { code?: string; message?: string };
}

export interface ChoiceDrainLock {
  hostId: string;
  workType: string;
  tierIndex: number;
  rowPosition: number;
  savedLink: ChoiceLink;
  rowIdentity: string;
  settingsRevision: string;
  since: number;
}

export interface ChoiceConversionPlan {
  sourceRevision: string;
  mappingFingerprint: string;
  replacedPaths: string[][];
  newRuleIds: string[];
  newRules: ChoiceLimitRule[];
  unmapped: { path: (string | number)[]; reason: string }[];
  convertible: boolean;
  settings: SessionChoiceDocument | null;
}

export interface ChoiceLimitScope {
  agent: string;
  host?: string | null;
  model?: string | null;
}

export interface ChoiceLimitRule {
  [key: string]: unknown;
  id: string;
  agent?: string;
  host?: string | null;
  model?: string | null;
  members?: ChoiceLimitScope[];
  maxRunning?: number | null;
  reserve?: {
    [key: string]: unknown;
    mode?: "daily" | "total";
    window?: string;
    pct?: number | null;
    stopPct?: number | null;
  } | null;
  reserveExempt?: string[];
}

export interface ChoiceBindingCap {
  ruleId: string;
  maxRunning: number;
  members: ChoiceLimitScope[];
  runningCount: number | null;
  reservedCount: number | null;
  countState: string;
}

export interface ChoiceEffectiveField {
  defined: boolean;
  value: unknown;
  sourceRuleId: string | null;
  specificity: number | null;
  scope: ChoiceLimitScope | null;
  members?: ChoiceLimitScope[] | null;
  runningCount?: number | null;
  reservedCount?: number | null;
  countState?: string;
  bindingRules?: ChoiceBindingCap[];
}

export interface ChoiceCandidatePreview {
  mode: "candidate";
  candidate: { agent_id: string; host_id: string; model: string };
  policyActive: boolean;
  configuredInTiers: boolean;
  effectiveLimits: Record<string, ChoiceEffectiveField>;
  legacyChecks: { pool: string; limits: ChoiceLimit | null; linkMaxRunning: number | null };
  lines?: ChoiceLine[];
  check: { state: "passes_checks" | "blocked" | "unknown"; reason: string | null };
  admission: false;
  savedRevision: string;
  draftFingerprint: string | null;
}

export class ChoiceRequestError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.name = "ChoiceRequestError";
    this.status = status;
    this.detail = detail;
  }
}
export const EMPTY_CHOICE: SessionChoiceDocument = {
  enabled: false,
  strategy: "drain",
  workTypes: [],
  presets: [],
  limits: {},
  hosts: {},
  abilityOrder: [],
};
export interface ChoiceMeters {
  products?: Record<string, string | null>;
  meters: UsageAccountView[];
  running: Record<string, string[]>;
  floors: Record<string, { until: "reset" | "manual"; active: boolean }>;
}

export async function choiceRequest<T>(path = "", init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(`/v1/me/session-choice${path}`, init);
  const data = await response.json();
  if (!response.ok)
    throw new ChoiceRequestError(
      response.status,
      data.detail ?? `Session choice request failed (${response.status}).`,
    );
  return data as T;
}
const METERS_REFRESH_MS = 60_000;

export function useSessionChoiceDocument({
  enabled = true,
  refreshRuntime = false,
}: { enabled?: boolean; refreshRuntime?: boolean } = {}) {
  return useQuery({
    queryKey: ["sessionChoice"],
    queryFn: () => choiceRequest<ChoiceEnvelope>(),
    enabled,
    retry: false,
    refetchInterval: refreshRuntime ? METERS_REFRESH_MS : false,
  });
}
export function useChoiceMeters(hostId: string) {
  return useQuery({
    queryKey: ["sessionChoiceMeters", hostId],
    queryFn: () => choiceRequest<ChoiceMeters>(`/meters?host_id=${encodeURIComponent(hostId)}`),
    enabled: Boolean(hostId),
    retry: false,
    refetchInterval: METERS_REFRESH_MS,
  });
}

export interface ChoiceLine {
  state: "ok" | "warn" | "stop" | "exhausted" | "unknown";
  warn_line_pct: number | null;
  stop_line_pct: number | null;
  released: boolean;
  running?: string[];
}
export function useChoiceLines() {
  return useQuery({
    queryKey: ["sessionChoiceLines"],
    queryFn: async () => {
      const response = await authenticatedFetch("/v1/me/usage/lines?running=1");
      if (!response.ok) throw new Error("Could not load quota lines.");
      return (await response.json()) as { tiles: Record<string, ChoiceLine> };
    },
    retry: false,
    refetchInterval: 60_000,
  });
}
