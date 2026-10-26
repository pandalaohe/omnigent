import { useState } from "react";
import { AgentPicker } from "@/components/AgentPicker";
import { useHarnessCapabilities } from "@/lib/agentLabels";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useHosts } from "@/hooks/useHosts";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import {
  layerId,
  layerShape,
  instructionRegistryItems,
  instructionGridAxes,
  hasInstructionText,
  useSaveInstructionLayer,
  type InstructionLayer,
  type InstructionRole,
  useInstructionLayers,
  useInstructionPreview,
  type LayerKey,
} from "@/hooks/useInstructionLayers";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode } from "@/lib/capabilities";
import { useSearchParams } from "@/lib/routing";
import {
  readInstructionGridPreferences,
  useInstructionGridPreferences,
} from "@/lib/instructionGridPreferences";
import { HostPicker } from "./instructions/HostPicker";
import { RemoveAxisDialog } from "./instructions/RemoveAxisDialog";
import { LayerEditor } from "./instructions/LayerEditor";
import { ChildSessionsPanel } from "./instructions/ChildSessionsPanel";
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
  const capabilities = useHarnessCapabilities();
  const [layout, setLayout] = useInstructionGridPreferences();
  const save = useSaveInstructionLayer();
  const [removal, setRemoval] = useState<{
    axis: "hosts" | "agents";
    id: string;
    name: string;
    layers: InstructionLayer[];
  } | null>(null);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [editor, setEditor] = useState<{
    key: LayerKey;
    title: string;
    initialText: string;
  } | null>(null);
  const session = params.get("session");
  const roleParam = params.get("role");
  const role: InstructionRole =
    roleParam === "mother" || roleParam === "child" ? roleParam : "route";
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
  const axes = instructionGridAxes(
    data.data ?? { data: [], agents: [], order: [], route_sizes: [], size_reminder_chars: 10000 },
    hosts.data ?? [],
    layout,
  );
  const removeFromLayout = (axis: "hosts" | "agents", id: string) => {
    const latest = readInstructionGridPreferences();
    setLayout({ ...latest, [axis]: latest[axis].filter((value) => value !== id) });
  };
  const remove = (axis: "hosts" | "agents", id: string, name: string) => {
    const layers = (axis === "hosts" ? axes.hostLayers : axes.agentLayers).get(id) ?? [];
    if (layers.length === 0) removeFromLayout(axis, id);
    else if (canEdit) {
      setRemoveError(null);
      setRemoval({ axis, id, name, layers });
    }
  };
  const confirmRemoval = async () => {
    if (!removal || removing || !canEdit) return;
    setRemoving(true);
    setRemoveError(null);
    try {
      for (const layer of removal.layers) {
        // eslint-disable-next-line no-await-in-loop -- Clear each revision before starting the next save.
        await save.mutateAsync({
          scope: layer.scope,
          agent_key: layer.agent_key,
          host_id: layer.host_id,
          text: "",
          expected_revision_id: layer.revision_id,
        });
        setRemoval((pending) =>
          pending
            ? {
                ...pending,
                layers: pending.layers.filter((item) => layerId(item) !== layerId(layer)),
              }
            : null,
        );
      }
      removeFromLayout(removal.axis, removal.id);
      setRemoval(null);
    } catch (error) {
      setRemoveError(error instanceof Error ? error.message : String(error));
    } finally {
      setRemoving(false);
    }
  };
  const pickerItems = instructionRegistryItems(
    data.data?.agents ?? [],
    capabilities,
    hosts.data ?? [],
  );

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
      layerShape(key) === "independent" && !hasInstructionText(existing?.text)
        ? [
            {
              scope: key.scope === "independent" ? "shared" : key.scope,
              agent_key: "",
              host_id: "",
            },
            {
              scope: key.scope === "independent" ? "agent" : key.scope,
              agent_key: key.agent_key,
              host_id: "",
            },
            {
              scope: key.scope === "independent" ? "host" : key.scope,
              agent_key: "",
              host_id: key.host_id,
            },
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
            .filter(hasInstructionText)
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
        <ChildSessionsPanel />
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
              <Tabs
                value={role}
                onValueChange={(value) =>
                  setParams((previous) => {
                    const next = new URLSearchParams(previous);
                    next.set("role", value);
                    return next;
                  })
                }
              >
                <TabsList aria-label="Instruction grid">
                  <TabsTrigger value="route">Shared instructions</TabsTrigger>
                  <TabsTrigger value="mother">Mother</TabsTrigger>
                  <TabsTrigger value="child">Child</TabsTrigger>
                </TabsList>
                <TabsContent value={role} className="mt-2">
                  <LayerGrid
                    role={role}
                    data={data.data}
                    hosts={axes.hosts}
                    allHosts={hosts.data ?? []}
                    agents={axes.agents}
                    agentLayers={axes.agentLayers}
                    hostLayers={axes.hostLayers}
                    onRemove={remove}
                    addAgent={
                      <AgentPicker
                        items={pickerItems}
                        taken={new Set(axes.agents.map((agent) => agent.key))}
                        onPick={(key) => {
                          const latest = readInstructionGridPreferences();
                          setLayout({ ...latest, agents: [...latest.agents, key] });
                        }}
                        title="Add agent"
                        description="Give one agent its own instruction text."
                        trigger={<Button variant="outline">Add agent</Button>}
                      />
                    }
                    addHost={
                      <HostPicker
                        hosts={hosts.data ?? []}
                        taken={new Set(axes.hosts.map((host) => host.host_id))}
                        onPick={(id) => {
                          const latest = readInstructionGridPreferences();
                          setLayout({ ...latest, hosts: [...latest.hosts, id] });
                        }}
                      />
                    }
                    canEdit={canEdit}
                    onEdit={edit}
                    onRoute={(host, agent) =>
                      route({
                        host,
                        agent,
                        ...(role === "route" ? {} : { child: role === "child" ? "1" : "0" }),
                        ...(role === "mother" ? { spawn: "1" } : {}),
                      })
                    }
                  />
                </TabsContent>
              </Tabs>
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
        {removal && (
          <RemoveAxisDialog
            name={removal.name}
            layers={removal.layers.map((layer) => ({
              id: layerId(layer),
              grid: layer.scope === "mother" || layer.scope === "child" ? layer.scope : "route",
              label:
                layerShape(layer) === "agent"
                  ? (data.data?.agents.find((agent) => agent.key === layer.agent_key)
                      ?.display_name ?? layer.agent_key)
                  : layerShape(layer) === "host"
                    ? (hosts.data?.find((host) => host.host_id === layer.host_id)?.name ??
                      layer.host_id)
                    : `${hosts.data?.find((host) => host.host_id === layer.host_id)?.name ?? layer.host_id} / ${data.data?.agents.find((agent) => agent.key === layer.agent_key)?.display_name ?? layer.agent_key}`,
              chars: layer.chars,
            }))}
            loading={removing}
            error={removeError}
            onCancel={() => setRemoval(null)}
            onConfirm={() => void confirmRemoval()}
          />
        )}
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
