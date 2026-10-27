import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

export interface StandingPassRecord {
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

export interface StandingPassState {
  root_id: string;
  record: StandingPassRecord;
}

export async function getStandingPass(
  sessionId: string,
  signal?: AbortSignal,
): Promise<StandingPassState> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/standing-pass`,
    { signal },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export async function changeStandingPass(
  sessionId: string,
  op: "revoke" | "grant",
  kind: string,
): Promise<StandingPassState> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/standing-pass`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op, kinds: [kind] }),
    },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}
