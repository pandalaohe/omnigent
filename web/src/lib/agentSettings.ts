import type { Host } from "@/hooks/useHosts";
import type { HarnessCapabilityMap } from "@/lib/sessionDefaultModes";
import { callingDefaultParent } from "@/lib/callingDefaults";
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
