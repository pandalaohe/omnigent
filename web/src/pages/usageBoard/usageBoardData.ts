// Usage board reads: one owner-scoped overview, polled while the page is
// visible. The wire shape is the server's own view contract
// (``omnigent/usage/contract.py``), read as-is so a field the server adds
// arrives without a mapping to keep in step.

import { useQuery } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/identity";

export interface UsageWindowView {
  id: string;
  kind: string;
  label: string | null;
  group: string | null;
  percent_used: number | null;
  remaining_pct: number | null;
  window_minutes: number | null;
  resets_at: number | null;
  time_left_s: number | null;
  pace_pct: number | null;
  state: string;
  ahead_pts: number | null;
  used_value: number | null;
  limit_value: number | null;
  unit: string | null;
  past_reset: boolean;
}

export interface UsageHostView {
  host_id: string;
  name: string;
  read_at: number | null;
  windows: UsageWindowView[];
}

export interface UsageAccountMark {
  level?: string;
  requests?: number;
  failed?: number;
  rate?: number;
}

export interface UsageAccountPeriod {
  start: number;
  days: number;
  moved_from: number | null;
}

export interface UsageAccountView {
  id: string;
  kind: "native" | "account";
  display_name: string;
  plan: string | null;
  band: string | null;
  product: string | null;
  details_url: string | null;
  renews_at: number | null;
  state: string;
  reason: string | null;
  deciding_window: string | null;
  pace_pct: number | null;
  read_at: number | null;
  age_s: number | null;
  freshness: "fresh" | "old" | "none";
  mark: UsageAccountMark | null;
  period: UsageAccountPeriod | null;
  windows: UsageWindowView[];
  hosts: UsageHostView[];
  models?: { id: string }[];
}

export interface UsageOverview {
  schema_version: number;
  generated_at: number;
  tiers: { tight_below_pct?: number; low_below_pct?: number; floor_pct?: number | null };
  accounts: UsageAccountView[];
  agents: Record<string, Record<string, unknown>>;
}

/** One in-flight poll interval; the page refetches only while visible. */
export const USAGE_BOARD_POLL_MS = 60_000;

export class UsageBoardUnavailableError extends Error {
  constructor() {
    super("The usage module is off on this server.");
    this.name = "UsageBoardUnavailableError";
  }
}

async function readOverview(): Promise<UsageOverview> {
  const response = await authenticatedFetch("/v1/me/usage");
  if (response.status === 404) throw new UsageBoardUnavailableError();
  if (!response.ok) {
    throw new Error(`Usage read failed (${response.status})`);
  }
  return (await response.json()) as UsageOverview;
}

export function useUsageOverview(enabled: boolean) {
  return useQuery({
    queryKey: ["usageBoard", "overview"],
    queryFn: readOverview,
    enabled,
    // Poll while the tab is visible; a hidden tab keeps the last reading and
    // refetches once when it comes back.
    refetchInterval: () => (document.visibilityState === "visible" ? USAGE_BOARD_POLL_MS : false),
    refetchOnWindowFocus: true,
    staleTime: 0,
    retry: false,
  });
}

/** Remove one reported account, with its reading. Native tiles have none. */
export async function deleteUsageAccount(accountId: string): Promise<void> {
  const response = await authenticatedFetch(
    `/v1/me/usage/accounts/${encodeURIComponent(accountId)}`,
    { method: "DELETE" },
  );
  if (!response.ok && response.status !== 404) {
    throw new Error(`Could not remove the account (${response.status})`);
  }
}
