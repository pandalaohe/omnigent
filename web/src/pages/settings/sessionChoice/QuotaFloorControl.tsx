import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { choiceRequest, useSessionChoiceDocument } from "./sessionChoiceData";
import type { UsageAccountView } from "@/pages/usageBoard/usageBoardData";

export interface ChoiceFloor {
  until: "reset" | "manual";
  windows?: Record<string, [number, number]>;
}
export function floorActive(entry: ChoiceFloor | undefined, reading: UsageAccountView | undefined) {
  if (!entry) return false;
  if (entry.until === "manual") return true;
  const windows = Object.entries(entry.windows ?? {});
  if (!windows.length || windows.some(([, [reset]]) => reset <= Date.now() / 1000)) return false;
  if (reading?.freshness !== "fresh") return true;
  return windows.every(([id, [reset, used]]) => {
    const current = reading.windows.find((w) => w.id === id);
    return (
      current &&
      !current.past_reset &&
      (current.percent_used == null || current.percent_used > used - 5) &&
      (current.resets_at == null || Math.abs(current.resets_at - reset) <= 3600)
    );
  });
}

export function QuotaFloorControl({
  tile,
  label,
  account,
}: {
  tile: string;
  label: string;
  account?: UsageAccountView;
}) {
  const document = useSessionChoiceDocument();
  const queryClient = useQueryClient();
  const enabled = document.data?.settings?.enabled === true;
  const floors = useQuery({
    queryKey: ["sessionChoiceFloors"],
    queryFn: () => choiceRequest<Record<string, ChoiceFloor>>("/floor"),
    enabled,
    staleTime: 30_000,
    retry: false,
    refetchInterval: 60_000,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!enabled || !floors.data) return null;
  return (
    <div
      className="flex flex-wrap items-center gap-1.5"
      onClick={(event) => event.stopPropagation()}
    >
      <label className="flex items-center gap-1 text-xs">
        Floor
        <select
          aria-label={`${label} floor`}
          className="h-8 max-w-36 rounded border border-input bg-background px-1 text-xs"
          disabled={busy}
          value={floorActive(floors.data[tile], account) ? floors.data[tile].until : "kept"}
          onChange={async (event) => {
            const mode = event.target.value;
            setBusy(true);
            setError(null);
            try {
              await choiceRequest("/floor", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ tile, off: mode !== "kept", keep: mode === "manual" }),
              });
              await floors.refetch();
              await queryClient.invalidateQueries({ queryKey: ["sessionChoiceMeters"] });
            } catch (failure) {
              setError((failure as Error).message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <option value="kept">Kept</option>
          <option value="reset">Off until reset</option>
          <option value="manual">Use up</option>
        </select>
      </label>
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
