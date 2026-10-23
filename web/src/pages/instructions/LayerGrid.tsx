import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Host } from "@/hooks/useHosts";
import {
  hasInstructionText,
  layerId,
  type InstructionLayers,
  type LayerKey,
} from "@/hooks/useInstructionLayers";

export function LayerGrid({
  data,
  hosts,
  agents,
  allHosts,
  addAgent,
  addHost,
  agentLayers,
  hostLayers,
  onRemove,
  canEdit,
  onEdit,
  onRoute,
}: {
  data: InstructionLayers;
  hosts: Host[];
  allHosts: Host[];
  addAgent: ReactNode;
  addHost: ReactNode;
  agentLayers: Map<string, InstructionLayers["data"]>;
  hostLayers: Map<string, InstructionLayers["data"]>;
  onRemove: (axis: "hosts" | "agents", id: string, name: string) => void;
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
          {key.scope === "independent" ? "Independent" : title}
        </span>
        <span className="text-xs text-muted-foreground">
          {layer && hasInstructionText(layer.text)
            ? `${layer.chars.toLocaleString()} chars`
            : "No text"}
          {layer?.stale_layers?.length ? " · Changed sources" : ""}
        </span>
      </button>
    );
  };
  const remove = (axis: "hosts" | "agents", id: string, name: string) => {
    const held = (axis === "hosts" ? hostLayers : agentLayers).get(id)?.length;
    return (
      <Button
        size="sm"
        variant="ghost"
        aria-label={`Remove ${name} ${axis === "hosts" ? "row" : "column"}`}
        disabled={!!held && !canEdit}
        title={held && !canEdit ? "Only administrators can clear saved text." : undefined}
        onClick={() => onRemove(axis, id, name)}
      >
        Remove
      </Button>
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
                  {remove("agents", agent.key, agent.display_name)}
                </th>
              ))}
              <th className="border-l p-2">{addAgent}</th>
            </tr>
          </thead>
          <tbody>
            {hosts.map((host) => (
              <tr className="border-b last:border-b-0" key={host.host_id}>
                <th className="p-2 font-normal">
                  {cell({ scope: "host", agent_key: "", host_id: host.host_id }, host.name)}
                  {remove("hosts", host.host_id, host.name)}
                </th>
                {agents.map((agent) => {
                  const size = data.route_sizes.find(
                    (route) => route.host_id === host.host_id && route.agent_key === agent.key,
                  );
                  const independentKey: LayerKey = {
                    scope: "independent",
                    agent_key: agent.key,
                    host_id: host.host_id,
                  };
                  const independent = layers.get(layerId(independentKey));
                  return (
                    <td className="border-l p-2 align-top" key={agent.key}>
                      {independent &&
                      !independent.host_removed &&
                      hasInstructionText(independent.text) ? (
                        cell(
                          { scope: "independent", agent_key: agent.key, host_id: host.host_id },
                          `${host.name} / ${agent.display_name}`,
                        )
                      ) : (
                        <div className="px-3 py-2">
                          <span className="block text-sm font-medium">Stacked</span>
                          {canEdit && (
                            <Button
                              size="sm"
                              variant="ghost"
                              aria-label={`Make ${host.name} / ${agent.display_name} independent`}
                              onClick={() =>
                                onEdit(
                                  {
                                    scope: "independent",
                                    agent_key: agent.key,
                                    host_id: host.host_id,
                                  },
                                  `${host.name} / ${agent.display_name}`,
                                )
                              }
                            >
                              Make independent
                            </Button>
                          )}
                        </div>
                      )}
                      <div className="flex flex-wrap items-center justify-between px-3">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => onRoute(host.host_id, agent.key)}
                        >
                          View route
                        </Button>
                        {size && size.total_chars > data.size_reminder_chars && (
                          <Tooltip>
                            <TooltipTrigger
                              className="cursor-help rounded-sm text-xs font-medium text-warning focus-visible:outline-2 focus-visible:outline-ring"
                              aria-label={`${size.total_chars.toLocaleString()} instruction characters`}
                            >
                              {(size.total_chars / 1000).toFixed(1)}k
                            </TooltipTrigger>
                            <TooltipContent>
                              {size.total_chars.toLocaleString()} characters: largest of top-level
                              and child-session totals
                              {size.framework_unavailable
                                ? ". Built-in sections are unavailable and not counted."
                                : ", including built-in sections."}{" "}
                              Text is never truncated.
                            </TooltipContent>
                          </Tooltip>
                        )}
                      </div>
                    </td>
                  );
                })}
                <td className="border-l p-2" />
              </tr>
            ))}
            <tr>
              <th className="p-2 font-normal">{addHost}</th>
              {agents.map((agent) => (
                <td key={agent.key} className="border-l p-2" />
              ))}
              <td className="border-l p-2" />
            </tr>
          </tbody>
        </table>
      </div>
      {hosts.length === 0 && agents.length === 0 && (
        <p className="text-sm text-muted-foreground">
          Add an agent column or a host row to give one agent or one host its own text.
        </p>
      )}
      {allHosts.length === 0 && (
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
