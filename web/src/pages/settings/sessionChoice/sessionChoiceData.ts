import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { UsageAccountView } from "@/pages/usageBoard/usageBoardData";

export interface ChoiceLink {
  agent: string;
  model?: string | null;
  effort?: string | null;
  meter?: string | null;
  maxRunning?: number | null;
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
  corrected?: string[];
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
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `Could not save session choice (${response.status}). Reload and try again.`,
    );
  return data as T;
}
export function useSessionChoiceDocument({ enabled = true }: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: ["sessionChoice"],
    queryFn: () => choiceRequest<ChoiceEnvelope>(),
    enabled,
    retry: false,
  });
}
export function useChoiceMeters(hostId: string) {
  return useQuery({
    queryKey: ["sessionChoiceMeters", hostId],
    queryFn: () => choiceRequest<ChoiceMeters>(`/meters?host_id=${encodeURIComponent(hostId)}`),
    enabled: Boolean(hostId),
    retry: false,
    refetchInterval: 60_000,
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
