import { authenticatedFetch } from "./identity";
import { ApiError, apiErrorFromResponse } from "./sessionsApi";

export interface AgentFolderField {
  key: string;
  present: boolean;
  value: string | number | boolean | null;
  writable: boolean;
  undo_available: boolean;
}

export interface AgentFolderSettings {
  family: string;
  root: string;
  choosable: boolean;
  exists: boolean;
  read_only_reason: string | null;
  revision: string | null;
  fields: AgentFolderField[];
  account: { kind: string; label: string } | null;
}

export type AgentFolderChange =
  | { action: "write"; key: string; value: AgentFolderField["value"]; revision: string | null }
  | { action: "undo"; key: string }
  | { action: "root"; root: string };

export const agentFolderQueryKey = (hostId: string, harness: string) =>
  ["agent-folder-settings", hostId, harness] as const;

export async function requestAgentFolderSettings(
  hostId: string,
  harness: string,
  change?: AgentFolderChange,
): Promise<AgentFolderSettings> {
  const { action, ...body } = change ?? { action: "read" };
  const suffix = action === "undo" || action === "root" ? `/${action}` : "";
  const response = await authenticatedFetch(
    `/v1/hosts/${encodeURIComponent(hostId)}/agent-settings/${encodeURIComponent(harness)}${suffix}`,
    change
      ? {
          method: action === "undo" ? "POST" : "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      : undefined,
  );
  if (!response.ok) {
    const error = await response
      .clone()
      .json()
      .catch(() => null);
    if (typeof error?.error === "string") throw new ApiError(error.error, response.status, null);
    throw await apiErrorFromResponse(response);
  }
  return response.json();
}
