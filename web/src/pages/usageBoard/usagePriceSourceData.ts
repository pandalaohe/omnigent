import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

export type PriceFormat = "litellm" | "openrouter" | "modelsdev";
export interface PriceSelection {
  source: "builtin" | PriceFormat | "custom";
  format?: PriceFormat | null;
  url?: string | null;
}
export interface PriceSourceStatus extends PriceSelection {
  can_edit: boolean;
  model_count: number;
  fetched_at: number | null;
  error: string | null;
}

export async function usageJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(`/v1/me/usage/${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      typeof body?.detail === "string" ? body.detail : `Usage read failed (${response.status})`,
    );
  }
  return response.json() as Promise<T>;
}

export function usePriceSource() {
  return useQuery({
    queryKey: ["usageBoard", "priceSource"],
    queryFn: () => usageJson<PriceSourceStatus>("price-source"),
    retry: false,
  });
}

export function savePriceSource(selection: PriceSelection) {
  return usageJson<PriceSourceStatus>("price-source", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(selection),
  });
}
