import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** The server-wide instructions text appended to every new session. */
export interface GlobalInstructions {
  text: string;
  revision_id: string | null;
  updated_at: number | null;
  updated_by: string | null;
  max_chars: number;
}

/** One saved revision of the global instructions text. */
export interface GlobalInstructionRevision {
  id: string;
  text: string;
  created_at: number;
  created_by: string | null;
}

// ── Query helpers ────────────────────────────────────────────────────────────

const QUERY_KEY = ["global-instructions"];
const REVISIONS_QUERY_KEY = ["global-instructions", "revisions"];

/** OmnigentError bodies carry ``{"error": {"message"}}``; keep the status otherwise. */
async function errorMessage(res: Response): Promise<string> {
  let message = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { error?: { message?: unknown } };
    if (typeof body.error?.message === "string") message = body.error.message;
  } catch {
    /* Non-JSON error body: the status fallback stands. */
  }
  return message;
}

async function fetchGlobalInstructions(): Promise<GlobalInstructions> {
  const res = await authenticatedFetch("/v1/global-instructions");
  if (!res.ok) throw new Error(await errorMessage(res));
  return (await res.json()) as GlobalInstructions;
}

async function fetchGlobalInstructionRevisions(): Promise<GlobalInstructionRevision[]> {
  const res = await authenticatedFetch("/v1/global-instructions/revisions");
  if (!res.ok) throw new Error(await errorMessage(res));
  const body = (await res.json()) as { data: GlobalInstructionRevision[] };
  return body.data;
}

// ── Hooks ────────────────────────────────────────────────────────────────────

/** Fetch the live global instructions text and its metadata. */
export function useGlobalInstructions() {
  return useQuery({
    queryKey: QUERY_KEY,
    queryFn: fetchGlobalInstructions,
    staleTime: 5_000,
  });
}

/**
 * Fetch the saved revisions, newest first.
 *
 * The revisions endpoint requires admin, so callers pass ``enabled`` once the
 * admin state is known to avoid a 403 on every non-admin page load.
 */
export function useGlobalInstructionRevisions(enabled = true) {
  return useQuery({
    queryKey: REVISIONS_QUERY_KEY,
    queryFn: fetchGlobalInstructionRevisions,
    staleTime: 5_000,
    enabled,
  });
}

/** PUT /v1/global-instructions — save a new revision (empty text turns it off). */
export function useSaveGlobalInstructions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (text: string) => {
      const res = await authenticatedFetch("/v1/global-instructions", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) throw new Error(await errorMessage(res));
      return (await res.json()) as GlobalInstructions;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });
}
