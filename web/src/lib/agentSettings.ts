import type { Host } from "@/hooks/useHosts";
import type { RegistryItem } from "@/hooks/useAgentRegistry";
import type { HarnessCapabilityMap } from "@/lib/sessionDefaultModes";
import { callingDefaultParent, callingHarnessLabel } from "@/lib/callingDefaults";
import { harnessUnavailableReasonOnHost } from "@/lib/harnessSetup";

/** Offline hosts retain their last reported inventory. Unknown inventory stays visible. */
export function agentPresentOnHost(harness: string, host: Host | undefined): boolean {
  if (!host) return false;
  const reason = harnessUnavailableReasonOnHost(harness, host);
  return !["platform-unsupported", "binary-missing", "unconfigured"].includes(reason ?? "");
}

export function agentProductHarness(harness: string, capabilities?: HarnessCapabilityMap): string {
  const parent = callingDefaultParent(harness, capabilities);
  if (parent) return parent;
  const key = capabilities?.[harness]?.native_agent_key;
  return (
    (key &&
      Object.entries(capabilities ?? {}).find(
        ([id, capability]) =>
          id !== harness &&
          capability.native_agent_key === key &&
          capability.integration_mode?.startsWith("native-"),
      )?.[0]) ||
    harness
  );
}

/** Keep a product followed immediately by its SDK/RPC modes. */
export function groupAgentHarnesses(
  harnesses: readonly string[],
  capabilities?: HarnessCapabilityMap,
) {
  const rows = [...new Set(harnesses)].filter((row) => row !== "*");
  const roots = [...new Set(rows.map((row) => agentProductHarness(row, capabilities)))];
  return roots.flatMap((root) => [
    ...(rows.includes(root) ? [root] : []),
    ...rows.filter((row) => row !== root && agentProductHarness(row, capabilities) === root),
  ]);
}

export const agentSettingsPath = (harness: string, hostId?: string) =>
  `/settings/agents/${encodeURIComponent(harness)}${hostId ? `?host=${encodeURIComponent(hostId)}` : ""}`;

export const customAgentSettingsPath = (id: string) =>
  `/settings/agents/custom/${encodeURIComponent(id)}`;

export const AGENT_SETTINGS_TABLE_PATH = "/settings/agents#agent-settings-table";

export function agentSettingsHost(
  harness: string,
  hosts: readonly Host[],
  visibleHostId?: string,
): string | undefined {
  const reported = hosts.filter(
    (host) =>
      Object.hasOwn(host.configured_harnesses ?? {}, harness) && agentPresentOnHost(harness, host),
  );
  return reported.length === 1 ? reported[0].host_id : visibleHostId;
}

export function agentSettingsHarnessLabel(
  harness: string,
  capabilities: HarnessCapabilityMap,
  registeredLabel?: string,
): string {
  const label = registeredLabel ?? callingHarnessLabel(harness, capabilities);
  return (harness === "pi" || harness === "pi-rpc") && (label === "Pi" || label === harness)
    ? "Pi RPC"
    : label;
}

/** Badges are stored by catalog agent ID, which can arrive after the host inventory. */
export function harnessSettingsBadgeId(
  harness: string,
  products: Readonly<Record<string, RegistryItem>>,
): string | undefined {
  return Object.entries(products).find(([, item]) => item.harness === harness)?.[0];
}
