import { useQuery } from "@tanstack/react-query";
import { PromptNotSent } from "@/components/PromptNotSent";
import type { InstructionLayers } from "@/hooks/useInstructionLayers";
import type { Host } from "@/hooks/useHosts";
import { listCallingDefaultCatalogs } from "@/lib/callingDefaultsApi";

export function StartupPromptNotices({
  agents,
  hosts,
}: {
  agents: InstructionLayers["agents"];
  hosts: readonly Host[];
}) {
  const catalogs = useQuery({
    queryKey: ["calling-default-catalogs"],
    queryFn: listCallingDefaultCatalogs,
  });
  const rows =
    catalogs.data?.flatMap((row) => {
      if (row.injects_prompt !== false) return [];
      const host = hosts.find((entry) => entry.host_id === row.host_id);
      const agent = agents.find((entry) => entry.harnesses?.includes(row.harness));
      return host
        ? [{ ...row, host, name: agent?.display_name ?? row.harness.replace(/^acp:/, "") }]
        : [];
    }) ?? [];
  if (!rows.length) return null;
  return (
    <section aria-label="Agents whose prompt is not sent" className="space-y-2">
      {rows.map((row) => (
        <div
          key={`${row.host_id}/${row.harness}`}
          className="flex flex-wrap items-center gap-2 text-sm"
        >
          <span>
            {row.name} · {row.host.name || row.host_id}
          </span>
          <PromptNotSent name={row.name} />
        </div>
      ))}
    </section>
  );
}
