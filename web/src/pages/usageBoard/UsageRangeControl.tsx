import { useUsageContextPreferences } from "@/hooks/useUsageContextPreferences";
import {
  patchUsageBoardPreferences,
  type UsageBoardPreferences,
} from "@/lib/usageContextPreferences";
import { Input } from "@/components/ui/input";
import { toDateInput } from "./usageBoardSelect";

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
  const today = toDateInput(Date.now());
  const from = range.from ?? today;
  const to = range.to ?? today;
  const valid = (date: string) => /^\d{4}-\d{2}-\d{2}$/.test(date) && date <= today;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="flex items-center gap-2 text-sm">
        Range
        <select
          aria-label="Usage range"
          className="h-9 rounded-md border border-input bg-background px-2 text-sm"
          value={range.key}
          onChange={(event) => {
            const key = event.target.value as UsageRange["key"];
            patchUsageBoardPreferences({
              range: key === "custom" ? { key, from, to } : { key },
            });
          }}
        >
          {Object.entries(USAGE_RANGES).map(([key, name]) => (
            <option key={key} value={key}>
              {name}
            </option>
          ))}
        </select>
      </label>
      {range.key === "custom" ? (
        <div className="flex flex-wrap items-center gap-2">
          <Input
            type="date"
            aria-label="Usage from date"
            className="h-9 w-36"
            required
            value={from}
            max={today}
            onChange={(event) => {
              const next = event.target.value;
              if (valid(next))
                patchUsageBoardPreferences({
                  range: { key: "custom", from: next, to: next > to ? next : to },
                });
            }}
          />
          <span className="text-xs text-muted-foreground">to</span>
          <Input
            type="date"
            aria-label="Usage to date"
            className="h-9 w-36"
            required
            value={to}
            min={from || undefined}
            max={today}
            onChange={(event) => {
              const next = event.target.value;
              if (valid(next) && next >= from)
                patchUsageBoardPreferences({
                  range: { key: "custom", from, to: next },
                });
            }}
          />
        </div>
      ) : null}
    </div>
  );
}
