// Code settings for a first-class project: the repositories, each host's
// project folder and repository folders with live git facts, and a preview of
// what agents are told. Rendered OUTSIDE the settings dialog's defaults <form>
// (nested forms are invalid HTML), and every action here applies immediately
// instead of going through Save.
import { useEffect, useId, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { WorkspacePickerDialog } from "./WorkspacePickerDialog";
import { useHosts } from "@/hooks/useHosts";
import { useProjectHostRoots } from "@/hooks/useConversations";
import {
  deleteProjectEntry,
  deleteProjectHostBinding,
  deleteProjectRepository,
  getAgentCodeNote,
  getHostFolderFacts,
  getProjectCollaboration,
  listProjectEntries,
  putProjectEntry,
  putProjectHostBinding,
  putProjectRepository,
  verifyProjectHostBinding,
  type HostFolderFacts,
  type ProjectRepository,
  type PutProjectRepositoryBody,
  type SetupOutcome,
} from "@/lib/projectsApi";

const inputClassName =
  "w-full rounded-md border bg-transparent px-3 py-2 text-ui outline-none disabled:cursor-not-allowed disabled:opacity-50";

const mutedButtonClassName =
  "h-auto self-start p-0 text-muted-foreground text-sm hover:bg-transparent";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Try again.";
}

function hookStatusLabel(status: string): string {
  return status === "timed_out" ? "timed out" : status;
}

function sameFolder(path: string, other: string | null | undefined): boolean {
  if (!other) return false;
  return path.replace(/[/\\]+$/, "") === other.replace(/[/\\]+$/, "");
}

/** Map a name candidate onto the server's ref-segment rule. */
function sanitiseRepositoryName(value: string): string {
  return value
    .replace(/[^A-Za-z0-9._-]/g, "-")
    .replace(/^\.+/, "")
    .slice(0, 100);
}

function repositoryNameFromUrl(remoteUrl: string, folder: string): string {
  const lastSegment = remoteUrl.trim().replace(/\/+$/, "").split(/[/:]/).pop() ?? "";
  const folderBase =
    folder
      .replace(/[/\\]+$/, "")
      .split(/[/\\]/)
      .pop() ?? "";
  const source = lastSegment.replace(/\.git$/i, "") || folderBase;
  return sanitiseRepositoryName(source) || "repository";
}

function defaultRemote(remotes: { name: string; url: string }[]) {
  return remotes.find((remote) => remote.name === "origin") ?? remotes[0] ?? null;
}

/** What the one folder picker is choosing a path for. */
type FolderPickerTarget =
  | { kind: "entry"; hostId: string }
  | { kind: "binding"; hostId: string; repositoryId: string; bindingName: string }
  | { kind: "add"; hostId: string };

/** Render an outcome timestamp as its UTC wall time, e.g. `2026-10-09 12:34 UTC`. */
function formatOutcomeTime(at: string): string {
  const time = new Date(at).getTime();
  if (Number.isNaN(time)) return at;
  return `${new Date(time).toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

function folderFactsText(facts: HostFolderFacts): string {
  if (!facts.exists) return "folder missing";
  if (!facts.is_dir) return facts.error ?? "not a folder";
  if (!facts.is_repo) return "not a git repository";
  const parts: string[] = [];
  if (facts.detached) parts.push("detached HEAD");
  else if (facts.branch) parts.push(facts.branch);
  if (facts.head) parts.push(`HEAD ${facts.head.slice(0, 7)}`);
  if (facts.dirty === true) parts.push("uncommitted changes");
  return parts.length > 0 ? parts.join(" · ") : "no commits yet";
}

interface FolderFactsLineProps {
  projectId: string;
  hostId: string;
  path: string;
  testId: string;
  /** The repository this folder belongs to, when it is a repository folder. */
  repository?: ProjectRepository;
  outcome?: SetupOutcome;
  onRunAgain?: () => void;
  onUseRemote?: (url: string) => void;
  disabled?: boolean;
}

function FolderFactsLine({
  projectId,
  hostId,
  path,
  testId,
  repository,
  outcome,
  onRunAgain,
  onUseRemote,
  disabled,
}: FolderFactsLineProps) {
  const factsQuery = useQuery({
    queryKey: ["project-code-folder-facts", projectId, hostId, path],
    queryFn: () => getHostFolderFacts(hostId, path),
    retry: false,
  });

  if (factsQuery.isPending) {
    return (
      <p className="text-sm text-muted-foreground" data-testid={testId}>
        Reading folder…
      </p>
    );
  }
  if (factsQuery.isError) {
    return (
      <p className="text-sm text-muted-foreground" data-testid={testId}>
        Couldn&apos;t read this folder.
      </p>
    );
  }
  const result = factsQuery.data;
  if (result.state === "offline") {
    return (
      <p className="text-sm text-muted-foreground" data-testid={testId}>
        host offline
      </p>
    );
  }
  if (result.state === "unsupported") {
    return (
      <p className="text-sm text-muted-foreground" data-testid={testId}>
        host needs an update
      </p>
    );
  }
  if (result.state === "error") {
    return (
      <p className="text-sm text-muted-foreground" data-testid={testId}>
        {result.message}
      </p>
    );
  }

  const facts = result.facts;
  const remote = defaultRemote(facts.remotes);
  const remoteMismatch =
    repository !== undefined &&
    repository.remote_url !== "" &&
    facts.remotes.length > 0 &&
    !facts.remotes.some((candidate) => candidate.url === repository.remote_url);

  return (
    <div className="space-y-1">
      <p className="text-sm text-muted-foreground" data-testid={testId}>
        {folderFactsText(facts)}
      </p>
      {remoteMismatch && (
        <p
          className="text-sm text-amber-700 dark:text-amber-400"
          data-testid={`${testId}-remote-mismatch`}
        >
          None of this folder&apos;s remote URLs matches the repository URL.
        </p>
      )}
      {onUseRemote && remote && (
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className={mutedButtonClassName}
          data-testid={`${testId}-use-remote`}
          onClick={() => onUseRemote(remote.url)}
          disabled={disabled}
        >
          Use this folder&apos;s remote
        </Button>
      )}
      {facts.setup_command_configured && onRunAgain && (
        <div className="space-y-1" data-testid={`${testId}-setup`}>
          <p className="text-sm font-medium">Host setup command</p>
          <p className="text-sm text-muted-foreground" data-testid={`${testId}-setup-outcome`}>
            {outcome ? (
              <>
                Last run: {hookStatusLabel(outcome.status)}
                {typeof outcome.exit_code === "number" ? ` (exit code ${outcome.exit_code})` : ""}
                {` · ${formatOutcomeTime(outcome.at)}`}
              </>
            ) : (
              "No run yet since the server started."
            )}
          </p>
          <p className="text-sm text-muted-foreground">Recorded since the server started.</p>
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid={`${testId}-run-again`}
            onClick={onRunAgain}
            disabled={disabled}
          >
            Run again
          </Button>
        </div>
      )}
    </div>
  );
}

function AgentCodeNotePanel({ projectId, hostId }: { projectId: string; hostId: string }) {
  const note = useQuery({
    queryKey: ["project-code-agent-note", projectId, hostId],
    queryFn: () => getAgentCodeNote(projectId, hostId),
    retry: false,
  });

  return (
    <details className="text-sm" data-testid={`project-code-agent-note-${hostId}`}>
      <summary className="cursor-pointer font-medium">What the agent is told</summary>
      <div className="mt-2 space-y-1">
        {note.isPending && <p className="text-muted-foreground">Loading…</p>}
        {note.isError && <p className="text-muted-foreground">Couldn&apos;t load the preview.</p>}
        {note.data && (
          <>
            {note.data.text ? (
              <pre className="whitespace-pre-wrap font-mono text-sm">{note.data.text}</pre>
            ) : (
              <p className="text-muted-foreground">No code locations on this host yet.</p>
            )}
            {!note.data.delivered && note.data.reason === "host_update_needed" && (
              <p
                className="text-amber-700 dark:text-amber-400"
                data-testid={`project-code-agent-note-update-${hostId}`}
              >
                This host needs an update before agents receive this
              </p>
            )}
            {!note.data.delivered && note.data.reason === "host_offline" && (
              <p
                className="text-amber-700 dark:text-amber-400"
                data-testid={`project-code-agent-note-offline-${hostId}`}
              >
                Host offline — shown from saved settings
              </p>
            )}
            <p className="text-muted-foreground">OpenCode sessions do not receive this yet</p>
          </>
        )}
      </div>
    </details>
  );
}

export function ProjectCodeSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const collaboration = useQuery({
    queryKey: ["project-collaboration", projectId],
    queryFn: () => getProjectCollaboration(projectId),
    retry: false,
  });
  const entriesQuery = useQuery({
    queryKey: ["project-entries", projectId],
    queryFn: () => listProjectEntries(projectId),
    retry: false,
  });
  const hostRoots = useProjectHostRoots(projectId);
  const hosts = useHosts();
  const formId = useId();

  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [uncheckedKeys, setUncheckedKeys] = useState<ReadonlySet<string>>(new Set());
  const [picker, setPicker] = useState<FolderPickerTarget | null>(null);
  const [extraHostIds, setExtraHostIds] = useState<string[]>([]);
  const [branchDrafts, setBranchDrafts] = useState<Record<string, string>>({});
  const [folderDrafts, setFolderDrafts] = useState<Record<string, string>>({});

  const [addOpen, setAddOpen] = useState(false);
  const [addMode, setAddMode] = useState<"folder" | "address">("folder");
  const [addHostId, setAddHostId] = useState("");
  const [addFolder, setAddFolder] = useState("");
  const [addFactsPath, setAddFactsPath] = useState<string | null>(null);
  const [addRemoteName, setAddRemoteName] = useState("");
  const [addName, setAddName] = useState("");
  const [addNameEdited, setAddNameEdited] = useState(false);
  const [addBranch, setAddBranch] = useState("");
  const [addUrl, setAddUrl] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  // The (host, path) selection the defaults were filled for, so refetches and
  // name edits for the same folder keep whatever the user changed.
  const filledAddFactsKey = useRef<string | null>(null);

  // Sandbox hosts are server-provisioned launch targets, never folder hosts.
  const eligibleHosts = (hosts.data ?? []).filter((host) => !host.sandbox_provider);
  const effectiveAddHostId = addHostId || eligibleHosts[0]?.host_id || "";
  const addFactsQuery = useQuery({
    queryKey: ["project-code-folder-facts", projectId, effectiveAddHostId, addFactsPath],
    queryFn: () => getHostFolderFacts(effectiveAddHostId, addFactsPath ?? ""),
    enabled: addOpen && addMode === "folder" && addFactsPath !== null && effectiveAddHostId !== "",
    retry: false,
  });

  // Fill the add form from the committed folder's facts, once per (host, path)
  // selection. The key carries the path, so a response for an older path cannot
  // land here; later edits and refetches for the same key leave the form alone.
  useEffect(() => {
    const result = addFactsQuery.data;
    if (!result || result.state !== "ok" || addFactsPath === null) return;
    const key = `${effectiveAddHostId}::${addFactsPath}`;
    if (filledAddFactsKey.current === key) return;
    filledAddFactsKey.current = key;
    const remote = defaultRemote(result.facts.remotes);
    setAddRemoteName(remote?.name ?? "");
    setAddBranch(result.facts.branch ?? "main");
    setAddName(repositoryNameFromUrl(remote?.url ?? "", addFactsPath));
  }, [addFactsQuery.data, addFactsPath, effectiveAddHostId]);

  const invalidate = (hostId?: string) => {
    void queryClient.invalidateQueries({ queryKey: ["project-collaboration", projectId] });
    void queryClient.invalidateQueries({ queryKey: ["project-host-roots", projectId] });
    void queryClient.invalidateQueries({ queryKey: ["project-entries", projectId] });
    if (hostId) {
      void queryClient.invalidateQueries({
        queryKey: ["project-code-agent-note", projectId, hostId],
      });
      void queryClient.invalidateQueries({
        queryKey: ["project-code-folder-facts", projectId, hostId],
      });
    } else {
      void queryClient.invalidateQueries({ queryKey: ["project-code-agent-note", projectId] });
      void queryClient.invalidateQueries({ queryKey: ["project-code-folder-facts", projectId] });
    }
  };

  const runAction = async (action: () => Promise<void>) => {
    setActionError(null);
    setPending(true);
    try {
      await action();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setPending(false);
    }
  };

  const markUnchecked = (key: string, unchecked: boolean) => {
    setUncheckedKeys((current) => {
      const next = new Set(current);
      if (unchecked) next.add(key);
      else next.delete(key);
      return next;
    });
  };

  const putRepository = (
    repository: ProjectRepository,
    changes: Partial<PutProjectRepositoryBody>,
  ) =>
    putProjectRepository(projectId, repository.name, {
      remote_url: repository.remote_url,
      default_branch: repository.default_branch,
      ...(repository.context_manifest_path
        ? { context_manifest_path: repository.context_manifest_path }
        : {}),
      ...changes,
    });

  const saveEntry = (hostId: string, workspace: string) => {
    void runAction(async () => {
      const entry = await putProjectEntry(projectId, hostId, workspace);
      markUnchecked(`${hostId}::project`, entry.checked === false);
      invalidate(hostId);
    });
  };

  const removeEntry = (hostId: string) => {
    void runAction(async () => {
      await deleteProjectEntry(projectId, hostId);
      invalidate(hostId);
    });
  };

  const saveBinding = (
    hostId: string,
    repository: ProjectRepository,
    workspace: string,
    name: string,
  ) => {
    void runAction(async () => {
      const binding = await putProjectHostBinding(projectId, hostId, name, {
        workspace,
        repository_name: repository.name,
      });
      markUnchecked(`${hostId}::${repository.name}`, binding.checked === false);
      invalidate(hostId);
    });
  };

  const removeBinding = (hostId: string, bindingName: string) => {
    void runAction(async () => {
      await deleteProjectHostBinding(projectId, hostId, bindingName);
      invalidate(hostId);
    });
  };

  const runEntrySetup = (hostId: string, workspace: string) => {
    void runAction(async () => {
      await putProjectEntry(projectId, hostId, workspace);
      invalidate(hostId);
    });
  };

  const runBindingSetup = (hostId: string, bindingName: string) => {
    void runAction(async () => {
      await verifyProjectHostBinding(projectId, hostId, bindingName);
      invalidate(hostId);
    });
  };

  const changeRole = (repository: ProjectRepository, role: "code" | "related") => {
    if (repository.role === role) return;
    void runAction(async () => {
      await putRepository(repository, { role });
      invalidate();
    });
  };

  const commitBranch = (repository: ProjectRepository) => {
    const draft = (branchDrafts[repository.name] ?? repository.default_branch).trim();
    if (!draft || draft === repository.default_branch) return;
    void runAction(async () => {
      await putRepository(repository, { default_branch: draft });
      setBranchDrafts((current) =>
        Object.fromEntries(Object.entries(current).filter(([name]) => name !== repository.name)),
      );
      invalidate();
    });
  };

  const removeRepository = (repository: ProjectRepository) => {
    void runAction(async () => {
      await deleteProjectRepository(projectId, repository.name);
      invalidate();
    });
  };

  const applyFolderRemote = (repository: ProjectRepository, url: string) => {
    void runAction(async () => {
      await putRepository(repository, { remote_url: url });
      invalidate();
    });
  };

  const openPicker = (target: FolderPickerTarget) => setPicker(target);

  const commitAddFolder = () => {
    const path = addFolder.trim();
    if (!path || !effectiveAddHostId || path === addFactsPath) return;
    setAddFactsPath(path);
  };

  const resetAddForm = () => {
    setAddOpen(false);
    setAddMode("folder");
    setAddFolder("");
    setAddFactsPath(null);
    setAddRemoteName("");
    setAddName("");
    setAddNameEdited(false);
    setAddBranch("");
    setAddUrl("");
    setAddError(null);
    filledAddFactsKey.current = null;
  };

  if (collaboration.isPending || entriesQuery.isPending) {
    return <p className="text-muted-foreground text-sm">Loading code settings…</p>;
  }

  if (collaboration.isError || entriesQuery.isError) {
    return (
      <p className="text-destructive text-ui" role="alert" data-testid="project-code-error">
        Couldn&apos;t load code settings. Close and reopen to try again.
      </p>
    );
  }

  const repositories = collaboration.data.repositories;
  const bindings = collaboration.data.bindings;
  const problems = collaboration.data.problems;
  const outcomes = collaboration.data.setup_outcomes ?? [];
  const entries = entriesQuery.data ?? [];
  const hasCodeRepository = repositories.some((repository) => repository.role === "code");
  const entryByHost = new Map(entries.map((entry) => [entry.host_id, entry] as const));
  const hostName = (hostId: string) =>
    (hosts.data ?? []).find((host) => host.host_id === hostId)?.name ?? hostId;
  const cardHostIds = [
    ...new Set([
      ...entries.map((entry) => entry.host_id),
      ...bindings.map((binding) => binding.host_id),
      ...(hostRoots.data?.roots ?? []).map((root) => root.host_id),
      ...extraHostIds,
    ]),
  ];
  const addableHosts = eligibleHosts.filter((host) => !cardHostIds.includes(host.host_id));
  const selectedAddHost = eligibleHosts.find((host) => host.host_id === effectiveAddHostId);
  // Facts are only shown for the committed path; typing clears them until commit.
  const addFacts = addFactsPath !== null ? addFactsQuery.data : undefined;
  const addRemote =
    addFacts?.state === "ok"
      ? (addFacts.facts.remotes.find((remote) => remote.name === addRemoteName) ??
        defaultRemote(addFacts.facts.remotes))
      : null;
  const canSubmitAdd =
    addMode === "address"
      ? addName.trim().length > 0
      : addFacts?.state === "ok" && addFolder.trim().length > 0 && !!effectiveAddHostId;

  const submitAdd = () => {
    const name =
      addMode === "address"
        ? addName.trim()
        : addName.trim() || repositoryNameFromUrl(addRemote?.url ?? "", addFolder.trim());
    if (repositories.some((repository) => repository.name === name)) {
      setAddError(`A repository named ${name} already exists`);
      return;
    }
    setAddError(null);
    void runAction(async () => {
      const role = hasCodeRepository ? "related" : "code";
      if (addMode === "address") {
        await putProjectRepository(projectId, name, {
          remote_url: addUrl.trim(),
          default_branch: addBranch.trim() || "main",
          role,
        });
        resetAddForm();
        invalidate();
        return;
      }
      if (addFacts?.state !== "ok" || !addFolder.trim() || !effectiveAddHostId) return;
      await putProjectRepository(projectId, name, {
        remote_url: addRemote?.url ?? "",
        default_branch: addBranch.trim() || "main",
        role,
      });
      try {
        await putProjectHostBinding(projectId, effectiveAddHostId, name, {
          workspace: addFolder.trim(),
          repository_name: name,
        });
      } catch (error) {
        // The repository is committed already; refresh despite the failure.
        invalidate(effectiveAddHostId);
        throw error;
      }
      resetAddForm();
      invalidate(effectiveAddHostId);
    });
  };

  const confirmPicker = (path: string) => {
    const target = picker;
    setPicker(null);
    if (!target) return;
    if (target.kind === "add") {
      setAddFolder(path);
      setAddFactsPath(path.trim());
      return;
    }
    if (target.kind === "entry") {
      saveEntry(target.hostId, path);
      return;
    }
    const repository = repositories.find((candidate) => candidate.id === target.repositoryId);
    if (repository) saveBinding(target.hostId, repository, path, target.bindingName);
  };

  const saveTypedEntry = (hostId: string) => {
    const key = `${hostId}::project`;
    const path = (folderDrafts[key] ?? entryByHost.get(hostId)?.workspace ?? "").trim();
    if (!path) return;
    saveEntry(hostId, path);
  };

  const saveTypedBinding = (hostId: string, repository: ProjectRepository, bindingName: string) => {
    const key = `${hostId}::binding::${bindingName}`;
    const existing = bindings.find(
      (binding) => binding.host_id === hostId && binding.repository_id === repository.id,
    );
    const path = (folderDrafts[key] ?? existing?.workspace ?? "").trim();
    if (!path) return;
    saveBinding(hostId, repository, path, bindingName);
  };

  const renderHostCard = (hostId: string) => {
    const host = (hosts.data ?? []).find((candidate) => candidate.host_id === hostId);
    const root = (hostRoots.data?.roots ?? []).find((candidate) => candidate.host_id === hostId);
    const entry = entryByHost.get(hostId);
    const hostOnline = host?.status === "online";
    return (
      <div
        key={hostId}
        className="min-w-0 space-y-3 rounded-lg border p-3"
        data-testid={`project-code-host-${hostId}`}
      >
        <div className="flex items-center gap-2 font-medium text-ui">
          <span
            className={`size-2 rounded-full ${hostOnline ? "bg-green-500" : "bg-muted-foreground"}`}
          />
          <span className="min-w-0 truncate" title={hostName(hostId)}>
            {hostName(hostId)}
          </span>
          {host?.platform && (
            <span className="text-sm font-normal text-muted-foreground">{host.platform}</span>
          )}
        </div>

        <div className="space-y-1" data-testid={`project-code-entry-${hostId}`}>
          <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
            <div className="min-w-0 flex-1 basis-48">
              <p className="text-sm font-medium">Project folder</p>
              {entry ? (
                <p
                  className="truncate font-mono text-sm text-muted-foreground"
                  title={entry.workspace}
                >
                  {entry.workspace}
                </p>
              ) : (
                <p className="text-sm text-muted-foreground">No project folder set</p>
              )}
            </div>
            <div className="flex shrink-0 gap-1">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                data-testid={`project-code-entry-browse-${hostId}`}
                onClick={() => openPicker({ kind: "entry", hostId })}
                disabled={pending || !hostOnline}
              >
                {entry ? "Change…" : "Set folder…"}
              </Button>
              {entry && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  data-testid={`project-code-entry-remove-${hostId}`}
                  onClick={() => removeEntry(hostId)}
                  disabled={pending}
                >
                  Remove
                </Button>
              )}
            </div>
          </div>
          {!hostOnline && (
            <div className="flex min-w-0 gap-2">
              <input
                className={`${inputClassName} min-w-0 flex-1 font-mono`}
                data-testid={`project-code-entry-path-${hostId}`}
                aria-label={`Project folder on ${hostName(hostId)}`}
                placeholder="/path/to/project"
                value={folderDrafts[`${hostId}::project`] ?? entry?.workspace ?? ""}
                onChange={(event) =>
                  setFolderDrafts((current) => ({
                    ...current,
                    [`${hostId}::project`]: event.target.value,
                  }))
                }
                disabled={pending}
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                data-testid={`project-code-entry-save-${hostId}`}
                onClick={() => saveTypedEntry(hostId)}
                disabled={pending}
              >
                Save
              </Button>
            </div>
          )}
          {uncheckedKeys.has(`${hostId}::project`) && (
            <p
              className="text-sm text-amber-700 dark:text-amber-400"
              data-testid={`project-code-unchecked-${hostId}-project`}
            >
              Saved without checking — host offline
            </p>
          )}
          {entry && (
            <FolderFactsLine
              projectId={projectId}
              hostId={hostId}
              path={entry.workspace}
              testId={`project-code-entry-facts-${hostId}`}
              outcome={outcomes.find(
                (outcome) => outcome.host_id === hostId && outcome.kind === "entry",
              )}
              onRunAgain={() => runEntrySetup(hostId, entry.workspace)}
              disabled={pending}
            />
          )}
        </div>

        {repositories.map((repository) => {
          const binding = bindings.find(
            (candidate) =>
              candidate.host_id === hostId && candidate.repository_id === repository.id,
          );
          const bindingName = binding?.name ?? repository.name;
          const sameAsEntry = binding ? sameFolder(binding.workspace, entry?.workspace) : false;
          return (
            <div
              key={repository.id}
              className="space-y-1 border-t pt-2"
              data-testid={`project-code-repo-folder-${hostId}-${repository.name}`}
            >
              <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
                <div className="min-w-0 flex-1 basis-48">
                  <p className="text-sm font-medium">{repository.name} folder</p>
                  {binding ? (
                    <p
                      className="truncate font-mono text-sm text-muted-foreground"
                      title={binding.workspace}
                    >
                      {binding.workspace}
                    </p>
                  ) : (
                    <p className="text-sm text-muted-foreground">No folder set</p>
                  )}
                  {sameAsEntry && (
                    <p
                      className="text-sm text-muted-foreground"
                      data-testid={`project-code-binding-same-${hostId}-${repository.name}`}
                    >
                      Same as the project folder
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 gap-1">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    data-testid={`project-code-binding-browse-${hostId}-${repository.name}`}
                    onClick={() =>
                      openPicker({
                        kind: "binding",
                        hostId,
                        repositoryId: repository.id,
                        bindingName,
                      })
                    }
                    disabled={pending || !hostOnline}
                  >
                    {binding ? "Change…" : "Set folder…"}
                  </Button>
                  {binding && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      data-testid={`project-code-binding-remove-${hostId}-${repository.name}`}
                      onClick={() => removeBinding(hostId, binding.name)}
                      disabled={pending}
                    >
                      Remove
                    </Button>
                  )}
                </div>
              </div>
              {!hostOnline && (
                <div className="flex min-w-0 gap-2">
                  <input
                    className={`${inputClassName} min-w-0 flex-1 font-mono`}
                    data-testid={`project-code-binding-path-${hostId}-${repository.name}`}
                    aria-label={`${repository.name} folder on ${hostName(hostId)}`}
                    placeholder="/path/to/repository"
                    value={
                      folderDrafts[`${hostId}::binding::${bindingName}`] ?? binding?.workspace ?? ""
                    }
                    onChange={(event) =>
                      setFolderDrafts((current) => ({
                        ...current,
                        [`${hostId}::binding::${bindingName}`]: event.target.value,
                      }))
                    }
                    disabled={pending}
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    data-testid={`project-code-binding-save-${hostId}-${repository.name}`}
                    onClick={() => saveTypedBinding(hostId, repository, bindingName)}
                    disabled={pending}
                  >
                    Save
                  </Button>
                </div>
              )}
              {uncheckedKeys.has(`${hostId}::${repository.name}`) && (
                <p
                  className="text-sm text-amber-700 dark:text-amber-400"
                  data-testid={`project-code-unchecked-${hostId}-${repository.name}`}
                >
                  Saved without checking — host offline
                </p>
              )}
              {binding && (
                <FolderFactsLine
                  projectId={projectId}
                  hostId={hostId}
                  path={binding.workspace}
                  testId={`project-code-binding-facts-${hostId}-${repository.name}`}
                  repository={repository}
                  outcome={outcomes.find(
                    (outcome) =>
                      outcome.host_id === hostId &&
                      outcome.kind === "binding" &&
                      outcome.target === binding.name,
                  )}
                  onRunAgain={() => runBindingSetup(hostId, binding.name)}
                  onUseRemote={(url) => applyFolderRemote(repository, url)}
                  disabled={pending}
                />
              )}
            </div>
          );
        })}

        {problems
          .filter((problem) => problem.host_id === hostId)
          .map((problem) => {
            const orphan = bindings.find((binding) => binding.id === problem.binding_id);
            return (
              <div
                key={problem.binding_id}
                className="space-y-1 rounded-md border border-amber-400/60 p-2"
                data-testid={`project-code-dangling-${hostId}-${problem.binding_id}`}
              >
                <p className="text-sm text-amber-700 dark:text-amber-400">
                  This folder points at a repository that no longer exists.
                </p>
                {orphan && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className={mutedButtonClassName}
                    data-testid={`project-code-dangling-remove-${hostId}-${orphan.name}`}
                    onClick={() => removeBinding(hostId, orphan.name)}
                    disabled={pending}
                  >
                    Remove
                  </Button>
                )}
              </div>
            );
          })}

        {root ? (
          <div
            className="space-y-1 text-sm text-muted-foreground"
            data-testid={`project-code-root-${hostId}`}
          >
            <p>
              New sessions open in: {root.workspace}
              {root.source === "config" ? " (legacy project default)" : ""}
            </p>
            <p>New worktrees come from: {root.checkout ?? root.workspace}</p>
          </div>
        ) : (
          <p
            className="text-sm text-amber-700 dark:text-amber-400"
            data-testid={`project-code-refused-${hostId}`}
          >
            Sessions here are refused — set a project folder
          </p>
        )}

        <AgentCodeNotePanel projectId={projectId} hostId={hostId} />
      </div>
    );
  };

  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="project-code-section">
      <div className="min-w-0">
        <span className="font-medium text-ui">Project code</span>
        <p className="text-sm text-muted-foreground">
          Where this project&apos;s code lives on each host.
        </p>
        <p className="text-sm text-muted-foreground" data-testid="project-code-immediate-note">
          Changes here save immediately.
        </p>
        <p className="text-sm text-muted-foreground">Changes reach new sessions only.</p>
      </div>

      {actionError && (
        <p className="text-destructive text-ui" role="alert" data-testid="project-code-error">
          {actionError}
        </p>
      )}

      <section className="min-w-0 space-y-3" data-testid="project-code-repositories">
        <h3 className="font-medium text-ui">Repositories</h3>
        {repositories.length === 0 && (
          <p className="text-sm text-muted-foreground">No repositories yet.</p>
        )}
        {repositories.length > 0 && !hasCodeRepository && (
          <p
            className="text-sm text-amber-700 dark:text-amber-400"
            data-testid="project-code-no-code-repo"
          >
            No code repository yet
          </p>
        )}
        {repositories.map((repository) => (
          <div
            key={repository.id}
            className="min-w-0 space-y-2 rounded-lg border p-3"
            data-testid={`project-code-repo-${repository.name}`}
          >
            <div className="flex min-w-0 items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium text-ui" title={repository.name}>
                  {repository.name}
                </p>
                <p
                  className="truncate font-mono text-sm text-muted-foreground"
                  title={repository.remote_url}
                >
                  {repository.remote_url || "No git location"}
                </p>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="shrink-0"
                data-testid={`project-code-repo-remove-${repository.name}`}
                onClick={() => removeRepository(repository)}
                disabled={pending}
              >
                Remove
              </Button>
            </div>
            <div className="flex min-w-0 flex-wrap gap-4">
              <label className="space-y-1 text-sm">
                <span className="block font-medium">Role</span>
                <select
                  className={inputClassName}
                  data-testid={`project-code-repo-role-${repository.name}`}
                  aria-label={`Role for ${repository.name}`}
                  value={repository.role}
                  onChange={(event) =>
                    changeRole(repository, event.target.value === "code" ? "code" : "related")
                  }
                  disabled={pending}
                >
                  <option value="code">Code we change</option>
                  <option value="related">Related code</option>
                </select>
              </label>
              <label className="min-w-0 flex-1 space-y-1 text-sm">
                <span className="block font-medium">Default branch</span>
                <input
                  className={inputClassName}
                  data-testid={`project-code-repo-branch-${repository.name}`}
                  aria-label={`Default branch for ${repository.name}`}
                  value={branchDrafts[repository.name] ?? repository.default_branch}
                  onChange={(event) =>
                    setBranchDrafts((current) => ({
                      ...current,
                      [repository.name]: event.target.value,
                    }))
                  }
                  onBlur={() => commitBranch(repository)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") event.currentTarget.blur();
                  }}
                  disabled={pending}
                />
              </label>
            </div>
          </div>
        ))}

        {!addOpen ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid="project-code-add-repo-open"
            onClick={() => {
              setAddOpen(true);
              setAddMode("folder");
            }}
          >
            + Add repository
          </Button>
        ) : (
          <form
            className="min-w-0 space-y-3 rounded-lg border bg-muted/20 p-4"
            onSubmit={(event) => {
              event.preventDefault();
              if (canSubmitAdd) submitAdd();
            }}
          >
            {addMode === "folder" ? (
              <>
                <div className="space-y-1">
                  <label htmlFor={`${formId}-add-host`} className="block font-medium">
                    Host
                  </label>
                  <select
                    id={`${formId}-add-host`}
                    className={inputClassName}
                    data-testid="project-code-add-host"
                    value={effectiveAddHostId}
                    onChange={(event) => {
                      setAddHostId(event.target.value);
                      setAddFolder("");
                      setAddFactsPath(null);
                    }}
                    disabled={pending}
                  >
                    {eligibleHosts.map((host) => (
                      <option key={host.host_id} value={host.host_id}>
                        {host.name}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-1">
                  <label htmlFor={`${formId}-add-folder`} className="block font-medium">
                    Folder on this host
                  </label>
                  <div className="flex min-w-0 gap-2">
                    <input
                      id={`${formId}-add-folder`}
                      className={`${inputClassName} min-w-0 flex-1 font-mono`}
                      data-testid="project-code-add-folder"
                      placeholder="/path/to/repository"
                      value={addFolder}
                      title={addFolder || undefined}
                      onChange={(event) => {
                        setAddFolder(event.target.value);
                        setAddFactsPath(null);
                      }}
                      onBlur={commitAddFolder}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") event.currentTarget.blur();
                      }}
                      disabled={pending}
                    />
                    <Button
                      type="button"
                      variant="outline"
                      data-testid="project-code-add-browse"
                      onClick={() => {
                        if (selectedAddHost) {
                          openPicker({ kind: "add", hostId: selectedAddHost.host_id });
                        }
                      }}
                      disabled={pending || !selectedAddHost || selectedAddHost.status !== "online"}
                    >
                      Browse…
                    </Button>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    Type the absolute path, or Browse… the host
                  </p>
                </div>
                {addFactsPath !== null && addFactsQuery.isPending && (
                  <p className="text-sm text-muted-foreground" data-testid="project-code-add-facts">
                    Reading folder…
                  </p>
                )}
                {addFactsPath !== null && addFactsQuery.isError && (
                  <p className="text-sm text-muted-foreground" data-testid="project-code-add-facts">
                    Couldn&apos;t read this folder.
                  </p>
                )}
                {addFacts?.state === "offline" && (
                  <p className="text-sm text-muted-foreground" data-testid="project-code-add-facts">
                    host offline
                  </p>
                )}
                {addFacts?.state === "unsupported" && (
                  <p className="text-sm text-muted-foreground" data-testid="project-code-add-facts">
                    host needs an update
                  </p>
                )}
                {addFacts?.state === "error" && (
                  <p className="text-sm text-muted-foreground" data-testid="project-code-add-facts">
                    {addFacts.message}
                  </p>
                )}
                {addFacts?.state === "ok" && (
                  <>
                    <div className="space-y-1">
                      <label htmlFor={`${formId}-add-remote`} className="block font-medium">
                        Git remote
                      </label>
                      <select
                        id={`${formId}-add-remote`}
                        className={inputClassName}
                        data-testid="project-code-add-remote"
                        value={addRemote?.name ?? ""}
                        onChange={(event) => {
                          const name = event.target.value;
                          setAddRemoteName(name);
                          if (!addNameEdited && addFacts.state === "ok") {
                            const selected = addFacts.facts.remotes.find(
                              (remote) => remote.name === name,
                            );
                            setAddName(repositoryNameFromUrl(selected?.url ?? "", addFolder));
                          }
                        }}
                        disabled={pending || addFacts.facts.remotes.length === 0}
                      >
                        {addFacts.facts.remotes.map((remote) => (
                          <option key={remote.name} value={remote.name}>
                            {remote.name}
                          </option>
                        ))}
                      </select>
                      {addFacts.facts.remotes.length === 0 && (
                        <p className="text-sm text-muted-foreground">
                          This folder has no git remotes.
                        </p>
                      )}
                    </div>
                    <div className="space-y-1">
                      <label htmlFor={`${formId}-add-name`} className="block font-medium">
                        Name
                      </label>
                      <input
                        id={`${formId}-add-name`}
                        className={inputClassName}
                        data-testid="project-code-add-name"
                        value={addName}
                        onChange={(event) => {
                          setAddName(event.target.value);
                          setAddNameEdited(true);
                          setAddError(null);
                        }}
                        disabled={pending}
                      />
                    </div>
                    <div className="space-y-1">
                      <label htmlFor={`${formId}-add-branch`} className="block font-medium">
                        Default branch
                      </label>
                      <input
                        id={`${formId}-add-branch`}
                        className={inputClassName}
                        data-testid="project-code-add-branch"
                        value={addBranch}
                        onChange={(event) => setAddBranch(event.target.value)}
                        disabled={pending}
                      />
                    </div>
                  </>
                )}
                {addFolder.trim() === "" && addFacts?.state !== "ok" && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className={mutedButtonClassName}
                    data-testid="project-code-add-by-address"
                    onClick={() => setAddMode("address")}
                    disabled={pending}
                  >
                    Add by address
                  </Button>
                )}
              </>
            ) : (
              <>
                <div className="space-y-1">
                  <label htmlFor={`${formId}-add-url`} className="block font-medium">
                    Git location
                  </label>
                  <input
                    id={`${formId}-add-url`}
                    className={`${inputClassName} font-mono`}
                    data-testid="project-code-add-url"
                    placeholder="https://git.example.test/team/repo.git"
                    value={addUrl}
                    onChange={(event) => setAddUrl(event.target.value)}
                    disabled={pending}
                  />
                </div>
                <div className="space-y-1">
                  <label htmlFor={`${formId}-add-address-name`} className="block font-medium">
                    Name
                  </label>
                  <input
                    id={`${formId}-add-address-name`}
                    className={inputClassName}
                    data-testid="project-code-add-name"
                    value={addName}
                    onChange={(event) => {
                      setAddName(event.target.value);
                      setAddError(null);
                    }}
                    disabled={pending}
                  />
                </div>
                <div className="space-y-1">
                  <label htmlFor={`${formId}-add-address-branch`} className="block font-medium">
                    Default branch
                  </label>
                  <input
                    id={`${formId}-add-address-branch`}
                    className={inputClassName}
                    data-testid="project-code-add-branch"
                    value={addBranch}
                    onChange={(event) => setAddBranch(event.target.value)}
                    disabled={pending}
                  />
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className={mutedButtonClassName}
                  data-testid="project-code-add-by-folder"
                  onClick={() => setAddMode("folder")}
                  disabled={pending}
                >
                  Use a folder instead
                </Button>
              </>
            )}
            {addError && (
              <p
                className="text-destructive text-sm"
                role="alert"
                data-testid="project-code-add-error"
              >
                {addError}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                type="submit"
                size="sm"
                data-testid="project-code-add-submit"
                disabled={pending || !canSubmitAdd}
              >
                Add repository
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={resetAddForm}
                disabled={pending}
              >
                Cancel
              </Button>
            </div>
          </form>
        )}
      </section>

      <section className="min-w-0 space-y-3" data-testid="project-code-hosts">
        <div className="flex min-w-0 items-center justify-between gap-3">
          <h3 className="font-medium text-ui">Hosts</h3>
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="project-code-refresh"
            onClick={() => {
              void queryClient.invalidateQueries({ queryKey: ["project-code-folder-facts"] });
            }}
          >
            Refresh
          </Button>
        </div>
        <p className="text-sm text-muted-foreground">
          Sessions open in the project folder; new worktrees come from the code repository&apos;s
          folder when this host has one.
        </p>
        {cardHostIds.map(renderHostCard)}
        {addableHosts.length > 0 && (
          <label className="block space-y-1 text-sm">
            <span className="block font-medium">Add a host</span>
            <select
              className={inputClassName}
              data-testid="project-code-add-host-picker"
              value=""
              onChange={(event) => {
                const hostId = event.target.value;
                if (hostId) {
                  setExtraHostIds((current) =>
                    current.includes(hostId) ? current : [...current, hostId],
                  );
                }
              }}
            >
              <option value="">Add a host…</option>
              {addableHosts.map((host) => (
                <option key={host.host_id} value={host.host_id}>
                  {host.name}
                </option>
              ))}
            </select>
          </label>
        )}
      </section>

      {picker && (
        <WorkspacePickerDialog
          open
          onOpenChange={(open) => {
            if (!open) setPicker(null);
          }}
          hostId={picker.hostId}
          initialPath={
            picker.kind === "entry"
              ? entryByHost.get(picker.hostId)?.workspace
              : picker.kind === "binding"
                ? bindings.find(
                    (binding) =>
                      binding.host_id === picker.hostId &&
                      binding.repository_id === picker.repositoryId,
                  )?.workspace
                : addFolder || undefined
          }
          onConfirm={confirmPicker}
        />
      )}
    </div>
  );
}
