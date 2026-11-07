import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

export type AutoRotationSource = "session" | "user" | "default";
export type AutoRotationApproximatePart = "usage" | "compaction_point" | "compactions";
export type AutoRotationIneligibleReason = "not_top_level" | "archived" | "harness";

export interface AutoRotationStatus {
  eligible: boolean;
  ineligible_reason: AutoRotationIneligibleReason | null;
  harness: string | null;
  enabled: boolean;
  compactions_setting: number;
  remaining_pct_setting: number;
  sources: {
    enabled: AutoRotationSource;
    compactions: AutoRotationSource;
    remaining_pct: AutoRotationSource;
  };
  compactions: number;
  remaining_pct: number | null;
  approximate: AutoRotationApproximatePart[];
  due: boolean;
  rotation_pending: boolean;
}

export interface AutoRotationPatch {
  enabled?: "on" | "off" | "default";
  compactions?: number | "default";
  remaining_pct?: number | "default";
}

function sessionAutoRotationPath(sessionId: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/auto-rotation`;
}

async function readAutoRotationResponse(response: Response): Promise<AutoRotationStatus> {
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as AutoRotationStatus;
}

export async function getAutoRotation(
  sessionId: string,
  signal?: AbortSignal,
): Promise<AutoRotationStatus> {
  const response = await authenticatedFetch(
    sessionAutoRotationPath(sessionId),
    signal ? { signal } : undefined,
  );
  return readAutoRotationResponse(response);
}

export async function patchAutoRotation(
  sessionId: string,
  patch: AutoRotationPatch,
): Promise<AutoRotationStatus> {
  const response = await authenticatedFetch(sessionAutoRotationPath(sessionId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return readAutoRotationResponse(response);
}
