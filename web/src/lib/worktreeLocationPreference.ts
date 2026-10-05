/**
 * User preference: where new worktrees are created, on every host.
 *
 * Stored server-side under the ``worktree_location`` namespace as
 * ``{"pathTemplate": "<template>"}``. Deliberately not part of
 * ``userPreferencesSync``: that sync retries silently, so the server's
 * template validation error could never reach the user.
 */

import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

const WORKTREE_LOCATION_NAMESPACE = "worktree_location";

interface CurrentUserPreferences {
  preferences?: {
    settings?: {
      worktree_location?: { pathTemplate?: unknown } | null;
    } | null;
  } | null;
}

/** Read the stored worktree location template, or null when unset. */
export async function fetchWorktreePathTemplate(): Promise<string | null> {
  const response = await authenticatedFetch("/v1/me", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Loading the worktree location failed (${response.status})`);
  }
  const body = (await response.json()) as CurrentUserPreferences;
  const value = body.preferences?.settings?.worktree_location?.pathTemplate;
  return typeof value === "string" ? value : null;
}

/**
 * Save the worktree location template, or clear it with ``null`` so new
 * worktrees use the default next to the repository. Throws the server's
 * validation message on a refusal.
 */
export async function saveWorktreePathTemplate(template: string | null): Promise<void> {
  const response = await authenticatedFetch(`/v1/me/preferences/${WORKTREE_LOCATION_NAMESPACE}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: template === null ? null : { pathTemplate: template } }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
}
