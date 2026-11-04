import { useQuery } from "@tanstack/react-query";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { listCallingDefaultCatalogs } from "@/lib/callingDefaultsApi";
import { canonicalNativeHarness } from "@/lib/nativeHarnessModes";
import type { UsageAccountView } from "@/pages/usageBoard/usageBoardData";
import { ChoicePolicyEditor } from "./ChoicePolicyEditor";
import type { SessionChoiceDocument } from "./sessionChoiceData";

export function ChoicePolicySettings({
  document,
  host,
  meters,
  products,
  onChange,
}: {
  document: SessionChoiceDocument;
  host: string;
  meters: UsageAccountView[];
  products: Record<string, string | null>;
  onChange: (document: SessionChoiceDocument) => void;
}) {
  const agents = useAvailableAgents();
  const hosts = useHosts();
  const catalogs = useQuery({
    queryKey: ["calling-defaults-catalogs"],
    queryFn: listCallingDefaultCatalogs,
    staleTime: 60_000,
    retry: false,
  });
  return (
    <ChoicePolicyEditor
      document={document}
      host={host}
      hosts={hosts.data ?? []}
      agents={(agents.data ?? []).map((agent) => ({
        ...agent,
        product: agent.harness
          ? (products[canonicalNativeHarness(agent.harness.replaceAll("_", "-"))] ?? null)
          : null,
      }))}
      meters={meters}
      catalogs={catalogs.data ?? []}
      onChange={onChange}
    />
  );
}
