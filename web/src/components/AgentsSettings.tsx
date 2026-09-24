import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { PinIcon, PinOffIcon, PlusIcon } from "lucide-react";

import { AgentEditor, BuiltinAgentView, memberSettings } from "@/components/AgentEditor";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useAgentBadgePreferences } from "@/hooks/useAgentBadgePreferences";
import {
  readAgentBadgePreferences,
  writeAgentBadgePreferences,
  type AgentBadgeValue,
} from "@/lib/agentBadgePreferences";
import { isAcpHarnessAgent } from "@/lib/agentGrouping";
import {
  MAX_PINNED_AGENTS,
  resolvePinnedAgentIds,
  unpinAgent,
  useAgentPins,
} from "@/lib/agentPins";
import { useBrainHarnessLabels } from "@/lib/agentLabels";
import { buildAgentBundle } from "@/lib/agentBundle";
import {
  CUSTOM_AGENTS_QUERY_KEY,
  createCustomAgent,
  customAgentForPicker,
  deleteCustomAgent,
  duplicateBuiltinAgent,
  getCustomAgent,
  importCustomAgent,
  updateCustomAgent,
  useCustomAgents,
  type CustomAgent,
  type CustomAgentMember,
} from "@/lib/customAgentsApi";
import { isNativeCodingAgent } from "@/lib/nativeCodingAgents";
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
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";
import { CreateAgentDialog } from "@/shell/CreateAgentDialog";

const PIN_LIMIT_HINT = `Up to ${MAX_PINNED_AGENTS} Agents can be pinned`;

// Pin toggle for one Agent row: toggles wait for both Agent lists, and
// unpinned rows past the cap show a hint. A disabled button swallows pointer
// events, so the tooltip hangs off a wrapper span.
function PinToggle({
  agentId,
  name,
  pinned,
  canPin,
  ready,
  onToggle,
}: {
  agentId: string;
  name: string;
  pinned: boolean;
  canPin: boolean;
  ready: boolean;
  onToggle: (agentId: string) => void;
}) {
  const capBlocked = !pinned && !canPin;
  const disabled = capBlocked || !ready;
  const button = (
    <Button
      variant="ghost"
      size="sm"
      aria-label={`${pinned ? "Unpin" : "Pin"} ${name}`}
      aria-pressed={pinned}
      disabled={disabled}
      onClick={() => onToggle(agentId)}
    >
      {pinned ? <PinOffIcon className="size-3.5" /> : <PinIcon className="size-3.5" />}
    </Button>
  );
  if (!capBlocked || !ready) return button;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex">{button}</span>
      </TooltipTrigger>
      <TooltipContent>{PIN_LIMIT_HINT}</TooltipContent>
    </Tooltip>
  );
}

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
  // Pins address rows the way the picker does: a built-in by its /v1/agents
  // id, a saved Agent by its `ca_` id via customAgentForPicker. Toggling
  // writes the whole resolved list, materializing the Polly + Debby default.
  const { storedIds: storedPinnedIds, setPinnedIds } = useAgentPins();
  // Toggling writes the list resolved against the rows on screen, so it waits
  // for both lists: saving a partial roster would drop pins it cannot see.
  const pinsReady =
    available.data !== undefined &&
    !available.error &&
    catalog.data !== undefined &&
    !catalog.error;
  const customAgentRows = catalog.data ?? [];
  const pinCandidates = useMemo(
    () => [...builtinAgents, ...customAgentRows.map(customAgentForPicker)],
    [builtinAgents, customAgentRows],
  );
  const pinnedIds = useMemo(
    () => resolvePinnedAgentIds(storedPinnedIds, pinCandidates),
    [storedPinnedIds, pinCandidates],
  );
  const pinnedIdSet = useMemo(() => new Set(pinnedIds), [pinnedIds]);
  const atPinLimit = pinnedIds.length >= MAX_PINNED_AGENTS;
  function togglePin(agentId: string) {
    setPinnedIds(
      pinnedIdSet.has(agentId) ? pinnedIds.filter((id) => id !== agentId) : [...pinnedIds, agentId],
    );
  }
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
            <PinToggle
              agentId={agent.id}
              name={agent.display_name}
              pinned={pinnedIdSet.has(agent.id)}
              canPin={!atPinLimit}
              ready={pinsReady}
              onToggle={togglePin}
            />
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
              <PinToggle
                agentId={agent.id}
                name={agent.name}
                pinned={pinnedIdSet.has(agent.id)}
                canPin={!atPinLimit}
                ready={pinsReady}
                onToggle={togglePin}
              />
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
      {viewing && (
        <BuiltinAgentView
          key={viewing.id}
          agent={viewing}
          onClose={() => {
            setViewing(null);
            setError(null);
          }}
          onDuplicated={async (copy) => {
            await refresh();
            setViewing(null);
            setEditing(copy);
          }}
        />
      )}
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
                  unpinAgent(deleting.id);
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
