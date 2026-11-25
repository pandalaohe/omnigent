import { useId } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Switch } from "@/components/ui/switch";
import { useIsAdmin } from "@/hooks/useIsAdmin";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isSingleUserMode, parseTelemetryStatus } from "@/lib/capabilities";
import { authenticatedFetch } from "@/lib/identity";
import { interfaceText } from "@/lib/interfaceText";

const queryKey = ["usage-telemetry"];

export function UsageTelemetrySetting() {
  const info = useServerInfo();
  const isAdmin = useIsAdmin() || isSingleUserMode(info);
  const client = useQueryClient();
  const labelId = useId();
  const descriptionId = useId();
  const status = useQuery({
    queryKey,
    queryFn: async () => {
      const response = await authenticatedFetch("/v1/info", { cache: "no-store" });
      if (!response.ok) throw new Error(interfaceText("usageTelemetryUnavailable"));
      return parseTelemetryStatus((await response.json()).telemetry) ?? null;
    },
  });
  const update = useMutation({
    mutationFn: async (enabled: boolean) => {
      const response = await authenticatedFetch("/v1/system/telemetry", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ telemetry_enabled: enabled }),
      });
      const saved = response.ok ? parseTelemetryStatus(await response.json()) : undefined;
      if (!saved) throw new Error(interfaceText("usageTelemetrySaveFailed"));
      return saved;
    },
    onSuccess: (saved) => client.setQueryData(queryKey, saved),
    onError: () => void client.invalidateQueries({ queryKey }),
  });
  const value = status.data;

  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span id={labelId} className="text-ui font-medium">
          {interfaceText("usageTelemetry")}
        </span>
        {value &&
          (isAdmin ? (
            <Switch
              checked={value.enabled}
              disabled={value.locked || update.isPending}
              onCheckedChange={(enabled) => update.mutate(enabled)}
              aria-labelledby={labelId}
              aria-describedby={descriptionId}
            />
          ) : (
            <span className="text-sm text-muted-foreground">
              {interfaceText(value.enabled ? "usageTelemetryOn" : "usageTelemetryOff")}
            </span>
          ))}
        {value?.locked && (
          <span className="text-sm text-muted-foreground">
            {interfaceText("usageTelemetryLocked", { source: value.source })}
          </span>
        )}
      </div>
      <p id={descriptionId} className="mt-1 text-sm text-muted-foreground">
        {interfaceText("usageTelemetryDescription")}
      </p>
      {status.isPending && <p role="status">{interfaceText("usageTelemetryLoading")}</p>}
      {(status.isError || value === null) && (
        <p role="alert">{interfaceText("usageTelemetryUnavailable")}</p>
      )}
      {update.isPending && <p role="status">{interfaceText("usageTelemetrySaving")}</p>}
      {update.isSuccess && <p role="status">{interfaceText("usageTelemetrySaved")}</p>}
      {update.isError && <p role="alert">{interfaceText("usageTelemetrySaveFailed")}</p>}
    </div>
  );
}
