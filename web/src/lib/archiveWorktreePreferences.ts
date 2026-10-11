import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";
import { useViewerId } from "@/hooks/useViewerId";

export const DELETE_WORKTREES_ON_ARCHIVE_STORAGE_KEY = "omnigent:delete-worktrees-on-archive";
export type ArchiveWorktreeMode = "never" | "green_only" | "yellow_too";
export interface ArchiveWorktreePreference {
  mode: ArchiveWorktreeMode;
  ask_before_archive: boolean;
  rebuildable_paths: string[];
}

export const DEFAULT_REBUILDABLE_PATHS = [
  "node_modules/",
  ".venv/",
  "venv/",
  "__pycache__/",
  ".pytest_cache/",
  ".ruff_cache/",
  ".mypy_cache/",
  ".tox/",
  "dist/",
  "build/",
  ".next/",
  "coverage/",
];

function defaultPreference(ask = true): ArchiveWorktreePreference {
  return {
    mode: "never",
    ask_before_archive: ask,
    rebuildable_paths: [...DEFAULT_REBUILDABLE_PATHS],
  };
}

function parseMode(value: unknown): ArchiveWorktreeMode | null {
  return value === "never" || value === "green_only" || value === "yellow_too" ? value : null;
}

function parsePreference(value: unknown): ArchiveWorktreePreference | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const stored = value as Record<string, unknown>;
  if (Object.keys(stored).length === 1 && "mode" in stored) {
    if (stored.mode === "delete_safe") return { ...defaultPreference(), mode: "green_only" };
    if (stored.mode === "never") return defaultPreference(false);
    return null;
  }
  const mode = parseMode(stored.mode);
  if (
    !mode ||
    Object.keys(stored).length !== 3 ||
    typeof stored.ask_before_archive !== "boolean" ||
    !Array.isArray(stored.rebuildable_paths) ||
    stored.rebuildable_paths.length > 64 ||
    !stored.rebuildable_paths.every((path) => typeof path === "string" && path.length <= 256)
  )
    return null;
  return {
    mode,
    ask_before_archive: stored.ask_before_archive,
    rebuildable_paths: stored.rebuildable_paths,
  };
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
export async function fetchArchiveWorktreePreference(): Promise<ArchiveWorktreePreference> {
  const generation = getOmnigentHostGeneration();
  const response = await authenticatedFetch("/v1/me", { cache: "no-store" });
  if (!response.ok) throw new Error(`Loading archive preferences failed (${response.status})`);
  const body = await response.json();
  if (generation !== getOmnigentHostGeneration()) throw new Error("Server connection changed");
  const preferences = body?.preferences;
  if (
    preferences !== null &&
    (typeof preferences !== "object" ||
      Array.isArray(preferences) ||
      preferences.settings === null ||
      typeof preferences.settings !== "object" ||
      Array.isArray(preferences.settings))
  ) {
    return defaultPreference(false);
  }
  const stored = preferences?.settings?.worktree_archive;
  if (stored !== undefined) {
    if (generation === getOmnigentHostGeneration()) clearLegacyChoice();
    return parsePreference(stored) ?? defaultPreference(false);
  }
  const legacy = legacyChoice();
  if (legacy === null) return defaultPreference();
  if (generation !== getOmnigentHostGeneration()) throw new Error("Server connection changed");
  const migrated = await authenticatedFetch("/v1/me/preferences/worktree_archive/migrate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ delete_safe: legacy }),
  });
  if (!migrated.ok) throw await apiErrorFromResponse(migrated);
  const result = parsePreference(await migrated.json());
  if (generation !== getOmnigentHostGeneration()) throw new Error("Server connection changed");
  if (!result) throw new Error("Invalid archive preference response");
  if (generation === getOmnigentHostGeneration()) clearLegacyChoice();
  return result;
}

export async function saveArchiveWorktreePreference(
  value: ArchiveWorktreePreference,
): Promise<void> {
  const generation = getOmnigentHostGeneration();
  const response = await authenticatedFetch("/v1/me/preferences/worktree_archive", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  if (generation !== getOmnigentHostGeneration()) throw new Error("Server connection changed");
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
    onSuccess: (_result, value) => {
      queryClient.setQueryData(queryKey, value);
      void queryClient.invalidateQueries({ queryKey: ["session-worktree-status"] });
    },
  });
  return { preference, save };
}
