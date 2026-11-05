import { useState } from "react";
import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import {
  patchUsageBoardPreferences,
  type UsageBoardPreferences,
} from "@/lib/usageContextPreferences";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";

export const USAGE_RANGES = {
  today: "Today",
  h24: "24 hours",
  d7: "7 days",
  period: "This reset period",
  d30: "30 days",
  d90: "90 days",
  custom: "Custom",
};
export type UsageRange = NonNullable<UsageBoardPreferences["range"]>;

export function usageRangeQuery(range: UsageRange): URLSearchParams {
  const params = new URLSearchParams({
    range: range.key,
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  });
  if (range.key === "custom") {
    if (range.from) params.set("from", range.from);
    if (range.to) params.set("to", range.to);
  }
  return params;
}

export function UsageRangeControl() {
  const board = useUsageContextPreferences().board;
  const range = board.range ?? { key: "d7" };
  const [custom, setCustom] = useState(range.key === "custom");
  const [from, setFrom] = useState(range.from ?? "");
  const [to, setTo] = useState(range.to ?? "");
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="flex items-center gap-2 text-sm">
        Range
        <select
          aria-label="Usage range"
          className="h-9 rounded-md border border-input bg-background px-2 text-sm"
          value={custom ? "custom" : range.key}
          onChange={(event) => {
            const key = event.target.value as UsageRange["key"];
            setCustom(key === "custom");
            if (key !== "custom") patchUsageBoardPreferences({ range: { key } });
          }}
        >
          {Object.entries(USAGE_RANGES).map(([key, name]) => (
            <option key={key} value={key}>
              {name}
            </option>
          ))}
        </select>
      </label>
      {custom ? (
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            patchUsageBoardPreferences({ range: { key: "custom", from, to } });
          }}
        >
          <Input
            type="date"
            aria-label="Usage from date"
            className="h-9 w-36"
            required
            value={from}
            max={to || undefined}
            onChange={(event) => setFrom(event.target.value)}
          />
          <span className="text-xs text-muted-foreground">to</span>
          <Input
            type="date"
            aria-label="Usage to date"
            className="h-9 w-36"
            required
            value={to}
            min={from || undefined}
            onChange={(event) => setTo(event.target.value)}
          />
          <Button size="sm" variant="outline" type="submit">
            Apply
          </Button>
        </form>
      ) : null}
    </div>
  );
}
