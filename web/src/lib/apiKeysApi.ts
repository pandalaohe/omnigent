import { authenticatedFetch } from "./identity";

export interface ManagedApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  created_at: number;
  expires_at: number | null;
  last_used_at: number | null;
  regenerated_at: number | null;
}

export interface ApiKeyList {
  keys: ManagedApiKey[];
  read_only: { name: string; scopes: string[]; source: string; last_used_at: number | null }[];
  available_scopes: string[];
}

export async function apiKeysRequest<T>(path = "", method = "GET", body?: object): Promise<T> {
  const response = await authenticatedFetch(`/v1/me/api-keys${path}`, {
    method,
    cache: "no-store",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(
      typeof data?.detail === "string" ? data.detail : `Request failed (${response.status})`,
    );
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
