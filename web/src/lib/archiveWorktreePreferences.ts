import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import { useViewerId } from "@/hooks/useViewerId";

export const DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY = "omnigent:delete-worktrees-on-archive";
export type ArchiveWorktreeMode = "delete_safe" | "never";

function parseMode(value: unknown): ArchiveWorktreeMode | null {
  return value === "delete_safe" || value === "never" ? value : null;
}

function legacyChoice(): boolean | null {
  try {
    const value = window.localStorage.getItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY);
    return value === "true" ? true : value === "false" ? false : null;
  } catch {
    return null;
  }
}

function clearLegacyChoice() {
  try {
    window.localStorage.removeItem(DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY);
  } catch {
    // The server is authoritative even if this browser cannot clear storage.
  }
}

/** Carry the old browser choice over only when the server has no choice yet. */
export async function fetchArchiveWorktreePreference(): Promise<ArchiveWorktreeMode> {
  const generation = getOmnigentHostGeneration();
  const response = await authenticatedFetch("/v1/me", { cache: "no-store" });
  if (!response.ok) throw new Error(`Loading archive preferences failed (${response.status})`);
  const body = await response.json();
  if (generation !== getOmnigentHostGeneration()) throw new Error("Server connection changed");
  const stored = body?.preferences?.settings?.worktree_archive;
  const mode = parseMode(stored?.mode);
  if (stored != null) {
    if (generation === getOmnigentHostGeneration()) clearLegacyChoice();
    return mode ?? "never";
  }
  const legacy = legacyChoice();
  if (legacy === null) return "never";
  if (generation !== getOmnigentHostGeneration()) throw new Error("Server connection changed");
  const migrated = await authenticatedFetch("/v1/me/preferences/worktree_archive/migrate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ delete_safe: legacy }),
  });
  if (!migrated.ok) throw await apiErrorFromResponse(migrated);
  const result = parseMode((await migrated.json())?.mode);
  if (generation !== getOmnigentHostGeneration()) throw new Error("Server connection changed");
  if (!result) throw new Error("Invalid archive preference response");
  if (generation === getOmnigentHostGeneration()) clearLegacyChoice();
  return result;
}

export async function saveArchiveWorktreePreference(mode: ArchiveWorktreeMode): Promise<void> {
  const response = await authenticatedFetch("/v1/me/preferences/worktree_archive", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: { mode } }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
}

export function useArchiveWorktreePreference() {
  const info = useServerInfo();
  const viewerId = useViewerId();
  const queryClient = useQueryClient();
  const queryKey = [
    "archive-worktree-preference",
    getOmnigentServerIdentity(),
    getOmnigentHostGeneration(),
    viewerId,
  ];
  const enabled = info !== "loading" && info.worktree_status === true && viewerId !== null;
  const preference = useQuery({
    queryKey,
    queryFn: fetchArchiveWorktreePreference,
    enabled,
    staleTime: 0,
  });
  const save = useMutation({
    mutationFn: saveArchiveWorktreePreference,
    onMutate: () => queryClient.cancelQueries({ queryKey }),
    onSuccess: (_result, mode) => queryClient.setQueryData(queryKey, mode),
  });
  return { preference, save };
}
