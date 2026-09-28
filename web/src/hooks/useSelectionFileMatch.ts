// Resolves a chat selection to workspace files the FileViewer can open.
//
// Two-stage lookup: a local resolution confirmed by a parent-directory listing
// (shared with `useWorkspaceFileExists` through its cache key), then the
// server's recursive `/search` filtered to the selection's own shape. The
// popup uses this to offer "Open in panel" only when a click can land somewhere.

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { splitWorkspaceFileCitation } from "@/components/ai-elements/streamdown-security";
import {
  fetchDirEntriesTolerant,
  resolveChatFilePath,
  useWorkspaceFileSearch,
  useWorkspaceServeable,
  type WorkspaceFile,
} from "@/hooks/useWorkspaceChangedFiles";
import { useWorkspacePaths } from "@/shell/FileViewerContext";

/** One selection-matched file, with the line the selection cited (if any). */
export interface SelectionFileMatch {
  path: string;
  line: number | null;
}

export interface SelectionFileMatchResult {
  matches: SelectionFileMatch[];
  /** True while a lookup that could still produce matches is in flight. */
  pending: boolean;
  /** True when the search stopped early, so `matches` may be incomplete. */
  truncated: boolean;
}

/** Longer selections are prose or pasted blobs, not a file reference. */
const MAX_CANDIDATE_LENGTH = 300;
/** Cap on the partial-match list the popup shows. */
const MAX_MATCHES = 8;

const NO_MATCHES: SelectionFileMatch[] = [];
const NO_FILES: WorkspaceFile[] = [];

/**
 * Normalize selected text into a cited path candidate, or null when it can't
 * name a workspace file on its own. Strips wrapping quotes/backticks, sentence
 * punctuation and a `:line` / `#Lline` citation; the remaining string must
 * carry a parent segment or an extension before it's worth a lookup.
 */
function selectionCandidate(text: string): { path: string; line: number | null } | null {
  const trimmed = text.trim();
  if (!trimmed || trimmed.length > MAX_CANDIDATE_LENGTH) return null;
  if (/[\r\n]/.test(trimmed)) return null;
  const unquoted = trimmed
    .replace(/^[`"']+/, "")
    .replace(/[`"']+$/, "")
    .replace(/[.,;:)]+$/, "");
  if (!unquoted) return null;
  const citation = splitWorkspaceFileCitation(unquoted);
  const path = citation.path;
  if (!path) return null;
  if (!path.includes("/") && !/\.[A-Za-z0-9]+$/.test(path)) return null;
  return { path, line: citation.line };
}

/** Parent directory whose listing proves `path`; "" means the workspace root. */
export function parentDirectoryOf(path: string): string {
  if (path.startsWith("/")) return path.slice(0, path.lastIndexOf("/")) || "/";
  return path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
}

function basenameOf(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

/**
 * Find workspace files named by a chat selection.
 *
 * @param conversationId Session/conversation id, or undefined when not ready.
 * @param text Settled selection text; "" disables the lookup.
 */
export function useSelectionFileMatch(
  conversationId: string | undefined,
  text: string,
): SelectionFileMatchResult {
  const { root, home } = useWorkspacePaths();
  const serveable = useWorkspaceServeable(conversationId);
  const candidate = useMemo(() => selectionCandidate(text), [text]);
  const resolution = useMemo(
    () => (candidate ? resolveChatFilePath(candidate.path, root, home) : null),
    [candidate, root, home],
  );
  const parentDir = useMemo(
    () => (resolution ? parentDirectoryOf(resolution.path) : null),
    [resolution],
  );

  const exactQuery = useQuery({
    // Same key and fetcher as `useWorkspaceFileExists` so one listing serves
    // both the inline-code span and the popup.
    queryKey: ["workspace-dir-listing", conversationId, parentDir],
    queryFn: () => fetchDirEntriesTolerant(conversationId!, parentDir!),
    enabled: !!conversationId && parentDir !== null && serveable !== false,
    staleTime: 30_000,
  });
  const exactListing = exactQuery.data ?? null;
  const exactHit =
    resolution !== null &&
    !!exactListing?.files.some((e) => e.type === "file" && e.path === resolution.path);
  // The search waits for the exact listing so a slower search can't flash a
  // list for a file the cheap check has already confirmed. An errored exact
  // check falls through — it is not proof of absence.
  const exactSettled =
    resolution === null || !conversationId || exactQuery.isSuccess || exactQuery.isError;
  const searchSegment = candidate && !exactHit && exactSettled ? basenameOf(candidate.path) : "";
  const searchQuery = useWorkspaceFileSearch(conversationId, searchSegment);
  const searchReady = searchQuery.isSuccess && !searchQuery.isPlaceholderData;

  const matches = useMemo(() => {
    if (!candidate) return NO_MATCHES;
    if (exactHit && resolution) return [{ path: resolution.path, line: candidate.line }];
    if (!searchSegment || !searchReady) return NO_MATCHES;
    const files = searchQuery.data?.files ?? NO_FILES;
    const lastSegment = basenameOf(candidate.path);
    const hasParent = candidate.path.includes("/");
    const kept = files.filter((f) => {
      if (f.type !== "file") return false;
      // A basename selection may be the tail of a longer name (`panel_report.html`
      // of `weekly_chat-link-panel_report.html`); a path selection must match the tail.
      return hasParent ? f.path.endsWith(candidate.path) : basenameOf(f.path).includes(lastSegment);
    });
    // A result whose basename is exactly the selection is the likeliest
    // intent; float those above substring-only hits before the cap.
    const ordered = [...kept].sort(
      (a, b) =>
        Number(basenameOf(b.path) === lastSegment) - Number(basenameOf(a.path) === lastSegment),
    );
    return ordered.slice(0, MAX_MATCHES).map((f) => ({ path: f.path, line: candidate.line }));
  }, [candidate, exactHit, resolution, searchReady, searchSegment, searchQuery.data]);

  const pending =
    !!candidate &&
    !!conversationId &&
    serveable !== false &&
    (exactQuery.isLoading || (!!searchSegment && !searchReady && !searchQuery.isError));
  const truncated = searchReady && !!searchSegment && !!searchQuery.data?.truncated;

  return { matches, pending, truncated };
}
