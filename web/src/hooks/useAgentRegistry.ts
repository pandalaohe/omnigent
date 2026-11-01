import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { customAgentsSettingsEnabled } from "@/lib/capabilities";
import {
  AGENT_CATALOG_QUERY_KEY,
  AVAILABLE_AGENTS_STALE_MS,
  USER_AGENTS_QUERY_KEY,
  fetchAgentCatalog,
  fetchUserAgents,
  type AvailableAgent,
} from "@/hooks/useAvailableAgents";
import { useHosts, type Host } from "@/hooks/useHosts";
import {
  BRAIN_HARNESS_LABELS,
  capitalizeAgentName,
  fetchHarnessCatalog,
  type HarnessCatalog,
  type HarnessCapability,
} from "@/lib/agentLabels";
import { useCustomAgents, type CustomAgent } from "@/lib/customAgentsApi";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import { isAcpHarnessAgent, isSdkAgent } from "@/lib/agentGrouping";

export type SectionId = "yours" | "native" | "sdk" | "acp" | "builtin" | "other";
export type RegistryKey = string;

export interface RegistryItem {
  key: RegistryKey;
  label: string;
  name?: string;
  section: SectionId;
  harness: string | null;
  description: string | null;
  source: "builtin" | "installed" | "library";
  deployed: boolean;
}

export const SECTION_LABELS: Record<SectionId, string> = {
  yours: "Your agents",
  native: "Native CLI",
  sdk: "SDK",
  acp: "ACP",
  builtin: "Built-in agents",
  other: "Other",
};

export const SECTION_GLOSSES: Partial<Record<SectionId, string>> = {
  native: "The vendor's own command-line app, run in a terminal",
  sdk: "Runs in chat through the vendor's SDK or headless CLI",
  acp: "Any CLI that speaks the Agent Client Protocol",
  builtin: "Omnigent agents that plan and hand work to a team",
};

export const SECTION_ORDER: SectionId[] = ["yours", "native", "sdk", "acp", "builtin", "other"];

export interface RegistryHarnessOption {
  id: string;
  label: string;
  section: SectionId;
  deployed: boolean;
}

/** Harness menu rows; an allowed list keeps brain-only menus within their catalog. */
export function registryHarnessOptions(
  items: readonly RegistryItem[],
  fallbackLabels: Record<string, string> = {},
  allowedIds?: readonly string[],
): RegistryHarnessOption[] {
  const harnessItems = items.filter((item) => item.key.startsWith("harness:") && item.harness);
  const byId = new Map(harnessItems.map((item) => [item.harness!, item]));
  const ids = allowedIds ?? harnessItems.map((item) => item.harness!);
  return [...new Set(ids)].flatMap((id) => {
    const item = byId.get(id);
    if (allowedIds && item?.section === "native") return [];
    return [
      {
        id,
        label: item?.label ?? fallbackLabels[id] ?? id,
        section: item?.section ?? "other",
        deployed: item?.deployed ?? true,
      },
    ];
  });
}

/** Resolve display names without changing the stored source identity. */
export function registrySourceLabels(
  items: readonly RegistryItem[],
  agentName: string | null | undefined,
  harness: string | null | undefined,
): { agentLabel: string; harnessLabel: string } {
  const agent = items.find((item) => item.name === agentName && item.harness === harness);
  const harnessItem = harness ? items.find((item) => item.key === `harness:${harness}`) : undefined;
  return {
    agentLabel: agent?.label ?? (agentName || "Unknown"),
    harnessLabel:
      harnessItem?.label ?? (harness ? (BRAIN_HARNESS_LABELS[harness] ?? harness) : "Unknown"),
  };
}

export function harnessSection(mode: string | null | undefined, harness: string | null): SectionId {
  if (mode === "native-tui" || mode === "native-server") return "native";
  if (mode === "sdk-in-process" || mode === "cli-subprocess") return "sdk";
  if (mode === "acp-subprocess") return "acp";
  // Older servers have no capability map; use the existing native catalog.
  if (harness && nativeCodingAgentForHarness(harness)) return "native";
  if (harness?.startsWith("acp:")) return "acp";
  return "other";
}

export function reportedOnAnyHost(
  harness: string | null,
  capability: HarnessCapability | undefined,
  hosts: readonly Host[],
): boolean {
  // A harness absent from the server catalog cannot be assessed reliably.
  if (!harness || !capability) return true;
  const reports = hosts
    .filter((host) => !host.sandbox_provider)
    .map((host) => host.configured_harnesses)
    .filter((map): map is Record<string, boolean | string> => !!map && Object.keys(map).length > 0);
  if (reports.length === 0) return true;
  return reports.some((map) => {
    const state = map[harness];
    return (
      state === true ||
      ((state === "needs-auth" || state === "version-too-low") &&
        capability?.integration_mode !== "sdk-in-process")
    );
  });
}

function productHarnessForAgent(agent: AvailableAgent): string | null {
  if (agent.builtin === false) return null;
  if (agent.product_of !== undefined) return agent.product_of ?? null;
  const harness = agent.harness;
  if (!harness) return null;
  const native = nativeCodingAgentForHarness(harness);
  if (native?.agentName === agent.name || isSdkAgent(agent) || isAcpHarnessAgent(agent))
    return harness;
  return null;
}

export function buildRegistry(
  catalog: readonly AvailableAgent[],
  userAgents: readonly AvailableAgent[],
  library: readonly CustomAgent[],
  harnessCatalog: HarnessCatalog,
  hosts: readonly Host[],
): RegistryItem[] {
  const capabilities = harnessCatalog.capabilitiesByHarness;
  const userAgentIds = new Set(userAgents.map((agent) => agent.id));
  const capabilityFor = (harness: string | null): HarnessCapability | undefined => {
    if (!harness) return undefined;
    return (
      capabilities[harness] ??
      harnessCatalog.rows.find((row) => row.id === harness)?.capabilities ??
      (harness.startsWith("acp:") ? capabilities.acp : undefined)
    );
  };
  const productLabels = new Map<string, string>();
  const items = new Map<string, RegistryItem>();
  const add = (item: RegistryItem) => items.set(item.key, item);
  const productRows = catalog
    .filter((agent) => !userAgentIds.has(agent.id))
    .map((agent) => ({ agent, harness: productHarnessForAgent(agent) }))
    .filter((row) => !!row.harness);
  for (const { agent, harness: productHarness } of productRows) {
    const harness = productHarness!;
    productLabels.set(harness, agent.display_name);
    add({
      key: `harness:${harness}`,
      label: agent.display_name,
      name: agent.name,
      section: harnessSection(
        capabilityFor(harness)?.integration_mode ?? agent.integration_mode,
        harness,
      ),
      harness,
      description: agent.description,
      source: "builtin",
      deployed: reportedOnAnyHost(harness, capabilityFor(harness), hosts),
    });
  }
  for (const row of harnessCatalog.rows) {
    if (!row.id || !row.label || items.has(`harness:${row.id}`)) continue;
    const harness = row.id;
    add({
      key: `harness:${harness}`,
      label: row.label,
      section: harnessSection(capabilityFor(harness)?.integration_mode, harness),
      harness,
      description: null,
      source: "builtin",
      deployed: reportedOnAnyHost(harness, capabilityFor(harness), hosts),
    });
  }
  const allAgents = [
    ...catalog.map(
      (agent) =>
        ({
          agent,
          source: agent.builtin === false || userAgentIds.has(agent.id) ? "installed" : "builtin",
        }) as const,
    ),
    ...userAgents.map((agent) => ({ agent, source: "installed" }) as const),
    ...library.map((agent) => ({
      agent: {
        id: agent.id,
        display_name: agent.name,
        description: agent.description,
        harness: agent.harness,
      },
      source: "library" as const,
    })),
  ];
  for (const { agent, source } of allAgents) {
    // The same catalog id can appear in scope=user; preserve one stable identity.
    if (source !== "builtin" || !productHarnessForAgent(agent as AvailableAgent)) {
      if (items.has(agent.id)) continue;
      const harness = agent.harness ?? null;
      add({
        key: agent.id,
        name: "name" in agent ? agent.name : agent.display_name,
        label:
          source === "installed" && "name" in agent
            ? capitalizeAgentName(agent.name)
            : agent.display_name,
        section: source === "builtin" ? "builtin" : "yours",
        harness,
        description: agent.description ?? null,
        source,
        deployed: reportedOnAnyHost(harness, capabilityFor(harness), hosts),
      });
    }
  }
  const names = new Map<string, number>();
  for (const item of items.values()) names.set(item.label, (names.get(item.label) ?? 0) + 1);
  for (const item of items.values()) {
    if (names.get(item.label)! > 1 && item.section === "yours") {
      item.label = `${item.label} · ${harnessCatalog.labels[item.harness ?? ""] ?? productLabels.get(item.harness ?? "") ?? item.harness ?? "Agent"} · ${item.source === "library" ? "library" : "installed"}`;
    }
  }
  return [...items.values()].sort(
    (a, b) =>
      SECTION_ORDER.indexOf(a.section) - SECTION_ORDER.indexOf(b.section) ||
      a.label.localeCompare(b.label),
  );
}

/** Product ids remain direct-session exceptions even though their picker row is the harness. */
export function buildLegacyProductRegistry(
  catalog: readonly AvailableAgent[],
  userAgents: readonly AvailableAgent[],
  items: readonly RegistryItem[],
): Record<string, RegistryItem> {
  const userIds = new Set(userAgents.map((agent) => agent.id));
  const byKey = new Map(items.map((item) => [item.key, item]));
  return Object.fromEntries(
    catalog.flatMap((agent) => {
      if (userIds.has(agent.id)) return [];
      const harness = productHarnessForAgent(agent);
      if (!harness) return [];
      const pickerItem = byKey.get(`harness:${harness}`);
      return [
        [
          agent.id,
          {
            key: agent.id,
            label: agent.display_name,
            name: agent.name,
            section: pickerItem?.section ?? harnessSection(agent.integration_mode, harness),
            harness,
            description: agent.description,
            source: "builtin" as const,
            deployed: pickerItem?.deployed ?? true,
          },
        ],
      ];
    }),
  );
}

export function useAgentRegistry() {
  const info = useServerInfo();
  const catalog = useQuery({
    queryKey: AGENT_CATALOG_QUERY_KEY,
    queryFn: fetchAgentCatalog,
    staleTime: AVAILABLE_AGENTS_STALE_MS,
  });
  const user = useQuery({
    queryKey: USER_AGENTS_QUERY_KEY,
    queryFn: fetchUserAgents,
    enabled: info !== "loading" && info.agent_install === true,
    staleTime: AVAILABLE_AGENTS_STALE_MS,
  });
  const library = useCustomAgents(customAgentsSettingsEnabled(info));
  const harnesses = useQuery({
    queryKey: ["harness-labels"],
    queryFn: fetchHarnessCatalog,
    staleTime: 30_000,
  });
  const hosts = useHosts();
  const items = useMemo(
    () =>
      buildRegistry(
        catalog.data?.agents ?? [],
        user.data ?? [],
        library.data ?? [],
        harnesses.data ?? {
          rows: [],
          capabilitiesByHarness: {},
          labels: {},
          setupSteps: {},
          acpHarnesses: new Set(),
          effortFamilies: {},
        },
        hosts.data ?? [],
      ),
    [catalog.data, user.data, library.data, harnesses.data, hosts.data],
  );
  const isError =
    catalog.isError ||
    harnesses.isError ||
    (user.isError && user.isEnabled) ||
    (library.isError && library.isEnabled);
  const legacyProducts = buildLegacyProductRegistry(
    catalog.data?.agents ?? [],
    user.data ?? [],
    items,
  );
  const legacyLabels = Object.fromEntries(
    Object.entries(legacyProducts).map(([key, item]) => [key, item.label]),
  );
  return {
    items,
    legacyProducts,
    legacyLabels,
    isLoading: catalog.isPending || harnesses.isPending,
    isError,
    retry: () => {
      void Promise.all([catalog.refetch(), harnesses.refetch(), user.refetch(), library.refetch()]);
    },
  };
}
