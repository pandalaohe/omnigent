import { useState } from "react";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useHosts } from "@/hooks/useHosts";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import {
  layerId,
  useInstructionLayers,
  useInstructionPreview,
  type LayerKey,
} from "@/hooks/useInstructionLayers";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode } from "@/lib/capabilities";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import { useSearchParams } from "@/lib/routing";
import { LayerEditor } from "./instructions/LayerEditor";
import { LayerGrid } from "./instructions/LayerGrid";
import { RoutePicker } from "./instructions/RoutePicker";
import { RouteView } from "./instructions/RouteView";

export function InstructionsPage() {
  const data = useInstructionLayers();
  const hosts = useHosts({ includeSandbox: true });
  const isAdmin = useIsAdmin();
  const info = useServerInfo();
  const canEdit = isAdmin || isSingleUserMode(info);
  const [params, setParams] = useSearchParams();
  const [showAll, setShowAll] = useState(false);
  const [editor, setEditor] = useState<{
    key: LayerKey;
    title: string;
    initialText: string;
  } | null>(null);
  const session = params.get("session");
  const view = session || params.get("view") === "route" ? "route" : "overview";
  const hostId = params.get("host") ?? hosts.data?.[0]?.host_id ?? "";
  const agentKey = params.get("agent") ?? data.data?.agents[0]?.key ?? "";
  const previewParams =
    view !== "route"
      ? null
      : session
        ? new URLSearchParams({ session_id: session })
        : hostId && agentKey
          ? new URLSearchParams({
              host_id: hostId,
              agent_key: agentKey,
              child: String(params.get("child") === "1"),
              peer: String(params.get("peer") !== "0"),
              ...(params.has("spawn") ? { spawn: String(params.get("spawn") !== "0") } : {}),
            })
          : null;
  const preview = useInstructionPreview(previewParams);
  const context = session ? preview.data?.route : undefined;
  const selectedHost = session ? (context?.host_id ?? "") : hostId;
  const selectedAgent = session ? (context?.agent_key ?? "") : agentKey;
  const configured = new Set<string | undefined>(
    (hosts.data ?? []).flatMap((host) =>
      Object.entries(host.configured_harnesses ?? {})
        .filter(([, state]) => state !== false)
        .map(([harness]) => nativeCodingAgentForHarness(harness)?.key)
        .filter(Boolean),
    ),
  );
  const agents =
    data.data?.agents.filter(
      (agent) => showAll || configured.size === 0 || configured.has(agent.key),
    ) ?? [];

  const route = (changes: Record<string, string>) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("host", selectedHost);
      next.set("agent", selectedAgent);
      if (session && context) {
        next.set("host", context.host_id ?? "");
        next.set("agent", context.agent_key ?? "");
        next.set("child", context.child ? "1" : "0");
        next.set("peer", context.peer ? "1" : "0");
        next.set("spawn", context.spawn ? "1" : "0");
      }
      next.delete("session");
      next.set("view", "route");
      Object.entries(changes).forEach(([key, value]) => next.set(key, value));
      return next;
    });
  const edit = (key: LayerKey, title: string) => {
    if (!canEdit) return;
    const existing = data.data?.data.find((layer) => layerId(layer) === layerId(key));
    const stack =
      key.scope === "independent" && !existing?.text.trim()
        ? [
            { scope: "shared", agent_key: "", host_id: "" },
            { scope: "agent", agent_key: key.agent_key, host_id: "" },
            { scope: "host", agent_key: "", host_id: key.host_id },
          ]
            .map(
              (source) =>
                data.data?.data.find(
                  (layer) =>
                    layer.scope === source.scope &&
                    layer.agent_key === source.agent_key &&
                    layer.host_id === source.host_id,
                )?.text,
            )
            .filter((text) => text?.trim())
            .join("\n\n")
        : undefined;
    setEditor({ key, title, initialText: stack ?? existing?.text ?? "" });
  };

  return (
    <PageScroll contentClassName="px-4 sm:px-8" extraBottom="2.5rem">
      <div className="mx-auto w-full max-w-5xl space-y-6">
        <header>
          <h1 className="text-2xl font-semibold">Instructions</h1>
          <p className="mt-2 max-w-prose text-sm leading-6 text-muted-foreground">
            See what a new session receives on a host and agent route. Manage shared instructions,
            add specific guidance, or make one route independent.
          </p>
        </header>
        {data.isError && (
          <p role="alert" className="text-sm text-destructive">
            {data.error.message}
          </p>
        )}
        {hosts.isError && (
          <p role="alert" className="text-sm text-destructive">
            Could not load your hosts. {hosts.error.message}
          </p>
        )}
        {data.isPending && (
          <p role="status" className="text-sm text-muted-foreground">
            Loading instruction layers…
          </p>
        )}
        {data.data && (
          <Tabs
            value={view}
            onValueChange={(value) =>
              setParams((previous) => {
                const next = new URLSearchParams(previous);
                next.set("view", value);
                if (value === "overview") next.delete("session");
                return next;
              })
            }
          >
            <TabsList>
              <TabsTrigger value="overview">Overview</TabsTrigger>
              <TabsTrigger value="route">Route total</TabsTrigger>
            </TabsList>
            <TabsContent value="overview" className="mt-4 space-y-4">
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={showAll}
                  onChange={(event) => setShowAll(event.target.checked)}
                />{" "}
                Show all agent products
              </label>
              <LayerGrid
                data={data.data}
                hosts={hosts.data ?? []}
                agents={agents}
                canEdit={canEdit}
                onEdit={edit}
                onRoute={(host, agent) => route({ host, agent })}
              />
            </TabsContent>
            <TabsContent value="route" className="mt-4 space-y-5">
              {session && (
                <p className="text-sm text-muted-foreground">
                  Previewing a new session with this session's configuration. This is not a record
                  of its original launch.
                </p>
              )}
              <div className="flex flex-col gap-3 sm:flex-row">
                <RoutePicker
                  label="Host"
                  items={(hosts.data ?? []).map((host) => ({
                    value: host.host_id,
                    label: host.name,
                  }))}
                  value={selectedHost}
                  onChange={(host) => route({ host })}
                />
                <RoutePicker
                  label="Agent"
                  items={data.data.agents.map((agent) => ({
                    value: agent.key,
                    label: agent.display_name,
                  }))}
                  value={selectedAgent}
                  onChange={(agent) => route({ agent })}
                />
              </div>
              <div className="flex flex-wrap items-center gap-5 text-sm">
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={context?.child ?? params.get("child") === "1"}
                    onChange={(event) => route({ child: event.target.checked ? "1" : "0" })}
                  />{" "}
                  Child session
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={
                      context?.spawn ??
                      (params.has("spawn")
                        ? params.get("spawn") !== "0"
                        : (preview.data?.route.spawn ?? true))
                    }
                    onChange={(event) => route({ spawn: event.target.checked ? "1" : "0" })}
                  />{" "}
                  Can open children
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={context?.peer ?? params.get("peer") !== "0"}
                    onChange={(event) => route({ peer: event.target.checked ? "1" : "0" })}
                  />{" "}
                  Peer messaging
                </label>
              </div>
              {canEdit && selectedHost && selectedAgent && (
                <Button
                  variant="outline"
                  onClick={() =>
                    edit(
                      { scope: "independent", host_id: selectedHost, agent_key: selectedAgent },
                      "Independent setting",
                    )
                  }
                >
                  Edit independent setting
                </Button>
              )}
              {preview.isPending && previewParams !== null && (
                <p role="status" className="text-sm text-muted-foreground">
                  Composing route…
                </p>
              )}
              {preview.isError && (
                <p role="alert" className="text-sm text-destructive">
                  {preview.error.message}
                </p>
              )}
              {previewParams === null && (
                <p className="text-sm text-muted-foreground">
                  Connect a host and select an agent to preview its instructions.
                </p>
              )}
              {preview.data && (
                <RouteView
                  key={previewParams?.toString()}
                  data={preview.data}
                  canEdit={canEdit}
                  onEdit={edit}
                  budget={data.data.size_reminder_chars}
                />
              )}
            </TabsContent>
          </Tabs>
        )}
        <p className="border-t pt-4 text-xs leading-5 text-muted-foreground">
          Changes apply to sessions started or relaunched after saving. Running terminal sessions
          keep their text until relaunched; SDK sessions and OpenCode may take the new text when
          they next reconnect to the server.
          {!canEdit ? " Only administrators can edit these layers." : ""}
        </p>
        {editor && canEdit && (
          <LayerEditor
            key={layerId(editor.key)}
            layerKey={editor.key}
            layer={data.data?.data.find((layer) => layerId(layer) === layerId(editor.key))}
            title={editor.title}
            initialText={editor.initialText}
            onClose={() => setEditor(null)}
          />
        )}
      </div>
    </PageScroll>
  );
}
