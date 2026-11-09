import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { customAgentsSettingsEnabled } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
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
import {
  nativeCodingAgentForAgentName,
  nativeCodingAgentForHarness,
} from "@/lib/nativeCodingAgents";
import { isAcpHarnessAgent, isSdkAgent } from "@/lib/agentGrouping";

export type SectionId = "yours" | "native" | "sdk" | "acp" | "builtin" | "other";
export type RegistryKey = string;

export interface RegistryItem {
  key: RegistryKey;
  aliasKeys?: RegistryKey[];
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

function formatHarnessFallbackLabel(harness: string): string {
  if (BRAIN_HARNESS_LABELS[harness]) return BRAIN_HARNESS_LABELS[harness];
  const native = nativeCodingAgentForHarness(harness);
  if (native) return native.displayName;
  if (harness === "pi-rpc") return "Pi RPC";
  if (harness === "acp" || harness.startsWith("acp:")) return SECTION_LABELS.acp;
  return harness;
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
    agentLabel:
      agent?.label ??
      nativeCodingAgentForAgentName(agentName)?.displayName ??
      (agentName === "pi-rpc" ? "Pi" : agentName || "Unknown"),
    harnessLabel: harnessItem?.label ?? (harness ? formatHarnessFallbackLabel(harness) : "Unknown"),
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
    const rowLabel = row.label;
    const section = harnessSection(capabilityFor(harness)?.integration_mode, harness);
    const existingSameKind = [...items.values()].find(
      (existing) =>
        existing.section === section && existing.label.toLowerCase() === rowLabel.toLowerCase(),
    );
    if (existingSameKind) {
      const aliasKey = `harness:${harness}`;
      if (aliasKey !== existingSameKind.key && !existingSameKind.aliasKeys?.includes(aliasKey)) {
        existingSameKind.aliasKeys = [...(existingSameKind.aliasKeys ?? []), aliasKey];
      }
      existingSameKind.deployed =
        existingSameKind.deployed || reportedOnAnyHost(harness, capabilityFor(harness), hosts);
      continue;
    }
    add({
      key: `harness:${harness}`,
      label: rowLabel,
      section,
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
    // The same catalog id can appear in scope=user, and same-name records of the
    // same kind collapse into a single settings row.
    if (source !== "builtin" || !productHarnessForAgent(agent as AvailableAgent)) {
      if (items.has(agent.id)) continue;
      const harness = agent.harness ?? null;
      const label =
        source === "installed" && "name" in agent
          ? capitalizeAgentName(agent.name)
          : agent.display_name;
      const section: SectionId = source === "builtin" ? "builtin" : "yours";
      const existingSameKind = [...items.values()].find(
        (existing) =>
          existing.section === section &&
          existing.harness === harness &&
          existing.label.toLowerCase() === label.toLowerCase(),
      );
      if (existingSameKind) {
        if (agent.id !== existingSameKind.key && !existingSameKind.aliasKeys?.includes(agent.id)) {
          existingSameKind.aliasKeys = [...(existingSameKind.aliasKeys ?? []), agent.id];
        }
        existingSameKind.deployed =
          existingSameKind.deployed || reportedOnAnyHost(harness, capabilityFor(harness), hosts);
        continue;
      }
      add({
        key: agent.id,
        name: "name" in agent ? agent.name : agent.display_name,
        label,
        section,
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
      const harnessLabel = item.harness
        ? (harnessCatalog.labels[item.harness] ??
          productLabels.get(item.harness) ??
          capabilities[item.harness]?.label ??
          formatHarnessFallbackLabel(item.harness))
        : "Agent";
      item.label = `${item.label} · ${harnessLabel} · ${item.source === "library" ? "library" : "installed"}`;
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

/** The catalog id a product row stands for, which is what per-agent preferences such as badges key on. */
export function productIdFor(
  item: RegistryItem | undefined,
  registry: { legacyProducts: Record<string, RegistryItem> },
): string | null {
  if (!item || !item.key.startsWith("harness:")) return item?.key ?? null;
  return (
    Object.values(registry.legacyProducts).find((product) => product.harness === item.harness)
      ?.key ?? null
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
    isLoading:
      catalog.isPending ||
      harnesses.isPending ||
      (user.isPending && user.isEnabled) ||
      (library.isPending && library.isEnabled),
    isError,
    retry: () => {
      void Promise.all([catalog.refetch(), harnesses.refetch(), user.refetch(), library.refetch()]);
    },
  };
}

export const AGENTS_WITH_SESSIONS_QUERY_KEY = ["agents-with-sessions"] as const;

/** Agent ids and template labels on at least one session, or an error. */
export function useAgentsWithSessions() {
  return useQuery({
    queryKey: AGENTS_WITH_SESSIONS_QUERY_KEY,
    queryFn: async ({ signal }) => {
      const response = await authenticatedFetch("/v1/agents/with-sessions", { signal });
      if (!response.ok) throw await apiErrorFromResponse(response);
      const body = (await response.json()) as {
        agent_ids?: unknown;
        template_by_agent_id?: Record<string, string>;
      };
      if (
        !Array.isArray(body.agent_ids) ||
        body.agent_ids.some((value) => typeof value !== "string")
      ) {
        throw new Error("The server returned an invalid session list.");
      }
      return Object.assign(new Set(body.agent_ids as string[]), {
        templateByAgentId: body.template_by_agent_id ?? {},
      });
    },
    staleTime: 30_000,
    retry: false,
  });
}

/** Every key a registry row represents, including collapsed same-kind ids. */
export function representedRegistryKeys(
  items: readonly RegistryItem[],
  key: RegistryKey,
): RegistryKey[] {
  const item = items.find(
    (candidate) => candidate.key === key || candidate.aliasKeys?.includes(key),
  );
  return item ? [item.key, ...(item.aliasKeys ?? [])] : [key];
}

/** Map session agent ids (and template labels) to the registry rows they name. */
export function rowsForAgentIds(
  items: readonly RegistryItem[],
  legacyProducts: Record<string, RegistryItem>,
  agentIds: ReadonlySet<string>,
): Set<RegistryKey> {
  const byKey = new Map<string, RegistryItem>();
  for (const item of items) {
    byKey.set(item.key, item);
    for (const aliasKey of item.aliasKeys ?? []) {
      byKey.set(aliasKey, item);
    }
  }
  const keys = new Set<RegistryKey>();
  for (const agentId of agentIds) {
    const legacy = legacyProducts[agentId];
    const item =
      byKey.get(agentId) ??
      (legacy?.harness ? byKey.get(`harness:${legacy.harness}`) : undefined) ??
      legacy;
    if (item) keys.add(item.key);
  }
  return keys;
}
