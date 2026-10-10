// Editor for a project's stored default session settings (`config`), reached
// from the project-folder kebab menu. Writing a project's config is what lets
// the new-chat composer pre-fill host / working directory / agent and the
// isolated-worktree default when starting a session in the project.
//
// A first-class project shows two tabs: "Session defaults" (host / per-host
// agent / model / effort / worktree) and "Code" (the project's repositories
// and folders, whose actions save immediately). A label-only folder has no
// project row yet, so it keeps the single form below — its Save creates the
// project and writes the directories it collected.
//
// "Session defaults" is a Hosts block: a per-host list (default agent / model
// / effort) with a detail pane at >= md and a single-open accordion below,
// plus an "All hosts" row carrying the legacy `config.agent_id` /
// `config.model` fallback. Per-host sets live in `config.calling_defaults`,
// shape-validated server-side; the resolution chain that consumes them is
// server-side. The All-hosts agent picker reuses the composer's component;
// the label-only Directory field reuses its filesystem browser (inline, so it
// scrolls inside the modal).
// Fields are optional: an unset one stores no default (an absent key), and an
// all-default dialog stores an empty config.

import { ChevronDownIcon, PencilIcon, Trash2Icon } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, type FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";
import { SessionDefaultModeSelect } from "@/components/SessionDefaultModeSelect";
import { sessionDefaultModeOptions } from "@/lib/sessionDefaultModes";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EFFORT_SELECT_NONE, MODEL_SELECT_DEFAULT } from "@/components/HarnessConfigControls";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useProjectConfig, useUpdateProjectConfig } from "@/hooks/useConversations";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHosts, type Host } from "@/hooks/useHosts";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { isSdkAgent, selectableSessionAgents } from "@/lib/agentGrouping";
import { CALLING_DEFAULT_HARNESSES, callingHarnessLabel } from "@/lib/callingDefaults";
import {
  listCallingDefaultCatalogs,
  syncCallingDefaults,
  type CallingDefaultsCatalogRow,
} from "@/lib/callingDefaultsApi";
import { sandboxOptionLabel } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { nativeModelLabel, normalizeEffortLabel } from "@/lib/composerModelLabel";
import { shouldGuardDialogDismiss } from "@/lib/dialogDismissGuard";
import { harnessReadinessOnHost } from "@/lib/harnessSetup";
import { SANDBOX_HOST_CHOICE } from "@/lib/hostPreferences";
import { effortLevelsFor } from "@/lib/modelEffortOptions";
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
import { ApiError } from "@/lib/sessionsApi";
import type { NativeModelOption } from "@/lib/types";
import { cn } from "@/lib/utils";
import { readAlwaysUseWorktree } from "@/lib/worktreeDefaultPreferences";
import { AgentHarnessPicker } from "./NewChatDialog";
import { ProjectCodeSection } from "./ProjectCodeSection";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";

/** Select sentinel for "no default" — Radix Select can't hold an empty value. */
const NONE = "__none__";

/** List-row id for the legacy all-hosts fallback row (never a real host id). */
const ALL_HOSTS = "__all_hosts__";

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

/** One harness entry inside a host's calling-defaults draft. */
interface HarnessSetDraft {
  speed?: string | null;
  permission?: string | null;
  model: string | null;
  effort: string | null;
}

/** One host's draft: its directory entry plus `config.calling_defaults[host]`. */
interface HostRowDraft extends DirectoryRow {
  agentId: string | null;
  /** Every harness entry, keyed by canonical harness id. The selected agent's
   *  harness is "the set"; every other key shows under "Other harness models". */
  harnesses: Record<string, HarnessSetDraft>;
}

/**
 * Seed the directory rows from the project's stored entries. With no entry
 * yet, the config's legacy `workspace` stands in when it names a concrete
 * default host: for a label-only folder Save promotes that row into an entry,
 * while a real project only shows it and keeps the stored value on Save.
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sanitizeHarnessSet(value: unknown): HarnessSetDraft {
  const set: HarnessSetDraft = { model: null, effort: null };
  if (!isRecord(value)) return set;
  if (typeof value.model === "string" && value.model !== "") set.model = value.model;
  if (typeof value.effort === "string" && value.effort !== "") set.effort = value.effort;
  for (const field of ["speed", "permission"] as const) {
    if (typeof value[field] === "string" && value[field] !== "") set[field] = value[field];
  }
  return set;
}

/** Parse one stored `calling_defaults[host]`; malformed fields are dropped. */
function sanitizeHostDefault(value: unknown): {
  agentId: string | null;
  harnesses: Record<string, HarnessSetDraft>;
} {
  const parsed: { agentId: string | null; harnesses: Record<string, HarnessSetDraft> } = {
    agentId: null,
    harnesses: {},
  };
  if (!isRecord(value)) return parsed;
  if (typeof value.agent_id === "string" && value.agent_id !== "") parsed.agentId = value.agent_id;
  if (isRecord(value.harnesses)) {
    for (const [harness, entry] of Object.entries(value.harnesses)) {
      if (harness !== "") parsed.harnesses[harness] = sanitizeHarnessSet(entry);
    }
  }
  return parsed;
}

/**
 * Seed the host rows from the entries plus the stored calling defaults. A host
 * with a stored default but no entry still gets a row, so its setting stays
 * visible (and deletable) instead of being dropped by the next save.
 */
function seedHostRows(entries: ProjectHostEntry[], config: ProjectConfig): HostRowDraft[] {
  const rows: HostRowDraft[] = seedDirectoryRows(entries, config).map((row) => ({
    ...row,
    agentId: null,
    harnesses: {},
  }));
  const defaults = isRecord(config.calling_defaults) ? config.calling_defaults : {};
  for (const [hostId, value] of Object.entries(defaults)) {
    if (hostId === "") continue;
    const parsed = sanitizeHostDefault(value);
    const existing = rows.find((row) => row.hostId === hostId);
    if (existing) {
      existing.agentId = parsed.agentId;
      existing.harnesses = parsed.harnesses;
    } else {
      rows.push({ hostId, path: "", ...parsed });
    }
  }
  return rows;
}

/** Build the server-shaped `config.calling_defaults`, omitting empty entries. */
function buildCallingDefaults(rows: readonly HostRowDraft[]): ProjectConfig["calling_defaults"] {
  const defaults: NonNullable<ProjectConfig["calling_defaults"]> = {};
  for (const row of rows) {
    const entry: NonNullable<ProjectConfig["calling_defaults"]>[string] = {};
    if (row.agentId) entry.agent_id = row.agentId;
    const harnesses: NonNullable<typeof entry.harnesses> = {};
    for (const [harness, set] of Object.entries(row.harnesses)) {
      const clean: { model?: string; effort?: string; speed?: string; permission?: string } = {};
      if (set.model) clean.model = set.model;
      if (set.effort) clean.effort = set.effort;
      if (set.speed) clean.speed = set.speed;
      if (set.permission) clean.permission = set.permission;
      if (Object.keys(clean).length > 0) harnesses[harness] = clean;
    }
    if (Object.keys(harnesses).length > 0) entry.harnesses = harnesses;
    if (Object.keys(entry).length > 0) defaults[row.hostId] = entry;
  }
  return Object.keys(defaults).length > 0 ? defaults : undefined;
}

/** The harness's ladder, keeping a stored value the catalog doesn't list. */
function effortLevelsForSet(
  harness: string,
  models: readonly NativeModelOption[],
  set: HarnessSetDraft,
): string[] {
  const levels = Array.from(new Set(effortLevelsFor(harness, models, set.model) ?? []));
  if (set.effort && !levels.includes(set.effort)) levels.unshift(set.effort);
  return levels;
}

/** The canonical harness an agent runs, or null for an unknown bundle. */
function agentHarness(agent: AvailableAgent): string | null {
  return nativeCodingAgentForAvailableAgent(agent)?.harness ?? agent.harness ?? null;
}

/** A joint agent stores no model / effort of its own — its members do. */
function isJointAgent(agent: AvailableAgent): boolean {
  return agent.id.startsWith("ca_") || (agent.members?.length ?? 0) > 1;
}

/** The harness whose model / effort the host detail shows as "the set". */
function selectedHarnessFor(agent: AvailableAgent | null): string | null {
  if (agent === null || isJointAgent(agent)) return null;
  return agentHarness(agent);
}

/** Same usability rule as the composer picker: unready agents aren't offered. */
function agentUsableOnHost(agent: AvailableAgent, host: Host | undefined): boolean {
  if (agent.id.startsWith("ca_") && !agent.harness) return true;
  return harnessReadinessOnHost(agent.harness, host).selectable;
}

function pairKey(hostId: string, harness: string): string {
  return `${hostId}\u0000${harness}`;
}

function catalogFor(
  catalogs: readonly CallingDefaultsCatalogRow[],
  hostId: string,
  harness: string,
): CallingDefaultsCatalogRow | undefined {
  return catalogs.find((row) => row.host_id === hostId && row.harness === harness);
}

/** Whether a catalog row accepts *model*: the server matches `id` or `model`. */
function catalogAccepts(row: CallingDefaultsCatalogRow, model: string): boolean {
  return row.models.some((option) => option.id === model || option.model === model);
}

/** Merge filtered-sync rows over the loaded list, replacing matching pairs. */
function mergeCatalogRows(
  previous: readonly CallingDefaultsCatalogRow[],
  next: readonly CallingDefaultsCatalogRow[],
): CallingDefaultsCatalogRow[] {
  const byPair = new Map(previous.map((row) => [pairKey(row.host_id, row.harness), row]));
  for (const row of next) byPair.set(pairKey(row.host_id, row.harness), row);
  return [...byPair.values()];
}

function catalogModelLabel(
  catalogs: readonly CallingDefaultsCatalogRow[],
  hostId: string,
  harness: string,
  model: string | null,
): string | null {
  if (!model) return null;
  const option = catalogFor(catalogs, hostId, harness)?.models.find(
    (candidate) => candidate.id === model,
  );
  return option ? nativeModelLabel(option) : model;
}

/** The list row's chip: agent · model · effort, or the unset fallback. */
function hostRowSummary(
  row: HostRowDraft,
  agents: readonly AvailableAgent[],
  catalogs: readonly CallingDefaultsCatalogRow[],
): string {
  const agent = row.agentId ? (agents.find((a) => a.id === row.agentId) ?? null) : null;
  if (agent === null && row.agentId) {
    // Discovery hasn't resolved the stored agent; show its id, not "Not set".
    return row.agentId;
  }
  if (agent) {
    const parts = [agent.display_name];
    if (isJointAgent(agent)) {
      parts.push("Set by members");
    } else {
      const harness = selectedHarnessFor(agent);
      const set = harness ? row.harnesses[harness] : undefined;
      const model = harness
        ? catalogModelLabel(catalogs, row.hostId, harness, set?.model ?? null)
        : null;
      if (model) parts.push(model);
      if (set?.effort) parts.push(normalizeEffortLabel(set.effort));
    }
    return parts.join(" · ");
  }
  for (const [harness, set] of Object.entries(row.harnesses)) {
    if (!set.model && !set.effort) continue;
    const parts = [callingHarnessLabel(harness)];
    const model = catalogModelLabel(catalogs, row.hostId, harness, set.model);
    if (model) parts.push(model);
    if (set.effort) parts.push(normalizeEffortLabel(set.effort));
    return parts.join(" · ");
  }
  return "Not set · uses master table";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Try again.";
}

/** Hook statuses surfaced as a warning and that keep the dialog open on Save. */
const WARNING_HOOK_STATUSES = new Set(["failed", "timed_out", "unreachable"]);

function hookStatusLabel(status: string): string {
  return status === "timed_out" ? "timed out" : status;
}

const SYNC_ALL_KEY = ["calling-defaults", "sync-all"] as const;
const SYNC_ALL_STALE_MS = 5 * 60_000;

/**
 * The cached `(host, harness)` catalogs behind the model dropdowns. Opening
 * the dialog lists the cached rows and starts one full sync (every online
 * host × configured harness, same as Settings › Calling defaults › "Sync
 * models"), replacing the list when it lands. Failures are ignored — the
 * cached rows stay, and a missing pair still offers the stored value +
 * Default. Reopens within five minutes share the in-flight / fresh sync.
 */
function useCallingDefaultCatalogs(open: boolean) {
  const queryClient = useQueryClient();
  const [catalogs, setCatalogs] = useState<CallingDefaultsCatalogRow[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const requestedPairs = useRef(new Set<string>());
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    if (!open) return;
    // Scoped to this opening: responses from an earlier opening are dropped,
    // and a list answering after the sync must not overwrite its rows.
    let active = true;
    let synced = false;
    requestedPairs.current.clear();
    void listCallingDefaultCatalogs()
      .then((rows) => {
        if (active && !synced) setCatalogs(rows);
      })
      .catch(() => {
        // Dropdowns fall back to the stored value + Default.
      });
    const lastSync = queryClient.getQueryState(SYNC_ALL_KEY);
    const fresh =
      lastSync?.status === "success" && Date.now() - lastSync.dataUpdatedAt < SYNC_ALL_STALE_MS;
    if (!fresh) {
      setRefreshing(true);
      void queryClient
        .fetchQuery({
          queryKey: SYNC_ALL_KEY,
          queryFn: () => syncCallingDefaults(),
          staleTime: SYNC_ALL_STALE_MS,
        })
        .then((rows) => {
          if (!active) return;
          synced = true;
          // The sync is the snapshot; only pairs a per-host dropdown synced
          // during this opening survive it.
          setCatalogs((previous) =>
            mergeCatalogRows(
              previous.filter((row) =>
                requestedPairs.current.has(pairKey(row.host_id, row.harness)),
              ),
              rows,
            ),
          );
        })
        .catch(() => {
          // A failed sync leaves the cached rows in place.
        })
        .finally(() => {
          if (active) setRefreshing(false);
        });
    }
    return () => {
      active = false;
      setRefreshing(false);
    };
  }, [open, queryClient]);
  const ensureCatalog = (hostId: string, harness: string) => {
    if (catalogFor(catalogs, hostId, harness) !== undefined) return;
    const key = pairKey(hostId, harness);
    if (requestedPairs.current.has(key)) return;
    requestedPairs.current.add(key);
    void syncCallingDefaults({ hostId, harness })
      .then((rows) => {
        if (alive.current) setCatalogs((previous) => mergeCatalogRows(previous, rows));
      })
      .catch(() => {
        // A failed sync leaves the pair absent; the dropdown stays usable.
      });
  };
  return { catalogs, ensureCatalog, refreshing };
}

// Inside the <form>, Radix's hidden native <select> reports a new controlled
// value it has no option for yet as ""; no item has value "", so it's no pick.
function onPick(handler: (value: string) => void): (value: string) => void {
  return (value) => {
    if (value !== "") handler(value);
  };
}

/** A "Default"-clearing model dropdown for one (host, harness) catalog pair. */
function HostModelSelect({
  value,
  models,
  testId,
  disabled,
  onOpen,
  onOpenChange,
  onChange,
}: {
  value: string | null;
  models: readonly NativeModelOption[];
  testId: string;
  disabled?: boolean;
  onOpen?: () => void;
  onOpenChange?: (open: boolean) => void;
  onChange: (model: string | null) => void;
}) {
  const options = models.map((model) => ({ id: model.id, label: nativeModelLabel(model) }));
  if (value && !options.some((option) => option.id === value)) {
    options.unshift({ id: value, label: value });
  }
  return (
    <Select
      value={value ?? MODEL_SELECT_DEFAULT}
      onValueChange={onPick((next) => onChange(next === MODEL_SELECT_DEFAULT ? null : next))}
      onOpenChange={(open) => {
        if (open) onOpen?.();
        onOpenChange?.(open);
      }}
      disabled={disabled}
    >
      <SelectTrigger className="h-8 w-full min-w-0" aria-label="Model" data-testid={testId}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper" align="start" className="w-(--radix-select-trigger-width)">
        <SelectItem value={MODEL_SELECT_DEFAULT}>Default</SelectItem>
        {options.map((option) => (
          <SelectItem key={option.id} value={option.id}>
            <span className="block min-w-0 truncate" title={option.label}>
              {option.label}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** A "Default"-clearing effort dropdown over the harness's effort ladder. */
function HostEffortSelect({
  value,
  levels,
  testId,
  disabled,
  onOpen,
  onOpenChange,
  onChange,
}: {
  value: string | null;
  levels: readonly string[];
  testId: string;
  disabled?: boolean;
  onOpen?: () => void;
  onOpenChange?: (open: boolean) => void;
  onChange: (effort: string | null) => void;
}) {
  return (
    <Select
      value={value ?? EFFORT_SELECT_NONE}
      onValueChange={onPick((next) => onChange(next === EFFORT_SELECT_NONE ? null : next))}
      onOpenChange={(open) => {
        if (open) onOpen?.();
        onOpenChange?.(open);
      }}
      disabled={disabled}
    >
      <SelectTrigger className="h-8 w-full min-w-0" aria-label="Effort" data-testid={testId}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper" align="start" className="w-(--radix-select-trigger-width)">
        <SelectItem value={EFFORT_SELECT_NONE}>Default</SelectItem>
        {levels.map((level) => (
          <SelectItem key={level} value={level}>
            {normalizeEffortLabel(level)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
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
  const isCompact = useIsMobileViewport();
  // A first-class project gets the two-tab layout; a label-only folder is
  // still created by this dialog's Save, so it keeps the single settings form.
  const isRealProject = projectId !== null;
  const labelOnly = projectId === null;
  // Shared with the Code section (same query key): the code repository's
  // default branch feeds the base-branch hint.
  const { data: collaboration } = useQuery({
    queryKey: ["project-collaboration", projectId],
    queryFn: () => getProjectCollaboration(projectId as string),
    enabled: open && projectId !== null,
    retry: false,
  });
  const codeRepository =
    collaboration?.repositories.find((repository) => repository.role === "code") ?? null;
  const baseBranchHint =
    codeRepository && codeRepository.default_branch !== ""
      ? `Blank: the code repository's default branch (${codeRepository.default_branch}), else the current branch.`
      : "Branch new worktrees fork from; blank uses the current branch";
  // Sandbox is only a real default when the server can provision managed
  // sandbox hosts — mirror the composer's gate so we don't offer a target that
  // can only fail on create.
  const managedSandboxesEnabled = info !== "loading" && info.managed_sandboxes_enabled;
  const sandboxProvider = info !== "loading" ? info.sandbox_provider : null;

  // Draft fields. Seeded from the stored config + entries each time the dialog
  // opens (or the fetches arrive); local until saved.
  const [hostId, setHostId] = useState<string>(NONE);
  // One row per host: its directory entry plus its `calling_defaults` draft.
  // The Host field above is the default host; the rows are independent of it.
  const [hostRows, setHostRows] = useState<HostRowDraft[]>([]);
  // Which row's detail is shown: a host id or ALL_HOSTS; null below md when
  // the accordion is collapsed. Below md this is the single open accordion
  // row; at >= md it's the highlighted list row.
  const [selectedRowId, setSelectedRowId] = useState<string | null>(ALL_HOSTS);
  // Which row's filesystem browser is expanded (host id), if any.
  const [openRow, setOpenRow] = useState<string | null>(null);
  // Which row's "Other harness models" list is expanded, if any.
  const [otherHarnessOpenFor, setOtherHarnessOpenFor] = useState<string | null>(null);
  // The first entry write to fail on Save: its server message renders on that
  // row, the sequence stops, and no config is written.
  const [entriesError, setEntriesError] = useState<{ hostId: string; message: string } | null>(
    null,
  );
  // Non-entry save failures (project promotion / config PATCH) — rendered with
  // the same alert as `updateConfig.error`.
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Post-bind outcome per host from the last Save, so the row can show a
  // warning under its path.
  const [entryHookOutcomes, setEntryHookOutcomes] = useState<ReadonlyMap<string, PostBindResult>>(
    () => new Map(),
  );
  // The entry writes already persisted (host id → path), seeded from the
  // fetched rows and advanced as each write succeeds. Save derives its PUT /
  // DELETE plan from this baseline, not from the server rows, so a retry
  // after a partial failure (a DELETE that landed, a config PATCH that did
  // not) sends only what is still pending instead of repeating a DELETE that
  // now 404s.
  const [savedEntries, setSavedEntries] = useState<ReadonlyMap<string, string>>(() => new Map());
  // The dialog open (or project) whose drafts have been seeded, so a later
  // entries refetch (a Code-tab folder change) can't overwrite edited drafts.
  const seededKeyRef = useRef<string | null>(null);
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
  // Legacy "All hosts" defaults (`config.agent_id` / `config.model`), shown in
  // the All hosts row's detail.
  const [agentId, setAgentId] = useState<string | null>(null);
  // Default model for new sessions, only meaningful when the default agent is
  // a native harness with a model choice; NONE stores no default (unset key).
  const [model, setModel] = useState<string>(NONE);
  const [activeTab, setActiveTab] = useState("defaults");
  const tabsId = useId();
  const { catalogs, ensureCatalog, refreshing } = useCallingDefaultCatalogs(open);
  // The single host-row set. A label-only folder has no stored project yet, so
  // there is nothing to fetch until Save promotes it.
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
    (host) => !host.sandbox_provider && !hostRows.some((row) => row.hostId === host.host_id),
  );

  useEffect(() => {
    if (open) {
      setActiveTab("defaults");
      // Outcomes are dialog-session state; a refetch must not wipe a warning
      // the user still needs to see.
      setEntryHookOutcomes(new Map());
    } else {
      // A close re-arms the one-time seed for the next opening.
      seededKeyRef.current = null;
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
    if (openRow !== null) {
      event.preventDefault();
      return;
    }
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
    // Wait for the config and the entry rows, or the config-fallback row would
    // be seeded and then immediately replaced by the fetched rows.
    if (isLoading || entriesLoading) return;
    // Seed once per opening (or project): a Code-tab folder change refetches
    // entries, and re-seeding then would discard unsaved Session-default edits.
    const seedKey = projectId ?? "__label_only__";
    if (seededKeyRef.current === seedKey) return;
    seededKeyRef.current = seedKey;
    const c: ProjectConfig = stored ?? {};
    setHostId(c.host_id ?? NONE);
    const rows = seedHostRows(entries, c);
    setHostRows(rows);
    setSelectedRowId(rows[0]?.hostId ?? ALL_HOSTS);
    setSavedEntries(new Map(entries.map((entry) => [entry.host_id, entry.workspace])));
    setUseWorktree(c.use_worktree ?? readAlwaysUseWorktree());
    setBaseBranch(c.base_branch ?? "");
    setAgentId(c.agent_id ?? null);
    setModel(c.model ?? NONE);
    setOpenRow(null);
    setOtherHarnessOpenFor(null);
    setEntriesError(null);
    setSaveError(null);
  }, [open, projectId, stored, isLoading, loadFailed, entriesLoadFailed, entriesLoading, entries]);

  // Entry sync derived from the persisted baseline + draft rows: PUTs for
  // rows whose path changed (or that are new), DELETEs for persisted rows the
  // user removed. Comparing against `savedEntries` (not the query's rows) is
  // what lets a retry converge after a partial failure.
  const changedRows = hostRows.filter((row) => {
    const path = row.path.trim();
    return path !== "" && savedEntries.get(row.hostId) !== path;
  });
  const removedHostIds = [...savedEntries.keys()].filter(
    (entryHostId) => !hostRows.some((row) => row.hostId === entryHostId),
  );
  // The default host's row supplies the config's `workspace` mirror. A missing
  // row, a blank path, or the sandbox default host leaves it unset.
  const defaultRow =
    hostId !== NONE && hostId !== SANDBOX_HOST_CHOICE
      ? hostRows.find((row) => row.hostId === hostId)
      : undefined;
  const defaultHostRowPath = defaultRow ? trimOrUndef(defaultRow.path) : undefined;

  const addHostRow = (rowHostId: string) => {
    setEntriesError(null);
    setHostRows((rows) =>
      rows.some((row) => row.hostId === rowHostId)
        ? rows
        : [...rows, { hostId: rowHostId, path: "", agentId: null, harnesses: {} }],
    );
    setOtherHarnessOpenFor(null);
    setSelectedRowId(rowHostId);
  };

  const removeHostRow = (rowHostId: string) => {
    const index = hostRows.findIndex((row) => row.hostId === rowHostId);
    const neighbour = hostRows[index + 1] ?? hostRows[index - 1];
    setEntriesError(null);
    setOpenRow((current) => (current === rowHostId ? null : current));
    setOtherHarnessOpenFor((current) => (current === rowHostId ? null : current));
    setHostRows((rows) => rows.filter((row) => row.hostId !== rowHostId));
    setSelectedRowId((current) =>
      current === rowHostId ? (neighbour?.hostId ?? ALL_HOSTS) : current,
    );
  };

  // Below md a row tap toggles the single-open accordion; at >= md it only
  // moves the highlight (the detail pane is always visible).
  const selectRow = (rowId: string) => {
    setOtherHarnessOpenFor(null);
    setSelectedRowId((current) => (isCompact && current === rowId ? null : rowId));
  };

  const setDirectoryPath = (rowHostId: string, path: string) => {
    setEntriesError(null);
    setHostRows((rows) => rows.map((row) => (row.hostId === rowHostId ? { ...row, path } : row)));
  };

  const setRowAgent = (rowHostId: string, nextAgentId: string | null) => {
    setHostRows((rows) =>
      rows.map((row) => (row.hostId === rowHostId ? { ...row, agentId: nextAgentId } : row)),
    );
  };

  const setHarnessField = (
    rowHostId: string,
    harness: string,
    field: "model" | "effort" | "speed" | "permission",
    value: string | null,
  ) => {
    setHostRows((rows) =>
      rows.map((row) => {
        if (row.hostId !== rowHostId) return row;
        const current = row.harnesses[harness] ?? { model: null, effort: null };
        return {
          ...row,
          harnesses: { ...row.harnesses, [harness]: { ...current, [field]: value } },
        };
      }),
    );
  };

  const addOtherHarness = (rowHostId: string, harness: string) => {
    setHarnessField(rowHostId, harness, "model", null);
    ensureCatalog(rowHostId, harness);
  };

  const removeOtherHarness = (rowHostId: string, harness: string) => {
    setHostRows((rows) =>
      rows.map((row) => {
        if (row.hostId !== rowHostId) return row;
        const next = { ...row.harnesses };
        Reflect.deleteProperty(next, harness);
        return { ...row, harnesses: next };
      }),
    );
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
    // The stored workspace is only a single-host mirror for upstream readers.
    // For a real project it follows the default host's saved project folder
    // when one exists; otherwise the stored value is kept, because this form
    // no longer edits folders (the Code tab does). For a label-only folder the
    // default host's row is the draft that Save is about to turn into an entry.
    if (labelOnly) {
      if (defaultHostRowPath) config.workspace = defaultHostRowPath;
      else delete config.workspace;
    } else {
      const defaultEntry = entries.find((entry) => entry.host_id === hostId);
      if (defaultEntry) config.workspace = defaultEntry.workspace;
    }
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
    // Per-host sets are rebuilt from the drafts: a deleted row drops its key,
    // and cleared fields / empty harness groups are omitted entirely (never a
    // "default" / clear word).
    const callingDefaults = buildCallingDefaults(hostRows);
    if (callingDefaults) config.calling_defaults = callingDefaults;
    else delete config.calling_defaults;

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
      // Entry writes belong to the label-only path, where Save is what creates
      // the project. A real project's folders are edited in the Code tab.
      // PUT changed rows, DELETE removed rows, sequentially: the first failure
      // stops the sequence, its server message lands on that row, the dialog
      // stays open and no config is written. Sequential by design — the next
      // write must not start after a failure. Each success advances the
      // baseline so a retry only sends what is still pending.
      if (labelOnly) {
        for (const row of changedRows) {
          const path = row.path.trim();
          let written: ProjectHostEntry & { post_bind?: PostBindResult };
          try {
            // eslint-disable-next-line no-await-in-loop
            written = await putProjectEntry(id, row.hostId, path);
          } catch (error) {
            // Select the row so its detail (where the message renders) is on
            // screen; the user may have been editing another host.
            setSelectedRowId(row.hostId);
            setEntriesError({ hostId: row.hostId, message: errorMessage(error) });
            return;
          }
          entryWriteCommitted = true;
          setSavedEntries((current) => new Map(current).set(row.hostId, path));
          const postBind = written.post_bind;
          if (postBind && WARNING_HOOK_STATUSES.has(postBind.status)) hookWarningSeen = true;
          setEntryHookOutcomes((current) => {
            const next = new Map(current);
            if (postBind) next.set(row.hostId, postBind);
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
        // Partial progress included: the folder readers (Code tab / host
        // roots) must see rows this Save already wrote.
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
  // per-host rows untouched. Close any open browser so the newly selected
  // default host's row starts collapsed.
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
  // Mirror the server's create-time offered check: it reads each placement
  // host's cached catalog, skips errored / empty rows, and matches `id` or `model`.
  const offerRows = useMemo(
    () =>
      catalogs.filter(
        (row) =>
          row.harness === selectedNativeSpec?.harness &&
          row.error === null &&
          row.models.length > 0,
      ),
    [catalogs, selectedNativeSpec],
  );
  const modelOptions = useMemo(() => {
    const [first, ...rest] = offerRows;
    if (!first) return [];
    return first.models
      .filter((option) => rest.every((row) => catalogAccepts(row, option.id)))
      .map((option) => ({ id: option.id, label: nativeModelLabel(option) }));
  }, [offerRows]);
  // Keep a stored model the offer doesn't list as a labeled fallback item, so
  // opening + saving the dialog doesn't silently drop the default.
  const storedModelMissing = model !== NONE && !modelOptions.some((m) => m.id === model);
  // Flag only what the server would refuse; with no qualifying row it checks nothing.
  const storedModelNotOffered =
    storedModelMissing &&
    offerRows.length > 0 &&
    !offerRows.every((row) => catalogAccepts(row, model));
  // With no options and nothing stored, the select could only render "No
  // default" — an action it can't perform. Disable it and say why.
  const modelPickerEmpty = modelOptions.length === 0 && model === NONE;
  const modelHint = refreshing
    ? "Refreshing models…"
    : storedModelNotOffered
      ? "Not offered by every host — pick another model or set it per host below"
      : modelPickerEmpty
        ? offerRows.length === 0
          ? "No model catalog available — connect a host to choose from its models"
          : "No model is offered by every host — set the model per host below"
        : "Default model for new sessions with this agent";
  // Offer the control only for a model-taking harness; when it can only render
  // "No default" it stays visible but disabled with an explanatory hint.
  const supportsModelDefault = harnessTakesModel;
  // The host the agent picker's readiness badges check against (its config
  // hints show whether a harness is set up there). Null when no concrete host.
  const warningHost = onlineHosts.find((h) => h.host_id === browsableHostId) ?? null;

  const allHostsSummary =
    [
      selectedAgent?.display_name ?? agentId,
      model !== NONE ? (modelOptions.find((o) => o.id === model)?.label ?? model) : undefined,
    ]
      .filter(Boolean)
      .join(" · ") || "Not set";

  const renderDirectoryField = (row: HostRowDraft) => {
    const rowHost = hostsById.get(row.hostId);
    const browsable = rowHost?.status === "online";
    const rowOpen = openRow === row.hostId;
    const disabled = isLoading || saving;
    return (
      <div className="flex min-w-0 flex-col gap-1.5">
        {browsable ? (
          // A compact trigger showing the row's current path; clicking opens
          // the shared workspace browser dialog. Navigation there is
          // provisional until the user confirms a folder, which becomes this
          // row's path.
          <>
            <button
              type="button"
              onClick={() => setOpenRow(row.hostId)}
              aria-expanded={rowOpen}
              disabled={disabled}
              data-testid={`project-settings-entry-browse-${row.hostId}`}
              aria-label={`Project directory on ${rowHost?.name ?? row.hostId}`}
              className="flex h-8 w-full items-center justify-between gap-2 rounded-md border border-input bg-transparent px-3 text-ui outline-none disabled:cursor-not-allowed disabled:opacity-50"
            >
              <span
                className={
                  row.path ? "min-w-0 truncate font-mono" : "truncate text-muted-foreground"
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
            <WorkspacePickerDialog
              open={rowOpen}
              onOpenChange={(next) => setOpenRow(next ? row.hostId : null)}
              hostId={row.hostId}
              initialPath={row.path}
              onConfirm={(path) => setDirectoryPath(row.hostId, path)}
            />
          </>
        ) : (
          <input
            data-testid={`project-settings-entry-path-${row.hostId}`}
            aria-label={`Project directory on ${rowHost?.name ?? row.hostId}`}
            className="w-full rounded-md border bg-transparent px-3 py-2 text-ui outline-none disabled:cursor-not-allowed disabled:opacity-50"
            placeholder="/path/to/repo"
            value={row.path}
            title={row.path || undefined}
            onChange={(e) => setDirectoryPath(row.hostId, e.target.value)}
            disabled={disabled}
          />
        )}
      </div>
    );
  };

  const renderEntryOutcome = (row: HostRowDraft) => {
    const rowOutcome = entryHookOutcomes.get(row.hostId);
    const rowOutcomeWarning = rowOutcome ? WARNING_HOOK_STATUSES.has(rowOutcome.status) : false;
    const rowError = entriesError?.hostId === row.hostId ? entriesError : null;
    if (!rowOutcomeWarning && !rowError) return null;
    return (
      <>
        {rowOutcomeWarning && rowOutcome && (
          <div
            className="text-destructive text-ui"
            role="status"
            data-testid={`project-settings-entry-post-bind-${row.hostId}`}
          >
            Directory saved; host setup command {hookStatusLabel(rowOutcome.status)}
            {rowOutcome.error ? `: ${rowOutcome.error}` : ""}
            {typeof rowOutcome.exit_code === "number" ? ` (exit code ${rowOutcome.exit_code})` : ""}
            {rowOutcome.output ? (
              <pre className="mt-1 whitespace-pre-wrap">{rowOutcome.output}</pre>
            ) : null}
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
      </>
    );
  };

  const renderAgentSelect = (row: HostRowDraft) => {
    const rowHost = hostsById.get(row.hostId);
    const candidates = agentList.filter(
      (agent) => agentUsableOnHost(agent, rowHost) || agent.id === row.agentId,
    );
    const sdkAgents = candidates.filter(isSdkAgent);
    const otherAgents = candidates.filter((agent) => !isSdkAgent(agent));
    const storedMissing = row.agentId !== null && !agentList.some((a) => a.id === row.agentId);
    const renderItem = (agent: AvailableAgent) => (
      <SelectItem key={agent.id} value={agent.id}>
        <span className="block min-w-0 truncate" title={agent.display_name}>
          {agent.display_name}
        </span>
      </SelectItem>
    );
    return (
      <Select
        value={row.agentId ?? NONE}
        onValueChange={onPick((value) => setRowAgent(row.hostId, value === NONE ? null : value))}
        onOpenChange={onDropdownOpenChange}
        disabled={isLoading || saving}
      >
        <SelectTrigger
          className="w-full min-w-0"
          data-testid={`project-settings-host-agent-${row.hostId}`}
        >
          <SelectValue placeholder="Default (none)" />
        </SelectTrigger>
        <SelectContent position="popper" align="start" className="w-(--radix-select-trigger-width)">
          <SelectItem value={NONE}>Default (none)</SelectItem>
          {sdkAgents.length > 0 && (
            <SelectGroup>
              <SelectLabel>SDK</SelectLabel>
              {sdkAgents.map(renderItem)}
            </SelectGroup>
          )}
          {otherAgents.length > 0 && (
            <SelectGroup>
              <SelectLabel>Agents</SelectLabel>
              {otherAgents.map(renderItem)}
            </SelectGroup>
          )}
          {storedMissing && row.agentId !== null && (
            <SelectItem value={row.agentId}>{row.agentId}</SelectItem>
          )}
        </SelectContent>
      </Select>
    );
  };

  const renderHarnessSetFields = (row: HostRowDraft, harness: string) => {
    const set = row.harnesses[harness] ?? { model: null, effort: null };
    const models = catalogFor(catalogs, row.hostId, harness)?.models ?? [];
    const levels = effortLevelsForSet(harness, models, set);
    return (
      <>
        <Field label="Model" hint="Default model for new sessions on this host">
          <HostModelSelect
            value={set.model}
            models={models}
            testId={`project-settings-host-model-${row.hostId}`}
            disabled={isLoading || saving}
            onOpen={() => ensureCatalog(row.hostId, harness)}
            onOpenChange={onDropdownOpenChange}
            onChange={(next) => setHarnessField(row.hostId, harness, "model", next)}
          />
        </Field>
        <Field label="Effort" hint="Default thinking effort for new sessions on this host">
          <HostEffortSelect
            value={set.effort}
            levels={levels}
            testId={`project-settings-host-effort-${row.hostId}`}
            disabled={isLoading || saving}
            onOpen={() => ensureCatalog(row.hostId, harness)}
            onOpenChange={onDropdownOpenChange}
            onChange={(next) => setHarnessField(row.hostId, harness, "effort", next)}
          />
        </Field>
        {(["speed", "permission"] as const).map((field) =>
          sessionDefaultModeOptions(harness, field).length > 0 ? (
            <Field key={field} label={field === "speed" ? "Speed" : "Permission"}>
              <SessionDefaultModeSelect
                harness={harness}
                field={field}
                value={set[field]}
                label={field === "speed" ? "Speed" : "Permission"}
                testId={`project-settings-host-${field}-${row.hostId}`}
                disabled={isLoading || saving}
                onOpenChange={onDropdownOpenChange}
                onChange={(next) => setHarnessField(row.hostId, harness, field, next)}
              />
            </Field>
          ) : null,
        )}
      </>
    );
  };

  const renderOtherHarnessSection = (row: HostRowDraft, selectedHarness: string | null) => {
    const otherRows = Object.entries(row.harnesses).filter(
      ([harness]) => harness !== selectedHarness,
    );
    const candidates = CALLING_DEFAULT_HARNESSES.filter(
      (harness) => harness !== selectedHarness && !(harness in row.harnesses),
    );
    if (otherRows.length === 0 && candidates.length === 0) return null;
    const expanded = otherHarnessOpenFor === row.hostId;
    const disabled = isLoading || saving;
    return (
      <div className="flex min-w-0 flex-col gap-1.5">
        <button
          type="button"
          className="flex min-w-0 items-center gap-1 self-start text-sm text-muted-foreground hover:text-foreground"
          aria-expanded={expanded}
          onClick={() => setOtherHarnessOpenFor(expanded ? null : row.hostId)}
          data-testid={`project-settings-host-other-toggle-${row.hostId}`}
        >
          <ChevronDownIcon
            className={cn("size-4 shrink-0 transition-transform", expanded && "rotate-180")}
          />
          <span className="min-w-0 truncate">
            Other harness models{otherRows.length > 0 ? ` (${otherRows.length})` : ""}
          </span>
        </button>
        {expanded && (
          <div className="flex min-w-0 flex-col gap-2">
            {otherRows.map(([harness, set]) => {
              const models = catalogFor(catalogs, row.hostId, harness)?.models ?? [];
              const levels = effortLevelsForSet(harness, models, set);
              return (
                <div
                  key={harness}
                  className="flex min-w-0 flex-col gap-1.5 rounded-md border p-2"
                  data-testid={`project-settings-host-other-${row.hostId}-${harness}`}
                >
                  <div className="flex min-w-0 items-center justify-between gap-2">
                    <span
                      className="min-w-0 truncate text-ui font-medium"
                      title={callingHarnessLabel(harness)}
                    >
                      {callingHarnessLabel(harness)}
                    </span>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-xs"
                      className="shrink-0 text-muted-foreground hover:text-destructive"
                      aria-label={`Delete ${callingHarnessLabel(harness)} setting`}
                      onClick={() => removeOtherHarness(row.hostId, harness)}
                      disabled={disabled}
                      data-testid={`project-settings-host-other-remove-${row.hostId}-${harness}`}
                    >
                      <Trash2Icon />
                    </Button>
                  </div>
                  <div className="grid min-w-0 grid-cols-1 gap-1.5 sm:grid-cols-2">
                    <HostModelSelect
                      value={set.model}
                      models={models}
                      testId={`project-settings-host-other-model-${row.hostId}-${harness}`}
                      disabled={disabled}
                      onOpen={() => ensureCatalog(row.hostId, harness)}
                      onOpenChange={onDropdownOpenChange}
                      onChange={(next) => setHarnessField(row.hostId, harness, "model", next)}
                    />
                    <HostEffortSelect
                      value={set.effort}
                      levels={levels}
                      testId={`project-settings-host-other-effort-${row.hostId}-${harness}`}
                      disabled={disabled}
                      onOpen={() => ensureCatalog(row.hostId, harness)}
                      onOpenChange={onDropdownOpenChange}
                      onChange={(next) => setHarnessField(row.hostId, harness, "effort", next)}
                    />
                    {(["speed", "permission"] as const).map((field) => (
                      <SessionDefaultModeSelect
                        key={field}
                        harness={harness}
                        field={field}
                        value={set[field]}
                        label={field === "speed" ? "Speed" : "Permission"}
                        testId={`project-settings-host-other-${field}-${row.hostId}-${harness}`}
                        disabled={disabled}
                        onOpenChange={onDropdownOpenChange}
                        onChange={(next) => setHarnessField(row.hostId, harness, field, next)}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
            {candidates.length > 0 && (
              <Select
                value={NONE}
                onValueChange={(harness) => {
                  if (harness !== NONE) addOtherHarness(row.hostId, harness);
                }}
                onOpenChange={onDropdownOpenChange}
                disabled={disabled}
              >
                <SelectTrigger
                  className="w-full min-w-0"
                  data-testid={`project-settings-host-other-add-${row.hostId}`}
                >
                  <SelectValue placeholder="Add harness row" />
                </SelectTrigger>
                <SelectContent
                  position="popper"
                  align="start"
                  className="w-(--radix-select-trigger-width)"
                >
                  <SelectItem value={NONE}>Add harness row</SelectItem>
                  {candidates.map((harness) => (
                    <SelectItem key={harness} value={harness}>
                      {callingHarnessLabel(harness)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
        )}
      </div>
    );
  };

  const renderHostDetail = (row: HostRowDraft) => {
    const agent = row.agentId ? (agentList.find((a) => a.id === row.agentId) ?? null) : null;
    const harness = selectedHarnessFor(agent);
    const joint = agent !== null && isJointAgent(agent);
    return (
      <div
        className="flex min-w-0 flex-col gap-3 rounded-md border p-3"
        data-testid={`project-settings-host-detail-${row.hostId}`}
      >
        {labelOnly && (
          <Field label="Directory" hint="Where new sessions open on this host">
            {renderDirectoryField(row)}
          </Field>
        )}
        <Field label="Agent" hint="Default agent / harness for new sessions on this host">
          {renderAgentSelect(row)}
        </Field>
        {joint ? (
          <Field label="Model & effort" hint="Joint agents take their members' settings">
            <span
              className="text-ui text-muted-foreground"
              data-testid={`project-settings-host-members-${row.hostId}`}
            >
              Set by members
            </span>
          </Field>
        ) : harness ? (
          renderHarnessSetFields(row, harness)
        ) : null}
        {renderOtherHarnessSection(row, harness)}
        {renderEntryOutcome(row)}
      </div>
    );
  };

  const renderHostRow = (row: HostRowDraft) => {
    const rowHost = hostsById.get(row.hostId);
    const name = rowHost?.name ?? row.hostId;
    const summary = hostRowSummary(row, agentList, catalogs);
    const selected = selectedRowId === row.hostId;
    return (
      <div
        role="button"
        tabIndex={0}
        data-testid={`project-settings-entry-${row.hostId}`}
        className={cn(
          "flex min-w-0 cursor-pointer flex-col gap-1 rounded-md border p-2 text-left",
          selected && "border-primary/60 bg-muted/40",
        )}
        onClick={() => selectRow(row.hostId)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectRow(row.hostId);
          }
        }}
      >
        <div className="flex min-w-0 items-center gap-1">
          <span className="min-w-0 flex-1 truncate text-ui font-medium" title={name}>
            {name}
          </span>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="shrink-0 text-muted-foreground"
            aria-label={`Edit ${name}`}
            title="Edit"
            onClick={(event) => {
              event.stopPropagation();
              selectRow(row.hostId);
            }}
            data-testid={`project-settings-entry-edit-${row.hostId}`}
          >
            <PencilIcon />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="shrink-0 text-muted-foreground hover:text-destructive"
            aria-label={`Delete ${name}`}
            title="Delete"
            onClick={(event) => {
              event.stopPropagation();
              removeHostRow(row.hostId);
            }}
            disabled={isLoading || saving}
            data-testid={`project-settings-entry-remove-${row.hostId}`}
          >
            <Trash2Icon />
          </Button>
        </div>
        {(row.path || !isRealProject) && (
          <span
            className={cn(
              "min-w-0 truncate text-xs",
              row.path ? "font-mono" : "text-muted-foreground",
            )}
            title={row.path || undefined}
            data-testid={`project-settings-host-path-${row.hostId}`}
          >
            {row.path || "No directory"}
          </span>
        )}
        <span
          className="min-w-0 max-w-full truncate self-start rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
          title={summary}
          data-testid={`project-settings-host-summary-${row.hostId}`}
        >
          {summary}
        </span>
      </div>
    );
  };

  const renderAllHostsRow = () => {
    const selected = selectedRowId === ALL_HOSTS;
    return (
      <div
        role="button"
        tabIndex={0}
        data-testid="project-settings-all-hosts"
        className={cn(
          "flex min-w-0 cursor-pointer flex-col gap-1 rounded-md border p-2 text-left",
          selected && "border-primary/60 bg-muted/40",
        )}
        onClick={() => selectRow(ALL_HOSTS)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            selectRow(ALL_HOSTS);
          }
        }}
      >
        <div className="flex min-w-0 items-center gap-1">
          <span className="min-w-0 flex-1 truncate text-ui font-medium">All hosts</span>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            className="shrink-0 text-muted-foreground"
            aria-label="Edit the all-hosts defaults"
            title="Edit"
            onClick={(event) => {
              event.stopPropagation();
              selectRow(ALL_HOSTS);
            }}
            data-testid="project-settings-all-hosts-edit"
          >
            <PencilIcon />
          </Button>
        </div>
        <span
          className="min-w-0 max-w-full truncate self-start rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
          title={allHostsSummary}
          data-testid="project-settings-all-hosts-summary"
        >
          {allHostsSummary}
        </span>
      </div>
    );
  };

  const renderAllHostsDetail = () => (
    <div
      className="flex min-w-0 flex-col gap-3 rounded-md border p-3"
      data-testid="project-settings-all-hosts-detail"
    >
      <p className="text-sm text-muted-foreground">Used for hosts without their own defaults.</p>
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
        <Field label="Model" hint={modelHint}>
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
              {storedModelMissing && (
                <SelectItem value={model}>
                  {storedModelNotOffered ? `${model} (not offered by every host)` : model}
                </SelectItem>
              )}
            </SelectContent>
          </Select>
        </Field>
      )}
    </div>
  );

  const selectedRow = hostRows.find((row) => row.hostId === selectedRowId) ?? null;

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
            Defaults and code locations for <b>{projectName}</b>.
          </DialogDescription>
          {isRealProject && (
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
                  value="code"
                  id={`${tabsId}-code-tab`}
                  aria-controls={`${tabsId}-code-panel`}
                  className="flex-none px-2 pb-3"
                >
                  Code
                </TabsTrigger>
              </TabsList>
            </Tabs>
          )}
        </DialogHeader>
        <form
          id="project-settings-defaults-form"
          role={isRealProject ? "tabpanel" : undefined}
          aria-labelledby={isRealProject ? `${tabsId}-defaults-tab` : undefined}
          tabIndex={isRealProject ? 0 : undefined}
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

          {/* One row per host: its project directory plus its per-host session
              defaults. At >= md the selected row's detail shows in the right
              pane; below md each row expands inline (single-open). The "All
              hosts" row carries the legacy config.agent_id / config.model. */}
          <div className="grid min-w-0 grid-cols-1 gap-2">
            <div className="flex min-w-0 flex-col">
              <span className="font-medium text-ui">Hosts</span>
              <span className="text-muted-foreground text-sm">
                {isRealProject
                  ? "Agent, model and effort to use on each host."
                  : "Per-host directories and session defaults"}
              </span>
            </div>
            <div className="grid min-w-0 grid-cols-1 gap-3 md:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]">
              <div
                className="flex min-w-0 flex-col gap-2"
                data-testid="project-settings-directories"
              >
                {hostRows.length === 0 && (
                  <p
                    className="rounded-md border border-dashed px-3 py-2 text-muted-foreground text-ui"
                    data-testid="project-settings-directories-empty"
                  >
                    {isRealProject ? (
                      <>
                        No host defaults yet. Folders are set in the{" "}
                        <button
                          type="button"
                          className="underline"
                          data-testid="project-settings-open-code-tab"
                          onClick={() => setActiveTab("code")}
                        >
                          Code tab
                        </button>
                        .
                      </>
                    ) : (
                      "No project directory yet"
                    )}
                  </p>
                )}
                {hostRows.map((row) =>
                  isCompact ? (
                    <div key={row.hostId} className="flex min-w-0 flex-col gap-2">
                      {renderHostRow(row)}
                      {selectedRowId === row.hostId && renderHostDetail(row)}
                    </div>
                  ) : (
                    <Fragment key={row.hostId}>{renderHostRow(row)}</Fragment>
                  ),
                )}
                {renderAllHostsRow()}
                {isCompact && selectedRowId === ALL_HOSTS && renderAllHostsDetail()}
                {addableHosts.length > 0 && (
                  // A menu-shaped Select: its value never changes, so picking a
                  // host adds a defaults row and the trigger keeps its label.
                  <Select
                    value={NONE}
                    onValueChange={(value) => {
                      if (value !== NONE) addHostRow(value);
                    }}
                    onOpenChange={onDropdownOpenChange}
                    disabled={isLoading || saving}
                  >
                    <SelectTrigger className="w-full" data-testid="project-settings-add-host">
                      <SelectValue placeholder="Add host defaults" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={NONE}>Add host defaults</SelectItem>
                      {addableHosts.map((host) => (
                        <SelectItem key={host.host_id} value={host.host_id}>
                          {host.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
                {entriesError && !hostRows.some((row) => row.hostId === entriesError.hostId) && (
                  // A DELETE that failed has no row left to carry the message.
                  <p
                    className="text-destructive text-ui"
                    role="alert"
                    data-testid="project-settings-entries-error"
                  >
                    {entriesError.message}
                  </p>
                )}
              </div>
              {!isCompact && (
                <div className="min-w-0">
                  {selectedRow ? (
                    renderHostDetail(selectedRow)
                  ) : selectedRowId === ALL_HOSTS ? (
                    renderAllHostsDetail()
                  ) : (
                    <p className="text-muted-foreground text-ui">
                      Select a host to edit its defaults.
                    </p>
                  )}
                </div>
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
            <Field label="Base branch" hint={baseBranchHint} htmlFor="project-settings-base-branch">
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
        {projectId !== null && (
          <div
            id={`${tabsId}-code-panel`}
            role="tabpanel"
            aria-labelledby={`${tabsId}-code-tab`}
            tabIndex={0}
            hidden={activeTab !== "code"}
            className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden px-5 py-5 sm:px-6"
          >
            <ProjectCodeSection projectId={projectId} />
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
