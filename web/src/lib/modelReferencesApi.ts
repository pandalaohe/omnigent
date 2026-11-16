import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export interface ModelReferenceRow {
  kind: string;
  entity_id: string;
  label: string;
  path: string;
  saved: string;
  harness?: string | null;
  host: string | null;
  outcome: string;
  resolved: string | null;
  candidates: string[];
  still_listed_on: string[];
  manual: boolean;
}
export interface ModelReferenceCheck {
  rows: ModelReferenceRow[];
  aliases: { pairs: [string, string][] };
  aliases_revision: string;
  hosts: { id: string; name: string }[];
  models: string[];
  host_config_manual: boolean;
}
export interface ModelReferenceReplaceResult {
  committed: { kind: string; entity_id: string; count: number; paths: string[] }[];
  conflicts: { kind: string; entity_id: string }[];
  failed: { kind: string; entity_id: string }[];
  remaining: number | null;
  aliases: string;
  complete: boolean;
  count: number;
}
export interface ModelReferenceReplacement {
  mode: "model" | "provider";
  from: string;
  to: string;
}

async function request<T>(suffix: string, method = "GET", body?: unknown): Promise<T> {
  const response = await authenticatedFetch(`/v1/me/model-references${suffix}`, {
    method,
    ...(body === undefined
      ? {}
      : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json() as Promise<T>;
}
export const checkModelReferences = () => request<ModelReferenceCheck>("");
export const replaceModelReferences = (body: ModelReferenceReplacement) =>
  request<ModelReferenceReplaceResult>("/replace", "POST", body);
export const saveModelProviderAliases = (pairs: [string, string][], expectedRevision?: string) =>
  request<{ pairs: [string, string][] }>("/aliases", "PUT", {
    pairs,
    expected_revision: expectedRevision,
  });
