// Collaboration settings for a first-class project (repositories + per-host
// bindings behind the `project_assignments` release feature). Rendered OUTSIDE
// the settings dialog's defaults <form> (nested forms are invalid HTML), and
// every action here applies immediately instead of going through Save.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { useHosts } from "@/hooks/useHosts";
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
  const hosts = useHosts();
  const [actionError, setActionError] = useState<string | null>(null);
  const [hookWarning, setHookWarning] = useState<PostBindResult | null>(null);

  const [repoName, setRepoName] = useState("");
  const [repoUrl, setRepoUrl] = useState("");
  const [repoBranch, setRepoBranch] = useState("main");
  const [repoManifest, setRepoManifest] = useState("");

  const [bindingHost, setBindingHost] = useState("");
  const [bindingRepo, setBindingRepo] = useState("");
  const [bindingWorkspace, setBindingWorkspace] = useState("");
  const [bindingName, setBindingName] = useState("");
  const [bindingPrimary, setBindingPrimary] = useState(true);

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
        setActionError(CONFLICT_MESSAGE);
        invalidate();
      } else {
        setActionError(errorMessage(error));
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
    onError: (error: unknown) => setActionError(errorMessage(error)),
    onSuccess: () => {
      setActionError(null);
      setRepoName("");
      setRepoUrl("");
      setRepoBranch("main");
      setRepoManifest("");
      invalidate();
    },
  });

  const repoRemoveMutation = useMutation({
    mutationFn: (name: string) => deleteProjectRepository(projectId, name),
    retry: false,
    onMutate: () => setActionError(null),
    onError: (error: unknown) => setActionError(errorMessage(error)),
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
    onError: (error: unknown) => setActionError(errorMessage(error)),
    onSuccess: (binding) => {
      setActionError(null);
      setBindingWorkspace("");
      setHookWarning(postBindWarning(binding.post_bind));
      invalidate();
    },
  });

  const bindingRemoveMutation = useMutation({
    mutationFn: (input: { hostId: string; name: string }) =>
      deleteProjectHostBinding(projectId, input.hostId, input.name),
    retry: false,
    onMutate: () => setActionError(null),
    onError: (error: unknown) => setActionError(errorMessage(error)),
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
    onError: (error: unknown) => setActionError(errorMessage(error)),
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

  const groupedHosts = [...new Set(bindings.map((b) => b.host_id))];

  return (
    <div className="flex flex-col gap-4" data-testid="project-collaboration-section">
      <div className="flex items-center justify-between gap-4">
        <div className="flex flex-col">
          <span className="font-medium text-ui">Project collaboration</span>
          <span className="text-muted-foreground text-sm">
            Share repositories across hosts for assignments.
          </span>
        </div>
        <Switch
          data-testid="project-collaboration-enabled"
          checked={data.enabled}
          onCheckedChange={(next) => toggleMutation.mutate(next)}
          disabled={toggleMutation.isPending}
        />
      </div>

      <div className="flex flex-col gap-2">
        <span className="font-medium text-ui">Repositories</span>
        {repositories.map((repo) => (
          <div key={repo.id} className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 flex-col">
              <span className="truncate text-ui">{repo.name}</span>
              <span className="truncate text-muted-foreground text-sm">
                {repo.remote_url} · {repo.default_branch}
              </span>
            </div>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              data-testid={`project-collaboration-repo-remove-${repo.name}`}
              onClick={() => repoRemoveMutation.mutate(repo.name)}
              disabled={repoRemoveMutation.isPending}
            >
              Remove
            </Button>
          </div>
        ))}
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!repoName.trim() || !repoUrl.trim()) return;
            repoAddMutation.mutate();
          }}
        >
          <input
            data-testid="project-collaboration-repo-name"
            className={inputClassName}
            placeholder="Repository name"
            value={repoName}
            onChange={(e) => setRepoName(e.target.value)}
            disabled={repoAddMutation.isPending}
          />
          <input
            data-testid="project-collaboration-repo-url"
            className={inputClassName}
            placeholder="Remote URL"
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            disabled={repoAddMutation.isPending}
          />
          <input
            data-testid="project-collaboration-repo-branch"
            className={inputClassName}
            placeholder="Default branch"
            value={repoBranch}
            onChange={(e) => setRepoBranch(e.target.value)}
            disabled={repoAddMutation.isPending}
          />
          <input
            data-testid="project-collaboration-repo-manifest"
            className={inputClassName}
            placeholder="Manifest path (optional)"
            value={repoManifest}
            onChange={(e) => setRepoManifest(e.target.value)}
            disabled={repoAddMutation.isPending}
          />
          <div>
            <Button
              type="submit"
              size="sm"
              data-testid="project-collaboration-repo-add"
              disabled={repoAddMutation.isPending}
            >
              Add repository
            </Button>
          </div>
        </form>
      </div>

      <div className="flex flex-col gap-2">
        <span className="font-medium text-ui">Host bindings</span>
        {groupedHosts.map((hostId) => (
          <div key={hostId} className="flex flex-col gap-1">
            <span className="text-muted-foreground text-sm">{hostName(hostId)}</span>
            {bindings
              .filter((b) => b.host_id === hostId)
              .map((binding) => (
                <div key={binding.id} className="flex items-center justify-between gap-2">
                  <div className="flex min-w-0 flex-col">
                    <span className="truncate text-ui">
                      {binding.name}
                      {binding.is_primary && (
                        <span className="ml-2 text-muted-foreground text-sm">primary</span>
                      )}
                    </span>
                    <span className="truncate text-muted-foreground text-sm">
                      {repositoryNameById(binding.repository_id)} · {binding.workspace}
                    </span>
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
          </div>
        ))}
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!canAddBinding || !effectiveBindingHost || !bindingWorkspace.trim()) return;
            bindingAddMutation.mutate({
              hostId: effectiveBindingHost,
              name: bindingName.trim() || "primary",
              workspace: bindingWorkspace.trim(),
              repositoryName: effectiveBindingRepo,
              isPrimary: bindingPrimary,
            });
          }}
        >
          <select
            data-testid="project-collaboration-binding-host"
            className={inputClassName}
            value={effectiveBindingHost}
            onChange={(e) => setBindingHost(e.target.value)}
            disabled={!canAddBinding || bindingAddMutation.isPending}
          >
            {eligibleHosts.map((h) => (
              <option key={h.host_id} value={h.host_id}>
                {h.name}
              </option>
            ))}
          </select>
          <select
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
          <input
            data-testid="project-collaboration-binding-workspace"
            className={inputClassName}
            placeholder="/path/to/checkout"
            value={bindingWorkspace}
            onChange={(e) => setBindingWorkspace(e.target.value)}
            disabled={!canAddBinding || bindingAddMutation.isPending}
          />
          <input
            data-testid="project-collaboration-binding-name"
            className={inputClassName}
            placeholder="primary"
            value={bindingName}
            onChange={(e) => setBindingName(e.target.value)}
            disabled={!canAddBinding || bindingAddMutation.isPending}
          />
          <div className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground text-sm">Primary binding</span>
            <Switch
              data-testid="project-collaboration-binding-primary"
              checked={bindingPrimary}
              onCheckedChange={setBindingPrimary}
              disabled={!canAddBinding || bindingAddMutation.isPending}
            />
          </div>
          <div>
            <Button
              type="submit"
              size="sm"
              data-testid="project-collaboration-binding-add"
              disabled={!canAddBinding || bindingAddMutation.isPending}
            >
              Add binding
            </Button>
          </div>
        </form>
      </div>

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

      {actionError && (
        <p
          className="text-destructive text-ui"
          role="alert"
          data-testid="project-collaboration-error"
        >
          {actionError}
        </p>
      )}
    </div>
  );
}
