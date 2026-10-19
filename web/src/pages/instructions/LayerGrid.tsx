import { Button } from "@/components/ui/button";
import type { Host } from "@/hooks/useHosts";
import { layerId, type InstructionLayers, type LayerKey } from "@/hooks/useInstructionLayers";

export function LayerGrid({
  data,
  hosts,
  agents,
  canEdit,
  onEdit,
  onRoute,
}: {
  data: InstructionLayers;
  hosts: Host[];
  agents: InstructionLayers["agents"];
  canEdit: boolean;
  onEdit: (key: LayerKey, title: string) => void;
  onRoute: (host: string, agent: string) => void;
}) {
  const layers = new Map(data.data.map((layer) => [layerId(layer), layer]));
  const cell = (key: LayerKey, title: string) => {
    const layer = layers.get(layerId(key));
    return (
      <button
        type="button"
        disabled={!canEdit || layer?.host_removed}
        onClick={() => onEdit(key, title)}
        className="w-full rounded-md px-3 py-2 text-left enabled:hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring disabled:cursor-default"
        aria-label={`Edit ${title}`}
      >
        <span className="block text-sm font-medium">
          {key.scope === "independent"
            ? layer?.text.trim()
              ? "Independent"
              : "Use stacked layers"
            : title}
        </span>
        <span className="text-xs text-muted-foreground">
          {layer?.text.trim() ? `${layer.chars.toLocaleString()} chars` : "No text"}
          {layer?.stale_layers?.length ? " · Changed sources" : ""}
        </span>
      </button>
    );
  };
  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2">
        {(["mother", "child"] as const).map((scope) => (
          <div className="rounded-lg border" key={scope}>
            {cell(
              { scope, agent_key: "", host_id: "" },
              scope === "mother" ? "Mother block" : "Child block",
            )}
          </div>
        ))}
      </div>
      <p className="text-sm text-muted-foreground">
        Shared → Agent → Host. An independent cell replaces all three on its route. Mother and child
        blocks apply afterwards.
      </p>
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full border-collapse text-left">
          <caption className="sr-only">Instruction layers by host and agent</caption>
          <thead>
            <tr className="border-b bg-muted/40">
              <th className="min-w-40 p-2">
                {cell({ scope: "shared", agent_key: "", host_id: "" }, "Shared")}
              </th>
              {agents.map((agent) => (
                <th key={agent.key} className="min-w-44 border-l p-2">
                  {cell({ scope: "agent", agent_key: agent.key, host_id: "" }, agent.display_name)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {hosts.map((host) => (
              <tr className="border-b last:border-b-0" key={host.host_id}>
                <th className="p-2 font-normal">
                  {cell({ scope: "host", agent_key: "", host_id: host.host_id }, host.name)}
                </th>
                {agents.map((agent) => {
                  const size = data.route_sizes.find(
                    (route) => route.host_id === host.host_id && route.agent_key === agent.key,
                  );
                  return (
                    <td className="border-l p-2 align-top" key={agent.key}>
                      {cell(
                        { scope: "independent", agent_key: agent.key, host_id: host.host_id },
                        `${host.name} / ${agent.display_name}`,
                      )}
                      <div className="flex flex-wrap items-center justify-between px-3">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => onRoute(host.host_id, agent.key)}
                        >
                          View route
                        </Button>
                        {size?.over_budget && (
                          <span className="text-xs font-medium">Over 10,000</span>
                        )}
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {hosts.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Connect a host to view routes. Shared, Agent and Role layers can still be edited.
        </p>
      )}
      {data.data.some((layer) => layer.host_removed) && (
        <section>
          <h3 className="mb-2 text-sm font-medium">Removed hosts</h3>
          <p className="text-xs text-muted-foreground">
            Their saved layers and history are retained.
          </p>
          {data.data
            .filter((layer) => layer.host_removed)
            .map((layer) => (
              <p className="mt-1 break-all text-sm" key={layerId(layer)}>
                {layer.host_id} · {layer.agent_key || "All agents"} · {layer.chars.toLocaleString()}{" "}
                chars
              </p>
            ))}
        </section>
      )}
    </div>
  );
}
