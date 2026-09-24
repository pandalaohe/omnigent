import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { PlusIcon, TrashIcon } from "lucide-react";

import { AgentMemberTrigger } from "@/components/AgentMemberTrigger";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useNewChatHostId } from "@/hooks/useHosts";
import { BRAIN_HARNESS_LABELS, useBrainHarnessLabels } from "@/lib/agentLabels";
import {
  getCustomAgent,
  updateCustomAgent,
  type CustomAgent,
  type CustomAgentDetail,
  type CustomAgentMember,
} from "@/lib/customAgentsApi";
import { NATIVE_CODING_AGENTS, nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";

/**
 * Worker harnesses a non-lead member may name. The server writes the member's
 * `executor.config.harness` verbatim (`omnigent/server/custom_agent_bundles.py`),
 * so this is the worker set the UI offers, not a server restriction: the
 * native CLI rows of the web's native-agent registry (the mirror of the
 * server's `native_agents()`) plus the headless `pi` harness the built-in
 * Polly `pi` sub-agent names (`examples/polly/agents/pi/config.yaml`).
 */
const SUBAGENT_HARNESS_IDS: readonly string[] = [
  ...NATIVE_CODING_AGENTS.filter((agent) => agent.harness !== "pi-native").map(
    (agent) => agent.harness,
  ),
  "pi",
];

/** The spec's agent-name rule (`omnigent/spec/validator.py` `_AGENT_NAME_PATTERN`),
 *  re-stated for role names by `AgentMember.valid_role_name`
 *  (`omnigent/server/routes/custom_agents.py`). */
const ROLE_NAME_PATTERN = /^[a-zA-Z0-9_-]+$/;

/** Fallback harness for a legacy detail that carries none; CreateAgentDialog's default. */
const DEFAULT_HARNESS = Object.keys(BRAIN_HARNESS_LABELS)[0];

const ROLE_NAME_HINT = "Role name must match [a-zA-Z0-9_-]+ (no dots, slashes, or whitespace).";

const CONFLICT_RELOAD_MESSAGE =
  "This Agent changed elsewhere and was reloaded — reapply your edits.";

const REMOVE_SAVED_MEMBER_HINT = "Removing a member deletes its saved settings when you save.";

/** One editable member row; the lead (index 0) is the Agent itself. */
interface MemberDraft {
  key: number;
  name: string;
  description: string;
  harness: string;
  model: string | null;
  effort: string | null;
  /** Loaded from the saved roster: the server keys its config dir by this name. */
  saved: boolean;
}

function draftFromMember(member: CustomAgentMember, key: number): MemberDraft {
  return {
    key,
    name: member.name,
    description: member.description ?? "",
    harness: member.harness,
    model: member.model,
    effort: member.reasoning_effort,
    saved: true,
  };
}

function initialMembers(detail: CustomAgentDetail): MemberDraft[] {
  if (detail.members && detail.members.length > 0) {
    return detail.members.map(draftFromMember);
  }
  return [
    {
      key: 0,
      name: detail.name,
      description: detail.description ?? "",
      harness: detail.harness ?? DEFAULT_HARNESS,
      model: detail.model,
      effort: null,
      saved: true,
    },
  ];
}

/** The rows a `members` PATCH sends: lead first, its name/description taken from the Agent fields. */
function memberPayload(
  members: MemberDraft[],
  name: string,
  description: string,
): CustomAgentMember[] {
  return members.map((member, index) => ({
    name: (index === 0 ? name : member.name).trim(),
    description: (index === 0 ? description : member.description).trim() || null,
    harness: member.harness,
    model: member.model,
    reasoning_effort: member.effort,
    lead: index === 0,
  }));
}

/**
 * Roster identity without the lead's Agent-level name/description: a scalar
 * PATCH preserves the bundle bytes, while a `members` PATCH re-dumps the
 * member configs and drops their comments, so a name-only edit must not
 * count as a roster change.
 */
function rosterSignature(members: MemberDraft[]): string {
  return JSON.stringify(
    members.map((member, index) => ({
      name: index === 0 ? "" : member.name.trim(),
      description: index === 0 ? null : member.description.trim() || null,
      harness: member.harness,
      model: member.model,
      effort: member.effort,
    })),
  );
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "The Agent could not be saved.";
}

/** A PATCH conflict means the row moved; the caller reloads the detail. */
function isVersionConflict(error: unknown): boolean {
  return (
    typeof error === "object" && error !== null && (error as { status?: unknown }).status === 409
  );
}

function subagentHarnessOptions(labels: Record<string, string>): { id: string; label: string }[] {
  return SUBAGENT_HARNESS_IDS.map((id) => ({
    id,
    label: labels[id] ?? nativeCodingAgentForHarness(id)?.displayName ?? id,
  }));
}

/**
 * Editor for a saved Agent's name, description, instructions and member
 * roster. The lead member is the Agent itself, so its row shows the Agent's
 * name and description and only its harness / model / effort is editable.
 */
export function AgentEditor({
  agent,
  onClose,
  onSaved,
  extraFields,
  submitDisabled = false,
}: {
  agent: CustomAgent;
  onClose: () => void;
  onSaved: () => Promise<void>;
  extraFields?: ReactNode;
  submitDisabled?: boolean;
}) {
  const detail = useQuery({
    queryKey: ["custom-agent", agent.id],
    queryFn: () => getCustomAgent(agent.id),
    staleTime: 0,
  });
  const hostId = useNewChatHostId();
  const brainHarnessLabels = useBrainHarnessLabels();
  const leadHarnessOptions = Object.entries(brainHarnessLabels).map(([id, label]) => ({
    id,
    label,
  }));
  const memberHarnessOptions = subagentHarnessOptions(brainHarnessLabels);
  const [name, setName] = useState(agent.name);
  const [description, setDescription] = useState(agent.description ?? "");
  const [instructions, setInstructions] = useState("");
  const [members, setMembers] = useState<MemberDraft[]>([]);
  const [baseline, setBaseline] = useState<MemberDraft[]>([]);
  /* The version the form loaded at; a save always sends this one, so data
     refetched behind the open form cannot make it overwrite unseen changes. */
  const [baseVersion, setBaseVersion] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [focusKey, setFocusKey] = useState<number | null>(null);
  const initialized = useRef(false);
  const nextKey = useRef(0);
  const nameRefs = useRef(new Map<number, HTMLInputElement | null>());

  const applyDetail = useCallback((data: CustomAgentDetail) => {
    const initial = initialMembers(data);
    initialized.current = true;
    nextKey.current = initial.length;
    setName(data.name);
    setDescription(data.description ?? "");
    setInstructions(data.instructions ?? "");
    setMembers(initial);
    setBaseline(initial);
    setBaseVersion(data.version);
  }, []);

  useEffect(() => {
    /* Initialise only from detail fetched after this mount: the cache may hold
       an earlier visit's copy, and isFetchedAfterMount also flips on a failed
       fetch. */
    if (
      !detail.data ||
      detail.isFetching ||
      !detail.isFetchedAfterMount ||
      detail.error !== null ||
      initialized.current
    ) {
      return;
    }
    applyDetail(detail.data);
  }, [detail.data, detail.isFetching, detail.isFetchedAfterMount, detail.error, applyDetail]);

  useEffect(() => {
    if (focusKey === null) return;
    nameRefs.current.get(focusKey)?.focus();
    setFocusKey(null);
  }, [focusKey]);

  const roleErrors = useMemo(() => {
    const errors = new Map<number, string>();
    const seen = new Set<string>(name.trim() === "" ? [] : [name.trim()]);
    /* The server matches sub-agent config dirs by YAML name, so a new row
       reusing a saved member's name would inherit its hidden config. */
    const savedNames = new Set(baseline.slice(1).map((member) => member.name.trim()));
    for (const member of members.slice(1)) {
      const role = member.name.trim();
      if (role === "") {
        errors.set(member.key, "Role name is required.");
      } else if (!ROLE_NAME_PATTERN.test(role)) {
        errors.set(member.key, ROLE_NAME_HINT);
      } else if (seen.has(role)) {
        errors.set(member.key, "Role names must be unique and differ from the Agent name.");
      } else if (!member.saved && savedNames.has(role)) {
        errors.set(member.key, `${role} was a saved member — undo its removal or save first`);
      } else {
        seen.add(role);
      }
    }
    return errors;
  }, [members, name, baseline]);

  const leadModelMissing = members.length > 0 && members[0].model === null;
  const dirty = useMemo(() => {
    if (!detail.data || !initialized.current) return false;
    return (
      name.trim() !== detail.data.name ||
      (description.trim() || null) !== (detail.data.description?.trim() || null) ||
      instructions !== (detail.data.instructions ?? "") ||
      JSON.stringify(memberPayload(members, name, description)) !==
        JSON.stringify(memberPayload(baseline, detail.data.name, detail.data.description ?? ""))
    );
  }, [name, description, instructions, members, baseline, detail.data]);
  const rosterChanged = useMemo(
    () => initialized.current && rosterSignature(members) !== rosterSignature(baseline),
    [members, baseline],
  );
  const savedMemberRemoved = useMemo(() => {
    const live = new Set(members.slice(1).map((member) => member.name.trim()));
    return baseline.slice(1).some((member) => !live.has(member.name.trim()));
  }, [members, baseline]);

  const unavailable = !detail.data || detail.isFetching || baseVersion === null;
  const canSave =
    !busy &&
    !unavailable &&
    !submitDisabled &&
    name.trim() !== "" &&
    roleErrors.size === 0 &&
    !leadModelMissing;

  function updateMember(key: number, patch: Partial<MemberDraft>) {
    setMembers((current) =>
      current.map((member) => (member.key === key ? { ...member, ...patch } : member)),
    );
  }

  function addMember() {
    const lead = members[0];
    if (!lead) return;
    const key = nextKey.current;
    nextKey.current += 1;
    setMembers((current) => [
      ...current,
      {
        key,
        name: "",
        description: "",
        harness: lead.harness,
        model: null,
        effort: null,
        saved: false,
      },
    ]);
    setFocusKey(key);
  }

  async function save() {
    if (!canSave || baseVersion === null) return;
    setBusy(true);
    setError(null);
    try {
      await updateCustomAgent(agent.id, {
        name: name.trim(),
        description: description.trim() || null,
        instructions: instructions || null,
        version: baseVersion,
        ...(rosterChanged ? { members: memberPayload(members, name, description) } : {}),
      });
      await onSaved();
      onClose();
    } catch (cause) {
      if (isVersionConflict(cause)) {
        const refreshed = await detail.refetch();
        if (refreshed.isSuccess) {
          applyDetail(refreshed.data);
          setError(CONFLICT_RELOAD_MESSAGE);
        } else {
          setError(errorText(cause));
        }
      } else {
        setError(errorText(cause));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent data-testid="agent-editor" className="flex max-h-[85vh] flex-col sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Edit Agent</DialogTitle>
          <DialogDescription>
            Changes apply when this saved Agent is selected for a new session.
          </DialogDescription>
        </DialogHeader>
        <div className="-mx-3 -my-2 min-h-0 space-y-4 overflow-x-hidden overflow-y-auto px-3 py-2">
          {detail.isLoading && <p role="status">Loading Agent…</p>}
          {detail.error && (
            <p role="alert" className="text-sm text-destructive">
              {errorText(detail.error)}
            </p>
          )}
          <label className="block space-y-1.5 text-sm">
            <span>Name</span>
            <Input
              data-testid="agent-editor-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              disabled={unavailable || busy}
            />
          </label>
          <label className="block space-y-1.5 text-sm">
            <span>Description</span>
            <Input
              data-testid="agent-editor-description"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              disabled={unavailable || busy}
            />
          </label>
          <label className="block space-y-1.5 text-sm">
            <span>Instructions</span>
            <Textarea
              data-testid="agent-editor-instructions"
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              disabled={unavailable || busy}
              className="min-h-32"
            />
          </label>
          <div className="space-y-2">
            <span className="text-sm">Members</span>
            {members.map((member, index) =>
              index === 0 ? (
                <div
                  key={member.key}
                  data-testid="agent-member-row"
                  className="flex items-center gap-2 rounded-md border border-border p-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">{name.trim()}</span>
                      <span className="shrink-0 rounded-full border border-border px-1.5 text-xs text-muted-foreground">
                        Lead
                      </span>
                    </div>
                    {description.trim() !== "" && (
                      <p className="truncate text-xs text-muted-foreground">{description.trim()}</p>
                    )}
                  </div>
                  <AgentMemberTrigger
                    harness={member.harness}
                    model={member.model}
                    effort={member.effort}
                    harnessOptions={leadHarnessOptions}
                    hostId={hostId}
                    onChange={(next) => updateMember(member.key, next)}
                    disabled={unavailable || busy}
                  />
                </div>
              ) : (
                <div
                  key={member.key}
                  data-testid="agent-member-row"
                  className="space-y-1 rounded-md border border-border p-2"
                >
                  <div className="flex items-center gap-2">
                    {member.saved ? (
                      <span
                        data-testid="agent-member-role-name"
                        className="min-w-0 flex-1 truncate text-sm leading-8"
                        title={member.name}
                      >
                        {member.name}
                      </span>
                    ) : (
                      <Input
                        ref={(element) => {
                          if (element) nameRefs.current.set(member.key, element);
                          else nameRefs.current.delete(member.key);
                        }}
                        data-testid="agent-member-name"
                        aria-label="Role name"
                        placeholder="role-name"
                        value={member.name}
                        onChange={(event) => updateMember(member.key, { name: event.target.value })}
                        disabled={unavailable || busy}
                      />
                    )}
                    <AgentMemberTrigger
                      harness={member.harness}
                      model={member.model}
                      effort={member.effort}
                      harnessOptions={memberHarnessOptions}
                      hostId={hostId}
                      onChange={(next) => updateMember(member.key, next)}
                      disabled={unavailable || busy}
                    />
                    <Button
                      variant="ghost"
                      size="icon"
                      aria-label={`Remove ${member.name.trim() || "member"}`}
                      data-testid="agent-member-remove"
                      disabled={unavailable || busy}
                      onClick={() =>
                        setMembers((current) => current.filter((row) => row.key !== member.key))
                      }
                    >
                      <TrashIcon className="size-3.5" />
                    </Button>
                  </div>
                  <Input
                    data-testid="agent-member-description"
                    aria-label="Role description"
                    placeholder="What this member does"
                    value={member.description}
                    onChange={(event) =>
                      updateMember(member.key, { description: event.target.value })
                    }
                    disabled={unavailable || busy}
                  />
                  {roleErrors.has(member.key) && (
                    <p role="alert" className="text-xs text-destructive">
                      {roleErrors.get(member.key)}
                    </p>
                  )}
                </div>
              ),
            )}
            {leadModelMissing && (
              <p className="text-xs text-muted-foreground">
                Pick a model — the omnigent executor requires one.
              </p>
            )}
            <Button
              variant="ghost"
              size="sm"
              data-testid="agent-editor-add-member"
              className="gap-1.5"
              disabled={unavailable || busy}
              onClick={addMember}
            >
              <PlusIcon className="size-3.5" />
              Add member
            </Button>
            {savedMemberRemoved && (
              <p className="text-xs text-muted-foreground">{REMOVE_SAVED_MEMBER_HINT}</p>
            )}
          </div>
          {extraFields}
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
        </div>
        <DialogFooter className="sm:items-center">
          <p
            data-testid="agent-editor-footer-note"
            className="text-xs text-muted-foreground sm:mr-auto"
          >
            Saves to {name.trim() || agent.name}. Every new session uses the saved members; sessions
            already running keep theirs.
          </p>
          {dirty && (
            <span data-testid="agent-editor-dirty" className="text-xs text-muted-foreground">
              Unsaved changes
            </span>
          )}
          <Button variant="ghost" disabled={busy} onClick={onClose}>
            Cancel
          </Button>
          <Button data-testid="agent-editor-save" disabled={!canSave} onClick={() => void save()}>
            {busy ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
