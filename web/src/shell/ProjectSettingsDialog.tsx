// Editor for a project's stored default session settings (`config`), reached
// from the project-folder kebab menu. Writing a project's config is what lets
// the new-chat composer pre-fill host / working directory / agent and the
// isolated-worktree default when starting a session in the project.
//
// Scope mirrors what the composer prefills today: host, workspace, agent,
// whether new sessions start in a fresh git worktree, the base branch a
// worktree forks from (which overrides the user-global default in Settings ›
// Git), and — when the default agent is a native harness with a model choice
// (Claude Code / Codex) — a default model for new sessions. Reasoning-effort /
// harness stay per-agent run config, out of scope here. The host and agent
// pickers reuse the composer's components; the working directory reuses its
// filesystem browser (inline, so it scrolls inside the modal).
// Fields are optional: an unset one stores no default (an absent key), and an
// all-default dialog stores an empty config.

import { ChevronDownIcon } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useProjectConfig, useUpdateProjectConfig } from "@/hooks/useConversations";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHostModelOptions, useHosts } from "@/hooks/useHosts";
import { selectableSessionAgents } from "@/lib/agentGrouping";
import { isFeatureEnabled, sandboxOptionLabel } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { ProjectCollaborationSection } from "./ProjectCollaborationSection";
import { readAlwaysUseWorktree } from "@/lib/worktreeDefaultPreferences";
import { SANDBOX_HOST_CHOICE } from "@/lib/hostPreferences";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import {
  isNativeCodingAgent,
  nativeAgentHasCapability,
  nativeCodingAgentForAvailableAgent,
} from "@/lib/nativeCodingAgents";
import {
  createProject,
  deleteProjectEntry,
  getProjectCollaboration,
  listProjectEntries,
  putProjectEntry,
  type PostBindResult,
  type ProjectConfig,
  type ProjectHostEntry,
} from "@/lib/projectsApi";
import { shouldGuardDialogDismiss } from "@/lib/dialogDismissGuard";
import { ApiError } from "@/lib/sessionsApi";
import { AgentHarnessPicker } from "./NewChatDialog";
import { HostWorkspacePicker, isNavigablePath } from "./WorkspacePicker";

/** Select sentinel for "no default" — Radix Select can't hold an empty value. */
const NONE = "__none__";

/** A labeled row: label + optional hint on the left, the control on the right. */
function Field({
  label,
  hint,
  htmlFor,
  switchRow = false,
  children,
}: {
  label: string;
  hint?: string;
  htmlFor?: string;
  switchRow?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className={
        switchRow
          ? "grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-3 sm:grid-cols-[minmax(0,1fr)_18rem]"
          : "grid min-w-0 grid-cols-1 gap-1.5 sm:grid-cols-[minmax(0,1fr)_18rem] sm:gap-4"
      }
    >
      <label htmlFor={htmlFor} className="flex min-w-0 flex-col pt-1.5">
        <span className="font-medium text-ui">{label}</span>
        {hint && <span className="text-muted-foreground text-sm">{hint}</span>}
      </label>
      <div className="min-w-0 w-full">{children}</div>
    </div>
  );
}

/** Trim a text input to `undefined` when blank, so an empty field stores no
 *  default (an unset key) rather than an empty string. */
function trimOrUndef(value: string): string | undefined {
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed;
}

/** A draft entry row: one project directory per host. */
interface DirectoryRow {
  hostId: string;
  path: string;
}

/**
 * Seed the directory rows from the project's stored entries. A project with
 * no entry yet falls back to the config's `workspace` when it names a
 * concrete default host — Save then promotes that row into a real entry.
 */
function seedDirectoryRows(entries: ProjectHostEntry[], config: ProjectConfig): DirectoryRow[] {
  if (entries.length > 0) {
    return entries.map((entry) => ({ hostId: entry.host_id, path: entry.workspace }));
  }
  const hostId = config.host_id;
  const workspace = trimOrUndef(config.workspace ?? "");
  if (hostId !== undefined && hostId !== NONE && hostId !== SANDBOX_HOST_CHOICE && workspace) {
    return [{ hostId, path: workspace }];
  }
  return [];
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Try again.";
}

/** Hook statuses surfaced as a warning and that keep the dialog open on Save. */
const WARNING_HOOK_STATUSES = new Set(["failed", "timed_out", "unreachable"]);

function hookStatusLabel(status: string): string {
  return status === "timed_out" ? "timed out" : status;
}

/** The last post-bind outcome for one row, and where it came from. */
interface EntryHookOutcome {
  result: PostBindResult;
  /** Save surfaces only warnings; a per-row run shows any status. */
  source: "save" | "run";
}

export function ProjectSettingsDialog({
  open,
  onOpenChange,
  projectId,
  projectName,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** First-class project id, or `null` for a label-only folder (promoted on
   *  save — a row is created under `projectName` so its config can be stored). */
  projectId: string | null;
  projectName: string;
}) {
  const queryClient = useQueryClient();
  // A label-only folder has no row to read; its config starts empty and the
  // save promotes it. Fetch only runs for a first-class project.
  const { data: stored, isLoading, isError } = useProjectConfig(open ? projectId : null);
  // A failed config GET for a first-class project must NOT be treated as "no
  // config" — saving the resulting blank draft would send `{}` and wipe the
  // project's stored defaults. Block Save (and surface the error) until the
  // fetch succeeds. A label-only folder (no id) has nothing to load, so it's
  // never in this state.
  const loadFailed = projectId !== null && isError;
  const updateConfig = useUpdateProjectConfig();
  const hosts = useHosts();
  // Pin the stored default agent into discovery so an already-configured
  // session-scoped agent (archived / paginated out of the scan) still
  // resolves here — matching what the composer's prefill will see.
  const { data: agents } = useAvailableAgents({
    pinnedAgentIds: stored?.agent_id != null ? [stored.agent_id] : [],
  });
  const info = useServerInfo();
  // Collaboration config lives outside this form (its actions apply
  // immediately, never through Save) and only for a first-class project.
  const showCollaboration = projectId !== null && isFeatureEnabled(info, "project_assignments");
  // Sandbox is only a real default when the server can provision managed
  // sandbox hosts — mirror the composer's gate so we don't offer a target that
  // can only fail on create.
  const managedSandboxesEnabled = info !== "loading" && info.managed_sandboxes_enabled;
  const sandboxProvider = info !== "loading" ? info.sandbox_provider : null;

  // Draft fields. Seeded from the stored config + entries each time the dialog
  // opens (or the fetches arrive); local until saved.
  const [hostId, setHostId] = useState<string>(NONE);
  // One directory per host — the project's entry rows. The Host field above is
  // the default host; the rows are independent of it.
  const [directoryRows, setDirectoryRows] = useState<DirectoryRow[]>([]);
  // Which row's filesystem browser is expanded (host id), if any.
  const [openRow, setOpenRow] = useState<string | null>(null);
  // The first entry write to fail on Save: its server message renders on that
  // row, the sequence stops, and no config is written.
  const [entriesError, setEntriesError] = useState<{ hostId: string; message: string } | null>(
    null,
  );
  // Non-entry save failures (project promotion / config PATCH) — rendered with
  // the same alert as `updateConfig.error`.
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Post-bind outcome per host from the last Save (warnings only) or the
  // per-row run (any status), so the row can show it under its path.
  const [entryHookOutcomes, setEntryHookOutcomes] = useState<ReadonlyMap<string, EntryHookOutcome>>(
    () => new Map(),
  );
  // The row whose "Run post-bind command" is in flight, if any.
  const [runningPostBindHostId, setRunningPostBindHostId] = useState<string | null>(null);
  // The entry writes already persisted (host id → path), seeded from the
  // fetched rows and advanced as each write succeeds. Save derives its PUT /
  // DELETE plan from this baseline, not from the server rows, so a retry
  // after a partial failure (a DELETE that landed, a config PATCH that did
  // not) sends only what is still pending instead of repeating a DELETE that
  // now 404s.
  const [savedEntries, setSavedEntries] = useState<ReadonlyMap<string, string>>(() => new Map());
  // Worktree default for the project. The toggle seeds from the project's
  // stored value when set, else from the user-global "always use a worktree"
  // default (Settings › Git). On save it stays "inherit" (stores nothing) while
  // it matches the global default, and stores an explicit true/false only to
  // override it — so a project can force a worktree on or opt out of a global on.
  const [useWorktree, setUseWorktree] = useState(false);
  // Base branch a new worktree forks from; blank stores no default (falls
  // through to the user-global default in Settings › Git). Only meaningful
  // alongside the worktree default, but kept independent so it survives toggling.
  const [baseBranch, setBaseBranch] = useState("");
  const [agentId, setAgentId] = useState<string | null>(null);
  // Default model for new sessions, only meaningful when the default agent is
  // a native harness with a model choice; NONE stores no default (unset key).
  const [model, setModel] = useState<string>(NONE);
  const [activeTab, setActiveTab] = useState("defaults");
  const tabsId = useId();
  const { data: collaborationStatus } = useQuery({
    queryKey: ["project-collaboration", projectId],
    queryFn: () => getProjectCollaboration(projectId!),
    enabled: open && showCollaboration,
    retry: false,
  });
  // The single "Project directory" row set. A label-only folder has no stored
  // project yet, so there is nothing to fetch until Save promotes it.
  const {
    data: storedEntries,
    isPending: entriesPending,
    isError: entriesErrorState,
  } = useQuery({
    queryKey: ["project-entries", projectId],
    queryFn: () => listProjectEntries(projectId as string),
    enabled: open && projectId !== null,
    retry: false,
    staleTime: 30_000,
  });
  // Same rationale as `loadFailed`: a failed entries GET must not be read as
  // "no entries" — Save would reuse a blank row set and could delete/overwrite
  // rows it never saw.
  const entriesLoadFailed = projectId !== null && entriesErrorState;
  const entriesLoading = projectId !== null && entriesPending;
  const entries = useMemo(() => storedEntries ?? [], [storedEntries]);
  const hostsById = useMemo(
    () => new Map((hosts.data ?? []).map((host) => [host.host_id, host])),
    [hosts.data],
  );
  // Hosts the user owns that no row covers yet — the "Add host" choices.
  // Sandbox hosts are server-provisioned launch targets, never entry hosts
  // (the entries API refuses them).
  const addableHosts = (hosts.data ?? []).filter(
    (host) => !host.sandbox_provider && !directoryRows.some((row) => row.hostId === host.host_id),
  );

  useEffect(() => {
    if (open) {
      setActiveTab("defaults");
      // Outcomes are dialog-session state; a refetch must not wipe a warning
      // the user still needs to see.
      setEntryHookOutcomes(new Map());
      setRunningPostBindHostId(null);
    }
  }, [open]);

  // The agent picker and the host Select portal their dropdowns OUTSIDE
  // DialogContent, so their dismiss (pick an option / click the body while
  // open) can bubble up as an outside-interaction and close the whole modal.
  // Track open dropdowns + a grace window and swallow only those dismisses —
  // real backdrop clicks and Escape still close. (Mirrors the scheduled-task
  // dialog, whose guard helper we reuse.) Making the picker's dropdown modal is
  // what lets it scroll inside the Dialog's scroll-lock; this guard is the
  // trade-off that keeps that modal dropdown from closing the settings dialog.
  const dropdownOpenCountRef = useRef(0);
  const dropdownClosedAtRef = useRef(0);
  const onDropdownOpenChange = (isOpen: boolean) => {
    if (isOpen) {
      dropdownOpenCountRef.current += 1;
    } else {
      dropdownOpenCountRef.current = Math.max(0, dropdownOpenCountRef.current - 1);
      dropdownClosedAtRef.current = Date.now();
    }
  };
  const guardDialogDismiss = (event: {
    target: EventTarget | null;
    preventDefault: () => void;
  }) => {
    if (
      shouldGuardDialogDismiss(event.target, {
        selectOpen: dropdownOpenCountRef.current > 0,
        msSinceSelectClose: Date.now() - dropdownClosedAtRef.current,
      })
    ) {
      event.preventDefault();
    }
  };

  useEffect(() => {
    if (!open) return;
    // Don't seed a blank draft from a failed load — Save is blocked anyway, and
    // clobbering the fields would risk sending `{}` if the guard ever regressed.
    if (loadFailed || entriesLoadFailed) return;
    // Wait for the entry rows, or the config-fallback row would be seeded and
    // then immediately replaced by the fetched rows.
    if (entriesLoading) return;
    const c: ProjectConfig = stored ?? {};
    setHostId(c.host_id ?? NONE);
    setDirectoryRows(seedDirectoryRows(entries, c));
    setSavedEntries(new Map(entries.map((entry) => [entry.host_id, entry.workspace])));
    setUseWorktree(c.use_worktree ?? readAlwaysUseWorktree());
    setBaseBranch(c.base_branch ?? "");
    setAgentId(c.agent_id ?? null);
    setModel(c.model ?? NONE);
    setOpenRow(null);
    setEntriesError(null);
    setSaveError(null);
  }, [open, stored, loadFailed, entriesLoadFailed, entriesLoading, entries]);

  // Entry sync derived from the persisted baseline + draft rows: PUTs for
  // rows whose path changed (or that are new), DELETEs for persisted rows the
  // user removed. Comparing against `savedEntries` (not the query's rows) is
  // what lets a retry converge after a partial failure.
  const changedRows = directoryRows.filter((row) => {
    const path = row.path.trim();
    return path !== "" && savedEntries.get(row.hostId) !== path;
  });
  const removedHostIds = [...savedEntries.keys()].filter(
    (entryHostId) => !directoryRows.some((row) => row.hostId === entryHostId),
  );
  // The default host's row supplies the config's `workspace` mirror. A missing
  // row, a blank path, or the sandbox default host leaves it unset.
  const defaultRow =
    hostId !== NONE && hostId !== SANDBOX_HOST_CHOICE
      ? directoryRows.find((row) => row.hostId === hostId)
      : undefined;
  const defaultHostRowPath = defaultRow ? trimOrUndef(defaultRow.path) : undefined;

  const addDirectoryRow = (rowHostId: string) => {
    setEntriesError(null);
    setDirectoryRows((rows) =>
      rows.some((row) => row.hostId === rowHostId)
        ? rows
        : [...rows, { hostId: rowHostId, path: "" }],
    );
  };

  const removeDirectoryRow = (rowHostId: string) => {
    setEntriesError(null);
    setOpenRow((current) => (current === rowHostId ? null : current));
    setDirectoryRows((rows) => rows.filter((row) => row.hostId !== rowHostId));
  };

  const setDirectoryPath = (rowHostId: string, path: string) => {
    setEntriesError(null);
    setDirectoryRows((rows) =>
      rows.map((row) => (row.hostId === rowHostId ? { ...row, path } : row)),
    );
  };

  // Re-run the host's post-bind command for a persisted row by re-PUTting the
  // SAVED path (never the draft), and show whatever status comes back.
  const runPostBindCommand = async (rowHostId: string) => {
    const id = projectId;
    const savedPath = savedEntries.get(rowHostId);
    if (id === null || savedPath === undefined) return;
    setEntriesError(null);
    setRunningPostBindHostId(rowHostId);
    try {
      const written = await putProjectEntry(id, rowHostId, savedPath);
      const postBind = written.post_bind;
      setEntryHookOutcomes((current) => {
        const next = new Map(current);
        if (postBind) next.set(rowHostId, { result: postBind, source: "run" });
        else next.delete(rowHostId);
        return next;
      });
      void queryClient.invalidateQueries({ queryKey: ["project-host-roots", id] });
      void queryClient.invalidateQueries({ queryKey: ["project-collaboration", id] });
    } catch (error) {
      setEntriesError({ hostId: rowHostId, message: errorMessage(error) });
    } finally {
      setRunningPostBindHostId(null);
    }
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    // Guard against submitting a blank draft seeded from a failed load, which
    // the server would read as "clear the stored defaults".
    if (loadFailed || entriesLoadFailed || saving) return;
    // Preserve config keys owned by other dialogs, then replace only the fields
    // this form edits.
    const config: ProjectConfig = { ...(stored ?? {}) };
    if (hostId !== NONE) config.host_id = hostId;
    else delete config.host_id;
    // The stored workspace is only a single-host mirror for upstream readers:
    // the default host's row when it has one, else nothing. Placement reads the
    // entries once the project has any, so this can never route a session to a
    // directory the dialog didn't save.
    if (defaultHostRowPath) config.workspace = defaultHostRowPath;
    else delete config.workspace;
    if (agentId) config.agent_id = agentId;
    else delete config.agent_id;
    // Store the worktree choice only when it overrides the user-global default;
    // while it matches, leave the key unset so the project keeps inheriting
    // (and an all-default dialog still clears to {}).
    if (useWorktree !== readAlwaysUseWorktree()) config.use_worktree = useWorktree;
    else delete config.use_worktree;
    // A base branch only forks a worktree, so it's only meaningful when the
    // worktree default is on — drop it otherwise so it can't linger as a stale,
    // invisible default after the toggle is turned off.
    const base = useWorktree ? trimOrUndef(baseBranch) : undefined;
    if (base) config.base_branch = base;
    else delete config.base_branch;
    // A model default is only meaningful for an agent whose harness takes a
    // model override — drop it otherwise so a stale alias can't linger after
    // the default agent changes to one without a model choice. While the
    // stored default agent hasn't resolved from discovery yet, its capability
    // is unknowable — keep the stored model (already in the spread) so an
    // unrelated save during that window can't silently delete a valid default.
    const agentUnresolved = agentId != null && selectedAgent === null;
    if (supportsModelDefault && model !== NONE) config.model = model;
    else if (!agentUnresolved) delete config.model;

    setEntriesError(null);
    setSaveError(null);
    setSaving(true);
    let id = projectId;
    // Whether any entry PUT / DELETE committed this run: the finally block
    // refreshes host-roots + collaboration even when a later write fails.
    let entryWriteCommitted = false;
    // A hook warning keeps the dialog open after the save instead of closing.
    let hookWarningSeen = false;
    try {
      if (id === null) {
        // A label-only folder is promoted first; the entry writes below use the
        // returned id.
        try {
          id = (await createProject(projectName)).id;
        } catch (error) {
          setSaveError(errorMessage(error));
          return;
        }
      }
      // PUT changed rows, DELETE removed rows, sequentially: the first failure
      // stops the sequence, its server message lands on that row, the dialog
      // stays open and no config is written. Sequential by design — the next
      // write must not start after a failure. Each success advances the
      // baseline so a retry only sends what is still pending.
      for (const row of changedRows) {
        const path = row.path.trim();
        let written: ProjectHostEntry & { post_bind?: PostBindResult };
        try {
          // eslint-disable-next-line no-await-in-loop
          written = await putProjectEntry(id, row.hostId, path);
        } catch (error) {
          setEntriesError({ hostId: row.hostId, message: errorMessage(error) });
          return;
        }
        entryWriteCommitted = true;
        setSavedEntries((current) => new Map(current).set(row.hostId, path));
        const postBind = written.post_bind;
        if (postBind && WARNING_HOOK_STATUSES.has(postBind.status)) hookWarningSeen = true;
        setEntryHookOutcomes((current) => {
          const next = new Map(current);
          if (postBind) next.set(row.hostId, { result: postBind, source: "save" });
          else next.delete(row.hostId);
          return next;
        });
      }
      for (const removedHostId of removedHostIds) {
        try {
          // eslint-disable-next-line no-await-in-loop
          await deleteProjectEntry(id, removedHostId);
        } catch (error) {
          // Already gone (a committed earlier attempt, or another tab):
          // the goal state holds, so the save continues.
          if (!(error instanceof ApiError && error.status === 404)) {
            setEntriesError({ hostId: removedHostId, message: errorMessage(error) });
            return;
          }
        }
        entryWriteCommitted = true;
        setSavedEntries((current) => {
          const next = new Map(current);
          next.delete(removedHostId);
          return next;
        });
      }
      try {
        await updateConfig.mutateAsync({ id, name: projectName, config });
      } catch {
        // updateConfig.isError renders the server message below.
        return;
      }
      void queryClient.invalidateQueries({ queryKey: ["project-entries", id] });
      if (!hookWarningSeen) onOpenChange(false);
    } finally {
      if (entryWriteCommitted && id !== null) {
        // Partial progress included: the Collaboration tab reads host-roots
        // for the entry default, and the entries refetch would reseed drafts.
        void queryClient.invalidateQueries({ queryKey: ["project-host-roots", id] });
        void queryClient.invalidateQueries({ queryKey: ["project-collaboration", id] });
      }
      setSaving(false);
    }
  };

  // Offer only online hosts (an offline host can only fail on create), plus the
  // sandbox when the server supports it. If the project's stored host is no
  // longer online, keep it as a labeled fallback item so opening + saving the
  // dialog doesn't silently drop the saved default.
  const onlineHosts = (hosts.data ?? []).filter((h) => h.status === "online");
  const storedHostMissing =
    hostId !== NONE &&
    hostId !== SANDBOX_HOST_CHOICE &&
    !onlineHosts.some((h) => h.host_id === hostId);
  // The filesystem browser needs a concrete, online host to list against — so
  // it's only offered when a real host is the default (not sandbox / no-default
  // / an offline stored host). Otherwise the field is a plain path input.
  const browsableHostId =
    hostId !== NONE && hostId !== SANDBOX_HOST_CHOICE && !storedHostMissing ? hostId : null;

  // The default host is a config field of its own — changing it leaves the
  // per-host directory rows untouched. Close any open browser so the newly
  // selected default host's row starts collapsed.
  const onHostChange = (nextHostId: string) => {
    if (nextHostId !== hostId) setOpenRow(null);
    setHostId(nextHostId);
  };

  // Agent picker groups, mirroring the composer's split (native harness CLIs vs
  // SDK / bundle agents). The picker takes both lists and a selection.
  // Same resolver as the composer's picker (selectableSessionAgents): the two
  // surfaces must offer the SAME set, or a project could pin a default the
  // composer refuses to show — and then silently substitutes another for.
  const agentList = useMemo(() => selectableSessionAgents(agents ?? []), [agents]);
  const harnessEntries = useMemo(() => agentList.filter(isNativeCodingAgent), [agentList]);
  const agentEntries = useMemo(() => agentList.filter((a) => !isNativeCodingAgent(a)), [agentList]);
  const selectedAgent = agentList.find((a) => a.id === agentId) ?? null;
  const agentLabel = selectedAgent ? selectedAgent.display_name : "No default";
  // Model default is offered for harnesses whose create call carries a model
  // override — the declared modelPicker capability, plus Codex, whose picker
  // is host-resolved rather than capability-flagged (mirrors the composer's
  // model_override gate in NewChatDialog).
  const selectedNativeSpec = nativeCodingAgentForAvailableAgent(selectedAgent);
  const harnessTakesModel =
    nativeAgentHasCapability(selectedAgent, "modelPicker") ||
    selectedNativeSpec?.harness === "codex-native";
  // Live host-resolved model options. The project's default host when set,
  // else the first online host (the composer's auto-pick) — without the
  // fallback a Codex project (no static catalog) could never populate the
  // picker unless a host default was also stored.
  const modelCatalogHostId = browsableHostId ?? onlineHosts[0]?.host_id ?? null;
  const { data: hostModelOptions } = useHostModelOptions(
    modelCatalogHostId,
    selectedNativeSpec?.harness ?? "",
    harnessTakesModel && modelCatalogHostId !== null,
  );
  const modelOptions = useMemo(() => {
    const live = (hostModelOptions ?? []).map((o) => ({
      id: o.id,
      label: o.displayName ?? o.id,
    }));
    if (live.length > 0) return live;
    return selectedNativeSpec?.harness === "claude-native"
      ? CLAUDE_NATIVE_MODELS.map((m) => ({ id: m.id, label: m.label }))
      : [];
  }, [hostModelOptions, selectedNativeSpec]);
  // Keep a stored model the current options don't list as a labeled fallback
  // item, so opening + saving the dialog doesn't silently drop the default.
  const storedModelMissing = model !== NONE && !modelOptions.some((m) => m.id === model);
  // With no catalog resolved and nothing stored, the select could only offer
  // "No default" — an action it can't perform. Degrade honestly (documented
  // catalog gap for host-resolved harnesses): disable it and say why, rather
  // than hide the field the harness legitimately supports.
  const modelPickerEmpty = modelOptions.length === 0 && model === NONE;
  // Offer the control only for a model-taking harness; when it can only render
  // "No default" it stays visible but disabled with an explanatory hint.
  const supportsModelDefault = harnessTakesModel;
  // The host the agent picker's readiness badges check against (its config
  // hints show whether a harness is set up there). Null when no concrete host.
  const warningHost = onlineHosts.find((h) => h.host_id === browsableHostId) ?? null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        onClick={(e) => e.stopPropagation()}
        className="flex w-[calc(100%-1rem)] max-w-[calc(100%-1rem)] flex-col gap-0 overflow-hidden rounded-2xl p-0 max-h-[85vh] sm:max-w-[45rem] sm:rounded-[30px]"
        // Keep a nested dropdown's dismiss (pick an option, or click the modal
        // body while it's open) from closing the whole Dialog. See
        // `guardDialogDismiss`; real backdrop clicks and Escape still close.
        onPointerDownOutside={guardDialogDismiss}
        onInteractOutside={guardDialogDismiss}
      >
        <DialogHeader className="shrink-0 border-b px-5 pt-5 sm:px-6 sm:pt-6">
          <DialogTitle>Project settings</DialogTitle>
          <DialogDescription>
            Defaults and collaboration for <b>{projectName}</b>.
          </DialogDescription>
          {showCollaboration && (
            <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
              <TabsList variant="line" className="mt-3 -mb-px w-full justify-start gap-4 px-0">
                <TabsTrigger
                  value="defaults"
                  id={`${tabsId}-defaults-tab`}
                  aria-controls="project-settings-defaults-form"
                  className="flex-none px-2 pb-3"
                >
                  Session defaults
                </TabsTrigger>
                <TabsTrigger
                  value="collaboration"
                  id={`${tabsId}-collaboration-tab`}
                  aria-controls={`${tabsId}-collaboration-panel`}
                  className="flex-none px-2 pb-3"
                >
                  Collaboration
                  {collaborationStatus?.enabled && (
                    <span className="rounded-full bg-primary/10 px-1.5 text-xs text-primary">
                      On
                    </span>
                  )}
                </TabsTrigger>
              </TabsList>
            </Tabs>
          )}
        </DialogHeader>
        <form
          id="project-settings-defaults-form"
          role={showCollaboration ? "tabpanel" : undefined}
          aria-labelledby={showCollaboration ? `${tabsId}-defaults-tab` : undefined}
          tabIndex={showCollaboration ? 0 : undefined}
          onSubmit={onSubmit}
          hidden={activeTab !== "defaults"}
          className={
            activeTab === "defaults"
              ? "flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-y-auto overflow-x-hidden px-5 py-5 sm:px-6"
              : "hidden"
          }
        >
          <Field label="Host" hint="Where new sessions run by default">
            <Select
              value={hostId}
              onValueChange={onHostChange}
              onOpenChange={onDropdownOpenChange}
              disabled={isLoading}
            >
              <SelectTrigger className="w-full" data-testid="project-settings-host">
                <SelectValue placeholder="No default" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NONE}>No default</SelectItem>
                {managedSandboxesEnabled && (
                  <SelectItem value={SANDBOX_HOST_CHOICE}>
                    {sandboxOptionLabel(sandboxProvider)}
                  </SelectItem>
                )}
                {onlineHosts.map((h) => (
                  <SelectItem key={h.host_id} value={h.host_id}>
                    {h.name}
                  </SelectItem>
                ))}
                {storedHostMissing && (
                  <SelectItem value={hostId}>
                    {stored?.host_id === hostId ? `${hostId} (unavailable)` : hostId}
                  </SelectItem>
                )}
              </SelectContent>
            </Select>
          </Field>

          {/* One project directory per host — the project's entry rows. The
              Host field above stays the default host; these rows are what
              sessions open in. */}
          <div className="grid min-w-0 grid-cols-1 gap-1.5">
            <div className="flex min-w-0 flex-col">
              <span className="font-medium text-ui">Project directory</span>
              <span className="text-muted-foreground text-sm">
                Where new sessions open — one directory per host
              </span>
            </div>
            <div className="flex min-w-0 flex-col gap-2" data-testid="project-settings-directories">
              {directoryRows.length === 0 && (
                <p
                  className="rounded-md border border-dashed px-3 py-2 text-muted-foreground text-ui"
                  data-testid="project-settings-directories-empty"
                >
                  No project directory yet
                </p>
              )}
              {directoryRows.map((row) => {
                const rowHost = hostsById.get(row.hostId);
                const rowError = entriesError?.hostId === row.hostId ? entriesError : null;
                // The filesystem browser needs a live host; an offline or
                // unregistered host falls back to typing a path.
                const browsable = rowHost?.status === "online";
                const rowOpen = openRow === row.hostId;
                const rowSaved = savedEntries.has(row.hostId);
                const rowOutcome = entryHookOutcomes.get(row.hostId);
                const rowOutcomeWarning = rowOutcome
                  ? WARNING_HOOK_STATUSES.has(rowOutcome.result.status)
                  : false;
                const showOutcome =
                  rowOutcome !== undefined && (rowOutcome.source === "run" || rowOutcomeWarning);
                return (
                  <div
                    key={row.hostId}
                    className="flex min-w-0 flex-col gap-1.5 rounded-md border p-2"
                    data-testid={`project-settings-entry-${row.hostId}`}
                  >
                    <div className="flex min-w-0 items-center justify-between gap-2">
                      <span
                        className="min-w-0 truncate text-ui"
                        title={rowHost?.name ?? row.hostId}
                      >
                        {rowHost?.name ?? row.hostId}
                      </span>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-auto shrink-0 p-0 text-muted-foreground text-sm hover:bg-transparent"
                        data-testid={`project-settings-entry-remove-${row.hostId}`}
                        onClick={() => removeDirectoryRow(row.hostId)}
                        disabled={isLoading || saving}
                      >
                        Remove
                      </Button>
                    </div>
                    {browsable ? (
                      // A compact trigger showing the current path; clicking
                      // expands the filesystem browser as an overlay anchored
                      // to the trigger. The browser is rendered inside
                      // DialogContent (not a portaled popover) so it scrolls —
                      // a modal Dialog's scroll-lock blocks wheel events on
                      // portaled content — but positioned `absolute` so it
                      // floats over the fields below. onNavigate updates the
                      // row's path live as you browse.
                      <div className="relative flex flex-col gap-1.5">
                        <button
                          type="button"
                          onClick={() => setOpenRow(rowOpen ? null : row.hostId)}
                          aria-expanded={rowOpen}
                          disabled={isLoading || saving}
                          data-testid={`project-settings-entry-browse-${row.hostId}`}
                          aria-label={`Project directory on ${rowHost?.name ?? row.hostId}`}
                          className="flex h-8 w-full items-center justify-between gap-2 rounded-md border border-input bg-transparent px-3 text-ui outline-none disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          <span
                            className={
                              row.path
                                ? "min-w-0 truncate font-mono"
                                : "truncate text-muted-foreground"
                            }
                            title={row.path || undefined}
                          >
                            {row.path || "Browse…"}
                          </span>
                          <ChevronDownIcon
                            className={`size-4 shrink-0 opacity-50 transition-transform ${
                              rowOpen ? "rotate-180" : ""
                            }`}
                          />
                        </button>
                        {rowOpen && (
                          <>
                            {/* Click-away: a transparent full-modal backdrop
                            that closes the browser (keeping the current path)
                            on any click outside it. */}
                            <button
                              type="button"
                              aria-label="Close directory browser"
                              className="fixed inset-0 z-10 cursor-default"
                              onClick={() => setOpenRow(null)}
                            />
                            <div className="absolute top-full right-0 left-0 z-20 mt-1 rounded-[12px] border border-border bg-popover p-2 shadow-menu dark:border-white/10 dark:backdrop-blur-xl dark:backdrop-saturate-150 [&>[data-testid=workspace-picker]]:border-0">
                              <HostWorkspacePicker
                                hostId={row.hostId}
                                initialPath={isNavigablePath(row.path) ? row.path : undefined}
                                onNavigate={(path) => setDirectoryPath(row.hostId, path)}
                              />
                            </div>
                          </>
                        )}
                      </div>
                    ) : (
                      <input
                        data-testid={`project-settings-entry-path-${row.hostId}`}
                        aria-label={`Project directory on ${rowHost?.name ?? row.hostId}`}
                        className="w-full rounded-md border bg-transparent px-3 py-2 text-ui outline-none disabled:cursor-not-allowed disabled:opacity-50"
                        placeholder="/path/to/repo"
                        value={row.path}
                        title={row.path || undefined}
                        onChange={(e) => setDirectoryPath(row.hostId, e.target.value)}
                        disabled={isLoading || saving}
                      />
                    )}
                    {rowSaved && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-auto self-start p-0 text-muted-foreground text-sm hover:bg-transparent"
                        data-testid={`project-settings-entry-run-post-bind-${row.hostId}`}
                        onClick={() => void runPostBindCommand(row.hostId)}
                        disabled={isLoading || saving || runningPostBindHostId !== null}
                      >
                        Run post-bind command
                      </Button>
                    )}
                    {showOutcome && rowOutcome && (
                      <div
                        className={
                          rowOutcomeWarning
                            ? "text-destructive text-ui"
                            : "text-ui text-muted-foreground"
                        }
                        role="status"
                        data-testid={`project-settings-entry-post-bind-${row.hostId}`}
                      >
                        {rowOutcome.result.status === "ok" ? (
                          "Post-bind command succeeded"
                        ) : (
                          <>
                            {rowOutcomeWarning ? "Directory saved; " : ""}post-bind command{" "}
                            {hookStatusLabel(rowOutcome.result.status)}
                            {rowOutcome.result.error ? `: ${rowOutcome.result.error}` : ""}
                            {typeof rowOutcome.result.exit_code === "number"
                              ? ` (exit code ${rowOutcome.result.exit_code})`
                              : ""}
                            {rowOutcome.result.output ? (
                              <pre className="mt-1 whitespace-pre-wrap">
                                {rowOutcome.result.output}
                              </pre>
                            ) : null}
                          </>
                        )}
                      </div>
                    )}
                    {rowError && (
                      <p
                        className="text-destructive text-ui"
                        role="alert"
                        data-testid={`project-settings-entry-error-${row.hostId}`}
                      >
                        {rowError.message}
                      </p>
                    )}
                  </div>
                );
              })}
              {entriesError && !directoryRows.some((row) => row.hostId === entriesError.hostId) && (
                // A DELETE that failed has no row left to carry the message.
                <p
                  className="text-destructive text-ui"
                  role="alert"
                  data-testid="project-settings-entries-error"
                >
                  {entriesError.message}
                </p>
              )}
              {addableHosts.length > 0 && (
                // A menu-shaped Select: its value never changes, so picking a
                // host adds a row and the trigger keeps reading "Add host".
                <Select
                  value={NONE}
                  onValueChange={(value) => {
                    if (value !== NONE) addDirectoryRow(value);
                  }}
                  onOpenChange={onDropdownOpenChange}
                  disabled={isLoading || saving}
                >
                  <SelectTrigger className="w-full" data-testid="project-settings-add-host">
                    <SelectValue placeholder="Add host" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={NONE}>Add host</SelectItem>
                    {addableHosts.map((host) => (
                      <SelectItem key={host.host_id} value={host.host_id}>
                        {host.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
          </div>

          <Field
            switchRow
            label="Random worktree"
            hint="Start each new session in a fresh randomly-named git worktree (vs. directly in the workspace). Overrides the global default in Settings › Git."
          >
            <div className="flex justify-end">
              <Switch
                data-testid="project-settings-worktree"
                checked={useWorktree}
                onCheckedChange={setUseWorktree}
                disabled={isLoading}
              />
            </div>
          </Field>

          {useWorktree && (
            <Field
              label="Base branch"
              hint="Branch new worktrees fork from; blank uses the current branch"
              htmlFor="project-settings-base-branch"
            >
              <input
                id="project-settings-base-branch"
                data-testid="project-settings-base-branch"
                className="w-full rounded-md border bg-transparent px-3 py-2 text-ui outline-none disabled:cursor-not-allowed disabled:opacity-50"
                placeholder="e.g. main"
                value={baseBranch}
                onChange={(e) => setBaseBranch(e.target.value)}
                disabled={isLoading}
              />
            </Field>
          )}

          <Field label="Agent" hint="Default agent / harness for new sessions">
            <div className="flex flex-col items-end gap-1" data-testid="project-settings-agent">
              <AgentHarnessPicker
                agentEntries={agentEntries}
                harnessEntries={harnessEntries}
                effectiveAgentId={agentId}
                agentLabel={agentLabel}
                hasAgents={agentList.length > 0}
                host={warningHost}
                onSelectAgent={(a) => {
                  // A model default belongs to the picked harness's vocab —
                  // don't carry an alias onto a different agent.
                  if (a.id !== agentId) setModel(NONE);
                  setAgentId(a.id);
                }}
                pendingAgent={null}
                pendingAgentId="__unused_pending_agent__"
                onSelectPending={() => {}}
                // No interactive create flow here, so hide the "Create custom
                // agent" action and leave the handler inert.
                onCreateCustomAgent={() => {}}
                allowCreateCustomAgent={false}
                sandboxSelected={hostId === SANDBOX_HOST_CHOICE}
                // Modal so the menu establishes its own scroll context and can
                // scroll inside the Dialog's scroll-lock (a non-modal dropdown
                // portals outside that lock and can't scroll). The dismiss
                // guard on DialogContent keeps this modal dropdown's own close
                // from bubbling up and closing the settings dialog.
                dropdownModal
                onOpenChange={onDropdownOpenChange}
                // Bound the menu height so it scrolls inside the modal instead
                // of running off the bottom; fixed width matches the composer.
                contentClassName="max-h-80 w-80"
                contentAlign="end"
                // Fill the field column and match the sibling <Select> triggers
                // (full width, bordered, h-8) so the control right-aligns with
                // the host / effort dropdowns instead of floating mid-row.
                triggerClassName="h-8 w-full justify-between rounded-md border border-input bg-transparent px-3 text-foreground hover:bg-transparent hover:text-foreground"
                triggerLabelClassName="max-w-none text-ui"
              />
              {agentId && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-auto p-0 text-muted-foreground text-sm hover:bg-transparent"
                  onClick={() => {
                    setAgentId(null);
                    setModel(NONE);
                  }}
                >
                  Clear
                </Button>
              )}
            </div>
          </Field>

          {supportsModelDefault && (
            <Field
              label="Model"
              hint={
                modelPickerEmpty
                  ? "No model catalog available — pick a Host default (or connect a host) to choose from its models"
                  : "Default model for new sessions with this agent"
              }
            >
              <Select
                value={model}
                onValueChange={setModel}
                onOpenChange={onDropdownOpenChange}
                disabled={isLoading || modelPickerEmpty}
              >
                <SelectTrigger className="w-full" data-testid="project-settings-model">
                  <SelectValue placeholder="No default" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>No default</SelectItem>
                  {modelOptions.map((m) => (
                    <SelectItem key={m.id} value={m.id}>
                      {m.label}
                    </SelectItem>
                  ))}
                  {storedModelMissing && <SelectItem value={model}>{model}</SelectItem>}
                </SelectContent>
              </Select>
            </Field>
          )}

          {loadFailed && (
            <p
              className="text-destructive text-ui"
              role="alert"
              data-testid="project-settings-load-error"
            >
              Couldn't load this project's settings. Close and reopen to try again — saving is
              disabled so your existing defaults aren't overwritten.
            </p>
          )}
          {entriesLoadFailed && (
            <p
              className="text-destructive text-ui"
              role="alert"
              data-testid="project-settings-entries-load-error"
            >
              Couldn't load this project's directories. Close and reopen to try again — saving is
              disabled so they aren't overwritten.
            </p>
          )}
          {(saveError ?? (updateConfig.isError ? (updateConfig.error as Error).message : null)) && (
            <p className="text-destructive text-ui" role="alert">
              {saveError ?? (updateConfig.error as Error).message}
            </p>
          )}
        </form>
        {showCollaboration && projectId !== null && (
          <div
            id={`${tabsId}-collaboration-panel`}
            role="tabpanel"
            aria-labelledby={`${tabsId}-collaboration-tab`}
            tabIndex={0}
            hidden={activeTab !== "collaboration"}
            className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden px-5 py-5 sm:px-6"
          >
            <ProjectCollaborationSection projectId={projectId} />
          </div>
        )}
        <DialogFooter className="m-0 shrink-0 rounded-none border-t bg-popover px-5 py-4 sm:px-6 sm:py-4">
          {activeTab === "defaults" ? (
            <>
              <Button
                type="button"
                variant="ghost"
                onClick={() => onOpenChange(false)}
                disabled={updateConfig.isPending || saving}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                form="project-settings-defaults-form"
                data-testid="project-settings-save"
                loading={updateConfig.isPending || saving}
                disabled={isLoading || entriesLoading || loadFailed || entriesLoadFailed}
              >
                Save
              </Button>
            </>
          ) : (
            <>
              <span className="mr-auto self-center text-sm text-muted-foreground">
                Changes here save immediately.
              </span>
              <Button type="button" onClick={() => onOpenChange(false)}>
                Done
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
