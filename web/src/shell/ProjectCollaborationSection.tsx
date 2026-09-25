// Collaboration settings for a first-class project (repositories + per-host
// bindings behind the `project_assignments` release feature). Rendered OUTSIDE
// the settings dialog's defaults <form> (nested forms are invalid HTML), and
// every action here applies immediately instead of going through Save.
import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { HostWorkspacePicker, isNavigablePath } from "./WorkspacePicker";
import { useHosts } from "@/hooks/useHosts";
import { useProjectHostRoots } from "@/hooks/useConversations";
import {
  deleteProjectHostBinding,
  deleteProjectRepository,
  getProjectCollaboration,
  putProjectHostBinding,
  putProjectRepository,
  setProjectCollaborationEnabled,
  verifyProjectHostBinding,
  type PostBindResult,
  type ProjectCollaborationProblem,
} from "@/lib/projectsApi";

const CONFLICT_MESSAGE =
  "Collaboration settings changed elsewhere; the latest settings are shown. Try again.";

/** Hook statuses worth surfacing in the settings section; the rest are silent. */
const WARNING_HOOK_STATUSES = new Set(["failed", "timed_out", "unreachable"]);

function postBindWarning(result: PostBindResult | undefined): PostBindResult | null {
  return result && WARNING_HOOK_STATUSES.has(result.status) ? result : null;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Try again.";
}

function problemText(
  problem: ProjectCollaborationProblem,
  hostName: (hostId: string) => string,
  bindingName: (bindingId: string) => string | null,
): string {
  if (problem.code === "missing_primary") {
    return `Host "${hostName(problem.host_id)}" has enabled bindings but no primary binding.`;
  }
  const name = bindingName(problem.binding_id) ?? problem.binding_id;
  return `Binding "${name}" on host "${hostName(problem.host_id)}" points at a repository that is no longer registered.`;
}

const inputClassName =
  "w-full rounded-md border bg-transparent px-3 py-2 text-ui outline-none disabled:cursor-not-allowed disabled:opacity-50";

export function ProjectCollaborationSection({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const queryKey = ["project-collaboration", projectId];
  const collaboration = useQuery({
    queryKey,
    queryFn: () => getProjectCollaboration(projectId),
    retry: false,
  });
  // The project's root per host; an entry-sourced root is the host's default
  // checkout until a primary enabled binding replaces it (R-CHECKOUT).
  const hostRoots = useProjectHostRoots(projectId);
  const hosts = useHosts();
  const [actionError, setActionError] = useState<{
    message: string;
    source: "repository" | "checkout" | "other";
  } | null>(null);
  const [hookWarning, setHookWarning] = useState<PostBindResult | null>(null);

  const [repoName, setRepoName] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [repoBranch, setRepoBranch] = useState("main");
  const [repoManifest, setRepoManifest] = useState("");
  const [repoNameEdited, setRepoNameEdited] = useState(false);
  const [repoFormOpen, setRepoFormOpen] = useState(false);
  const [bindingFormLocation, setBindingFormLocation] = useState<string | null>(null);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const formId = useId();

  const [bindingHost, setBindingHost] = useState("");
  const [bindingRepo, setBindingRepo] = useState("");
  const [bindingWorkspace, setBindingWorkspace] = useState("");
  const [bindingName, setBindingName] = useState("");
  const [bindingPrimary, setBindingPrimary] = useState<boolean | null>(null);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey });
    void queryClient.invalidateQueries({ queryKey: ["project-host-roots", projectId] });
  };

  const toggleMutation = useMutation({
    mutationFn: (next: boolean) => {
      const revision = collaboration.data?.revision ?? 0;
      return setProjectCollaborationEnabled(projectId, next, revision);
    },
    retry: false,
    onMutate: () => setActionError(null),
    // A 409 means another writer moved the revision first: show the fixed
    // notice and refetch so the switch reflects the server's latest state.
    onError: (error: unknown) => {
      if ((error as { status?: number }).status === 409) {
        setActionError({ message: CONFLICT_MESSAGE, source: "other" });
        invalidate();
      } else {
        setActionError({ message: errorMessage(error), source: "other" });
      }
    },
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
  });

  const repoAddMutation = useMutation({
    mutationFn: () =>
      putProjectRepository(projectId, repoName.trim(), {
        remote_url: repoUrl.trim(),
        default_branch: repoBranch.trim() || "main",
        ...(repoManifest.trim() ? { context_manifest_path: repoManifest.trim() } : {}),
      }),
    retry: false,
    onMutate: () => setActionError(null),
    onError: (error: unknown) =>
      setActionError({ message: errorMessage(error), source: "repository" }),
    onSuccess: () => {
      setActionError(null);
      setRepoName("");
      setRepoUrl("");
      setRepoBranch("main");
      setRepoManifest("");
      setRepoNameEdited(false);
      setRepoFormOpen(false);
      invalidate();
    },
  });

  const repoRemoveMutation = useMutation({
    mutationFn: (name: string) => deleteProjectRepository(projectId, name),
    retry: false,
    onMutate: () => setActionError(null),
    onError: (error: unknown) => setActionError({ message: errorMessage(error), source: "other" }),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
  });

  const bindingAddMutation = useMutation({
    mutationFn: (input: {
      hostId: string;
      name: string;
      workspace: string;
      repositoryName: string;
      isPrimary: boolean;
    }) =>
      putProjectHostBinding(projectId, input.hostId, input.name, {
        workspace: input.workspace,
        repository_name: input.repositoryName,
        is_primary: input.isPrimary,
      }),
    retry: false,
    onMutate: () => {
      setActionError(null);
      setHookWarning(null);
    },
    onError: (error: unknown) =>
      setActionError({ message: errorMessage(error), source: "checkout" }),
    onSuccess: (binding) => {
      setActionError(null);
      setBindingWorkspace("");
      setBindingName("");
      setBindingFormLocation(null);
      setWorkspaceOpen(false);
      setHookWarning(postBindWarning(binding.post_bind));
      invalidate();
    },
  });

  const bindingRemoveMutation = useMutation({
    mutationFn: (input: { hostId: string; name: string }) =>
      deleteProjectHostBinding(projectId, input.hostId, input.name),
    retry: false,
    onMutate: () => setActionError(null),
    onError: (error: unknown) => setActionError({ message: errorMessage(error), source: "other" }),
    onSuccess: () => {
      setActionError(null);
      invalidate();
    },
  });

  const bindingVerifyMutation = useMutation({
    mutationFn: (input: { hostId: string; name: string }) =>
      verifyProjectHostBinding(projectId, input.hostId, input.name),
    retry: false,
    onMutate: () => {
      setActionError(null);
      setHookWarning(null);
    },
    onError: (error: unknown) => setActionError({ message: errorMessage(error), source: "other" }),
    onSuccess: (binding) => {
      setActionError(null);
      setHookWarning(postBindWarning(binding.post_bind));
      invalidate();
    },
  });

  if (collaboration.isPending) {
    return <p className="text-muted-foreground text-sm">Loading collaboration settings…</p>;
  }

  if (collaboration.isError) {
    return (
      <p
        className="text-destructive text-ui"
        role="alert"
        data-testid="project-collaboration-error"
      >
        Couldn&apos;t load collaboration settings. Close and reopen to try again.
      </p>
    );
  }

  const data = collaboration.data;
  const repositories = data.repositories;
  const bindings = data.bindings;
  const hostName = (hostId: string) =>
    (hosts.data ?? []).find((h) => h.host_id === hostId)?.name ?? hostId;
  const bindingNameById = (bindingId: string) =>
    bindings.find((b) => b.id === bindingId)?.name ?? null;
  const repositoryNameById = (repositoryId: string) =>
    repositories.find((r) => r.id === repositoryId)?.name ?? repositoryId;
  // Sandbox hosts are server-provisioned launch targets, never manual
  // binding targets — mirror the pickers that hide them (see useHosts).
  const eligibleHosts = (hosts.data ?? []).filter((h) => !h.sandbox_provider);
  const effectiveBindingHost = bindingHost || eligibleHosts[0]?.host_id || "";
  // The stored repo choice can outlive its registration (e.g. selected `api`,
  // then `api` removed while `web` remains) — fall back to the first
  // registered repository so the select and the request agree.
  const effectiveBindingRepo = repositories.some((r) => r.name === bindingRepo)
    ? bindingRepo
    : (repositories[0]?.name ?? "");
  const canAddBinding = repositories.length > 0;
  const entryRootByHost = new Map(
    (hostRoots.data?.roots ?? [])
      .filter((root) => root.source === "entry")
      .map((root) => [root.host_id, root.workspace] as const),
  );
  const hasPrimaryEnabledBinding = (hostId: string) =>
    bindings.some((b) => b.host_id === hostId && b.is_primary && b.enabled);

  // Host groups: every host with a binding, plus every host whose entry is its
  // default checkout — a host with an entry but no bindings still gets a group.
  const groupedHosts = [...new Set([...bindings.map((b) => b.host_id), ...entryRootByHost.keys()])];
  const selectedHost = eligibleHosts.find((h) => h.host_id === effectiveBindingHost);
  const effectiveBindingName = bindingName.trim() || effectiveBindingRepo;
  const replacedBinding = bindings.find(
    (b) => b.host_id === effectiveBindingHost && b.name === effectiveBindingName,
  );
  const otherPrimaryBinding = bindings.find(
    (b) => b.host_id === effectiveBindingHost && b.is_primary && b.name !== effectiveBindingName,
  );
  const effectiveBindingPrimary = otherPrimaryBinding
    ? false
    : (bindingPrimary ??
      replacedBinding?.is_primary ??
      !bindings.some((b) => b.host_id === effectiveBindingHost && b.is_primary));
  const actionErrorElement = actionError && (
    <p className="text-destructive text-ui" role="alert" data-testid="project-collaboration-error">
      {actionError.message}
    </p>
  );
  const openBindingForm = (location: string, hostId: string) => {
    setBindingFormLocation(location);
    setBindingHost(hostId);
    setBindingPrimary(null);
    // Prefill the host's entry (its default checkout); a hand-set binding path
    // still wins on submit.
    setBindingWorkspace(entryRootByHost.get(hostId) ?? "");
    setBindingName("");
    setWorkspaceOpen(false);
  };
  const closeBindingForm = () => {
    setBindingFormLocation(null);
    setBindingPrimary(null);
    setBindingWorkspace("");
    setBindingName("");
    setWorkspaceOpen(false);
  };
  const bindingForm = (
    <form
      className="min-w-0 space-y-3 rounded-lg border bg-muted/20 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (!canAddBinding || !effectiveBindingHost || !bindingWorkspace.trim()) return;
        bindingAddMutation.mutate({
          hostId: effectiveBindingHost,
          name: effectiveBindingName,
          workspace: bindingWorkspace.trim(),
          repositoryName: effectiveBindingRepo,
          isPrimary: effectiveBindingPrimary,
        });
      }}
    >
      <div className="space-y-1">
        <label htmlFor={`${formId}-host`} className="block font-medium">
          Host
        </label>
        <select
          id={`${formId}-host`}
          data-testid="project-collaboration-binding-host"
          className={inputClassName}
          value={effectiveBindingHost}
          onChange={(e) => {
            const nextHostId = e.target.value;
            // Follow the new host's entry only while the field is untouched
            // (still empty or still the previous host's entry).
            setBindingWorkspace((current) => {
              const previousEntry = entryRootByHost.get(effectiveBindingHost) ?? "";
              return current === "" || current === previousEntry
                ? (entryRootByHost.get(nextHostId) ?? "")
                : current;
            });
            setBindingHost(nextHostId);
            setBindingPrimary(null);
            setWorkspaceOpen(false);
          }}
          disabled={!canAddBinding || bindingAddMutation.isPending}
        >
          {eligibleHosts.map((h) => (
            <option key={h.host_id} value={h.host_id}>
              {h.name}
            </option>
          ))}
        </select>
      </div>
      <div className="space-y-1">
        <label htmlFor={`${formId}-repository`} className="block font-medium">
          Repository
        </label>
        <select
          id={`${formId}-repository`}
          data-testid="project-collaboration-binding-repo"
          className={inputClassName}
          value={effectiveBindingRepo}
          onChange={(e) => setBindingRepo(e.target.value)}
          disabled={!canAddBinding || bindingAddMutation.isPending}
        >
          {repositories.map((r) => (
            <option key={r.id} value={r.name}>
              {r.name}
            </option>
          ))}
        </select>
      </div>
      <div className="space-y-1">
        <label htmlFor={`${formId}-workspace`} className="block font-medium">
          Folder on this host
        </label>
        <div className="relative flex min-w-0 gap-2">
          <input
            id={`${formId}-workspace`}
            data-testid="project-collaboration-binding-workspace"
            className={`${inputClassName} min-w-0 flex-1 font-mono`}
            placeholder="/path/to/checkout"
            value={bindingWorkspace}
            title={bindingWorkspace || undefined}
            onChange={(e) => setBindingWorkspace(e.target.value)}
            disabled={!canAddBinding || bindingAddMutation.isPending}
          />
          <Button
            type="button"
            variant="outline"
            data-testid="project-collaboration-binding-browse"
            onClick={() => setWorkspaceOpen((value) => !value)}
            disabled={selectedHost?.status !== "online" || bindingAddMutation.isPending}
          >
            Browse…
          </Button>
          {workspaceOpen && selectedHost && (
            <>
              <button
                type="button"
                aria-label="Close checkout browser"
                className="fixed inset-0 z-10 cursor-default"
                onClick={() => setWorkspaceOpen(false)}
              />
              <div
                className="absolute top-full right-0 left-0 z-20 mt-1 rounded-xl border bg-popover p-2 shadow-menu"
                onKeyDown={(e) => {
                  if (
                    e.key === "Enter" &&
                    e.target instanceof HTMLInputElement &&
                    (e.target.type === "text" || e.target.type === "search")
                  )
                    e.preventDefault();
                }}
              >
                <HostWorkspacePicker
                  hostId={selectedHost.host_id}
                  initialPath={isNavigablePath(bindingWorkspace) ? bindingWorkspace : undefined}
                  onNavigate={setBindingWorkspace}
                />
              </div>
            </>
          )}
        </div>
        <p className="text-sm text-muted-foreground">Type the absolute path, or Browse… the host</p>
      </div>
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div>
          <label htmlFor={`${formId}-primary`} className="font-medium">
            Default checkout for this host
          </label>
          <p className="text-sm text-muted-foreground">
            {otherPrimaryBinding
              ? `“${otherPrimaryBinding.name}” is this host's default checkout.`
              : "Assignments that don't name a checkout land here. One per host."}
          </p>
        </div>
        <Switch
          id={`${formId}-primary`}
          data-testid="project-collaboration-binding-primary"
          checked={effectiveBindingPrimary}
          onCheckedChange={setBindingPrimary}
          disabled={!canAddBinding || bindingAddMutation.isPending || !!otherPrimaryBinding}
        />
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer font-medium">More options</summary>
        <div className="mt-2 space-y-1">
          <label htmlFor={`${formId}-name`} className="block font-medium">
            Checkout name
          </label>
          <input
            id={`${formId}-name`}
            data-testid="project-collaboration-binding-name"
            className={inputClassName}
            placeholder={effectiveBindingRepo}
            value={bindingName}
            onChange={(e) => setBindingName(e.target.value)}
            disabled={!canAddBinding || bindingAddMutation.isPending}
          />
          <p className="text-muted-foreground">Defaults to the repository name</p>
        </div>
      </details>
      {replacedBinding && (
        <p
          className="text-sm text-amber-700 dark:text-amber-400"
          data-testid="project-collaboration-binding-replace-warning"
        >
          A checkout named “{effectiveBindingName}” already exists on{" "}
          {hostName(effectiveBindingHost)} and will be replaced.
        </p>
      )}
      {actionError?.source === "checkout" && actionErrorElement}
      <div className="flex flex-wrap gap-2">
        <Button
          type="submit"
          size="sm"
          data-testid="project-collaboration-binding-add"
          disabled={!canAddBinding || bindingAddMutation.isPending}
        >
          Add checkout
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={closeBindingForm}
          disabled={bindingAddMutation.isPending}
        >
          Cancel
        </Button>
      </div>
    </form>
  );

  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="project-collaboration-section">
      <div className="flex min-w-0 items-start justify-between gap-4">
        <div className="min-w-0">
          <span className="font-medium text-ui">Project collaboration</span>
          <p className="text-sm text-muted-foreground">
            Share repositories across hosts so an agent on one host can hand work to another.
          </p>
        </div>
        <Switch
          data-testid="project-collaboration-enabled"
          checked={data.enabled}
          onCheckedChange={(next) => toggleMutation.mutate(next)}
          disabled={toggleMutation.isPending}
        />
      </div>
      {actionError &&
        !(
          (actionError.source === "repository" && repoFormOpen) ||
          (actionError.source === "checkout" && bindingFormLocation !== null)
        ) &&
        actionErrorElement}
      {data.problems.length > 0 && (
        <div className="flex flex-col gap-1">
          {data.problems.map((problem) => (
            <p
              key={
                problem.code === "missing_primary"
                  ? `missing_primary-${problem.host_id}`
                  : `dangling_repository-${problem.binding_id}`
              }
              className="text-ui"
              data-testid="project-collaboration-problem"
            >
              {problemText(problem, hostName, bindingNameById)}
            </p>
          ))}
        </div>
      )}

      {hookWarning && (
        <div
          className="text-destructive text-ui"
          role="status"
          data-testid="project-collaboration-hook-warning"
        >
          Binding saved; post-bind command{" "}
          {hookWarning.status === "timed_out" ? "timed out" : hookWarning.status}
          {hookWarning.error ? `: ${hookWarning.error}` : ""}
          {typeof hookWarning.exit_code === "number" ? ` (exit code ${hookWarning.exit_code})` : ""}
          {hookWarning.output ? (
            <pre className="mt-1 whitespace-pre-wrap">{hookWarning.output}</pre>
          ) : null}
        </div>
      )}

      <section className="min-w-0 space-y-3">
        <div>
          <h3 className="font-medium text-ui">1 Repositories</h3>
          <p className="text-sm text-muted-foreground">The git repositories this project shares.</p>
        </div>
        {repositories.map((repo) => (
          <div
            key={repo.id}
            className="flex min-w-0 items-start justify-between gap-3 rounded-lg border p-3"
          >
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium text-ui" title={repo.name}>
                {repo.name}
              </p>
              <p
                className="truncate font-mono text-sm text-muted-foreground"
                title={repo.remote_url}
              >
                {repo.remote_url}
              </p>
              <span
                className="mt-1 inline-block max-w-full truncate rounded border px-1.5 text-xs text-muted-foreground"
                title={repo.default_branch}
              >
                {repo.default_branch}
              </span>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="shrink-0"
              data-testid={`project-collaboration-repo-remove-${repo.name}`}
              onClick={() => repoRemoveMutation.mutate(repo.name)}
              disabled={repoRemoveMutation.isPending}
            >
              Remove
            </Button>
          </div>
        ))}
        {!repoFormOpen ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid="project-collaboration-repo-open"
            onClick={() => setRepoFormOpen(true)}
          >
            + Add repository
          </Button>
        ) : (
          <form
            className="min-w-0 space-y-3 rounded-lg border bg-muted/20 p-4"
            onSubmit={(e) => {
              e.preventDefault();
              if (!repoName.trim() || !repoUrl.trim()) return;
              repoAddMutation.mutate();
            }}
          >
            <div className="space-y-1">
              <label htmlFor={`${formId}-url`} className="block font-medium">
                Remote URL
              </label>
              <input
                id={`${formId}-url`}
                data-testid="project-collaboration-repo-url"
                className={`${inputClassName} font-mono`}
                value={repoUrl}
                title={repoUrl || undefined}
                onChange={(e) => {
                  const url = e.target.value;
                  setRepoUrl(url);
                  if (!repoNameEdited)
                    setRepoName(
                      url
                        .replace(/\/+$/, "")
                        .split(/[/:]/)
                        .pop()
                        ?.replace(/\.git$/i, "") ?? "",
                    );
                }}
                disabled={repoAddMutation.isPending}
              />
              <p className="text-sm text-muted-foreground">The address every host clones from</p>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${formId}-repo-name`} className="block font-medium">
                Name
              </label>
              <input
                id={`${formId}-repo-name`}
                data-testid="project-collaboration-repo-name"
                className={inputClassName}
                value={repoName}
                onChange={(e) => {
                  setRepoName(e.target.value);
                  setRepoNameEdited(true);
                }}
                disabled={repoAddMutation.isPending}
              />
              <p className="text-sm text-muted-foreground">Short name used in assignments</p>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${formId}-branch`} className="block font-medium">
                Default branch
              </label>
              <input
                id={`${formId}-branch`}
                data-testid="project-collaboration-repo-branch"
                className={inputClassName}
                value={repoBranch}
                onChange={(e) => setRepoBranch(e.target.value)}
                disabled={repoAddMutation.isPending}
              />
              <p className="text-sm text-muted-foreground">Branch new work starts from</p>
            </div>
            <details className="text-sm">
              <summary className="cursor-pointer font-medium">More options</summary>
              <div className="mt-2 space-y-1">
                <label htmlFor={`${formId}-manifest`} className="block font-medium">
                  Context manifest path (optional)
                </label>
                <input
                  id={`${formId}-manifest`}
                  data-testid="project-collaboration-repo-manifest"
                  className={inputClassName}
                  value={repoManifest}
                  onChange={(e) => setRepoManifest(e.target.value)}
                  disabled={repoAddMutation.isPending}
                />
                <p className="text-muted-foreground">
                  File in the repo that identifies the project; leave blank if unsure
                </p>
              </div>
            </details>
            {actionError?.source === "repository" && actionErrorElement}
            <div className="flex gap-2">
              <Button
                type="submit"
                size="sm"
                data-testid="project-collaboration-repo-add"
                disabled={repoAddMutation.isPending}
              >
                Add repository
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={repoAddMutation.isPending}
                onClick={() => {
                  setRepoFormOpen(false);
                  setRepoName("");
                  setRepoUrl("");
                  setRepoBranch("main");
                  setRepoManifest("");
                  setRepoNameEdited(false);
                }}
              >
                Cancel
              </Button>
            </div>
          </form>
        )}
      </section>

      <section className="min-w-0 space-y-3">
        <div>
          <h3 className="font-medium text-ui">
            2 Host checkouts{" "}
            <span className="text-sm font-normal text-muted-foreground">(host bindings)</span>
          </h3>
          <p className="text-sm text-muted-foreground">
            Where each host keeps its copy of a repository. Assignments sent to a host start in its
            default checkout.
          </p>
          {!canAddBinding && (
            <p className="text-sm text-muted-foreground">Add a repository first.</p>
          )}
        </div>
        {groupedHosts.map((hostId) => {
          const hostBindings = bindings.filter((b) => b.host_id === hostId);
          const host = (hosts.data ?? []).find((h) => h.host_id === hostId);
          return (
            <div key={hostId} className="min-w-0 space-y-2 rounded-lg border p-3">
              <div className="flex items-center gap-2 font-medium text-ui">
                <span
                  className={`size-2 rounded-full ${host?.status === "online" ? "bg-green-500" : "bg-muted-foreground"}`}
                />
                <span className="min-w-0 truncate" title={hostName(hostId)}>
                  {hostName(hostId)}
                </span>{" "}
                {host?.platform && (
                  <span className="text-sm font-normal text-muted-foreground">{host.platform}</span>
                )}
              </div>
              {!hasPrimaryEnabledBinding(hostId) && entryRootByHost.has(hostId) && (
                <p
                  className="font-mono text-sm text-muted-foreground"
                  data-testid={`project-collaboration-entry-default-${hostId}`}
                >
                  Default: project directory {entryRootByHost.get(hostId)} — sessions branch from
                  here until a primary binding is set.
                </p>
              )}
              {hostBindings.map((binding) => (
                <div
                  key={binding.id}
                  className="flex min-w-0 flex-wrap items-start justify-between gap-2 border-t pt-2"
                >
                  <div className="min-w-0 flex-1 basis-48">
                    <div className="flex min-w-0 items-center gap-2">
                      <span
                        className="min-w-0 truncate font-mono text-sm text-ui"
                        title={binding.workspace}
                      >
                        {binding.workspace}
                      </span>
                      {binding.is_primary && (
                        <span className="shrink-0 rounded border px-1.5 text-xs text-muted-foreground">
                          Default
                        </span>
                      )}
                    </div>
                    <p className="truncate text-sm text-muted-foreground">
                      {repositoryNameById(binding.repository_id)} repository · checkout name:{" "}
                      {binding.name}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-1">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      data-testid={`project-collaboration-binding-verify-${binding.host_id}-${binding.name}`}
                      onClick={() =>
                        bindingVerifyMutation.mutate({
                          hostId: binding.host_id,
                          name: binding.name,
                        })
                      }
                      disabled={bindingVerifyMutation.isPending}
                    >
                      Verify
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      data-testid={`project-collaboration-binding-remove-${binding.host_id}-${binding.name}`}
                      onClick={() =>
                        bindingRemoveMutation.mutate({
                          hostId: binding.host_id,
                          name: binding.name,
                        })
                      }
                      disabled={bindingRemoveMutation.isPending}
                    >
                      Remove
                    </Button>
                  </div>
                </div>
              ))}
              {hostBindings.flatMap((binding, index) => {
                const other = hostBindings
                  .slice(0, index)
                  .find(
                    (candidate) =>
                      candidate.workspace.replace(/[/\\]+$/, "") ===
                      binding.workspace.replace(/[/\\]+$/, ""),
                  );
                return other
                  ? [
                      <p
                        key={binding.id}
                        className="text-sm text-amber-700 dark:text-amber-400"
                        data-testid="project-collaboration-duplicate-folder"
                      >
                        Same folder as “{other.name}” — you probably only need one.
                      </p>,
                    ]
                  : [];
              })}
              {bindingFormLocation === hostId ? (
                bindingForm
              ) : (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  data-testid={`project-collaboration-binding-open-${hostId}`}
                  disabled={!canAddBinding}
                  title={!canAddBinding ? "Add a repository first." : undefined}
                  onClick={() => openBindingForm(hostId, hostId)}
                >
                  + Add checkout on {hostName(hostId)}
                </Button>
              )}
            </div>
          );
        })}
        {bindingFormLocation === "another" ? (
          bindingForm
        ) : (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            data-testid="project-collaboration-binding-open"
            disabled={!canAddBinding}
            title={!canAddBinding ? "Add a repository first." : undefined}
            onClick={() => openBindingForm("another", eligibleHosts[0]?.host_id ?? "")}
          >
            {groupedHosts.length ? "+ Add checkout on another host" : "+ Add checkout"}
          </Button>
        )}
      </section>
    </div>
  );
}
