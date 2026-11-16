import { authenticatedFetch } from "./identity";

export interface ModelUsage {
  users: number;
  slots: { feature: string; slot: string; users: number }[];
}
export interface ModelRow {
  key: string;
  display_name: string;
  model_id: string;
  status: "config" | "unchecked" | "listed" | "gone" | "not_listed";
  used_by?: ModelUsage;
}
export interface ModelCatalog {
  fetched_at: string | null;
  ids: string[];
  complete?: boolean;
}
export interface ModelConnection {
  name: string;
  display_name: string;
  kind: "key" | "gateway" | "local";
  base_url: string;
  wire_api: "chat" | "responses";
  models: ModelRow[];
  catalog?: ModelCatalog | null;
  used_by?: number;
  model_previous_ids?: Record<string, string[]>;
  key_set: boolean;
  key_source: string;
  key_env?: string;
  editable: boolean;
}
export interface ConnectionDraft {
  display_name: string;
  kind: ModelConnection["kind"];
  base_url: string;
  wire_api: ModelConnection["wire_api"];
  models: Record<string, string>;
  model_display_names?: Record<string, string>;
  key?: { value: string } | { env: string };
}
export const MODEL_CONNECTIONS_CHANGED = "omnigent:model-connections-changed";
export class ModelConnectionError extends Error {
  status: number;
  detail: { kind?: string; rows?: Record<string, ModelUsage>; users?: number };
  constructor(
    status: number,
    detail: { kind?: string; rows?: Record<string, ModelUsage>; users?: number },
  ) {
    super(
      status === 409
        ? "Provider is in use or has changed. Review the affected models."
        : `Provider request failed (${status})`,
    );
    this.status = status;
    this.detail = detail;
  }
}
async function request<T>(path = "", method = "GET", body?: unknown): Promise<T> {
  const response = await authenticatedFetch(`/v1/model-connections${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!response.ok) {
    const errorBody = await response.json().catch(() => ({}));
    throw new ModelConnectionError(response.status, errorBody.detail ?? {});
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
export const listModelConnections = () =>
  request<{ connections: ModelConnection[]; can_manage: boolean }>();
export const saveModelConnection = (name: string, draft: ConnectionDraft, create = false) =>
  request<ModelConnection>(
    `/${encodeURIComponent(name)}${create ? "?create=true" : ""}`,
    "PUT",
    draft,
  );
export const refreshModelCatalog = (name: string) =>
  request<ModelConnection | { ok: false; error_kind: string }>(
    `/${encodeURIComponent(name)}/refresh`,
    "POST",
  );
export const getModelCatalog = (name: string) =>
  request<ModelCatalog>(`/${encodeURIComponent(name)}/catalog`);
export const deleteModelConnection = (name: string) =>
  request<void>(`/${encodeURIComponent(name)}`, "DELETE");
export const fetchConnectionModels = (name: string, draft: ConnectionDraft) =>
  request<{ ok: boolean; model_ids: string[]; error_kind: string | null }>("/test", "POST", {
    name,
    ...draft,
  });
