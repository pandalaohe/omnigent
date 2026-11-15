import { authenticatedFetch } from "./identity";

export interface ModelConnection {
  name: string;
  display_name: string;
  kind: "key" | "gateway" | "local";
  base_url: string;
  wire_api: "chat" | "responses";
  models: string[];
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
  key?: { value: string } | { env: string };
}
export const MODEL_CONNECTIONS_CHANGED = "omnigent:model-connections-changed";
async function request<T>(path = "", method = "GET", body?: unknown): Promise<T> {
  const response = await authenticatedFetch(`/v1/model-connections${path}`, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`Provider request failed (${response.status})`);
  return response.status === 204 ? (undefined as T) : response.json();
}
export const listModelConnections = () =>
  request<{ connections: ModelConnection[]; can_manage: boolean }>();
export const saveModelConnection = (name: string, draft: ConnectionDraft) =>
  request<ModelConnection>(`/${encodeURIComponent(name)}`, "PUT", draft);
export const deleteModelConnection = (name: string) =>
  request<void>(`/${encodeURIComponent(name)}`, "DELETE");
export const fetchConnectionModels = (name: string, draft: ConnectionDraft) =>
  request<{ ok: boolean; model_ids: string[]; error_kind: string | null }>("/test", "POST", {
    name,
    ...draft,
  });
