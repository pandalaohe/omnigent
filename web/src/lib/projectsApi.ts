// Typed client for the `/v1/projects` first-class projects CRUD
// (`omnigent/server/routes/projects.py`). Projects are owner-private
// containers that group sessions and exist independently of their members —
// so they can be empty, renamed, and deleted without touching sessions.
//
// Session→project membership lives on the session, not here: file/unfile a
// session with `PATCH /v1/sessions/{id}` `{ project_id }` (see sessionsApi).
//
// All requests go through the existing Vite `/v1` proxy. TS surface is
// camelCase-friendly, but the project shape is already flat snake_case-free
// (`id`, `name`), so no boundary conversion is needed.

import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

/**
 * Default session settings a project stores, pre-filled into the new-chat
 * composer. All fields optional — an unset key means "no default for this
 * slot". The vocabulary is client-owned; the server persists the object whole
 * and never acts on it, so adding a key here needs no backend change.
 */
export interface ProjectConfig {
  /** Default host id, or the sandbox sentinel. */
  host_id?: string;
  /** Default working directory / repo path on that host. */
  workspace?: string;
  /** Default agent id for new sessions. */
  agent_id?: string;
  /** Chosen emoji icon (a unicode grapheme, e.g. "🔥"). Unset → default folder. */
  icon?: string;
  /**
   * Per-project worktree default. When `true`, a new session in a git workspace
   * starts in a fresh randomly-named worktree; when `false`, it starts directly
   * in the workspace. Both are meaningful: a set value overrides the user-global
   * "always use a worktree" default (Settings › Git). An unset key falls through
   * to that global default.
   */
  use_worktree?: boolean;
  /**
   * Default base branch a new worktree forks from, pre-filled into the
   * composer's base-branch field. Takes precedence over the user-global default
   * (Settings › Git); an unset key falls through to that global default. Blank
   * is never stored (treated the same as unset).
   */
  base_branch?: string;
  /**
   * Default model for new sessions when the default agent is a native coding
   * harness with a model choice (e.g. Claude Code's version-agnostic "opus"
   * alias, or a Codex model id resolved on the host). Only meaningful
   * alongside an `agent_id` whose harness takes a model override; unset =
   * the harness's own configured default.
   */
  model?: string;
}

/** A first-class project. Mirrors the `ProjectObject` response shape. */
export interface Project {
  id: string;
  name: string;
  /** Owner user id; `null` in single-user / OSS mode. */
  user_id?: string | null;
  created_at?: number;
  updated_at?: number | null;
  /** Stored default session settings; `{}` when the project has none. */
  config?: ProjectConfig;
}

interface ProjectListResponse {
  object: "list";
  data: Project[];
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; message?: string };
    return body.error?.message ?? body.message ?? `${res.status} ${res.statusText}`;
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

/** List the caller's projects (owner-scoped), oldest first. */
export async function listProjects(): Promise<Project[]> {
  const res = await authenticatedFetch("/v1/projects");
  if (!res.ok) throw new Error(await readError(res));
  const body = (await res.json()) as ProjectListResponse;
  return body.data;
}

/** Fetch a single project (including its `config`) by id. 404s if not owned. */
export async function getProject(id: string): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`);
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/**
 * Create a project, optionally with initial `config` defaults. Rejects with the
 * server's message on a duplicate name (409) so callers can surface it inline.
 */
export async function createProject(name: string, config?: ProjectConfig): Promise<Project> {
  const res = await authenticatedFetch("/v1/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config ? { name, config } : { name }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/** Rename a project (O(1) — members reference the id, not the name string). */
export async function renameProject(id: string, name: string): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/**
 * Replace a project's stored `config` defaults. Passing `{}` clears them
 * (the server treats `{}` as "clear", distinct from omitting the field, which
 * leaves config unchanged). Only `config` is sent, so the name is untouched.
 */
export async function updateProjectConfig(id: string, config: ProjectConfig): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/**
 * Delete a project. Only the container is removed; member sessions are kept
 * (never cascade-deleted). Their `project_id` is left dangling server-side, but
 * the dual-read listing joins against the (now-absent) project, so they surface
 * as unfiled. Returns 404 if not found / not owned.
 */
export async function deleteProject(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await readError(res));
}

/** A repository registered on a collaboration project. */
export interface ProjectRepository {
  id: string;
  project_id: string;
  name: string;
  remote_url: string;
  default_branch: string;
  context_manifest_path: string;
  revision: number;
  created_at: number;
  updated_at: number | null;
}

/**
 * Outcome of the host's post-bind hook on a binding PUT/verify. Response-only
 * (never persisted), and absent entirely on servers that predate the hook.
 */
export interface PostBindResult {
  status: string;
  exit_code: number | null;
  output: string | null;
  error: string | null;
}

/** A per-host directory binding of a collaboration project. */
export interface ProjectHostBinding {
  id: string;
  project_id: string;
  host_id: string;
  name: string;
  is_primary: boolean;
  repository_id: string;
  workspace: string;
  enabled: boolean;
  revision: number;
  path_verified_at: number | null;
  created_at: number;
  updated_at: number | null;
  /** Hook outcome from the PUT/verify that returned this binding, if any. */
  post_bind?: PostBindResult;
}

/** Machine-readable collaboration config problem. */
export type ProjectCollaborationProblem =
  | { code: "missing_primary"; host_id: string }
  | { code: "dangling_repository"; binding_id: string; host_id: string; repository_id: string };

/** Collaboration config plus validation status for a project. */
export interface ProjectCollaboration {
  enabled: boolean;
  revision: number;
  repositories: ProjectRepository[];
  bindings: ProjectHostBinding[];
  problems: ProjectCollaborationProblem[];
}

/** Body for `PUT .../repositories/{name}` (extra keys forbidden server-side). */
export interface PutProjectRepositoryBody {
  remote_url: string;
  default_branch: string;
  context_manifest_path?: string;
}

/** Body for `PUT .../hosts/{host_id}/bindings/{name}`. */
export interface PutProjectHostBindingBody {
  workspace: string;
  repository_name: string;
  is_primary?: boolean;
  enabled?: boolean;
}

async function readCollaborationJsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as T;
}

/** Fetch a project's collaboration config plus validation status. */
export async function getProjectCollaboration(id: string): Promise<ProjectCollaboration> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}/collaboration`);
  return readCollaborationJsonOrThrow<ProjectCollaboration>(res);
}

/** Flip the collaboration switch with an optimistic-concurrency revision. */
export async function setProjectCollaborationEnabled(
  id: string,
  enabled: boolean,
  expectedRevision: number,
): Promise<{ enabled: boolean; revision: number }> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}/collaboration`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled, expected_revision: expectedRevision }),
  });
  return readCollaborationJsonOrThrow<{ enabled: boolean; revision: number }>(res);
}

/** Register a repository or revise its registration. */
export async function putProjectRepository(
  id: string,
  name: string,
  body: PutProjectRepositoryBody,
): Promise<ProjectRepository> {
  const res = await authenticatedFetch(
    `/v1/projects/${encodeURIComponent(id)}/repositories/${encodeURIComponent(name)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  return readCollaborationJsonOrThrow<ProjectRepository>(res);
}

/** Delete a registered repository. */
export async function deleteProjectRepository(id: string, name: string): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/projects/${encodeURIComponent(id)}/repositories/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
}

/** Validate and store a host binding. */
export async function putProjectHostBinding(
  id: string,
  hostId: string,
  name: string,
  body: PutProjectHostBindingBody,
): Promise<ProjectHostBinding> {
  const res = await authenticatedFetch(
    `/v1/projects/${encodeURIComponent(id)}/hosts/${encodeURIComponent(hostId)}/bindings/${encodeURIComponent(name)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
  return readCollaborationJsonOrThrow<ProjectHostBinding>(res);
}

/** Delete a host binding. */
export async function deleteProjectHostBinding(
  id: string,
  hostId: string,
  name: string,
): Promise<void> {
  const res = await authenticatedFetch(
    `/v1/projects/${encodeURIComponent(id)}/hosts/${encodeURIComponent(hostId)}/bindings/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
  if (!res.ok) throw await apiErrorFromResponse(res);
}

/** Re-run host validation for the stored binding path. */
export async function verifyProjectHostBinding(
  id: string,
  hostId: string,
  name: string,
): Promise<ProjectHostBinding> {
  const res = await authenticatedFetch(
    `/v1/projects/${encodeURIComponent(id)}/hosts/${encodeURIComponent(hostId)}/bindings/${encodeURIComponent(name)}/verify`,
    { method: "POST" },
  );
  return readCollaborationJsonOrThrow<ProjectHostBinding>(res);
}
