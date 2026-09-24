import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { PlusIcon } from "lucide-react";

import { AgentEditor } from "@/components/AgentEditor";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAgentBadgePreferences } from "@/hooks/useAgentBadgePreferences";
import {
  readAgentBadgePreferences,
  writeAgentBadgePreferences,
  type AgentBadgeValue,
} from "@/lib/agentBadgePreferences";
import { isAcpHarnessAgent } from "@/lib/agentGrouping";
import { useBrainHarnessLabels } from "@/lib/agentLabels";
import { buildAgentBundle } from "@/lib/agentBundle";
import {
  CUSTOM_AGENTS_QUERY_KEY,
  createCustomAgent,
  deleteCustomAgent,
  duplicateBuiltinAgent,
  getCustomAgent,
  importCustomAgent,
  updateCustomAgent,
  useCustomAgents,
  type CustomAgent,
  type CustomAgentMember,
} from "@/lib/customAgentsApi";
import { isNativeCodingAgent, nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import { AgentBadge } from "./AgentBadge";
import { AgentBadgeEditor } from "./AgentBadgeEditor";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Textarea } from "./ui/textarea";
import { Switch } from "./ui/switch";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from "./ui/dialog";
import { CreateAgentDialog } from "@/shell/CreateAgentDialog";

function saveBadge(agentId: string, value: AgentBadgeValue | null) {
  const preferences = readAgentBadgePreferences();
  const entries = value
    ? { ...preferences.entries, [agentId]: value }
    : Object.fromEntries(Object.entries(preferences.entries).filter(([id]) => id !== agentId));
  writeAgentBadgePreferences({ ...preferences, entries });
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "The Agent could not be saved.";
}

/** AgentEditor's label resolution: catalog label, then the native registry, then the id. */
function harnessLabel(labels: Record<string, string>, harness: string): string {
  return labels[harness] ?? nativeCodingAgentForHarness(harness)?.displayName ?? harness;
}

function memberSettings(member: CustomAgentMember, labels: Record<string, string>): string {
  return [harnessLabel(labels, member.harness), member.model ?? "Default", member.reasoning_effort]
    .filter(Boolean)
    .join(" · ");
}

/** One-line roster summary for a saved Agent; `members` arrives lead-first. */
function rosterSummary(members: CustomAgentMember[], labels: Record<string, string>): string {
  const [lead, ...others] = members;
  if (others.length === 0) return memberSettings(lead, labels);
  return `${members.length} members · ${lead.name} (Lead), ${others
    .map((member) => member.name)
    .join(", ")}`;
}

function builtinSubtitle(agent: AvailableAgent): string {
  const count = agent.members?.length ?? 0;
  return [agent.description, count > 1 ? `${count} members` : null, "read-only"]
    .filter(Boolean)
    .join(" · ");
}

export function AgentsSettings() {
  const queryClient = useQueryClient();
  const catalog = useCustomAgents();
  const available = useAvailableAgents();
  const preferences = useAgentBadgePreferences();
  const harnessLabels = useBrainHarnessLabels();
  // Harness-backed built-ins are flat executor specs, not editable member
  // Agents — the same Harnesses/Agents split NewChatDialog draws.
  const builtinAgents = (available.data ?? []).filter(
    (agent) => agent.builtin === true && !isNativeCodingAgent(agent) && !isAcpHarnessAgent(agent),
  );
  const [createOpen, setCreateOpen] = useState(false);
  const [newBadge, setNewBadge] = useState<AgentBadgeValue | null>(null);
  const [newBadgeValid, setNewBadgeValid] = useState(true);
  const [editing, setEditing] = useState<CustomAgent | null>(null);
  const [badgeEditing, setBadgeEditing] = useState<{ id: string; name: string } | null>(null);
  const [viewing, setViewing] = useState<AvailableAgent | null>(null);
  const [deleting, setDeleting] = useState<CustomAgent | null>(null);
  const [duplicatingId, setDuplicatingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: CUSTOM_AGENTS_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: ["available-agents"] }),
    ]);
  }

  const importedNames = new Set(catalog.data?.map((agent) => agent.name));
  const sessionAgents = (available.data ?? []).filter(
    (agent) => agent.sessionId && !agent.templateId && !importedNames.has(agent.name),
  );

  async function importAgent(agent: AvailableAgent) {
    if (!agent.sessionId || busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await importCustomAgent(agent.sessionId);
      const oldBadge = readAgentBadgePreferences().entries[agent.id];
      if (oldBadge) saveBadge(created.id, oldBadge);
      await refresh();
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  }

  async function duplicateBuiltin(agent: AvailableAgent) {
    if (duplicatingId !== null) return;
    setDuplicatingId(agent.id);
    setError(null);
    try {
      const copy = await duplicateBuiltinAgent(agent.id);
      await refresh();
      setViewing(null);
      setEditing(copy);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setDuplicatingId(null);
    }
  }

  return (
    <section aria-label="Agents" className="mx-auto w-full max-w-3xl space-y-7">
      <div>
        <h1 className="text-lg font-semibold">Agents</h1>
      </div>
      <div className="flex items-center justify-between gap-4 border-b pb-5">
        <label htmlFor="show-agent-badges" className="text-sm">
          Show Agent badges
        </label>
        <Switch
          id="show-agent-badges"
          checked={preferences.enabled}
          onCheckedChange={(enabled) =>
            writeAgentBadgePreferences({ ...readAgentBadgePreferences(), enabled })
          }
        />
      </div>
      <div>
        <h2 className="mb-2 text-sm text-muted-foreground">Built-in</h2>
        {available.isLoading && (
          <p role="status" className="text-sm text-muted-foreground">
            Loading agents…
          </p>
        )}
        {available.error && (
          <p role="alert" className="text-sm text-destructive">
            {errorText(available.error)}
          </p>
        )}
        {builtinAgents.map((agent) => (
          <div key={agent.id} className="flex min-h-14 items-center gap-2 border-b py-2">
            <AgentBadge agentId={agent.id} />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm">{agent.display_name}</div>
              <div className="truncate text-xs text-muted-foreground">{builtinSubtitle(agent)}</div>
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setBadgeEditing({ id: agent.id, name: agent.display_name })}
              aria-label={`Edit badge for ${agent.display_name}`}
            >
              Edit badge
            </Button>
            <Button
              variant="ghost"
              size="sm"
              aria-label={`View ${agent.display_name}`}
              disabled={duplicatingId !== null}
              onClick={() => {
                setError(null);
                setViewing(agent);
              }}
            >
              View
            </Button>
            <Button
              variant="ghost"
              size="sm"
              aria-label={`Duplicate ${agent.display_name}`}
              disabled={duplicatingId !== null}
              onClick={() => void duplicateBuiltin(agent)}
            >
              Duplicate
            </Button>
          </div>
        ))}
      </div>
      <div>
        <div className="mb-2 flex items-center justify-between gap-3">
          <h2 className="text-sm text-muted-foreground">Custom agents</h2>
          <Button
            size="sm"
            variant="outline"
            disabled={catalog.isLoading || !!catalog.error}
            onClick={() => {
              setNewBadge(null);
              setCreateOpen(true);
            }}
          >
            <PlusIcon className="size-3.5" />
            New
          </Button>
        </div>
        {catalog.isLoading && (
          <p role="status" className="text-sm text-muted-foreground">
            Loading custom agents…
          </p>
        )}
        {catalog.error && (
          <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>{errorText(catalog.error)}</p>
            <Button variant="outline" size="sm" onClick={() => void catalog.refetch()}>
              Retry
            </Button>
          </div>
        )}
        {catalog.data?.length === 0 && (
          <p className="py-4 text-sm text-muted-foreground">No custom agents yet.</p>
        )}
        {catalog.data?.map((agent) => {
          const members = agent.members;
          return (
            <div key={agent.id} className="flex min-h-14 items-center gap-2 border-b py-2">
              <AgentBadge agentId={agent.id} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm">{agent.name}</div>
                {members && members.length > 0 ? (
                  <>
                    <div className="truncate text-xs text-muted-foreground">
                      {rosterSummary(members, harnessLabels)}
                    </div>
                    {agent.description && (
                      <div className="truncate text-xs text-muted-foreground">
                        {agent.description}
                      </div>
                    )}
                  </>
                ) : (
                  agent.description && (
                    <div className="truncate text-xs text-muted-foreground">
                      {agent.description}
                    </div>
                  )
                )}
              </div>
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Edit ${agent.name}`}
                onClick={() => {
                  setError(null);
                  setEditing(agent);
                }}
              >
                Edit
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="text-destructive"
                aria-label={`Delete ${agent.name}`}
                onClick={() => {
                  setError(null);
                  setDeleting(agent);
                }}
              >
                Delete
              </Button>
            </div>
          );
        })}
      </div>
      {sessionAgents.length > 0 && !catalog.error && (
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">
            Import from existing sessions
          </summary>
          {sessionAgents.map((agent) => (
            <div key={agent.id} className="mt-2 flex items-center gap-2 border-b py-2">
              <AgentBadge agentId={agent.id} />
              <span className="min-w-0 flex-1 truncate">{agent.display_name}</span>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => void importAgent(agent)}
              >
                Import
              </Button>
            </div>
          ))}
        </details>
      )}
      {error && !deleting && !viewing && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <CreateAgentDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        submitDisabled={!newBadgeValid}
        extraFields={
          <AgentBadgeEditor
            value={newBadge}
            onChange={setNewBadge}
            onValidityChange={setNewBadgeValid}
          />
        }
        onCreate={async (input) => {
          const created = await createCustomAgent(await buildAgentBundle(input));
          if (newBadge) saveBadge(created.id, newBadge);
          await refresh();
        }}
      />
      {badgeEditing && (
        <AgentSettingsEditor
          key={badgeEditing.id}
          agent={{ ...badgeEditing, custom: false }}
          onClose={() => setBadgeEditing(null)}
          onSaved={refresh}
        />
      )}
      {editing && (
        <SavedAgentEditor
          key={editing.id}
          agent={editing}
          onClose={() => setEditing(null)}
          onSaved={refresh}
        />
      )}
      <Dialog
        open={viewing !== null}
        onOpenChange={(open) => {
          if (!open && duplicatingId === null) {
            setViewing(null);
            setError(null);
          }
        }}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{viewing?.display_name}</DialogTitle>
            <DialogDescription>Built-in · read-only</DialogDescription>
          </DialogHeader>
          {viewing?.members == null ? (
            <p className="py-2 text-sm text-muted-foreground">Members unavailable</p>
          ) : (
            <div className="space-y-2">
              {viewing.members.map((member) => (
                <div
                  key={member.name}
                  data-testid="builtin-member-row"
                  className="flex items-center gap-2 rounded-md border border-border p-2"
                >
                  <span className="min-w-0 flex-1 truncate text-sm">{member.name}</span>
                  {member.lead && (
                    <span className="shrink-0 rounded-full border border-border px-1.5 text-xs text-muted-foreground">
                      Lead
                    </span>
                  )}
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {memberSettings(member, harnessLabels)}
                  </span>
                </div>
              ))}
            </div>
          )}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter className="sm:items-center">
            <p className="text-xs text-muted-foreground sm:mr-auto">
              Built-in agents can't be changed. Duplicate {viewing?.display_name} to get a copy you
              can edit.
            </p>
            <Button
              variant="outline"
              disabled={duplicatingId !== null}
              onClick={() => viewing && void duplicateBuiltin(viewing)}
            >
              Duplicate to edit
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open && !busy) setDeleting(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete {deleting?.name}?</DialogTitle>
            <DialogDescription>
              Remove this Agent from your catalog. Existing sessions and their configuration are
              preserved.
            </DialogDescription>
          </DialogHeader>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button variant="ghost" disabled={busy} onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={async () => {
                if (!deleting || busy) return;
                setBusy(true);
                setError(null);
                try {
                  await deleteCustomAgent(deleting.id);
                  await refresh();
                  setDeleting(null);
                } catch (cause) {
                  setError(errorText(cause));
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? "Deleting…" : "Delete Agent"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}

function AgentSettingsEditor({
  agent,
  onClose,
  onSaved,
}: {
  agent: { id: string; name: string; custom: boolean };
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const detail = useQuery({
    queryKey: ["custom-agent", agent.id],
    queryFn: () => getCustomAgent(agent.id),
    enabled: agent.custom,
    staleTime: 0,
  });
  const [name, setName] = useState(agent.name);
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [badge, setBadge] = useState<AgentBadgeValue | null>(
    () => readAgentBadgePreferences().entries[agent.id] ?? null,
  );
  const [badgeValid, setBadgeValid] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (detail.data) {
      setName(detail.data.name);
      setDescription(detail.data.description ?? "");
      setInstructions(detail.data.instructions ?? "");
    }
  }, [detail.data]);
  const unavailable = agent.custom && (!detail.data || detail.isFetching);
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent className="flex max-h-[85vh] flex-col sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{agent.custom ? "Edit Agent" : `${agent.name} · Badge`}</DialogTitle>
        </DialogHeader>
        <div className="-mx-3 -my-2 min-h-0 space-y-4 overflow-x-hidden overflow-y-auto px-3 py-2">
          {agent.custom && (
            <>
              {detail.isLoading && <p role="status">Loading Agent…</p>}
              {detail.error && (
                <p role="alert" className="text-sm text-destructive">
                  {errorText(detail.error)}
                </p>
              )}
              <label className="block space-y-1.5 text-sm">
                <span>Name</span>
                <Input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  disabled={unavailable || busy}
                />
              </label>
              <label className="block space-y-1.5 text-sm">
                <span>Description</span>
                <Input
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  disabled={unavailable || busy}
                />
              </label>
              <label className="block space-y-1.5 text-sm">
                <span>Instructions</span>
                <Textarea
                  value={instructions}
                  onChange={(e) => setInstructions(e.target.value)}
                  disabled={unavailable || busy}
                  className="min-h-32"
                />
              </label>
            </>
          )}
          <AgentBadgeEditor value={badge} onChange={setBadge} onValidityChange={setBadgeValid} />
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={busy || unavailable || !name.trim() || !badgeValid}
            onClick={async () => {
              if (busy) return;
              setBusy(true);
              setError(null);
              try {
                if (agent.custom && detail.data)
                  await updateCustomAgent(agent.id, {
                    name: name.trim(),
                    description: description.trim() || null,
                    instructions: instructions || null,
                    version: detail.data.version,
                  });
                saveBadge(agent.id, badge);
                await onSaved();
                onClose();
              } catch (cause) {
                setError(errorText(cause));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** Edit a saved Agent's roster plus its optional badge. */
function SavedAgentEditor({
  agent,
  onClose,
  onSaved,
}: {
  agent: CustomAgent;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const [badge, setBadge] = useState<AgentBadgeValue | null>(
    () => readAgentBadgePreferences().entries[agent.id] ?? null,
  );
  const [badgeValid, setBadgeValid] = useState(true);
  return (
    <AgentEditor
      agent={agent}
      onClose={onClose}
      onSaved={async () => {
        saveBadge(agent.id, badge);
        await onSaved();
      }}
      extraFields={
        <AgentBadgeEditor value={badge} onChange={setBadge} onValidityChange={setBadgeValid} />
      }
      submitDisabled={!badgeValid}
    />
  );
}
