import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { sortAgentsForDisplay } from "@/lib/agentGrouping";
import { harnessReadinessOnHost } from "@/lib/harnessSetup";
import {
  nativeCodingAgentForAvailableAgent,
  nativeCodingAgentForHarness,
} from "@/lib/nativeCodingAgents";

const MAX_PRIMARY = 3;
const PRIMARY_ORDER = ["claude", "cursor", "codex"];
const SECONDARY_ORDER = ["opencode", "pi"];

export interface RankHarnessRowsInput {
  entries: readonly AvailableAgent[];
  host: Host | null | undefined;
  recentHarnesses: readonly string[];
  hideUnconfigured: boolean;
  selectedId: string | null;
  promotedId: string | null;
}

export interface RankedHarnessRows {
  primary: AvailableAgent[];
  more: AvailableAgent[];
}

export function rankHarnessRows({
  entries,
  host,
  recentHarnesses,
  hideUnconfigured,
  selectedId,
  promotedId,
}: RankHarnessRowsInput): RankedHarnessRows {
  const visible: AvailableAgent[] = [];
  const usable: AvailableAgent[] = [];
  for (const entry of entries) {
    const readiness = harnessReadinessOnHost(entry.harness, host);
    if (
      entry.id !== selectedId &&
      hideUnconfigured &&
      !readiness.selectable &&
      readiness.fallbackRelevant
    )
      continue;
    visible.push(entry);
    if (readiness.selectable && readiness.reason !== "readiness-unknown") usable.push(entry);
  }

  const iconRank = (entry: AvailableAgent, order: readonly string[]): number => {
    const index = order.indexOf(nativeCodingAgentForAvailableAgent(entry)?.iconKind ?? "");
    return index < 0 ? order.length : index;
  };
  const recentIndex = (entry: AvailableAgent): number => {
    const native = nativeCodingAgentForAvailableAgent(entry);
    // Bare product keys are recency aliases, but the harness resolver only accepts harness ids.
    const index = recentHarnesses.findIndex((stored) =>
      native
        ? nativeCodingAgentForHarness(stored)?.key === native.key || stored === native.key
        : stored === entry.harness,
    );
    return index < 0 ? Number.POSITIVE_INFINITY : index;
  };

  const fixedOrder = sortAgentsForDisplay(usable).sort(
    (first, second) => iconRank(first, PRIMARY_ORDER) - iconRank(second, PRIMARY_ORDER),
  );
  const primary = fixedOrder
    .filter((entry) => Number.isFinite(recentIndex(entry)))
    .sort((first, second) => recentIndex(first) - recentIndex(second))
    .slice(0, MAX_PRIMARY);
  for (const entry of fixedOrder) {
    if (primary.length >= MAX_PRIMARY) break;
    if (!primary.some((ranked) => ranked.id === entry.id)) primary.push(entry);
  }
  const promoted = visible.find((entry) => entry.id === promotedId);
  if (promoted && !primary.some((entry) => entry.id === promoted.id)) primary.push(promoted);

  const primaryIds = new Set(primary.map((entry) => entry.id));
  const more = sortAgentsForDisplay(visible.filter((entry) => !primaryIds.has(entry.id))).sort(
    (first, second) => iconRank(first, SECONDARY_ORDER) - iconRank(second, SECONDARY_ORDER),
  );
  return { primary, more };
}
