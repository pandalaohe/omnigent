import { useMemo, useState, type ReactNode } from "react";
import { BotIcon, PlusIcon, SearchIcon, TrashIcon } from "lucide-react";
import { Link, useLocation, useNavigate } from "@/lib/routing";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { customAgentsSettingsEnabled } from "@/lib/capabilities";
import { useAcpHarnessIds, useHarnessLabels } from "@/lib/agentLabels";
import {
  nativeCodingAgentForAgentName,
  nativeCodingAgentForHarness,
} from "@/lib/nativeCodingAgents";
import { isAcpHarnessAgent } from "@/lib/agentGrouping";
import { Button } from "@/components/ui/button";
import { AgentSettingsEditor, AgentsSettings } from "@/components/AgentsSettings";
import { AGENT_SETTINGS_TABLE_PATH, customAgentSettingsPath } from "@/lib/agentSettings";
import { SettingsPager } from "@/components/SettingsPager";
import { AgentNameTag } from "@/components/AgentNameTag";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { CreateAgentForm } from "@/shell/CreateAgentDialog";
import { useSettingsRoute } from "@/shell/settingsNav";
import {
  useCustomAgents,
  useCustomAgent,
  useSaveCustomAgent,
  useDeleteCustomAgent,
  type ManagedAgent,
} from "@/hooks/useCustomAgents";
import { CustomAgentConfigForm } from "./CustomAgentConfigForm";
import {
  CUSTOM_AGENT_CREATE_ENTRY,
  CUSTOM_AGENT_DETAIL_ENTRY,
  CUSTOM_AGENTS_LIST_ENTRY,
} from "@/pages/settings/search/catalog/customAgents";
import { SettingsItem } from "@/pages/settings/search/SettingsItem";
import { settingsControlProps } from "@/pages/settings/search/settingsControl";
import {
  SettingsDataRegion,
  settingsOverlayProps,
} from "@/pages/settings/search/settingsOwnership";
import { BackButton } from "./HarnessCatalog";
import { ApiError } from "@/lib/sessionsApi";

const BASE = "/settings/agents#custom-agents";
const formatHarnessLabel = (
  harness: string | null | undefined,
  labels: Record<string, string>,
): string => {
  if (!harness) return "Unknown";
  return (
    labels[harness] ??
    nativeCodingAgentForHarness(harness)?.displayName ??
    (harness === "pi-rpc" ? "Pi RPC" : harness.startsWith("acp:") ? "ACP" : harness)
  );
};
const date = (value: number | null) =>
  value === null
    ? "—"
    : new Date(value * 1000).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      });

export function SettingsCustomAgentsSection() {
  const info = useServerInfo();
  const { agentId } = useSettingsRoute();
  if (agentId) return <InstalledAgentsSettingsSection />;
  return (
    <AgentsSettings
      renderInstalled={
        customAgentsSettingsEnabled(info)
          ? (pin) => <InstalledAgentsSettingsSection embedded renderPin={pin} />
          : undefined
      }
    />
  );
}

function InstalledAgentsSettingsSection({
  embedded = false,
  renderPin,
}: {
  embedded?: boolean;
  renderPin?: (id: string, name: string) => ReactNode;
}) {
  const info = useServerInfo();
  const enabled = customAgentsSettingsEnabled(info);
  const supported = enabled && info !== "loading" && info.agent_detail === true;
  const { agentId } = useSettingsRoute();
  const navigate = useNavigate();
  const fromAgentsTable = useLocation().state?.fromAgentsTable === true;
  const returnTo = fromAgentsTable ? AGENT_SETTINGS_TABLE_PATH : BASE;
  const returnLabel = fromAgentsTable ? "Agents" : "Custom agents";
  const { query: servers, pager: serverPager } = useCustomAgents("server", supported && !agentId);
  const { query: yours, pager: userPager } = useCustomAgents("user", supported && !agentId);
  const save = useSaveCustomAgent();
  const labels = useHarnessLabels(supported);
  const acpHarnesses = useAcpHarnessIds(supported);
  const [search, setSearch] = useState("");
  const detailId = agentId === "new" ? undefined : agentId;
  const detail = useCustomAgent(detailId, supported);

  const searchTerm = search.trim().toLowerCase();
  const { builtins, filtered } = useMemo(() => {
    const matches = (a: ManagedAgent) =>
      `${a.name} ${a.description ?? ""}`.toLowerCase().includes(searchTerm);
    return {
      builtins: (servers.data?.data ?? []).filter(
        (a) =>
          !(
            a.builtin &&
            (nativeCodingAgentForAgentName(a.name) ||
              isAcpHarnessAgent({
                harness: a.harness,
                acpHarness: acpHarnesses.has(a.harness ?? "") || undefined,
              }))
          ) &&
          (!embedded || !a.builtin) &&
          matches(a),
      ),
      filtered: (yours.data?.data ?? []).filter(matches),
    };
  }, [servers.data, yours.data, acpHarnesses, searchTerm, embedded]);
  const userHasPages = userPager.page > 0 || userPager.hasNext;
  const serverHasPages = serverPager.page > 0 || serverPager.hasNext;

  if (!enabled) return null;
  if (!supported) return <UnsupportedServer />;
  const back = (
    <BackButton label={returnLabel} to={returnTo} componentId="settings.custom_agents.back" />
  );
  if (agentId === "new") {
    return (
      <SettingsItem entry={CUSTOM_AGENT_CREATE_ENTRY} className="@container">
        {back}
        <h1 className="mb-6 text-2xl font-medium">{CUSTOM_AGENT_CREATE_ENTRY.label}</h1>
        <CreateAgentForm
          settingsEntry={CUSTOM_AGENT_CREATE_ENTRY}
          submitLabel={CUSTOM_AGENT_CREATE_ENTRY.parts.submit}
          notice="Reusing an installed agent's name replaces its configuration and updates its sessions. Use a new name to keep both agents."
          onCancel={() => navigate(BASE)}
          onCreate={async (input) => {
            const agent = await save.mutateAsync(input);
            navigate(customAgentSettingsPath(agent.id));
          }}
        />
      </SettingsItem>
    );
  }
  if (detailId) {
    return (
      <SettingsItem entry={CUSTOM_AGENT_DETAIL_ENTRY} className="@container">
        {!detail.data && back}
        {detail.isError && detail.data && (
          <p role="alert" className="mb-4 text-ui">
            Could not refresh this agent.{" "}
            <Button variant="link" onClick={() => void detail.refetch()}>
              Retry
            </Button>
          </p>
        )}
        {detail.isError && !detail.data ? (
          detail.error instanceof ApiError && [405, 501].includes(detail.error.status) ? (
            <UnsupportedServer />
          ) : (
            <p role="alert">
              Could not load this agent.{" "}
              <Button variant="link" onClick={() => void detail.refetch()}>
                Retry
              </Button>
            </p>
          )
        ) : detail.data ? (
          <AgentDetail
            key={detail.data.id}
            agent={detail.data}
            owned={detail.data.user_owned}
            editable={info.agent_config_edit === true}
            harness={formatHarnessLabel(detail.data.harness, labels)}
            returnLabel={returnLabel}
            returnTo={returnTo}
            onDeleted={() => navigate(returnTo)}
          />
        ) : (
          <>
            <h1 className="mb-6 text-2xl font-medium">Custom agent</h1>
            <p className="text-ui text-muted-foreground">
              {detail.isPending
                ? "Loading agent…"
                : "Agent not found. It may have been removed or belong to another user."}
            </p>
          </>
        )}
      </SettingsItem>
    );
  }
  return (
    <SettingsItem entry={CUSTOM_AGENTS_LIST_ENTRY} className="@container">
      {!embedded && (
        <h1 className="settings-page-title pb-6 text-2xl font-medium">Custom agents</h1>
      )}
      <div className="mb-6 flex items-center gap-2">
        <div className="flex h-8 min-w-0 flex-1 items-center gap-2 rounded-lg border border-border px-2.5">
          <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          <input
            {...settingsControlProps(CUSTOM_AGENTS_LIST_ENTRY, "search")}
            type="search"
            {...settingsControlProps(CUSTOM_AGENTS_LIST_ENTRY, "search")}
            placeholder="Search configurations…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="min-w-0 flex-1 bg-transparent text-ui outline-none placeholder:text-muted-foreground/50"
          />
        </div>
        <Button asChild size="sm">
          <Link
            {...settingsControlProps(CUSTOM_AGENTS_LIST_ENTRY, "create")}
            to={customAgentSettingsPath("new")}
            componentId="settings.custom_agents.create"
          >
            <PlusIcon />
            Create
          </Link>
        </Button>
      </div>
      {(userHasPages || serverHasPages) && (
        <p className="mb-4 text-xs text-muted-foreground">
          Search filters the current page in each list.
        </p>
      )}
      <section className="mb-8" aria-label="Your agents">
        {!embedded && (
          <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Yours
          </h2>
        )}
        {filtered.length > 0 && (
          <SettingsDataRegion
            entry={CUSTOM_AGENTS_LIST_ENTRY}
            region="yourAgents"
            className="overflow-x-auto"
          >
            <table className="w-full min-w-[36rem] text-left text-ui">
              <thead>
                <tr className="border-b border-border">
                  <th scope="col" className="h-10 px-2 font-medium">
                    Name
                  </th>
                  <th scope="col" className="h-10 w-[130px] px-2 font-medium">
                    Agent SDK
                  </th>
                  <th scope="col" className="h-10 w-[120px] whitespace-nowrap px-2 font-medium">
                    Created
                  </th>
                  <th scope="col" className="h-10 w-[120px] whitespace-nowrap px-2 font-medium">
                    Last edited
                  </th>
                  {renderPin && (
                    <th scope="col" className="h-10 px-2 font-medium">
                      Pin
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {filtered.map((agent) => (
                  <tr
                    key={agent.id}
                    data-settings-region-row
                    className="relative border-b border-border transition-colors last:border-0 hover:bg-muted/40 focus-within:bg-muted/40"
                  >
                    <td className="max-w-0 p-2">
                      <Link
                        to={customAgentSettingsPath(agent.id)}
                        data-settings-row-action="open"
                        componentId="settings.custom_agents.review"
                        className="flex items-center gap-2.5 rounded-sm outline-none after:absolute after:inset-0 focus-visible:ring-2 focus-visible:ring-ring"
                        aria-label={agent.name}
                      >
                        <span className="flex size-7 shrink-0 items-center justify-center rounded-lg border border-border text-muted-foreground">
                          <BotIcon className="size-[15px]" aria-hidden />
                        </span>
                        <span className="min-w-0">
                          <span className="block truncate font-medium">
                            <AgentNameTag
                              agentId={agent.id}
                              name={agent.name}
                              harness={agent.harness}
                            />
                          </span>
                          <span className="block truncate text-xs text-muted-foreground">
                            {agent.description}
                          </span>
                        </span>
                      </Link>
                    </td>
                    <td className="whitespace-nowrap p-2" title={agent.harness ?? undefined}>
                      {formatHarnessLabel(agent.harness, labels)}
                    </td>
                    <td className="whitespace-nowrap p-2">{date(agent.created_at)}</td>
                    <td className="whitespace-nowrap p-2">{date(agent.updated_at)}</td>
                    {renderPin && (
                      <td className="relative z-10 p-2">{renderPin(agent.id, agent.name)}</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </SettingsDataRegion>
        )}
        <ListStatus
          query={yours}
          empty={filtered.length === 0}
          emptyText={
            search || userHasPages
              ? "No matching agents on this page."
              : "No custom agents yet. Create an agent to keep it available for new sessions."
          }
        />
        <SettingsPager label="Your agent pages" {...userPager} />
      </section>
      {(!embedded ||
        builtins.length > 0 ||
        serverHasPages ||
        servers.isPending ||
        servers.isError) && (
        <section aria-label="Server agents">
          <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Built-in &amp; server agents
          </h2>
          <SettingsDataRegion
            entry={CUSTOM_AGENTS_LIST_ENTRY}
            region="serverAgents"
            className="grid gap-3 @lg:grid-cols-2"
          >
            {builtins.map((agent) => (
              <Link
                key={agent.id}
                data-settings-row-action="open"
                data-settings-region-row
                to={customAgentSettingsPath(agent.id)}
                componentId="settings.custom_agents.review_server"
                className="flex flex-col gap-3 rounded-xl border border-border p-4 hover:bg-muted/40"
              >
                <span className="flex items-center gap-3 font-medium">
                  <BotIcon className="size-5 shrink-0" aria-hidden />
                  <AgentNameTag agentId={agent.id} name={agent.name} harness={agent.harness} />
                </span>
                <span className="text-ui text-muted-foreground">
                  {agent.description || "Server-provided agent"}
                </span>
              </Link>
            ))}
          </SettingsDataRegion>
          <ListStatus
            query={servers}
            empty={builtins.length === 0}
            emptyText={
              search || serverHasPages
                ? "No matching server agents on this page."
                : "No server agents."
            }
          />
          <SettingsPager label="Server agent pages" {...serverPager} />
        </section>
      )}
    </SettingsItem>
  );
}

function UnsupportedServer() {
  return (
    <div>
      <h1 className="mb-6 text-2xl font-medium">Custom agents</h1>
      <p role="alert" className="text-ui text-muted-foreground">
        Custom agents settings is unavailable on this server. Update the server and reload to enable
        it.
      </p>
    </div>
  );
}

function ListStatus({
  query,
  empty,
  emptyText,
}: {
  query: ReturnType<typeof useCustomAgents>["query"];
  empty: boolean;
  emptyText: string;
}) {
  if (query.isPending) return <p className="py-4 text-ui text-muted-foreground">Loading agents…</p>;
  if (query.isError)
    return (
      <p role="alert" className="py-4 text-ui">
        Could not load agents.{" "}
        <Button
          {...settingsControlProps(CUSTOM_AGENTS_LIST_ENTRY, "retry")}
          variant="link"
          onClick={() => void query.refetch()}
        >
          Retry
        </Button>
      </p>
    );
  return empty ? <p className="py-4 text-ui text-muted-foreground">{emptyText}</p> : null;
}

function AgentDetail({
  agent,
  owned,
  editable,
  harness,
  returnLabel,
  returnTo,
  onDeleted,
}: {
  agent: ManagedAgent;
  owned: boolean | undefined;
  editable: boolean;
  harness: string;
  returnLabel: string;
  returnTo: string;
  onDeleted: () => void;
}) {
  const remove = useDeleteCustomAgent();
  const navigate = useNavigate();
  const [confirm, setConfirm] = useState(false);
  const [badgeEditing, setBadgeEditing] = useState(false);
  const [inUse, setInUse] = useState<string | null>(null);
  function confirmDelete() {
    remove.reset();
    setInUse(null);
    setConfirm(true);
  }
  const canEdit = owned === true && editable;
  const badgeAction = (
    <Button
      variant="ghost"
      size="sm"
      aria-label={`Edit badge for ${agent.name}`}
      onClick={() => setBadgeEditing(true)}
    >
      Edit badge
    </Button>
  );
  async function onRemove() {
    try {
      const count = await remove.mutateAsync({ id: agent.id, force: inUse !== null });
      if (count === null) onDeleted();
      else setInUse(count);
    } catch {
      /* The mutation error is displayed in the dialog. */
    }
  }
  return (
    <>
      {canEdit ? (
        <CustomAgentConfigForm
          id={agent.id}
          name={agent.name}
          badgeAction={badgeAction}
          returnLabel={returnLabel}
          onBack={() => navigate(returnTo)}
          onDelete={confirmDelete}
        />
      ) : (
        <>
          <BackButton label={returnLabel} to={returnTo} componentId="settings.custom_agents.back" />
          <div className="flex items-start justify-between gap-4">
            <h1 className="settings-page-title min-w-0 break-words text-2xl font-medium">
              {agent.name}
            </h1>
            <div className="flex shrink-0 items-center gap-2">
              {badgeAction}
              {owned === true && (
                <Button
                  variant="ghost"
                  size="icon"
                  {...settingsControlProps(CUSTOM_AGENT_DETAIL_ENTRY, "delete")}
                  onClick={confirmDelete}
                >
                  <TrashIcon className="size-4" />
                </Button>
              )}
            </div>
          </div>
          <p className="mt-3 text-sm text-muted-foreground">
            {agent.description || "No description."}
          </p>
        </>
      )}
      <dl className="my-6 grid grid-cols-2 gap-4 text-ui">
        {!canEdit && (
          <div>
            <dt className="text-muted-foreground">Harness</dt>
            <dd title={agent.harness ?? undefined}>{harness}</dd>
          </div>
        )}
        <div>
          <dt className="text-muted-foreground">Version</dt>
          <dd>{agent.version}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Created</dt>
          <dd>{date(agent.created_at)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Last edited</dt>
          <dd>{date(agent.updated_at)}</dd>
        </div>
      </dl>
      {!canEdit && (
        <section className="mb-6">
          <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            MCP servers
          </h2>
          {agent.mcp_servers?.length ? (
            <ul className="space-y-2">
              {agent.mcp_servers.map((server) => (
                <li key={server.name} className="rounded-xl border border-border p-3">
                  <p className="text-ui font-medium">{server.name}</p>
                  <p className="text-xs text-muted-foreground">
                    {server.transport}
                    {server.description ? ` · ${server.description}` : ""}
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-ui text-muted-foreground">No MCP servers listed.</p>
          )}
        </section>
      )}
      <section className="mb-6">
        <h2 className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Skills
        </h2>
        {agent.skills?.length ? (
          <ul className="space-y-2">
            {agent.skills.map((skill) => (
              <li key={skill.name} className="rounded-xl border border-border p-3">
                <p className="text-ui font-medium">{skill.name}</p>
                <p className="text-xs text-muted-foreground">{skill.description}</p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-ui text-muted-foreground">No skills listed.</p>
        )}
      </section>
      <p className="text-xs text-muted-foreground">
        {owned === true
          ? editable
            ? "Changes apply to sessions on their next turn."
            : "Update the server to enable configuration editing."
          : owned === false
            ? "Server-provided agents are read-only."
            : "Update the server to enable agent deletion. Ownership information is unavailable."}
      </p>
      {badgeEditing && (
        <AgentSettingsEditor
          agent={{ id: agent.id, name: agent.name, harness: agent.harness, custom: false }}
          onClose={() => setBadgeEditing(false)}
          onSaved={async () => {}}
        />
      )}
      <Dialog
        open={confirm}
        onOpenChange={(open) => {
          if (!remove.isPending) setConfirm(open);
        }}
      >
        <DialogContent
          showCloseButton={!remove.isPending}
          {...settingsOverlayProps(CUSTOM_AGENT_DETAIL_ENTRY, "delete")}
        >
          <DialogHeader>
            <DialogTitle>Delete {agent.name}?</DialogTitle>
            <DialogDescription>
              {inUse === null
                ? "This removes the agent from your collection. This cannot be undone."
                : `${inUse} session(s) still use this agent. Removing it can stop those sessions from continuing. Remove it anyway?`}
            </DialogDescription>
          </DialogHeader>
          {remove.error && (
            <p role="alert" className="text-ui text-destructive">
              {remove.error.message}
            </p>
          )}
          <DialogFooter>
            <Button
              data-settings-dialog-action="Cancel"
              variant="ghost"
              disabled={remove.isPending}
              onClick={() => setConfirm(false)}
            >
              Cancel
            </Button>
            <Button
              {...settingsControlProps(
                CUSTOM_AGENT_DETAIL_ENTRY,
                "confirmDelete",
                inUse === null ? undefined : { alternative: "inUse" },
              )}
              variant="destructive"
              loading={remove.isPending}
              disabled={remove.isPending}
              onClick={() => void onRemove()}
            >
              {inUse === null ? "Delete" : "Remove anyway"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
