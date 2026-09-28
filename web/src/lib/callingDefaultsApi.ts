// Typed clients for the `/v1/calling-defaults` routes
// (`omnigent/server/routes/calling_defaults.py`): the cached model catalogs
// that fill the settings dropdowns, and the read-only resolution preview used
// by New Chat seeding. The chain itself is server-side only.

import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";
import type { NativeModelOption } from "./types";

/** One cached `(host, harness)` model catalog plus its staleness. */
export interface CallingDefaultsCatalogRow {
  host_id: string;
  harness: string;
  models: NativeModelOption[];
  /** Unix epoch seconds of the last successful sync; `null` when none. */
  fetched_at: number | null;
  /** The last sync failed, or the host is offline on this replica. */
  stale: boolean;
  /** The last sync's failure text (e.g. `"unsupported"`), or `null`. */
  error: string | null;
}

/** One default-sourced value the cached catalog does not offer. */
export interface CallingDefaultsProblem {
  field: string;
  setting: string;
  message: string;
}

/** One create's resolved calling triple, with per-field sources and problems. */
export interface CallingDefaultsResolution {
  agent_id: string | null;
  harness: string | null;
  model: string | null;
  effort: string | null;
  /** Source tokens keyed by field: `agent`, `model`, `effort`. */
  sources: Record<string, string>;
  problems: CallingDefaultsProblem[];
}

export interface CallingDefaultsSyncOptions {
  hostId?: string;
  harness?: string;
}

export interface CallingDefaultsResolveOptions {
  projectId?: string;
  hostId?: string;
  agentId?: string;
  harness?: string;
}

async function readCatalogRows(res: Response): Promise<CallingDefaultsCatalogRow[]> {
  if (!res.ok) throw await apiErrorFromResponse(res);
  const body = (await res.json()) as { rows?: CallingDefaultsCatalogRow[] };
  return body.rows ?? [];
}

/** Refresh the caller's cached model catalogs; both filters narrow the sync. */
export async function syncCallingDefaults(
  options: CallingDefaultsSyncOptions = {},
): Promise<CallingDefaultsCatalogRow[]> {
  const body: Record<string, string> = {};
  if (options.hostId) body.host_id = options.hostId;
  if (options.harness) body.harness = options.harness;
  const res = await authenticatedFetch("/v1/calling-defaults/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return readCatalogRows(res);
}

/** Read back the caller's cached catalogs, stale rows included. */
export async function listCallingDefaultCatalogs(): Promise<CallingDefaultsCatalogRow[]> {
  const res = await authenticatedFetch("/v1/calling-defaults/catalogs");
  return readCatalogRows(res);
}

/** Resolve one create's agent / harness / model / effort; a pure read. */
export async function resolveCallingDefaults(
  options: CallingDefaultsResolveOptions = {},
): Promise<CallingDefaultsResolution> {
  const params = new URLSearchParams();
  if (options.projectId) params.set("project_id", options.projectId);
  if (options.hostId) params.set("host_id", options.hostId);
  if (options.agentId) params.set("agent_id", options.agentId);
  if (options.harness) params.set("harness", options.harness);
  const query = params.toString();
  const res = await authenticatedFetch(`/v1/calling-defaults/resolve${query ? `?${query}` : ""}`);
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as CallingDefaultsResolution;
}
