import type { ReactElement, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { BuiltinPromptEditor } from "@/components/BuiltinPromptEditor";
import { PromptNotSent } from "@/components/PromptNotSent";
import {
  listCallingDefaultCatalogs,
  type CallingDefaultsCatalogRow,
} from "@/lib/callingDefaultsApi";
import { ArrowRightIcon } from "lucide-react";
import { AgentNameTag } from "@/components/AgentNameTag";
import { LayeredTable } from "@/components/LayeredTable";
import { Button } from "@/components/ui/button";
import {
  useInstructionLayers,
  useInstructionPreview,
  type InstructionLayers,
} from "@/hooks/useInstructionLayers";
import type { Host } from "@/hooks/useHosts";
import { agentPresentOnHost, agentProductHarness, agentSettingsPath } from "@/lib/agentSettings";
import { missingCellText } from "@/lib/layeredValues";
import { harnessAccountKey } from "@/lib/nativeHarnessModes";
import { Link } from "@/lib/routing";
import type { HarnessCapabilityMap } from "@/lib/sessionDefaultModes";

function instructionAgent(
  data: InstructionLayers | undefined,
  harness: string,
  capabilities?: HarnessCapabilityMap,
) {
  const product = agentProductHarness(harness, capabilities);
  return (
    data?.agents.find((agent) => agent.harnesses?.includes(harness)) ??
    data?.agents.find(
      (agent) =>
        agent.harnesses?.includes(product) ||
        agent.key === capabilities?.[product]?.native_agent_key,
    )
  );
}

function startupPromptsPath(agentKey: string, hostId: string) {
  return `/settings/instructions?${new URLSearchParams({ view: "route", agent: agentKey, host: hostId })}`;
}

function ProviderValue({
  harness,
  host,
  capabilities,
}: {
  harness: string;
  host: Host;
  capabilities: HarnessCapabilityMap;
}) {
  if (host.gateway_inference?.[harness] === true) return "Unity Gateway";
  const account =
    host.harness_accounts?.[harnessAccountKey(agentProductHarness(harness, capabilities))];
  if (!account) return "Not reported";
  if (account.kind === "providers")
    return account.providers?.map((provider) => provider.label).join(", ") || "Not reported";
  if (account.kind === "api_key") return "API key";
  if (account.kind === "chatgpt" || account.kind === "subscription") return "Personal subscription";
  return account.kind === "none" ? "Not signed in" : "Not reported";
}

export function AgentContextTable({
  field,
  hosts,
  harnesses,
  names,
  agentIds,
  capabilities,
}: {
  field: "prompt" | "provider";
  hosts: readonly Host[];
  harnesses: readonly string[];
  names: Readonly<Record<string, string>>;
  agentIds: Readonly<Record<string, string>>;
  capabilities: HarnessCapabilityMap;
}) {
  return field === "prompt" ? (
    <PromptTable
      hosts={hosts}
      harnesses={harnesses}
      names={names}
      agentIds={agentIds}
      capabilities={capabilities}
    />
  ) : (
    <ContextTable
      field={field}
      hosts={hosts}
      harnesses={harnesses}
      names={names}
      agentIds={agentIds}
      capabilities={capabilities}
    />
  );
}

type ContextProps = Parameters<typeof AgentContextTable>[0];
function PromptTable(props: Omit<ContextProps, "field">) {
  const layers = useInstructionLayers();
  const catalogs = useQuery({
    queryKey: ["calling-default-catalogs"],
    queryFn: listCallingDefaultCatalogs,
  });
  return (
    <>
      {layers.isError && (
        <p role="alert" className="text-sm text-destructive">
          Could not load prompts.{" "}
          <Button variant="link" onClick={() => void layers.refetch()}>
            Retry
          </Button>
        </p>
      )}
      <ContextTable
        {...props}
        field="prompt"
        layers={layers.data}
        loading={layers.isPending}
        catalogs={catalogs.data}
      />
    </>
  );
}

function ContextTable({
  field,
  hosts,
  harnesses,
  names,
  agentIds,
  capabilities,
  layers,
  loading,
  catalogs,
}: ContextProps & {
  layers?: InstructionLayers;
  loading?: boolean;
  catalogs?: CallingDefaultsCatalogRow[];
}) {
  const dependent = (harness: string) =>
    agentProductHarness(harness, capabilities) !== harness &&
    harnesses.includes(agentProductHarness(harness, capabilities));
  return (
    <LayeredTable
      label={`${field === "prompt" ? "Prompt" : "Provider"} by agent and host`}
      cornerLabel="Agent"
      readOnly
      rows={harnesses.map((harness) => ({
        id: harness,
        label: names[harness] ?? harness,
        content: (
          <span className={dependent(harness) ? "pl-4" : undefined}>
            <AgentNameTag
              agentId={agentIds[harness] ?? null}
              name={names[harness] ?? harness}
              harness={harness}
              markOnly={dependent(harness)}
            />
            {dependent(harness) && <span className="sr-only">{names[harness] ?? harness}</span>}
          </span>
        ),
        action: (
          <Link
            to={agentSettingsPath(harness, hosts.length === 1 ? hosts[0].host_id : undefined)}
            aria-label={`${names[harness] ?? harness} details`}
            componentId="settings.agents.detail"
            className="flex min-h-11 w-11 shrink-0 items-center justify-center rounded-sm text-muted-foreground hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ArrowRightIcon className="size-4" aria-hidden />
          </Link>
        ),
      }))}
      columns={hosts.map((host) => ({ id: host.host_id, label: host.name || host.host_id }))}
      renderEditor={(_at, trigger: ReactElement) => (
        <div className="flex min-h-11 min-w-0 flex-col justify-center px-2.5 py-1.75">
          {(trigger.props as { children: ReactNode }).children}
        </div>
      )}
      renderCell={(harness, hostId) => {
        const host = hosts.find((candidate) => candidate.host_id === hostId)!;
        if (!agentPresentOnHost(harness, host))
          return { content: null, unavailable: missingCellText(harness, host.platform) };
        if (field === "provider")
          return {
            content: <ProviderValue harness={harness} host={host} capabilities={capabilities} />,
          };
        if (
          catalogs?.some(
            (row) =>
              row.host_id === hostId && row.harness === harness && row.injects_prompt === false,
          )
        )
          return { content: <PromptNotSent name={names[harness] ?? harness} /> };
        const agent = instructionAgent(layers, harness, capabilities);
        const size =
          agent &&
          layers?.route_sizes.find(
            (route) => route.agent_key === agent.key && route.host_id === hostId,
          );
        return {
          content: agent ? (
            <Link
              to={startupPromptsPath(agent.key, hostId)}
              componentId="settings.agents.prompt"
              className="underline underline-offset-4"
            >
              {size ? `${size.total_chars.toLocaleString()} chars` : "View prompt"}
            </Link>
          ) : loading ? (
            "Loading…"
          ) : (
            "Not available"
          ),
        };
      }}
    />
  );
}

/** Composed layers stay read-only here; Startup prompts owns their editor. */
export function AgentPromptCard({
  harness,
  host,
  capabilities,
  agentId,
  name,
}: {
  harness: string;
  host: Host;
  capabilities?: HarnessCapabilityMap;
  agentId?: string;
  name?: string;
}) {
  const layers = useInstructionLayers();
  const catalogs = useQuery({
    queryKey: ["calling-default-catalogs"],
    queryFn: listCallingDefaultCatalogs,
    enabled: harness.startsWith("acp:"),
  });
  const notSent = catalogs.data?.some(
    (row) =>
      row.host_id === host.host_id && row.harness === harness && row.injects_prompt === false,
  );
  const mode = capabilities?.[harness]?.integration_mode;
  const editable = agentId && (mode === "sdk-in-process" || mode === "cli-subprocess");
  const agent = instructionAgent(layers.data, harness, capabilities);
  const preview = useInstructionPreview(
    agent ? new URLSearchParams({ host_id: host.host_id, agent_key: agent.key }) : null,
  );
  if (!agent && !notSent && !editable) return null;
  return (
    <section className="space-y-3" aria-label="Prompt">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-ui font-medium">Prompt</h2>
        {agent && !notSent && (
          <Button asChild variant="link" size="sm">
            <Link
              to={startupPromptsPath(agent.key, host.host_id)}
              componentId="settings.agents.edit_prompt"
            >
              Edit in Startup prompts
            </Link>
          </Button>
        )}
      </div>
      {notSent && <PromptNotSent name={name ?? agent?.display_name ?? harness} />}
      {editable && <BuiltinPromptEditor key={agentId} agentId={agentId} />}
      {agent && !notSent && preview.isPending && (
        <p className="text-sm text-muted-foreground">Loading prompt…</p>
      )}
      {preview.isError && (
        <p role="alert" className="text-sm text-destructive">
          Could not load prompt.{" "}
          <Button variant="link" onClick={() => void preview.refetch()}>
            Retry
          </Button>
        </p>
      )}
      {!notSent &&
        preview.data?.parts
          .filter(
            (part) =>
              (part.chars > 0 || part.text === null) && (!editable || part.kind !== "author"),
          )
          .map((part) => (
            <details
              key={JSON.stringify([part.kind, part.scope, part.agent_key, part.host_id])}
              className="border-b border-border py-2"
            >
              <summary className="cursor-pointer text-sm">
                {part.scope ?? part.kind.replaceAll("_", " ")} ·{" "}
                {part.text === null ? "Set at launch" : `${part.chars.toLocaleString()} chars`}
                {!part.sent ? " · Not sent" : ""}
              </summary>
              {part.text && (
                <pre className="mt-2 whitespace-pre-wrap wrap-anywhere text-sm">{part.text}</pre>
              )}
            </details>
          ))}
    </section>
  );
}
