import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export interface StandingApprovalRecord {
  kinds: string[];
  exceptions: string[];
  since: number;
  events: {
    op: "materialise" | "carry" | "revoke" | "grant";
    kinds: string[];
    source: string;
    at: number;
  }[];
}

export interface StandingApprovalState {
  root_id: string;
  record: StandingApprovalRecord;
}

export async function getStandingApproval(
  sessionId: string,
  signal?: AbortSignal,
): Promise<StandingApprovalState> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/standing-approval`,
    { signal },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export async function changeStandingApproval(
  sessionId: string,
  op: "revoke" | "grant",
  kind: string,
): Promise<StandingApprovalState> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/standing-approval`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op, kinds: [kind] }),
    },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}
