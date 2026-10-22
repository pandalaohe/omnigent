import { authenticatedFetch } from "./identity";

export interface AnnotationReceiver {
  session_id: string | null;
  revision: number;
  set_at: number | null;
  target_editable?: boolean | null;
}

export interface ConnectedApp {
  grant_id: string;
  client_name: string;
  label: string | null;
  scope: string;
  created_at: number;
  expires_at: number | null;
  last_used_at: number | null;
}

export class BrowserAnnotationsApiError extends Error {
  readonly status: number;

  constructor(status: number) {
    super(`Browser annotations request failed (${status})`);
    this.status = status;
  }
}

async function jsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) throw new BrowserAnnotationsApiError(response.status);
  return (await response.json()) as T;
}

export async function getAnnotationReceiver(sessionId?: string): Promise<AnnotationReceiver> {
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  return jsonResponse<AnnotationReceiver>(
    await authenticatedFetch(`/v1/me/annotation-receiver${query}`),
  );
}

export async function setAnnotationReceiver(
  sessionId: string | null,
  revision: number,
): Promise<AnnotationReceiver> {
  return jsonResponse<AnnotationReceiver>(
    await authenticatedFetch("/v1/me/annotation-receiver", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, revision }),
    }),
  );
}

export async function getConnectedApps(): Promise<ConnectedApp[]> {
  const data = await jsonResponse<{ apps: ConnectedApp[] }>(
    await authenticatedFetch("/v1/me/connected-apps"),
  );
  return data.apps;
}

export async function revokeConnectedApp(grantId: string): Promise<void> {
  await jsonResponse<{ revoked: boolean }>(
    await authenticatedFetch(`/v1/me/connected-apps/${encodeURIComponent(grantId)}`, {
      method: "DELETE",
    }),
  );
}
